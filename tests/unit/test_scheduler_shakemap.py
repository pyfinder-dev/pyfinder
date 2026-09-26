"""Offline lifecycle tests across scheduler, real SQLite, and the HTTP adapter."""

from concurrent.futures import Future
import json
import logging
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import URLError

from pyfinder.findermanager import FinDerManager
from pyfinder.services.eventtracker import EventTracker
from pyfinder.services.scheduler import FollowUpScheduler
from pyfinder.services.querypolicy import RRSMQueryPolicy
from pyfinder.services.shakemap_client import ShakeMapClient
from pyfinder.services.shakemap_inputs import PreparedShakeMapInputs
from pyfinder.services.shakemap_workflow import ShakeMapWorkflow
from pyfinder.start_monitoring import _build_shakemap_boundary
from tests.unit.test_scheduler import SelectorDouble
from tests.unit.test_shakemap_client import FakeTransport, acknowledgement, detail, row
from tests.unit.test_shakemap_inputs import make_solution


CALCULATION_ID = "event-1_t00005"


class ImmediateExecutor:
    """Use real futures but let the test choose each scheduler cycle explicitly."""

    def submit(self, operation, *args):
        future = Future()
        try:
            future.set_result(operation(*args))
        except BaseException as error:
            future.set_exception(error)
        return future

    def shutdown(self, wait):
        pass


class Manager(FinDerManager):
    """Replace acquisition/FinDer only; exercise the real manager export handoff."""

    def __init__(self):
        self.entry_kind = self.ALERT_BACKED
        self.calls = 0
        self.solution = make_solution()

    def for_alert_context(self, **arguments):
        self.event_context = arguments["event_context"]
        self.metadata = arguments["metadata"]
        return self

    def run(self, event_id):
        self.calls += 1
        return self.solution


class SchedulerShakeMapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "scheduler.sqlite"
        self.inputs = Path(self.temp.name).resolve() / "inputs"
        self.inputs.mkdir()
        self.schedulers = []
        self.addCleanup(self.close_schedulers)

    def close_schedulers(self):
        for scheduler in self.schedulers:
            scheduler.close()

    def scheduler(self, *responses):
        transport = FakeTransport(*responses)
        workflow = ShakeMapWorkflow(
            self.path, ShakeMapClient("http://fake:8000", transport=transport),
        )
        tracker = EventTracker(self.path)
        scheduler = FollowUpScheduler(
            tracker=tracker,
            service_policies={"RRSM": RRSMQueryPolicy()},
            finder_config_selector=SelectorDouble(),
            configuration={"shakemap": {"configuration": "global", "overwrite": True}},
            logger=logging.getLogger("test.scheduler.shakemap"),
            shakemap_workflow=workflow,
            shakemap_inputs=PreparedShakeMapInputs(self.inputs),
        )
        # No thread timing or sleeping is needed to test phase transitions.
        scheduler.executor.shutdown(wait=True)
        scheduler._monitor_executor.shutdown(wait=True)
        scheduler.executor = ImmediateExecutor()
        scheduler._monitor_executor = ImmediateExecutor()
        scheduler._finder_manager_class = Manager()
        self.schedulers.append(scheduler)
        return scheduler, transport

    def seed(self, scheduler, service="RRSM"):
        alert = {
            "unid": "event-1", "lat": 46.0, "lon": 7.0, "mag": 5.5,
            "depth": 8.0, "time": "2026-08-06T09:55:00Z", "magtype": "Mw",
        }
        scheduler.tracker._db.insert_scheduled_item(
            "event-1", service, alert["time"], "updated",
            "2000-01-01T00:00:00+00:00", current_delay_time=5,
            emsc_alert_json=json.dumps(alert),
        )

    def meta(self, scheduler):
        return scheduler.tracker.get_event_meta("event-1", "RRSM", 5)

    def response(self, status="SUCCESS", sequence=42):
        return 200, detail(row(sequence=sequence, status=status), event_id=CALCULATION_ID)

    def test_success_waits_for_matching_job_even_when_no_work_is_due(self):
        scheduler, transport = self.scheduler(
            (202, acknowledgement(event_id=CALCULATION_ID)), self.response(),
        )
        self.seed(scheduler)
        scheduler.run_once()
        self.assertEqual(self.meta(scheduler)["status"], "processing")
        self.assertEqual([call[0] for call in transport.calls], ["POST"])

        scheduler.run_once()
        self.assertEqual(self.meta(scheduler)["status"], "completed")
        self.assertEqual(scheduler._finder_manager_class.calls, 1)
        self.assertEqual([call[0] for call in transport.calls], ["POST", "GET"])

    def test_deliberate_same_id_after_cleanup_is_a_new_submission(self):
        scheduler, transport = self.scheduler(
            (202, acknowledgement(event_id=CALCULATION_ID)), self.response(),
            (202, acknowledgement(event_id=CALCULATION_ID, internal_sequence=43)),
            self.response(sequence=43),
        )
        self.seed(scheduler)
        scheduler.run_once()
        first_token = scheduler.tracker.get_execution_id("event-1", "RRSM", 5)
        scheduler.run_once()
        self.assertEqual(scheduler.tracker.cleanup_terminal_events(), 1)

        self.seed(scheduler)
        scheduler.run_once()
        second_token = scheduler.tracker.get_execution_id("event-1", "RRSM", 5)
        self.assertNotEqual(first_token, second_token)
        self.assertEqual(self.meta(scheduler)["status"], "processing")
        scheduler.run_once()

        self.assertEqual(self.meta(scheduler)["status"], "completed")
        self.assertEqual(scheduler._finder_manager_class.calls, 2)
        self.assertEqual(
            scheduler.shakemap_workflow.get(first_token)["request"]["event_id"],
            scheduler.shakemap_workflow.get(second_token)["request"]["event_id"],
        )
        self.assertEqual([call[0] for call in transport.calls].count("POST"), 2)

    def test_later_same_id_waits_with_exact_inputs_until_prior_result_is_saved(self):
        scheduler, transport = self.scheduler(
            (202, acknowledgement(event_id=CALCULATION_ID)),
            self.response("RUNNING"), self.response(),
            (202, acknowledgement(event_id=CALCULATION_ID, internal_sequence=43)),
            self.response(sequence=43),
        )
        scheduler.service_policies["OTHER"] = RRSMQueryPolicy()
        self.seed(scheduler)
        scheduler.run_once()
        first_token = scheduler.tracker.get_execution_id("event-1", "RRSM", 5)

        # Simulate the first service upload's retained optional file. The next
        # deliberate request is a point source under the SAME public identity.
        rupture = self.inputs / CALCULATION_ID / "rupture.json"
        rupture.write_bytes(b"previous finite rupture")
        scheduler._finder_manager_class.solution = make_solution()
        scheduler._finder_manager_class.solution.rupture = None
        self.seed(scheduler, service="OTHER")
        scheduler.run_once()
        second_token = scheduler.tracker.get_execution_id("event-1", "OTHER", 5)

        self.assertNotEqual(first_token, second_token)
        self.assertIsNone(scheduler.shakemap_workflow.get(second_token))
        self.assertTrue(rupture.exists())
        prepared = scheduler.shakemap_workflow.prepared_scheduled_submissions()
        self.assertEqual([item["event_id"] for item in prepared], [CALCULATION_ID] * 2)
        self.assertNotIn("rupture.json", prepared[1]["files"])
        self.assertEqual(scheduler._finder_manager_class.calls, 2)
        self.assertEqual([call[0] for call in transport.calls], ["POST", "GET"])

        scheduler.run_once()
        first_record = scheduler.shakemap_workflow.get(first_token)
        self.assertEqual(first_record["observation"]["details"]["status"], "SUCCESS")
        self.assertEqual(scheduler.shakemap_workflow.get(second_token)["internal_sequence"], 43)
        self.assertFalse(rupture.exists())
        self.assertNotIn(b'filename="rupture.json"', transport.calls[-1][3])
        scheduler.run_once()
        self.assertEqual(scheduler.tracker.get_event_meta("event-1", "OTHER", 5)["status"], "completed")
        self.assertEqual(scheduler._finder_manager_class.calls, 2)
        self.assertEqual([call[0] for call in transport.calls], ["POST", "GET", "GET", "POST", "GET"])

    def test_uncertain_predecessor_retains_later_request_without_input_cleanup(self):
        scheduler, transport = self.scheduler(URLError("acknowledgement lost"))
        scheduler.service_policies["OTHER"] = RRSMQueryPolicy()
        self.seed(scheduler)
        scheduler.run_once()
        rupture = self.inputs / CALCULATION_ID / "rupture.json"
        rupture.write_bytes(b"server may still be reading this")

        scheduler._finder_manager_class.solution = make_solution()
        scheduler._finder_manager_class.solution.rupture = None
        self.seed(scheduler, service="OTHER")
        scheduler.run_once()
        scheduler.run_once()

        self.assertEqual(rupture.read_bytes(), b"server may still be reading this")
        self.assertEqual(len(scheduler.shakemap_workflow.prepared_scheduled_submissions()), 2)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(scheduler._finder_manager_class.calls, 2)
        self.assertEqual(scheduler.tracker.get_event_meta("event-1", "OTHER", 5)["status"], "processing")

    def test_queued_rejection_recovers_failed_local_retry_write_without_recounting(self):
        scheduler, transport = self.scheduler(
            (202, acknowledgement(event_id=CALCULATION_ID)),
            self.response("RUNNING"), self.response(),
            (503, {"error": "temporarily unavailable"}),
        )
        scheduler.service_policies["OTHER"] = RRSMQueryPolicy()
        self.seed(scheduler)
        scheduler.run_once()
        self.seed(scheduler, service="OTHER")
        scheduler.run_once()

        with patch.object(
            scheduler.tracker, "mark_for_retry",
            side_effect=sqlite3.OperationalError("retry write unavailable"),
        ):
            scheduler.run_once()
        row_before = scheduler.tracker.get_event_meta("event-1", "OTHER", 5)
        self.assertEqual(row_before["status"], "processing")
        self.assertEqual(row_before["retry_count"], 1)

        scheduler.run_once()
        row_after = scheduler.tracker.get_event_meta("event-1", "OTHER", 5)
        self.assertEqual(row_after["status"], "failed")
        self.assertEqual(row_after["retry_count"], 1)
        self.assertEqual(scheduler.shakemap_workflow.pending_scheduled_attempts(), [])
        self.assertEqual([call[0] for call in transport.calls], ["POST", "GET", "GET", "POST"])

    def test_unknown_acceptance_never_enters_execution_retry(self):
        scheduler, transport = self.scheduler(URLError("lost acknowledgement"))
        self.seed(scheduler)
        scheduler.run_once()
        scheduler.run_once()
        self.assertEqual(self.meta(scheduler)["status"], "processing")
        self.assertEqual(self.meta(scheduler)["retry_count"], 0)
        self.assertEqual(scheduler._finder_manager_class.calls, 1)
        self.assertEqual(len(transport.calls), 1)

    def test_poll_failure_retries_observation_without_running_finder(self):
        scheduler, transport = self.scheduler(
            (202, acknowledgement(event_id=CALCULATION_ID)),
            URLError("read unavailable"), self.response(),
        )
        self.seed(scheduler)
        for _ in range(3):
            scheduler.run_once()
        self.assertEqual(self.meta(scheduler)["status"], "completed")
        self.assertEqual(self.meta(scheduler)["retry_count"], 0)
        self.assertEqual(scheduler._finder_manager_class.calls, 1)
        self.assertEqual([call[0] for call in transport.calls], ["POST", "GET", "GET"])

    def test_native_failure_is_terminal_without_recalculation(self):
        scheduler, transport = self.scheduler(
            (202, acknowledgement(event_id=CALCULATION_ID)), self.response("FAILED"),
        )
        self.seed(scheduler)
        scheduler.run_once()
        scheduler.run_once()
        scheduler.run_once()
        self.assertEqual(self.meta(scheduler)["status"], "failed")
        self.assertEqual(scheduler._finder_manager_class.calls, 1)
        self.assertEqual([call[0] for call in transport.calls].count("POST"), 1)

    def test_explicit_rejection_retains_existing_execution_retry(self):
        scheduler, _ = self.scheduler((503, {"error": "not ready"}))
        self.seed(scheduler)
        scheduler.run_once()
        metadata = self.meta(scheduler)
        self.assertEqual(metadata["status"], "pending")
        self.assertEqual(metadata["retry_count"], 1)
        self.assertEqual(scheduler.shakemap_workflow.pending_scheduled_attempts(), [])

    def test_terminal_evidence_is_reapplied_after_local_write_failure(self):
        scheduler, transport = self.scheduler(
            (202, acknowledgement(event_id=CALCULATION_ID)), self.response(),
        )
        self.seed(scheduler)
        scheduler.run_once()
        with patch.object(
            scheduler.tracker, "finish_shakemap_execution",
            side_effect=sqlite3.OperationalError("temporary write failure"),
        ):
            scheduler.run_once()
        self.assertEqual(self.meta(scheduler)["status"], "processing")
        self.assertEqual(scheduler.shakemap_workflow.unresolved(), [])

        scheduler.run_once()
        self.assertEqual(self.meta(scheduler)["status"], "completed")
        self.assertEqual([call[0] for call in transport.calls], ["POST", "GET"])

    def test_restart_observes_external_result_without_reopening_failed_row(self):
        scheduler, _ = self.scheduler((202, acknowledgement(event_id=CALCULATION_ID)))
        self.seed(scheduler)
        scheduler.run_once()
        # Simulate process loss: close connections without orderly finalization.
        scheduler.shakemap_workflow.close()
        scheduler.tracker.close()
        self.schedulers.remove(scheduler)

        restarted, transport = self.scheduler(self.response())
        self.assertEqual(self.meta(restarted)["status"], "failed")
        restarted.run_once()
        self.assertEqual(self.meta(restarted)["status"], "failed")
        self.assertEqual(restarted._finder_manager_class.calls, 0)
        self.assertEqual([call[0] for call in transport.calls], ["GET"])

    def test_shutdown_preserves_accepted_external_job_and_finalizes_local_owner(self):
        scheduler, transport = self.scheduler((202, acknowledgement(event_id=CALCULATION_ID)))
        self.seed(scheduler)
        scheduler.run_once()
        scheduler.stop_and_drain()
        self.assertEqual(self.meta(scheduler)["status"], "failed")
        self.assertEqual(len(scheduler.shakemap_workflow.unresolved()), 1)
        self.assertEqual([call[0] for call in transport.calls], ["POST"])

    def test_no_solution_never_submits(self):
        scheduler, transport = self.scheduler()
        scheduler._finder_manager_class.solution = None
        self.seed(scheduler)
        scheduler.run_once()
        self.assertEqual(self.meta(scheduler)["status"], "pending")
        self.assertEqual(transport.calls, [])

    def test_continuous_boundary_is_explicit_and_validates_configuration(self):
        self.assertEqual(_build_shakemap_boundary({}, None), (None, None))
        settings = {
            "service-enabled": True, "service-url": "http://fake:8000",
            "input-directory": str(self.inputs), "configuration": "italy",
        }
        workflow, inputs = _build_shakemap_boundary({"shakemap": settings}, self.path)
        self.addCleanup(workflow.close)
        self.assertIsInstance(inputs, PreparedShakeMapInputs)
        self.assertEqual(workflow.unresolved(), [])
        with self.assertRaises(ValueError):
            _build_shakemap_boundary({"shakemap": {**settings, "configuration": "old/path"}}, self.path)


if __name__ == "__main__":
    unittest.main()
