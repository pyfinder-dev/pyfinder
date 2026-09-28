"""Caller recovery uses exact service facts and never guesses remote acceptance."""

from copy import deepcopy
import unittest
import logging
from unittest.mock import patch
from urllib.error import URLError

from pyfinder.services.shakemap_diagnostics import regional_configuration_failure, safe_text, error_diagnostic
from tests.unit import test_scheduler_shakemap as harness

CALCULATION_ID = harness.CALCULATION_ID
from tests.unit.test_shakemap_client import acknowledgement, detail, row


class EvidenceDouble:
    """Keep immutable snapshots, with no disk/service/SMTP side effects."""

    def __init__(self):
        self.captures = []
        self.fail = False
        self.scheduler = None
        self.enqueued = []
        self.drains = 0

    def capture(self, record, diagnostic, **arguments):
        if self.fail:
            raise OSError("evidence disk unavailable")
        self.captures.append((deepcopy(record), deepcopy(diagnostic), arguments))
        return {"attempt": record["attempt_id"], "sequence": record["internal_sequence"]}

    def capture_finder(self, *arguments):
        pass

    def drain_pending(self):
        self.drains += 1
        if self.scheduler is not None:
            assert not self.scheduler._shakemap_phase_lock._is_owned()

    def enqueue(self, *arguments):
        self.enqueued.append(deepcopy(arguments))

    def close(self):
        pass


