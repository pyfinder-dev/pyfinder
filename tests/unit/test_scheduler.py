"""Deterministic tests for scheduled-item lifecycle orchestration."""

import ast
import builtins
from datetime import datetime, timezone
import inspect
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

from pyfinder.eventcontext import EventContext
from pyfinder.finderutils import FinderSolution
from pyfinder.services import eventtracker as eventtracker_module
from pyfinder.services import database as database_module
from pyfinder.services import scheduler as scheduler_module
from pyfinder.services.eventtracker import EventTracker
from pyfinder.services.querypolicy import RRSMQueryPolicy
from pyfinder.services.scheduler import (
    FollowUpScheduler,
    SchedulerLifecycleError,
)


EVENT_ID = "event-1"
SERVICE = "RRSM"
DELAY = 5


def usable_solution(event_id=EVENT_ID):
    """Return the scheduler's accepted domain result type."""
    return FinderSolution(event_id=event_id)


class ControlledFuture:
    """Small future whose completion is controlled without worker threads."""

    def __init__(self):
        self._done = False
        self._result = None
        self._error = None
        self._callbacks = []
        self.result_calls = 0

    @property
    def done(self):
        return self._done

    def add_done_callback(self, callback):
        self._callbacks.append(callback)
        if self._done:
            callback(self)

    def set_result(self, result):
        self._result = result
        self._done = True
        for callback in tuple(self._callbacks):
            callback(self)

    def set_exception(self, error):
        self._error = error
        self._done = True
        for callback in tuple(self._callbacks):
            callback(self)

    def result(self):
        self.result_calls += 1
        if not self._done:
            raise AssertionError("Future result was requested before completion")
        if self._error is not None:
            raise self._error
        return self._result


class ImmediateExecutor:
    """Execute submitted work synchronously and expose it through a future."""

    def __init__(self):
        self.futures = []
        self.shutdown_calls = []

    def submit(self, function, *args):
        future = ControlledFuture()
        self.futures.append(future)
        try:
            future.set_result(function(*args))
        except BaseException as error:
            future.set_exception(error)
        return future

    def shutdown(self, wait):
        self.shutdown_calls.append(wait)


class DeferredExecutor:
    """Retain submitted calls until a test explicitly completes their future."""

    def __init__(self):
        self.submissions = []
        self.shutdown_calls = []

    def submit(self, function, *args):
        future = ControlledFuture()
        self.submissions.append((function, args, future))
        return future

    def shutdown(self, wait):
        self.shutdown_calls.append(wait)


class SubmissionFailureExecutor:
    """Reject every submission after the scheduler has assigned the row."""

    def submit(self, function, *args):
        raise RuntimeError("executor is unavailable")

    def shutdown(self, wait):
        pass


class ManagerFactory:
    """Construct fake managers with a controlled scheduler-visible outcome."""

    def __init__(self, result=None, error=None, before_run=None):
        self.result = result
        self.error = error
        self.before_run = before_run
        self.constructions = []
        self.run_event_ids = []

    def for_alert_context(
        self,
        *,
        options,
        metadata,
        event_context,
        context_diagnostic,
        configuration,
        logger,
        finder_configuration_name=None,
        finder_configuration=None,
    ):
        self.constructions.append(
            {
                "options": options,
                "metadata": metadata,
                "event_context": event_context,
                "context_diagnostic": context_diagnostic,
                "configuration": configuration,
                "logger": logger,
                "finder_configuration_name": finder_configuration_name,
                "finder_configuration": finder_configuration,
            }
        )
        return self

    def run(self, event_id):
        self.run_event_ids.append(event_id)
        if self.before_run is not None:
            self.before_run()
        if self.error is not None:
            raise self.error
        return self.result


class SelectorDouble:
    """Record scheduler selection calls and return one controlled decision."""

    def __init__(
        self,
        configuration_name="test-profile",
        configuration=None,
        error=None,
    ):
        self.configuration = (
            {"DATA_FOLDER": "test-data"}
            if configuration is None
            else configuration
        )
        self.decision = types.SimpleNamespace(
            configuration_name=configuration_name,
            configuration=self.configuration,
        )
        self.error = error
        self.resolve_calls = []

    def resolve(self, latitude, longitude):
        self.resolve_calls.append(
            {"latitude": latitude, "longitude": longitude}
        )
        if self.error is not None:
            raise self.error
        return self.decision


class FatalWorkerTermination(BaseException):
    """Represent an unexpected BaseException-derived worker termination."""


def event_metadata(
    delay=DELAY,
    next_delay=15,
    retry_count=0,
    latitude=46.5,
    longitude=7.5,
):
    """Return the smallest valid metadata shape used by the scheduler."""
    context = EventContext.from_alert_mapping(
        {
            "unid": EVENT_ID,
            "lat": latitude,
            "lon": longitude,
            "mag": 5.5,
            "depth": 10.0,
            "time": "2026-08-06T09:55:00Z",
            "magtype": "Mw",
        },
        scheduled_event_id=EVENT_ID,
    )
    return {
        EventTracker.Field.current_delay_time: delay,
        EventTracker.Field.next_delay_time: next_delay,
        EventTracker.Field.last_query_time: "2026-08-06T10:00:00+00:00",
        EventTracker.Field.retry_count: retry_count,
        EventTracker.Field.region: "TEST REGION",
        EventTracker.Field.emsc_latitude: latitude,
        EventTracker.Field.emsc_longitude: longitude,
        EventTracker.Field.event_context: context,
        EventTracker.Field.event_context_error: None,
    }


