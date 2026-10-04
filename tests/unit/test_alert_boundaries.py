"""Offline checks for the new email/evidence boundary; no real SMTP."""

import copy
import json
from pathlib import Path
import smtplib
import sqlite3
import unittest
import tempfile
from pyfinder.services.alert import (
    AlertSettings,
    deliver_message,
    load_alert_settings,
    render_message,
)
from pyfinder.services.alert_delivery import AlertService
from pyfinder.services.alert_evidence import EvidenceStore


def settings(required=()):
    return AlertSettings(
        "configured.invalid",
        2525,
        "sender@example.invalid",
        [
            {
                "name": "ops",
                "recipients": ["recipient@example.invalid"],
                "outcomes": ["SUCCESS", "FAILED"],
                "required_attachments": list(required),
            }
        ],
    )


class SMTP:
    behavior = "success"
    calls = []

    def __init__(self, host, port, **options):
        self.calls.append((host, port, options))

    def starttls(self, **kwargs):
        pass

    def login(self, *args):
        pass

    def close(self):
        pass

    def send_message(self, *args, **kwargs):
        self.calls.append("send")
        if self.behavior == "timeout":
            raise TimeoutError("private response")
        if self.behavior == "reject":
            raise smtplib.SMTPDataError(554, b"private response")
        if self.behavior == "partial":
            return {"private-recipient": (550, b"private response")}
        return {}


def reset_smtp():
    SMTP.calls = []
    SMTP.behavior = "success"


def record(tmp_path, sequence=1, status="SUCCESS"):
    service = tmp_path / "native"
    event = service / ".service/events/event"
    (event / "logs").mkdir(parents=True, exist_ok=True)
    identity = {"event_id": "event", "internal_sequence": sequence}
    (event / "status.json").write_text(json.dumps(identity))
    (event / "provenance.json").write_text(json.dumps(identity))
    (event / "product-manifest.json").write_text(json.dumps(identity))
    (event / "logs/service.log").write_text("native log")
    row = {
        "attempt_id": f"attempt-{sequence}",
        "internal_sequence": sequence,
        "request": {"event_id": "event", "configuration": "global"},
        "observation": {"details": {"internal_sequence": sequence, "status": status}},
    }
    return (service, event, row)


