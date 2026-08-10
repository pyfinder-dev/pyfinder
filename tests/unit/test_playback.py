"""Offline tests for deterministic EMSC alert playback."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import inspect
import json
import tempfile
import unittest
import urllib.request
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

    def test_builtin_events_have_exact_verified_static_origins(self):
        expected_origins = {
            "20161030_0000029": "2016-10-30T06:40:18.3Z",
            "20230206_0000008": "2023-02-06T01:17:36.1Z",
            "20230206_0000222": "2023-02-06T10:24:49.6Z",
            "20250522_0000028": "2025-05-22T03:19:34.6Z",
            "20250423_0000104": "2025-04-23T09:49:11.93Z",
            "20250520_0000201": "2025-05-20T20:36:52.26Z",
            "20250922_0000172": "2025-09-22T09:02:44.04Z",
        }

        definitions = playback.generate_event_list()

        self.assertEqual(
            {event["unid"]: event["time"] for event in definitions},
            expected_origins,
        )

    def test_replay_clock_changes_lastupdate_without_changing_origins(self):
        first_clock = datetime(2030, 1, 2, 3, 4, 5, 6000,
                               tzinfo=timezone.utc)
        second_clock = datetime(2040, 6, 7, 8, 9, 10, 11000,
                                tzinfo=timezone.utc)

        with mock.patch("pyfinder.playback.datetime") as controlled_datetime:
            controlled_datetime.now.return_value = first_clock
            first = playback.generate_event_list()
            controlled_datetime.now.return_value = second_clock
            second = playback.generate_event_list()

        self.assertEqual(
            [event["time"] for event in first],
            [event["time"] for event in second],
        )
        self.assertEqual(
            {event["lastupdate"] for event in first},
            {"2030-01-02T03:04:05.006000Z"},
        )
        self.assertEqual(
            {event["lastupdate"] for event in second},
            {"2040-06-07T08:09:10.011000Z"},
        )

    def test_complete_builtins_validate_without_mutating_definitions(self):
        definitions = playback.generate_event_list()
        original = deepcopy(definitions)
        logger = mock.Mock()

        manager = EventAlertWSPlaybackManager(
            definitions,
            mock.Mock(),
            logger=logger,
        )

        self.assertEqual(len(manager.event_list), 7)
        self.assertEqual(definitions, original)
        logger.warning.assert_not_called()

    def test_builtin_generation_performs_no_runtime_metadata_query(self):
        source = inspect.getsource(playback.generate_event_list)
        with mock.patch.object(
            urllib.request,
            "urlopen",
            side_effect=AssertionError("playback attempted a live lookup"),
        ) as urlopen:
            definitions = playback.generate_event_list()

        self.assertEqual(len(definitions), 7)
        urlopen.assert_not_called()
        self.assertNotIn("EMSCFeltReportClient", source)
        self.assertNotIn("query(", source)


if __name__ == "__main__":
    unittest.main()
