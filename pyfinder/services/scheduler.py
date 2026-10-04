# -*- coding: utf-8 -*-
""" 
Main module for the automated FinDer execution via the FollowUpScheduler class, 
which manages the scheduling of follow-up queries. This class manages if another
data update is expected, executes the FinDerManager to process the event, and
handles the results.
"""
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import partial
import logging
import threading

from pyfinder.eventcontext import EventContext
from pyfinder.finderconfigs import (
    GlobalFinderConfigError,
    build_default_selector,
)
from pyfinder.finderutils import FinderSolution
from pyfinder.services.eventtracker import EventTracker
from pyfinder.services.shakemap_inputs import ShakeMapCalculationBusy
from pyfinder.services.querypolicy import build_service_policies
from pyfinder.services.shakemap_diagnostics import (
    diagnostic as shakemap_diagnostic,
    format_diagnostic,
    regional_configuration_failure,
    local_failure_diagnostic,
    safe_text,
)


class SchedulerLifecycleError(RuntimeError):
    """Report a scheduled-item transition that was not persisted as required."""


class FollowUpScheduler:
    """
    FollowUpScheduler is responsible for managing the scheduling of follow-up queries.
    It uses a thread pool to handle multiple events concurrently and logs the process.
    The scheduler checks for due events and processes them according to the defined 
    policies via dedicated policy instances supplied during construction.
    """

    def __init__(
        self,
        tracker: EventTracker=None,
        service_policies=None,
        finder_config_selector=None,
        db_path=None,
        logger=None,
        configuration=None,
        shakemap_workflow=None,
        shakemap_inputs=None,
        shakemap_notifier=None,
        provider_backed=False,
        finder_options=None,
    ):
        # Supported process composition supplies the scheduler-owned logger.
        # Direct construction retains a non-file logger for library use.
        self.logger = logger or self._setup_file_logger()
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

        if finder_config_selector is None:
            try:
                finder_config_selector = build_default_selector(
                    logger=self.logger
                )
            except GlobalFinderConfigError:
                self.logger.critical(
                    "Global FinDer configuration validation failed; "
                    "aborting scheduler startup",
                    exc_info=True,
                )
                raise
        self.finder_config_selector = finder_config_selector

        # FinDer is needed only after startup policy and computational-profile
        # validation have succeeded.
        from pyfinder.findermanager import FinDerManager

        self._finder_manager_class = FinDerManager

        if configuration is None:
            from pyfinder.pyfinderconfig import pyfinderconfig

            configuration = pyfinderconfig
        self.configuration = configuration
        self.provider_backed = provider_backed
        self.finder_options = dict(finder_options or {})

        # Only explicit process composition enables the external workflow.
        # Both continuous and playback owners retain their own database when
        # accepted remote calculations outlive the local process.
        if (shakemap_workflow is None) != (shakemap_inputs is None):
            raise ValueError("ShakeMap workflow and caller-owned inputs are required together")
        self.shakemap_workflow = shakemap_workflow
        self.shakemap_inputs = shakemap_inputs
        self.shakemap_notifier = shakemap_notifier
        self._shakemap_log_states = {}
        self._shakemap_phase_lock = threading.RLock()
        self._monitor_executor = None
        self._monitor_future = None

        # A tracker supplied by playback remains caller-owned until this
        # constructor succeeds. A tracker created here has no other owner and
        # must be closed if later scheduler construction fails.
        owns_tracker_during_construction = tracker is None
        if tracker is None:
            if db_path is None:
                raise ValueError(
                    "the scheduler requires an explicit operational "
                    "database path when no tracker is supplied"
                )
            tracker = EventTracker(str(db_path))
        self.tracker = tracker
        executor = None
        try:
            self.tracker.set_logger(self.logger)
            self.logger.info("EventTracker initialized for the scheduler.")

            # Recovery must happen before the executor exists. Otherwise new
            # due work could start while rows abandoned by the previous process
            # still look like active local executions.
            recovered_rows = self.tracker.recover_abandoned_processing()
            self.logger.info(
                "Recovered %s abandoned processing rows during scheduler "
                "startup.",
                recovered_rows,
            )

            # Python signal handlers run on the main thread and can re-enter
            # shutdown() while run_once() already owns this coordination lock.
            # Reentrancy prevents that self-deadlock while still excluding a
            # different thread until active discovery and dispatch finish.
            self._state_lock = threading.RLock()
            self._shutdown_lock = threading.Lock()
            self._future_condition = threading.Condition()
            self._submitted_futures = {}
            self._futures_being_observed = set()
            self._accepting_work = True
            self._drain_complete = False
            self._shutdown_complete = False

            # Thread pool with up to 10 workers
            executor = ThreadPoolExecutor(max_workers=10)
            self.executor = executor
            if self.shakemap_workflow is not None or self.shakemap_notifier is not None:
                # Native jobs wait in ShakeMap, not in the FinDer worker pool.
                # One observer serializes local outcome application and performs
                # finite HTTP reads; no worker loops until a native job finishes.
                self._monitor_executor = ThreadPoolExecutor(max_workers=1)
            self.logger.info(
                "ThreadPoolExecutor initialized for the scheduler."
            )
            self.logger.info("FollowUpScheduler initialization completed.")
        except BaseException as construction_error:
            cleanup_errors = []
            if self._monitor_executor is not None:
                try:
                    self._monitor_executor.shutdown(wait=True)
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)
            if executor is not None:
                try:
                    executor.shutdown(wait=True)
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)
            if owns_tracker_during_construction:
                try:
                    self.tracker.close()
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)
            for cleanup_error in cleanup_errors:
                construction_error.add_note(
                    "Scheduler construction cleanup also failed: "
                    "{0}: {1}".format(
                        type(cleanup_error).__name__,
                        cleanup_error,
                    )
                )
            raise

        # Native ShakeMap configuration belongs to the separate service.
        # The caller selects its name at submission; no local profile or
        # configuration download is prepared by scheduler construction.

    @staticmethod
    def _welcome_message(logger):
        """ Print a welcome message to the console and log it. """
        
        logger.info("=========================================================")
        logger.info(" A new scheduler for event updates is being initialized. ")
        getattr(logger, "ok", logger.info)(
            "This is an ok message for testing purposes."
        )
        logger.info("---------------------------------------------------------")
        logger.info("BEGIN: Init FollowUpScheduler")

    @staticmethod
    def _setup_file_logger():
        """Return a non-file fallback for direct library construction."""
        return logging.getLogger(__name__)

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
        notifier = getattr(self, "shakemap_notifier", None)
        if notifier is not None:
            execution_id = self.tracker.get_execution_id(event_id, service, current_delay_time)
            if execution_id is not None:
                # Durable notification intent precedes the terminal transition.
                # This does not send mail or change the scientific outcome.
                notifier.enqueue(execution_id, local_failure_diagnostic(
                    execution_id, event_id, service, diagnostic,
                ))
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
            "with_seiscomp": True,
            "event_id": event_id,
            "test": False,
            "use_library": False,
        }
        finder_options.update(getattr(self, "finder_options", {}))
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

        if getattr(self, "provider_backed", False):
            # A fast schedule retains nominal delay names. Do not describe its
            # next nominal stage as a wall-clock wait in downstream evidence.
            solution_metadata.pop("minutes_until_next_update")
            solution_metadata["nominal_next_delay"] = next_delay
            solution_metadata["scheduled_for_utc"] = event_meta.get(
                EventTracker.Field.next_query_time
            )
            # Assignment can precede worker start when the executor is busy.
            # Record actual handler entry, rather than relabeling queue time.
            solution_metadata["execution_started_at_utc"] = datetime.now(
                timezone.utc
            ).isoformat()
            self.logger.info(
                "Playback event=%s nominal_delay_minutes=%s due_at_utc=%s "
                "execution_started_at_utc=%s",
                event_id, current_delay_time,
                solution_metadata["scheduled_for_utc"],
                solution_metadata["execution_started_at_utc"],
            )

        self.logger.info(
            "FinderManager will run for the scheduled delay for %s: %s minutes.",
            event_id,
            current_delay_time,
        )
        try:
            event_context = event_meta.get(EventTracker.Field.event_context)
            context_diagnostic = event_meta.get(
                EventTracker.Field.event_context_error
            )
            manager_arguments = {
                "options": finder_options,
                "configuration": self.configuration,
                "logger": self.logger,
                "metadata": solution_metadata,
                "event_context": event_context,
                "context_diagnostic": context_diagnostic,
            }
            notifier = getattr(self, "shakemap_notifier", None)
            if notifier is not None:
                execution_id = self.tracker.get_execution_id(
                    event_id=event_id, service=service,
                    current_delay_time=current_delay_time,
                )
                manager_arguments["evidence_callback"] = partial(
                    notifier.capture_finder, execution_id,
                )
            if isinstance(event_context, EventContext):
                decision = self.finder_config_selector.resolve(
                    latitude=event_context.get_latitude(),
                    longitude=event_context.get_longitude(),
                )
                manager_arguments.update(
                    finder_configuration_name=decision.configuration_name,
                    finder_configuration=decision.configuration,
                )

            if getattr(self, "provider_backed", False):
                # Playback deliberately starts with an event ID. Acquisition
                # selects authoritative provider context inside this attempt;
                # absent continuous alert context never enables this branch.
                manager_arguments.pop("event_context")
                manager_arguments.pop("context_diagnostic")
                finder_manager = self._finder_manager_class.for_on_demand(
                    **manager_arguments
                )
            else:
                finder_manager = self._finder_manager_class.for_alert_context(
                    **manager_arguments
                )
            finder_solution = finder_manager.run(event_id=event_id)
        except Exception as error:
            diagnostic = self._failure_diagnostic(
                "Scheduled FinDer execution failed",
                error,
            )
            self.logger.error(
                "Scheduled FinDer execution failed for event %s and "
                "service %s: %s",
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

        if not isinstance(finder_solution, FinderSolution):
            diagnostic = (
                "FinDerManager did not return a usable FinderSolution; "
                "received {0}".format(type(finder_solution).__name__)
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

        if getattr(self, "shakemap_workflow", None) is not None:
            self._submit_shakemap(
                finder_manager, finder_solution, event_id, service,
                current_delay_time, event_meta, policy,
            )
            return finder_solution

        identity = (event_id, service, current_delay_time)
        notifier = getattr(self, "shakemap_notifier", None)
        if notifier is not None:
            execution_id = self.tracker.get_execution_id(*identity)
            value = local_failure_diagnostic(execution_id, event_id, service, "FinDer execution completed")
            value.update(final_outcome="SUCCESS", action="Review retained FinDer input evidence")
            notifier.enqueue(execution_id, value)
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

    def _submit_shakemap(
        self, manager, solution, event_id, service, delay, event_meta, policy,
    ):
        """Retain each deliberate request without changing its public ID."""
        with self._shakemap_phase_lock:
            attempt_id = self.tracker.get_execution_id(
                event_id=event_id,
                service=service,
                current_delay_time=delay,
            )
            if not attempt_id:
                raise SchedulerLifecycleError(
                    "Assigned execution has no persisted attempt identity"
                )

            workflow = self.shakemap_workflow
            workflow.bind_scheduled_attempt(attempt_id, event_id, service, delay)

            try:
                # Persist the exact bundle so a later same-ID request can wait
                # for its predecessor without rerunning FinDer or changing IDs.
                # An existing intent is authoritative even if its ack was lost.
                if workflow.get(attempt_id) is not None:
                    return

                calculation_id, files = manager.prepare_shakemap(solution)
                settings = self.configuration.get("shakemap", {})
                workflow.prepare_scheduled_submission(
                    attempt_id,
                    calculation_id,
                    files,
                    configuration=settings.get("configuration", "global"),
                    overwrite=settings.get("overwrite", True),
                )
            except Exception as error:
                self._record_failed_attempt(
                    event_id=event_id,
                    service=service,
                    current_delay_time=delay,
                    event_meta=event_meta,
                    policy=policy,
                    diagnostic=self._failure_diagnostic("ShakeMap preparation failed", error),
                )
                workflow.finish_scheduled_attempt(attempt_id)
                return

            self._dispatch_shakemap_submissions()

    def _retry_rejected_shakemap(self, link, error):
        """Apply existing retry pacing only when there is no remote acceptance."""
        event_id, service, delay = (
            link["event_id"], link["service"], link["current_delay_time"],
        )
        metadata = self.tracker.get_event_meta(
            event_id=event_id,
            service=service,
            current_delay_time=delay,
        )
        if (
            metadata is not None
            and metadata[EventTracker.Field.status] == "processing"
            and self.tracker.get_execution_id(
                event_id=event_id,
                service=service,
                current_delay_time=delay,
            ) == link["attempt_id"]
        ):
            policy = self.service_policies.get(service)
            if policy is None:
                self.tracker.finish_shakemap_execution(
                    execution_id=link["attempt_id"], success=False,
                    diagnostic="ShakeMap submission has no configured scheduler policy",
                )
            else:
                self._record_failed_attempt(
                    event_id=event_id,
                    service=service,
                    current_delay_time=delay,
                    event_meta=metadata,
                    policy=policy,
                    diagnostic=self._failure_diagnostic("ShakeMap submission failed", error),
                )
        self.shakemap_workflow.finish_scheduled_attempt(link["attempt_id"])

    def _dispatch_shakemap_submissions(self):
        """Send retained requests in order, preserving evidence before same-ID overwrite.

        A preceding accepted job must have a recorded terminal observation before
        a later same-ID POST may replace it. Uncertain acceptance also holds that
        ID: its server handler may still be writing canonical inputs. Other IDs
        remain eligible. This queue retains deliberate requests; it never renames
        their public calculation ID or retries an already-sent request.
        """
        workflow = self.shakemap_workflow
        links = {
            workflow.submission_attempt_id(link["attempt_id"]): link
            for link in workflow.pending_scheduled_attempts()
        }
        unresolved = workflow.unresolved()
        active_ids = {link["attempt_id"] for link in links.values()} | {
            record["attempt_id"] for record in unresolved
        }
        prepared = workflow.prepared_scheduled_submissions(attempt_ids=active_ids)
        prepared_ids = {item["attempt_id"] for item in prepared}
        # Also respect older standalone attempts in this same workflow database.
        blocked_ids = {
            record["request"]["event_id"] for record in unresolved
            if record["attempt_id"] not in prepared_ids
        }
        for request in prepared:
            attempt_id = request["attempt_id"]
            calculation_id = request["event_id"]
            record = workflow.get(attempt_id)

            if record is not None:
                if record["submission_state"] == "REJECTED" and attempt_id in links:
                    # The normal rejection path already attempted the paced
                    # retry transition. If it was interrupted, observe that
                    # persistence failure just as the worker callback does:
                    # finalize remaining local ownership, without incrementing
                    # retry_count again or replaying the rejected request.
                    link = links[attempt_id]
                    self._capture_shakemap(link, record, final=True, final_outcome="FAILED")
                    self.tracker.finish_shakemap_execution(
                        execution_id=link["attempt_id"], success=False,
                        diagnostic=record["last_error"] or "ShakeMap rejection finalization was interrupted",
                    )
                    workflow.finish_scheduled_attempt(link["attempt_id"])

                observation = record["observation"]
                terminal = (
                    record["submission_state"] == "REJECTED"
                    or (
                        observation is not None
                        and observation["details"]["status"] in {"SUCCESS", "FAILED"}
                        and record["last_error"] is None
                    )
                )
                if not terminal or attempt_id in links or workflow.evidence_pending(attempt_id):
                    # A saved terminal observation alone is insufficient: its
                    # immutable evidence and final intent must also be captured.
                    blocked_ids.add(calculation_id)
                continue

            link = links.get(attempt_id)
            if link is None:
                continue
            metadata = self.tracker.get_event_meta(
                event_id=link["event_id"], service=link["service"],
                current_delay_time=link["current_delay_time"],
            )
            if (
                metadata is None
                or metadata[EventTracker.Field.status] != "processing"
                or self.tracker.get_execution_id(
                    event_id=link["event_id"], service=link["service"],
                    current_delay_time=link["current_delay_time"],
                ) != link["attempt_id"]
            ):
                # Forced restart has already failed local processing. Observe
                # jobs that were sent, but do not start this abandoned request.
                retained = workflow.get(link["attempt_id"])
                if retained is not None:
                    self._capture_shakemap(link, retained, final=True, final_outcome="INTERRUPTED")
                workflow.finish_scheduled_attempt(link["attempt_id"])
                continue

            if calculation_id in blocked_ids:
                continue

            try:
                if request["base_url"] != workflow.client.base_url:
                    raise ValueError("Prepared ShakeMap request belongs to a different endpoint")
                # This marker coordinates independent invocation databases,
                # not just workers sharing this scheduler. Keep it through both
                # a regional failure and its global recovery, until evidence is
                # immutable. Unknown acceptance must retain it across exit.
                self.shakemap_inputs.claim(
                    calculation_id, link["attempt_id"], workflow.database_path,
                )
                with self.shakemap_inputs.submission(calculation_id, request["files"]):
                    submitted = workflow.submit(
                        attempt_id,
                        calculation_id,
                        request["files"],
                        configuration=request["configuration"],
                        overwrite=request["overwrite"],
                    )
            except ShakeMapCalculationBusy as error:
                # A conflict is not failed acquisition and must not rerun
                # FinDer. Its owning database remains the recovery authority.
                self._terminally_fail_assigned_item(
                    link["event_id"], link["service"], link["current_delay_time"],
                    str(error),
                )
                workflow.finish_scheduled_attempt(link["attempt_id"])
            except Exception as error:
                record = workflow.get(attempt_id)
                if record is None or record["submission_state"] == "REJECTED":
                    if attempt_id != link["attempt_id"]:
                        # A rejected global recovery ends this chain. It does not
                        # grant a new FinDer run or another regional/global POST.
                        retained = record or workflow.get(link["attempt_id"])
                        self._capture_shakemap(
                            link, retained, final=True, final_outcome="FAILED",
                            local_error=safe_text(str(error)),
                        )
                        self.tracker.finish_shakemap_execution(
                            execution_id=link["attempt_id"], success=False,
                            diagnostic=(record or {}).get("last_error") or safe_text(str(error)),
                        )
                        workflow.finish_scheduled_attempt(link["attempt_id"])
                    else:
                        self._retry_rejected_shakemap(link, error)
                    # No request was accepted. A definite rejection or a local
                    # pre-submission failure cannot leave an active native job.
                    self.shakemap_inputs.release(calculation_id, link["attempt_id"])
                else:
                    blocked_ids.add(calculation_id)
                    self._log_shakemap(record)
            else:
                self._log_shakemap(submitted)
                blocked_ids.add(calculation_id)

    def _log_shakemap(self, record):
        """Log changed facts, not every identical polling pass or full response."""
        value = shakemap_diagnostic(record)
        text = format_diagnostic(value)
        attempt_id = record["attempt_id"]
        if self._shakemap_log_states.get(attempt_id) != text:
            self.logger.info(text)
            self._shakemap_log_states[attempt_id] = text

    def _capture_shakemap(self, link, record, *, final, final_outcome=None, local_error=None):
        """Freeze evidence and notification intent before any same-ID replacement."""
        value = shakemap_diagnostic(record)
        workflow = self.shakemap_workflow
        chain = workflow.attempt_chain(link["attempt_id"])
        value["attempts"] = [shakemap_diagnostic(item) for item in chain]
        value["final_outcome"] = final_outcome or value["status"] or (
            "FAILED" if value["submission_state"] == "REJECTED" else "UNKNOWN"
        )
        active = workflow.submission_attempt_id(link["attempt_id"])
        value["fallback_submission_state"] = (
            "NOT_REQUESTED" if active == link["attempt_id"] else
            (workflow.get(active) or {}).get("submission_state", "NOT_SENT")
        )
        if final_outcome == "INTERRUPTED" and value["fallback_submission_state"] == "NOT_SENT":
            value["fallback_submission_state"] = "ABORTED_BEFORE_SUBMISSION"
        if len(chain) > 1 or active != link["attempt_id"]:
            value["fallback_reason"] = shakemap_diagnostic(chain[0])["reason"]
            value["initial_configuration"] = chain[0]["request"]["configuration"]
        if local_error:
            value["local_error"] = local_error
        notifier = getattr(self, "shakemap_notifier", None)
        if notifier is None:
            return None
        return notifier.capture(
            record, value, execution_id=link["attempt_id"], final=final,
        )

    def _apply_shakemap_result(self, link, record):
        """Keep regional recovery inside its owning scheduled execution."""
        observation = record["observation"]
        if observation is None:
            return
        details = observation["details"]
        if details["status"] not in {"SUCCESS", "FAILED"}:
            return
        self._log_shakemap(record)
        workflow = self.shakemap_workflow
        eligible = regional_configuration_failure(record)
        metadata = self.tracker.get_event_meta(
            event_id=link["event_id"], service=link["service"],
            current_delay_time=link["current_delay_time"],
        )
        owns_processing = (
            metadata is not None and metadata[EventTracker.Field.status] == "processing"
            and self.tracker.get_execution_id(
                event_id=link["event_id"], service=link["service"],
                current_delay_time=link["current_delay_time"],
            ) == link["attempt_id"]
        )
        if eligible and self._accepting_work and owns_processing:
            evidence = self._capture_shakemap(link, record, final=False)
            if not evidence:
                raise SchedulerLifecycleError(
                    "Regional recovery requires immutable evidence capture before replacement"
                )
            workflow.prepare_global_fallback(link["attempt_id"], evidence)
            workflow.clear_local_error(record["attempt_id"])
            self.logger.warning(
                "ShakeMap execution %s retained failed regional sequence %s; "
                "preparing one explicit global submission for calculation %s",
                link["attempt_id"], record["internal_sequence"], record["request"]["event_id"],
            )
            return

        # Native failure and unusable archived products remain distinct. A
        # shutdown does not start new recovery work or claim global success.
        success = details["status"] == "SUCCESS" and details["products_ready"]
        self._capture_shakemap(
            link, record, final=True,
            final_outcome=(
                "INTERRUPTED" if eligible or not owns_processing else
                "SUCCESS" if success else "FAILED"
            ),
        )
        value = shakemap_diagnostic(record)
        diagnostic = None if success else format_diagnostic(value)
        changed = self.tracker.finish_shakemap_execution(
            execution_id=link["attempt_id"], success=success, diagnostic=diagnostic,
        )
        if changed not in (0, 1):
            raise SchedulerLifecycleError("ShakeMap finalization changed multiple scheduled rows")
        workflow.finish_scheduled_attempt(link["attempt_id"])
        workflow.clear_local_error(record["attempt_id"])
        self._shakemap_log_states.pop(record["attempt_id"], None)
        self.shakemap_inputs.release(record["request"]["event_id"], link["attempt_id"])

    def _poll_shakemap_once(self):
        """Observe external jobs independently of discovery of new due work."""
        if self.shakemap_workflow is None:
            # The same finite monitor also delivers caller-only notifications;
            # a disabled ShakeMap adapter must not strand the durable mail queue.
            self.shakemap_notifier.drain_pending()
            return
        with self._shakemap_phase_lock:
            workflow = self.shakemap_workflow
            links = {
                workflow.submission_attempt_id(item["attempt_id"]): item
                for item in workflow.pending_scheduled_attempts()
            }
            records = {item["attempt_id"]: item for item in workflow.unresolved()}
            # A terminal result may already be saved while its local transition
            # failed. Such records are absent from unresolved(), but still need
            # guarded finalization through their retained association.
            for attempt_id in links:
                record = workflow.get(attempt_id)
                if record is not None:
                    records[attempt_id] = record

            for attempt_id, record in records.items():
                if record["submission_state"] != "ACCEPTED":
                    continue
                try:
                    observation = record["observation"]
                    if (
                        observation is None
                        or observation["details"]["status"] not in {"SUCCESS", "FAILED"}
                        or record["last_error"] is not None
                    ):
                        record = workflow.poll(attempt_id)
                    self._log_shakemap(record)
                    if attempt_id in links:
                        self._apply_shakemap_result(links[attempt_id], record)
                    elif record["observation"]["details"]["status"] in {"SUCCESS", "FAILED"}:
                        self._capture_shakemap(
                            {"attempt_id": workflow.execution_for_attempt(attempt_id)},
                            record, final=True, final_outcome="INTERRUPTED",
                        )
                        workflow.clear_local_error(attempt_id)
                        self.shakemap_inputs.release(
                            record["request"]["event_id"], workflow.execution_for_attempt(attempt_id),
                        )
                except Exception as error:
                    # A lost read or local write is not a native FAILED result.
                    # Keep evidence/association and let the next pass observe
                    # this same sequence while other jobs continue to progress.
                    retained = workflow.get(attempt_id)
                    if retained is not None and retained.get("last_error"):
                        self._log_shakemap(retained)
                    else:
                        workflow.retain_local_error(attempt_id, error)
                        self.logger.error(
                            "ShakeMap evidence/lifecycle handling for attempt %s failed: %s",
                            attempt_id, type(error).__name__,
                        )

            # Observation precedes replacement. Once the older result is saved,
            # a retained same-ID request can be sent during this same pass.
            self._dispatch_shakemap_submissions()

        # SMTP is deliberately outside the phase lock and cannot release a
        # calculation ID or rewrite its scientific outcome.
        notifier = getattr(self, "shakemap_notifier", None)
        if notifier is not None:
            notifier.drain_pending()

    def _schedule_shakemap_monitor(self):
        """Keep at most one finite observation pass running in this scheduler."""
        if getattr(self, "_monitor_executor", None) is None:
            return
        if self._monitor_future is not None:
            if not self._monitor_future.done():
                return
            try:
                self._monitor_future.result()
            except Exception:
                self.logger.exception("ShakeMap monitor pass failed")
        self._monitor_future = self._monitor_executor.submit(self._poll_shakemap_once)

    def _drain_shakemap_monitor(self):
        """Stop local monitoring without waiting for remote calculations to finish."""
        if getattr(self, "_monitor_executor", None) is None:
            return
        self._monitor_executor.shutdown(wait=True)
        if self._monitor_future is not None:
            try:
                self._monitor_future.result()
            except Exception:
                self.logger.exception("ShakeMap monitor failed during shutdown")

        if self.shakemap_workflow is None:
            return

        # Workers have already drained, so no new submission can appear here.
        # Preserve terminal evidence already recorded; otherwise explicitly fail
        # local ownership while retaining external jobs for observation on restart.
        for link in self.shakemap_workflow.pending_scheduled_attempts():
            active = self.shakemap_workflow.submission_attempt_id(link["attempt_id"])
            record = self.shakemap_workflow.get(active)
            if (
                record is not None
                and record["observation"] is not None
                and record["observation"]["details"]["status"] in {"SUCCESS", "FAILED"}
                and record["last_error"] is None
            ):
                self._apply_shakemap_result(link, record)
            else:
                retained = record or self.shakemap_workflow.get(link["attempt_id"])
                if retained is not None:
                    self._capture_shakemap(link, retained, final=True, final_outcome="INTERRUPTED")
                self.tracker.finish_shakemap_execution(
                    execution_id=link["attempt_id"], success=False,
                    diagnostic="Local scheduler stopped; external ShakeMap outcome remains separate",
                )
                self.shakemap_workflow.finish_scheduled_attempt(link["attempt_id"])

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

    def _stop_and_drain_locked(self):
        """Stop assignment and finish worker persistence while locked."""
        if self._drain_complete:
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
        self._drain_shakemap_monitor()
        notifier = getattr(self, "shakemap_notifier", None)
        if notifier is not None:
            # Shutdown may just have queued an interruption. Finish one bounded
            # delivery pass after scientific/evidence ownership has been settled.
            notifier.drain_pending()
        self._drain_complete = True

    def stop_and_drain(self):
        """Stop accepting work and finish every retained worker outcome."""
        with self._shutdown_lock:
            self._stop_and_drain_locked()

    def close(self):
        """Close scheduler persistence after all worker outcomes finish."""
        with self._shutdown_lock:
            if self._shutdown_complete:
                return
            self._stop_and_drain_locked()
            try:
                if getattr(self, "shakemap_workflow", None) is not None:
                    self.shakemap_workflow.close()
            finally:
                try:
                    notifier = getattr(self, "shakemap_notifier", None)
                    if notifier is not None:
                        notifier.close()
                finally:
                    self.tracker.close()
            self._shutdown_complete = True
            self.logger.info("FollowUpScheduler shutdown complete.")

    def shutdown(self):
        """Drain scheduler work and close its persistence."""
        self.close()

    def run_once(self):
        """ Run the scheduler once to check for due events and process them. """
        with self._state_lock:
            if not self._accepting_work:
                return

            # External calculations progress even when there are no newly due
            # FinDer executions. Observation runs outside the discovery lock.
            self._schedule_shakemap_monitor()
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

    def run_until_complete(self, *, shutdown_event, interval_seconds=1):
        """Finish selected playback work without discarding unresolved outcomes.

        This loop deliberately includes future pending rows. A quiet worker
        pool alone is not completion while later steps or native jobs remain.
        Observation failures and ambiguous acceptance stop this invocation with
        retained evidence; they never authorize another scientific submission.
        Continuous operation keeps its existing run_forever behavior.
        """
        while not shutdown_event.is_set():
            self.run_once()

            # One monitor pass has bounded HTTP/SMTP operations. Observe it
            # before checking final evidence and delivery state, so completion
            # cannot race a terminal mail enqueue or native result capture.
            monitor = self._monitor_future
            if monitor is not None:
                try:
                    monitor.result()
                except Exception:
                    self.logger.exception("Playback downstream observation failed; state retained")
                    return 1

            with self._future_condition:
                workers_active = bool(
                    self._submitted_futures or self._futures_being_observed
                )
            if workers_active:
                shutdown_event.wait(interval_seconds)
                continue

            workflow = self.shakemap_workflow
            unresolved = workflow.unresolved() if workflow is not None else []
            requires_attention = any(
                record["submission_state"] != "ACCEPTED" or record["last_error"]
                for record in unresolved
            )
            if requires_attention:
                self.logger.error(
                    "Playback requires attention: remote acceptance, observation or "
                    "evidence remains unresolved. State is retained; no automatic replay."
                )
                return 1

            counts = self.tracker.status_counts()
            links = workflow.pending_scheduled_attempts() if workflow is not None else []
            if counts.get("pending", 0) or unresolved or links:
                shutdown_event.wait(interval_seconds)
                continue

            # A processing row with no worker or remote association indicates
            # incomplete lifecycle persistence, never a successful empty queue.
            states = []
            if self.shakemap_notifier is not None:
                states = self.shakemap_notifier.delivery_states()
            if any(state in {"PENDING", "SENDING"} for state in states):
                # A local worker may enqueue mail just after the observation
                # pass drained it. Let the next bounded pass handle that intent
                # before deciding the command outcome.
                shutdown_event.wait(interval_seconds)
                continue

            success = (
                bool(counts) and set(counts) == {"completed"}
                and all(state in {"SENT", "SUPPRESSED"} for state in states)
            )
            self.logger.info("Playback calculation counts=%s; delivery states=%s", counts, states)
            return 0 if success else 1

        self.logger.warning("Playback interrupted; remaining schedules and remote state are retained")
        return 130

    def run_forever(self, interval_seconds=10, shutdown_event=None):
        """ 
        Run the scheduler until it is stopped or shutdown is requested.
        """
        import time
        self.logger.info(f"Scheduler running every {interval_seconds} seconds.")

        while True:
            with self._state_lock:
                if not self._accepting_work:
                    break
            if shutdown_event is not None and shutdown_event.is_set():
                break
            self.run_once()
            if shutdown_event is None:
                time.sleep(interval_seconds)
            elif shutdown_event.wait(interval_seconds):
                break