class AlertBoundaryTests(unittest.TestCase):
    """Exercise immutable evidence and delivery outcomes without network I/O."""

    def setUp(self):
        reset_smtp()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def _assert_transport_classification(self, behavior, state):
        SMTP.behavior = behavior
        result = deliver_message(
            settings(),
            ["receiver@example.invalid"],
            render_message(settings(), {}, {}),
            smtp_factory=SMTP,
        )
        self.assertEqual(result["state"], state)
        self.assertEqual(SMTP.calls[0][:2], ("configured.invalid", 2525))
        self.assertEqual(SMTP.calls[0][2]["timeout"], 30)
        self.assertNotIn("private", json.dumps(result))

    def test_render_is_utc_escaped_and_pure(self):
        diagnostic = {
            "event_id": "id",
            "status": "SUCCESS",
            "origin_time_epoch": 0,
            "reason": "<danger>",
        }
        previous = copy.deepcopy(diagnostic)
        message = render_message(settings(), diagnostic, {"data_0": b"exact bytes"})
        self.assertEqual(diagnostic, previous)
        self.assertIn(
            "1970-01-01T00:00:00+00:00",
            message.get_body(preferencelist=("plain",)).get_content(),
        )
        self.assertIn(
            "&lt;danger&gt;", message.get_body(preferencelist=("html",)).get_content()
        )
        self.assertTrue("Bcc" not in message and "recipient" not in message["To"])
        self.assertEqual(
            next(message.iter_attachments()).get_payload(decode=True), b"exact bytes"
        )

    def test_disabled_and_invalid_configuration(self):
        self.assertIs(load_alert_settings(), None)
        path = self.root / "secret.json"
        path.write_text('{"password":"must-not-leak"}')
        with self.assertRaises(ValueError) as error:
            load_alert_settings(path)
        self.assertNotIn("must-not-leak", str(error.exception))

    def test_finder_evidence_is_immutable(self):
        store = EvidenceStore(self.root / "evidence", self.root / "native")
        original = self.root / "data_0"
        original.write_bytes(b"first exact invocation")
        one = store.capture_finder("private-attempt-1", {"data_0": original})
        original.write_bytes(b"next deliberate same ID")
        two = store.capture_finder("private-attempt-2", {"data_0": original})
        self.assertEqual(
            (Path(one["directory"]) / "data_0").read_bytes(), b"first exact invocation"
        )
        self.assertEqual(
            (Path(two["directory"]) / "data_0").read_bytes(), b"next deliberate same ID"
        )
        self.assertEqual(
            store.capture_finder("private-attempt-1", {"data_0": original}), one
        )

    def test_native_evidence_survives_replacement(self):
        service, event, row = record(self.root)
        store = EvidenceStore(self.root / "evidence", service)
        one = store.capture_shakemap(row)
        (event / "logs/service.log").write_text("replacement log")
        (event / "status.json").write_text(
            json.dumps({"event_id": "event", "internal_sequence": 2})
        )
        self.assertEqual(store.capture_shakemap(row), one)
        self.assertEqual(
            (Path(one["directory"]) / "service.log").read_text(), "native log"
        )
        self.assertEqual(
            one["metadata"]["unavailable"],
            [
                "shake.log",
                "intensity.jpg",
                "event.xml",
                "event_dat.xml",
                "rupture.json",
            ],
        )

    def test_wrong_sequence_blocks_capture(self):
        service, event, row = record(self.root)
        (event / "status.json").write_text(
            json.dumps({"event_id": "event", "internal_sequence": 2})
        )
        with self.assertRaises(ValueError):
            EvidenceStore(self.root / "evidence", service).capture_shakemap(row)

    def test_redirected_finder_file_is_refused(self):
        original = self.root / "original"
        original.write_text("data")
        link = self.root / "link"
        link.symlink_to(original)
        with self.assertRaises(ValueError):
            EvidenceStore(self.root / "evidence", self.root / "native").capture_finder(
                "attempt", {"data_0": link}
            )

    def test_durable_send_deduplicates_poll_and_restart(self):
        db = self.root / "workflow.sqlite"
        store = EvidenceStore(self.root / "evidence", self.root / "native")
        alert = AlertService(db, store, settings(), smtp_factory=SMTP)
        diagnostic = {"event_id": "same-id", "status": "FAILED"}
        alert.enqueue("one", diagnostic)
        alert.enqueue("one", diagnostic)
        alert.drain_pending()
        alert.drain_pending()
        alert.close()
        restarted = AlertService(db, store, settings(), smtp_factory=SMTP)
        restarted.drain_pending()
        restarted.enqueue("two", diagnostic)
        restarted.drain_pending()
        self.assertEqual(SMTP.calls.count("send"), 2)
        restarted.close()

    def test_ambiguous_is_not_replayed(self):
        db = self.root / "workflow.sqlite"
        store = EvidenceStore(self.root / "evidence", self.root / "native")
        SMTP.behavior = "timeout"
        alert = AlertService(db, store, settings(), smtp_factory=SMTP)
        alert.enqueue("one", {"event_id": "event", "status": "FAILED"})
        alert.drain_pending()
        self.assertFalse(alert.retry_failed("one", "ops"))
        alert.close()
        restarted = AlertService(db, store, settings(), smtp_factory=SMTP)
        restarted.drain_pending()
        self.assertEqual(SMTP.calls.count("send"), 1)
        restarted.close()

    def test_definite_rejection_can_be_explicitly_requeued(self):
        db = self.root / "workflow.sqlite"
        store = EvidenceStore(self.root / "evidence", self.root / "native")
        SMTP.behavior = "reject"
        alert = AlertService(db, store, settings(), smtp_factory=SMTP)
        alert.enqueue("one", {"status": "FAILED"})
        alert.drain_pending()
        self.assertTrue(alert.retry_failed("one", "ops"))
        SMTP.behavior = "success"
        alert.drain_pending()
        self.assertEqual(SMTP.calls.count("send"), 2)
        alert.close()

    def test_required_absent_blocks_without_smtp(self):
        db = self.root / "workflow.sqlite"
        store = EvidenceStore(self.root / "evidence", self.root / "native")
        alert = AlertService(db, store, settings(["data_0"]), smtp_factory=SMTP)
        alert.enqueue("one", {"status": "FAILED"})
        alert.drain_pending()
        with sqlite3.connect(db) as con:
            self.assertEqual(
                con.execute("SELECT state FROM alert_deliveries").fetchone()[0],
                "BLOCKED",
            )
        self.assertEqual(SMTP.calls, [])
        alert.close()

    def test_capture_does_not_send_and_retains_both_native_attempts(self):
        db = self.root / "workflow.sqlite"
        service, event, row = record(self.root, status="FAILED")
        alert = AlertService(
            db,
            EvidenceStore(self.root / "evidence", service),
            settings(),
            smtp_factory=SMTP,
        )
        alert.capture(
            row,
            {"status": "FAILED", "selected_configuration": "region"},
            execution_id="chain",
            final=False,
        )
        _, _, second = record(self.root, sequence=2, status="SUCCESS")
        alert.capture(
            second,
            {
                "status": "SUCCESS",
                "selected_configuration": "global",
                "requested_configuration": "region",
            },
            execution_id="chain",
            final=True,
        )
        self.assertEqual(SMTP.calls, [])
        with sqlite3.connect(db) as con:
            payload = json.loads(
                con.execute("SELECT payload FROM alert_deliveries").fetchone()[0]
            )
        self.assertEqual(
            [item["status"] for item in payload["diagnostic"]["attempts"]],
            ["FAILED", "SUCCESS"],
        )
        alert.drain_pending()
        self.assertEqual(SMTP.calls.count("send"), 1)
        alert.close()

    def test_interrupted_sending_becomes_unknown(self):
        db = self.root / "workflow.sqlite"
        store = EvidenceStore(self.root / "evidence", self.root / "native")
        alert = AlertService(db, store, settings(), smtp_factory=SMTP)
        alert.enqueue("one", {"status": "FAILED"})
        alert.close()
        with sqlite3.connect(db) as con:
            con.execute("UPDATE alert_deliveries SET state='SENDING'")
        restarted = AlertService(db, store, settings(), smtp_factory=SMTP)
        restarted.drain_pending()
        with sqlite3.connect(db) as con:
            self.assertEqual(
                con.execute("SELECT state FROM alert_deliveries").fetchone()[0],
                "UNKNOWN",
            )
        self.assertEqual(SMTP.calls, [])
        restarted.close()

    def test_transport_success(self):
        self._assert_transport_classification("success", "SENT")

    def test_transport_reject(self):
        self._assert_transport_classification("reject", "FAILED")

    def test_transport_partial(self):
        self._assert_transport_classification("partial", "PARTIAL")

    def test_transport_timeout(self):
        self._assert_transport_classification("timeout", "UNKNOWN")