def make_tracker(metadata=None):
    """Return a tracker mock with successful narrow lifecycle defaults."""
    tracker = mock.Mock()
    tracker.get_due_events.return_value = [(EVENT_ID, SERVICE, DELAY)]
    tracker.mark_as_processing.return_value = 1
    tracker.get_event_meta.return_value = (
        event_metadata() if metadata is None else metadata
    )
    tracker.mark_completed.return_value = 1
    tracker.increment_retry_count.return_value = 1
    tracker.mark_for_retry.return_value = 1
    tracker.mark_failed.return_value = 1
    return tracker


def make_scheduler(
    tracker,
    executor,
    manager_factory=None,
    service_policies=None,
    finder_config_selector=None,
):
    """Build a scheduler without invoking unrelated production startup work."""
    instance = FollowUpScheduler.__new__(FollowUpScheduler)
    instance.logger = mock.Mock()
    instance.configuration = {
        "finder-executable": {"output-root-folder": "/runtime/work"}
    }
    instance.tracker = tracker
    instance.service_policies = (
        {SERVICE: RRSMQueryPolicy()}
        if service_policies is None
        else service_policies
    )
    instance._finder_manager_class = manager_factory or ManagerFactory(
        result=usable_solution()
    )
    instance.finder_config_selector = (
        SelectorDouble()
        if finder_config_selector is None
        else finder_config_selector
    )
    instance._state_lock = threading.RLock()
    instance._shutdown_lock = threading.Lock()
    instance._future_condition = threading.Condition()
    instance._submitted_futures = {}
    instance._futures_being_observed = set()
    instance._accepting_work = True
    instance._drain_complete = False
    instance._shutdown_complete = False
    instance.executor = executor
    return instance


class SchedulerDispatchTests(unittest.TestCase):
    def test_successful_conditional_assignment_dispatches_with_named_calls(self):
        tracker = make_tracker()
        executor = DeferredExecutor()
        scheduler = make_scheduler(tracker=tracker, executor=executor)

        scheduler.run_once()

        tracker.get_due_events.assert_called_once_with(service=None)
        tracker.mark_as_processing.assert_called_once_with(
            event_id=EVENT_ID,
            service=SERVICE,
            current_delay_time=DELAY,
        )
        tracker.get_event_meta.assert_called_once_with(
            event_id=EVENT_ID,
            service=SERVICE,
            current_delay_time=DELAY,
        )
        self.assertEqual(len(executor.submissions), 1)

    def test_assignment_result_zero_skips_metadata_and_dispatch(self):
        tracker = make_tracker()
        tracker.mark_as_processing.return_value = 0
        executor = DeferredExecutor()
        scheduler = make_scheduler(tracker=tracker, executor=executor)

        scheduler.run_once()

        tracker.get_event_meta.assert_not_called()
        tracker.mark_failed.assert_not_called()
        self.assertEqual(executor.submissions, [])

    def test_assignment_invalid_affected_count_remains_observable(self):
        tracker = make_tracker()
        tracker.mark_as_processing.return_value = None
        scheduler = make_scheduler(tracker, DeferredExecutor())

        with self.assertRaisesRegex(
            SchedulerLifecycleError,
            "pending-to-processing assignment changed None rows",
        ):
            scheduler.run_once()

    def test_missing_metadata_becomes_terminal_failed(self):
        tracker = make_tracker()
        tracker.get_event_meta.return_value = None
        executor = DeferredExecutor()
        scheduler = make_scheduler(tracker, executor)

        scheduler.run_once()

        tracker.mark_failed.assert_called_once()
        self.assertEqual(
            tracker.mark_failed.call_args.kwargs["event_id"],
            EVENT_ID,
        )
        self.assertIn(
            "metadata was unavailable",
            tracker.mark_failed.call_args.kwargs["error_message"],
        )
        self.assertEqual(executor.submissions, [])

    def test_missing_policy_becomes_terminal_failed(self):
        tracker = make_tracker()
        executor = DeferredExecutor()
        scheduler = make_scheduler(
            tracker,
            executor,
            service_policies={},
        )

        scheduler.run_once()

        tracker.mark_failed.assert_called_once()
        self.assertIn(
            "No configured scheduler policy",
            tracker.mark_failed.call_args.kwargs["error_message"],
        )
        self.assertEqual(executor.submissions, [])

    def test_failed_validation_transition_is_not_reported_as_success(self):
        tracker = make_tracker()
        tracker.get_event_meta.return_value = None
        tracker.mark_failed.return_value = 0
        scheduler = make_scheduler(tracker, DeferredExecutor())

        with self.assertRaisesRegex(
            SchedulerLifecycleError,
            "processing-to-failed transition changed 0 rows",
        ):
            scheduler.run_once()

        self.assertFalse(
            any(
                "was marked failed" in str(call)
                for call in scheduler.logger.error.call_args_list
            )
        )

    def test_submission_failure_terminally_fails_assigned_row(self):
        tracker = make_tracker()
        scheduler = make_scheduler(tracker, SubmissionFailureExecutor())

        scheduler.run_once()

        tracker.mark_failed.assert_called_once()
        self.assertIn(
            "Executor submission failed",
            tracker.mark_failed.call_args.kwargs["error_message"],
        )
        self.assertEqual(scheduler._submitted_futures, {})


