"""Offline tests for the authoritative earthquake EventContext boundary."""

import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from pyfinder.eventcontext import (
    EventContext,
    EventContextError,
    ProviderModelAccessError,
)
from pyfinder.services.eventtracker import EventTracker
from pyfinder.utils import dataformatter
from pyfinder.utils import timeutils


EVENT_ID = "context-event"


def alert_mapping(**changes):
    """Return one complete EMSC-style alert with controllable values."""
    alert = {
        "unid": EVENT_ID,
        "lat": "46.25",
        "lon": "7.75",
        "mag": "5.6",
        "depth": "12.5",
        "time": "2026-08-10T08:15:30.250000Z",
        "magtype": "Mw",
        "flynn_region": "TEST REGION",
        "action": "create",
        "provider-extra": {"version": 1},
    }
    alert.update(changes)
    return alert


class ProviderEventModel:
    """Expose the public getter variants used by ParamWS event models."""

    def get_event_unid(self):
        return EVENT_ID

    def get_latitude(self):
        return "46.25"

    def get_longitude(self):
        return "7.75"

    def get_magnitude(self):
        return "5.6"

    def get_depth(self):
        return "12.5"

    def get_event_time(self):
        return "2026-08-10T08:15:30.250000Z"


class EventContextValidationTests(unittest.TestCase):
    def test_module_import_does_not_load_operational_dependencies(self):
        script = r"""
import sys

blocked_prefixes = (
    "paramws",
    "pyfinder.findermanager",
    "pyfinder.services.scheduler",
    "pyfinder.utils.station_merger",
)


class Blocker:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(blocked_prefixes):
            raise AssertionError("operational dependency imported: " + fullname)
        return None


sys.meta_path.insert(0, Blocker())
from pyfinder.eventcontext import EventContext
print(EventContext.__name__)
"""

        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(self.repository_root()),
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "EventContext")

    @staticmethod
    def repository_root():
        return Path(__file__).resolve().parents[2]

    def test_complete_alert_is_copied_into_existing_getter_interface(self):
        alert = alert_mapping()

        context = EventContext.from_alert_mapping(
            alert,
            scheduled_event_id=EVENT_ID,
        )
        alert.update(
            unid="changed",
            lat=0,
            lon=0,
            mag=0,
            depth=0,
            time="2000-01-01T00:00:00Z",
            magtype="ML",
        )

        self.assertEqual(context.get_event_id(), EVENT_ID)
        self.assertEqual(context.get_event_unid(), EVENT_ID)
        self.assertEqual(context.get_latitude(), 46.25)
        self.assertEqual(context.get_longitude(), 7.75)
        self.assertEqual(context.get_magnitude(), 5.6)
        self.assertEqual(context.get_depth(), 12.5)
        self.assertEqual(
            context.get_origin_time(),
            "2026-08-10T08:15:30.250000Z",
        )
        self.assertEqual(context.get_event_time(), context.get_origin_time())
        self.assertEqual(context.get_magnitude_type(), "Mw")

    def test_absent_magnitude_type_defaults_to_empty_string(self):
        alert = alert_mapping()
        alert.pop("magtype")

        context = EventContext.from_alert_mapping(
            alert,
            scheduled_event_id=EVENT_ID,
        )

        self.assertEqual(context.get_magnitude_type(), "")

    def test_provider_model_is_copied_through_the_same_validation_policy(self):
        context = EventContext.from_provider_model(
            ProviderEventModel(),
            requested_event_id=EVENT_ID,
        )

        self.assertEqual(context.get_event_id(), EVENT_ID)
        self.assertEqual(context.get_latitude(), 46.25)
        self.assertEqual(context.get_longitude(), 7.75)
        self.assertEqual(context.get_magnitude(), 5.6)
        self.assertEqual(context.get_depth(), 12.5)
        self.assertEqual(
            context.get_origin_time(),
            "2026-08-10T08:15:30.250000Z",
        )
        self.assertEqual(context.get_magnitude_type(), "")

    def test_unusable_provider_candidate_is_rejected_without_provider_imports(self):
        candidate = ProviderEventModel()
        candidate.get_event_unid = mock.Mock(return_value="another-event")

        with self.assertRaises(EventContextError):
            EventContext.from_provider_model(
                candidate,
                requested_event_id=EVENT_ID,
            )

    def test_provider_accessor_failure_has_explicit_dependency_boundary(self):
        candidate = ProviderEventModel()
        dependency_error = TypeError("malformed dependency accessor")
        candidate.get_latitude = mock.Mock(side_effect=dependency_error)

        with self.assertRaises(ProviderModelAccessError) as raised:
            EventContext.from_provider_model(
                candidate,
                requested_event_id=EVENT_ID,
            )

        self.assertIs(raised.exception.__cause__, dependency_error)

    def test_every_required_invalid_category_is_rejected(self):
        invalid_cases = {
            "non-mapping": None,
            "empty event ID": alert_mapping(unid="  "),
            "identifier mismatch": alert_mapping(unid="another-event"),
            "missing latitude": alert_mapping(lat=None),
            "boolean latitude": alert_mapping(lat=True),
            "nonnumeric longitude": alert_mapping(lon="east"),
            "nonfinite magnitude": alert_mapping(mag=math.inf),
            "nonfinite depth": alert_mapping(depth=math.nan),
            "latitude below range": alert_mapping(lat=-90.1),
            "latitude above range": alert_mapping(lat=90.1),
            "longitude below range": alert_mapping(lon=-180.1),
            "longitude above range": alert_mapping(lon=180.1),
            "negative depth": alert_mapping(depth=-0.1),
            "missing origin time": alert_mapping(time=None),
            "invalid origin time": alert_mapping(time="not-a-time"),
        }

        for category, alert in invalid_cases.items():
            with self.subTest(category=category):
                with self.assertRaises(EventContextError):
                    EventContext.from_alert_mapping(
                        alert,
                        scheduled_event_id=EVENT_ID,
                    )

    def test_current_timestamp_conversion_behavior_is_unchanged(self):
        self.assertIs(dataformatter.get_epoch_time, timeutils.get_epoch_time)
        accepted = (
            "2026-08-10T08:15:30.250000Z",
            "2026-08-10T08:15:30.250000",
            "2026-08-10T08:15:30Z",
            "2026-08-10 08:15:30",
        )
        for value in accepted:
            with self.subTest(value=value):
                self.assertIsNotNone(timeutils.get_epoch_time(value))
        self.assertIsNone(
            timeutils.get_epoch_time("2026-08-10T08:15:30+00:00")
        )


class PersistedEventContextTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.tracker = EventTracker(
            self.temporary_directory.name + "/events.db",
            logger=mock.Mock(),
        )

    def tearDown(self):
        self.tracker.close()
        self.temporary_directory.cleanup()

    def test_pending_refresh_changes_context_without_rescheduling(self):
        original_alert = alert_mapping()
        next_query_time = "2026-08-10T09:00:00+00:00"
        self.tracker.register_new_schedule(
            event_id=EVENT_ID,
            service="RRSM",
            origin_time="separately-normalized-origin",
            last_update_time="2026-08-10T08:16:00+00:00",
            current_delay_time=5,
            next_delay_time=15,
            next_query_time=next_query_time,
            emsc_alert_json=json.dumps(original_alert),
        )

        original_meta = self.tracker.get_event_meta(EVENT_ID, "RRSM", 5)
        original_context = original_meta[EventTracker.Field.event_context]
        self.assertEqual(original_context.get_latitude(), 46.25)
        self.assertEqual(
            original_context.get_origin_time(),
            original_alert["time"],
        )
        self.assertNotEqual(
            original_context.get_origin_time(),
            original_meta[EventTracker.Field.origin_time],
        )
        self.assertEqual(
            json.loads(original_meta[EventTracker.Field.emsc_alert_json]),
            original_alert,
        )

        refreshed_alert = alert_mapping(
            lat=47.1,
            lon=8.2,
            mag=5.9,
            depth=8.0,
            time="2026-08-10T08:15:31.500000Z",
            magtype="ML",
            **{"provider-extra": {"version": 2}},
        )
        refreshed_rows = self.tracker.refresh_metadata_after_emsc_update(
            event_id=EVENT_ID,
            service="RRSM",
            new_last_update_time="2026-08-10T08:17:00+00:00",
            origin_time="different-normalized-origin",
            emsc_alert_json=json.dumps(refreshed_alert),
        )

        refreshed_meta = self.tracker.get_event_meta(EVENT_ID, "RRSM", 5)
        refreshed_context = refreshed_meta[EventTracker.Field.event_context]
        self.assertEqual(refreshed_rows, 1)
        self.assertEqual(refreshed_context.get_latitude(), 47.1)
        self.assertEqual(refreshed_context.get_longitude(), 8.2)
        self.assertEqual(refreshed_context.get_magnitude(), 5.9)
        self.assertEqual(refreshed_context.get_depth(), 8.0)
        self.assertEqual(
            refreshed_context.get_origin_time(),
            refreshed_alert["time"],
        )
        self.assertEqual(refreshed_context.get_magnitude_type(), "ML")
        self.assertEqual(
            refreshed_meta[EventTracker.Field.next_query_time],
            next_query_time,
        )
        self.assertEqual(
            json.loads(refreshed_meta[EventTracker.Field.emsc_alert_json]),
            refreshed_alert,
        )

    def test_unusable_snapshots_preserve_diagnostic_without_partial_context(self):
        snapshots = {
            "malformed JSON": "{",
            "non-mapping JSON": json.dumps([1, 2, 3]),
            "incomplete mapping": json.dumps({"unid": EVENT_ID}),
        }
        for index, (category, snapshot) in enumerate(snapshots.items()):
            with self.subTest(category=category):
                delay = index + 1
                self.tracker.register_new_schedule(
                    event_id=EVENT_ID,
                    service="RRSM",
                    origin_time="stored-origin",
                    last_update_time="stored-update",
                    current_delay_time=delay,
                    next_delay_time=None,
                    next_query_time="2026-08-10T09:00:00+00:00",
                    emsc_alert_json=snapshot,
                )

                meta = self.tracker.get_event_meta(EVENT_ID, "RRSM", delay)

                self.assertIsNone(meta[EventTracker.Field.event_context])
                self.assertTrue(meta[EventTracker.Field.event_context_error])
                self.assertEqual(
                    meta[EventTracker.Field.emsc_alert_json],
                    snapshot,
                )


if __name__ == "__main__":
    unittest.main()