class AlertConfigurationAndRecoveryTests(unittest.TestCase):
    """Exercise only temporary configuration and fake transport, never live secrets."""

    def setUp(self):
        reset_smtp()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.store = EvidenceStore(self.root / "evidence", self.root / "native")
        self.db = self.root / "alerts.sqlite"

    def test_legacy_configuration_preserves_recipients_sender_and_subject(self):
        path = self.root / "legacy.json"
        path.write_text(
            json.dumps(
                {
                    "smtp_server": "actual-configured.invalid",
                    "smtp_port": 587,
                    "from": "sender@example.invalid",
                    "to": ["recipient@example.invalid"],
                    "address": "historically-unused@example.invalid",
                    "password": "fake-only",
                    "subject": "Existing configured subject",
                }
            )
        )
        configured = load_alert_settings(path)
        self.assertEqual(configured.host, "actual-configured.invalid")
        self.assertEqual(
            configured.lists[0]["recipients"], ["recipient@example.invalid"]
        )
        self.assertEqual(configured.username, configured.sender)
        self.assertEqual(
            render_message(configured, {}, {})["Subject"], "Existing configured subject"
        )
        self.assertNotIn("fake-only", repr(configured))
        self.assertNotIn("recipient@example.invalid", repr(configured))

    def test_explicit_disable_never_discovers_private_files(self):
        from unittest import mock
        from pyfinder.services.alert import configured_alert_settings

        with (
            mock.patch("pathlib.Path.exists") as exists,
            mock.patch("pathlib.Path.read_text") as read,
        ):
            self.assertIsNone(
                configured_alert_settings(environment={"PYFINDER_ALERT_CONFIG": ""})
            )
        exists.assert_not_called()
        read.assert_not_called()

    def test_legacy_discovery_order_without_reading_credentials(self):
        from unittest import mock
        from pyfinder.services.alert import configured_alert_settings

        with (
            mock.patch("pathlib.Path.exists", side_effect=[False, True]),
            mock.patch(
                "pyfinder.services.alert.load_alert_settings", return_value="validated"
            ) as load,
        ):
            self.assertEqual(configured_alert_settings(environment={}), "validated")
        self.assertEqual(load.call_args.args[0].name, ".pyfinder_alert_config.json")
        self.assertEqual(load.call_args.args[0].parent.name, "pyfinder")

    def test_uncertain_capture_is_incomplete_and_not_native_failed(self):
        service = AlertService(self.db, self.store, settings(), smtp_factory=SMTP)
        self.addCleanup(service.close)
        uncertain = {
            "attempt_id": "private",
            "internal_sequence": None,
            "submission_state": "UNCERTAIN",
            "request": {"event_id": "event"},
            "observation": None,
        }
        bundle = service.capture(
            uncertain,
            {"status": "FAILED", "final_outcome": "UNKNOWN"},
            execution_id="execution",
            final=True,
        )
        self.assertEqual(bundle["metadata"]["kind"], "incomplete-observation")
        self.assertFalse((self.root / "native").exists())
        service.drain_pending()
        self.assertEqual(SMTP.calls, [])
        message = render_message(
            settings(), {"status": "FAILED", "final_outcome": "UNKNOWN"}, {}
        )
        self.assertIn("UNKNOWN", message["Subject"])

    def test_changed_audience_blocks_queued_send(self):
        service = AlertService(self.db, self.store, settings(), smtp_factory=SMTP)
        self.addCleanup(service.close)
        service.enqueue("execution", {"status": "FAILED"})
        service.settings.lists[0]["recipients"] = ["different@example.invalid"]
        service.drain_pending()
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(
                connection.execute("SELECT state FROM alert_deliveries").fetchone()[0],
                "BLOCKED",
            )
        self.assertEqual(SMTP.calls, [])

    def test_operator_listing_and_failed_only_retry_do_not_send(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from pyfinder.services.alert_delivery import main

        service = AlertService(self.db, self.store, settings(), smtp_factory=SMTP)
        self.addCleanup(service.close)
        service.enqueue("execution", {"status": "FAILED"})
        SMTP.behavior = "reject"
        service.drain_pending()
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["--database", str(self.db), "list"]), 0)
        self.assertEqual(json.loads(output.getvalue())["state"], "FAILED")
        self.assertNotIn("recipient@example.invalid", output.getvalue())
        self.assertEqual(
            main(["--database", str(self.db), "retry-failed", "execution", "ops"]), 0
        )
        self.assertEqual(SMTP.calls.count("send"), 1)

    def test_final_attempt_cannot_borrow_required_native_attachment(self):
        service_root, event, first = record(self.root, status="FAILED")
        service = AlertService(
            self.db,
            EvidenceStore(self.root / "native-evidence", service_root),
            settings(["product-manifest.json"]),
            smtp_factory=SMTP,
        )
        self.addCleanup(service.close)
        service.capture(first, {"status": "FAILED"}, execution_id="chain", final=False)
        _, _, second = record(self.root, sequence=2)
        (event / "product-manifest.json").unlink()
        service.capture(second, {"status": "SUCCESS"}, execution_id="chain", final=True)
        service.drain_pending()
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(
                connection.execute("SELECT state FROM alert_deliveries").fetchone()[0],
                "BLOCKED",
            )
        self.assertEqual(SMTP.calls, [])


