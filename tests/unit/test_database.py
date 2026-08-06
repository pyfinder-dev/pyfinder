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
            "expiration_time": "2026-08-11T10:00:00+00:00",
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
        self.assertEqual(
            row["expiration_time"], "2026-08-11T10:00:00+00:00"
        )
        self.assertEqual(row["current_delay_time"], 5)
        self.assertEqual(row["next_delay_time"], 15)
        self.assertEqual(row["emsc_alert_json"], '{"action": "create"}')

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
                "expiration_time": "2026-08-11T10:00:00+00:00",
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

    def test_every_scheduler_required_mutable_field_remains_writable(self):
        self.insert_item()

        self.db._update_event_fields(
            event_id="event-1",
            service="RRSM",
            current_delay_time=5,
            status=database.STATUS_INCOMPLETE,
            last_query_time="2026-08-06T10:06:00+00:00",
            last_error="provider unavailable",
            retry_count=2,
            next_query_time="2026-08-06T10:10:00+00:00",
        )

        row = self.fetch_rows()[0]
        self.assertEqual(row["status"], database.STATUS_INCOMPLETE)
        self.assertEqual(
            row["last_query_time"],
            "2026-08-06T10:06:00+00:00",
        )
        self.assertEqual(row["last_error"], "provider unavailable")
        self.assertEqual(row["retry_count"], 2)
        self.assertEqual(
            row["next_query_time"],
            "2026-08-06T10:10:00+00:00",
        )

    def test_untrusted_mutation_fields_fail_before_sql_execution(self):
        monitored_cursor = mock.Mock(wraps=self.db.cursor)
        self.db.cursor = monitored_cursor
        rejected_fields = (
            ("unknown", "not_a_column"),
            ("targeted metadata", "origin_time"),
            ("targeted metadata", "emsc_alert_json"),
            ("schema only", "priority"),
            ("schema only", "last_data_hash"),
            ("malformed", ""),
            ("SQL fragment", "status = 'completed'"),
        )

        for category, field in rejected_fields:
            with self.subTest(category=category, field=field):
                with self.assertRaises(ValueError):
                    self.db._update_event_fields(
                        event_id="event-1",
                        service="RRSM",
                        current_delay_time=5,
                        **{field: "value"},
                    )

        monitored_cursor.execute.assert_not_called()

    def test_identity_mutation_fields_fail_before_sql_execution(self):
        monitored_cursor = mock.Mock(wraps=self.db.cursor)
        self.db.cursor = monitored_cursor

        for field in ("event_id", "service", "current_delay_time"):
            with self.subTest(field=field):
                # Identity names collide with the method's row-identity
                # parameters, so Python rejects them before the allowlist or
                # SQL execution can be reached.
                with self.assertRaises(TypeError):
                    self.db._update_event_fields(
                        event_id="event-1",
                        service="RRSM",
                        current_delay_time=5,
                        **{field: "replacement"},
                    )

        monitored_cursor.execute.assert_not_called()

    def test_invalid_mutation_field_with_none_still_fails(self):
        monitored_cursor = mock.Mock(wraps=self.db.cursor)
        self.db.cursor = monitored_cursor

        with self.assertRaises(ValueError):
            self.db._update_event_fields(
                event_id="event-1",
                service="RRSM",
                current_delay_time=5,
                priority=None,
            )

        monitored_cursor.execute.assert_not_called()

    def test_all_none_allowed_mutation_is_a_no_op(self):
        monitored_cursor = mock.Mock(wraps=self.db.cursor)
        monitored_connection = mock.Mock(wraps=self.db.conn)
        self.db.cursor = monitored_cursor
        self.db.conn = monitored_connection

        self.db._update_event_fields(
            event_id="event-1",
            service="RRSM",
            current_delay_time=5,
            status=None,
            last_query_time=None,
            last_error=None,
            retry_count=None,
            next_query_time=None,
        )

        monitored_cursor.execute.assert_not_called()
        monitored_connection.commit.assert_not_called()
        monitored_connection.rollback.assert_not_called()

    def test_sql_like_mutation_value_is_stored_as_ordinary_data(self):
        self.insert_item()
        sql_like_text = "failure'); DROP TABLE event_tracker; --"

        self.db._update_event_fields(
            event_id="event-1",
            service="RRSM",
            current_delay_time=5,
            last_error=sql_like_text,
        )

        rows = self.fetch_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["last_error"], sql_like_text)

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
        self.db.cursor = monitored_cursor
        with mock.patch.object(
            database,
            "datetime",
            FixedDateTime,
        ), mock.patch.object(
            self.db,
            "_update_event_fields",
            autospec=True,
        ) as generic_update:
            self.db.mark_event_completed(
                event_id="event-1",
                service="RRSM",
                current_delay_time=5,
            )

        generic_update.assert_not_called()
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
            ),
        )
        row = self.fetch_rows()[0]
        self.assertEqual(row["status"], database.STATUS_COMPLETED)
        self.assertEqual(
            row["last_query_time"],
            "2026-08-06T12:00:00+00:00",
        )

    def test_active_writes_roll_back_execute_failures(self):
        operations = (
            ("metadata refresh", lambda: self.update_pending_metadata()),
            (
                "completion",
                lambda: self.db.mark_event_completed(
                    event_id="event-1",
                    service="RRSM",
                    current_delay_time=5,
                ),
            ),
            (
                "generic mutation",
                lambda: self.db._update_event_fields(
                    event_id="event-1",
                    service="RRSM",
                    current_delay_time=5,
                    last_error="failure",
                ),
            ),
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
                "completion",
                lambda: self.db.mark_event_completed(
                    event_id="event-1",
                    service="RRSM",
                    current_delay_time=5,
                ),
            ),
            (
                "generic mutation",
                lambda: self.db._update_event_fields(
                    event_id="event-1",
                    service="RRSM",
                    current_delay_time=5,
                    last_error="failure",
                ),
            ),
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

    def test_active_write_rollback_failure_does_not_mask_primary_error(self):
        self.insert_item()
        real_connection = self.db.conn
        operation_error = sqlite3.OperationalError("commit failed")
        rollback_error = sqlite3.OperationalError("rollback failed")
        monitored_connection = mock.Mock(wraps=real_connection)
        monitored_connection.commit.side_effect = operation_error
        monitored_connection.rollback.side_effect = rollback_error
        self.db.conn = monitored_connection

        with self.assertRaises(sqlite3.OperationalError) as raised:
            self.db._update_event_fields(
                event_id="event-1",
                service="RRSM",
                current_delay_time=5,
                last_error="failure",
            )

        self.assertIs(raised.exception, operation_error)
        self.assertIs(raised.exception.__cause__, rollback_error)
        monitored_connection.rollback.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
