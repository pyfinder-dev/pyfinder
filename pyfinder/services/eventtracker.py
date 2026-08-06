# -*- coding: utf-8 -*-
""" 
Module for the EventTracker class for encapsulating the database operations for 
event update status tracking and management. This class serves as a wrapper around
the ThreadSafeDB class to provide a relatively higher-level interface for managing 
events, including registering, updating, and querying.
"""

from pyfinder.services.database import ThreadSafeDB
from pyfinder.services.database import (
    STATUS_PENDING,
    STATUS_PROCESSING,
    STATUS_COMPLETED,
    STATUS_INCOMPLETE
)
from datetime import datetime, timedelta, timezone
import json
import logging
from pyfinder.utils.timeutils import parse_normalized_iso8601


class ScheduleRegistrationError(RuntimeError):
    """Report failed delay stages after all schedule inserts were attempted."""

    def __init__(self, event_id, service, successful_rows, failures):
        self.event_id = event_id
        self.service = service
        self.successful_rows = successful_rows
        self.failures = tuple(failures)
        self.failed_delays = tuple(delay for delay, _ in self.failures)
        super().__init__(
            "Schedule registration failed for event {0} and service {1}; "
            "{2} rows persisted and delay stages {3} failed".format(
                event_id,
                service,
                successful_rows,
                list(self.failed_delays),
            )
        )


