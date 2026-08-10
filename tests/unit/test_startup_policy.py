"""Unit tests for logged policy validation at service startup boundaries."""

from copy import deepcopy
import socket
import sqlite3
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

from pyfinder import start_monitoring
from pyfinder.finderconfigs import GlobalFinderConfigError
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

    def join(self):
        pass


class PolicyStartupTests(unittest.TestCase):
    @staticmethod
    def runtime_context():
        return SimpleNamespace(
            process_log_path="/runtime/logs/continuous/monitoring.log",
            listener_log_path=(
                "/runtime/logs/continuous/seismiclistener.log"
            ),
            scheduler_log_path=(
                "/runtime/logs/continuous/followupscheduler.log"
            ),
            operational_database_path=(
                "/runtime/state/scheduled_queries.sqlite3"
            ),
            isolated_configuration=deepcopy,
        )

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
            start_monitoring,
            "build_default_selector",
            autospec=True,
        ) as selector_builder, mock.patch.object(
            start_monitoring.threading, "Thread", autospec=True
        ) as thread_constructor, mock.patch.object(
            start_monitoring, "FollowUpScheduler", autospec=True
        ) as scheduler_constructor, mock.patch.object(
            seismiclistener, "build_emsc_listener", autospec=True
        ) as listener_constructor, mock.patch.object(
            scheduler, "EventTracker", autospec=True
        ) as tracker_constructor, mock.patch.object(
            scheduler, "ThreadPoolExecutor", autospec=True
        ) as executor_constructor, mock.patch.object(
            sqlite3, "connect", autospec=True
        ) as database_connect, mock.patch.object(
            socket, "socket", autospec=True
        ) as socket_constructor:
            with self.assertRaisesRegex(ValueError, "invalid startup policy"):
                start_monitoring.start_services(
                    runtime_context=self.runtime_context()
                )

        self.assertEqual(events, ["logger", "logger", "logger", "policy"])
        logger.exception.assert_called_once()
        selector_builder.assert_not_called()
        thread_constructor.assert_not_called()
        scheduler_constructor.assert_not_called()
        listener_constructor.assert_not_called()
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
        listener_instance = mock.Mock()
        finder_config_selector = object()

        def configure_logger(*args, **kwargs):
            events.append("logger")
            return logger

        def build_policies():
            events.append("policy")
            return service_policies

        def build_selector(*, logger):
            events.append("selector")
            self.assertIs(logger, startup_logger)
            return finder_config_selector

        def construct_thread(target, daemon):
            events.append("listener-thread")
            return ImmediateThread(target=target, daemon=daemon)

        def construct_listener(**kwargs):
            events.append("listener-construction")
            listener_instance.run.side_effect = lambda: events.append(
                "listener"
            )
            return listener_instance

        def construct_scheduler(**kwargs):
            events.append("scheduler")
            return scheduler_instance

        startup_logger = logger

        with mock.patch.object(
            start_monitoring, "file_logger", side_effect=configure_logger
        ), mock.patch.object(
            start_monitoring,
            "build_service_policies",
            side_effect=build_policies,
        ), mock.patch.object(
            start_monitoring,
            "build_default_selector",
            side_effect=build_selector,
        ) as selector_builder, mock.patch.object(
            start_monitoring.threading,
            "Thread",
            side_effect=construct_thread,
        ), mock.patch.object(
            start_monitoring.seismiclistener,
            "build_emsc_listener",
            side_effect=construct_listener,
        ) as listener_constructor, mock.patch.object(
            start_monitoring,
            "FollowUpScheduler",
            side_effect=construct_scheduler,
        ) as scheduler_constructor:
            start_monitoring.start_services(
                runtime_context=self.runtime_context()
            )

        self.assertEqual(
            events,
            [
                "logger",
                "logger",
                "logger",
                "policy",
                "selector",
                "listener-construction",
                "scheduler",
                "listener-thread",
                "listener",
            ],
        )
        selector_builder.assert_called_once_with(logger=logger)
        self.assertIs(
            listener_constructor.call_args.kwargs["policy"], rrsm_policy
        )
        self.assertIs(
            scheduler_constructor.call_args.kwargs["service_policies"],
            service_policies,
        )
        self.assertIs(
            scheduler_constructor.call_args.kwargs[
                "finder_config_selector"
            ],
            finder_config_selector,
        )
        scheduler_instance.run_forever.assert_called_once_with(
            shutdown_event=mock.ANY
        )
        listener_instance.stop.assert_called_once_with()
        scheduler_instance.shutdown.assert_called_once_with()
        listener_instance.close.assert_called_once_with()

    def test_full_startup_global_error_aborts_before_operational_resources(self):
        events = []
        logger = mock.Mock()
        error = GlobalFinderConfigError("global unusable")
        config_module_before = sys.modules.get("pyfinder.utils.config_fetcher")
        tornado_modules_before = {
            name: sys.modules.get(name)
            for name in ("tornado", "tornado.ioloop", "tornado.websocket")
        }

        def configure_logger(*args, **kwargs):
            events.append("logger")
            return logger

        def build_policies():
            events.append("policy")
            return {"RRSM": object()}

        def fail_selector(*, logger):
            events.append("selector")
            raise error

        with mock.patch.object(
            start_monitoring,
            "file_logger",
            side_effect=configure_logger,
        ), mock.patch.object(
            start_monitoring,
            "build_service_policies",
            side_effect=build_policies,
        ), mock.patch.object(
            start_monitoring,
            "build_default_selector",
            side_effect=fail_selector,
        ) as selector_builder, mock.patch.object(
            start_monitoring.threading,
            "Thread",
            autospec=True,
        ) as thread_constructor, mock.patch.object(
            start_monitoring,
            "FollowUpScheduler",
            autospec=True,
        ) as scheduler_constructor, mock.patch.object(
            seismiclistener,
            "build_emsc_listener",
            autospec=True,
        ) as listener_constructor, mock.patch.object(
            scheduler,
            "EventTracker",
            autospec=True,
        ) as tracker_constructor, mock.patch.object(
            scheduler,
            "ThreadPoolExecutor",
            autospec=True,
        ) as executor_constructor, mock.patch.object(
            sqlite3,
            "connect",
            autospec=True,
        ) as database_connect, mock.patch.object(
            socket,
            "socket",
            autospec=True,
        ) as socket_constructor:
            with self.assertRaises(GlobalFinderConfigError) as raised:
                start_monitoring.start_services(
                    runtime_context=self.runtime_context()
                )

        self.assertIs(raised.exception, error)
        self.assertEqual(
            events,
            ["logger", "logger", "logger", "policy", "selector"],
        )
        selector_builder.assert_called_once_with(logger=logger)
        logger.critical.assert_called_once()
        self.assertIs(logger.critical.call_args.kwargs["exc_info"], True)
        thread_constructor.assert_not_called()
        scheduler_constructor.assert_not_called()
        listener_constructor.assert_not_called()
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
        ) as file_logger, mock.patch.object(
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
                seismiclistener.start_emsc_listener(logger=logger)

        self.assertEqual(events, ["policy"])
        file_logger.assert_not_called()
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
            scheduler,
            "build_default_selector",
            autospec=True,
        ) as selector_builder, mock.patch.object(
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
        selector_builder.assert_not_called()
        tracker_constructor.assert_not_called()
        executor_constructor.assert_not_called()
        database_connect.assert_not_called()
        self.assertIs(
            sys.modules.get("pyfinder.utils.config_fetcher"),
            config_module_before,
        )


if __name__ == "__main__":
    unittest.main()
