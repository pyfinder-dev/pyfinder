"""Small durable terminal-delivery ledger; SMTP never runs in capture()."""

from datetime import datetime, timezone
import json
import hashlib
import logging
from pathlib import Path
import sqlite3
import threading

from pyfinder.services.alert import deliver_message, render_message
from pyfinder.services.alert_evidence import read_regular, identity


class AlertService:
    """One continuous-process owner persists intent before any SMTP call.

    Pending work survives restart. A previously claimed send becomes UNKNOWN:
    the process cannot know whether the SMTP server accepted it before a crash.
    Definite failures may be explicitly requeued; ambiguous or partial delivery
    is never replayed automatically. This does not promise exactly-once SMTP.
    """

    def __init__(self, db_path, evidence_store, settings=None, *, logger=None, smtp_factory=None):
        database = Path(db_path)
        if not database.is_absolute() or str(db_path) == ":memory:":
            raise ValueError("Alert ledger requires an explicit absolute database path")
        self.evidence_store = evidence_store
        self.settings = settings
        self.logger = logger or logging.getLogger(__name__)
        self.smtp_factory = smtp_factory
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(str(db_path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._connection:
            self._connection.execute("""
                CREATE TABLE IF NOT EXISTS alert_evidence (
                    execution_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
                    diagnostic TEXT NOT NULL, bundle TEXT NOT NULL,
                    PRIMARY KEY (execution_id, attempt_id)
                )
            """)
            self._connection.execute("""
                CREATE TABLE IF NOT EXISTS alert_deliveries (
                    execution_id TEXT NOT NULL, audience TEXT NOT NULL,
                    state TEXT NOT NULL, payload TEXT NOT NULL,
                    message_id TEXT NOT NULL, result TEXT,
                    PRIMARY KEY (execution_id, audience)
                )
            """)
            self._connection.execute(
                "UPDATE alert_deliveries SET state='UNKNOWN' WHERE state='SENDING'"
            )

    def capture_finder(self, execution_id, files):
        return self.evidence_store.capture_finder(execution_id, files)

    def capture(self, record, diagnostic, *, execution_id, final):
        """Retain this native attempt before permitting a same-ID replacement."""
        bundle = self.evidence_store.capture_shakemap(record)
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO alert_evidence VALUES (?, ?, ?, ?)
                   ON CONFLICT(execution_id, attempt_id) DO UPDATE SET
                       diagnostic=excluded.diagnostic, bundle=excluded.bundle""",
                (execution_id, record["attempt_id"], json.dumps(diagnostic), json.dumps(bundle)),
            )
            if final:
                self._enqueue(execution_id, diagnostic)
        return bundle

    def enqueue(self, execution_id, diagnostic, evidence=None):
        """Also accept final local failures that never obtained a native job."""
        with self._lock, self._connection:
            self._enqueue(execution_id, diagnostic)

    def _enqueue(self, execution_id, diagnostic):
        attempts = self._connection.execute(
            "SELECT diagnostic, bundle FROM alert_evidence WHERE execution_id=? ORDER BY rowid",
            (execution_id,),
        ).fetchall()
        bundles = [json.loads(row["bundle"]) for row in attempts]
        finder = self.evidence_store.load("finder:" + execution_id)
        if finder is not None:
            bundles.insert(0, finder)
        summary = self.evidence_store.load("finder-summary:" + execution_id)
        if summary is not None:
            bundles.insert(1 if finder is not None else 0, summary)
        report = dict(diagnostic)
        if summary is not None:
            report["finder_solution"] = json.loads(read_regular(
                Path(summary["directory"]) / "finder-summary.json",
            ))
        if finder is not None and "event-context.json" in finder["files"]:
            report["event_context"] = json.loads(read_regular(
                Path(finder["directory"]) / "event-context.json",
            ))
        # Preserve the shared projection's chain as supplied. A later terminal
        # observation replaces the current evidence pointer; prior immutable
        # bundles and already-enqueued interruption reports remain unchanged.
        report.setdefault("attempts", [json.loads(row["diagnostic"]) for row in attempts])
        report["evidence"] = [bundle["metadata"] for bundle in bundles]
        report["reported_at_utc"] = datetime.now(timezone.utc).isoformat()
        # No recipients or credentials are serialized into scientific evidence.
        audiences = [] if self.settings is None else [
            audience for audience in self.settings.lists
            if diagnostic.get("final_outcome", diagnostic.get("status")) in audience["outcomes"]
        ]
        suppressed = not audiences
        if suppressed:
            audiences = [{"name": "disabled-or-unrouted", "required_attachments": []}]
        for audience in audiences:
            # Required native artifacts belong to the final attempt, not a
            # preceding regional failure; original FinDer input belongs to both.
            names = set(bundles[-1]["files"]) if bundles else set()
            if finder is not None:
                names.update(finder["files"])
            if summary is not None:
                names.update(summary["files"])
            missing = set(audience["required_attachments"]) - names
            state = "BLOCKED" if missing else "PENDING"
            if suppressed:
                state = "SUPPRESSED"
            payload = {
                "diagnostic": report, "bundles": bundles,
                "missing_required": sorted(missing),
                "audience_identity": self._audience_identity(audience),
            }
            from email.utils import make_msgid
            self._connection.execute(
                "INSERT OR IGNORE INTO alert_deliveries VALUES (?, ?, ?, ?, ?, NULL)",
                (execution_id, audience["name"], state, json.dumps(payload), make_msgid()),
            )

    def _audience_identity(self, audience):
        """Block a queued message if its recipient/policy settings have changed."""
        value = {"audience": audience, "sender": self.settings.sender if self.settings else None}
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def drain_pending(self):
        """Claim each pending delivery once; callers invoke outside phase locks."""
        if self.settings is None:
            return
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM alert_deliveries WHERE state='PENDING' ORDER BY rowid"
            ).fetchall()
        for row in rows:
            with self._lock, self._connection:
                changed = self._connection.execute(
                    "UPDATE alert_deliveries SET state='SENDING' WHERE execution_id=? AND audience=? AND state='PENDING'",
                    (row["execution_id"], row["audience"]),
                ).rowcount
            if not changed:
                continue
            result = {"state": "UNKNOWN", "reason": "Delivery interrupted"}
            try:
                audience = next(item for item in self.settings.lists if item["name"] == row["audience"])
                payload = json.loads(row["payload"])
                if payload["audience_identity"] != self._audience_identity(audience):
                    raise ValueError("Queued audience settings changed")
                attachments = {}
                for index, bundle in enumerate(payload["bundles"]):
                    for name, expected in bundle["files"].items():
                        contents = read_regular(Path(bundle["directory"]) / name)
                        if identity(contents) != expected:
                            raise ValueError("Retained attachment changed")
                        attachments[f"attempt-{index + 1}-{name}"] = contents
                message = render_message(self.settings, payload["diagnostic"], attachments, message_id=row["message_id"])
            except Exception:
                result = {"state": "BLOCKED", "reason": "Message evidence or audience unavailable"}
            else:
                result = deliver_message(self.settings, audience["recipients"], message, smtp_factory=self.smtp_factory)
            finally:
                with self._lock, self._connection:
                    self._connection.execute(
                        "UPDATE alert_deliveries SET state=?, result=? WHERE execution_id=? AND audience=?",
                        (result["state"], json.dumps(result), row["execution_id"], row["audience"]),
                    )
                self.logger.info("Terminal alert execution=%s state=%s", row["execution_id"], result["state"])

    def delivery_states(self):
        """Expose delivery outcomes without leaking recipients or message bodies."""
        with self._lock:
            return [row[0] for row in self._connection.execute(
                "SELECT state FROM alert_deliveries ORDER BY rowid"
            )]

    def retry_failed(self, execution_id, audience):
        """Explicitly retry only definite failure, never UNKNOWN or PARTIAL."""
        with self._lock, self._connection:
            return self._connection.execute(
                "UPDATE alert_deliveries SET state='PENDING' WHERE execution_id=? AND audience=? AND state='FAILED'",
                (execution_id, audience),
            ).rowcount == 1

    def close(self):
        with self._lock:
            self._connection.close()


def main(argv=None):
    """Inspect or explicitly requeue definite failures without invoking SMTP.

    This command never constructs AlertService, reads credentials, or changes
    a running process's SENDING state. UNKNOWN and PARTIAL cannot be requeued.
    """
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="Show delivery state and safe reason, never message bodies or recipients.")
    retry = commands.add_parser("retry-failed", help="Requeue a definite failure for the continuous owner.")
    retry.add_argument("execution_id")
    retry.add_argument("audience")
    args = parser.parse_args(argv)
    mode = "ro" if args.command == "list" else "rw"
    try:
        with sqlite3.connect(args.database.resolve().as_uri() + "?mode=" + mode, uri=True) as connection:
            if args.command == "list":
                rows = connection.execute("SELECT execution_id, audience, state, result, payload FROM alert_deliveries ORDER BY rowid")
                for execution, audience, state, result, payload in rows:
                    print(json.dumps({
                        "execution_id": execution, "audience": audience, "state": state,
                        "result": json.loads(result) if result else None,
                        "missing_required": json.loads(payload)["missing_required"],
                    }))
            else:
                changed = connection.execute(
                    "UPDATE alert_deliveries SET state='PENDING' WHERE execution_id=? AND audience=? AND state='FAILED'",
                    (args.execution_id, args.audience),
                ).rowcount
                if not changed:
                    parser.exit(1, "No definite failed delivery matched; ambiguous or partial delivery cannot be replayed.\n")
    except (OSError, sqlite3.Error):
        parser.exit(1, "Cannot access an initialized alert delivery database.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
