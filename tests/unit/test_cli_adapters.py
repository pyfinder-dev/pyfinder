"""Direct tests for the installed command's workflow adapters."""

import atexit
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
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


class OnDemandAdapterTests(unittest.TestCase):
    def setUp(self):
        self.original_log_name = findermanager.pyfinderconfig[
            "finder-executable"
        ]["log-file-name"]

    def tearDown(self):
        findermanager.pyfinderconfig["finder-executable"][
            "log-file-name"
        ] = self.original_log_name

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

        with mock.patch.object(
            findermanager.FinDerManager,
            "for_on_demand",
            return_value=manager,
        ) as constructor, redirect_stdout(output):
            result = findermanager.run_cli(arguments)

        self.assertEqual(result, 0)
        constructor.assert_called_once()
        constructor_arguments = constructor.call_args.kwargs
        self.assertIs(
            constructor_arguments["configuration"],
            findermanager.pyfinderconfig,
        )
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

    def test_explicit_test_mode_uses_configured_event_and_preserves_no_solution(self):
        arguments = cli.build_parser().parse_args(["on-demand", "--test"])
        configured_event_id = findermanager.pyfinderconfig["general"][
            "test-event-id"
        ]
        manager = mock.Mock()
        manager.run.return_value = None
        output = io.StringIO()

        with mock.patch.object(
            findermanager.FinDerManager,
            "for_on_demand",
            return_value=manager,
        ) as constructor, redirect_stdout(output):
            result = findermanager.run_cli(arguments)

        self.assertEqual(result, 0)
        options = constructor.call_args.kwargs["options"]
        self.assertTrue(options["test"])
        self.assertEqual(options["event_id"], configured_event_id)
        self.assertNotIn("log_file", options)
        manager.run.assert_called_once_with(event_id=configured_event_id)
        self.assertEqual(output.getvalue(), "No FinDer solution returned.\n")
        self.assertEqual(
            findermanager.pyfinderconfig["finder-executable"][
                "log-file-name"
            ],
            self.original_log_name,
        )


class PlaybackAdapterTests(unittest.TestCase):
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

        with mock.patch.object(
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
            result = playback.run_cli(arguments)

        self.assertEqual(result, 0)
        self.assertIn("Event ID: event-1, M5.1, Region: REGION ONE", output.getvalue())
        self.assertIn("Event ID: event-2, M6.2, Region: REGION TWO", output.getvalue())
        tracker_constructor.assert_not_called()
        scheduler_constructor.assert_not_called()
        playback_constructor.assert_not_called()
        thread_constructor.assert_not_called()

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

        with mock.patch.object(
            playback,
            "generate_event_list",
            return_value=[selected_event, ignored_event],
        ), mock.patch.object(
            playback.os.path,
            "exists",
            return_value=False,
        ), mock.patch.object(
            playback.os,
            "remove",
        ) as remove, mock.patch.object(
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
            result = playback.run_cli(arguments)

        self.assertEqual(result, 0)
        remove.assert_not_called()
        tracker_constructor.assert_called_once_with("test_playback.db")
        playback_constructor.assert_called_once_with(
            event_list=[selected_event],
            event_tracker=tracker,
            speedup_factor=1.0,
            default_services=["RRSM"],
        )
        scheduler_constructor.assert_called_once_with(tracker=tracker)
        thread_constructor.assert_called_once_with(
            target=scheduler.run_forever,
            daemon=True,
        )
        scheduler_thread.start.assert_called_once_with()
        playback_manager.start_auto.assert_called_once_with()
        playback_manager.pause.assert_called_once_with()
        scheduler.shutdown.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