class SchedulerExecutionTests(unittest.TestCase):
    def test_persisted_context_survives_scheduler_manager_handoff(self):
        temporary_directory = tempfile.TemporaryDirectory()
        stored_tracker = EventTracker(
            temporary_directory.name + "/events.db",
            logger=mock.Mock(),
        )
        alert = {
            "unid": EVENT_ID,
            "lat": "46.25",
            "lon": "7.75",
            "mag": "5.8",
            "depth": "11.5",
            "time": "2026-08-10T08:15:30.250000Z",
            "magtype": "Mw",
            "flynn_region": "TEST REGION",
        }
        try:
            stored_tracker.register_new_schedule(
                event_id=EVENT_ID,
                service=SERVICE,
                origin_time="2026-08-10T08:15:30+00:00",
                last_update_time="2026-08-10T08:16:00+00:00",
                current_delay_time=DELAY,
                next_delay_time=15,
                next_query_time="2026-08-10T09:00:00+00:00",
                emsc_alert_json=json.dumps(alert),
            )
            metadata = stored_tracker.get_event_meta(
                EVENT_ID,
                SERVICE,
                DELAY,
            )
        finally:
            stored_tracker.close()
            temporary_directory.cleanup()

        tracker = make_tracker(metadata=metadata)
        manager = ManagerFactory(result=usable_solution())
        selector = SelectorDouble()
        scheduler = make_scheduler(
            tracker,
            ImmediateExecutor(),
            manager,
            finder_config_selector=selector,
        )

        scheduler.run_once()

        context = manager.constructions[0]["event_context"]
        self.assertIs(context, metadata[EventTracker.Field.event_context])
        self.assertEqual(context.get_event_id(), EVENT_ID)
        self.assertEqual(context.get_latitude(), 46.25)
        self.assertEqual(context.get_longitude(), 7.75)
        self.assertEqual(context.get_magnitude(), 5.8)
        self.assertEqual(context.get_depth(), 11.5)
        self.assertEqual(context.get_origin_time(), alert["time"])
        self.assertEqual(context.get_magnitude_type(), "Mw")
        self.assertEqual(
            selector.resolve_calls,
            [{"latitude": 46.25, "longitude": 7.75}],
        )

    def test_selection_uses_only_emsc_coordinates_and_reaches_manager(self):
        metadata = event_metadata(latitude="46.25", longitude="7.75")
        metadata[EventTracker.Field.region] = "FLYNN REGION IS NOT A COORDINATE"
        metadata[EventTracker.Field.emsc_latitude] = -10.0
        metadata[EventTracker.Field.emsc_longitude] = 120.0
        tracker = make_tracker(metadata=metadata)
        selected_configuration = {
            "DATA_FOLDER": "selected-data",
            "MODEL": {"name": "regional"},
        }
        selector = SelectorDouble(
            configuration_name="switzerland-alpine",
            configuration=selected_configuration,
        )
        manager = ManagerFactory(result=usable_solution())
        scheduler = make_scheduler(
            tracker,
            ImmediateExecutor(),
            manager,
            finder_config_selector=selector,
        )

        scheduler.run_once()

        self.assertEqual(
            selector.resolve_calls,
            [{"latitude": 46.25, "longitude": 7.75}],
        )
        self.assertEqual(len(manager.constructions), 1)
        construction = manager.constructions[0]
        self.assertIs(
            construction["event_context"],
            metadata[EventTracker.Field.event_context],
        )
        self.assertEqual(
            construction["finder_configuration_name"],
            "switzerland-alpine",
        )
        self.assertIs(
            construction["finder_configuration"],
            selected_configuration,
        )
        self.assertIs(construction["configuration"], scheduler.configuration)
        self.assertIs(construction["logger"], scheduler.logger)
        self.assertEqual(construction["metadata"]["current_delay"], DELAY)
        self.assertEqual(manager.run_event_ids, [EVENT_ID])
        tracker.mark_completed.assert_called_once()

    def test_unusable_context_skips_selector_and_reaches_alert_failure_entry(self):
        metadata = event_metadata()
        metadata[EventTracker.Field.event_context] = None
        metadata[EventTracker.Field.event_context_error] = "invalid origin time"
        tracker = make_tracker(metadata=metadata)
        selector = SelectorDouble()
        manager = ManagerFactory(result=None)
        scheduler = make_scheduler(
            tracker,
            ImmediateExecutor(),
            manager,
            finder_config_selector=selector,
        )

        scheduler.run_once()

        self.assertEqual(selector.resolve_calls, [])
        self.assertEqual(len(manager.constructions), 1)
        construction = manager.constructions[0]
        self.assertIsNone(construction["event_context"])
        self.assertEqual(
            construction["context_diagnostic"],
            "invalid origin time",
        )
        self.assertEqual(manager.run_event_ids, [EVENT_ID])
        tracker.increment_retry_count.assert_called_once()
        tracker.mark_completed.assert_not_called()

    def test_normal_global_fallback_runs_one_manager_and_completes(self):
        tracker = make_tracker()
        global_configuration = {"DATA_FOLDER": "global-data"}
        selector = SelectorDouble(
            configuration_name="global",
            configuration=global_configuration,
        )
        manager = ManagerFactory(result=usable_solution())
        scheduler = make_scheduler(
            tracker,
            ImmediateExecutor(),
            manager,
            finder_config_selector=selector,
        )

        scheduler.run_once()

        self.assertEqual(len(selector.resolve_calls), 1)
        self.assertEqual(len(manager.constructions), 1)
        self.assertIs(
            manager.constructions[0]["finder_configuration"],
            global_configuration,
        )
        self.assertEqual(manager.run_event_ids, [EVENT_ID])
        tracker.mark_completed.assert_called_once_with(
            event_id=EVENT_ID,
            service=SERVICE,
            current_delay_time=DELAY,
        )
        tracker.increment_retry_count.assert_not_called()

    def test_unexpected_selector_error_uses_existing_retry_lifecycle(self):
        tracker = make_tracker()
        selector = SelectorDouble(error=RuntimeError("selector failed"))
        manager = ManagerFactory(result=usable_solution())
        scheduler = make_scheduler(
            tracker,
            ImmediateExecutor(),
            manager,
            finder_config_selector=selector,
        )

        scheduler.run_once()

        self.assertEqual(len(selector.resolve_calls), 1)
        self.assertEqual(manager.constructions, [])
        self.assertEqual(manager.run_event_ids, [])
        tracker.increment_retry_count.assert_called_once_with(
            event_id=EVENT_ID,
            service=SERVICE,
            current_delay_time=DELAY,
        )
        tracker.mark_for_retry.assert_called_once()
        self.assertIn(
            "selector failed",
            tracker.mark_for_retry.call_args.kwargs["error_message"],
        )
        tracker.mark_completed.assert_not_called()

    def test_one_injected_selector_is_reused_for_multiple_attempts(self):
        tracker = make_tracker()
        selector = SelectorDouble()
        manager = ManagerFactory(result=usable_solution())
        scheduler = make_scheduler(
            tracker,
            ImmediateExecutor(),
            manager,
            finder_config_selector=selector,
        )
        policy = RRSMQueryPolicy()

        scheduler._handle_event(
            event_id="event-1",
            service=SERVICE,
            current_delay_time=5,
            event_meta=event_metadata(latitude=45.0, longitude=6.0),
            policy=policy,
        )
        scheduler._handle_event(
            event_id="event-2",
            service=SERVICE,
            current_delay_time=15,
            event_meta=event_metadata(latitude=46.0, longitude=7.0),
            policy=policy,
        )

        self.assertIs(scheduler.finder_config_selector, selector)
        self.assertEqual(
            selector.resolve_calls,
            [
                {"latitude": 45.0, "longitude": 6.0},
                {"latitude": 46.0, "longitude": 7.0},
            ],
        )
        self.assertEqual(len(manager.constructions), 2)
        self.assertEqual(manager.run_event_ids, ["event-1", "event-2"])

    def test_final_stage_is_completed_only_after_manager_success(self):
        tracker = make_tracker(metadata=event_metadata(next_delay=None))

        def verify_not_precompleted():
            tracker.mark_completed.assert_not_called()

        manager = ManagerFactory(
            result=usable_solution(),
            before_run=verify_not_precompleted,
        )
        executor = ImmediateExecutor()
        scheduler = make_scheduler(tracker, executor, manager)

        scheduler.run_once()

        tracker.mark_completed.assert_called_once_with(
            event_id=EVENT_ID,
            service=SERVICE,
            current_delay_time=DELAY,
        )
        self.assertEqual(executor.futures[0].result_calls, 1)

    def test_success_completes_nonfinal_and_final_stages_after_execution(self):
        for next_delay in (15, None):
            with self.subTest(next_delay=next_delay):
                tracker = make_tracker(
                    metadata=event_metadata(next_delay=next_delay)
                )
                manager = ManagerFactory(result=usable_solution())
                scheduler = make_scheduler(
                    tracker,
                    ImmediateExecutor(),
                    manager,
                )

                scheduler.run_once()

                self.assertEqual(manager.run_event_ids, [EVENT_ID])
                tracker.mark_completed.assert_called_once_with(
                    event_id=EVENT_ID,
                    service=SERVICE,
                    current_delay_time=DELAY,
                )

    def test_manager_none_increments_once_and_returns_to_pending(self):
        tracker = make_tracker()
        manager = ManagerFactory(result=None)
        scheduler = make_scheduler(tracker, ImmediateExecutor(), manager)

        scheduler.run_once()

        tracker.increment_retry_count.assert_called_once_with(
            event_id=EVENT_ID,
            service=SERVICE,
            current_delay_time=DELAY,
        )
        tracker.mark_for_retry.assert_called_once()
        tracker.mark_completed.assert_not_called()

    def test_non_solution_results_use_the_failed_attempt_lifecycle(self):
        results = (
            Path("materialized-input"),
            {"solution": "not-a-domain-result"},
            True,
            False,
            "materialized-input",
            1,
            object(),
        )
        for result in results:
            with self.subTest(result_type=type(result).__name__):
                tracker = make_tracker()
                scheduler = make_scheduler(
                    tracker,
                    ImmediateExecutor(),
                    ManagerFactory(result=result),
                )

                scheduler.run_once()

                tracker.increment_retry_count.assert_called_once_with(
                    event_id=EVENT_ID,
                    service=SERVICE,
                    current_delay_time=DELAY,
                )
                tracker.mark_for_retry.assert_called_once()
                tracker.mark_completed.assert_not_called()
                self.assertIn(
                    "usable FinderSolution",
                    tracker.mark_for_retry.call_args.kwargs["error_message"],
                )

    def test_ordinary_manager_exception_increments_once_and_is_retried(self):
        tracker = make_tracker()
        manager = ManagerFactory(error=ValueError("ordinary manager failure"))
        scheduler = make_scheduler(tracker, ImmediateExecutor(), manager)

        scheduler.run_once()

        tracker.increment_retry_count.assert_called_once_with(
            event_id=EVENT_ID,
            service=SERVICE,
            current_delay_time=DELAY,
        )
        tracker.mark_for_retry.assert_called_once()
        tracker.mark_failed.assert_not_called()
        tracker.mark_completed.assert_not_called()

    def test_policy_receives_newly_persisted_retry_count(self):
        tracker = make_tracker(metadata=event_metadata(retry_count=0))
        tracker.increment_retry_count.return_value = 2
        received_metadata = []
        policy = mock.Mock()

        def record_policy_call(metadata):
            received_metadata.append(dict(metadata))
            return True

        policy.should_retry_on_failure.side_effect = record_policy_call
        scheduler = make_scheduler(
            tracker,
            ImmediateExecutor(),
            ManagerFactory(result=None),
            service_policies={SERVICE: policy},
        )

        scheduler.run_once()

        self.assertEqual(received_metadata[0][EventTracker.Field.retry_count], 2)
        tracker.mark_for_retry.assert_called_once()

    def test_count_three_is_terminal_even_if_policy_requests_retry(self):
        tracker = make_tracker()
        tracker.increment_retry_count.return_value = 3
        policy = mock.Mock()
        policy.should_retry_on_failure.return_value = True
        scheduler = make_scheduler(
            tracker,
            ImmediateExecutor(),
            ManagerFactory(result=None),
            service_policies={SERVICE: policy},
        )

        scheduler.run_once()

        tracker.mark_for_retry.assert_not_called()
        tracker.mark_failed.assert_called_once()
        self.assertIn(
            "Retry limit reached after 3 failed attempts",
            tracker.mark_failed.call_args.kwargs["error_message"],
        )

    def test_invalid_retry_increment_is_observed_and_terminally_finalized(self):
        tracker = make_tracker()
        tracker.increment_retry_count.return_value = None
        executor = ImmediateExecutor()
        scheduler = make_scheduler(
            tracker,
            executor,
            ManagerFactory(result=None),
        )

        scheduler.run_once()

        self.assertEqual(executor.futures[0].result_calls, 1)
        tracker.mark_for_retry.assert_not_called()
        tracker.mark_failed.assert_called_once()
        self.assertTrue(
            any(
                "invalid persisted count" in str(call)
                for call in scheduler.logger.error.call_args_list
            )
        )

    def test_unexpected_worker_termination_is_observed_and_failed(self):
        tracker = make_tracker()
        executor = ImmediateExecutor()
        scheduler = make_scheduler(
            tracker,
            executor,
            ManagerFactory(error=FatalWorkerTermination("worker stopped")),
        )

        scheduler.run_once()

        self.assertEqual(executor.futures[0].result_calls, 1)
        tracker.increment_retry_count.assert_not_called()
        tracker.mark_failed.assert_called_once()
        self.assertIn(
            "FatalWorkerTermination",
            tracker.mark_failed.call_args.kwargs["error_message"],
        )

    def test_completion_affected_count_zero_is_observed(self):
        tracker = make_tracker()
        tracker.mark_completed.return_value = 0
        executor = ImmediateExecutor()
        scheduler = make_scheduler(
            tracker,
            executor,
            ManagerFactory(result=usable_solution()),
        )

        scheduler.run_once()

        self.assertEqual(executor.futures[0].result_calls, 1)
        tracker.mark_failed.assert_called_once()
        self.assertTrue(
            any(
                "processing-to-completed transition changed 0 rows" in str(call)
                for call in scheduler.logger.error.call_args_list
            )
        )

    def test_worker_and_terminal_persistence_failures_are_both_reported(self):
        tracker = make_tracker()
        tracker.mark_failed.side_effect = OSError("database unavailable")
        scheduler = make_scheduler(
            tracker,
            ImmediateExecutor(),
            ManagerFactory(error=FatalWorkerTermination("worker stopped")),
        )

        scheduler.run_once()

        error_calls = [str(call) for call in scheduler.logger.error.call_args_list]
        self.assertTrue(any("FatalWorkerTermination" in call for call in error_calls))
        self.assertTrue(any("database unavailable" in call for call in error_calls))


