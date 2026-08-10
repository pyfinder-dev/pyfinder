"""Direct tests for the installed command's workflow adapters."""

import atexit
from contextlib import contextmanager
from contextlib import redirect_stdout
from copy import deepcopy
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock


_PARAMWS_LOG_DIRECTORY = tempfile.TemporaryDirectory(
    prefix="pyfinder-cli-adapter-unit-"
)
atexit.register(_PARAMWS_LOG_DIRECTORY.cleanup)
_original_paramws_log_file = os.environ.get("PARAMWS_LOG_FILE")
os.environ["PARAMWS_LOG_FILE"] = str(
    Path(_PARAMWS_LOG_DIRECTORY.name) / "paramws.log"
)
try:
    from pyfinder import cli, findermanager, playback
    from pyfinder.services import eventtracker as eventtracker_module
    from pyfinder.services import scheduler as scheduler_module
finally:
    if _original_paramws_log_file is None:
        os.environ.pop("PARAMWS_LOG_FILE", None)
    else:
        os.environ["PARAMWS_LOG_FILE"] = _original_paramws_log_file


class RuntimeContextDouble:
    def __init__(self, root, workflow):
        self.root = Path(root)
        self.workflow = workflow
        self.process_log_path = self.root / "playback.log"
        self.scheduler_log_path = self.root / "followupscheduler.log"
        self.work_root = self.root / "work"
        self.database_path = self.root / "playback.sqlite3"
        self.database_allocations = 0

    def isolated_configuration(self, configuration):
        isolated = deepcopy(configuration)
        isolated["finder-executable"]["output-root-folder"] = str(
            self.work_root
        )
        return isolated

    @contextmanager
    def playback_database(self):
        self.database_allocations += 1
        yield self.database_path


class RecordingRuntimeContext(RuntimeContextDouble):
    def __init__(self, root, workflow, events):
        super().__init__(root, workflow)
        self.events = events

    @contextmanager
    def playback_database(self):
        self.database_allocations += 1
        self.events.append("database-enter")
        self.database_path.touch()
        try:
            yield self.database_path
        finally:
            self.database_path.unlink(missing_ok=True)
            self.events.append("database-removed")


