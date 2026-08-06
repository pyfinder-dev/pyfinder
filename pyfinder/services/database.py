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
import hashlib
from datetime import datetime, timezone


STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_COMPLETED = "completed"
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
        "expiration_time",
        "current_delay_time",
        "next_delay_time",
        "emsc_alert_json",
        "last_data_snapshot",
    )
    _MUTABLE_EVENT_FIELDS = frozenset({
        "status",
        "last_query_time",
        "last_error",
        "retry_count",
        "next_query_time",
    })

    def __init__(self, db_path="event_update_follow_up.db"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.cursor = self.conn.cursor()
        self._enable_wal()
        self._create_table()

    def _enable_wal(self):
        """Enable Write-Ahead Logging (WAL) mode for better concurrency."""
        self.cursor.execute('PRAGMA journal_mode=WAL;')

    def _create_table(self):
        """Create the event tracking table if it doesn't exist."""
        with self._lock:
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
                PRIMARY KEY (event_id, service, current_delay_time)
            )
            ''')
            self.conn.commit()

    def calculate_hash(self, data):
        """Calculate a hash of the provided data for change detection."""
        return hashlib.sha256(data.encode('utf-8')).hexdigest()

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
            next_query_time, expiration_time=None, current_delay_time=None,
            next_delay_time=None, emsc_alert_json=None):
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
                    expiration_time,
                    current_delay_time, next_delay_time, emsc_alert_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                expiration_time,
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

    def mark_event_completed(self, event_id, service, current_delay_time):
        """Mark an event as completed with timestamp."""
        now = datetime.now(timezone.utc).isoformat(timespec='seconds')
        self._execute_write(
            statement='''
                UPDATE event_tracker
                SET status = ?, last_query_time = ?
                WHERE event_id = ? AND service = ? AND current_delay_time = ?
            ''',
            parameters=(
                STATUS_COMPLETED,
                now,
                event_id,
                service,
                current_delay_time,
            ),
        )

    def cleanup_expired_events(self):
        """Remove or mark expired events as inactive."""
        now = datetime.now(timezone.utc).isoformat(timespec='seconds')
        with self._lock:
            self.cursor.execute('''
            DELETE FROM event_tracker
            WHERE expiration_time <= ?
            ''', (now,))
            self.conn.commit()

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


    def query_by_priority(self, min_priority=1):
        """Get events with priority greater than or equal to a given value."""
        with self._lock:
            self.cursor.execute('''
                SELECT event_id, service, priority, next_query_time
                FROM event_tracker
                WHERE priority >= ?
                ORDER BY priority DESC, next_query_time ASC
            ''', (min_priority,))
            return self.cursor.fetchall()

    def _update_event_fields(self, event_id, service, current_delay_time, **kwargs):
        """
        Update the narrow lifecycle fields still required by the scheduler.
        """
        invalid_fields = set(kwargs) - self._MUTABLE_EVENT_FIELDS
        if invalid_fields:
            raise ValueError(
                "Unsupported mutable event fields: {0}".format(
                    ", ".join(sorted(invalid_fields))
                )
            )

        columns = []
        values = []
        for key, value in kwargs.items():
            if value is not None:
                columns.append(f"{key} = ?")
                values.append(value)
        if not columns:
            return
        values.extend([event_id, service, current_delay_time])
        self._execute_write(
            statement=f'''
                UPDATE event_tracker
                SET {", ".join(columns)}
                WHERE event_id = ? AND service = ? AND current_delay_time = ?
            ''',
            parameters=values,
        )