class SchedulerRetryPersistenceTests(unittest.TestCase):
    class FixedDateTime(datetime):
        fixed_now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return cls.fixed_now.replace(tzinfo=None)
            return cls.fixed_now.astimezone(tz)

    def _run_failed_attempt(self, starting_count):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        tracker = EventTracker(
            str(Path(temporary_directory.name) / "scheduler.sqlite")
        )
        self.addCleanup(tracker.close)
        tracker.register_new_schedule(
            event_id=EVENT_ID,
            service=SERVICE,
            origin_time="2026-08-06T10:00:00+00:00",
            last_update_time="2026-08-06T10:01:00+00:00",
            current_delay_time=DELAY,
            next_delay_time=15,
            next_query_time="2000-01-01T00:00:00+00:00",
        )
        if starting_count:
            with sqlite3.connect(
                Path(temporary_directory.name) / "scheduler.sqlite"
            ) as connection:
                connection.execute(
                    """
                    UPDATE event_tracker
                    SET retry_count = ?
                    WHERE event_id = ?
                        AND service = ?
                        AND current_delay_time = ?
                    """,
                    (starting_count, EVENT_ID, SERVICE, DELAY),
                )

        instance = make_scheduler(
            tracker,
            ImmediateExecutor(),
            ManagerFactory(result=None),
        )
        with mock.patch.object(
            eventtracker_module,
            "datetime",
            self.FixedDateTime,
        ):
            instance.run_once()
        return tracker.get_event_meta(
            event_id=EVENT_ID,
            service=SERVICE,
            current_delay_time=DELAY,
        )

    def test_initial_failure_retries_exactly_ten_seconds_later(self):
        metadata = self._run_failed_attempt(starting_count=0)

        self.assertEqual(metadata[EventTracker.Field.retry_count], 1)
        self.assertEqual(metadata[EventTracker.Field.status], "pending")
        self.assertEqual(
            metadata[EventTracker.Field.next_query_time],
            "2026-08-06T12:00:10+00:00",
        )

    def test_first_retry_failure_retries_exactly_ten_seconds_later(self):
        metadata = self._run_failed_attempt(starting_count=1)

        self.assertEqual(metadata[EventTracker.Field.retry_count], 2)
        self.assertEqual(metadata[EventTracker.Field.status], "pending")
        self.assertEqual(
            metadata[EventTracker.Field.next_query_time],
            "2026-08-06T12:00:10+00:00",
        )

    def test_second_retry_failure_becomes_terminal_without_another_retry(self):
        metadata = self._run_failed_attempt(starting_count=2)

        self.assertEqual(metadata[EventTracker.Field.retry_count], 3)
        self.assertEqual(metadata[EventTracker.Field.status], "failed")
        self.assertNotEqual(
            metadata[EventTracker.Field.next_query_time],
            "2026-08-06T12:00:10+00:00",
        )


