# -*- coding: utf-8 -*-
# !/usr/bin/env python
""" 
Database utility for tracking events and their query status. This module normally 
should not be used directly, but rather through the services.eventtracker.EventTracker 
class, which provides a higher-level interface for database operations related to event 
updates and follow-ups.
"""

import sqlite3
import threading
from datetime import datetime, timezone
from uuid import uuid4


STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_INCOMPLETE = "incomplete"


class ThreadSafeDB:
    _lock = threading.Lock()
    SCHEDULER_METADATA_FIELDS = (
        "event_id",
        "service",
        "origin_time",
        "last_query_time",
        "next_query_time",
        "status",
        "retry_count",
        "current_delay_time",
        "next_delay_time",
        "emsc_alert_json",
        "last_data_snapshot",
    )

    def __init__(self, db_path="event_update_follow_up.db"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.cursor = self.conn.cursor()
        try:
            self._enable_wal()
            self._create_table()
        except BaseException:
            self.conn.close()
            raise

    def _enable_wal(self):
        """Enable Write-Ahead Logging (WAL) mode for better concurrency."""
        self.cursor.execute('PRAGMA journal_mode=WAL;')

    def _create_table(self):
        """Create the table and preserve existing rows when adding execution IDs."""
        with self._lock:
            # Serialize schema inspection and alteration across connections.
            # Existing databases keep their rows and composite primary key.
            self.cursor.execute("BEGIN IMMEDIATE")
            # All timestamps are stored as UTC ISO 8601 strings
            self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS event_tracker (
                event_id TEXT,
                service TEXT,
                status TEXT,
                origin_time TEXT,
                last_update_time TEXT,
                last_query_time TEXT,
                next_query_time TEXT,
                current_delay_time REAL DEFAULT NULL,
                next_delay_time REAL DEFAULT NULL,
                retry_count INTEGER DEFAULT 0,
                expiration_time TEXT,
                priority INTEGER DEFAULT 1,
                last_error TEXT DEFAULT NULL,
                last_data_hash TEXT DEFAULT NULL,
                last_data_snapshot TEXT DEFAULT NULL,
                emsc_alert_json TEXT DEFAULT NULL,
                last_modified TEXT DEFAULT (DATETIME('now')),
                execution_id TEXT DEFAULT NULL,
                PRIMARY KEY (event_id, service, current_delay_time)
            )
            ''')
            columns = {
                row[1] for row in self.cursor.execute("PRAGMA table_info(event_tracker)")
            }
            if "execution_id" not in columns:
                self.cursor.execute(
                    "ALTER TABLE event_tracker ADD COLUMN execution_id TEXT DEFAULT NULL"
                )

            self.conn.commit()

    def _execute_write(self, statement, parameters):
        """Execute one write while preserving its primary failure on rollback."""
        with self._lock:
            try:
                self.cursor.execute(statement, parameters)
                affected_rows = self.cursor.rowcount
                self.conn.commit()
                return affected_rows
            except Exception as operation_error:
                # A failed write or commit must not leave an open transaction
                # that a later operation could commit accidentally.
                try:
                    self.conn.rollback()
                except Exception as rollback_error:
                    # Keep the write or commit error primary while retaining
                    # the rollback failure as useful secondary context.
                    raise operation_error from rollback_error
                raise

    def insert_scheduled_item(
            self, event_id, service, origin_time, last_update_time,
            next_query_time, current_delay_time=None, next_delay_time=None,
            emsc_alert_json=None):
        """Persist one scheduled item and commit it independently."""
        if next_query_time is None:
            raise ValueError("A scheduled item requires next_query_time")
        if current_delay_time is None:
            raise ValueError("A scheduled item requires current_delay_time")

        self._execute_write(
            statement='''
                INSERT INTO event_tracker (
                    event_id, service, status, origin_time, last_update_time,
                    last_query_time, next_query_time, retry_count,
                    current_delay_time, next_delay_time, emsc_alert_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            parameters=(
                event_id,
                service,
                STATUS_PENDING,
                origin_time,
                last_update_time,
                None,
                next_query_time,
                0,
                current_delay_time,
                next_delay_time,
                emsc_alert_json,
            ),
        )

    def fetch_due_events(self, service=None):
        """Fetch events that are due for querying, optionally filtered by service."""
        now = datetime.now(timezone.utc).isoformat(timespec='seconds')
        query = '''
        SELECT event_id, service, current_delay_time FROM event_tracker 
        WHERE next_query_time <= ? AND status IN (?)
        '''
        params = [now, STATUS_PENDING]
        
        if service:
            query += ' AND service = ?'
            params.append(service)
        query += ' ORDER BY priority DESC, next_query_time'
        
        with self._lock:
            self.cursor.execute(query, params)
            return self.cursor.fetchall()

    def event_service_exists(self, event_id, service):
        """Return whether any scheduled row exists for an event and service."""
        with self._lock:
            self.cursor.execute('''
                SELECT 1
                FROM event_tracker
                WHERE event_id = ? AND service = ?
                LIMIT 1
            ''', (event_id, service))
            return self.cursor.fetchone() is not None

    def update_pending_emsc_metadata(
        self,
        event_id,
        service,
        origin_time,
        last_update_time,
        emsc_alert_json,
        last_modified,
    ):
        """Refresh EMSC metadata on every matching pending scheduled row."""
        return self._execute_write(
            statement='''
                UPDATE event_tracker
                SET origin_time = ?,
                    last_update_time = ?,
                    emsc_alert_json = ?,
                    last_modified = ?
                WHERE event_id = ? AND service = ? AND status = ?
            ''',
            parameters=(
                origin_time,
                last_update_time,
                emsc_alert_json,
                last_modified,
                event_id,
                service,
                STATUS_PENDING,
            ),
        )

    def mark_event_processing(
        self,
        event_id,
        service,
        current_delay_time,
        last_query_time,
    ):
        """Assign a fresh internal execution identity to one pending row.

        This token identifies a deliberate execution, not the public calculation
        ID. A retry gets a new token; a repeated claim of processing work does not.
        """
        return self._execute_write(
            statement='''
                UPDATE event_tracker
                SET status = ?, last_query_time = ?, execution_id = ?
                WHERE event_id = ?
                    AND service = ?
                    AND current_delay_time = ?
                    AND status = ?
            ''',
            parameters=(
                STATUS_PROCESSING,
                last_query_time,
                uuid4().hex,
                event_id,
                service,
                current_delay_time,
                STATUS_PENDING,
            ),
        )

    def get_execution_id(self, event_id, service, current_delay_time):
        """Read the current internal attempt without changing scheduler metadata."""
        with self._lock:
            row = self.cursor.execute('''
                SELECT execution_id FROM event_tracker
                WHERE event_id = ? AND service = ? AND current_delay_time = ?
            ''', (event_id, service, current_delay_time)).fetchone()
            return row[0] if row is not None else None

    def finish_shakemap_execution(self, execution_id, *, success, diagnostic=None):
        """Apply an external outcome only to its still-owned processing row.

        Cleanup and re-registration can reuse the scheduled row's public fields.
        The execution token prevents a late external result from finalizing that
        replacement. Forced-restart failures remain failed for the same reason.
        """
        now = datetime.now(timezone.utc).isoformat(timespec='seconds')
        return self._execute_write(
            statement='''
                UPDATE event_tracker
                SET status = ?, last_query_time = ?, last_error = ?
                WHERE execution_id = ? AND status = ?
            ''',
            parameters=(
                STATUS_COMPLETED if success else STATUS_FAILED,
                now,
                None if success else diagnostic,
                execution_id,
                STATUS_PROCESSING,
            ),
        )

    def mark_event_completed(self, event_id, service, current_delay_time):
        """Complete one processing row and return the affected-row count."""
        now = datetime.now(timezone.utc).isoformat(timespec='seconds')
        return self._execute_write(
            statement='''
                UPDATE event_tracker
                SET status = ?, last_query_time = ?
                WHERE event_id = ?
                    AND service = ?
                    AND current_delay_time = ?
                    AND status = ?
            ''',
            parameters=(
                STATUS_COMPLETED,
                now,
                event_id,
                service,
                current_delay_time,
                STATUS_PROCESSING,
            ),
        )

    def increment_processing_retry_count(
        self,
        event_id,
        service,
        current_delay_time,
    ):
        """Increment a processing row and return its newly committed count."""
        with self._lock:
            try:
                self.cursor.execute('''
                    UPDATE event_tracker
                    SET retry_count = COALESCE(retry_count, 0) + 1
                    WHERE event_id = ?
                        AND service = ?
                        AND current_delay_time = ?
                        AND status = ?
                ''', (
                    event_id,
                    service,
                    current_delay_time,
                    STATUS_PROCESSING,
                ))
                if self.cursor.rowcount == 0:
                    self.conn.commit()
                    return None

                # Read the value before committing so the increment and value
                # returned to orchestration describe the same transaction.
                self.cursor.execute('''
                    SELECT retry_count
                    FROM event_tracker
                    WHERE event_id = ?
                        AND service = ?
                        AND current_delay_time = ?
                        AND status = ?
                ''', (
                    event_id,
                    service,
                    current_delay_time,
                    STATUS_PROCESSING,
                ))
                row = self.cursor.fetchone()
                if row is None:
                    raise RuntimeError(
                        "Incremented processing row could not be read back"
                    )
                updated_count = row[0]
                self.conn.commit()
                return updated_count
            except Exception as operation_error:
                try:
                    self.conn.rollback()
                except Exception as rollback_error:
                    raise operation_error from rollback_error
                raise

    def mark_event_pending_for_retry(
        self,
        event_id,
        service,
        current_delay_time,
        last_error,
        next_query_time,
    ):
        """Return one processing row to pending at an explicit retry time."""
        return self._execute_write(
            statement='''
                UPDATE event_tracker
                SET status = ?, last_error = ?, next_query_time = ?
                WHERE event_id = ?
                    AND service = ?
                    AND current_delay_time = ?
                    AND status = ?
            ''',
            parameters=(
                STATUS_PENDING,
                last_error,
                next_query_time,
                event_id,
                service,
                current_delay_time,
                STATUS_PROCESSING,
            ),
        )

    def mark_event_failed(
        self,
        event_id,
        service,
        current_delay_time,
        last_error,
        last_query_time,
    ):
        """Fail one processing row without consuming pending catch-up work."""
        return self._execute_write(
            statement='''
                UPDATE event_tracker
                SET status = ?, last_error = ?, last_query_time = ?
                WHERE event_id = ?
                    AND service = ?
                    AND current_delay_time = ?
                    AND status = ?
            ''',
            parameters=(
                STATUS_FAILED,
                last_error,
                last_query_time,
                event_id,
                service,
                current_delay_time,
                STATUS_PROCESSING,
            ),
        )

    def fail_abandoned_processing(self, last_error, last_query_time):
        """Fail every row left processing by an earlier local runtime."""
        return self._execute_write(
            statement='''
                UPDATE event_tracker
                SET status = ?, last_error = ?, last_query_time = ?
                WHERE status = ?
            ''',
            parameters=(
                STATUS_FAILED,
                last_error,
                last_query_time,
                STATUS_PROCESSING,
            ),
        )

    def cleanup_terminal_events(self):
        """Delete every row belonging to fully terminal event groups."""
        # Scheduled rows are also the durable registration identity. The
        # grouping boundary must therefore be the whole event, not one service
        # or delay stage. A NULL or unfamiliar status falls into ELSE and
        # protects every row for that event from deletion.
        return self._execute_write(
            statement='''
                DELETE FROM event_tracker
                WHERE event_id IN (
                    SELECT event_id
                    FROM event_tracker
                    GROUP BY event_id
                    HAVING SUM(
                        CASE
                            WHEN status IN (?, ?, ?) THEN 0
                            ELSE 1
                        END
                    ) = 0
                )
            ''',
            parameters=(
                STATUS_COMPLETED,
                STATUS_FAILED,
                STATUS_INCOMPLETE,
            ),
        )

    def close(self):
        """Close the database connection."""
        with self._lock:
            self.conn.close()

    def get_event_meta(self, event_id, service, current_delay_time):
        """Return the stored metadata used by scheduler execution paths."""
        selected_columns = ", ".join(self.SCHEDULER_METADATA_FIELDS)
        with self._lock:
            self.cursor.execute(f'''
            SELECT {selected_columns}
            FROM event_tracker
            WHERE event_id = ? AND service = ? AND current_delay_time = ?
            ''', (event_id, service, current_delay_time))
            row = self.cursor.fetchone()
            if row:
                return dict(zip(self.SCHEDULER_METADATA_FIELDS, row))
            return None
