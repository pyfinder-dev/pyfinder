"""Run provider-backed calculations through the complete scheduled workflow.

Playback chooses event IDs and schedule coverage. It does not reconstruct past
provider responses or manufacture EMSC alerts. Each calculation acquires the
currently available data and selects its event context inside FinDerManager.
"""

from datetime import datetime, timedelta, timezone
import logging
import signal
import sys
import threading

from pyfinder.pyfinderconfig import pyfinderconfig
from pyfinder.runtime import RuntimeBootstrapError
from pyfinder.services.alert import configured_alert_settings
from pyfinder.services.eventtracker import EventTracker, ScheduleRegistrationError
from pyfinder.services.querypolicy import build_service_policies
from pyfinder.services.scheduler import FollowUpScheduler
from pyfinder.services.shakemap_settings import continuous_shakemap_configuration
from pyfinder.services.workflow_resources import (
    build_notifier,
    build_shakemap_boundary,
)
from pyfinder.utils.customlogger import file_logger


# These are event selections, not authoritative earthquake metadata. The former
# alert definitions and injector remain under legacy/playback_alert_injector.py.
DEFAULT_EVENT_IDS = (
    "20161030_0000029",
    "20230206_0000008",
    "20230206_0000222",
    "20250522_0000028",
    "20250423_0000104",
    "20250520_0000201",
    "20250922_0000172",
)


def register_playback(
    tracker,
    event_ids,
    policy,
    *,
    full_schedule=False,
    fast=False,
    registered_at=None,
):
    """Persist nominal identities separately from actual submission due times.

    Registration time is scheduling metadata only. The scientific origin and
    alert snapshot are intentionally absent: providers supply the earthquake
    context when each calculation executes.
    """
    registered_at = registered_at or datetime.now(timezone.utc)
    delays = list(policy.QUERY_SCHEDULE_MINUTES) if full_schedule else [0]
    registration_errors = []

    for event_id in dict.fromkeys(event_ids):
        failures = []
        successful_rows = 0

        for index, delay in enumerate(delays):
            due_offset = index * 2 if fast else delay
            following = delays[index + 1] if index + 1 < len(delays) else None
            due_at = registered_at + timedelta(minutes=due_offset)

            try:
                tracker.register_new_schedule(
                    event_id=event_id,
                    service=policy.service_name,
                    origin_time=None,
                    last_update_time=registered_at.isoformat(timespec="seconds"),
                    current_delay_time=delay,
                    next_delay_time=following,
                    next_query_time=due_at.isoformat(timespec="seconds"),
                    emsc_alert_json=None,
                )
                successful_rows += 1
                tracker.logger.info(
                    "Playback event=%s nominal_delay_minutes=%s due_at_utc=%s",
                    event_id, delay, due_at.isoformat(),
                )
            except Exception as error:
                # Independent inserts preserve useful work if one stage fails.
                # Still attempt every requested stage/event before surfacing
                # the registration failure to the command owner.
                failures.append((delay, error))

        if failures:
            registration_errors.append(ScheduleRegistrationError(
                event_id, policy.service_name, successful_rows, failures,
            ))

    if registration_errors:
        primary_error = registration_errors[0]
        for error in registration_errors[1:]:
            primary_error.add_note(str(error))
        raise primary_error


def run_cli(arguments, *, runtime_context):
    """Own one finite playback invocation and retain its diagnostic state."""
    if arguments.list_events:
        print("Predetermined playback event IDs:")
        print("\n".join(DEFAULT_EVENT_IDS))
        return 0

    event_ids = arguments.event_ids or DEFAULT_EVENT_IDS
    process_logger = file_logger(
        runtime_context.process_log_path,
        module_name="Playback",
        rotate=True,
        overwrite=False,
        level=getattr(logging, arguments.verbosity),
    )
    scheduler_logger = file_logger(
        runtime_context.scheduler_log_path,
        module_name="PlaybackFollowUpScheduler",
        rotate=True,
        overwrite=False,
    )
    configuration = continuous_shakemap_configuration(
        runtime_context.isolated_configuration(pyfinderconfig)
    )
    if not configuration.get("shakemap", {}).get("service-enabled", False):
        raise RuntimeBootstrapError(
            "Playback requires the full chain: enable PYFINDER_SHAKEMAP_ENABLED "
            "and configure the ShakeMap URL and shared input directory."
        )
    alert_settings = configured_alert_settings()
    if alert_settings is None:
        process_logger.info(
            "Email delivery is explicitly suppressed: "
            "no alert configuration is enabled."
        )
    if arguments.fast and not arguments.full_schedule:
        process_logger.info(
            "--fast has no effect with one immediate calculation per event."
        )

    shutdown_event = threading.Event()
    previous_handlers = {}
    scheduler = None
    tracker = None
    workflow = None
    notifier = None
    result = 1
    with runtime_context.playback_database() as database_path:
        process_logger.info("Playback state retained at %s", database_path)
        print(f"Playback state: {database_path}")
        try:
            policies = build_service_policies()
            tracker = EventTracker(str(database_path), logger=process_logger)
            workflow, inputs = build_shakemap_boundary(
                configuration, database_path,
            )
            notifier = build_notifier(
                runtime_context, database_path, inputs,
                alert_settings, scheduler_logger,
            )
            scheduler = FollowUpScheduler(
                tracker=tracker,
                service_policies=policies,
                logger=scheduler_logger,
                configuration=configuration,
                shakemap_workflow=workflow,
                shakemap_inputs=inputs,
                shakemap_notifier=notifier,
                provider_backed=True,
                finder_options={
                    "verbosity": arguments.verbosity,
                    "with_seiscomp": False,
                },
            )
            register_playback(
                tracker, event_ids, policies["RRSM"],
                full_schedule=arguments.full_schedule, fast=arguments.fast,
            )

            # Signals request normal scheduler drainage. Remote jobs may outlive
            # the process; shutdown records that fact without replaying a POST.
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, lambda *_args: shutdown_event.set())
            result = scheduler.run_until_complete(shutdown_event=shutdown_event)
        finally:
            # Resource ownership transfers only after scheduler construction.
            # Attempt every cleanup and preserve the original execution error
            # if cleanup itself also fails.
            primary_error = sys.exception()
            cleanup_errors = []
            if scheduler is not None:
                cleanup_operations = [scheduler.shutdown]
            else:
                cleanup_operations = [
                    resource.close
                    for resource in (workflow, notifier, tracker)
                    if resource is not None
                ]

            for operation in cleanup_operations:
                try:
                    operation()
                except BaseException as error:
                    process_logger.exception("Playback resource cleanup failed")
                    cleanup_errors.append(error)

            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)

            if cleanup_errors:
                if primary_error is None:
                    raise cleanup_errors[0]
                for error in cleanup_errors:
                    primary_error.add_note(f"Cleanup also failed: {error}")

        process_logger.info(
            "Playback finished with exit code %s; retained state: %s",
            result, database_path,
        )
        print(
            f"Playback finished with exit code {result}. "
            f"Retained state: {database_path}"
        )
    return result


if __name__ == "__main__":
    from pyfinder.cli import main

    sys.exit(main(["playback", *sys.argv[1:]]))
