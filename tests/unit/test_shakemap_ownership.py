"""Independent invocation ownership protects evidence before same-ID replacement."""

import json
from pathlib import Path
import unittest
from urllib.error import URLError

from pyfinder.services.shakemap_inputs import PreparedShakeMapInputs, ShakeMapCalculationBusy
from tests.unit import test_scheduler_shakemap as harness
from tests.unit import test_shakemap_recovery as recovery
from tests.unit.test_shakemap_client import acknowledgement


class CalculationOwnershipTests(unittest.TestCase):
    setUp = harness.SchedulerShakeMapTests.setUp
    close_schedulers = harness.SchedulerShakeMapTests.close_schedulers
    scheduler = harness.SchedulerShakeMapTests.scheduler
    seed = harness.SchedulerShakeMapTests.seed
    meta = harness.SchedulerShakeMapTests.meta
    response = harness.SchedulerShakeMapTests.response

    def owner(self):
        path = self.inputs / ".pyfinder-locks" / (harness.CALCULATION_ID + ".lock")
        return json.loads(path.read_text()) if path.read_text() else None

    def test_independent_database_cannot_replace_native_job_before_evidence(self):
        first, transport = self.scheduler(
            (202, acknowledgement(event_id=harness.CALCULATION_ID)), self.response(),
        )
        self.seed(first)
        first.run_once()
        first_owner = self.owner()
        self.assertEqual(first_owner["database_path"], str(self.path))

        self.path = self.path.with_name("other-invocation.sqlite")
        second, second_transport = self.scheduler()
        self.seed(second)
        second.run_once()
        self.assertEqual(self.meta(second)["status"], "failed")
        self.assertEqual(self.meta(second)["retry_count"], 0)
        self.assertEqual(second_transport.calls, [])
        self.assertEqual(self.owner(), first_owner)

        # The first owner may finish normally; only its terminal evidence and
        # finalization release the marker. Its POST was sent exactly once.
        first.run_once()
        self.assertEqual(self.meta(first)["status"], "completed")
        self.assertIsNone(self.owner())
        self.assertEqual([call[0] for call in transport.calls], ["POST", "GET"])

    def test_terminal_evidence_failure_keeps_owner_and_blocks_other_database(self):
        first, _ = self.scheduler(
            (202, acknowledgement(event_id=harness.CALCULATION_ID)), self.response(),
        )
        evidence = recovery.EvidenceDouble()
        evidence.fail = True
        first.shakemap_notifier = evidence
        self.seed(first)
        first.run_once()
        owner = self.owner()
        first.run_once()
        self.assertEqual(self.owner(), owner)
        self.assertEqual(self.meta(first)["status"], "processing")
        self.path = self.path.with_name("evidence-held.sqlite")
        second, second_transport = self.scheduler()
        self.seed(second)
        second.run_once()
        self.assertEqual(second_transport.calls, [])
        self.assertEqual(self.owner(), owner)
        # Permit fixture shutdown to retain the same terminal evidence without
        # introducing a second remote observation in this focused regression.
        evidence.fail = False
        first.shakemap_workflow.clear_local_error(
            first.tracker.get_execution_id("event-1", "RRSM", 5)
        )

    def test_uncertain_submission_keeps_owner_after_shutdown(self):
        first, transport = self.scheduler(URLError("response lost"))
        self.seed(first)
        first.run_once()
        owner = self.owner()
        first.close()
        self.assertEqual(self.owner(), owner)
        self.path = self.path.with_name("after-stop.sqlite")
        second, second_transport = self.scheduler()
        self.seed(second)
        second.run_once()
        self.assertEqual(second_transport.calls, [])
        self.assertEqual(self.meta(second)["status"], "failed")
        self.assertEqual(len(transport.calls), 1)

    def test_definite_submission_rejection_releases_owner_before_paced_retry(self):
        scheduler, _ = self.scheduler((400, {"detail": "invalid request"}))
        self.seed(scheduler)
        scheduler.run_once()
        self.assertIsNone(self.owner())
        self.assertEqual(self.meta(scheduler)["status"], "pending")
        self.assertEqual(self.meta(scheduler)["retry_count"], 1)

    def test_unreadable_marker_fails_closed_and_inode_survives_release(self):
        inputs = PreparedShakeMapInputs(self.inputs)
        inputs.claim("same", "execution", self.path)
        marker = self.inputs / ".pyfinder-locks/same.lock"
        inode = marker.stat().st_ino
        with self.assertRaises(ShakeMapCalculationBusy):
            inputs.release("same", "someone-else")
        inputs.release("same", "execution")
        self.assertEqual(marker.stat().st_ino, inode)
        self.assertEqual(marker.read_bytes(), b"")
        marker.write_text("incomplete ownership write")
        with self.assertRaises(ShakeMapCalculationBusy):
            inputs.claim("same", "new", self.path)
        self.assertEqual(marker.read_text(), "incomplete ownership write")

    def test_same_owner_can_continue_but_same_token_from_other_database_cannot(self):
        inputs = PreparedShakeMapInputs(self.inputs)
        inputs.claim("same", "execution", self.path)
        second = PreparedShakeMapInputs(self.inputs)
        second.claim("same", "execution", self.path)
        with self.assertRaises(ShakeMapCalculationBusy):
            second.claim("same", "execution", self.path.with_name("other.sqlite"))


class RegionalOwnershipTests(unittest.TestCase):
    setUp = recovery.RegionalRecoveryTests.setUp
    close_schedulers = recovery.RegionalRecoveryTests.close_schedulers
    scheduler = recovery.RegionalRecoveryTests.scheduler
    seed = recovery.RegionalRecoveryTests.seed
    meta = recovery.RegionalRecoveryTests.meta
    response = recovery.RegionalRecoveryTests.response
    regional_response = recovery.RegionalRecoveryTests.regional_response
    global_ack = recovery.RegionalRecoveryTests.global_ack
    regional_scheduler = recovery.RegionalRecoveryTests.regional_scheduler

    def test_regional_global_pair_keeps_original_owner_and_restart_releases_after_observation(self):
        scheduler, transport, _ = self.regional_scheduler(self.regional_response(), self.global_ack())
        marker = self.inputs / ".pyfinder-locks" / (harness.CALCULATION_ID + ".lock")
        original = json.loads(marker.read_text())
        scheduler.run_once()
        self.assertEqual(json.loads(marker.read_text()), original)
        self.assertEqual([call[0] for call in transport.calls], ["POST", "GET", "POST"])

        # Model process loss by closing connections without graceful draining.
        # The restarted continuous owner observes the accepted job, never POSTs.
        scheduler.shakemap_workflow.close()
        scheduler.tracker.close()
        self.schedulers.remove(scheduler)
        restarted, restarted_transport = self.scheduler(self.response(sequence=43))
        restarted.shakemap_notifier = recovery.EvidenceDouble()
        restarted.run_once()
        self.assertEqual([call[0] for call in restarted_transport.calls], ["GET"])
        self.assertEqual(marker.read_bytes(), b"")
        self.assertEqual(self.meta(restarted)["status"], "failed")
