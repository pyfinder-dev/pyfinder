"""Unit tests for logged policy validation at service startup boundaries."""

import socket
import sqlite3
import sys
import unittest
from unittest import mock

from pyfinder import start_monitoring
from pyfinder.services import eventtracker
from pyfinder.services import querypolicy
from pyfinder.services import scheduler
from pyfinder.services import seismiclistener


class ImmediateThread:
    """Run a mocked thread target synchronously without creating a thread."""

    def __init__(self, target, daemon):
        self.target = target
        self.daemon = daemon

    def start(self):
        self.target()


class PolicyStartupTests(unittest.TestCase):
    def setUp(self):
        start_monitoring._listener_thread = None
        start_monitoring._scheduler = None

    def tearDown(self):
        start_monitoring._listener_thread = None
        start_monitoring._scheduler = None

    def test_full_startup_logs_validation_failure_before_operational_resources(self):
        events = []
        logger = mock.Mock()
        config_module_before = sys.modules.get("pyfinder.utils.config_fetcher")
        tornado_modules_before = {
            name: sys.modules.get(name)
            for name in ("tornado", "tornado.ioloop", "tornado.websocket")
        }

        def configure_logger(*args, **kwargs):
            events.append("logger")
            return logger

        def fail_validation():
            events.append("policy")
            raise ValueError("invalid startup policy")

        with mock.patch.object(
            start_monitoring, "file_logger", side_effect=configure_logger
        ), mock.patch.object(
            start_monitoring,
            "build_service_policies",
            side_effect=fail_validation,
        ), mock.patch.object(
            start_monitoring.threading, "Thread", autospec=True
        ) as thread_constructor, mock.patch.object(
            start_monitoring, "FollowUpScheduler", autospec=True
        ) as scheduler_constructor, mock.patch.object(
            seismiclistener, "start_emsc_listener", autospec=True
        ) as listener_start, mock.patch.object(
            scheduler, "EventTracker", autospec=True
        ) as tracker_constructor, mock.patch.object(
            scheduler, "ThreadPoolExecutor", autospec=True
        ) as executor_constructor, mock.patch.object(
            sqlite3, "connect", autospec=True
        ) as database_connect, mock.patch.object(
            socket, "socket", autospec=True
        ) as socket_constructor:
            with self.assertRaisesRegex(ValueError, "invalid startup policy"):
                start_monitoring.start_services()

        self.assertEqual(events, ["logger", "policy"])
        logger.exception.assert_called_once()
        thread_constructor.assert_not_called()
        scheduler_constructor.assert_not_called()
        listener_start.assert_not_called()
        tracker_constructor.assert_not_called()
        executor_constructor.assert_not_called()
        database_connect.assert_not_called()
        socket_constructor.assert_not_called()
        self.assertIs(
            sys.modules.get("pyfinder.utils.config_fetcher"),
            config_module_before,
        )
        for module_name, previous_module in tornado_modules_before.items():
            self.assertIs(sys.modules.get(module_name), previous_module)

    def test_successful_full_startup_reuses_one_validated_policy_registry(self):
        events = []
        logger = mock.Mock()
        rrsm_policy = object()
        service_policies = {
            "RRSM": rrsm_policy,
            "ESM": object(),
            "EMSC": object(),
        }
        scheduler_instance = mock.Mock()

        def configure_logger(*args, **kwargs):
            events.append("logger")
            return logger

        def build_policies():
            events.append("policy")
            return service_policies

        with mock.patch.object(
            start_monitoring, "file_logger", side_effect=configure_logger
        ), mock.patch.object(
            start_monitoring,
            "build_service_policies",
            side_effect=build_policies,
        ), mock.patch.object(
            start_monitoring.threading,
            "Thread",
            side_effect=ImmediateThread,
        ), mock.patch.object(
            start_monitoring.seismiclistener,
            "start_emsc_listener",
            autospec=True,
        ) as listener_start, mock.patch.object(
            start_monitoring,
            "FollowUpScheduler",
            autospec=True,
            return_value=scheduler_instance,
        ) as scheduler_constructor:
            start_monitoring.start_services()

        self.assertEqual(events, ["logger", "policy"])
        self.assertIs(listener_start.call_args.kwargs["policy"], rrsm_policy)
        self.assertIs(
            scheduler_constructor.call_args.kwargs["service_policies"],
            service_policies,
        )
        scheduler_instance.run_forever.assert_called_once_with()

    def test_standalone_listener_logs_policy_failure_before_runtime_startup(self):
        events = []
        logger = mock.Mock()
        tornado_modules_before = {
            name: sys.modules.get(name)
            for name in ("tornado", "tornado.ioloop", "tornado.websocket")
        }

        def configure_logger(*args, **kwargs):
            events.append("logger")
            return logger

        def fail_validation():
            events.append("policy")
            raise ValueError("invalid listener policy")

        with mock.patch(
            "pyfinder.utils.customlogger.file_logger",
            side_effect=configure_logger,
        ), mock.patch.object(
            querypolicy,
            "RRSMQueryPolicy",
            side_effect=fail_validation,
        ), mock.patch.object(
            eventtracker, "EventTracker", autospec=True
        ) as tracker_constructor, mock.patch.object(
            sqlite3, "connect", autospec=True
        ) as database_connect, mock.patch.object(
            socket, "socket", autospec=True
        ) as socket_constructor:
            with self.assertRaisesRegex(ValueError, "invalid listener policy"):
                seismiclistener.start_emsc_listener()

        self.assertEqual(events, ["logger", "policy"])
        logger.exception.assert_called_once()
        tracker_constructor.assert_not_called()
        database_connect.assert_not_called()
        socket_constructor.assert_not_called()
        for module_name, previous_module in tornado_modules_before.items():
            self.assertIs(sys.modules.get(module_name), previous_module)

    def test_standalone_scheduler_logs_policy_failure_before_resources(self):
        events = []
        logger = mock.Mock()
        config_module_before = sys.modules.get("pyfinder.utils.config_fetcher")

        def configure_logger():
            events.append("logger")
            return logger

        def fail_validation():
            events.append("policy")
            raise ValueError("invalid scheduler policy")

        with mock.patch.object(
            scheduler.FollowUpScheduler,
            "_setup_file_logger",
            side_effect=configure_logger,
        ), mock.patch.object(
            scheduler,
            "build_service_policies",
            side_effect=fail_validation,
        ), mock.patch.object(
            scheduler, "EventTracker", autospec=True
        ) as tracker_constructor, mock.patch.object(
            scheduler, "ThreadPoolExecutor", autospec=True
        ) as executor_constructor, mock.patch.object(
            sqlite3, "connect", autospec=True
        ) as database_connect:
            with self.assertRaisesRegex(ValueError, "invalid scheduler policy"):
                scheduler.FollowUpScheduler()

        self.assertEqual(events, ["logger", "policy"])
        logger.exception.assert_called_once()
        tracker_constructor.assert_not_called()
        executor_constructor.assert_not_called()
        database_connect.assert_not_called()
        self.assertIs(
            sys.modules.get("pyfinder.utils.config_fetcher"),
            config_module_before,
        )


if __name__ == "__main__":
    unittest.main()
