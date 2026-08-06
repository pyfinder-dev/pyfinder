"""Unit tests for the scheduled-item persistence boundary."""

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from pyfinder.services import database


class ScheduledItemPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary_directory.name) / "scheduled-items.db"
        self.db = database.ThreadSafeDB(str(self.db_path))

    def tearDown(self):
        self.db.close()
        self.temporary_directory.cleanup()

    def insert_item(self, **overrides):
        values = {
            "event_id": "event-1",
            "service": "RRSM",
            "origin_time": "2026-08-06T10:00:00.000000+00:00",
            "last_update_time": "2026-08-06T10:01:00+00:00",
            "next_query_time": "2026-08-06T10:05:00+00:00",
            "current_delay_time": 5,
            "next_delay_time": 15,
            "emsc_alert_json": '{"action": "create"}',
        }
        values.update(overrides)
        return self.db.insert_scheduled_item(**values)

    def fetch_rows(self):
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM event_tracker ORDER BY current_delay_time"
                )
            ]
        finally:
            connection.close()

    def update_pending_metadata(self, **overrides):
        values = {
            "event_id": "event-1",
            "service": "RRSM",
            "origin_time": "2026-08-06T11:00:00.000000+00:00",
            "last_update_time": "2026-08-06T11:01:00+00:00",
            "emsc_alert_json": '{"action": "update"}',
            "last_modified": "2026-08-06T11:02:00+00:00",
        }
        values.update(overrides)
        return self.db.update_pending_emsc_metadata(**values)

    def set_lifecycle_state(
        self,
        status,
        retry_count=0,
        event_id="event-1",
        service="RRSM",
        current_delay_time=5,
    ):
        """Prepare lifecycle state directly without using behavior under test."""
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                UPDATE event_tracker
                SET status = ?, retry_count = ?
                WHERE event_id = ? AND service = ? AND current_delay_time = ?
                """,
                (
                    status,
                    retry_count,
                    event_id,
                    service,
                    current_delay_time,
                ),
            )

    def seed_cleanup_item(
        self,
        event_id,
        service,
        current_delay_time,
        status,
        expiration_time=None,
    ):
        """Create cleanup state through fixture-only direct schema setup."""
        self.insert_item(
            event_id=event_id,
            service=service,
            current_delay_time=current_delay_time,
            next_delay_time=current_delay_time + 1,
        )
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                UPDATE event_tracker
                SET status = ?, expiration_time = ?
                WHERE event_id = ? AND service = ? AND current_delay_time = ?
                """,
                (
                    status,
                    expiration_time,
                    event_id,
                    service,
                    current_delay_time,
                ),
            )

    def test_failed_is_intended_while_incomplete_remains_legacy(self):
        self.assertEqual(database.STATUS_FAILED, "failed")
        self.assertEqual(database.STATUS_INCOMPLETE, "incomplete")

    def test_single_scheduled_item_is_committed_with_supplied_fields(self):
        self.insert_item()

        rows = self.fetch_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["event_id"], "event-1")
        self.assertEqual(row["service"], "RRSM")
        self.assertEqual(row["status"], database.STATUS_PENDING)
        self.assertEqual(
            row["origin_time"], "2026-08-06T10:00:00.000000+00:00"
        )
        self.assertEqual(
            row["last_update_time"], "2026-08-06T10:01:00+00:00"
        )
        self.assertIsNone(row["last_query_time"])
        self.assertEqual(
            row["next_query_time"], "2026-08-06T10:05:00+00:00"
        )
        self.assertEqual(row["retry_count"], 0)
        self.assertIsNone(row["expiration_time"])
        self.assertEqual(row["current_delay_time"], 5)
        self.assertEqual(row["next_delay_time"], 15)
        self.assertEqual(row["emsc_alert_json"], '{"action": "create"}')

        with sqlite3.connect(self.db_path) as connection:
            columns = {
                column[1]
                for column in connection.execute("PRAGMA table_info(event_tracker)")
            }
        self.assertIn("expiration_time", columns)

    def test_failed_insert_rolls_back_and_later_stage_can_commit(self):
        real_connection = self.db.conn
        monitored_connection = mock.Mock(wraps=real_connection)
        self.db.conn = monitored_connection

        self.insert_item(current_delay_time=0, next_delay_time=5)
        monitored_connection.commit.assert_called_once_with()
        monitored_connection.reset_mock()

        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_item(current_delay_time=0, next_delay_time=5)

        monitored_connection.rollback.assert_called_once_with()
        monitored_connection.commit.assert_not_called()

        self.insert_item(
            current_delay_time=5,
            next_delay_time=15,
            next_query_time="2026-08-06T10:10:00+00:00",
        )
        self.assertEqual(
            [row["current_delay_time"] for row in self.fetch_rows()],
            [0, 5],
        )

    def test_commit_failure_rolls_back_and_propagates_commit_error(self):
        real_connection = self.db.conn
        monitored_connection = mock.Mock(wraps=real_connection)
        monitored_connection.commit.side_effect = sqlite3.OperationalError(
            "commit failed"
        )
        self.db.conn = monitored_connection

        with self.assertRaisesRegex(
            sqlite3.OperationalError,
            "commit failed",
        ):
            self.insert_item()

        monitored_connection.rollback.assert_called_once_with()
        self.assertEqual(self.fetch_rows(), [])

    def test_rollback_failure_does_not_mask_commit_error(self):
        real_connection = self.db.conn
        monitored_connection = mock.Mock(wraps=real_connection)
        commit_error = sqlite3.OperationalError("commit failed")
        rollback_error = sqlite3.OperationalError("rollback failed")
        monitored_connection.commit.side_effect = commit_error
        monitored_connection.rollback.side_effect = rollback_error
        self.db.conn = monitored_connection

        with self.assertRaises(sqlite3.OperationalError) as raised:
            self.insert_item()

        self.assertIs(raised.exception, commit_error)
        self.assertIs(raised.exception.__cause__, rollback_error)

    def test_next_query_time_is_required(self):
        required_values = {
            "event_id": "event-1",
            "service": "RRSM",
            "origin_time": "2026-08-06T10:00:00.000000+00:00",
            "last_update_time": "2026-08-06T10:01:00+00:00",
        }
        with self.assertRaises(TypeError):
            self.db.insert_scheduled_item(**required_values)

        with self.assertRaises(ValueError):
            self.insert_item(next_query_time=None)

    def test_current_delay_is_rejected_before_sql_execution(self):
        monitored_cursor = mock.Mock(wraps=self.db.cursor)
        self.db.cursor = monitored_cursor

        with self.assertRaisesRegex(
            ValueError,
            "requires current_delay_time",
        ):
            self.insert_item(current_delay_time=None)

        monitored_cursor.execute.assert_not_called()

    def test_zero_current_delay_is_a_valid_initial_stage(self):
        self.insert_item(current_delay_time=0, next_delay_time=5)

        rows = self.fetch_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["current_delay_time"], 0)
        self.assertEqual(rows[0]["status"], database.STATUS_PENDING)

    def test_insert_does_not_accept_caller_controlled_status(self):
        with self.assertRaises(TypeError):
            self.insert_item(status=database.STATUS_COMPLETED)

        self.assertEqual(self.fetch_rows(), [])

    def test_scheduler_metadata_has_exact_stored_shape_and_values(self):
        self.insert_item()
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                UPDATE event_tracker
                SET status = ?,
                    last_query_time = ?,
                    retry_count = ?,
                    last_data_snapshot = ?,
                    last_update_time = ?,
                    expiration_time = ?,
                    priority = ?,
                    last_error = ?,
                    last_data_hash = ?,
                    last_modified = ?
                WHERE event_id = ? AND service = ? AND current_delay_time = ?
                """,
                (
                    database.STATUS_PROCESSING,
                    "2026-08-06T10:04:00+00:00",
                    3,
                    "downstream snapshot",
                    "omitted update time",
                    "2000-01-01T00:00:00+00:00",
                    9,
                    "omitted error",
                    "omitted hash",
                    "omitted modification time",
                    "event-1",
                    "RRSM",
                    5,
                ),
            )

        metadata = self.db.get_event_meta(
            event_id="event-1",
            service="RRSM",
            current_delay_time=5,
        )

        self.assertIsInstance(metadata, dict)
        self.assertEqual(
            metadata,
            {
                "event_id": "event-1",
                "service": "RRSM",
                "origin_time": "2026-08-06T10:00:00.000000+00:00",
                "last_query_time": "2026-08-06T10:04:00+00:00",
                "next_query_time": "2026-08-06T10:05:00+00:00",
                "status": database.STATUS_PROCESSING,
                "retry_count": 3,
                "current_delay_time": 5,
                "next_delay_time": 15,
                "emsc_alert_json": '{"action": "create"}',
                "last_data_snapshot": "downstream snapshot",
            },
        )
        self.assertEqual(
            set(metadata),
            set(database.ThreadSafeDB.SCHEDULER_METADATA_FIELDS),
        )
        self.assertTrue(
            {
                "last_update_time",
                "expiration_time",
                "priority",
                "last_error",
                "last_data_hash",
                "last_modified",
            }.isdisjoint(metadata)
        )

    def test_scheduler_metadata_returns_none_for_missing_row(self):
        self.assertIsNone(
            self.db.get_event_meta(
                event_id="missing-event",
                service="RRSM",
                current_delay_time=0,
            )
        )

    def test_metadata_refresh_returns_matching_pending_row_count(self):
        self.insert_item(current_delay_time=0, next_delay_time=5)
        self.insert_item(
            current_delay_time=5,
            next_delay_time=15,
            next_query_time="2026-08-06T10:10:00+00:00",
        )
        self.insert_item(
            event_id="other-event",
            current_delay_time=0,
            next_delay_time=5,
        )

        updated_rows = self.update_pending_metadata()

        self.assertEqual(updated_rows, 2)
        target_rows = [
            row for row in self.fetch_rows() if row["event_id"] == "event-1"
        ]
        self.assertTrue(
            all(
                row["emsc_alert_json"] == '{"action": "update"}'
                for row in target_rows
            )
        )

    def test_completion_uses_fixed_write_and_preserves_timestamp_shape(self):
        self.insert_item()
        self.set_lifecycle_state(database.STATUS_PROCESSING)
        completion_time = datetime(
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
                return completion_time

        monitored_cursor = mock.Mock(wraps=self.db.cursor)
        monitored_cursor.rowcount = 1
        self.db.cursor = monitored_cursor
        with mock.patch.object(
            database,
            "datetime",
            FixedDateTime,
        ):
            affected_rows = self.db.mark_event_completed(
                event_id="event-1",
                service="RRSM",
                current_delay_time=5,
            )
        monitored_cursor.execute.assert_called_once()
        statement, parameters = monitored_cursor.execute.call_args.args
        self.assertIn("SET status = ?, last_query_time = ?", statement)
        self.assertEqual(
            parameters,
            (
                database.STATUS_COMPLETED,
                "2026-08-06T12:00:00+00:00",
                "event-1",
                "RRSM",
                5,
                database.STATUS_PROCESSING,
            ),
        )
        self.assertEqual(affected_rows, 1)
        row = self.fetch_rows()[0]
        self.assertEqual(row["status"], database.STATUS_COMPLETED)
        self.assertEqual(
            row["last_query_time"],
            "2026-08-06T12:00:00+00:00",
        )

    def test_processing_assignment_is_conditional_and_explicit(self):
        self.insert_item()
        processing_time = "2026-08-06T10:06:00+00:00"

        affected_rows = self.db.mark_event_processing(
            event_id="event-1",
            service="RRSM",
            current_delay_time=5,
            last_query_time=processing_time,
        )

        self.assertEqual(affected_rows, 1)
        row = self.fetch_rows()[0]
        self.assertEqual(row["status"], database.STATUS_PROCESSING)
        self.assertEqual(row["last_query_time"], processing_time)
        self.assertEqual(
            self.db.mark_event_processing(
                event_id="event-1",
                service="RRSM",
                current_delay_time=5,
                last_query_time="2026-08-06T10:07:00+00:00",
            ),
            0,
        )
        self.assertEqual(
            self.db.mark_event_processing(
                event_id="missing-event",
                service="RRSM",
                current_delay_time=5,
                last_query_time=processing_time,
            ),
            0,
        )

    def test_processing_assignment_rejects_every_non_pending_state(self):
        self.insert_item()
        for state in (
            database.STATUS_PROCESSING,
            database.STATUS_COMPLETED,
            database.STATUS_FAILED,
            database.STATUS_INCOMPLETE,
        ):
            with self.subTest(state=state):
                self.set_lifecycle_state(state)
                before = self.fetch_rows()[0]
                self.assertEqual(
                    self.db.mark_event_processing(
                        event_id="event-1",
                        service="RRSM",
                        current_delay_time=5,
                        last_query_time="2026-08-06T12:00:00+00:00",
                    ),
                    0,
                )
                self.assertEqual(self.fetch_rows()[0], before)

    def test_completion_rejects_missing_and_non_processing_rows(self):
        self.insert_item()
        for state in (
            database.STATUS_PENDING,
            database.STATUS_COMPLETED,
            database.STATUS_FAILED,
            database.STATUS_INCOMPLETE,
        ):
            with self.subTest(state=state):
                self.set_lifecycle_state(state)
                before = self.fetch_rows()[0]
                self.assertEqual(
                    self.db.mark_event_completed(
                        event_id="event-1",
                        service="RRSM",
                        current_delay_time=5,
                    ),
                    0,
                )
                self.assertEqual(self.fetch_rows()[0], before)
        self.assertEqual(
            self.db.mark_event_completed(
                event_id="missing-event",
                service="RRSM",
                current_delay_time=5,
            ),
            0,
        )

    def test_processing_retry_count_returns_each_newly_persisted_value(self):
        self.insert_item()
        self.set_lifecycle_state(database.STATUS_PROCESSING)

        for expected_count in (1, 2, 3):
            with self.subTest(expected_count=expected_count):
                self.assertEqual(
                    self.db.increment_processing_retry_count(
                        event_id="event-1",
                        service="RRSM",
                        current_delay_time=5,
                    ),
                    expected_count,
                )
                self.assertEqual(
                    self.fetch_rows()[0]["retry_count"],
                    expected_count,
                )

    def test_retry_increment_rejects_missing_and_non_processing_rows(self):
        self.insert_item()
        self.assertIsNone(
            self.db.increment_processing_retry_count(
                event_id="missing-event",
                service="RRSM",
                current_delay_time=5,
            )
        )
        for state in (
            database.STATUS_PENDING,
            database.STATUS_COMPLETED,
            database.STATUS_FAILED,
            database.STATUS_INCOMPLETE,
        ):
            with self.subTest(state=state):
                self.set_lifecycle_state(state, retry_count=2)
                self.assertIsNone(
                    self.db.increment_processing_retry_count(
                        event_id="event-1",
                        service="RRSM",
                        current_delay_time=5,
                    )
                )
                self.assertEqual(self.fetch_rows()[0]["retry_count"], 2)

    def test_retry_increment_execute_failure_rolls_back_increment(self):
        self.insert_item()
        self.set_lifecycle_state(database.STATUS_PROCESSING)
        real_cursor = self.db.cursor
        operation_error = sqlite3.OperationalError("readback failed")
        monitored_cursor = mock.Mock(wraps=real_cursor)
        real_connection = self.db.conn

        def execute_with_readback_failure(statement, parameters=()):
            if "SELECT retry_count" in statement:
                raise operation_error
            return real_cursor.execute(statement, parameters)

        monitored_cursor.execute.side_effect = execute_with_readback_failure
        monitored_connection = mock.Mock(wraps=real_connection)
        self.db.cursor = monitored_cursor
        self.db.conn = monitored_connection
        try:
            with self.assertRaises(sqlite3.OperationalError) as raised:
                self.db.increment_processing_retry_count(
                    event_id="event-1",
                    service="RRSM",
                    current_delay_time=5,
                )
        finally:
            self.db.cursor = real_cursor
            self.db.conn = real_connection

        self.assertIs(raised.exception, operation_error)
        monitored_connection.rollback.assert_called_once_with()
        monitored_connection.commit.assert_not_called()
        self.assertEqual(self.fetch_rows()[0]["retry_count"], 0)

    def test_retry_increment_commit_failure_rolls_back_increment(self):
        self.insert_item()
        self.set_lifecycle_state(database.STATUS_PROCESSING)
        real_connection = self.db.conn
        operation_error = sqlite3.OperationalError("commit failed")
        monitored_connection = mock.Mock(wraps=real_connection)
        monitored_connection.commit.side_effect = operation_error
        self.db.conn = monitored_connection
        try:
            with self.assertRaises(sqlite3.OperationalError) as raised:
                self.db.increment_processing_retry_count(
                    event_id="event-1",
                    service="RRSM",
                    current_delay_time=5,
                )
        finally:
            self.db.conn = real_connection

        self.assertIs(raised.exception, operation_error)
        monitored_connection.rollback.assert_called_once_with()
        self.assertEqual(self.fetch_rows()[0]["retry_count"], 0)

    def test_retry_transition_persists_explicit_time_and_diagnostic(self):
        self.insert_item()
        self.set_lifecycle_state(database.STATUS_PROCESSING, retry_count=1)
        retry_time = "2026-08-06T12:00:10+00:00"

        affected_rows = self.db.mark_event_pending_for_retry(
            event_id="event-1",
            service="RRSM",
            current_delay_time=5,
            last_error="provider unavailable",
            next_query_time=retry_time,
        )

        self.assertEqual(affected_rows, 1)
        row = self.fetch_rows()[0]
        self.assertEqual(row["status"], database.STATUS_PENDING)
        self.assertEqual(row["last_error"], "provider unavailable")
        self.assertEqual(row["next_query_time"], retry_time)
        self.assertEqual(row["retry_count"], 1)

    def test_retry_transition_rejects_missing_and_non_processing_rows(self):
        self.insert_item()
        for state in (
            database.STATUS_PENDING,
            database.STATUS_COMPLETED,
            database.STATUS_FAILED,
            database.STATUS_INCOMPLETE,
        ):
            with self.subTest(state=state):
                self.set_lifecycle_state(state)
                before = self.fetch_rows()[0]
                self.assertEqual(
                    self.db.mark_event_pending_for_retry(
                        event_id="event-1",
                        service="RRSM",
                        current_delay_time=5,
                        last_error="new failure",
                        next_query_time="2026-08-06T12:00:10+00:00",
                    ),
                    0,
                )
                self.assertEqual(self.fetch_rows()[0], before)
        self.assertEqual(
            self.db.mark_event_pending_for_retry(
                event_id="missing-event",
                service="RRSM",
                current_delay_time=5,
                last_error="new failure",
                next_query_time="2026-08-06T12:00:10+00:00",
            ),
            0,
        )

    def test_terminal_failure_persists_processing_outcome(self):
        failure_time = "2026-08-06T12:00:00+00:00"
        self.insert_item()
        self.set_lifecycle_state(database.STATUS_PROCESSING)

        affected_rows = self.db.mark_event_failed(
            event_id="event-1",
            service="RRSM",
            current_delay_time=5,
            last_error="required metadata missing",
            last_query_time=failure_time,
        )

        self.assertEqual(affected_rows, 1)
        row = self.fetch_rows()[0]
        self.assertEqual(row["status"], database.STATUS_FAILED)
        self.assertEqual(row["last_error"], "required metadata missing")
        self.assertEqual(row["last_query_time"], failure_time)

    def test_terminal_failure_rejects_pending_terminal_and_legacy_rows(self):
        self.insert_item()
        for state in (
            database.STATUS_PENDING,
            database.STATUS_COMPLETED,
            database.STATUS_FAILED,
            database.STATUS_INCOMPLETE,
        ):
            with self.subTest(state=state):
                self.set_lifecycle_state(state)
                before = self.fetch_rows()[0]
                self.assertEqual(
                    self.db.mark_event_failed(
                        event_id="event-1",
                        service="RRSM",
                        current_delay_time=5,
                        last_error="replacement failure",
                        last_query_time="2026-08-06T12:00:00+00:00",
                    ),
                    0,
                )
                self.assertEqual(self.fetch_rows()[0], before)
        self.assertEqual(
            self.db.mark_event_failed(
                event_id="missing-event",
                service="RRSM",
                current_delay_time=5,
                last_error="replacement failure",
                last_query_time="2026-08-06T12:00:00+00:00",
            ),
            0,
        )

    def test_startup_recovery_fails_all_and_only_processing_rows(self):
        states = (
            database.STATUS_PENDING,
            database.STATUS_PROCESSING,
            database.STATUS_PROCESSING,
            database.STATUS_COMPLETED,
            database.STATUS_FAILED,
            database.STATUS_INCOMPLETE,
        )
        for delay, state in enumerate(states):
            self.insert_item(
                current_delay_time=delay,
                next_delay_time=delay + 1,
            )
            self.set_lifecycle_state(state, current_delay_time=delay)
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "UPDATE event_tracker SET expiration_time = ?",
                ("2000-01-01T00:00:00+00:00",),
            )
        before = self.fetch_rows()

        affected_rows = self.db.fail_abandoned_processing(
            last_error="abandoned by local restart",
            last_query_time="2026-08-06T12:00:00+00:00",
        )

        self.assertEqual(affected_rows, 2)
        rows = self.fetch_rows()
        self.assertEqual(
            [row["status"] for row in rows],
            [
                database.STATUS_PENDING,
                database.STATUS_FAILED,
                database.STATUS_FAILED,
                database.STATUS_COMPLETED,
                database.STATUS_FAILED,
                database.STATUS_INCOMPLETE,
            ],
        )
        for row in rows[1:3]:
            self.assertEqual(row["last_error"], "abandoned by local restart")
        for index in (0, 3, 4, 5):
            self.assertEqual(rows[index], before[index])
        self.assertEqual(
            self.db.fail_abandoned_processing(
                last_error="second recovery must be a no-op",
                last_query_time="2026-08-06T12:01:00+00:00",
            ),
            0,
        )

    def test_cleanup_deletes_events_with_uniform_terminal_statuses(self):
        for index, status in enumerate(
            (
                database.STATUS_COMPLETED,
                database.STATUS_FAILED,
                database.STATUS_INCOMPLETE,
            )
        ):
            event_id = f"terminal-{index}"
            self.seed_cleanup_item(event_id, "RRSM", 0, status)
            self.seed_cleanup_item(event_id, "RRSM", 5, status)

        self.assertEqual(self.db.cleanup_terminal_events(), 6)
        self.assertEqual(self.fetch_rows(), [])
        self.assertEqual(self.db.cleanup_terminal_events(), 0)

    def test_cleanup_deletes_mixed_terminal_services_and_delays(self):
        terminal_rows = (
            ("RRSM", 0, database.STATUS_COMPLETED),
            ("RRSM", 5, database.STATUS_FAILED),
            ("OTHER", 0, database.STATUS_INCOMPLETE),
            ("OTHER", 15, database.STATUS_COMPLETED),
        )
        for service, delay, status in terminal_rows:
            self.seed_cleanup_item(
                "mixed-terminal-event",
                service,
                delay,
                status,
            )

        self.assertEqual(self.db.cleanup_terminal_events(), 4)
        self.assertEqual(self.fetch_rows(), [])

    def test_cleanup_protects_whole_event_for_every_nonterminal_status(self):
        blockers = (
            ("pending", database.STATUS_PENDING),
            ("processing", database.STATUS_PROCESSING),
            ("null", None),
            ("unknown", "awaiting_external_result"),
        )
        for label, blocker in blockers:
            event_id = f"blocked-{label}"
            self.seed_cleanup_item(
                event_id,
                "RRSM",
                0,
                database.STATUS_COMPLETED,
            )
            self.seed_cleanup_item(event_id, "OTHER", 5, blocker)

        before = self.fetch_rows()
        self.assertEqual(self.db.cleanup_terminal_events(), 0)
        self.assertEqual(self.fetch_rows(), before)

    def test_cleanup_deletes_eligible_event_and_leaves_blocked_event(self):
        for delay, status in enumerate(
            (
                database.STATUS_COMPLETED,
                database.STATUS_FAILED,
                database.STATUS_INCOMPLETE,
            )
        ):
            self.seed_cleanup_item("eligible-event", "RRSM", delay, status)
        self.seed_cleanup_item(
            "blocked-event",
            "RRSM",
            0,
            database.STATUS_COMPLETED,
        )
        self.seed_cleanup_item(
            "blocked-event",
            "OTHER",
            5,
            database.STATUS_PENDING,
        )

        self.assertEqual(self.db.cleanup_terminal_events(), 3)
        remaining = self.fetch_rows()
        self.assertEqual({row["event_id"] for row in remaining}, {"blocked-event"})
        self.assertEqual(len(remaining), 2)

    def test_cleanup_ignores_old_future_null_and_mixed_expiration_values(self):
        terminal_rows = (
            (0, database.STATUS_COMPLETED, "2000-01-01T00:00:00+00:00"),
            (5, database.STATUS_FAILED, "2099-01-01T00:00:00+00:00"),
            (15, database.STATUS_INCOMPLETE, None),
        )
        for delay, status, expiration_time in terminal_rows:
            self.seed_cleanup_item(
                "expiration-independent",
                "RRSM",
                delay,
                status,
                expiration_time=expiration_time,
            )
        self.seed_cleanup_item(
            "expired-pending",
            "RRSM",
            0,
            database.STATUS_PENDING,
            expiration_time="2000-01-01T00:00:00+00:00",
        )

        self.assertEqual(self.db.cleanup_terminal_events(), 3)
        remaining = self.fetch_rows()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["event_id"], "expired-pending")
        self.assertEqual(remaining[0]["status"], database.STATUS_PENDING)

    def test_cleanup_execute_failure_rolls_back_and_propagates(self):
        self.seed_cleanup_item(
            "cleanup-execute-failure",
            "RRSM",
            0,
            database.STATUS_COMPLETED,
        )
        real_cursor = self.db.cursor
        real_connection = self.db.conn
        operation_error = sqlite3.OperationalError("cleanup execute failed")
        monitored_cursor = mock.Mock(wraps=real_cursor)
        monitored_cursor.execute.side_effect = operation_error
        monitored_connection = mock.Mock(wraps=real_connection)
        self.db.cursor = monitored_cursor
        self.db.conn = monitored_connection
        try:
            with self.assertRaises(sqlite3.OperationalError) as raised:
                self.db.cleanup_terminal_events()
        finally:
            self.db.cursor = real_cursor
            self.db.conn = real_connection

        self.assertIs(raised.exception, operation_error)
        monitored_connection.rollback.assert_called_once_with()
        monitored_connection.commit.assert_not_called()
        self.assertEqual(len(self.fetch_rows()), 1)

    def test_cleanup_commit_failure_rolls_back_deletion_and_propagates(self):
        self.seed_cleanup_item(
            "cleanup-commit-failure",
            "RRSM",
            0,
            database.STATUS_COMPLETED,
        )
        real_connection = self.db.conn
        operation_error = sqlite3.OperationalError("cleanup commit failed")
        monitored_connection = mock.Mock(wraps=real_connection)
        monitored_connection.commit.side_effect = operation_error
        self.db.conn = monitored_connection
        try:
            with self.assertRaises(sqlite3.OperationalError) as raised:
                self.db.cleanup_terminal_events()
        finally:
            self.db.conn = real_connection

        self.assertIs(raised.exception, operation_error)
        monitored_connection.rollback.assert_called_once_with()
        self.assertEqual(len(self.fetch_rows()), 1)

    def test_active_writes_roll_back_execute_failures(self):
        operations = (
            ("metadata refresh", lambda: self.update_pending_metadata()),
            (
                "processing assignment",
                lambda: self.db.mark_event_processing(
                    event_id="event-1",
                    service="RRSM",
                    current_delay_time=5,
                    last_query_time="2026-08-06T12:00:00+00:00",
                ),
            ),
            (
                "completion",
                lambda: self.db.mark_event_completed(
                    event_id="event-1",
                    service="RRSM",
                    current_delay_time=5,
                ),
            ),
            (
                "retry increment",
                lambda: self.db.increment_processing_retry_count(
                    event_id="event-1",
                    service="RRSM",
                    current_delay_time=5,
                ),
            ),
            (
                "paced retry",
                lambda: self.db.mark_event_pending_for_retry(
                    event_id="event-1",
                    service="RRSM",
                    current_delay_time=5,
                    last_error="failure",
                    next_query_time="2026-08-06T12:00:10+00:00",
                ),
            ),
            (
                "terminal failure",
                lambda: self.db.mark_event_failed(
                    event_id="event-1",
                    service="RRSM",
                    current_delay_time=5,
                    last_error="failure",
                    last_query_time="2026-08-06T12:00:00+00:00",
                ),
            ),
            (
                "startup recovery",
                lambda: self.db.fail_abandoned_processing(
                    last_error="abandoned",
                    last_query_time="2026-08-06T12:00:00+00:00",
                ),
            ),
            ("terminal cleanup", self.db.cleanup_terminal_events),
        )
        real_cursor = self.db.cursor
        real_connection = self.db.conn

        for label, operation in operations:
            with self.subTest(operation=label):
                operation_error = sqlite3.OperationalError("execute failed")
                monitored_cursor = mock.Mock(wraps=real_cursor)
                monitored_cursor.execute.side_effect = operation_error
                monitored_connection = mock.Mock(wraps=real_connection)
                self.db.cursor = monitored_cursor
                self.db.conn = monitored_connection
                try:
                    with self.assertRaises(sqlite3.OperationalError) as raised:
                        operation()
                    self.assertIs(raised.exception, operation_error)
                    monitored_connection.rollback.assert_called_once_with()
                    monitored_connection.commit.assert_not_called()
                finally:
                    self.db.cursor = real_cursor
                    self.db.conn = real_connection

    def test_active_writes_roll_back_commit_failures(self):
        self.insert_item()
        operations = (
            ("metadata refresh", lambda: self.update_pending_metadata()),
            (
                "processing assignment",
                lambda: self.db.mark_event_processing(
                    event_id="event-1",
                    service="RRSM",
                    current_delay_time=5,
                    last_query_time="2026-08-06T12:00:00+00:00",
                ),
            ),
            (
                "completion",
                lambda: self.db.mark_event_completed(
                    event_id="event-1",
                    service="RRSM",
                    current_delay_time=5,
                ),
            ),
            (
                "retry increment",
                lambda: self.db.increment_processing_retry_count(
                    event_id="event-1",
                    service="RRSM",
                    current_delay_time=5,
                ),
            ),
            (
                "paced retry",
                lambda: self.db.mark_event_pending_for_retry(
                    event_id="event-1",
                    service="RRSM",
                    current_delay_time=5,
                    last_error="failure",
                    next_query_time="2026-08-06T12:00:10+00:00",
                ),
            ),
            (
                "terminal failure",
                lambda: self.db.mark_event_failed(
                    event_id="event-1",
                    service="RRSM",
                    current_delay_time=5,
                    last_error="failure",
                    last_query_time="2026-08-06T12:00:00+00:00",
                ),
            ),
            (
                "startup recovery",
                lambda: self.db.fail_abandoned_processing(
                    last_error="abandoned",
                    last_query_time="2026-08-06T12:00:00+00:00",
                ),
            ),
            ("terminal cleanup", self.db.cleanup_terminal_events),
        )
        real_connection = self.db.conn

        for label, operation in operations:
            with self.subTest(operation=label):
                operation_error = sqlite3.OperationalError("commit failed")
                monitored_connection = mock.Mock(wraps=real_connection)
                monitored_connection.commit.side_effect = operation_error
                self.db.conn = monitored_connection
                try:
                    with self.assertRaises(sqlite3.OperationalError) as raised:
                        operation()
                    self.assertIs(raised.exception, operation_error)
                    monitored_connection.commit.assert_called_once_with()
                    monitored_connection.rollback.assert_called_once_with()
                finally:
                    self.db.conn = real_connection

    def test_cleanup_rollback_failure_does_not_mask_primary_error(self):
        self.insert_item()
        self.set_lifecycle_state(database.STATUS_COMPLETED)
        real_connection = self.db.conn
        operation_error = sqlite3.OperationalError("commit failed")
        rollback_error = sqlite3.OperationalError("rollback failed")
        monitored_connection = mock.Mock(wraps=real_connection)
        monitored_connection.commit.side_effect = operation_error
        monitored_connection.rollback.side_effect = rollback_error
        self.db.conn = monitored_connection

        with self.assertRaises(sqlite3.OperationalError) as raised:
            self.db.cleanup_terminal_events()

        self.assertIs(raised.exception, operation_error)
        self.assertIs(raised.exception.__cause__, rollback_error)
        monitored_connection.rollback.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