class AlertAuditRegressionTests(unittest.TestCase):
    """Regressions at archive/restart boundaries found by independent audit."""

    def setUp(self):
        reset_smtp()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def test_archived_sequence_captures_original_tree_not_new_current(self):
        service, event, row = record(self.root, status="FAILED")
        archive = service / ".service/archive/event-20260927T010203000000Z"
        archive.mkdir(parents=True)
        event.rename(archive / "service")
        record(self.root, sequence=2)
        row["observation"]["scope"] = "archives"
        row["observation"]["details"]["shared_paths"] = {
            "archive": "/different/host/runtime/shakemap/.service/archive/"
            + archive.name,
        }
        store = EvidenceStore(self.root / "evidence", service)
        captured = store.capture_shakemap(row)
        self.assertEqual(captured["metadata"]["internal_sequence"], 1)
        self.assertEqual(
            json.loads((Path(captured["directory"]) / "provenance.json").read_text())[
                "internal_sequence"
            ],
            1,
        )

    def test_incomplete_record_does_not_shadow_later_terminal_evidence(self):
        service_root, event, terminal = record(self.root)
        store = EvidenceStore(self.root / "evidence", service_root)
        service = AlertService(
            self.root / "alerts.sqlite", store, settings(), smtp_factory=SMTP
        )
        self.addCleanup(service.close)
        incomplete = dict(terminal, observation=None)
        prior = service.capture(
            incomplete,
            {"final_outcome": "INTERRUPTED"},
            execution_id="chain",
            final=False,
        )
        complete = service.capture(
            terminal, {"status": "SUCCESS"}, execution_id="chain", final=True
        )
        self.assertNotEqual(prior["directory"], complete["directory"])
        self.assertTrue(Path(prior["directory"]).exists())
        with sqlite3.connect(self.root / "alerts.sqlite") as connection:
            payload = json.loads(
                connection.execute("SELECT payload FROM alert_deliveries").fetchone()[0]
            )
        self.assertIn("provenance.json", payload["bundles"][-1]["files"])

    def test_root_alias_is_normalized_but_artifact_symlinks_are_refused(self):
        physical = self.root / "physical"
        physical.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(physical, target_is_directory=True)
        store = EvidenceStore(alias / "evidence", alias / "native")
        self.assertEqual(store.root, physical / "evidence")
        source = physical / "data_0"
        source.write_bytes(b"exact")
        link = physical / "linked-data"
        link.symlink_to(source)
        with self.assertRaises(ValueError):
            store.capture_finder("attempt", {"data_0": link})


