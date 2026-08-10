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
        ) as constructor, redirect_stdout(output):
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
            playback.time,
            "sleep",
            side_effect=KeyboardInterrupt,
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
        thread_constructor.assert_called_once_with(
            target=scheduler.run_forever,
            daemon=True,
        )
        scheduler_thread.start.assert_called_once_with()
        playback_manager.start_auto.assert_called_once_with()
        playback_manager.pause.assert_called_once_with()
        scheduler.shutdown.assert_called_once_with()
        self.assertEqual(self.runtime_context.database_allocations, 1)


if __name__ == "__main__":
    unittest.main()
