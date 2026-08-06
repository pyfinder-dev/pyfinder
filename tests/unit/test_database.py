"""Unit tests for the scheduled-item persistence boundary."""

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


if __name__ == "__main__":
    unittest.main()