class SchedulerFutureAndShutdownTests(unittest.TestCase):
    def test_future_is_retained_until_normal_result_is_observed(self):
        tracker = make_tracker()
        executor = DeferredExecutor()
        scheduler = make_scheduler(tracker, executor)

        scheduler.run_once()
        future = executor.submissions[0][2]

        self.assertIn(future, scheduler._submitted_futures)
        self.assertEqual(future.result_calls, 0)
        future.set_result("worker finalized")
        self.assertNotIn(future, scheduler._submitted_futures)
        self.assertEqual(future.result_calls, 1)

    def test_shutdown_waits_for_worker_finalization_before_tracker_close(self):
        events = []
        tracker = make_tracker()
        tracker.mark_completed.side_effect = lambda **kwargs: (
            events.append("completed") or 1
        )
        tracker.close.side_effect = lambda: events.append("tracker_closed")

        class FinalizingExecutor(DeferredExecutor):
            def shutdown(self, wait):
                events.append(("executor_shutdown", wait))
                for function, args, future in self.submissions:
                    try:
                        future.set_result(function(*args))
                    except BaseException as error:
                        future.set_exception(error)
                events.append("executor_stopped")

        executor = FinalizingExecutor()
        scheduler = make_scheduler(
            tracker,
            executor,
            ManagerFactory(result=usable_solution()),
        )
        scheduler.run_once()

        scheduler.shutdown()
        scheduler.shutdown()

        self.assertEqual(
            events,
            [
                ("executor_shutdown", True),
                "completed",
                "executor_stopped",
                "tracker_closed",
            ],
        )
        tracker.close.assert_called_once_with()

    def test_drain_can_finish_before_the_owner_closes_persistence(self):
        tracker = make_tracker()
        executor = DeferredExecutor()
        scheduler = make_scheduler(tracker, executor)

        scheduler.stop_and_drain()

        self.assertEqual(executor.shutdown_calls, [True])
        tracker.close.assert_not_called()

        scheduler.close()
        scheduler.shutdown()

        tracker.close.assert_called_once_with()

    def test_same_thread_state_lock_reentry_preserves_shutdown_order(self):
        events = []
        tracker = make_tracker()
        tracker.mark_completed.side_effect = lambda **kwargs: (
            events.append("completed") or 1
        )
        tracker.close.side_effect = lambda: events.append("tracker_closed")

        class FinalizingExecutor(DeferredExecutor):
            def shutdown(self, wait):
                events.append(("executor_shutdown", wait))
                for function, args, future in self.submissions:
                    try:
                        future.set_result(function(*args))
                    except BaseException as error:
                        future.set_exception(error)
                events.append("executor_stopped")

        executor = FinalizingExecutor()
        logger = mock.Mock()
        logger.ok = mock.Mock()
        config_fetcher = types.ModuleType("pyfinder.utils.config_fetcher")
        config_fetcher.ensure_shakemap_config = mock.Mock(return_value=None)
        finder_manager = types.ModuleType("pyfinder.findermanager")
        finder_manager.FinDerManager = ManagerFactory

        with mock.patch.object(
            FollowUpScheduler,
            "_setup_file_logger",
            return_value=logger,
        ), mock.patch.object(
            scheduler_module,
            "ThreadPoolExecutor",
            return_value=executor,
        ), mock.patch.dict(
            sys.modules,
            {
                "pyfinder.findermanager": finder_manager,
                "pyfinder.utils.config_fetcher": config_fetcher,
            },
        ):
            instance = FollowUpScheduler(
                tracker=tracker,
                service_policies={SERVICE: RRSMQueryPolicy()},
                finder_config_selector=SelectorDouble(),
            )

        instance._finder_manager_class = ManagerFactory(
            result=usable_solution()
        )
        instance.run_once()
        future = executor.submissions[0][2]

        # Check the production-created lock before attempting re-entry so a
        # regression to a non-reentrant lock fails instead of hanging the test.
        self.assertIsInstance(instance._state_lock, type(threading.RLock()))
        with instance._state_lock:
            instance.shutdown()

        self.assertEqual(future.result_calls, 1)
        self.assertEqual(
            events,
            [
                ("executor_shutdown", True),
                "completed",
                "executor_stopped",
                "tracker_closed",
            ],
        )
        tracker.close.assert_called_once_with()


