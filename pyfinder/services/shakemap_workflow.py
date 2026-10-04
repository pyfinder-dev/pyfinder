"""Durably record one ShakeMap submission and observations of its exact job.

This is a callable integration building block, not a scheduler. The caller owns
attempt identity, polling cadence, and the interpretation of terminal outcomes.
This helper retains exact prepared input bytes and association records, but
does not change scheduled-row lifecycle, canonical input files, service
configuration or products. A committed intent is never automatically sent twice.
"""

from base64 import b64decode, b64encode
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
import sqlite3
import threading

from .shakemap_diagnostics import error_diagnostic, regional_configuration_failure

from .shakemap_client import (
    AcceptedJob,
    ShakeMapHTTPError,
    ShakeMapSubmissionUncertain,
    ShakeMapProtocolError,
)


class ShakeMapRecordingError(RuntimeError):
    """Remote acceptance is known in memory but could not be committed locally."""

    def __init__(self, job, acknowledgement):
        super().__init__("ShakeMap accepted the job but its identity could not be persisted")
        # Keep recovery evidence for the caller without dumping response bodies
        # into log messages. The database still contains the pre-POST intent.
        self.job = job
        self.acknowledgement = acknowledgement


def _now():
    """Use explicit UTC for audit timestamps; these do not schedule work."""
    return datetime.now(timezone.utc).isoformat()


