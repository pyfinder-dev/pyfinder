"""Unit tests for EMSC listener message validation and handoff."""

import importlib
import io
import json
import logging
import logging.handlers
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock

from pyfinder.services import seismiclistener


class SeismicListenerProcessingTests(unittest.TestCase):
    def setUp(self):
        self.log_stream = io.StringIO()
        self.logger = logging.Logger(self.id(), level=logging.DEBUG)
        self.logger.propagate = False
        handler = logging.StreamHandler(self.log_stream)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        self.logger.addHandler(handler)
        self.handoff = mock.Mock()

    def make_message(self, action="create", **property_changes):
        properties = {
            "unid": "20260805_0000001",
            "mag": 4.2,
            "time": "2025-05-28T18:00:04.22Z",
            "lastupdate": "2025-05-28T18:01:04.22Z",
            "flynn_region": "Northern Italy",
            "source": "EMSC",
        }
        properties.update(property_changes)
        return json.dumps(
            {"action": action, "data": {"properties": properties}}
        )

    def process(
        self,
        message=None,
        target_regions=None,
        min_magnitude=3.0,
        handoff=None,
    ):
        normalized_regions = seismiclistener.normalize_target_regions(
            target_regions
        )
        seismiclistener.process_emsc_message(
            self.make_message() if message is None else message,
            target_regions=normalized_regions,
            min_magnitude=min_magnitude,
            handoff=self.handoff if handoff is None else handoff,
            logger=self.logger,
        )

    def handed_information(self, call_number=-1):
        return self.handoff.call_args_list[call_number].args[0]

    def test_create_and_update_use_the_same_handoff_once_each(self):
        self.process(self.make_message(action="create"))
        self.process(self.make_message(action="update"))

        self.assertEqual(self.handoff.call_count, 2)
        self.assertEqual(
            [call.args[0]["action"] for call in self.handoff.call_args_list],
            ["create", "update"],
        )
        for call in self.handoff.call_args_list:
            self.assertEqual(
                call.args[1], "2025-05-28T18:00:04.220000"
            )
            self.assertEqual(call.args[2], "2025-05-28T18:01:04")

    def test_accepts_utf8_json_bytes(self):
        self.process(self.make_message().encode("utf-8"))
        self.handoff.assert_called_once()

    def test_region_matching_and_nonmatching(self):
        self.process(target_regions=["Italy"])
        self.handoff.assert_called_once()

        self.handoff.reset_mock()
        self.process(target_regions=["Switzerland"])
        self.handoff.assert_not_called()

    def test_region_worldwide_forms_accept(self):
        configurations = (None, [], "all", "WORLD", "   ")
        for configured_regions in configurations:
            with self.subTest(configured_regions=configured_regions):
                self.handoff.reset_mock()
                self.process(target_regions=configured_regions)
                self.handoff.assert_called_once()

    def test_region_configuration_normalizes_whitespace_and_case(self):
        normalized = seismiclistener.normalize_target_regions(
            ["  nOrThErN ItAlY  "]
        )
        self.assertEqual(normalized, ("northern italy",))
        self.process(target_regions=["  nOrThErN ItAlY  "])
        self.handoff.assert_called_once()

    def test_invalid_region_configuration_is_a_startup_error(self):
        for configured_regions in (5, b"Italy", {"region": "Italy"}, ["Italy", 4]):
            with self.subTest(configured_regions=configured_regions):
                with self.assertRaises(ValueError):
                    seismiclistener.normalize_target_regions(configured_regions)

    def test_numeric_and_numeric_string_magnitudes_are_normalized(self):
        for magnitude in (4, 4.25, "4.5"):
            with self.subTest(magnitude=magnitude):
                self.handoff.reset_mock()
                self.process(self.make_message(mag=magnitude))
                self.handoff.assert_called_once()
                handed_magnitude = self.handed_information()["mag"]
                self.assertIsInstance(handed_magnitude, float)
                self.assertEqual(handed_magnitude, float(magnitude))

    def test_magnitude_equality_at_threshold_is_accepted(self):
        self.process(self.make_message(mag="3.0"), min_magnitude=3.0)
        self.handoff.assert_called_once()

    def test_below_threshold_rejects_create_and_update(self):
        for action in ("create", "update"):
            with self.subTest(action=action):
                self.handoff.reset_mock()
                self.process(
                    self.make_message(action=action, mag=2.99),
                    min_magnitude=3.0,
                )
                self.handoff.assert_not_called()

    def test_explicit_zero_threshold_is_respected(self):
        threshold = seismiclistener.normalize_min_magnitude(0)
        self.assertEqual(threshold, 0.0)
        self.process(self.make_message(mag=0), min_magnitude=threshold)
        self.handoff.assert_called_once()

    def test_minimum_magnitude_default_and_numeric_string(self):
        self.assertEqual(seismiclistener.normalize_min_magnitude(), 3.0)
        self.assertEqual(
            seismiclistener.normalize_min_magnitude("2.75"), 2.75
        )

    def test_invalid_minimum_magnitude_is_a_startup_error(self):
        invalid_values = (True, False, "", "bad", float("nan"), float("inf"), object())
        for configured_magnitude in invalid_values:
            with self.subTest(configured_magnitude=configured_magnitude):
                with self.assertRaises(ValueError):
                    seismiclistener.normalize_min_magnitude(
                        configured_magnitude
                    )

    def test_invalid_alert_magnitudes_are_malformed(self):
        invalid_values = (
            True,
            False,
            "",
            "bad",
            None,
            float("nan"),
            float("inf"),
            float("-inf"),
        )
        for magnitude in invalid_values:
            with self.subTest(magnitude=magnitude):
                self.handoff.reset_mock()
                self.process(self.make_message(mag=magnitude))
                self.handoff.assert_not_called()

    def test_malformed_json_and_invalid_utf8_do_not_handoff(self):
        for message in ("{not json", b"\xff"):
            with self.subTest(message=message):
                self.handoff.reset_mock()
                self.process(message)
                self.handoff.assert_not_called()

    def test_root_data_and_properties_must_be_mappings(self):
        messages = (
            json.dumps(["not", "a", "mapping"]),
            json.dumps({"action": "create", "data": []}),
            json.dumps(
                {"action": "create", "data": {"properties": []}}
            ),
        )
        for message in messages:
            with self.subTest(message=message):
                self.handoff.reset_mock()
                self.process(message)
                self.handoff.assert_not_called()

    def test_each_required_member_must_be_present(self):
        envelope = json.loads(self.make_message())
        cases = ["action"] + list(seismiclistener._REQUIRED_PROPERTIES)
        for missing_member in cases:
            with self.subTest(missing_member=missing_member):
                candidate = json.loads(json.dumps(envelope))
                if missing_member == "action":
                    del candidate["action"]
                else:
                    del candidate["data"]["properties"][missing_member]
                self.handoff.reset_mock()
                self.process(json.dumps(candidate))
                self.handoff.assert_not_called()

    def test_empty_or_invalid_event_identifiers_are_malformed(self):
        for event_id in ("", "   ", 123, None):
            with self.subTest(event_id=event_id):
                self.handoff.reset_mock()
                self.process(self.make_message(unid=event_id))
                self.handoff.assert_not_called()

    def test_event_identifier_is_trimmed_for_handoff(self):
        self.process(self.make_message(unid="  event-1  "))
        self.assertEqual(self.handed_information()["unid"], "event-1")

    def test_flynn_region_must_be_a_string(self):
        for region in (None, 42, ["Italy"]):
            with self.subTest(region=region):
                self.handoff.reset_mock()
                self.process(self.make_message(flynn_region=region))
                self.handoff.assert_not_called()

    def test_only_exact_lowercase_actions_are_supported(self):
        for action in (
            "Create",
            "UPDATE",
            "delete",
            " create ",
            None,
            [],
            {},
        ):
            with self.subTest(action=action):
                self.handoff.reset_mock()
                self.process(self.make_message(action=action))
                self.handoff.assert_not_called()

        self.process(self.make_message(action="create"))
        self.handoff.assert_called_once()

    def test_additional_properties_are_preserved(self):
        self.process(
            self.make_message(
                depth=12.4,
                agency="EMSC",
                nested={"quality": "reviewed"},
            )
        )
        information = self.handed_information()
        self.assertEqual(information["depth"], 12.4)
        self.assertEqual(information["agency"], "EMSC")
        self.assertEqual(information["nested"], {"quality": "reviewed"})

    def test_decoded_source_mapping_is_not_mutated(self):
        source_properties = {
            "unid": "  event-2  ",
            "mag": "4.3",
            "time": "2025-05-28T18:00:04.22Z",
            "lastupdate": "2025-05-28T18:01:04.22Z",
            "flynn_region": "Northern Italy",
            "action": "stale-property-action",
            "extra": "preserved",
        }
        decoded = {
            "action": "update",
            "data": {"properties": source_properties},
        }
        original = dict(source_properties)

        with mock.patch.object(seismiclistener.json, "loads", return_value=decoded):
            self.process("ignored JSON text")

        self.assertEqual(source_properties, original)
        handed = self.handed_information()
        self.assertIsNot(handed, source_properties)
        self.assertEqual(handed["action"], "update")
        self.assertEqual(handed["unid"], "event-2")
        self.assertEqual(handed["mag"], 4.3)

    def test_current_parser_failure_is_malformed_without_handoff(self):
        with mock.patch.object(
            seismiclistener,
            "parse_normalized_iso8601",
            side_effect=ValueError("current parser rejected timestamp"),
        ) as parser:
            self.process()

        parser.assert_called_once()
        self.handoff.assert_not_called()
        self.assertIn("Malformed EMSC message", self.log_stream.getvalue())

    def test_downstream_exception_is_contained_without_retry(self):
        failing_handoff = mock.Mock(side_effect=RuntimeError("downstream failed"))
        self.process(handoff=failing_handoff)
        failing_handoff.assert_called_once()
        self.assertIn("EMSC handoff failure", self.log_stream.getvalue())

    def test_both_actions_use_the_same_eventtracker_operation_once(self):
        policy = object()
        for action, outcome in (
            ("create", ("no_pending_rows", 0)),
            ("update", ("registered", 8)),
        ):
            with self.subTest(action=action):
                tracker = mock.Mock()
                tracker.apply_emsc_alert.return_value = outcome
                handoff = seismiclistener._make_eventtracker_handoff(
                    tracker, policy, self.logger
                )
                log_start = len(self.log_stream.getvalue())

                self.process(
                    self.make_message(
                        action=action,
                        unid="  event-3  ",
                        depth=12.5,
                        agency="EMSC",
                    ),
                    handoff=handoff,
                )

                tracker.apply_emsc_alert.assert_called_once()
                tracker.batch_register_from_policy.assert_not_called()
                tracker.refresh_metadata_after_emsc_update.assert_not_called()
                arguments = tracker.apply_emsc_alert.call_args.kwargs
                self.assertEqual(arguments["event_id"], "event-3")
                self.assertIs(arguments["policy"], policy)
                self.assertEqual(
                    arguments["origin_time"],
                    "2025-05-28T18:00:04.220000",
                )
                self.assertEqual(
                    arguments["last_update_time"],
                    "2025-05-28T18:01:04",
                )
                snapshot = json.loads(arguments["emsc_alert_json"])
                self.assertEqual(snapshot["action"], action)
                self.assertEqual(snapshot["depth"], 12.5)
                self.assertEqual(snapshot["agency"], "EMSC")
                diagnostic = self.log_stream.getvalue()[log_start:]
                self.assertIn("EMSC {0} alert".format(action), diagnostic)
                self.assertNotIn("New event", diagnostic)
                self.assertNotIn("Starting to register", diagnostic)
                self.assertNotIn("Updated event", diagnostic)

    def test_eventtracker_failure_is_contained_and_later_alert_continues(self):
        tracker = mock.Mock()
        tracker.apply_emsc_alert.side_effect = (
            RuntimeError("persistence failed"),
            ("refreshed", 1),
        )
        handoff = seismiclistener._make_eventtracker_handoff(
            tracker, object(), self.logger
        )

        self.process(self.make_message(action="create"), handoff=handoff)
        self.process(self.make_message(action="update"), handoff=handoff)

        self.assertEqual(tracker.apply_emsc_alert.call_count, 2)
        self.assertEqual(
            [
                json.loads(call.kwargs["emsc_alert_json"])["action"]
                for call in tracker.apply_emsc_alert.call_args_list
            ],
            ["create", "update"],
        )
        self.assertIn("EMSC handoff failure", self.log_stream.getvalue())
        self.assertIn("Accepted EMSC handoff", self.log_stream.getvalue())

    def test_later_messages_continue_after_malformed_rejected_and_failed(self):
        sequenced_handoff = mock.Mock(
            side_effect=(RuntimeError("first accepted message failed"), None)
        )
        self.process("{bad JSON", handoff=sequenced_handoff)
        self.process(
            self.make_message(flynn_region="Switzerland"),
            target_regions=["Italy"],
            handoff=sequenced_handoff,
        )
        self.process(self.make_message(action="create"), handoff=sequenced_handoff)
        self.process(self.make_message(action="update"), handoff=sequenced_handoff)

        self.assertEqual(sequenced_handoff.call_count, 2)
        self.assertEqual(
            sequenced_handoff.call_args_list[-1].args[0]["action"], "update"
        )

    def test_logging_outcomes_are_operator_distinguishable(self):
        self.process()
        self.process("{bad JSON")
        self.process(self.make_message(action="delete"))
        self.process(
            self.make_message(flynn_region="Switzerland"),
            target_regions=["Italy"],
        )
        self.process(
            self.make_message(),
            handoff=mock.Mock(side_effect=RuntimeError("handoff failed")),
        )

        records = self.log_stream.getvalue()
        self.assertIn("Accepted EMSC handoff", records)
        self.assertIn("Malformed EMSC message", records)
        self.assertIn("Unsupported EMSC action", records)
        self.assertIn("EMSC filter rejection", records)
        self.assertIn("EMSC handoff failure", records)

    def test_import_has_no_operational_resource_side_effects(self):
        original_directory = os.getcwd()
        original_thread_count = threading.active_count()
        runtime_modules = (
            "tornado",
            "pyfinder.services.eventtracker",
            "pyfinder.services.querypolicy",
        )
        modules_before_reload = {
            name: sys.modules.get(name) for name in runtime_modules
        }
        try:
            with tempfile.TemporaryDirectory() as temporary_directory:
                os.chdir(temporary_directory)
                with mock.patch.object(
                    logging, "FileHandler", autospec=True
                ) as file_handler, mock.patch.object(
                    logging.handlers, "RotatingFileHandler", autospec=True
                ) as rotating_handler, mock.patch.object(
                    sqlite3, "connect", autospec=True
                ) as database_connect, mock.patch.object(
                    threading.Thread, "start", autospec=True
                ) as thread_start:
                    importlib.reload(seismiclistener)

                file_handler.assert_not_called()
                rotating_handler.assert_not_called()
                database_connect.assert_not_called()
                thread_start.assert_not_called()
                self.assertEqual(list(Path(temporary_directory).iterdir()), [])
                for module_name, previous_module in modules_before_reload.items():
                    self.assertIs(sys.modules.get(module_name), previous_module)
        finally:
            os.chdir(original_directory)
            importlib.reload(seismiclistener)

        self.assertEqual(threading.active_count(), original_thread_count)


if __name__ == "__main__":
    unittest.main()
