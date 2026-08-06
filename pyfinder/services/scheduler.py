# -*- coding: utf-8 -*-
""" 
Main module for the automated FinDer execution via the FollowUpScheduler class, 
which manages the scheduling of follow-up queries. This class manages if another
data update is expected, executes the FinDerManager to process the event, and
handles the results.
"""
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
import threading

from pyfinder.services.eventtracker import EventTracker
from pyfinder.services.querypolicy import build_service_policies
from pyfinder.utils.customlogger import file_logger


class SchedulerLifecycleError(RuntimeError):
    """Report a scheduled-item transition that was not persisted as required."""


class FollowUpScheduler:
    """
    FollowUpScheduler is responsible for managing the scheduling of follow-up queries.
    It uses a thread pool to handle multiple events concurrently and logs the process.
    The scheduler checks for due events and processes them according to the defined 
    policies via dedicated policy instances supplied during construction.
    """

    def __init__(self, tracker: EventTracker=None, service_policies=None):
        # Create a logger for the FollowUpScheduler and its sub-tasks
        self.logger = self._setup_file_logger()
        self._welcome_message(self.logger)

        if service_policies is None:
            try:
                service_policies = build_service_policies()
            except Exception:
                self.logger.exception(
                    "Policy validation failed; aborting scheduler startup"
                )
                raise
        self.service_policies = service_policies

        # FinDer is needed only after startup policy validation has succeeded.
        from pyfinder.findermanager import FinDerManager

        self._finder_manager_class = FinDerManager

        # Initialize the EventTracker for managing event updates
        if tracker is None:
            tracker = EventTracker("event_update_follow_up.db")
        self.tracker = tracker
        self.tracker.set_logger(self.logger)
        self.logger.info("EventTracker initialized for the scheduler.")

        # Recovery must happen before the executor exists. Otherwise new due
        # work could start while rows abandoned by the previous process still
        # look like active local executions.
        recovered_rows = self.tracker.recover_abandoned_processing()
        self.logger.info(
            "Recovered %s abandoned processing rows during scheduler startup.",
            recovered_rows,
        )

        # Python signal handlers run on the main thread and can re-enter
        # shutdown() while run_once() already owns this coordination lock.
        # Reentrancy prevents that self-deadlock while still excluding a
        # different thread until active discovery and dispatch have finished.
        self._state_lock = threading.RLock()
        self._shutdown_lock = threading.Lock()
        self._future_condition = threading.Condition()
        self._submitted_futures = {}
        self._futures_being_observed = set()
        self._accepting_work = True
        self._shutdown_complete = False

        # Thread pool with up to 10 workers
        self.executor = ThreadPoolExecutor(max_workers=10)
        self.logger.info("ThreadPoolExecutor initialized for the scheduler.")
        self.logger.info("FollowUpScheduler initialization completed.")

        # Ensure the shakemap configuration is available
        try:
            from pyfinder.utils.config_fetcher import ensure_shakemap_config

            self.logger.info("Ensuring ShakeMap configuration is available...")
            ensure_shakemap_config()
            self.logger.info("ShakeMap configuration cloned successfully.")
        except Exception as e:
            self.logger.error(f"Failed to ensure ShakeMap configuration: {e}")
            
    @staticmethod
    def _welcome_message(logger):
        """ Print a welcome message to the console and log it. """
        
        logger.info("=========================================================")
        logger.info(" A new scheduler for event updates is being initialized. ")
        logger.info("... Testing logger functionality ...")
        logger.error("This is an error message for testing purposes.")
        logger.info("This is an info message for testing purposes.")
        logger.ok("This is an ok message for testing purposes.")
        logger.info("---------------------------------------------------------")
        logger.info("BEGIN: Init FollowUpScheduler")

    @staticmethod
    def _setup_file_logger():
        """ Set up a file logger for the FollowUpScheduler."""
        return file_logger(
            module_name="FollowUpScheduler",
            log_file="followupscheduler.log",
            rotate=True,
            overwrite=False
            )

    @staticmethod
    def _failure_diagnostic(prefix, error):
        """Build a useful diagnostic even for exceptions with empty messages."""
        detail = str(error) or "no exception message"
        return f"{prefix}: {type(error).__name__}: {detail}"

    def _require_transition(self, operation, affected_rows, identity):
        """Require the one known processing row to complete a transition."""
        if affected_rows != 1:
            event_id, service, current_delay_time = identity
            raise SchedulerLifecycleError(
                f"{operation} changed {affected_rows!r} rows for event "
                f"{event_id}, service {service}, delay {current_delay_time}; "
                "expected exactly 1"
            )

    def _terminally_fail_assigned_item(
        self,
        event_id,
        service,
        current_delay_time,
        diagnostic,
    ):
        """Fail one assigned item and verify that it was still processing."""
        identity = (event_id, service, current_delay_time)
        affected_rows = self.tracker.mark_failed(
            event_id=event_id,
            service=service,
            current_delay_time=current_delay_time,
            error_message=diagnostic,
        )
        self._require_transition("processing-to-failed transition", affected_rows, identity)
        self.logger.error(
            "Event %s, service %s, delay %s was marked failed: %s",
            event_id,
            service,
            current_delay_time,
            diagnostic,
        )

    def _record_failed_attempt(
        self,
        event_id,
        service,
        current_delay_time,
        event_meta,
        policy,
        diagnostic,
    ):
        """Persist one failed attempt and apply the current retry policy."""
        identity = (event_id, service, current_delay_time)
        updated_count = self.tracker.increment_retry_count(
            event_id=event_id,
            service=service,
            current_delay_time=current_delay_time,
        )
        if (
            isinstance(updated_count, bool)
            or not isinstance(updated_count, int)
            or updated_count < 1
        ):
            raise SchedulerLifecycleError(
                "Retry increment returned an invalid persisted count "
                f"{updated_count!r} for event {event_id}, service {service}, "
                f"delay {current_delay_time}"
            )

        updated_meta = dict(event_meta)
        updated_meta[EventTracker.Field.retry_count] = updated_count
        policy_requests_retry = policy.should_retry_on_failure(updated_meta)

        # The persisted count is the hard attempt boundary. A policy can stop
        # earlier, but it must never create a fourth execution attempt.
        if updated_count < 3 and policy_requests_retry:
            affected_rows = self.tracker.mark_for_retry(
                event_id=event_id,
                service=service,
                current_delay_time=current_delay_time,
                error_message=diagnostic,
            )
            self._require_transition(
                "processing-to-pending retry transition",
                affected_rows,
                identity,
            )
            self.logger.error(
                "Event %s, service %s, delay %s failed on attempt %s and "
                "will be retried: %s",
                event_id,
                service,
                current_delay_time,
                updated_count,
                diagnostic,
            )
            return

        if updated_count >= 3:
            terminal_diagnostic = (
                f"Retry limit reached after {updated_count} failed attempts. "
                f"Last failure: {diagnostic}"
            )
        else:
            terminal_diagnostic = (
                f"Retry policy rejected another attempt after {updated_count} "
                f"failed attempts. Last failure: {diagnostic}"
            )
        self._terminally_fail_assigned_item(
            event_id=event_id,
            service=service,
            current_delay_time=current_delay_time,
            diagnostic=terminal_diagnostic,
        )

    def _handle_event(
        self,
        event_id,
        service,
        current_delay_time,
        event_meta,
        policy,
    ):
        """Execute one assigned item and finalize its persisted lifecycle."""
        next_delay = event_meta.get(EventTracker.Field.next_delay_time)
        self.logger.info("Running FinDerManager for event %s.", event_id)

        finder_options = {
            "verbosity": "INFO",
            "log_file": None,
            "with_seiscomp": True,
            "event_id": event_id,
            "test": False,
            "use_library": False,
        }
        # The combined command line is retained only for the manager's current
        # logging boundary; the manager is still constructed with structured
        # options below.
        finder_options["command_line_args"] = " ".join(
            [
                f"--{key}={value}"
                for key, value in finder_options.items()
                if key != "command_line_args"
            ]
        )
        solution_metadata = {
            "last_query_time": str(
                event_meta.get(EventTracker.Field.last_query_time)
            ),
            "minutes_until_next_update": next_delay,
            "current_delay": current_delay_time,
            "region": event_meta.get(EventTracker.Field.region),
        }

        self.logger.info(
            "FinderManager will run for the scheduled delay for %s: %s minutes.",
            event_id,
            current_delay_time,
        )
        try:
            finder_manager = self._finder_manager_class(
                options=finder_options,
                metadata=solution_metadata,
            )
            finder_solution = finder_manager.run(event_id=event_id)
        except Exception as error:
            diagnostic = self._failure_diagnostic(
                "FinDerManager execution failed",
                error,
            )
            self.logger.error(
                "FinDerManager failed for event %s and service %s: %s",
                event_id,
                service,
                diagnostic,
                exc_info=True,
            )
            self._record_failed_attempt(
                event_id=event_id,
                service=service,
                current_delay_time=current_delay_time,
                event_meta=event_meta,
                policy=policy,
                diagnostic=diagnostic,
            )
            return None

        if finder_solution is None:
            diagnostic = "FinDerManager returned no scheduler-visible result"
            self._record_failed_attempt(
                event_id=event_id,
                service=service,
                current_delay_time=current_delay_time,
                event_meta=event_meta,
                policy=policy,
                diagnostic=diagnostic,
            )
            return None

        identity = (event_id, service, current_delay_time)
        affected_rows = self.tracker.mark_completed(
            event_id=event_id,
            service=service,
            current_delay_time=current_delay_time,
        )
        self._require_transition(
            "processing-to-completed transition",
            affected_rows,
            identity,
        )
        self.logger.info(
            "Event %s marked completed for scheduled delay %s minutes for "
            "service %s.",
            event_id,
            current_delay_time,
            service,
        )
        return finder_solution

    def _retain_future(self, future, identity):
        """Keep a submitted future reachable until its result is observed."""
        with self._future_condition:
            self._submitted_futures[future] = identity
        future.add_done_callback(self._observe_future)

    def _observe_future(self, future):
        """Observe a worker outcome and bound failure finalization to one try."""
        with self._future_condition:
            identity = self._submitted_futures.get(future)
            if identity is None or future in self._futures_being_observed:
                return
            self._futures_being_observed.add(future)

        event_id, service, current_delay_time = identity
        try:
            future.result()
        except BaseException as worker_error:
            diagnostic = self._failure_diagnostic(
                "Scheduler worker terminated before lifecycle finalization",
                worker_error,
            )
            self.logger.error(
                "Worker outcome failed for event %s, service %s, delay %s: %s",
                event_id,
                service,
                current_delay_time,
                diagnostic,
                exc_info=(
                    type(worker_error),
                    worker_error,
                    worker_error.__traceback__,
                ),
            )
            try:
                self._terminally_fail_assigned_item(
                    event_id=event_id,
                    service=service,
                    current_delay_time=current_delay_time,
                    diagnostic=diagnostic,
                )
            except BaseException as finalization_error:
                self.logger.error(
                    "Worker failure for event %s, service %s, delay %s was "
                    "observed, but terminal finalization also failed: %s: %s. "
                    "Original worker failure: %s: %s",
                    event_id,
                    service,
                    current_delay_time,
                    type(finalization_error).__name__,
                    str(finalization_error) or "no exception message",
                    type(worker_error).__name__,
                    str(worker_error) or "no exception message",
                    exc_info=(
                        type(finalization_error),
                        finalization_error,
                        finalization_error.__traceback__,
                    ),
                )
        finally:
            with self._future_condition:
                self._futures_being_observed.discard(future)
                self._submitted_futures.pop(future, None)
                self._future_condition.notify_all()

    def _drain_future_observations(self):
        """Observe any retained futures not handled by their done callbacks."""
        while True:
            with self._future_condition:
                if not self._submitted_futures:
                    return
                unobserved = [
                    future
                    for future in self._submitted_futures
                    if future not in self._futures_being_observed
                ]
                if not unobserved:
                    self._future_condition.wait()
                    continue
            for future in unobserved:
                self._observe_future(future)

    def shutdown(self):
        """ Shutdown the FollowUpScheduler and clean up resources. """
        with self._shutdown_lock:
            if self._shutdown_complete:
                return

            self.logger.info("Shutting down FollowUpScheduler.")
            with self._state_lock:
                self._accepting_work = False

            # ThreadPoolExecutor.shutdown(wait=True) stops new submissions and
            # does not return until running work and its done callbacks finish.
            # Persistence must remain open for both worker transitions and the
            # callback's bounded terminal-failure attempt.
            self.logger.info("Waiting for scheduler worker finalization.")
            self.executor.shutdown(wait=True)
            self._drain_future_observations()

            self.tracker.close()
            self._shutdown_complete = True
            self.logger.info("FollowUpScheduler shutdown complete.")

    def run_once(self):
        """ Run the scheduler once to check for due events and process them. """
        with self._state_lock:
            if not self._accepting_work:
                return

            due_events = self.tracker.get_due_events(service=None)
            if not due_events:
                return

            self.logger.info("Due events fetched: %s", len(due_events))
            self.logger.info(
                "Due events: %s",
                [(event[0], event[1]) for event in due_events],
            )

            for event_id, service, current_delay_time in due_events:
                identity = (event_id, service, current_delay_time)
                affected_rows = self.tracker.mark_as_processing(
                    event_id=event_id,
                    service=service,
                    current_delay_time=current_delay_time,
                )
                if affected_rows == 0:
                    self.logger.info(
                        "Event %s, service %s, delay %s was no longer pending; "
                        "dispatch was skipped.",
                        event_id,
                        service,
                        current_delay_time,
                    )
                    continue
                self._require_transition(
                    "pending-to-processing assignment",
                    affected_rows,
                    identity,
                )
                self.logger.info(
                    "Processing event %s for service %s at delay %s.",
                    event_id,
                    service,
                    current_delay_time,
                )

                event_meta = self.tracker.get_event_meta(
                    event_id=event_id,
                    service=service,
                    current_delay_time=current_delay_time,
                )
                if event_meta is None:
                    self._terminally_fail_assigned_item(
                        event_id=event_id,
                        service=service,
                        current_delay_time=current_delay_time,
                        diagnostic=(
                            "Assigned scheduled item metadata was unavailable "
                            "before execution"
                        ),
                    )
                    continue
                if not isinstance(event_meta, Mapping):
                    self._terminally_fail_assigned_item(
                        event_id=event_id,
                        service=service,
                        current_delay_time=current_delay_time,
                        diagnostic=(
                            "Assigned scheduled item metadata was not a mapping"
                        ),
                    )
                    continue

                stored_delay = event_meta.get(
                    EventTracker.Field.current_delay_time
                )
                if stored_delay is None or stored_delay != current_delay_time:
                    self._terminally_fail_assigned_item(
                        event_id=event_id,
                        service=service,
                        current_delay_time=current_delay_time,
                        diagnostic=(
                            "Assigned scheduled item metadata did not contain "
                            "the expected current delay"
                        ),
                    )
                    continue

                policy = self.service_policies.get(service)
                self.logger.info("Policy for service %s: %s", service, policy)
                if policy is None:
                    self._terminally_fail_assigned_item(
                        event_id=event_id,
                        service=service,
                        current_delay_time=current_delay_time,
                        diagnostic=(
                            f"No configured scheduler policy was available "
                            f"for service {service}"
                        ),
                    )
                    continue

                self.logger.info(
                    "Event %s will be evaluated for delay stage %s minutes.",
                    event_id,
                    current_delay_time,
                )
                try:
                    future = self.executor.submit(
                        self._handle_event,
                        event_id,
                        service,
                        current_delay_time,
                        dict(event_meta),
                        policy,
                    )
                except BaseException as submission_error:
                    diagnostic = self._failure_diagnostic(
                        "Executor submission failed after processing assignment",
                        submission_error,
                    )
                    self.logger.error(
                        "Submission failed for event %s, service %s, delay %s: %s",
                        event_id,
                        service,
                        current_delay_time,
                        diagnostic,
                        exc_info=(
                            type(submission_error),
                            submission_error,
                            submission_error.__traceback__,
                        ),
                    )
                    try:
                        self._terminally_fail_assigned_item(
                            event_id=event_id,
                            service=service,
                            current_delay_time=current_delay_time,
                            diagnostic=diagnostic,
                        )
                    except BaseException as finalization_error:
                        self.logger.error(
                            "Submission and terminal finalization both failed "
                            "for event %s, service %s, delay %s. Submission: "
                            "%s: %s. Finalization: %s: %s",
                            event_id,
                            service,
                            current_delay_time,
                            type(submission_error).__name__,
                            str(submission_error) or "no exception message",
                            type(finalization_error).__name__,
                            str(finalization_error) or "no exception message",
                            exc_info=(
                                type(finalization_error),
                                finalization_error,
                                finalization_error.__traceback__,
                            ),
                        )
                        raise finalization_error from submission_error
                    if not isinstance(submission_error, Exception):
                        raise
                    continue
                self._retain_future(future=future, identity=identity)

    def run_forever(self, interval_seconds=10):
        """ 
        Run the scheduler in an infinite loop, checking for due events 
        at regular intervals. 
        """
        import time
        self.logger.info(f"Scheduler running every {interval_seconds} seconds.")

        try:
            while True:
                with self._state_lock:
                    if not self._accepting_work:
                        break
                self.run_once()
                time.sleep(interval_seconds)
        except KeyboardInterrupt:
            self.shutdown()
        except Exception as e:
            self.logger.error(f"Scheduler encountered an error: {e}")
            self.shutdown()
