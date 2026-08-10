from collections.abc import Mapping
from copy import deepcopy
import sys
import json
from datetime import datetime, timezone
import logging
import threading

from pyfinder.eventcontext import EventContext, EventContextError
from pyfinder.services.scheduler import FollowUpScheduler
from pyfinder.services.eventtracker import EventTracker
from pyfinder.services.querypolicy import RRSMQueryPolicy
from pyfinder.pyfinderconfig import pyfinderconfig
from pyfinder.utils.customlogger import file_logger
from pyfinder.utils.timeutils import get_epoch_time


def generate_event_list():
    """Return complete known alerts for one playback registration run.

    Historical origin times remain fixed scientific metadata. The shared
    last-update value identifies the current replay and is intentionally
    generated independently from those origins.
    """
    replay_last_update = datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    return [
        {
            "source_id": "00000001",
            "source_catalog": "EMSC-RTS",
            "flynn_region": "NORCIA, ITALY",
            "lat": 42.84,
            "lon": 13.11,
            "depth": 10.0,
            "evtype": "ke",
            "auth": "SCSN",
            "mag": 6.5,
            "magtype": "Mw",
            "unid": "20161030_0000029",
            "action": "create",
            "time": "2016-10-30T06:40:18.3Z",
            "lastupdate": replay_last_update,
        },
        {
            "source_id": "00000002",
            "source_catalog": "EMSC-RTS",
            "flynn_region": "PAZARCIK, TURKEY",
            "lat": 37.17,
            "lon": 37.08,
            "depth": 20.0,
            "evtype": "ke",
            "auth": "SCSN",
            "mag": 7.8,
            "magtype": "Mw",
            "unid": "20230206_0000008",
            "action": "create",
            "time": "2023-02-06T01:17:36.1Z",
            "lastupdate": replay_last_update,
        },
        {
            "source_id": "00000003",
            "source_catalog": "EMSC-RTS",
            "flynn_region": "ELBISTAN, TURKEY",
            "lat": 38.11,
            "lon": 37.24,
            "depth": 10.0,
            "evtype": "ke",
            "auth": "SCSN",
            "mag": 7.5,
            "magtype": "Mw",
            "unid": "20230206_0000222",
            "action": "create",
            "time": "2023-02-06T10:24:49.6Z",
            "lastupdate": replay_last_update,
        },
        {
            "source_id": "00000004",
            "source_catalog": "EMSC-RTS",
            "flynn_region": "CRETE, GREECE",
            "lat": 35.72,
            "lon": 25.91,
            "depth": 53.0,
            "evtype": "ke",
            "auth": "SCSN",
            "mag": 6.2,
            "magtype": "Mw",
            "unid": "20250522_0000028",
            "action": "create",
            "time": "2025-05-22T03:19:34.6Z",
            "lastupdate": replay_last_update,
        },
        {
            "source_id": "00000005",
            "source_catalog": "EMSC-RTS",
            "flynn_region": "ISTANBUL, TURKEY",
            "lat": 40.887,
            "lon": 28.138,
            "depth": 12.0,
            "evtype": "ke",
            "auth": "SCSN",
            "mag": 6.2,
            "magtype": "Mw",
            "unid": "20250423_0000104",
            "action": "create",
            "time": "2025-04-23T09:49:11.93Z",
            "lastupdate": replay_last_update,
        },
        {
            "source_id": "00000006",
            "source_catalog": "EMSC-RTS",
            "flynn_region": "MARMARA SEA, TURKEY",
            "lat": 40.815,
            "lon": 28.386,
            "depth": 8.2,
            "evtype": "ke",
            "auth": "SCSN",
            "mag": 4.2,
            "magtype": "ML",
            "unid": "20250520_0000201",
            "action": "create",
            "time": "2025-05-20T20:36:52.26Z",
            "lastupdate": replay_last_update,
        },
        {
            "source_id": "00000007",
            "source_catalog": "EMSC-RTS",
            "flynn_region": "WESTERN TURKEY",
            "lat": 39.1855,
            "lon": 28.1637,
            "depth": 12.2,
            "evtype": "ke",
            "auth": "SCSN",
            "mag": 4.5,
            "magtype": "ML",
            "unid": "20250922_0000172",
            "action": "create",
            "time": "2025-09-22T09:02:44.04Z",
            "lastupdate": replay_last_update,
        },
    ]

