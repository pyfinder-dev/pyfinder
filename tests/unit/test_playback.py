"""Offline tests for deterministic EMSC alert playback."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import inspect
import json
import tempfile
import unittest
from unittest import mock

from pyfinder import playback
from pyfinder.playback import EventAlertWSPlaybackManager
from pyfinder.services.eventtracker import EventTracker


def playback_event(event_id="playback-event", **changes):
    """Return one complete predefined alert accepted by current time parsing."""
    event = {
        "unid": event_id,
        "lat": 46.25,
        "lon": 7.75,
        "mag": 5.6,
        "depth": 12.5,
        "time": "2020-01-02T03:04:05.250000Z",
        "lastupdate": "2020-01-02T03:05:06.500000Z",
        "magtype": "Mw",
        "flynn_region": "TEST REGION",
        "action": "create",
        "provider-extra": {"preserve": True},
    }
    event.update(changes)
    return event


class PlaybackMetadataTests(unittest.TestCase):
    def test_canonical_package_import_exposes_playback_module(self):
        self.assertEqual(playback.__name__, "pyfinder.playback")

    def test_injection_preserves_all_values_without_mutating_caller_mapping(self):
        tracker = mock.Mock()
        logger = mock.Mock()
        event = playback_event()
        original = deepcopy(event)
        manager = EventAlertWSPlaybackManager(
            [event],
            tracker,
            logger=logger,
        )

        result = manager._inject_event(manager.event_list[0])

        self.assertTrue(result)
        self.assertEqual(event, original)
        self.assertIsNot(manager.event_list[0], event)
        registration = tracker.batch_register_from_policy.call_args.kwargs
        self.assertEqual(registration["event_id"], original["unid"])
        self.assertEqual(registration["origin_time"], original["time"])
        self.assertEqual(
            registration["last_update_time"],
            original["lastupdate"],
        )
        self.assertEqual(
            json.loads(registration["emsc_alert_json"]),
            original,
        )
        self.assertEqual(logger.warning.call_count, 0)

    def test_invalid_event_is_warned_and_ignored_while_valid_event_continues(self):
        logger = mock.Mock()
        invalid = playback_event("invalid", depth=-1)
        valid = playback_event("valid")

        manager = EventAlertWSPlaybackManager(
            [invalid, valid],
            mock.Mock(),
            logger=logger,
        )

        self.assertEqual(
            [event["unid"] for event in manager.event_list],
            ["valid"],
        )
        logger.warning.assert_called_once()
        self.assertIn("invalid", str(logger.warning.call_args))

    def test_direct_invalid_injection_does_not_register_or_repair_event(self):
        tracker = mock.Mock()
        logger = mock.Mock()
        manager = EventAlertWSPlaybackManager([], tracker, logger=logger)
        invalid = playback_event()
        invalid.pop("lastupdate")
        original = deepcopy(invalid)

        result = manager._inject_event(invalid)

        self.assertFalse(result)
        self.assertEqual(invalid, original)
        tracker.batch_register_from_policy.assert_not_called()
        logger.warning.assert_called_once()

    def test_registration_schedule_is_anchored_to_injection_time(self):
        temporary_directory = tempfile.TemporaryDirectory()
        tracker = EventTracker(
            temporary_directory.name + "/playback.db",
            logger=mock.Mock(),
        )
        registration_time = datetime(
            2030,
            4,
            5,
            6,
            7,
            8,
            tzinfo=timezone.utc,
        )
        historical_event = playback_event()
        try:
            manager = EventAlertWSPlaybackManager(
                [historical_event],
                tracker,
                logger=mock.Mock(),
            )
            with mock.patch(
                "pyfinder.services.eventtracker.datetime"
            ) as controlled_datetime:
                controlled_datetime.now.return_value = registration_time
                manager._inject_event(manager.event_list[0])

            tracker._db.cursor.execute(
                """
                SELECT current_delay_time, next_query_time, emsc_alert_json
                FROM event_tracker
                ORDER BY current_delay_time
                """
            )
            rows = tracker._db.cursor.fetchall()
        finally:
            tracker.close()
            temporary_directory.cleanup()

        self.assertTrue(rows)
        for delay, next_query_time, snapshot in rows:
            self.assertEqual(
                next_query_time,
                (
                    registration_time + timedelta(minutes=delay)
                ).isoformat(timespec="seconds"),
            )
            stored_event = json.loads(snapshot)
            self.assertEqual(stored_event["time"], historical_event["time"])
            self.assertEqual(
                stored_event["lastupdate"],
                historical_event["lastupdate"],
            )

    def test_builtin_event_generation_uses_no_current_clock_or_fabricated_data(self):
        definitions = playback.generate_event_list()

        self.assertEqual(
            [event["unid"] for event in definitions],
            [
                "20161030_0000029",
                "20230206_0000008",
                "20230206_0000222",
                "20250522_0000028",
                "20250423_0000104",
                "20250520_0000201",
                "20250922_0000172",
            ],
        )
        for event in definitions:
            self.assertNotIn("time", event)
            self.assertNotIn("lastupdate", event)
        source = inspect.getsource(playback.generate_event_list)
        self.assertNotIn("datetime.now", source)
        self.assertNotIn("timedelta", source)

    def test_incomplete_builtins_warn_by_id_without_mutation(self):
        definitions = playback.generate_event_list()
        original = deepcopy(definitions)
        logger = mock.Mock()

        manager = EventAlertWSPlaybackManager(
            definitions,
            mock.Mock(),
            logger=logger,
        )

        self.assertEqual(manager.event_list, [])
        self.assertEqual(definitions, original)
        self.assertEqual(logger.warning.call_count, len(definitions))
        warnings = [str(call) for call in logger.warning.call_args_list]
        for event in definitions:
            self.assertTrue(
                any(event["unid"] in warning for warning in warnings),
                warnings,
            )

    def test_completed_copy_of_builtin_proceeds_while_others_remain_rejected(self):
        definitions = playback.generate_event_list()
        original = deepcopy(definitions)
        completed = deepcopy(definitions[0])
        completed["time"] = "2016-10-30T06:40:18.000000Z"
        completed["lastupdate"] = "2016-10-30T06:41:00.000000Z"
        tracker = mock.Mock()
        logger = mock.Mock()

        manager = EventAlertWSPlaybackManager(
            [completed, *definitions[1:]],
            tracker,
            logger=logger,
        )

        self.assertEqual(
            [event["unid"] for event in manager.event_list],
            [completed["unid"]],
        )
        self.assertEqual(logger.warning.call_count, len(definitions) - 1)
        self.assertTrue(manager._inject_event(manager.event_list[0]))
        tracker.batch_register_from_policy.assert_called_once()
        self.assertEqual(definitions, original)
        self.assertNotIn("time", definitions[0])
        self.assertNotIn("lastupdate", definitions[0])


if __name__ == "__main__":
    unittest.main()
