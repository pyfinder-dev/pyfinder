"""Unit tests for EventTracker registration and EMSC metadata refresh."""

from datetime import datetime, timedelta, timezone
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
import unittest
from unittest import mock

from pyfinder.services import database
from pyfinder.services import eventtracker


class RRSMPolicyStub:
    """Supply the current RRSM schedule without constructing a real policy."""

    service_name = "RRSM"
    QUERY_SCHEDULE_MINUTES = [0, 5, 15, 60, 180, 360, 1440, 2880]


class EventTrackerMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary_directory.name) / "events.db"
        self.log_stream = io.StringIO()
        self.logger = logging.Logger(self.id(), level=logging.DEBUG)
        self.logger.addHandler(logging.StreamHandler(self.log_stream))
        self.tracker = eventtracker.EventTracker(
            str(self.db_path), logger=self.logger
        )
        self.policy = RRSMPolicyStub()

    def tearDown(self):
        self.tracker.close()
        self.temporary_directory.cleanup()

    def fetch_rows(self, event_id=None, service=None):
        query = "SELECT * FROM event_tracker"
        conditions = []
        parameters = []
        if event_id is not None:
            conditions.append("event_id = ?")
            parameters.append(event_id)
        if service is not None:
            conditions.append("service = ?")
            parameters.append(service)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY event_id, service, current_delay_time"

        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in connection.execute(query, parameters)]
        finally:
            connection.close()

    def seed_row(
        self,
        event_id,
        service,
        delay,
        status,
        next_query_time,
    ):
        self.tracker.register_new_schedule(
            event_id=event_id,
            service=service,
            origin_time="2000-01-01T00:00:00",
            last_update_time="2000-01-01T00:01:00",
            current_delay_time=delay,
            next_delay_time=delay + 1,
            next_query_time=next_query_time,
            emsc_alert_json='{"version": "old"}',
        )
        self.tracker.db_update_event_fields(
            event_id,
            service,
            delay,
            status=status,
            last_query_time="2000-01-01T00:02:00",
            next_query_time=next_query_time,
            next_delay_time=delay + 1,
            retry_count=4,
            expiration_time="2099-01-01T00:00:00",
            priority=7,
            last_error="old error",
            last_data_hash="old hash",
            last_data_snapshot="old downstream snapshot",
            last_modified="2000-01-01T00:03:00",
        )

    def apply(self, event_id, action="create", **metadata):
        alert = {"action": action, "extra": "preserved"}
        values = {
            "origin_time": "2026-08-05T10:00:00.000000",
            "last_update_time": "2026-08-05T10:01:00",
            "emsc_alert_json": json.dumps(alert),
        }
        values.update(metadata)
        return self.tracker.apply_emsc_alert(
            event_id=event_id,
            policy=self.policy,
            **values,
        )

    def test_absent_create_and_update_use_existing_policy_registration(self):
        expected_delays = self.policy.QUERY_SCHEDULE_MINUTES
        expected_next_delays = expected_delays[1:] + [None]
        expected_origin_time = "2026-08-05T10:00:00.000000"
        expected_last_update_time = "2026-08-05T10:01:00"
        registration_time = datetime(
            2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc
        )

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return registration_time

        with mock.patch.object(
            eventtracker, "datetime", FixedDateTime
        ), mock.patch.object(
            self.tracker,
            "batch_register_from_policy",
            wraps=self.tracker.batch_register_from_policy,
        ) as register:
            for action in ("create", "update"):
                with self.subTest(action=action):
                    event_id = "absent-{0}".format(action)
                    result = self.apply(event_id, action=action)
                    rows = self.fetch_rows(event_id, self.policy.service_name)

                    self.assertEqual(
                        result,
                        (
                            eventtracker.EventTracker.RESULT_REGISTERED,
                            len(expected_delays),
                        ),
                    )
                    self.assertEqual(len(rows), len(expected_delays))
                    self.assertEqual(
                        [row["current_delay_time"] for row in rows],
                        expected_delays,
                    )
                    self.assertEqual(
                        [row["next_delay_time"] for row in rows],
                        expected_next_delays,
                    )
                    self.assertTrue(
                        all(row["status"] == database.STATUS_PENDING for row in rows)
                    )
                    self.assertEqual(
                        [row["next_query_time"] for row in rows],
                        [
                            (registration_time + timedelta(minutes=delay))
                            .isoformat(timespec="seconds")
                            for delay in expected_delays
                        ],
                    )
                    expected_expiration = (
                        registration_time + timedelta(days=5)
                    ).isoformat(timespec="seconds")
                    for row in rows:
                        self.assertEqual(row["origin_time"], expected_origin_time)
                        self.assertEqual(
                            row["last_update_time"], expected_last_update_time
                        )
                        self.assertEqual(row["expiration_time"], expected_expiration)
                        self.assertEqual(
                            json.loads(row["emsc_alert_json"])["action"],
                            action,
                        )

        self.assertEqual(register.call_count, 2)

    def test_partial_registration_attempts_all_stages_and_is_not_reconstructed(self):
        event_id = "partial-event"
        failed_delay = 60
        attempted_delays = []
        original_register = self.tracker.register_new_schedule
        registration_time = datetime(
            2026, 8, 5, 13, 0, 0, tzinfo=timezone.utc
        )

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return registration_time

        def register_with_failure(*args, **kwargs):
            delay = kwargs["current_delay_time"]
            attempted_delays.append(delay)
            if delay == failed_delay:
                raise sqlite3.OperationalError("middle delay failed")
            return original_register(*args, **kwargs)

        with mock.patch.object(
            eventtracker, "datetime", FixedDateTime
        ), mock.patch.object(
            self.tracker,
            "register_new_schedule",
            side_effect=register_with_failure,
        ):
            with self.assertRaises(
                eventtracker.ScheduleRegistrationError
            ) as raised:
                self.apply(event_id, action="create")

        self.assertEqual(attempted_delays, self.policy.QUERY_SCHEDULE_MINUTES)
        error = raised.exception
        self.assertEqual(error.successful_rows, 7)
        self.assertEqual(error.failed_delays, (failed_delay,))
        self.assertEqual(len(error.failures), 1)
        self.assertIsInstance(error.failures[0][1], sqlite3.OperationalError)
        self.assertIs(error.__cause__, error.failures[0][1])

        rows = self.fetch_rows(event_id, "RRSM")
        persisted_delays = [row["current_delay_time"] for row in rows]
        self.assertEqual(
            persisted_delays,
            [
                delay
                for delay in self.policy.QUERY_SCHEDULE_MINUTES
                if delay != failed_delay
            ],
        )
        self.assertTrue(
            all(row["status"] == database.STATUS_PENDING for row in rows)
        )
        self.assertNotIn(failed_delay, persisted_delays)
        self.assertIn(180, persisted_delays)

        diagnostic = self.log_stream.getvalue()
        self.assertIn("Schedule registration incomplete", diagnostic)
        self.assertIn(event_id, diagnostic)
        self.assertIn("RRSM", diagnostic)
        self.assertIn("7 rows persisted", diagnostic)
        self.assertIn("[60]", diagnostic)
        self.assertNotIn("Registered event partial-event", diagnostic)

        schedule_before_refresh = {
            row["current_delay_time"]: (
                row["next_query_time"],
                row["next_delay_time"],
                row["expiration_time"],
                row["status"],
            )
            for row in rows
        }
        new_origin = "2026-08-05T14:00:00.000000"
        new_last_update = "2026-08-05T14:01:00"
        new_snapshot = json.dumps({"action": "update", "version": "new"})
        with mock.patch.object(
            self.tracker,
            "batch_register_from_policy",
            wraps=self.tracker.batch_register_from_policy,
        ) as register:
            result = self.apply(
                event_id,
                action="update",
                origin_time=new_origin,
                last_update_time=new_last_update,
                emsc_alert_json=new_snapshot,
            )

        register.assert_not_called()
        self.assertEqual(result, (eventtracker.EventTracker.RESULT_REFRESHED, 7))
        refreshed_rows = self.fetch_rows(event_id, "RRSM")
        self.assertEqual(len(refreshed_rows), 7)
        self.assertNotIn(
            failed_delay,
            [row["current_delay_time"] for row in refreshed_rows],
        )
        for row in refreshed_rows:
            self.assertEqual(row["origin_time"], new_origin)
            self.assertEqual(row["last_update_time"], new_last_update)
            self.assertEqual(row["emsc_alert_json"], new_snapshot)
            self.assertEqual(
                (
                    row["next_query_time"],
                    row["next_delay_time"],
                    row["expiration_time"],
                    row["status"],
                ),
                schedule_before_refresh[row["current_delay_time"]],
            )

    def test_total_registration_failure_attempts_every_stage_and_raises(self):
        event_id = "total-failure-event"
        attempted_delays = []

        def fail_every_insert(*args, **kwargs):
            delay = kwargs["current_delay_time"]
            attempted_delays.append(delay)
            raise sqlite3.OperationalError(
                "insert failed for delay {0}".format(delay)
            )

        with mock.patch.object(
            self.tracker,
            "register_new_schedule",
            side_effect=fail_every_insert,
        ):
            with self.assertRaises(
                eventtracker.ScheduleRegistrationError
            ) as raised:
                self.apply(event_id)

        self.assertEqual(attempted_delays, self.policy.QUERY_SCHEDULE_MINUTES)
        error = raised.exception
        self.assertEqual(error.successful_rows, 0)
        self.assertEqual(
            error.failed_delays,
            tuple(self.policy.QUERY_SCHEDULE_MINUTES),
        )
        self.assertEqual(
            len(error.failures),
            len(self.policy.QUERY_SCHEDULE_MINUTES),
        )
        self.assertIs(error.__cause__, error.failures[0][1])
        self.assertEqual(self.fetch_rows(event_id, "RRSM"), [])

        diagnostic = self.log_stream.getvalue()
        self.assertIn("Schedule registration incomplete", diagnostic)
        self.assertIn(event_id, diagnostic)
        self.assertIn("RRSM", diagnostic)
        self.assertIn("0 rows persisted", diagnostic)
        self.assertIn(str(self.policy.QUERY_SCHEDULE_MINUTES), diagnostic)
        self.assertNotIn("Registered event total-failure-event", diagnostic)

    def test_repeated_delivery_does_not_duplicate_scheduled_rows(self):
        event_id = "repeated-event"
        first = self.apply(event_id, action="create")
        second = self.apply(event_id, action="update")
        third = self.apply(event_id, action="update")

        self.assertEqual(first[0], eventtracker.EventTracker.RESULT_REGISTERED)
        self.assertEqual(
            second,
            (eventtracker.EventTracker.RESULT_REFRESHED, 8),
        )
        self.assertEqual(
            third,
            (eventtracker.EventTracker.RESULT_REFRESHED, 8),
        )
        self.assertEqual(len(self.fetch_rows(event_id, "RRSM")), 8)

    def test_refresh_updates_all_and_only_matching_pending_rows(self):
        target_event = "target-event"
        now = datetime.now(timezone.utc)
        future = (now + timedelta(days=1)).isoformat(timespec="seconds")
        due = now.isoformat(timespec="seconds")
        overdue = (now - timedelta(days=1)).isoformat(timespec="seconds")

        self.seed_row(target_event, "RRSM", 0, database.STATUS_PENDING, future)
        self.seed_row(target_event, "RRSM", 5, database.STATUS_PENDING, due)
        self.seed_row(target_event, "RRSM", 15, database.STATUS_PENDING, overdue)
        self.seed_row(
            target_event, "RRSM", 60, database.STATUS_PROCESSING, overdue
        )
        self.seed_row(
            target_event, "RRSM", 180, database.STATUS_COMPLETED, overdue
        )
        self.seed_row(
            target_event, "RRSM", 360, database.STATUS_INCOMPLETE, overdue
        )
        self.seed_row(
            target_event, "OTHER", 0, database.STATUS_PENDING, overdue
        )
        self.seed_row(
            "other-event", "RRSM", 0, database.STATUS_PENDING, overdue
        )

        before = {
            (row["event_id"], row["service"], row["current_delay_time"]): row
            for row in self.fetch_rows()
        }
        new_origin = "2026-08-05T11:00:00.000000"
        new_last_update = "2026-08-05T11:01:00"
        new_snapshot = json.dumps(
            {"action": "update", "flynn_region": "Northern Italy"}
        )
        result = self.apply(
            target_event,
            action="update",
            origin_time=new_origin,
            last_update_time=new_last_update,
            emsc_alert_json=new_snapshot,
        )
        after = {
            (row["event_id"], row["service"], row["current_delay_time"]): row
            for row in self.fetch_rows()
        }

        self.assertEqual(
            result,
            (eventtracker.EventTracker.RESULT_REFRESHED, 3),
        )
        protected_fields = (
            "event_id",
            "service",
            "status",
            "last_query_time",
            "next_query_time",
            "current_delay_time",
            "next_delay_time",
            "retry_count",
            "expiration_time",
            "priority",
            "last_error",
            "last_data_hash",
            "last_data_snapshot",
        )
        refreshed_keys = {
            (target_event, "RRSM", 0.0),
            (target_event, "RRSM", 5.0),
            (target_event, "RRSM", 15.0),
        }
        for key, previous in before.items():
            current = after[key]
            if key in refreshed_keys:
                self.assertEqual(current["origin_time"], new_origin)
                self.assertEqual(current["last_update_time"], new_last_update)
                self.assertEqual(current["emsc_alert_json"], new_snapshot)
                self.assertNotEqual(
                    current["last_modified"], previous["last_modified"]
                )
                for field in protected_fields:
                    self.assertEqual(current[field], previous[field], field)
            else:
                self.assertEqual(current, previous)

    def test_existing_event_without_pending_rows_is_not_reopened(self):
        event_id = "completed-event"
        self.seed_row(
            event_id,
            "RRSM",
            0,
            database.STATUS_COMPLETED,
            "2000-01-01T00:00:00",
        )
        before = self.fetch_rows(event_id, "RRSM")

        with mock.patch.object(
            self.tracker,
            "batch_register_from_policy",
            wraps=self.tracker.batch_register_from_policy,
        ) as register:
            result = self.apply(event_id, action="update")

        register.assert_not_called()
        self.assertEqual(
            result,
            (eventtracker.EventTracker.RESULT_NO_PENDING, 0),
        )
        self.assertEqual(self.fetch_rows(event_id, "RRSM"), before)

    def test_database_failures_propagate(self):
        with mock.patch.object(
            self.tracker._db,
            "event_service_exists",
            side_effect=sqlite3.OperationalError("existence failure"),
        ):
            with self.assertRaisesRegex(
                sqlite3.OperationalError, "existence failure"
            ):
                self.apply("failing-event")

        self.seed_row(
            "existing-event",
            "RRSM",
            0,
            database.STATUS_PENDING,
            "2000-01-01T00:00:00",
        )
        with mock.patch.object(
            self.tracker._db,
            "update_pending_emsc_metadata",
            side_effect=sqlite3.OperationalError("update failure"),
        ):
            with self.assertRaisesRegex(sqlite3.OperationalError, "update failure"):
                self.apply("existing-event")


class EventTrackerImportSafetyTests(unittest.TestCase):
    def test_import_does_not_construct_database_logger_or_policy(self):
        original_directory = os.getcwd()
        policy_module_before = sys.modules.get("pyfinder.services.querypolicy")
        try:
            with tempfile.TemporaryDirectory() as temporary_directory:
                os.chdir(temporary_directory)
                with mock.patch.object(
                    database.sqlite3, "connect", autospec=True
                ) as connect, mock.patch.object(
                    logging, "FileHandler", autospec=True
                ) as file_handler, mock.patch.object(
                    logging.handlers, "RotatingFileHandler", autospec=True
                ) as rotating_handler:
                    importlib.reload(database)
                    importlib.reload(eventtracker)

                connect.assert_not_called()
                file_handler.assert_not_called()
                rotating_handler.assert_not_called()
                self.assertIs(
                    sys.modules.get("pyfinder.services.querypolicy"),
                    policy_module_before,
                )
                self.assertEqual(list(Path(temporary_directory).iterdir()), [])
        finally:
            os.chdir(original_directory)
            importlib.reload(database)
            importlib.reload(eventtracker)


if __name__ == "__main__":
    unittest.main()