class EventAlertWSPlaybackManager:
    """
    Class to manage the playback of EMSC event alerts for testing purposes.
    This class replaces the real-time event alerts with a pre-defined list of events.
    The whole processing chain for the parametric dataset workflow is executed
    normally, so the RRSM and ESM web services will be actually called. 
    """
    def __init__(
        self,
        event_list,
        event_tracker,
        speedup_factor=1.0,
        default_services=None,
        logger=None,
        failure_callback=None,
    ):
        """
        event_list: List of events to be played back, in the same JSON structure as the alerts from EMSC
        event_tracker: EventTracker instance used to persist playback schedules
        scheduler: Instance of FollowUpScheduler
        speedup_factor: Speed multiplier for playback
        default_services: Default services if event doesn't specify
        failure_callback: Optional command-owner callback for worker failures
        """
        self.event_tracker = event_tracker
        self.speedup_factor = speedup_factor
        self.default_services = default_services or ["RRSM", "ESM"]
        self.logger = logger or logging.getLogger(__name__)
        self._failure_callback = failure_callback
        valid_events = []
        for event in event_list:
            validated_event = self._validated_event(event)
            if validated_event is not None:
                valid_events.append(validated_event)
        self.event_list = sorted(
            valid_events,
            key=lambda event: get_epoch_time(event["time"]),
        )
        self.index = 0
        self.running = False
        self._thread = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._worker_error = None

    def _validated_event(self, event):
        """Return an isolated usable alert mapping or warn and ignore it."""
        identity = event.get("unid") if isinstance(event, Mapping) else None
        try:
            if not isinstance(event, Mapping):
                raise EventContextError("the playback event is not a mapping")
            event_copy = deepcopy(dict(event))
            EventContext.from_alert_mapping(
                event_copy,
                scheduled_event_id=event_copy.get("unid"),
            )
            last_update = event_copy.get("lastupdate")
            if not isinstance(last_update, str) or not last_update.strip():
                raise EventContextError(
                    "last-update time is missing or not a string"
                )
            if get_epoch_time(last_update) is None:
                raise EventContextError(
                    "last-update time is not accepted by the current "
                    "timestamp converter"
                )
            return event_copy
        except EventContextError as error:
            self.logger.warning(
                "Ignoring playback event %r because its predefined metadata "
                "is unusable: %s",
                identity,
                error,
            )
            return None

    def start_auto(self):
        """Start automatic playback."""
        if self.running:
            print("[EMSC Event Alert Playback] Already running.")
            return

        self._stop_event.clear()
        self._worker_error = None
        self.running = True
        self._thread = threading.Thread(target=self._run)
        self._thread.start()
        print("[EMSC Event Alert Playback] Started automatic playback.")

    def pause(self):
        """Pause automatic playback."""
        if not self.running and self._thread is None:
            print("[EMSC Event Alert Playback] Already paused.")
            return

        self.stop()
        self.join()
        print("[EMSC Event Alert Playback] Paused.")

    def stop(self):
        """Stop playback alert injection without waiting for its thread."""
        self.running = False
        self._stop_event.set()

    def join(self):
        """Join and release the owned playback thread after it stops."""
        playback_thread = self._thread
        if (
            playback_thread is not None
            and playback_thread is not threading.current_thread()
        ):
            playback_thread.join()
        self._thread = None

    def inject_next_event(self):
        """Manually inject the next event."""
        with self._lock:
            if self.index >= len(self.event_list):
                print("[EMSC Event Alert Playback] All events have been injected.")
                return

            event = self.event_list[self.index]
            self.index += 1

        print(f"[EMSC Event Alert Playback] Injecting event {event['unid']} manually at {datetime.now().isoformat()}")
        self._inject_event(event)

    def _run(self):
        """Internal thread loop for automatic playback."""
        try:
            while (
                self.running
                and not self._stop_event.is_set()
                and self.index < len(self.event_list)
            ):
                with self._lock:
                    event = self.event_list[self.index]
                    self.index += 1

                print(f"[EMSC Event Alert Playback] Auto-injecting event {event['unid']} at {datetime.now().isoformat()}")
                self._inject_event(event)

                if self.index < len(self.event_list):
                    next_event = self.event_list[self.index]
                    sleep_seconds = (
                        get_epoch_time(next_event["time"])
                        - get_epoch_time(event["time"])
                    )
                    sleep_seconds /= self.speedup_factor
                    if sleep_seconds > 0:
                        # Long historical gaps remain bounded, and shutdown can
                        # interrupt the wait immediately.
                        self._stop_event.wait(min(sleep_seconds, 60))
            # Stay alive after injecting all events
            print("[EMSC Event Alert Playback] All events injected. Waiting for external termination...")
            while self.running and not self._stop_event.wait(0.5):
                pass
        except BaseException as error:
            self._worker_error = error
            self.running = False
            self._stop_event.set()
            if self._failure_callback is not None:
                try:
                    self._failure_callback(error)
                except BaseException as callback_error:
                    error.add_note(
                        "Playback failure notification also failed: "
                        "{0}: {1}".format(
                            type(callback_error).__name__,
                            callback_error,
                        )
                    )
        finally:
            self.running = False
            self._stop_event.set()

    def _inject_event(self, event):
        """Internal helper to inject an event into system."""
        event = self._validated_event(event)
        if event is None:
            return False

        # Use batch_register_from_policy for policy-based scheduling
        self.event_tracker.batch_register_from_policy(
            event_id=event['unid'],
            origin_time=event['time'],
            last_update_time=event['lastupdate'],
            emsc_alert_json=json.dumps(event),
            policy=RRSMQueryPolicy()
        )
        return True

    def reset(self):
        """Reset to beginning."""
        self.pause()
        with self._lock:
            self.index = 0