class RegionalRecoveryTests(unittest.TestCase):
    """Reuse the real scheduler/SQLite harness, including its lifecycle regressions."""

    # Reuse the boundary harness without rerunning its unrelated test methods.
    setUp = harness.SchedulerShakeMapTests.setUp
    close_schedulers = harness.SchedulerShakeMapTests.close_schedulers
    scheduler = harness.SchedulerShakeMapTests.scheduler
    seed = harness.SchedulerShakeMapTests.seed
    meta = harness.SchedulerShakeMapTests.meta
    response = harness.SchedulerShakeMapTests.response

    def regional_response(self, code="configuration_materialization_failed", sequence=42):
        provenance = {
            "event_id": CALCULATION_ID, "internal_sequence": sequence,
            "configuration": {
                "selected": "italy", "materialization": {
                    "selected_configuration": "italy", "materialized": False,
                    "failure": {"type": "NativeProfileError", "stage": "regional_sources"},
                },
            },
        }
        failure = {"code": code, "message": "Selected regional configuration is unavailable"}
        if code == "native_configuration_failed":
            failure["configuration_error"] = {
                "origin": "native_configured_module", "exception_type": "ModuleNotFoundError",
                "reference": "regional.model",
            }
        return 200, detail(row(
            sequence=sequence, status="FAILED", provenance=provenance,
            configuration={"selected": "italy"}, failure=failure,
        ), event_id=CALCULATION_ID)

    def regional_scheduler(self, *responses, overwrite=True):
        scheduler, transport = self.scheduler(
            (202, acknowledgement(event_id=CALCULATION_ID, requested_configuration="italy", overwrite=overwrite)),
            *responses,
        )
        scheduler.configuration["shakemap"].update(configuration="italy", overwrite=overwrite)
        notifier = EvidenceDouble()
        notifier.scheduler = scheduler
        scheduler.shakemap_notifier = notifier
        self.seed(scheduler)
        scheduler.run_once()
        return scheduler, transport, notifier

    def global_ack(self, overwrite=True):
        return 202, acknowledgement(event_id=CALCULATION_ID, internal_sequence=43, overwrite=overwrite)

    def test_regional_failure_retained_before_global_success_same_id(self):
        scheduler, transport, notifier = self.regional_scheduler(
            self.regional_response(), self.global_ack(False), self.response(sequence=43), overwrite=False,
        )
        scheduler.run_once()
        self.assertEqual(len(notifier.captures), 1)
        self.assertFalse(notifier.captures[0][2]["final"])
        self.assertEqual([call[0] for call in transport.calls], ["POST", "GET", "POST"])
        scheduler.run_once()
        self.assertEqual(self.meta(scheduler)["status"], "completed")
        root = scheduler.tracker.get_execution_id("event-1", "RRSM", 5)
        chain = scheduler.shakemap_workflow.attempt_chain(root)
        self.assertEqual([item["request"]["configuration"] for item in chain], ["italy", "global"])
        self.assertEqual([item["internal_sequence"] for item in chain], [42, 43])
        self.assertEqual({item["request"]["event_id"] for item in chain}, {CALCULATION_ID})
        self.assertTrue(all(item["request"]["overwrite"] is False for item in chain))
        self.assertEqual(scheduler._finder_manager_class.calls, 1)
        self.assertEqual(notifier.captures[-1][1]["initial_configuration"], "italy")
        self.assertEqual(notifier.captures[-1][1]["final_outcome"], "SUCCESS")

    def test_confirmed_native_configuration_failure_allows_one_global_failure(self):
        scheduler, transport, notifier = self.regional_scheduler(
            self.regional_response("native_configuration_failed"), self.global_ack(),
            self.response(status="FAILED", sequence=43),
        )
        scheduler.run_once()
        scheduler.run_once()
        scheduler.run_once()
        self.assertEqual(self.meta(scheduler)["status"], "failed")
        self.assertEqual([call[0] for call in transport.calls].count("POST"), 2)
        self.assertEqual(notifier.captures[-1][1]["final_outcome"], "FAILED")

    def test_unrelated_native_failure_never_falls_back(self):
        scheduler, transport, _ = self.regional_scheduler(self.regional_response("native_exit"))
        scheduler.run_once()
        self.assertEqual(self.meta(scheduler)["status"], "failed")
        self.assertEqual([call[0] for call in transport.calls], ["POST", "GET"])

    def test_capture_failure_holds_terminal_job_without_get_or_post_retry(self):
        scheduler, transport, notifier = self.regional_scheduler(
            self.regional_response(), self.global_ack(), self.response(sequence=43),
        )
        notifier.fail = True
        scheduler.run_once()
        self.assertEqual([call[0] for call in transport.calls], ["POST", "GET"])
        root = scheduler.tracker.get_execution_id("event-1", "RRSM", 5)
        self.assertTrue(scheduler.shakemap_workflow.evidence_pending(root))
        notifier.fail = False
        scheduler.run_once()
        self.assertEqual([call[0] for call in transport.calls], ["POST", "GET", "POST"])
        self.assertFalse(scheduler.shakemap_workflow.evidence_pending(root))
        scheduler.run_once()
        self.assertEqual(self.meta(scheduler)["status"], "completed")

    def test_uncertain_global_acceptance_never_resubmits(self):
        scheduler, transport, _ = self.regional_scheduler(self.regional_response(), URLError("lost ack"))
        scheduler.run_once()
        scheduler.run_once()
        scheduler.run_once()
        self.assertEqual([call[0] for call in transport.calls], ["POST", "GET", "POST"])
        root = scheduler.tracker.get_execution_id("event-1", "RRSM", 5)
        self.assertEqual(scheduler.shakemap_workflow.attempt_chain(root)[-1]["submission_state"], "UNCERTAIN")

    def test_forced_restart_does_not_start_regional_recovery(self):
        scheduler, _, _ = self.regional_scheduler()
        scheduler.shakemap_workflow.close()
        scheduler.tracker.close()
        self.schedulers.remove(scheduler)
        restarted, transport = self.scheduler(self.regional_response())
        restarted.shakemap_notifier = EvidenceDouble()
        restarted.run_once()
        self.assertEqual(self.meta(restarted)["status"], "failed")
        self.assertEqual([call[0] for call in transport.calls], ["GET"])
        self.assertEqual(restarted.shakemap_notifier.captures[-1][1]["final_outcome"], "INTERRUPTED")

    def test_prepared_fallback_aborted_after_restart_without_post(self):
        scheduler, _, notifier = self.regional_scheduler(self.regional_response())
        with patch.object(scheduler, "_dispatch_shakemap_submissions"):
            scheduler._poll_shakemap_once()
        root = scheduler.tracker.get_execution_id("event-1", "RRSM", 5)
        self.assertEqual(scheduler.shakemap_workflow.submission_attempt_id(root), root + ":global")
        scheduler.shakemap_workflow.close()
        scheduler.tracker.close()
        self.schedulers.remove(scheduler)
        restarted, transport = self.scheduler()
        restarted.shakemap_notifier = EvidenceDouble()
        restarted.run_once()
        self.assertEqual(transport.calls, [])
        self.assertEqual(restarted.shakemap_notifier.captures[-1][1]["fallback_submission_state"], "ABORTED_BEFORE_SUBMISSION")

    def test_wrong_sequence_cannot_authorize_recovery(self):
        scheduler, transport, _ = self.regional_scheduler(self.regional_response())
        scheduler.shakemap_workflow.poll(scheduler.tracker.get_execution_id("event-1", "RRSM", 5))
        root = scheduler.tracker.get_execution_id("event-1", "RRSM", 5)
        value = scheduler.shakemap_workflow.get(root)
        self.assertTrue(regional_configuration_failure(value))
        value["observation"]["details"]["provenance"]["internal_sequence"] = 999
        self.assertFalse(regional_configuration_failure(value))

    def test_diagnostics_strip_secrets_paths_and_control_characters(self):
        value = safe_text("failed\x00 password=hide token=abc http://name:secret@host /private/operator/path")
        for private in ("hide", "abc", "secret@", "/private", "\x00"):
            self.assertNotIn(private, value)

    def test_restart_observes_accepted_global_without_replay_or_reopening(self):
        scheduler, _, _ = self.regional_scheduler(self.regional_response(), self.global_ack())
        scheduler.run_once()
        scheduler.shakemap_workflow.close()
        scheduler.tracker.close()
        self.schedulers.remove(scheduler)
        restarted, transport = self.scheduler(self.response(sequence=43))
        restarted.shakemap_notifier = EvidenceDouble()
        restarted.run_once()
        self.assertEqual(self.meta(restarted)["status"], "failed")
        self.assertEqual([call[0] for call in transport.calls], ["GET"])
        self.assertEqual(restarted.shakemap_notifier.captures[-1][0]["internal_sequence"], 43)
        self.assertEqual(restarted.shakemap_notifier.captures[-1][1]["status"], "SUCCESS")
        self.assertEqual(restarted.shakemap_notifier.captures[-1][1]["final_outcome"], "INTERRUPTED")

    def test_unsent_global_input_failure_ends_chain_without_finder_retry(self):
        scheduler, transport, notifier = self.regional_scheduler(self.regional_response())
        with patch.object(scheduler.shakemap_inputs, "submission", side_effect=OSError("input unavailable")):
            scheduler.run_once()
        self.assertEqual(self.meta(scheduler)["status"], "failed")
        self.assertEqual(scheduler._finder_manager_class.calls, 1)
        self.assertEqual([call[0] for call in transport.calls], ["POST", "GET"])
        self.assertEqual(notifier.captures[-1][1]["fallback_submission_state"], "NOT_SENT")
        self.assertEqual(notifier.captures[-1][1]["local_error"], "input unavailable")

    def test_caller_only_success_and_failure_drain_with_adapter_disabled(self):
        scheduler, transport = self.scheduler()
        scheduler.shakemap_workflow.close()
        scheduler.shakemap_workflow = None
        scheduler.shakemap_inputs = None
        notifier = EvidenceDouble()
        notifier.scheduler = scheduler
        scheduler.shakemap_notifier = notifier
        self.seed(scheduler)
        scheduler.run_once()
        self.assertEqual(notifier.enqueued[-1][1]["final_outcome"], "SUCCESS")
        self.assertIsNone(notifier.enqueued[-1][1]["status"])
        scheduler.run_once()
        self.assertEqual(notifier.drains, 2)
        self.assertEqual(transport.calls, [])
        scheduler.close()
        self.assertEqual(notifier.drains, 3)
        self.schedulers.remove(scheduler)

    def test_shutdown_drains_after_interruption_snapshot(self):
        scheduler, _, notifier = self.regional_scheduler()
        scheduler.close()
        self.schedulers.remove(scheduler)
        self.assertEqual(notifier.captures[-1][1]["final_outcome"], "INTERRUPTED")
        self.assertGreater(notifier.drains, 1)

    def test_native_success_without_products_reports_failed_chain(self):
        unavailable = row(status="SUCCESS", products_ready=False)
        scheduler, _ = self.scheduler(
            (202, acknowledgement(event_id=CALCULATION_ID)),
            (200, detail(archives=[unavailable], event_id=CALCULATION_ID)),
        )
        notifier = EvidenceDouble()
        scheduler.shakemap_notifier = notifier
        self.seed(scheduler)
        scheduler.run_once()
        scheduler.run_once()
        self.assertEqual(self.meta(scheduler)["status"], "failed")
        self.assertEqual(notifier.captures[-1][1]["status"], "SUCCESS")
        self.assertEqual(notifier.captures[-1][1]["final_outcome"], "FAILED")

    def test_clean_shutdown_late_success_keeps_interrupted_caller_outcome(self):
        scheduler, _, notifier = self.regional_scheduler()
        scheduler.close()
        self.schedulers.remove(scheduler)
        self.assertEqual(notifier.captures[-1][1]["final_outcome"], "INTERRUPTED")
        restarted, transport = self.scheduler(self.response())
        restarted.shakemap_notifier = EvidenceDouble()
        restarted.run_once()
        result = restarted.shakemap_notifier.captures[-1][1]
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["final_outcome"], "INTERRUPTED")
        self.assertEqual([call[0] for call in transport.calls], ["GET"])

    def test_transition_file_log_keeps_identity_configuration_and_sequence(self):
        scheduler, _, _ = self.regional_scheduler(
            self.regional_response(), self.global_ack(), self.response(sequence=43),
        )
        path = self.path.parent / "diagnostics.log"
        handler = logging.FileHandler(path)
        scheduler.logger.addHandler(handler)
        previous = scheduler.logger.level
        scheduler.logger.setLevel(logging.INFO)
        try:
            scheduler.run_once()
            scheduler.run_once()
            handler.flush()
            text = path.read_text()
            for fact in (CALCULATION_ID, "requested=italy", "requested=global", "sequence=42", "sequence=43"):
                self.assertIn(fact, text)
        finally:
            scheduler.logger.removeHandler(handler)
            scheduler.logger.setLevel(previous)
            handler.close()

    def test_http_reason_projection_retains_public_action_without_private_body(self):
        from pyfinder.services.shakemap_client import ShakeMapHTTPError
        error = ShakeMapHTTPError(503, {
            "error": "service_unavailable", "message": "Selected data unavailable token=hidden",
            "private": "never serialize this arbitrary field",
        })
        text = error_diagnostic(error, operation="submission")
        self.assertIn("HTTP 503", text)
        self.assertIn("Selected data unavailable", text)
        self.assertIn("readiness", text)
        self.assertNotIn("hidden", text)
        self.assertNotIn("arbitrary", text)
