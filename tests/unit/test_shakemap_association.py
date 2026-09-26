"""Scheduled/external ownership checks using temporary SQLite and no services."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from pyfinder.services.database import ThreadSafeDB
from pyfinder.services.eventtracker import EventTracker
from pyfinder.services.shakemap_client import ShakeMapClient
from pyfinder.services.shakemap_workflow import ShakeMapWorkflow
from tests.unit.test_shakemap_client import FakeTransport


class ScheduledShakeMapAssociationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "scheduled.sqlite"
        self.tracker = EventTracker(self.path)
        self.addCleanup(self.tracker.close)
        # Association operations never need an HTTP client. Supplying None also
        # ensures these tests cannot accidentally exercise remote submission.
        self.workflow = ShakeMapWorkflow(self.path, None)
        self.addCleanup(self.workflow.close)
        self.identity = dict(event_id="quake-1", service="RRSM", current_delay_time=5)
        self.register()

    def register(self):
        self.tracker.register_new_schedule(
            **self.identity,
            origin_time="2026-09-26T00:00:00+00:00",
            last_update_time="2026-09-26T00:01:00+00:00",
            next_query_time="2026-09-26T00:05:00+00:00",
        )

    def claim(self, token):
        with patch("pyfinder.services.database.uuid4") as uuid:
            uuid.return_value.hex = token
            self.assertEqual(self.tracker.mark_as_processing(**self.identity), 1)
        self.assertEqual(self.tracker.get_execution_id(**self.identity), token)
        return token

    def bind(self, token):
        self.workflow.bind_scheduled_attempt(attempt_id=token, **self.identity)

    def test_assignment_changes_token_only_for_a_deliberate_new_attempt(self):
        self.assertIsNone(self.tracker.get_execution_id(**self.identity))
        self.claim("first")
        self.assertEqual(self.tracker.mark_as_processing(**self.identity), 0)
        self.assertEqual(self.tracker.get_execution_id(**self.identity), "first")

        self.tracker.mark_for_retry(**self.identity, error_message="pre-accept failure")
        self.claim("retry")
        self.assertNotIn("execution_id", self.tracker.get_event_meta(**self.identity))

    def test_association_is_idempotent_and_rejects_changed_or_unowned_mapping(self):
        token = self.claim("owned")
        self.bind(token)
        self.bind(token)
        self.assertEqual(len(self.workflow.pending_scheduled_attempts()), 1)

        with self.assertRaises(ValueError):
            self.workflow.bind_scheduled_attempt(token, "another-event", "RRSM", 5)
        with self.assertRaises(ValueError):
            self.bind("not-the-execution")
        with self.assertRaises(ValueError):
            self.workflow.bind_scheduled_attempt("", **self.identity)

        self.assertEqual(self.workflow.pending_scheduled_attempts(), [{
            "attempt_id": "owned", **self.identity, "finalized": 0,
        }])

    def test_pending_row_cannot_be_bound_even_with_preceding_token(self):
        self.claim("preceding")
        self.tracker.mark_for_retry(**self.identity, error_message="input failed")
        with self.assertRaises(ValueError):
            self.bind("preceding")

    def test_cleanup_keeps_association_and_old_result_cannot_finish_new_registration(self):
        self.claim("old")
        self.bind("old")
        self.assertEqual(self.tracker.finish_shakemap_execution("old", success=True), 1)
        self.assertEqual(self.tracker.cleanup_terminal_events(), 1)
        self.assertEqual(self.workflow.pending_scheduled_attempts()[0]["attempt_id"], "old")

        self.register()
        self.claim("replacement")
        for success in (True, False):
            self.assertEqual(
                self.tracker.finish_shakemap_execution("old", success=success), 0,
            )
        self.assertEqual(self.tracker.get_event_meta(**self.identity)["status"], "processing")
        self.assertEqual(self.workflow.finish_scheduled_attempt("old"), 1)
        self.assertEqual(self.workflow.finish_scheduled_attempt("old"), 0)
        self.assertEqual(self.workflow.pending_scheduled_attempts(), [])

        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute(
                "SELECT finalized FROM shakemap_scheduled_attempts WHERE attempt_id = 'old'"
            ).fetchone(), (1,))

    def test_forced_restart_failure_cannot_be_reopened_by_remote_success(self):
        self.claim("abandoned")
        self.bind("abandoned")
        self.assertEqual(self.tracker.recover_abandoned_processing(), 1)
        self.assertEqual(
            self.tracker.finish_shakemap_execution("abandoned", success=True), 0,
        )
        self.assertEqual(self.tracker.get_event_meta(**self.identity)["status"], "failed")

    def test_native_failure_is_terminal_without_counting_a_new_upstream_retry(self):
        self.claim("accepted")
        self.bind("accepted")
        self.assertEqual(self.tracker.finish_shakemap_execution(
            "accepted", success=False, diagnostic="native calculation failed",
        ), 1)
        metadata = self.tracker.get_event_meta(**self.identity)
        self.assertEqual(metadata["status"], "failed")
        self.assertEqual(metadata["retry_count"], 0)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute(
                "SELECT last_error FROM event_tracker"
            ).fetchone(), ("native calculation failed",))
        # Reconciliation itself is repeatable after its scheduled transition.
        self.bind("accepted")
        self.assertEqual(self.workflow.finish_scheduled_attempt("accepted"), 1)

    def preparation_client(self):
        """Expose production local validation with a transport that cannot POST."""
        transport = FakeTransport()
        self.workflow.client = ShakeMapClient("http://shakemap:8000", transport=transport)
        return transport

    def test_prepared_bytes_options_and_insertion_order_survive_reopen_and_finalize(self):
        transport = self.preparation_client()
        files = {"event.xml": b"\x00\xfforigin\n", "event_dat.xml": b"", "rupture.json": b"{}"}
        self.claim("z-first")
        self.bind("z-first")
        self.workflow.prepare_scheduled_submission(
            "z-first", "quake-1_t00005", files, configuration="regional", overwrite=False,
        )
        self.tracker.finish_shakemap_execution("z-first", success=True)
        self.workflow.finish_scheduled_attempt("z-first")
        self.tracker.cleanup_terminal_events()

        self.register()
        self.claim("a-second")
        self.bind("a-second")
        self.workflow.prepare_scheduled_submission("a-second", "quake-1_t00005", files)
        reopened = ShakeMapWorkflow(self.path, self.workflow.client)
        self.addCleanup(reopened.close)
        requests = reopened.prepared_scheduled_submissions()
        self.assertEqual(requests, [
            {
                "attempt_id": "z-first", "event_id": "quake-1_t00005", "files": files,
                "configuration": "regional", "overwrite": False,
                "base_url": "http://shakemap:8000",
            },
            {
                "attempt_id": "a-second", "event_id": "quake-1_t00005", "files": files,
                "configuration": "global", "overwrite": True,
                "base_url": "http://shakemap:8000",
            },
        ])
        self.assertIsNone(reopened.get("z-first"))
        self.assertEqual(transport.calls, [])

        # A consumer's changes to decoded results cannot change retained bytes.
        requests[0]["files"]["event.xml"] = b"changed"
        self.assertEqual(reopened.prepared_scheduled_submissions()[0]["files"], files)

    def test_prepared_selection_filters_before_decode_and_preserves_insertion_order(self):
        self.preparation_client()
        for index, token in enumerate(("first", "excluded", "last")):
            if index:
                self.register()
            self.claim(token)
            self.bind(token)
            self.workflow.prepare_scheduled_submission(
                token, "same-public-id", {"event.xml": token.encode()},
            )
            self.tracker.finish_shakemap_execution(token, success=True)
            self.workflow.finish_scheduled_attempt(token)
            self.tracker.cleanup_terminal_events()

        # Corrupted historical data must not be decoded when its ID was excluded.
        # This also catches implementations that load/decode all bundles first.
        with sqlite3.connect(self.path) as connection:
            connection.execute("""
                UPDATE shakemap_scheduled_attempts SET prepared_json = 'invalid-json'
                WHERE attempt_id = 'excluded'
            """)

        # More than one chunk, reverse caller order, a repeated ID, and unknown
        # IDs must still yield each retained request once in original order.
        selection = ["last"] + [f"missing-{index}" for index in range(901)] + ["first", "last"]
        requests = self.workflow.prepared_scheduled_submissions(attempt_ids=iter(selection))
        self.assertEqual([item["attempt_id"] for item in requests], ["first", "last"])
        self.assertEqual([item["files"] for item in requests], [
            {"event.xml": b"first"}, {"event.xml": b"last"},
        ])
        self.assertEqual(self.workflow.prepared_scheduled_submissions(attempt_ids=[]), [])
        self.assertEqual(self.workflow.prepared_scheduled_submissions(attempt_ids=["missing"]), [])

    def test_preparation_is_immutable_including_options_identity_and_endpoint(self):
        transport = self.preparation_client()
        self.claim("prepared")
        self.bind("prepared")
        initial = dict(
            attempt_id="prepared", event_id="public-id",
            files={"event.xml": b"origin", "event_dat.xml": b"observations"},
            configuration="regional", overwrite=False,
        )
        self.workflow.prepare_scheduled_submission(**initial)
        self.workflow.prepare_scheduled_submission(**initial)
        for changed in (
            {"event_id": "different-public-id"},
            {"files": {"event.xml": b"different"}},
            {"configuration": "global"},
            {"overwrite": True},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.workflow.prepare_scheduled_submission(**(initial | changed))

        self.workflow.client = ShakeMapClient("http://another:8000", transport=transport)
        with self.assertRaises(ValueError):
            self.workflow.prepare_scheduled_submission(**initial)
        self.assertEqual(len(self.workflow.prepared_scheduled_submissions()), 1)
        self.assertEqual(transport.calls, [])

    def test_preparation_requires_binding_and_validates_before_persistence(self):
        transport = self.preparation_client()
        with self.assertRaises(ValueError):
            self.workflow.prepare_scheduled_submission("unbound", "public-id", {})
        self.claim("bound")
        self.bind("bound")
        for changes in (
            {"files": {"event.xml": "not-bytes"}},
            {"configuration": "../regional"},
            {"overwrite": "yes"},
        ):
            request = dict(attempt_id="bound", event_id="public-id", files={}) | changes
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.workflow.prepare_scheduled_submission(**request)
        self.assertEqual(self.workflow.prepared_scheduled_submissions(), [])
        self.assertEqual(transport.calls, [])

    def test_preparation_column_migrates_existing_association_table_without_data_loss(self):
        path = self.path.with_name("before-preparation.sqlite")
        with sqlite3.connect(path) as connection:
            connection.execute("""
                CREATE TABLE shakemap_scheduled_attempts (
                    attempt_id TEXT PRIMARY KEY NOT NULL,
                    event_id TEXT NOT NULL, service TEXT NOT NULL,
                    current_delay_time REAL NOT NULL,
                    finalized INTEGER NOT NULL DEFAULT 0
                )
            """)
            connection.execute("""
                INSERT INTO shakemap_scheduled_attempts VALUES ('old', 'quake', 'RRSM', 5, 0)
            """)

        for _ in range(2):
            workflow = ShakeMapWorkflow(path, None)
            try:
                self.assertEqual(workflow.pending_scheduled_attempts(), [{
                    "attempt_id": "old", "event_id": "quake", "service": "RRSM",
                    "current_delay_time": 5, "finalized": 0,
                }])
                self.assertEqual(workflow.prepared_scheduled_submissions(), [])
            finally:
                workflow.close()


class ScheduledExecutionMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "old.sqlite"
        # This is the pre-integration schema, stated independently of production
        # DDL. Migration must retain legacy expiration/metadata and the same PK.
        self.columns = (
            "event_id", "service", "status", "origin_time", "last_update_time",
            "last_query_time", "next_query_time", "current_delay_time",
            "next_delay_time", "retry_count", "expiration_time", "priority",
            "last_error", "last_data_hash", "last_data_snapshot", "emsc_alert_json",
            "last_modified",
        )
        with sqlite3.connect(self.path) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("""
                CREATE TABLE event_tracker (
                    event_id TEXT, service TEXT, status TEXT, origin_time TEXT,
                    last_update_time TEXT, last_query_time TEXT, next_query_time TEXT,
                    current_delay_time REAL, next_delay_time REAL,
                    retry_count INTEGER DEFAULT 0, expiration_time TEXT,
                    priority INTEGER DEFAULT 1, last_error TEXT, last_data_hash TEXT,
                    last_data_snapshot TEXT, emsc_alert_json TEXT, last_modified TEXT,
                    PRIMARY KEY (event_id, service, current_delay_time)
                )
            """)
            self.rows = [
                ("quake", "RRSM", status, "origin", "update", None, "query", delay,
                 15, 2, "legacy expiration", 4, "diagnostic", "hash", "snapshot",
                 '{"a": 1}', "modified")
                for delay, status in ((0, "pending"), (5, "processing"), (10, "failed"))
            ]
            connection.executemany(
                "INSERT INTO event_tracker VALUES (" + ",".join("?" * 17) + ")",
                self.rows,
            )

    def assert_preserved(self):
        with sqlite3.connect(self.path) as connection:
            rows = connection.execute(
                "SELECT " + ",".join(self.columns) + " FROM event_tracker ORDER BY current_delay_time"
            ).fetchall()
            self.assertEqual(rows, self.rows)
            self.assertEqual(connection.execute(
                "SELECT execution_id FROM event_tracker"
            ).fetchall(), [(None,), (None,), (None,)])
            info = connection.execute("PRAGMA table_info(event_tracker)").fetchall()
            self.assertEqual([row[1] for row in info if row[5]], [
                "event_id", "service", "current_delay_time",
            ])

    def test_reopening_populated_legacy_schema_is_additive_and_idempotent(self):
        for _ in range(2):
            database = ThreadSafeDB(self.path)
            database.close()
            self.assert_preserved()

    def test_concurrent_independent_connections_do_not_race_schema_alteration(self):
        barrier = threading.Barrier(4)

        class IndependentConnection(ThreadSafeDB):
            def __init__(self, path):
                # Separate locks reproduce independently running processes while
                # retaining deterministic in-process test orchestration.
                self._lock = threading.Lock()
                super().__init__(path)

            def _create_table(self):
                barrier.wait(timeout=5)
                super()._create_table()

        def open_database():
            database = IndependentConnection(self.path)
            database.close()

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(open_database) for _ in range(4)]
            for future in futures:
                future.result(timeout=10)
        self.assert_preserved()


if __name__ == "__main__":
    unittest.main()
