"""Deterministic tests for scheduled-item lifecycle orchestration."""

import ast
from datetime import datetime, timezone
import inspect
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

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

    def __call__(self, options, metadata):
        self.constructions.append((options, metadata))
        return self

    def run(self, event_id):
        self.run_event_ids.append(event_id)
        if self.before_run is not None:
            self.before_run()
        if self.error is not None:
            raise self.error
        return self.result


class FatalWorkerTermination(BaseException):
    """Represent an unexpected BaseException-derived worker termination."""


def event_metadata(delay=DELAY, next_delay=15, retry_count=0):
    """Return the smallest valid metadata shape used by the scheduler."""
    return {
        EventTracker.Field.current_delay_time: delay,
        EventTracker.Field.next_delay_time: next_delay,
        EventTracker.Field.last_query_time: "2026-08-06T10:00:00+00:00",
        EventTracker.Field.retry_count: retry_count,
        EventTracker.Field.region: "TEST REGION",
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
):
    """Build a scheduler without invoking unrelated production startup work."""
    instance = FollowUpScheduler.__new__(FollowUpScheduler)
    instance.logger = mock.Mock()
    instance.tracker = tracker
    instance.service_policies = (
        {SERVICE: RRSMQueryPolicy()}
        if service_policies is None
        else service_policies
    )
    instance._finder_manager_class = manager_factory or ManagerFactory(
        result=object()
    )
    instance._state_lock = threading.RLock()
    instance._shutdown_lock = threading.Lock()
    instance._future_condition = threading.Condition()
    instance._submitted_futures = {}
    instance._futures_being_observed = set()
    instance._accepting_work = True
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
    def test_final_stage_is_completed_only_after_manager_success(self):
        tracker = make_tracker(metadata=event_metadata(next_delay=None))

        def verify_not_precompleted():
            tracker.mark_completed.assert_not_called()

        manager = ManagerFactory(
            result={"solution": "usable"},
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
                manager = ManagerFactory(result=object())
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

    def test_ordinary_manager_exception_increments_once(self):
        tracker = make_tracker()
        manager = ManagerFactory(error=ValueError("ordinary failure"))
        scheduler = make_scheduler(tracker, ImmediateExecutor(), manager)

        scheduler.run_once()

        tracker.increment_retry_count.assert_called_once()
        tracker.mark_for_retry.assert_called_once()
        tracker.mark_failed.assert_not_called()

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
            ManagerFactory(result=object()),
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
            ManagerFactory(result=object()),
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
            )

        instance._finder_manager_class = ManagerFactory(result=object())
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
                )

        executor_constructor.assert_not_called()


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
