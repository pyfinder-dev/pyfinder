"""Durably record one ShakeMap submission and observations of its exact job.

This is a callable integration building block, not a scheduler. The caller owns
attempt identity, polling cadence, and the interpretation of terminal outcomes.
Scheduled rows, scientific inputs, service configuration, and products are never
modified here. A committed intent is never automatically submitted a second time.
"""

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
import sqlite3
import threading

from .shakemap_client import (
    AcceptedJob,
    ShakeMapHTTPError,
    ShakeMapSubmissionUncertain,
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

        records = [self._decode(row) for row in rows]
        return [
            record for record in records
            if record["submission_state"] != "REJECTED"
            and (
                record["observation"] is None
                or record["observation"]["details"]["status"] not in {"SUCCESS", "FAILED"}
                or record["last_error"] is not None
            )
        ]

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
        when that is appropriate belongs to the eventual retry policy.
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
            ''', (state, type(error).__name__, _now(), attempt_id))

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
            except Exception as error:
                # A timeout, missing retained job, or malformed response is a
                # monitoring error; it is not evidence of a FAILED calculation.
                with self._connection:
                    self._connection.execute('''
                        UPDATE shakemap_submissions
                        SET last_error = ?, updated_at = ? WHERE attempt_id = ?
                    ''', (type(error).__name__, _now(), attempt_id))
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