class FinDerEvidenceExecutionBoundaryTests(unittest.TestCase):
    """Use the real execute/lock boundary while replacing native computation."""

    def test_input_capture_owns_workspace_lock_and_forwards_execution_token(self):
        import fcntl
        from functools import partial
        from unittest import mock
        from tests.unit.test_finderexec_configuration import (
            FinDerExecutableConfigurationTests,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            helper = FinDerExecutableConfigurationTests()
            configuration = helper.application_configuration()
            configuration["finder-executable"]["output-root-folder"] = str(
                root / "runs"
            )
            executable = helper.construct_executable(
                "global", {"DATA_FOLDER": "source"}, configuration
            )
            store = EvidenceStore(root / "evidence", root / "native")
            captured = []

            def capture(execution_id, files):
                with (Path(executable.working_directory) / ".pyfinder.lock").open(
                    "a"
                ) as lock:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                captured.append(
                    (execution_id, store.capture_finder(execution_id, files))
                )

            def materialize(*args):
                workspace = Path(executable.working_directory)
                (workspace / "data_0").write_bytes(
                    b"exact data_0 generated for invocation"
                )
                (workspace / "pyfinder_amplitudes_to_Finder.txt").write_bytes(
                    b"exact station identities"
                )

            def native():
                self.assertEqual(captured[0][0], "scheduled-private-token")
                directory = Path(captured[0][1]["directory"])
                self.assertEqual(
                    (directory / "data_0").read_bytes(),
                    b"exact data_0 generated for invocation",
                )

            executable.evidence_callback = partial(capture, "scheduled-private-token")
            event = mock.Mock()
            event.get_event_id.return_value = "public-event"
            with (
                mock.patch.object(executable, "_check_finder_executable"),
                mock.patch.object(
                    executable, "_materialize_inputs", side_effect=materialize
                ),
                mock.patch.object(executable, "_run_finder", side_effect=native),
                mock.patch.object(executable, "_collect_finder_output"),
            ):
                executable.execute([], event, augmented_event_id="public-event_t00005")
            self.assertEqual(
                set(captured[0][1]["files"]),
                {"data_0", "pyfinder_amplitudes_to_Finder.txt"},
            )


class HumanAlertReportTests(unittest.TestCase):
    """Verify truthful labels and copied scientific values, not just SMTP calls."""

    def test_readable_fallback_distinguishes_native_and_local_outcomes(self):
        diagnostic = {
            "event_id": "same-calculation",
            "status": "SUCCESS",
            "final_outcome": "INTERRUPTED",
            "initial_configuration": "italy",
            "requested_configuration": "global",
            "selected_configuration": "global",
            "fallback_reason": "Missing <regional model>",
            "observed_at": "2026-09-27T12:34:56+02:00",
            "internal_sequence": 22,
            "action": "Review retained logs",
            "unknown_future_field": "not body content",
            "evidence": [{"unavailable": ["shake.log"]}],
        }
        previous = copy.deepcopy(diagnostic)
        message = render_message(settings(), diagnostic, {"service.log": b"log"})
        plain = message.get_body(preferencelist=("plain",)).get_content()
        html = message.get_body(preferencelist=("html",)).get_content()
        self.assertIn("Final workflow outcome: INTERRUPTED", plain)
        self.assertIn("ShakeMap job outcome: SUCCESS", plain)
        self.assertIn("Requested configuration: italy", plain)
        self.assertIn("Service-selected configuration: global", plain)
        self.assertIn("Observed at (UTC): 2026-09-27T10:34:56+00:00", plain)
        self.assertNotIn("Earthquake origin", plain)
        self.assertNotIn("unknown_future_field", plain)
        self.assertNotIn("not body content", plain)
        self.assertNotIn("<pre>", html)
        self.assertIn("&lt;regional model&gt;", html)
        self.assertIn("unavailable: shake.log", plain)
        self.assertIn("Unavailable for this attempt", plain)
        self.assertEqual(diagnostic, previous)
        machine = [
            part
            for part in message.iter_attachments()
            if part.get_filename() == "calculation-report.json"
        ][0]
        self.assertEqual(json.loads(machine.get_payload(decode=True)), diagnostic)

    def test_selected_summary_preserves_values_without_mutating_or_using_internal_time(
        self,
    ):
        from unittest import mock
        from pyfinder.findermanager import FinDerManager
        from pyfinder.finderutils import FinderEvent, FinderSolution

        manager = FinDerManager.__new__(FinDerManager)
        manager.logger = mock.Mock()
        manager.metadata = {
            "origin_time": "2026-09-27T12:00:00+02:00",
            "magnitude": 4.2,
            "magnitude_type": "ML",
            "latitude": 45.0,
            "longitude": 7.0,
            "depth": 6.0,
        }
        solution = FinderSolution(
            event=FinderEvent(
                origin_time_epoch=123456789,
                latitude="46.1",
                longitude=7.1,
                depth=8.0,
                magnitude=5.6,
            )
        )
        original_event = copy.deepcopy(solution.event.__dict__)
        original_metadata = copy.deepcopy(manager.metadata)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            service = AlertService(
                root / "alerts.sqlite",
                EvidenceStore(root / "evidence", root / "native"),
                settings(),
                smtp_factory=SMTP,
            )
            self.addCleanup(service.close)
            manager.evidence_callback = lambda files: service.capture_finder(
                "private-token", files
            )
            service.capture_finder(
                "private-token",
                {
                    "event-context.json": json.dumps(manager.metadata).encode(),
                    "data_0": b"exact",
                },
            )
            manager._capture_selected_solution_summary(solution)
            service.enqueue("private-token", {"event_id": "event", "status": "SUCCESS"})
            with sqlite3.connect(root / "alerts.sqlite") as connection:
                report = json.loads(
                    connection.execute(
                        "SELECT payload FROM alert_deliveries"
                    ).fetchone()[0]
                )["diagnostic"]
            self.assertEqual(report["event_context"]["magnitude"], 4.2)
            self.assertEqual(report["finder_solution"]["magnitude"], 5.6)
            self.assertEqual(report["finder_solution"]["depth"], 8.0)
            self.assertEqual(report["finder_solution"]["latitude"], 46.1)
            self.assertNotIn("magnitude_type", report["finder_solution"])
            text = (
                render_message(settings(), report, {})
                .get_body(preferencelist=("plain",))
                .get_content()
            )
            self.assertIn("2026-09-27T10:00:00+00:00", text)
            self.assertNotIn("123456789", text)
            self.assertEqual(solution.event.__dict__, original_event)
            self.assertEqual(manager.metadata, original_metadata)

    def test_unmaterialized_region_is_selection_not_a_claim_of_native_use(self):
        diagnostic = {
            "event_id": "event",
            "status": "FAILED",
            "selected_configuration": "italy",
            "requested_configuration": "italy",
            "materialized": False,
            "attempts": [
                {
                    "requested_configuration": "italy",
                    "selected_configuration": "italy",
                    "materialized": False,
                    "status": "FAILED",
                }
            ],
        }
        text = (
            render_message(settings(), diagnostic, {})
            .get_body(preferencelist=("plain",))
            .get_content()
        )
        self.assertIn("Service-selected configuration: italy", text)
        self.assertIn("Configuration materialized: False", text)
        self.assertNotIn("used italy", text)
        self.assertNotIn("actually used", text)
        self.assertIn("Native execution: No native execution evidence", text)
        self.assertIn(
            "Authoritative earthquake and scheduled update\nSummary: Unavailable", text
        )

    def test_failed_product_gate_does_not_change_reported_native_exit(self):
        message = render_message(
            settings(),
            {
                "status": "FAILED",
                "native_outcome": {"started": True, "exit_code": 0, "signal": None},
                "reason": "Required products unavailable",
            },
            {},
        )
        text = message.get_body(preferencelist=("plain",)).get_content()
        self.assertIn("ShakeMap job outcome: FAILED", text)
        self.assertIn(
            "Native execution: Started: True; exit code: 0; signal: None reported", text
        )

    def test_absent_selected_values_are_not_filled_from_catalogue(self):
        message = render_message(
            settings(),
            {
                "event_context": {"magnitude": 4.0},
                "finder_solution": {"latitude": None, "magnitude": None},
            },
            {},
        )
        text = message.get_body(preferencelist=("plain",)).get_content()
        self.assertIn("Magnitude: Unavailable", text)
        self.assertIn("Magnitude: 4.0", text)


if __name__ == "__main__":
    unittest.main()