class OnDemandAdapterTests(unittest.TestCase):
    def setUp(self):
        self.runtime_directory = tempfile.TemporaryDirectory(
            prefix="pyfinder-on-demand-adapter-"
        )
        self.runtime_context = RuntimeContextDouble(
            self.runtime_directory.name,
            "on-demand",
        )
        self.original_log_name = findermanager.pyfinderconfig[
            "finder-executable"
        ]["log-file-name"]

    def tearDown(self):
        findermanager.pyfinderconfig["finder-executable"][
            "log-file-name"
        ] = self.original_log_name
        self.runtime_directory.cleanup()

    def test_explicit_event_is_translated_without_a_log_override(self):
        arguments = cli.build_parser().parse_args(
            [
                "on-demand",
                "--event-id",
                "event-123",
                "--verbosity",
                "debug",
            ]
        )
        manager = mock.Mock()
        manager.run.return_value = "solution-value"
        output = io.StringIO()

        process_logger = mock.Mock()
        with mock.patch.object(
            findermanager.customlogger,
            "file_logger",
            return_value=process_logger,
        ), mock.patch.object(
            sqlite3,
            "connect",
            autospec=True,
        ) as database_connect, mock.patch.object(
            findermanager.FinDerManager,
            "for_on_demand",
            return_value=manager,
        ) as constructor, mock.patch.object(
            eventtracker_module,
            "EventTracker",
        ) as tracker_constructor, mock.patch.object(
            scheduler_module,
            "FollowUpScheduler",
        ) as scheduler_constructor, redirect_stdout(output):
            result = findermanager.run_cli(
                arguments,
                runtime_context=self.runtime_context,
            )

        self.assertEqual(result, 0)
        constructor.assert_called_once()
        constructor_arguments = constructor.call_args.kwargs
        self.assertIsNot(
            constructor_arguments["configuration"],
            findermanager.pyfinderconfig,
        )
        self.assertEqual(
            constructor_arguments["configuration"]["finder-executable"][
                "output-root-folder"
            ],
            str(self.runtime_context.work_root),
        )
        self.assertIs(constructor_arguments["logger"], process_logger)
        options = constructor_arguments["options"]
        self.assertEqual(
            set(options),
            {
                "verbosity",
                "with_seiscomp",
                "event_id",
                "test",
                "use_library",
                "command_line_args",
            },
        )
        self.assertEqual(options["verbosity"], "DEBUG")
        self.assertEqual(options["event_id"], "event-123")
        self.assertFalse(options["test"])
        self.assertFalse(options["with_seiscomp"])
        self.assertFalse(options["use_library"])
        self.assertNotIn("log_file", options)
        manager.run.assert_called_once_with(event_id="event-123")
        self.assertEqual(output.getvalue(), "FinDer solution: solution-value\n")
        self.assertEqual(
            findermanager.pyfinderconfig["finder-executable"][
                "log-file-name"
            ],
            self.original_log_name,
        )
        self.assertEqual(self.runtime_context.database_allocations, 0)
        database_connect.assert_not_called()
        tracker_constructor.assert_not_called()
        scheduler_constructor.assert_not_called()

    def test_explicit_test_mode_uses_configured_event_and_preserves_no_solution(self):
        arguments = cli.build_parser().parse_args(["on-demand", "--test"])
        configured_event_id = findermanager.pyfinderconfig["general"][
            "test-event-id"
        ]
        manager = mock.Mock()
        manager.run.return_value = None
        output = io.StringIO()

        process_logger = mock.Mock()
        with mock.patch.object(
            findermanager.customlogger,
            "file_logger",
            return_value=process_logger,
        ), mock.patch.object(
            findermanager.FinDerManager,
            "for_on_demand",
            return_value=manager,
        ) as constructor, redirect_stdout(output):
            result = findermanager.run_cli(
                arguments,
                runtime_context=self.runtime_context,
            )

        self.assertEqual(result, 0)
        options = constructor.call_args.kwargs["options"]
        self.assertTrue(options["test"])
        self.assertEqual(options["event_id"], configured_event_id)
        self.assertNotIn("log_file", options)
        self.assertIs(constructor.call_args.kwargs["logger"], process_logger)
        manager.run.assert_called_once_with(event_id=configured_event_id)
        self.assertEqual(output.getvalue(), "No FinDer solution returned.\n")
        self.assertEqual(
            findermanager.pyfinderconfig["finder-executable"][
                "log-file-name"
            ],
            self.original_log_name,
        )
        self.assertEqual(self.runtime_context.database_allocations, 0)