def run_cli(arguments, *, runtime_context):
    """Run the existing playback workflow from parsed CLI arguments."""
    playback = None
    scheduler = None
    tracker = None
    scheduler_thread = None
    scheduler_thread_started = False
    shutdown_event = threading.Event()
    scheduler_failures = []
    playback_failures = []
    process_logger = file_logger(
        runtime_context.process_log_path,
        module_name="Playback",
        rotate=True,
        overwrite=False,
    )
    scheduler_logger = file_logger(
        runtime_context.scheduler_log_path,
        module_name="PlaybackFollowUpScheduler",
        rotate=True,
        overwrite=False,
    )
    application_configuration = runtime_context.isolated_configuration(
        pyfinderconfig
    )

    def report_playback_failure(error):
        playback_failures.append(error)
        shutdown_event.set()

    def shutdown_resources():
        """Stop playback, drain scheduled work, and join owned threads."""
        failures = []

        def attempt(description, operation):
            try:
                operation()
            except BaseException as error:
                process_logger.error(
                    "%s failed: %s",
                    description,
                    error,
                    exc_info=(type(error), error, error.__traceback__),
                )
                failures.append(error)

        if playback is not None:
            attempt("Stopping playback alert injection", playback.stop)
        shutdown_event.set()
        if scheduler is not None:
            attempt("Draining the playback scheduler", scheduler.stop_and_drain)
        if playback is not None:
            attempt("Joining the playback alert thread", playback.join)
        if scheduler_thread is not None and scheduler_thread_started:
            attempt("Joining the playback scheduler thread", scheduler_thread.join)
        if scheduler is not None:
            attempt("Closing playback scheduler persistence", scheduler.close)
        elif tracker is not None:
            attempt("Closing playback persistence", tracker.close)
        return failures

    # The predefined list of events to be played back
    event_list = generate_event_list()

    # If --list is specified, print available event IDs, the regions and exit
    if arguments.list_events:
        print("Available events for playback:")
        for event in event_list:
            print(f"Event ID: {event['unid']}, M{event['mag']}, Region: {event['flynn_region']}")
        return 0

    if arguments.event_id:
        event_list = [
            event
            for event in event_list
            if event["unid"] in arguments.event_id
        ]
    # Make sure we have events to play back
    if not event_list:
        print("No events found for the specified IDs. Exiting.")
        return 1

    with runtime_context.playback_database() as database_path:
        primary_error = None
        primary_traceback = None
        try:
            tracker = EventTracker(str(database_path), logger=process_logger)

            # Playback manager instance
            playback = EventAlertWSPlaybackManager(
                event_list=event_list,
                event_tracker=tracker,
                speedup_factor=1.0,
                default_services=["RRSM"],
                logger=process_logger,
                failure_callback=report_playback_failure,
            )

            # Now start scheduler and playback
            scheduler = FollowUpScheduler(
                tracker=tracker,
                logger=scheduler_logger,
                configuration=application_configuration,
            )
            def run_scheduler():
                try:
                    scheduler.run_forever(shutdown_event=shutdown_event)
                except BaseException as error:
                    scheduler_failures.append(error)
                finally:
                    shutdown_event.set()

            scheduler_thread = threading.Thread(
                target=run_scheduler,
                daemon=False,
            )
            scheduler_thread.start()
            scheduler_thread_started = True

            playback.start_auto()

            print("[Main] Running playback. Press Ctrl+C to exit.")
            try:
                while not shutdown_event.wait(1):
                    pass
            except (KeyboardInterrupt, SystemExit):
                print("\n[Main] Interrupt received. Shutting down...")
            if playback_failures:
                raise playback_failures[0]
            if scheduler_failures:
                raise scheduler_failures[0]
        except BaseException as error:
            primary_error = error
            primary_traceback = error.__traceback__
        finally:
            cleanup_failures = shutdown_resources()

        if primary_error is not None:
            for cleanup_error in cleanup_failures:
                primary_error.add_note(
                    "Cleanup also failed: {0}: {1}".format(
                        type(cleanup_error).__name__,
                        cleanup_error,
                    )
                )
            if isinstance(primary_error, (KeyboardInterrupt, SystemExit)):
                if cleanup_failures:
                    raise cleanup_failures[0]
            else:
                raise primary_error.with_traceback(primary_traceback)
        elif cleanup_failures:
            raise cleanup_failures[0]
    return 0


if __name__ == "__main__":
    from pyfinder.cli import main

    sys.exit(main(["playback", *sys.argv[1:]]))
