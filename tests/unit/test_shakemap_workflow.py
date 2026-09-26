"""Durable ShakeMap handoff checks using temporary SQLite and offline HTTP."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from urllib.error import URLError

from pyfinder.services.database import ThreadSafeDB
from pyfinder.services.shakemap_client import (
    ShakeMapClient,
    ShakeMapHTTPError,
    ShakeMapJobUnavailable,
    ShakeMapSubmissionUncertain,
    ShakeMapTransportError,
)
from pyfinder.services.shakemap_workflow import ShakeMapRecordingError, ShakeMapWorkflow
from tests.unit.test_shakemap_client import FakeTransport, acknowledgement, detail, row
from tests.unit.test_shakemap_inputs import ORIGIN, make_solution
from pyfinder.utils.shakemap import ShakeMapExporter


class ShakeMapWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "workflow.sqlite"
        self.files = {"event.xml": b"origin", "event_dat.xml": b"observations"}

    def workflow(self, *responses, base_url="http://shakemap:8000", transport=None):
        """Every instance has a real independent SQLite connection, but no network."""
        transport = transport if transport is not None else FakeTransport(*responses)
        client = ShakeMapClient(base_url, transport=transport)
        workflow = ShakeMapWorkflow(self.path, client)
        self.addCleanup(workflow.close)
        return workflow, transport

    def submit(self, workflow, **changes):
        arguments = dict(attempt_id="attempt-1", event_id="quake-1", files=self.files)
        arguments.update(changes)
        return workflow.submit(**arguments)

    def test_nonpersistent_or_missing_database_path_is_rejected(self):
        transport = FakeTransport()
        client = ShakeMapClient("http://shakemap:8000", transport=transport)
        for invalid in (None, "", "   ", ":memory:", "file::memory:?cache=shared"):
            with self.subTest(path=invalid), self.assertRaises((TypeError, ValueError)):
                ShakeMapWorkflow(invalid, client)
        self.assertEqual(transport.calls, [])

    def test_intent_is_committed_before_transport_is_called(self):
        observer, _ = self.workflow()
        response = FakeTransport((202, acknowledgement()))

        def transport(*args):
            # An independent connection must see intent before the fake remote
            # endpoint accepts anything. In-memory bookkeeping would not suffice.
            intent = observer.get("attempt-1")
            self.assertEqual(intent["submission_state"], "SUBMITTING")
            self.assertIsNone(intent["internal_sequence"])
            return response(*args)

        workflow, _ = self.workflow(transport=transport)
        result = self.submit(workflow)
        self.assertEqual(result["submission_state"], "ACCEPTED")
        self.assertEqual(result["internal_sequence"], 42)
        self.assertEqual(observer.get("attempt-1"), result)
        self.assertEqual(len(response.calls), 1)

    def test_repeat_after_reopening_returns_record_without_post(self):
        first, transport = self.workflow((202, acknowledgement()))
        expected = self.submit(first)
        reopened, no_requests = self.workflow()

        self.assertEqual(self.submit(reopened), expected)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(no_requests.calls, [])

    def test_changed_request_cannot_reuse_attempt_identity(self):
        workflow, transport = self.workflow((202, acknowledgement()))
        self.submit(workflow)

        for changes in (
            {"event_id": "another-event"},
            {"files": {**self.files, "event.xml": b"changed"}},
            {"configuration": "italy"},
            {"overwrite": False},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.submit(workflow, **changes)

        other_endpoint, untouched = self.workflow(base_url="http://other:8000")
        with self.assertRaises(ValueError):
            self.submit(other_endpoint)
        with self.assertRaises(ValueError):
            other_endpoint.poll("attempt-1")

        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(untouched.calls, [])

    def test_invalid_local_request_does_not_reserve_attempt(self):
        workflow, transport = self.workflow()
        for changes in ({"attempt_id": ""}, {"overwrite": 1}, {"files": {"bad/name": b"x"}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.submit(workflow, **changes)

        self.assertIsNone(workflow.get("attempt-1"))
        self.assertEqual(workflow.unresolved(), [])
        self.assertEqual(transport.calls, [])

    def test_database_insert_failure_prevents_post(self):
        workflow, transport = self.workflow()
        with sqlite3.connect(self.path) as connection:
            connection.execute('''
                CREATE TRIGGER reject_intent BEFORE INSERT ON shakemap_submissions
                BEGIN SELECT RAISE(ABORT, 'injected write failure'); END
            ''')

        with self.assertRaises(sqlite3.IntegrityError):
            self.submit(workflow)
        self.assertIsNone(workflow.get("attempt-1"))
        self.assertEqual(transport.calls, [])

    def test_uncertain_submission_survives_reopen_without_replay(self):
        workflow, transport = self.workflow(URLError("lost acknowledgement"))
        with self.assertRaises(ShakeMapSubmissionUncertain):
            self.submit(workflow)

        reopened, untouched = self.workflow()
        result = self.submit(reopened)
        self.assertEqual(result["submission_state"], "UNCERTAIN")
        self.assertIsNone(result["internal_sequence"])
        self.assertEqual(len(reopened.unresolved()), 1)
        with self.assertRaises(ValueError):
            reopened.poll("attempt-1")
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(untouched.calls, [])

    def test_unexpected_interruption_leaves_intent_and_prevents_replay(self):
        def interrupted(*args):
            raise KeyboardInterrupt()

        workflow, _ = self.workflow(transport=interrupted)
        with self.assertRaises(KeyboardInterrupt):
            self.submit(workflow)

        reopened, untouched = self.workflow()
        result = self.submit(reopened)
        self.assertEqual(result["submission_state"], "SUBMITTING")
        self.assertEqual(len(reopened.unresolved()), 1)
        self.assertEqual(untouched.calls, [])

    def test_rejection_is_preserved_without_automatic_retry(self):
        workflow, transport = self.workflow((400, {"error": "bad input"}))
        with self.assertRaises(ShakeMapHTTPError):
            self.submit(workflow)

        result = self.submit(workflow)
        self.assertEqual(result["submission_state"], "REJECTED")
        self.assertIsNone(result["internal_sequence"])
        self.assertEqual(workflow.unresolved(), [])
        self.assertEqual(len(transport.calls), 1)

    def test_acceptance_recording_failure_retains_job_and_blocks_replay(self):
        workflow, transport = self.workflow((202, acknowledgement()))
        with sqlite3.connect(self.path) as connection:
            connection.execute('''
                CREATE TRIGGER reject_acceptance BEFORE UPDATE ON shakemap_submissions
                WHEN NEW.submission_state = 'ACCEPTED'
                BEGIN SELECT RAISE(ABORT, 'injected acceptance write failure'); END
            ''')

        with self.assertRaises(ShakeMapRecordingError) as caught:
            self.submit(workflow)
        self.assertEqual(caught.exception.job.event_id, "quake-1")
        self.assertEqual(caught.exception.job.internal_sequence, 42)
        self.assertEqual(caught.exception.acknowledgement, acknowledgement())
        self.assertIsInstance(caught.exception.__cause__, sqlite3.IntegrityError)

        reopened, untouched = self.workflow()
        self.assertEqual(self.submit(reopened)["submission_state"], "SUBMITTING")
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(untouched.calls, [])

    def test_commit_failures_do_not_grant_permission_to_replay(self):
        # A deferred foreign-key violation fails at COMMIT rather than at the
        # write statement. Exercise both sides of the remote acceptance window
        # with actual SQLite rollback, without mocking the transaction methods.
        for phase in ("intent", "acceptance"):
            with self.subTest(phase=phase):
                attempt = f"attempt-{phase}"
                workflow, transport = self.workflow((202, acknowledgement()))
                connection = workflow._connection
                connection.execute("PRAGMA foreign_keys=ON")
                with connection:
                    connection.execute("CREATE TABLE IF NOT EXISTS commit_parent (id PRIMARY KEY)")
                    connection.execute('''
                        CREATE TABLE IF NOT EXISTS commit_child (
                            parent_id REFERENCES commit_parent(id)
                            DEFERRABLE INITIALLY DEFERRED
                        )
                    ''')
                    connection.execute("DROP TRIGGER IF EXISTS fail_commit")
                    if phase == "intent":
                        connection.execute('''
                            CREATE TRIGGER fail_commit AFTER INSERT ON shakemap_submissions
                            BEGIN INSERT INTO commit_child VALUES (1); END
                        ''')
                    else:
                        connection.execute('''
                            CREATE TRIGGER fail_commit AFTER UPDATE ON shakemap_submissions
                            WHEN NEW.submission_state = 'ACCEPTED'
                            BEGIN INSERT INTO commit_child VALUES (1); END
                        ''')

                if phase == "intent":
                    with self.assertRaises(sqlite3.IntegrityError):
                        self.submit(workflow, attempt_id=attempt)
                    self.assertIsNone(workflow.get(attempt))
                    self.assertEqual(transport.calls, [])
                else:
                    with self.assertRaises(ShakeMapRecordingError) as caught:
                        self.submit(workflow, attempt_id=attempt)
                    self.assertEqual(caught.exception.job.internal_sequence, 42)
                    self.assertIsInstance(caught.exception.__cause__, sqlite3.IntegrityError)
                    self.assertEqual(workflow.get(attempt)["submission_state"], "SUBMITTING")
                    reopened, untouched = self.workflow()
                    self.submit(reopened, attempt_id=attempt)
                    self.assertEqual(untouched.calls, [])

                with connection:
                    connection.execute("DROP TRIGGER fail_commit")

    def test_poll_persistence_failure_leaves_prior_observation_intact(self):
        workflow, _ = self.workflow(
            (202, acknowledgement()),
            (200, detail(row())),
            (200, detail(row(status="SUCCESS"))),
        )
        self.submit(workflow)
        previous = workflow.poll("attempt-1")
        with sqlite3.connect(self.path) as connection:
            connection.execute('''
                CREATE TRIGGER reject_observation BEFORE UPDATE ON shakemap_submissions
                BEGIN SELECT RAISE(ABORT, 'injected observation write failure'); END
            ''')

        with self.assertRaises(sqlite3.IntegrityError):
            workflow.poll("attempt-1")
        self.assertEqual(workflow.get("attempt-1"), previous)
        self.assertEqual(len(workflow.unresolved()), 1)

    def test_concurrent_duplicate_uses_only_one_post(self):
        entered = threading.Event()
        release = threading.Event()
        response = FakeTransport((202, acknowledgement()))

        def blocked_transport(*args):
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError("test did not release the transport")
            return response(*args)

        first, _ = self.workflow(transport=blocked_transport)
        second, untouched = self.workflow()
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.submit, first)
            try:
                self.assertTrue(entered.wait(timeout=5))
                record = self.submit(second)
                self.assertEqual(record["submission_state"], "SUBMITTING")
                self.assertEqual(untouched.calls, [])
            finally:
                release.set()
            self.assertEqual(future.result(timeout=5)["submission_state"], "ACCEPTED")

        self.assertEqual(len(response.calls), 1)

    def test_reopened_monitor_uses_exact_sequence_not_newer_success(self):
        first, _ = self.workflow((202, acknowledgement()))
        self.submit(first)
        reopened, transport = self.workflow(
            (200, detail(row(), row(sequence=43, status="SUCCESS"))),
        )

        record = reopened.poll("attempt-1")
        self.assertEqual(record["internal_sequence"], 42)
        self.assertEqual(record["observation"]["details"]["status"], "RUNNING")
        self.assertEqual(len(reopened.unresolved()), 1)
        self.assertEqual([call[0] for call in transport.calls], ["GET"])

    def test_terminal_evidence_is_separate_from_submission_and_products(self):
        for status in ("SUCCESS", "FAILED"):
            with self.subTest(status=status):
                attempt = f"attempt-{status}"
                workflow, _ = self.workflow(
                    (202, acknowledgement()),
                    (200, detail(archives=[row(status=status, products_ready=False)])),
                )
                self.submit(workflow, attempt_id=attempt)
                record = workflow.poll(attempt)

                self.assertEqual(record["submission_state"], "ACCEPTED")
                self.assertEqual(record["observation"]["scope"], "archives")
                self.assertEqual(record["observation"]["details"]["status"], status)
                self.assertFalse(record["observation"]["details"]["products_ready"])
                self.assertNotIn(attempt, [item["attempt_id"] for item in workflow.unresolved()])

    def test_read_failure_retains_previous_evidence_and_can_be_polled_again(self):
        workflow, _ = self.workflow(
            (202, acknowledgement()),
            (200, detail(row())),
            URLError("read interrupted"),
            (200, detail(row(status="SUCCESS"))),
        )
        self.submit(workflow)
        previous = workflow.poll("attempt-1")
        with self.assertRaises(ShakeMapTransportError):
            workflow.poll("attempt-1")

        failed_read = workflow.get("attempt-1")
        self.assertEqual(failed_read["observation"], previous["observation"])
        self.assertEqual(failed_read["observed_at"], previous["observed_at"])
        self.assertEqual(failed_read["submission_state"], "ACCEPTED")
        self.assertEqual(failed_read["last_error"], "ShakeMapTransportError")

        recovered = workflow.poll("attempt-1")
        self.assertEqual(recovered["observation"]["details"]["status"], "SUCCESS")
        self.assertIsNone(recovered["last_error"])
        self.assertEqual(workflow.unresolved(), [])

    def test_missing_sequence_is_not_replaced_by_current_success(self):
        workflow, _ = self.workflow(
            (202, acknowledgement()), (200, detail(row(sequence=43, status="SUCCESS"))),
        )
        self.submit(workflow)
        with self.assertRaises(ShakeMapJobUnavailable):
            workflow.poll("attempt-1")

        record = workflow.get("attempt-1")
        self.assertEqual(record["internal_sequence"], 42)
        self.assertIsNone(record["observation"])
        self.assertEqual(record["last_error"], "ShakeMapJobUnavailable")
        self.assertEqual(len(workflow.unresolved()), 1)

    def test_existing_scheduler_data_and_cleanup_leave_external_record_intact(self):
        database = ThreadSafeDB(self.path)
        self.addCleanup(database.close)
        database.insert_scheduled_item(
            "quake-1", "RRSM", "origin", "updated", "query", current_delay_time=0,
        )
        database.mark_event_processing("quake-1", "RRSM", 0, "started")
        workflow, _ = self.workflow((202, acknowledgement()))
        self.submit(workflow)

        # Adding the helper must not alter an existing row or its startup rule.
        self.assertEqual(database.get_event_meta("quake-1", "RRSM", 0)["status"], "processing")
        self.assertEqual(database.fail_abandoned_processing("restart", "now"), 1)
        self.assertEqual(database.cleanup_terminal_events(), 1)
        self.assertIsNone(database.get_event_meta("quake-1", "RRSM", 0))
        self.assertEqual(workflow.get("attempt-1")["internal_sequence"], 42)

    def test_real_exporter_bundle_reaches_client_through_durable_boundary(self):
        files = ShakeMapExporter(make_solution(), "quake-1", ORIGIN).export_all()
        workflow, transport = self.workflow((202, acknowledgement()))
        record = self.submit(workflow, files=files)
        self.assertEqual(set(record["request"]["files"]), set(files))
        for name, content in files.items():
            self.assertIn(content, transport.calls[0][3])
            self.assertEqual(record["request"]["files"][name]["size"], len(content))


if __name__ == "__main__":
    unittest.main()