class PlaybackAdapterTests(unittest.TestCase):
    def setUp(self):
        self.runtime_directory = tempfile.TemporaryDirectory(
            prefix="pyfinder-playback-adapter-"
        )
        self.runtime_context = RuntimeContextDouble(
            self.runtime_directory.name,
            "playback",
        )

    def tearDown(self):
        self.runtime_directory.cleanup()

    @staticmethod
    def event(event_id, magnitude=5.0, region="TEST REGION"):
        return {
            "unid": event_id,
            "mag": magnitude,
            "flynn_region": region,
        }

    @staticmethod
    def complete_event(event_id):
        return {
            "unid": event_id,
            "lat": 46.25,
            "lon": 7.75,
            "mag": 5.6,
            "depth": 12.5,
            "time": "2020-01-02T03:04:05.250000Z",
            "lastupdate": "2020-01-02T03:05:06.500000Z",
            "magtype": "Mw",
            "flynn_region": "TEST REGION",
            "action": "create",
        }

    def test_list_returns_before_constructing_runtime_objects(self):
        events = (
            self.event("event-1", magnitude=5.1, region="REGION ONE"),
            self.event("event-2", magnitude=6.2, region="REGION TWO"),
        )
        arguments = cli.build_parser().parse_args(["playback", "--list"])
        output = io.StringIO()

        process_logger = mock.Mock()
        scheduler_logger = mock.Mock()
        with mock.patch.object(
            playback,
            "file_logger",
            side_effect=(process_logger, scheduler_logger),
        ), mock.patch.object(
            playback,
            "generate_event_list",
            return_value=list(events),
        ), mock.patch.object(
            playback,
            "EventTracker",
        ) as tracker_constructor, mock.patch.object(
            playback,
            "FollowUpScheduler",
        ) as scheduler_constructor, mock.patch.object(
            playback,
            "EventAlertWSPlaybackManager",
        ) as playback_constructor, mock.patch.object(
            playback.threading,
            "Thread",
        ) as thread_constructor, redirect_stdout(output):
            result = playback.run_cli(
                arguments,
                runtime_context=self.runtime_context,
            )

        self.assertEqual(result, 0)
        self.assertIn("Event ID: event-1, M5.1, Region: REGION ONE", output.getvalue())
        self.assertIn("Event ID: event-2, M6.2, Region: REGION TWO", output.getvalue())
        tracker_constructor.assert_not_called()
        scheduler_constructor.assert_not_called()
        playback_constructor.assert_not_called()
        thread_constructor.assert_not_called()
        self.assertEqual(self.runtime_context.database_allocations, 0)

    def test_normal_run_filters_events_and_preserves_handoff_and_shutdown(self):
        selected_event = self.event("selected")
        ignored_event = self.event("ignored")
        arguments = cli.build_parser().parse_args(
            ["playback", "--event-id", "selected"]
        )
        tracker = mock.Mock()
        scheduler = mock.Mock()
        playback_manager = mock.Mock()
        scheduler_thread = mock.Mock()
        shutdown_event = mock.Mock()
        shutdown_event.wait.side_effect = KeyboardInterrupt

        process_logger = mock.Mock()
        scheduler_logger = mock.Mock()
        with mock.patch.object(
            playback,
            "file_logger",
            side_effect=(process_logger, scheduler_logger),
        ), mock.patch.object(
            playback,
            "generate_event_list",
            return_value=[selected_event, ignored_event],
        ), mock.patch.object(
            playback,
            "EventTracker",
            return_value=tracker,
        ) as tracker_constructor, mock.patch.object(
            playback,
            "FollowUpScheduler",
            return_value=scheduler,
        ) as scheduler_constructor, mock.patch.object(
            playback,
            "EventAlertWSPlaybackManager",
            return_value=playback_manager,
        ) as playback_constructor, mock.patch.object(
            playback.threading,
            "Thread",
            return_value=scheduler_thread,
        ) as thread_constructor, mock.patch.object(
            playback.threading,
            "Event",
            return_value=shutdown_event,
        ), redirect_stdout(io.StringIO()):
            result = playback.run_cli(
                arguments,
                runtime_context=self.runtime_context,
            )

        self.assertEqual(result, 0)
        tracker_constructor.assert_called_once_with(
            str(self.runtime_context.database_path),
            logger=process_logger,
        )
        playback_constructor.assert_called_once_with(
            event_list=[selected_event],
            event_tracker=tracker,
            speedup_factor=1.0,
            default_services=["RRSM"],
            logger=process_logger,
            failure_callback=mock.ANY,
        )
        scheduler_arguments = scheduler_constructor.call_args.kwargs
        self.assertIs(scheduler_arguments["tracker"], tracker)
        self.assertIs(scheduler_arguments["logger"], scheduler_logger)
        self.assertEqual(
            scheduler_arguments["configuration"]["finder-executable"][
                "output-root-folder"
            ],
            str(self.runtime_context.work_root),
        )
        thread_arguments = thread_constructor.call_args.kwargs
        self.assertTrue(callable(thread_arguments["target"]))
        self.assertFalse(thread_arguments["daemon"])
        scheduler_thread.start.assert_called_once_with()
        playback_manager.start_auto.assert_called_once_with()
        playback_manager.stop.assert_called_once_with()
        scheduler.stop_and_drain.assert_called_once_with()
        playback_manager.join.assert_called_once_with()
        scheduler_thread.join.assert_called_once_with()
        scheduler.close.assert_called_once_with()
        shutdown_event.set.assert_called_once_with()
        self.assertEqual(self.runtime_context.database_allocations, 1)

    def test_interruption_drains_and_joins_before_database_removal(self):
        events = []
        runtime_context = RecordingRuntimeContext(
            self.runtime_directory.name,
            "playback",
            events,
        )
        arguments = cli.build_parser().parse_args(
            ["playback", "--event-id", "selected"]
        )
        tracker = mock.Mock()
        tracker.close.side_effect = lambda: events.append("tracker-close")
        scheduler = mock.Mock()
        scheduler.stop_and_drain.side_effect = lambda: events.append(
            "scheduler-drain"
        )
        scheduler.close.side_effect = lambda: (
            events.append("scheduler-close"),
            tracker.close(),
        )
        playback_manager = mock.Mock()
        playback_manager.stop.side_effect = lambda: events.append(
            "playback-stop"
        )
        playback_manager.join.side_effect = lambda: events.append(
            "playback-thread-join"
        )
        scheduler_thread = mock.Mock()
        scheduler_thread.start.side_effect = lambda: events.append(
            "scheduler-thread-start"
        )
        scheduler_thread.join.side_effect = lambda: events.append(
            "scheduler-thread-join"
        )
        shutdown_event = mock.Mock()
        shutdown_event.wait.side_effect = KeyboardInterrupt

        with mock.patch.object(
            playback,
            "file_logger",
            side_effect=(mock.Mock(), mock.Mock()),
        ), mock.patch.object(
            playback,
            "generate_event_list",
            return_value=[self.event("selected")],
        ), mock.patch.object(
            playback,
            "EventTracker",
            return_value=tracker,
        ), mock.patch.object(
            playback,
            "FollowUpScheduler",
            return_value=scheduler,
        ), mock.patch.object(
            playback,
            "EventAlertWSPlaybackManager",
            return_value=playback_manager,
        ), mock.patch.object(
            playback.threading,
            "Thread",
            return_value=scheduler_thread,
        ), mock.patch.object(
            playback.threading,
            "Event",
            return_value=shutdown_event,
        ), redirect_stdout(io.StringIO()):
            result = playback.run_cli(
                arguments,
                runtime_context=runtime_context,
            )

        self.assertEqual(result, 0)
        tracker.close.assert_called_once_with()
        self.assertLess(
            events.index("playback-stop"),
            events.index("scheduler-drain"),
        )
        self.assertLess(
            events.index("scheduler-drain"),
            events.index("playback-thread-join"),
        )
        self.assertLess(
            events.index("playback-thread-join"),
            events.index("scheduler-thread-join"),
        )
        self.assertLess(
            events.index("scheduler-thread-join"),
            events.index("tracker-close"),
        )
        self.assertLess(
            events.index("tracker-close"),
            events.index("database-removed"),
        )

    def test_playback_construction_failure_closes_tracker_before_removal(self):
        events = []
        runtime_context = RecordingRuntimeContext(
            self.runtime_directory.name,
            "playback",
            events,
        )
        arguments = cli.build_parser().parse_args(["playback"])
        original_error = RuntimeError("playback construction failed")
        tracker = mock.Mock()
        tracker.close.side_effect = lambda: events.append("tracker-close")

        with mock.patch.object(
            playback,
            "file_logger",
            side_effect=(mock.Mock(), mock.Mock()),
        ), mock.patch.object(
            playback,
            "generate_event_list",
            return_value=[self.event("selected")],
        ), mock.patch.object(
            playback,
            "EventTracker",
            return_value=tracker,
        ), mock.patch.object(
            playback,
            "EventAlertWSPlaybackManager",
            side_effect=original_error,
        ), mock.patch.object(
            playback,
            "FollowUpScheduler",
        ) as scheduler_constructor, mock.patch.object(
            playback.threading,
            "Thread",
        ) as thread_constructor:
            with self.assertRaises(RuntimeError) as raised:
                playback.run_cli(
                    arguments,
                    runtime_context=runtime_context,
                )

        self.assertIs(raised.exception, original_error)
        scheduler_constructor.assert_not_called()
        thread_constructor.assert_not_called()
        tracker.close.assert_called_once_with()
        self.assertLess(
            events.index("tracker-close"),
            events.index("database-removed"),
        )

    def test_playback_worker_failure_wakes_owner_and_propagates_after_cleanup(self):
        events = []
        runtime_context = RecordingRuntimeContext(
            self.runtime_directory.name,
            "playback",
            events,
        )
        arguments = cli.build_parser().parse_args(
            ["playback", "--event-id", "selected"]
        )
        original_error = RuntimeError("registration failed")
        registration_attempted = threading.Event()
        tracker = mock.Mock()

        def fail_registration(**kwargs):
            events.append("registration-failed")
            registration_attempted.set()
            raise original_error

        tracker.batch_register_from_policy.side_effect = fail_registration
        tracker.close.side_effect = lambda: events.append("tracker-close")

        scheduler = mock.Mock()
        scheduler_started = threading.Event()
        observed_command_events = []

        def run_scheduler(*, shutdown_event):
            observed_command_events.append(shutdown_event)
            scheduler_started.set()
            try:
                shutdown_event.wait(3)
            finally:
                events.append("scheduler-worker-ended")

        scheduler.run_forever.side_effect = run_scheduler
        scheduler.stop_and_drain.side_effect = lambda: events.append(
            "scheduler-drain"
        )
        scheduler.close.side_effect = lambda: (
            events.append("scheduler-close"),
            tracker.close(),
        )

        real_manager_type = playback.EventAlertWSPlaybackManager
        managers = []
        playback_threads = []

        def construct_manager(**kwargs):
            manager = real_manager_type(**kwargs)
            original_start = manager.start_auto
            original_stop = manager.stop
            original_join = manager.join

            def start():
                original_start()
                playback_threads.append(manager._thread)

            def stop():
                original_stop()
                events.append("playback-stop")

            def join():
                original_join()
                events.append("playback-thread-joined")

            manager.start_auto = mock.Mock(side_effect=start)
            manager.stop = mock.Mock(side_effect=stop)
            manager.join = mock.Mock(side_effect=join)
            managers.append(manager)
            return manager

        outcome = {}

        def invoke_command():
            try:
                outcome["result"] = playback.run_cli(
                    arguments,
                    runtime_context=runtime_context,
                )
            except BaseException as error:
                outcome["error"] = error

        owner_thread = threading.Thread(target=invoke_command)
        thread_exception = mock.Mock()
        with mock.patch.object(
            playback,
            "file_logger",
            side_effect=(mock.Mock(), mock.Mock()),
        ), mock.patch.object(
            playback,
            "generate_event_list",
            return_value=[self.complete_event("selected")],
        ), mock.patch.object(
            playback,
            "EventTracker",
            return_value=tracker,
        ), mock.patch.object(
            playback,
            "FollowUpScheduler",
            return_value=scheduler,
        ), mock.patch.object(
            playback,
            "EventAlertWSPlaybackManager",
            side_effect=construct_manager,
        ), mock.patch.object(
            threading,
            "excepthook",
            thread_exception,
        ), redirect_stdout(io.StringIO()):
            owner_thread.start()
            self.assertTrue(scheduler_started.wait(1))
            self.assertTrue(registration_attempted.wait(1))
            owner_thread.join(1)
            command_hung = owner_thread.is_alive()
            if command_hung:
                observed_command_events[0].set()
                owner_thread.join(2)

        self.assertFalse(command_hung, "playback failure did not wake command")
        self.assertFalse(owner_thread.is_alive())
        self.assertIs(outcome.get("error"), original_error)
        self.assertNotIn("result", outcome)
        self.assertEqual(len(managers), 1)
        manager = managers[0]
        self.assertIs(manager._worker_error, original_error)
        self.assertFalse(manager.running)
        self.assertTrue(manager._stop_event.is_set())
        self.assertIsNone(manager._thread)
        self.assertEqual(len(playback_threads), 1)
        self.assertFalse(playback_threads[0].is_alive())
        manager.stop.assert_called_once_with()
        manager.join.assert_called_once_with()
        scheduler.stop_and_drain.assert_called_once_with()
        scheduler.close.assert_called_once_with()
        tracker.close.assert_called_once_with()
        tracker.batch_register_from_policy.assert_called_once()
        thread_exception.assert_not_called()
        self.assertFalse(runtime_context.database_path.exists())
        self.assertLess(
            events.index("playback-stop"),
            events.index("scheduler-drain"),
        )
        self.assertLess(
            events.index("scheduler-drain"),
            events.index("playback-thread-joined"),
        )
        self.assertLess(
            events.index("scheduler-worker-ended"),
            events.index("tracker-close"),
        )
        self.assertLess(
            events.index("tracker-close"),
            events.index("database-removed"),
        )


if __name__ == "__main__":
    unittest.main()