class EventTracker:
    RESULT_REGISTERED = "registered"
    RESULT_REFRESHED = "refreshed"
    RESULT_NO_PENDING = "no_pending_rows"

    class Field:
        event_id = "event_id"
        service = "service"
        status = "status"
        origin_time = "origin_time"
        last_update_time = "last_update_time"
        last_query_time = "last_query_time"
        next_query_time = "next_query_time"
        current_delay_time = "current_delay_time"
        next_delay_time = "next_delay_time"
        retry_count = "retry_count"
        expiration_time = "expiration_time"
        priority = "priority"
        last_error = "last_error"
        last_data_hash = "last_data_hash"
        last_data_snapshot = "last_data_snapshot"
        emsc_alert_json = "emsc_alert_json"
        last_modified = "last_modified"
        region = "region"
        
    def __init__(self, db_path="event_update_follow_up.db", logger=None):
        self._db = ThreadSafeDB(db_path)
        self.logger = logger or logging.getLogger("pyfinder")

    def set_logger(self, logger):
        """Set a custom logger for the EventTracker."""
        if not isinstance(logger, logging.Logger):
            raise ValueError("Logger must be an instance of logging.Logger")
        self.logger = logger
        logger.info("EventTracker logger set successfully.")

    def get_due_events(self, service):
        """Fetch events that are due for querying for a given service."""
        return self._db.fetch_due_events(service=service)

    def mark_completed(self, event_id, service, current_delay_time):
        """Mark event as completed with timestamp."""
        self._db.mark_event_completed(
            event_id=event_id,
            service=service,
            current_delay_time=current_delay_time,
        )

    def mark_as_processing(self, event_id, service, current_delay_time):
        """Mark an event as currently being processed."""
        now = datetime.now(timezone.utc).isoformat(timespec='seconds')
        self.db_update_event_fields(
            event_id=event_id,
            service=service,
            current_delay_time=current_delay_time,
            **{
                self.Field.status: STATUS_PROCESSING,
                self.Field.last_query_time: now,
            },
        )

    def cleanup_expired(self):
        """Clean up expired events from the database."""
        self._db.cleanup_expired_events()

    def close(self):
        """Close database connection."""
        self._db.close()

    def db_update_event_fields(self, event_id, service, current_delay_time, **fields):
        """Update selected fields for an event (status, next_query_time, etc.)."""
        self._db._update_event_fields(
            event_id=event_id,
            service=service,
            current_delay_time=current_delay_time,
            **fields
        )

    def get_event_meta(self, event_id, service, current_delay_time):
        """Return scheduler-facing metadata with an EMSC-derived region."""
        stored_meta = self._db.get_event_meta(
            event_id=event_id,
            service=service,
            current_delay_time=current_delay_time,
        )
        if stored_meta is None:
            return None

        meta = dict(stored_meta)
        meta[self.Field.region] = None
        alert_json = meta.get(self.Field.emsc_alert_json)
        if alert_json:
            try:
                parsed = json.loads(alert_json)
                if isinstance(parsed, dict):
                    region = parsed.get("flynn_region")
                    if region is not None:
                        meta[self.Field.region] = str(region)
            except (TypeError, ValueError):
                pass
        return meta

    def mark_failed(self, event_id, service, current_delay_time, error_message):
        """Mark an event as failed and log the error message."""
        now = datetime.now(timezone.utc).isoformat(timespec='seconds')
        self.db_update_event_fields(
            event_id=event_id,
            service=service,
            current_delay_time=current_delay_time,
            **{
                self.Field.status: STATUS_INCOMPLETE,
                self.Field.last_error: error_message,
                self.Field.last_query_time: now,
            },
        )

    def increment_retry_count(self, event_id, service, current_delay_time):
        """Increment the retry count for a given event and service."""
        meta = self.get_event_meta(
            event_id=event_id,
            service=service,
            current_delay_time=current_delay_time,
        )
        if not meta:
            return
        retry = (meta.get(self.Field.retry_count) or 0) + 1
        self.db_update_event_fields(
            event_id=event_id,
            service=service,
            current_delay_time=current_delay_time,
            **{
                self.Field.retry_count: retry,
            },
        )

    def query_by_priority(self, min_priority=1):
        """Get events with priority greater than or equal to a given value."""
        return self._db.query_by_priority(min_priority)
        
    def register_new_schedule(
            self, event_id, service, origin_time, last_update_time,
            current_delay_time=None, next_delay_time=None,
            next_query_time=None, emsc_alert_json=None, expiration_days=5):
        """
        Register a new scheduled service update for a specific event.

        This method will attempt to insert a new row. If the row already exists,
        it will raise an exception and will NOT fallback to updating.
        """
        expiration_time = (datetime.now(timezone.utc) + timedelta(days=expiration_days)).isoformat(timespec='seconds')
        self._db.insert_scheduled_item(
            event_id=event_id,
            service=service,
            origin_time=origin_time,
            last_update_time=last_update_time,
            next_query_time=next_query_time,
            expiration_time=expiration_time,
            current_delay_time=current_delay_time,
            next_delay_time=next_delay_time,
            emsc_alert_json=emsc_alert_json,
        )

    def batch_register_from_policy(
        self, event_id, policy, origin_time, last_update_time,
        emsc_alert_json=None, expiration_days=5):
        """
        Register multiple scheduled service updates using a policy instance.

        The policy instance must define:
        - policy.service_name (str)
        - policy.QUERY_SCHEDULE_MINUTES (List[float]) in minutes

        Each delay will create a separate row entry with a calculated next_query_time.
        """
        now = datetime.now(timezone.utc)
        delays = policy.QUERY_SCHEDULE_MINUTES
        successful_rows = 0
        failures = []
        for i, delay in enumerate(delays):
            next_delay = delays[i + 1] if i + 1 < len(delays) else None
            next_query_time = (
                now + timedelta(minutes=delay)
            ).isoformat(timespec='seconds')
            try:
                self.register_new_schedule(
                    event_id=event_id,
                    service=policy.service_name,
                    origin_time=origin_time,
                    last_update_time=last_update_time,
                    current_delay_time=delay,
                    next_delay_time=next_delay,
                    next_query_time=next_query_time,
                    emsc_alert_json=emsc_alert_json,
                    expiration_days=expiration_days,
                )
                successful_rows += 1
            except Exception as error:
                failures.append((delay, error))

        if failures:
            failed_delays = [delay for delay, _ in failures]
            self.logger.error(
                "Schedule registration incomplete for event %s and service %s: "
                "%s rows persisted; failed delay stages: %s",
                event_id,
                policy.service_name,
                successful_rows,
                failed_delays,
            )
            registration_error = ScheduleRegistrationError(
                event_id=event_id,
                service=policy.service_name,
                successful_rows=successful_rows,
                failures=failures,
            )
            raise registration_error from failures[0][1]

        return successful_rows
    
    def refresh_metadata_after_emsc_update(
        self, event_id, service, new_last_update_time, origin_time=None, emsc_alert_json=None
    ):
        """
        Update EMSC metadata for all pending stages of an event and service.
        """
        now = datetime.now(timezone.utc).isoformat(timespec='seconds')
        return self._db.update_pending_emsc_metadata(
            event_id=event_id,
            service=service,
            origin_time=origin_time,
            last_update_time=new_last_update_time,
            emsc_alert_json=emsc_alert_json,
            last_modified=now,
        )

    def apply_emsc_alert(
        self,
        event_id,
        policy,
        origin_time,
        last_update_time,
        emsc_alert_json=None,
        expiration_days=5,
    ):
        """Register a new event or refresh its existing pending metadata."""
        service = policy.service_name
        if not self._db.event_service_exists(
            event_id=event_id,
            service=service,
        ):
            registered_rows = self.batch_register_from_policy(
                event_id=event_id,
                policy=policy,
                origin_time=origin_time,
                last_update_time=last_update_time,
                emsc_alert_json=emsc_alert_json,
                expiration_days=expiration_days,
            )
            self.logger.info(
                "Registered event %s for service %s with %s scheduled rows",
                event_id,
                service,
                registered_rows,
            )
            return self.RESULT_REGISTERED, registered_rows

        refreshed_rows = self.refresh_metadata_after_emsc_update(
            event_id=event_id,
            service=service,
            new_last_update_time=last_update_time,
            origin_time=origin_time,
            emsc_alert_json=emsc_alert_json,
        )
        if refreshed_rows:
            self.logger.info(
                "Refreshed EMSC metadata for %s pending rows of event %s and service %s",
                refreshed_rows,
                event_id,
                service,
            )
            return self.RESULT_REFRESHED, refreshed_rows

        self.logger.info(
            "Event %s and service %s exist with no pending rows to refresh",
            event_id,
            service,
        )
        return self.RESULT_NO_PENDING, 0

        
    def defer_event(self, event_id, service, current_delay_time, minutes=10):
        """Postpone the next query time by N minutes for a specific event and service."""
        meta = self.get_event_meta(
            event_id=event_id,
            service=service,
            current_delay_time=current_delay_time,
        )
        if not meta or not meta.get(self.Field.next_query_time):
            return  # Nothing to defer

        current_time = parse_normalized_iso8601(meta[self.Field.next_query_time])
        new_time = current_time + timedelta(minutes=minutes)

        self.db_update_event_fields(
            event_id=event_id,
            service=service,
            current_delay_time=current_delay_time,
            **{
                self.Field.next_query_time: new_time.isoformat(timespec='seconds'),
            },
        )
