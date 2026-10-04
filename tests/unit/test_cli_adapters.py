"""Confined command composition tests; no provider, native binary or SMTP use."""

from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timezone
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from pyfinder import playback, runtime


class PlaybackAdapterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        for name in ("state", "logs", "runs", "playbacks"):
            (root / name).mkdir()
        self.context = runtime.build_runtime_context("playback", service_root=root)
        self.arguments = SimpleNamespace(
            event_ids=["arbitrary-event"],
            full_schedule=False,
            fast=False,
            list_events=False,
            verbosity="INFO",
        )

    def patches(self, stack):
        doubles = {}
        for name in (
            "file_logger", "EventTracker", "FollowUpScheduler", "build_notifier",
            "build_shakemap_boundary", "configured_alert_settings",
            "register_playback",
        ):
            doubles[name] = stack.enter_context(mock.patch.object(playback, name))
        doubles["configured_alert_settings"].return_value = None
        doubles["build_shakemap_boundary"].return_value = (mock.Mock(), mock.Mock())
        doubles["FollowUpScheduler"].return_value.run_until_complete.return_value = 0
        stack.enter_context(mock.patch.object(
            playback, "continuous_shakemap_configuration",
            return_value={"shakemap": {"service-enabled": True}},
        ))
        stack.enter_context(redirect_stdout(io.StringIO()))
        return doubles

    def test_list_never_constructs_scheduler_or_remote_services(self):
        self.arguments.list_events = True
        with ExitStack() as stack:
            doubles = self.patches(stack)
            self.assertEqual(playback.run_cli(self.arguments, runtime_context=self.context), 0)
        for name in (
            "FollowUpScheduler", "build_shakemap_boundary",
            "configured_alert_settings", "EventTracker",
        ):
            doubles[name].assert_not_called()

    def test_provider_entry_gets_full_workflow_and_persistent_private_database(self):
        with ExitStack() as stack:
            doubles = self.patches(stack)
            self.assertEqual(playback.run_cli(self.arguments, runtime_context=self.context), 0)
        settings = doubles["FollowUpScheduler"].call_args.kwargs
        self.assertTrue(settings["provider_backed"])
        self.assertFalse(settings["finder_options"]["with_seiscomp"])
        self.assertIs(settings["shakemap_notifier"], doubles["build_notifier"].return_value)
        self.assertEqual(doubles["register_playback"].call_args.args[1], ["arbitrary-event"])
        doubles["FollowUpScheduler"].return_value.shutdown.assert_called_once()
        self.assertTrue(self.context.operational_database_path.parent.exists())

    def test_default_events_and_fast_full_schedule_are_passed_to_registration(self):
        self.arguments.event_ids = None
        self.arguments.full_schedule = self.arguments.fast = True
        with ExitStack() as stack:
            doubles = self.patches(stack)
            playback.run_cli(self.arguments, runtime_context=self.context)
        self.assertEqual(doubles["register_playback"].call_args.args[1], playback.DEFAULT_EVENT_IDS)
        self.assertEqual(doubles["register_playback"].call_args.kwargs, {"full_schedule": True, "fast": True})

    def test_disabled_shakemap_is_a_visible_startup_error(self):
        with ExitStack() as stack:
            doubles = self.patches(stack)
            stack.enter_context(mock.patch.object(playback, "continuous_shakemap_configuration", return_value={}))
            with self.assertRaisesRegex(runtime.RuntimeBootstrapError, "requires the full chain"):
                playback.run_cli(self.arguments, runtime_context=self.context)
        doubles["EventTracker"].assert_not_called()

    def test_scheduler_construction_failure_closes_all_untransferred_resources(self):
        with ExitStack() as stack:
            doubles = self.patches(stack)
            doubles["FollowUpScheduler"].side_effect = RuntimeError("construction failed")
            with self.assertRaisesRegex(RuntimeError, "construction failed"):
                playback.run_cli(self.arguments, runtime_context=self.context)
        workflow, _inputs = doubles["build_shakemap_boundary"].return_value
        workflow.close.assert_called_once()
        doubles["build_notifier"].return_value.close.assert_called_once()
        doubles["EventTracker"].return_value.close.assert_called_once()

    def test_failure_or_interruption_drains_scheduler_and_preserves_exit_code(self):
        for result in (1, 130):
            with self.subTest(result=result), ExitStack() as stack:
                doubles = self.patches(stack)
                scheduler = doubles["FollowUpScheduler"].return_value
                scheduler.run_until_complete.return_value = result
                self.assertEqual(playback.run_cli(self.arguments, runtime_context=self.context), result)
                doubles["FollowUpScheduler"].return_value.shutdown.assert_called_once()
