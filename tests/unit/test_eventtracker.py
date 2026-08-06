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
        expiration_time="2099-01-01T00:00:00",
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
        # Fixture-only schema setup must not define which fields the
        # transitional production mutation API is allowed to change.
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                UPDATE event_tracker
                SET status = ?,
                    last_query_time = ?,
                    next_query_time = ?,
                    next_delay_time = ?,
                    retry_count = ?,
                    expiration_time = ?,
                    priority = ?,
                    last_error = ?,
                    last_data_hash = ?,
                    last_data_snapshot = ?,
                    last_modified = ?
                WHERE event_id = ? AND service = ? AND current_delay_time = ?
                """,
                (
                    status,
                    "2000-01-01T00:02:00",
                    next_query_time,
                    delay + 1,
                    4,
                    expiration_time,
                    7,
                    "old error",
                    "old hash",
                    "old downstream snapshot",
                    "2000-01-01T00:03:00",
                    event_id,
                    service,
                    delay,
                ),
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
                    for row in rows:
                        self.assertEqual(row["origin_time"], expected_origin_time)
                        self.assertEqual(
                            row["last_update_time"], expected_last_update_time
                        )
                        self.assertIsNone(row["expiration_time"])
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

    def test_scheduler_metadata_always_contains_derived_region_on_a_copy(self):
        cases = (
            ("valid", '{"flynn_region": "Northern Italy"}', "Northern Italy"),
            ("absent", None, None),
            ("empty", "", None),
            ("missing member", '{"action": "create"}', None),
            ("malformed", "{not-json", None),
            ("explicit null", '{"flynn_region": null}', None),
        )
        for label, snapshot, expected_region in cases:
            with self.subTest(label=label):
                stored_meta = {
                    "event_id": "event-1",
                    "emsc_alert_json": snapshot,
                }
                with mock.patch.object(
                    self.tracker._db,
                    "get_event_meta",
                    return_value=stored_meta,
                ):
                    result = self.tracker.get_event_meta(
                        event_id="event-1",
                        service="RRSM",
                        current_delay_time=0,
                    )

                self.assertIsInstance(result, dict)
                self.assertIn(eventtracker.EventTracker.Field.region, result)
                self.assertEqual(
                    result[eventtracker.EventTracker.Field.region],
                    expected_region,
                )
                self.assertIsNot(result, stored_meta)
                self.assertNotIn(
                    eventtracker.EventTracker.Field.region,
                    stored_meta,
                )

    def test_removed_registration_passthrough_arguments_fail_loudly(self):
        common_registration = {
            "event_id": "override-event",
            "service": "RRSM",
            "origin_time": "2026-08-06T10:00:00+00:00",
            "last_update_time": "2026-08-06T10:01:00+00:00",
            "current_delay_time": 0,
            "next_delay_time": 5,
            "next_query_time": "2026-08-06T10:00:00+00:00",
        }
        with self.assertRaises(TypeError):
            self.tracker.register_new_schedule(
                **common_registration,
                status=database.STATUS_COMPLETED,
            )

        with self.assertRaises(TypeError):
            self.tracker.batch_register_from_policy(
                event_id="batch-override-event",
                policy=self.policy,
                origin_time="2026-08-06T10:00:00+00:00",
                last_update_time="2026-08-06T10:01:00+00:00",
                priority=99,
            )
        with self.assertRaises(TypeError):
            self.tracker.register_new_schedule(
                **common_registration,
                expiration_days=5,
            )
        with self.assertRaises(TypeError):
            self.tracker.batch_register_from_policy(
                event_id="batch-expiration-event",
                policy=self.policy,
                origin_time="2026-08-06T10:00:00+00:00",
                last_update_time="2026-08-06T10:01:00+00:00",
                expiration_days=5,
            )
        with self.assertRaises(TypeError):
            self.tracker.apply_emsc_alert(
                event_id="alert-expiration-event",
                policy=self.policy,
                origin_time="2026-08-06T10:00:00+00:00",
                last_update_time="2026-08-06T10:01:00+00:00",
                expiration_days=5,
            )

        self.assertEqual(self.fetch_rows(), [])

    def test_named_schedule_registration_persists_pending_initial_state(self):
        self.tracker.register_new_schedule(
            event_id="named-event",
            service="RRSM",
            origin_time="2026-08-06T10:00:00+00:00",
            last_update_time="2026-08-06T10:01:00+00:00",
            current_delay_time=0,
            next_delay_time=5,
            next_query_time="2026-08-06T10:00:00+00:00",
            emsc_alert_json='{"action": "create"}',
        )

        rows = self.fetch_rows("named-event", "RRSM")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], database.STATUS_PENDING)
        self.assertEqual(rows[0]["current_delay_time"], 0)
        self.assertIsNone(rows[0]["expiration_time"])
        self.assertFalse(
            hasattr(eventtracker.EventTracker.Field, "expiration_time")
        )

    def test_obsolete_all_pending_wrapper_is_absent(self):
        self.assertFalse(
            hasattr(eventtracker.EventTracker, "get_all_pending_events")
        )

    def test_due_discovery_keeps_expired_pending_catch_up_only(self):
        now = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)
        overdue = (now - timedelta(days=1)).isoformat(timespec="seconds")
        future = (now + timedelta(days=1)).isoformat(timespec="seconds")

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return now

        self.seed_row(
            "due-pending",
            "RRSM",
            0,
            database.STATUS_PENDING,
            overdue,
            expiration_time="2000-01-01T00:00:00+00:00",
        )
        self.seed_row(
            "future-pending",
            "RRSM",
            0,
            database.STATUS_PENDING,
            future,
        )
        for event_id, state in (
            ("due-processing", database.STATUS_PROCESSING),
            ("due-completed", database.STATUS_COMPLETED),
            ("due-failed", database.STATUS_FAILED),
            ("due-incomplete", database.STATUS_INCOMPLETE),
        ):
            self.seed_row(event_id, "RRSM", 0, state, overdue)

        with mock.patch.object(database, "datetime", FixedDateTime):
            self.assertEqual(
                self.tracker.get_due_events(service="RRSM"),
                [("due-pending", "RRSM", 0.0)],
            )

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
            target_event, "RRSM", 720, database.STATUS_FAILED, overdue
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

    def test_retained_terminal_events_without_pending_rows_are_not_reopened(self):
        for status in (
            database.STATUS_COMPLETED,
            database.STATUS_FAILED,
            database.STATUS_INCOMPLETE,
        ):
            with self.subTest(status=status):
                event_id = f"retained-{status}"
                self.seed_row(
                    event_id,
                    "RRSM",
                    0,
                    status,
                    "2000-01-01T00:00:00",
                    expiration_time="2000-01-01T00:00:00+00:00",
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

    def test_explicit_cleanup_removes_identity_and_allows_fresh_registration(self):
        event_id = "cleaned-and-reregistered"
        first_result = self.apply(event_id, action="create")
        self.assertEqual(
            first_result,
            (eventtracker.EventTracker.RESULT_REGISTERED, 8),
        )
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                UPDATE event_tracker
                SET status = CASE
                        WHEN current_delay_time = 0 THEN ?
                        WHEN current_delay_time = 5 THEN ?
                        ELSE ?
                    END,
                    expiration_time = CASE
                        WHEN current_delay_time = 0 THEN ?
                        WHEN current_delay_time = 5 THEN ?
                        ELSE NULL
                    END
                WHERE event_id = ?
                """,
                (
                    database.STATUS_COMPLETED,
                    database.STATUS_FAILED,
                    database.STATUS_INCOMPLETE,
                    "2000-01-01T00:00:00+00:00",
                    "2099-01-01T00:00:00+00:00",
                    event_id,
                ),
            )

        self.assertEqual(self.tracker.cleanup_terminal_events(), 8)
        self.assertEqual(self.fetch_rows(event_id, "RRSM"), [])

        with mock.patch.object(
            self.tracker,
            "batch_register_from_policy",
            wraps=self.tracker.batch_register_from_policy,
        ) as register:
            second_result = self.apply(event_id, action="update")

        register.assert_called_once()
        self.assertEqual(
            second_result,
            (eventtracker.EventTracker.RESULT_REGISTERED, 8),
        )
        rows = self.fetch_rows(event_id, "RRSM")
        self.assertEqual(len(rows), 8)
        self.assertTrue(
            all(row["status"] == database.STATUS_PENDING for row in rows)
        )
        self.assertTrue(all(row["expiration_time"] is None for row in rows))

    def test_cleanup_blocked_event_retains_identity_and_is_not_reregistered(self):
        event_id = "blocked-cleanup-identity"
        self.apply(event_id, action="create")
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                UPDATE event_tracker
                SET status = ?, expiration_time = ?
                WHERE event_id = ?
                """,
                (
                    database.STATUS_COMPLETED,
                    "2000-01-01T00:00:00+00:00",
                    event_id,
                ),
            )
            connection.execute(
                """
                UPDATE event_tracker
                SET status = ?
                WHERE event_id = ? AND service = ? AND current_delay_time = 0
                """,
                (database.STATUS_PENDING, event_id, "RRSM"),
            )

        self.assertEqual(self.tracker.cleanup_terminal_events(), 0)
        with mock.patch.object(
            self.tracker,
            "batch_register_from_policy",
            wraps=self.tracker.batch_register_from_policy,
        ) as register:
            result = self.apply(event_id, action="update")

        register.assert_not_called()
        self.assertEqual(
            result,
            (eventtracker.EventTracker.RESULT_REFRESHED, 1),
        )
        self.assertEqual(len(self.fetch_rows(event_id, "RRSM")), 8)

    def test_registration_never_invokes_explicit_terminal_cleanup(self):
        with mock.patch.object(
            self.tracker._db,
            "cleanup_terminal_events",
            wraps=self.tracker._db.cleanup_terminal_events,
        ) as cleanup:
            self.apply("explicit-cleanup-only")

        cleanup.assert_not_called()

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

    def test_mark_completed_uses_one_persistence_transition(self):
        with mock.patch.object(
            self.tracker._db,
            "mark_event_completed",
            autospec=True,
            return_value=1,
        ) as mark_completed:
            result = self.tracker.mark_completed("event-1", "RRSM", 15)

        self.assertEqual(result, 1)
        mark_completed.assert_called_once_with(
            event_id="event-1",
            service="RRSM",
            current_delay_time=15,
        )
    def test_lifecycle_boundaries_forward_named_values_and_results(self):
        transition_time = datetime(
            2026,
            8,
            6,
            12,
            0,
            0,
            tzinfo=timezone.utc,
        )

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return transition_time

        with mock.patch.object(
            eventtracker,
            "datetime",
            FixedDateTime,
        ), mock.patch.object(
            self.tracker._db,
            "mark_event_processing",
            return_value=1,
        ) as mark_processing, mock.patch.object(
            self.tracker._db,
            "increment_processing_retry_count",
            return_value=2,
        ) as increment_retry, mock.patch.object(
            self.tracker._db,
            "mark_event_pending_for_retry",
            return_value=1,
        ) as mark_retry, mock.patch.object(
            self.tracker._db,
            "mark_event_failed",
            return_value=1,
        ) as mark_failed, mock.patch.object(
            self.tracker._db,
            "fail_abandoned_processing",
            return_value=3,
        ) as recover:
            processing_result = self.tracker.mark_as_processing(
                "event-1", "RRSM", 15
            )
            count_result = self.tracker.increment_retry_count(
                "event-1", "RRSM", 15
            )
            retry_result = self.tracker.mark_for_retry(
                "event-1",
                "RRSM",
                15,
                "provider unavailable",
            )
            failed_result = self.tracker.mark_failed(
                "event-1",
                "RRSM",
                15,
                "retry limit reached",
            )
            recovery_result = self.tracker.recover_abandoned_processing(
                error_message="abandoned by restart"
            )

        self.assertEqual(processing_result, 1)
        self.assertEqual(count_result, 2)
        self.assertEqual(retry_result, 1)
        self.assertEqual(failed_result, 1)
        self.assertEqual(recovery_result, 3)
        mark_processing.assert_called_once_with(
            event_id="event-1",
            service="RRSM",
            current_delay_time=15,
            last_query_time="2026-08-06T12:00:00+00:00",
        )
        increment_retry.assert_called_once_with(
            event_id="event-1",
            service="RRSM",
            current_delay_time=15,
        )
        mark_retry.assert_called_once_with(
            event_id="event-1",
            service="RRSM",
            current_delay_time=15,
            last_error="provider unavailable",
            next_query_time="2026-08-06T12:00:10+00:00",
        )
        mark_failed.assert_called_once_with(
            event_id="event-1",
            service="RRSM",
            current_delay_time=15,
            last_error="retry limit reached",
            last_query_time="2026-08-06T12:00:00+00:00",
        )
        recover.assert_called_once_with(
            last_error="abandoned by restart",
            last_query_time="2026-08-06T12:00:00+00:00",
        )

    def test_existing_positional_schedule_arguments_keep_their_meaning(self):
        # Positional use is intentional in this compatibility test. Production
        # callers use named arguments so field meaning is explicit at the call site.
        self.tracker.register_new_schedule(
            "positional-event",
            "RRSM",
            "2026-08-06T10:00:00.000000+00:00",
            "2026-08-06T10:01:00+00:00",
            5,
            15,
            "2026-08-06T10:05:00+00:00",
            '{"action": "create"}',
        )

        rows = self.fetch_rows("positional-event", "RRSM")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["current_delay_time"], 5)
        self.assertEqual(row["next_delay_time"], 15)
        self.assertEqual(
            row["next_query_time"],
            "2026-08-06T10:05:00+00:00",
        )
        self.assertEqual(row["emsc_alert_json"], '{"action": "create"}')


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