class SchedulerStartupTests(unittest.TestCase):
    def _constructor_patches(self, executor_constructor):
        logger = mock.Mock()
        logger.ok = mock.Mock()
        config_fetcher = types.ModuleType("pyfinder.utils.config_fetcher")
        config_fetcher.ensure_shakemap_config = mock.Mock(return_value=None)
        finder_manager = types.ModuleType("pyfinder.findermanager")
        finder_manager.FinDerManager = ManagerFactory
        return (
            mock.patch.object(
                FollowUpScheduler,
                "_setup_file_logger",
                return_value=logger,
            ),
            mock.patch.object(
                scheduler_module,
                "ThreadPoolExecutor",
                side_effect=executor_constructor,
            ),
            mock.patch.dict(
                sys.modules,
                {
                    "pyfinder.findermanager": finder_manager,
                    "pyfinder.utils.config_fetcher": config_fetcher,
                },
            ),
        )

    def test_injected_selector_is_reused_without_default_construction(self):
        tracker = mock.Mock()
        tracker.recover_abandoned_processing.return_value = 0
        executor = DeferredExecutor()
        selector = SelectorDouble()
        patches = self._constructor_patches(lambda max_workers: executor)

        with patches[0], patches[1], patches[2], mock.patch.object(
            scheduler_module,
            "build_default_selector",
            autospec=True,
        ) as selector_builder:
            instance = FollowUpScheduler(
                tracker=tracker,
                service_policies={SERVICE: RRSMQueryPolicy()},
                finder_config_selector=selector,
            )

        selector_builder.assert_not_called()
        self.assertIs(instance.finder_config_selector, selector)
        instance.shutdown()

    def test_constructor_does_not_download_local_shakemap_configuration(self):
        tracker = mock.Mock()
        tracker.recover_abandoned_processing.return_value = 0
        executor = DeferredExecutor()
        config_fetcher = types.ModuleType("pyfinder.utils.config_fetcher")
        config_fetcher.ensure_shakemap_config = mock.Mock()
        finder_manager = types.ModuleType("pyfinder.findermanager")
        finder_manager.FinDerManager = ManagerFactory

        with mock.patch.object(
            FollowUpScheduler,
            "_setup_file_logger",
            return_value=mock.Mock(),
        ), mock.patch.object(
            scheduler_module,
            "ThreadPoolExecutor",
            return_value=executor,
        ), mock.patch.dict(
            sys.modules,
            {
                "pyfinder.findermanager": finder_manager,
                "pyfinder.utils.config_fetcher": config_fetcher,
            },
        ):
            instance = FollowUpScheduler(
                tracker=tracker,
                service_policies={SERVICE: RRSMQueryPolicy()},
                finder_config_selector=SelectorDouble(),
            )

        config_fetcher.ensure_shakemap_config.assert_not_called()
        instance.shutdown()

    def test_standalone_builds_selector_before_all_operational_resources(self):
        events = []
        logger = mock.Mock()
        logger.ok = mock.Mock()
        policies = {SERVICE: RRSMQueryPolicy()}
        selector = SelectorDouble()
        tracker = mock.Mock()
        tracker.recover_abandoned_processing.side_effect = lambda: (
            events.append("recovery") or 0
        )
        executor = DeferredExecutor()
        finder_manager = types.ModuleType("pyfinder.findermanager")
        finder_manager.FinDerManager = ManagerFactory
        config_fetcher = types.ModuleType("pyfinder.utils.config_fetcher")
        config_fetcher.ensure_shakemap_config = mock.Mock(return_value=None)
        original_import = builtins.__import__

        def configure_logger():
            events.append("logger")
            return logger

        def build_policies():
            events.append("policies")
            return policies

        def build_selector(*, logger):
            events.append("selector")
            self.assertIs(logger, logger_instance)
            return selector

        def import_module(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "pyfinder.findermanager":
                events.append("manager_import")
            return original_import(name, globals, locals, fromlist, level)

        def construct_tracker(db_path):
            events.append("tracker")
            return tracker

        def construct_executor(max_workers):
            events.append(("executor", max_workers))
            return executor

        logger_instance = logger
        with mock.patch.object(
            FollowUpScheduler,
            "_setup_file_logger",
            side_effect=configure_logger,
        ), mock.patch.object(
            scheduler_module,
            "build_service_policies",
            side_effect=build_policies,
        ), mock.patch.object(
            scheduler_module,
            "build_default_selector",
            side_effect=build_selector,
        ) as selector_builder, mock.patch.object(
            scheduler_module,
            "EventTracker",
            side_effect=construct_tracker,
        ), mock.patch.object(
            scheduler_module,
            "ThreadPoolExecutor",
            side_effect=construct_executor,
        ), mock.patch.object(
            builtins,
            "__import__",
            side_effect=import_module,
        ), mock.patch.dict(
            sys.modules,
            {
                "pyfinder.findermanager": finder_manager,
                "pyfinder.utils.config_fetcher": config_fetcher,
            },
        ):
            instance = FollowUpScheduler(
                db_path="/runtime/state/scheduled_queries.sqlite3"
            )

        self.assertEqual(
            events,
            [
                "logger",
                "policies",
                "selector",
                "manager_import",
                "tracker",
                "recovery",
                ("executor", 10),
            ],
        )
        selector_builder.assert_called_once_with(logger=logger)
        self.assertIs(instance.finder_config_selector, selector)
        instance.shutdown()

    def test_standalone_global_error_aborts_before_operational_resources(self):
        events = []
        logger = mock.Mock()
        logger.ok = mock.Mock()
        error = scheduler_module.GlobalFinderConfigError("global unusable")
        tracker_constructor = mock.Mock()
        executor_constructor = mock.Mock()
        manager_imports = []
        original_import = builtins.__import__

        def configure_logger():
            events.append("logger")
            return logger

        def build_policies():
            events.append("policies")
            return {SERVICE: RRSMQueryPolicy()}

        def fail_selector(*, logger):
            events.append("selector")
            raise error

        def import_module(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "pyfinder.findermanager":
                manager_imports.append(name)
            return original_import(name, globals, locals, fromlist, level)

        with mock.patch.object(
            FollowUpScheduler,
            "_setup_file_logger",
            side_effect=configure_logger,
        ), mock.patch.object(
            scheduler_module,
            "build_service_policies",
            side_effect=build_policies,
        ), mock.patch.object(
            scheduler_module,
            "build_default_selector",
            side_effect=fail_selector,
        ), mock.patch.object(
            scheduler_module,
            "EventTracker",
            tracker_constructor,
        ), mock.patch.object(
            scheduler_module,
            "ThreadPoolExecutor",
            executor_constructor,
        ), mock.patch.object(
            builtins,
            "__import__",
            side_effect=import_module,
        ):
            with self.assertRaises(
                scheduler_module.GlobalFinderConfigError
            ) as raised:
                FollowUpScheduler()

        self.assertIs(raised.exception, error)
        self.assertEqual(events, ["logger", "policies", "selector"])
        self.assertEqual(manager_imports, [])
        tracker_constructor.assert_not_called()
        executor_constructor.assert_not_called()
        logger.critical.assert_called_once()
        self.assertIs(logger.critical.call_args.kwargs["exc_info"], True)

    def test_startup_recovery_precedes_executor_creation(self):
        events = []
        tracker = mock.Mock()
        tracker.recover_abandoned_processing.side_effect = lambda: (
            events.append("recovery") or 2
        )
        executor = DeferredExecutor()

        def construct_executor(max_workers):
            events.append(("executor", max_workers))
            return executor

        patches = self._constructor_patches(construct_executor)
        with patches[0], patches[1], patches[2]:
            instance = FollowUpScheduler(
                tracker=tracker,
                service_policies={SERVICE: RRSMQueryPolicy()},
                finder_config_selector=SelectorDouble(),
            )

        self.assertEqual(events, ["recovery", ("executor", 10)])
        tracker.recover_abandoned_processing.assert_called_once_with()
        instance.shutdown()

    def test_recovery_failure_aborts_before_executor_creation(self):
        tracker = mock.Mock()
        tracker.recover_abandoned_processing.side_effect = OSError(
            "recovery failed"
        )
        executor_constructor = mock.Mock()
        patches = self._constructor_patches(executor_constructor)

        with patches[0], patches[1], patches[2]:
            with self.assertRaisesRegex(OSError, "recovery failed"):
                FollowUpScheduler(
                    tracker=tracker,
                    service_policies={SERVICE: RRSMQueryPolicy()},
                    finder_config_selector=SelectorDouble(),
                )

        executor_constructor.assert_not_called()
        tracker.close.assert_not_called()

    def test_executor_failure_closes_scheduler_created_tracker(self):
        tracker = mock.Mock()
        tracker.recover_abandoned_processing.return_value = 0
        original_error = RuntimeError("executor construction failed")
        finder_manager = types.ModuleType("pyfinder.findermanager")
        finder_manager.FinDerManager = ManagerFactory

        with mock.patch.object(
            FollowUpScheduler,
            "_setup_file_logger",
            return_value=mock.Mock(),
        ), mock.patch.object(
            scheduler_module,
            "EventTracker",
            return_value=tracker,
        ), mock.patch.object(
            scheduler_module,
            "ThreadPoolExecutor",
            side_effect=original_error,
        ), mock.patch.dict(
            sys.modules,
            {"pyfinder.findermanager": finder_manager},
        ):
            with self.assertRaises(RuntimeError) as raised:
                FollowUpScheduler(
                    db_path="/runtime/state/scheduled_queries.sqlite3",
                    service_policies={SERVICE: RRSMQueryPolicy()},
                    finder_config_selector=SelectorDouble(),
                )

        self.assertIs(raised.exception, original_error)
        tracker.close.assert_called_once_with()


class SchedulerLoopTests(unittest.TestCase):
    def test_loop_propagates_the_original_iteration_failure(self):
        scheduler = make_scheduler(make_tracker(), DeferredExecutor())
        original_error = RuntimeError("scheduler iteration failed")
        scheduler.run_once = mock.Mock(side_effect=original_error)

        with self.assertRaises(RuntimeError) as raised:
            scheduler.run_forever(interval_seconds=0)

        self.assertIs(raised.exception, original_error)

    def test_requested_shutdown_stops_before_another_iteration(self):
        scheduler = make_scheduler(make_tracker(), DeferredExecutor())
        scheduler.run_once = mock.Mock()
        shutdown_event = threading.Event()
        shutdown_event.set()

        scheduler.run_forever(
            interval_seconds=0,
            shutdown_event=shutdown_event,
        )

        scheduler.run_once.assert_not_called()


class SchedulerBoundaryTests(unittest.TestCase):
    def test_domain_eventtracker_calls_use_named_arguments(self):
        source = inspect.getsource(scheduler_module)
        tree = ast.parse(source)
        domain_methods = {
            "get_due_events",
            "mark_as_processing",
            "get_event_meta",
            "mark_completed",
            "increment_retry_count",
            "mark_for_retry",
            "mark_failed",
        }
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Attribute)
            and isinstance(node.func.value.value, ast.Name)
            and node.func.value.value.id == "self"
            and node.func.value.attr == "tracker"
            and node.func.attr in domain_methods
        ]

        self.assertTrue(calls)
        for call in calls:
            self.assertEqual(
                call.args,
                [],
                msg=f"self.tracker.{call.func.attr} has positional arguments",
            )

    def test_scheduler_does_not_use_generic_event_field_mutation(self):
        source = inspect.getsource(scheduler_module)

        self.assertNotIn("db_update_event_fields", source)
        self.assertFalse(hasattr(EventTracker, "db_update_event_fields"))
        self.assertFalse(
            hasattr(database_module.ThreadSafeDB, "_update_event_fields")
        )


if __name__ == "__main__":
    unittest.main()