class ShakeMapWorkflow:
    """Record submissions in an explicitly supplied workflow database.

    One instance serializes its synchronous operations, including finite HTTP
    requests. Use one monitoring owner per database; this is not a distributed
    worker/lease system. A unique attempt key also prevents duplicate POSTs from
    independently opened instances. No implicit database path is selected.
    """

    def __init__(self, db_path, client):
        # SQLite accepts empty and in-memory names, but those cannot retain an
        # intent across process restarts. Require an actual filesystem path and
        # do not coerce None or arbitrary objects into surprising filenames.
        database_path = os.fspath(db_path)
        if (
            not isinstance(database_path, str)
            or not database_path.strip()
            or database_path == ":memory:"
            or database_path.startswith("file:")
        ):
            raise ValueError("db_path must name a persistent filesystem database")

        self.database_path = os.path.abspath(database_path)
        self.client = client
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(database_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row

        # This additive table can share the scheduler's SQLite file. It has no
        # cascading link to scheduled rows: their cleanup or forced-restart
        # failure must not erase an external job that can still be running.
        try:
            self._connection.execute("PRAGMA journal_mode=WAL")
            with self._connection:
                # Keep schema inspection and addition atomic across connections.
                self._connection.execute("BEGIN IMMEDIATE")
                self._connection.execute('''
                    CREATE TABLE IF NOT EXISTS shakemap_submissions (
                        attempt_id TEXT PRIMARY KEY NOT NULL,
                        request_json TEXT NOT NULL,
                        submission_state TEXT NOT NULL,
                        internal_sequence INTEGER,
                        acknowledgement_json TEXT,
                        observation_json TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        observed_at TEXT,
                        last_error TEXT
                    )
                ''')
                self._connection.execute("""
                    CREATE TABLE IF NOT EXISTS shakemap_scheduled_attempts (
                        attempt_id TEXT PRIMARY KEY NOT NULL,
                        event_id TEXT NOT NULL,
                        service TEXT NOT NULL,
                        current_delay_time REAL NOT NULL,
                        finalized INTEGER NOT NULL DEFAULT 0,
                        prepared_json TEXT
                    )
                """)
                # One optional global child belongs to the same scheduler execution.
                # The original submission and its retained evidence never change.
                self._connection.execute("""
                    CREATE TABLE IF NOT EXISTS shakemap_global_fallbacks (
                        execution_id TEXT PRIMARY KEY NOT NULL,
                        attempt_id TEXT UNIQUE NOT NULL,
                        regional_sequence INTEGER NOT NULL,
                        prepared_json TEXT NOT NULL,
                        evidence_json TEXT NOT NULL
                    )
                """)
                # Local capture/finalization failures keep terminal jobs pending
                # without turning their stored result into another HTTP read.
                self._connection.execute("""
                    CREATE TABLE IF NOT EXISTS shakemap_evidence_holds (
                        attempt_id TEXT PRIMARY KEY NOT NULL,
                        error TEXT NOT NULL
                    )
                """)
                columns = {
                    row[1] for row in self._connection.execute(
                        "PRAGMA table_info(shakemap_scheduled_attempts)"
                    )
                }
                if "prepared_json" not in columns:
                    self._connection.execute(
                        "ALTER TABLE shakemap_scheduled_attempts ADD COLUMN prepared_json TEXT"
                    )
        except BaseException:
            self._connection.close()
            raise

    def close(self):
        """Close only this helper's connection, after its operations finish."""
        with self._lock:
            self._connection.close()

    @staticmethod
    def _decode(row):
        """Expose structured request/evidence while keeping SQL details private."""
        if row is None:
            return None

        record = dict(row)
        for field in ("request", "acknowledgement", "observation"):
            payload = record.pop(f"{field}_json")
            record[field] = json.loads(payload) if payload is not None else None

        return record

    def get(self, attempt_id):
        """Read an attempt without making any service request."""
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM shakemap_submissions WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            return self._decode(row)

    def unresolved(self):
        """List unacknowledged or nonterminal attempts for caller-owned recovery.

        SUBMITTING may mean an in-flight request or a crash before/after sending.
        UNCERTAIN has the same no-replay requirement. Neither is classified as
        a failed calculation, nor can its sequence be guessed from event status.
        """
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM shakemap_submissions ORDER BY created_at, attempt_id"
            ).fetchall()

            held = {row[0] for row in self._connection.execute(
                "SELECT attempt_id FROM shakemap_evidence_holds"
            )}

        records = [self._decode(row) for row in rows]
        return [
            record for record in records
            if record["submission_state"] != "REJECTED"
            and (
                record["observation"] is None
                or record["observation"]["details"]["status"] not in {"SUCCESS", "FAILED"}
                or record["last_error"] is not None
                or record["attempt_id"] in held
            )
        ]

    def bind_scheduled_attempt(self, attempt_id, event_id, service, current_delay_time):
        """Retain the scheduled execution owning an impending external request.

        The caller supplies its execution token as attempt_id. This association
        deliberately outlives terminal scheduled-row cleanup and never changes
        the public calculation ID sent to ShakeMap.
        """
        if not isinstance(attempt_id, str) or not attempt_id.strip():
            raise ValueError("attempt_id must be a nonempty execution token")

        with self._lock, self._connection:
            # Reserve the write before checking ownership. Another connection
            # cannot retire/reassign the scheduled row between the check and bind.
            self._connection.execute("BEGIN IMMEDIATE")
            previous = self._connection.execute("""
                SELECT event_id, service, current_delay_time
                FROM shakemap_scheduled_attempts WHERE attempt_id = ?
            """, (attempt_id,)).fetchone()

            if previous is not None:
                if tuple(previous) != (event_id, service, current_delay_time):
                    raise ValueError("attempt_id already belongs to another scheduled row")
                return

            owned = self._connection.execute("""
                SELECT 1 FROM event_tracker
                WHERE event_id = ? AND service = ? AND current_delay_time = ?
                    AND execution_id = ? AND status = 'processing'
            """, (event_id, service, current_delay_time, attempt_id)).fetchone()
            if owned is None:
                raise ValueError("scheduled attempt does not own a processing row")

            self._connection.execute("""
                INSERT INTO shakemap_scheduled_attempts (
                    attempt_id, event_id, service, current_delay_time
                ) VALUES (?, ?, ?, ?)
            """, (attempt_id, event_id, service, current_delay_time))

    def pending_scheduled_attempts(self):
        """Read associations still requiring local lifecycle reconciliation.

        Include associations whose external result is already terminal: a crash
        may have interrupted the later local transition. External monitoring uses
        unresolved() separately and survives association finalization.
        """
        with self._lock:
            rows = self._connection.execute("""
                SELECT attempt_id, event_id, service, current_delay_time, finalized
                FROM shakemap_scheduled_attempts
                WHERE finalized = 0 ORDER BY attempt_id
            """).fetchall()
            return [dict(row) for row in rows]

    def finish_scheduled_attempt(self, attempt_id):
        """Record completed local reconciliation without deleting job evidence."""
        with self._lock, self._connection:
            return self._connection.execute("""
                UPDATE shakemap_scheduled_attempts SET finalized = 1
                WHERE attempt_id = ? AND finalized = 0
            """, (attempt_id,)).rowcount

    def prepare_scheduled_submission(
        self,
        attempt_id,
        event_id,
        files,
        *,
        configuration="global",
        overwrite=True,
    ):
        """Save a complete immutable handoff before the scheduler permits POST.

        Retaining the actual bytes allows a later deliberate request with the
        same public ID to wait for its predecessor without rerunning FinDer or
        rereading mutable input files. Preparing is not evidence of submission.
        """
        self.client.validate_submission(
            event_id, files, configuration=configuration, overwrite=overwrite,
        )
        files = dict(files)
        prepared = {
            "event_id": event_id,
            "configuration": configuration,
            "overwrite": overwrite,
            "base_url": self.client.base_url,
            "files": {
                name: b64encode(content).decode("ascii")
                for name, content in files.items()
            },
        }
        prepared_json = json.dumps(prepared, sort_keys=True)

        with self._lock, self._connection:
            # Ownership was recorded by bind_scheduled_attempt. Serialize this
            # read/write so independent connections cannot replace its payload.
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute("""
                SELECT prepared_json FROM shakemap_scheduled_attempts
                WHERE attempt_id = ?
            """, (attempt_id,)).fetchone()
            if row is None:
                raise ValueError("prepare requires a bound scheduled attempt")

            if row["prepared_json"] is not None:
                if json.loads(row["prepared_json"]) != prepared:
                    raise ValueError("attempt_id already has a different prepared request")
                return

            self._connection.execute("""
                UPDATE shakemap_scheduled_attempts SET prepared_json = ?
                WHERE attempt_id = ?
            """, (prepared_json, attempt_id))

    def prepared_scheduled_submissions(self, *, attempt_ids=None):
        """Return exact retained requests in association insertion order.

        Include finalized associations: an unresolved predecessor may still
        protect its public ID after local restart handling retires its row.
        The scheduler owns eligibility and must consult submission evidence.
        An optional selection avoids loading settled historical file bundles on
        each monitoring pass; omitting it still exposes the retained history.
        """
        selected_ids = None if attempt_ids is None else tuple(dict.fromkeys(attempt_ids))
        if selected_ids == ():
            return []

        with self._lock:
            query = """
                SELECT rowid AS insertion_order, attempt_id, prepared_json
                FROM shakemap_scheduled_attempts WHERE prepared_json IS NOT NULL
            """
            if selected_ids is None:
                rows = self._connection.execute(query + " ORDER BY rowid").fetchall()
            else:
                # Keep each statement below SQLite's traditional parameter limit.
                # Apply selection in SQL so excluded native bundles never cross
                # into Python; restore global insertion order across the chunks.
                rows = []
                for offset in range(0, len(selected_ids), 900):
                    chunk = selected_ids[offset:offset + 900]
                    placeholders = ", ".join("?" for _ in chunk)
                    rows.extend(self._connection.execute(
                        query + f" AND attempt_id IN ({placeholders})",
                        chunk,
                    ).fetchall())
                rows.sort(key=lambda row: row["insertion_order"])

        requests = []
        for row in rows:
            execution_id = row["attempt_id"]
            with self._lock:
                fallback = self._connection.execute(
                    "SELECT attempt_id, prepared_json FROM shakemap_global_fallbacks WHERE execution_id = ?",
                    (execution_id,),
                ).fetchone()
            prepared = json.loads(row["prepared_json"] if fallback is None else fallback["prepared_json"])
            prepared["attempt_id"] = execution_id if fallback is None else fallback["attempt_id"]
            prepared["files"] = {
                name: b64decode(content, validate=True)
                for name, content in prepared["files"].items()
            }
            requests.append(prepared)

        return requests

    def submission_attempt_id(self, execution_id):
        """Return the active native attempt without changing scheduler ownership."""
        with self._lock:
            row = self._connection.execute(
                "SELECT attempt_id FROM shakemap_global_fallbacks WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            return execution_id if row is None else row[0]

    def execution_for_attempt(self, attempt_id):
        """Keep a global child's scheduler identity available after local cleanup."""
        with self._lock:
            row = self._connection.execute(
                "SELECT execution_id FROM shakemap_global_fallbacks WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            return attempt_id if row is None else row[0]

    def attempt_chain(self, execution_id):
        """Read both native outcomes for one execution, preserving submission order."""
        identities = [execution_id]
        active = self.submission_attempt_id(execution_id)
        if active != execution_id:
            identities.append(active)
        return [record for identity in identities if (record := self.get(identity)) is not None]

    def prepare_global_fallback(self, execution_id, evidence):
        """Reserve one explicit global request only after regional evidence capture.

        The retained original input bytes and overwrite choice are reused. This
        transaction does not POST; the ordinary intent barrier still owns that.
        """
        if not evidence:
            raise ValueError("Regional evidence must be retained before global recovery")
        with self._lock, self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            if self.submission_attempt_id(execution_id) != execution_id:
                return self.submission_attempt_id(execution_id)
            regional = self.get(execution_id)
            if regional is None or not regional_configuration_failure(regional):
                raise ValueError("Regional recovery requires confirmed configuration failure")
            row = self._connection.execute(
                "SELECT prepared_json FROM shakemap_scheduled_attempts WHERE attempt_id = ?",
                (execution_id,),
            ).fetchone()
            if row is None or row[0] is None:
                raise ValueError("Regional recovery requires retained prepared inputs")
            prepared = json.loads(row[0])
            prepared["configuration"] = "global"
            attempt_id = execution_id + ":global"
            if self.get(attempt_id) is not None:
                raise ValueError("Global recovery identity already belongs to another submission")
            self._connection.execute("""
                INSERT INTO shakemap_global_fallbacks
                    (execution_id, attempt_id, regional_sequence, prepared_json, evidence_json)
                VALUES (?, ?, ?, ?, ?)
            """, (execution_id, attempt_id, regional["internal_sequence"],
                  json.dumps(prepared, sort_keys=True), json.dumps(evidence, sort_keys=True)))
            return attempt_id

    def submit(
        self,
        attempt_id,
        event_id,
        files,
        *,
        configuration="global",
        overwrite=True,
    ):
        """Record intent, then POST at most once for this caller-owned attempt.

        Repeating an identical call returns its durable record without network
        access. Reusing the attempt key for different inputs/selections raises
        ValueError. A deliberate new attempt requires a new caller key; choosing
        when that is appropriate belongs to the caller's retry policy.
        """
        if not isinstance(attempt_id, str) or not attempt_id.strip():
            raise ValueError("attempt_id must be a nonempty caller-owned string")

        # Validate before reserving an attempt, then copy the mapping for both
        # hashing and sending. Its byte values are immutable, so subsequent caller
        # edits to the mapping cannot alter the request behind its fingerprint.
        self.client.validate_submission(
            event_id, files, configuration=configuration, overwrite=overwrite,
        )
        files = dict(files)
        request = {
            "base_url": self.client.base_url,
            "event_id": event_id,
            "configuration": configuration,
            "overwrite": overwrite,
            "files": {
                name: {"sha256": sha256(content).hexdigest(), "size": len(content)}
                for name, content in files.items()
            },
        }
        request_json = json.dumps(request, sort_keys=True)

        with self._lock:
            now = _now()
            # The unique insert and its commit are the permission to send. A
            # failed insert/commit stops before POST. An existing key, even an
            # unresolved intent after restart, never grants permission to replay.
            with self._connection:
                inserted = self._connection.execute('''
                    INSERT INTO shakemap_submissions (
                        attempt_id, request_json, submission_state, created_at, updated_at
                    ) VALUES (?, ?, 'SUBMITTING', ?, ?)
                    ON CONFLICT(attempt_id) DO NOTHING
                ''', (attempt_id, request_json, now, now)).rowcount

            if not inserted:
                record = self.get(attempt_id)
                if record["request"] != request:
                    raise ValueError("attempt_id already belongs to a different ShakeMap request")
                return record

            try:
                job, acknowledgement = self.client.submit(
                    event_id, files, configuration=configuration, overwrite=overwrite,
                )
            except ShakeMapSubmissionUncertain as error:
                self._record_submission_error(attempt_id, "UNCERTAIN", error)
                raise
            except ShakeMapHTTPError as error:
                self._record_submission_error(attempt_id, "REJECTED", error)
                raise
            # Unexpected interruption deliberately leaves SUBMITTING. Lack of
            # a recognized response cannot prove the remote request was absent.

            try:
                with self._connection:
                    self._connection.execute('''
                        UPDATE shakemap_submissions
                        SET submission_state = 'ACCEPTED', internal_sequence = ?,
                            acknowledgement_json = ?, updated_at = ?, last_error = NULL
                        WHERE attempt_id = ?
                    ''', (
                        job.internal_sequence, json.dumps(acknowledgement),
                        _now(), attempt_id,
                    ))
            except Exception as error:
                raise ShakeMapRecordingError(job, acknowledgement) from error

            return self.get(attempt_id)

    def _record_submission_error(self, attempt_id, state, error):
        """Retain the classification without copying arbitrary response bodies."""
        with self._connection:
            self._connection.execute('''
                UPDATE shakemap_submissions
                SET submission_state = ?, last_error = ?, updated_at = ?
                WHERE attempt_id = ?
            ''', (state, error_diagnostic(error, operation="submission"), _now(), attempt_id))

    def retain_local_error(self, attempt_id, error):
        """Hold replacement until immutable capture and local finalization succeed."""
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO shakemap_evidence_holds VALUES (?, ?)",
                (attempt_id, error_diagnostic(error, operation="evidence")),
            )

    def evidence_pending(self, attempt_id):
        """A local evidence failure cannot release the public calculation ID."""
        with self._lock:
            return self._connection.execute(
                "SELECT 1 FROM shakemap_evidence_holds WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone() is not None

    def clear_local_error(self, attempt_id):
        """Release only the local hold after successful capture/finalization."""
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM shakemap_evidence_holds WHERE attempt_id = ?",
                (attempt_id,),
            )

    def poll(self, attempt_id):
        """Persist one observation of the accepted sequence, with no wait loop.

        Read failures leave the last known service evidence intact and propagate
        to the caller. SUCCESS here is native job evidence, not proof of copied
        products, notification delivery, or scheduled-workflow completion.
        """
        with self._lock:
            record = self.get(attempt_id)
            if record is None:
                raise KeyError(attempt_id)
            if record["submission_state"] != "ACCEPTED":
                raise ValueError("poll requires a durably accepted ShakeMap sequence")
            if record["request"]["base_url"] != self.client.base_url:
                raise ValueError("this attempt belongs to a different ShakeMap endpoint")

            job = AcceptedJob(
                record["request"]["event_id"], record["internal_sequence"],
            )
            try:
                status = self.client.poll(job)
                details = status.details
                observed = (details.get("configuration") or {}).get("selected")
                provenance = details.get("provenance")
                if provenance is not None:
                    if (provenance.get("event_id") != job.event_id
                            or provenance.get("internal_sequence") != job.internal_sequence):
                        raise ShakeMapProtocolError("Provenance belongs to another calculation")
                    selected = (provenance.get("configuration") or {}).get("selected")
                    if selected is not None and selected != record["request"]["configuration"]:
                        raise ShakeMapProtocolError("Provenance configuration differs from the request")
                if observed is not None and observed != record["request"]["configuration"]:
                    raise ShakeMapProtocolError("Observed configuration differs from the request")
            except Exception as error:
                # A timeout, missing retained job, or malformed response is a
                # monitoring error; it is not evidence of a FAILED calculation.
                with self._connection:
                    self._connection.execute('''
                        UPDATE shakemap_submissions
                        SET last_error = ?, updated_at = ? WHERE attempt_id = ?
                    ''', (error_diagnostic(error, operation="observation"), _now(), attempt_id))
                raise

            observation = {"scope": status.scope, "details": status.details}
            now = _now()
            with self._connection:
                self._connection.execute('''
                    UPDATE shakemap_submissions
                    SET observation_json = ?, observed_at = ?, updated_at = ?, last_error = NULL
                    WHERE attempt_id = ?
                ''', (json.dumps(observation), now, now, attempt_id))

            return self.get(attempt_id)
