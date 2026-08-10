"""Tests for continuous service construction and controlled shutdown."""

from copy import deepcopy
import logging
import signal
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from pyfinder import cli, start_monitoring
from pyfinder.services import seismiclistener
from pyfinder.services.seismiclistener import EMSCListener


class DeferredThread:
    """Record thread ownership without running its target."""

    def __init__(self, target, daemon, events):
        self.target = target
        self.daemon = daemon
        self.events = events

    def start(self):
        self.events.append("listener-thread-start")

    def join(self):
        self.events.append("listener-thread-join")


class ImmediateThread(DeferredThread):
    """Run the target during start while retaining join observations."""

    def start(self):
        self.events.append("listener-thread-start")
        self.target()


class ContinuousLifecycleTests(unittest.TestCase):
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

    def tearDown(self):
        start_monitoring._listener = None
        start_monitoring._listener_thread = None
        start_monitoring._scheduler = None
        start_monitoring._shutdown_event = None
        start_monitoring._launcher_logger = None

    def test_installed_continuous_signal_shutdown_uses_owned_order(self):
        events = []
        logger = mock.Mock(spec=logging.Logger)
        listener = mock.Mock()
        scheduler = mock.Mock()
        handlers = {}
        previous_handlers = {
            signal.SIGTERM: object(),
            signal.SIGINT: object(),
        }
        logger.info.side_effect = lambda message, *args: (
            events.append("startup-success")
            if "listener and scheduler started" in message
            else None
        )

        listener.stop.side_effect = lambda: events.append("listener-stop")
        listener.close.side_effect = lambda: events.append("listener-close")
        scheduler.shutdown.side_effect = lambda: events.append(
            "scheduler-shutdown"
        )

        def build_policies():
            events.append("policy-validation")
            return {"RRSM": object()}

        def build_selector(*, logger):
            events.append("finder-configuration-validation")
            return object()

        def build_listener(**kwargs):
            events.append("listener-construction")
            return listener

        def build_scheduler(**kwargs):
            events.append("scheduler-construction")
            return scheduler

        def construct_thread(target, daemon):
            events.append("listener-thread-construction")
            return DeferredThread(target, daemon, events)

        def get_handler(signum):
            return previous_handlers[signum]

        def set_handler(signum, handler):
            if handler is start_monitoring._request_shutdown:
                handlers[signum] = handler
                events.append(("signal-handler-installed", signum))
            else:
                events.append(("signal-handler-restored", signum))

        def run_scheduler(*, shutdown_event):
            events.append("scheduler-loop")
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            self.assertTrue(shutdown_event.is_set())
            events.append("scheduler-loop-stopped")

        scheduler.run_forever.side_effect = run_scheduler
        runtime_context = self.runtime_context()

        with mock.patch.object(
            start_monitoring,
            "file_logger",
            return_value=logger,
        ), mock.patch.object(
            start_monitoring,
            "build_service_policies",
            side_effect=build_policies,
        ), mock.patch.object(
            start_monitoring,
            "build_default_selector",
            side_effect=build_selector,
        ), mock.patch.object(
            start_monitoring.seismiclistener,
            "build_emsc_listener",
            side_effect=build_listener,
        ), mock.patch.object(
            start_monitoring,
            "FollowUpScheduler",
            side_effect=build_scheduler,
        ), mock.patch.object(
            start_monitoring.threading,
            "Thread",
            side_effect=construct_thread,
        ), mock.patch.object(
            start_monitoring.signal,
            "getsignal",
            side_effect=get_handler,
        ), mock.patch.object(
            start_monitoring.signal,
            "signal",
            side_effect=set_handler,
        ), mock.patch.object(
            start_monitoring.sys,
            "exit",
        ) as interpreter_exit, mock.patch.object(
            cli,
            "bootstrap_runtime",
            return_value=runtime_context,
        ), mock.patch.object(
            cli.importlib,
            "import_module",
            return_value=start_monitoring,
        ):
            result = cli.main(["continuous"])

        self.assertEqual(result, 0)
        interpreter_exit.assert_not_called()
        self.assertLess(
            events.index("listener-construction"),
            events.index("scheduler-construction"),
        )
        self.assertLess(
            events.index("scheduler-construction"),
            events.index("listener-thread-start"),
        )
        self.assertLess(
            events.index("listener-thread-start"),
            events.index("startup-success"),
        )
        signal_stop = events.index("listener-stop")
        scheduler_stop = events.index("scheduler-shutdown")
        thread_join = events.index("listener-thread-join")
        listener_close = events.index("listener-close")
        self.assertLess(signal_stop, scheduler_stop)
        self.assertLess(scheduler_stop, thread_join)
        self.assertLess(thread_join, listener_close)
        self.assertEqual(
            [event for event in events if event == "scheduler-shutdown"],
            ["scheduler-shutdown"],
        )

    def test_scheduler_construction_failure_closes_unstarted_listener(self):
        events = []
        original_error = RuntimeError("scheduler construction failed")
        listener = mock.Mock()
        listener.stop.side_effect = lambda: events.append("listener-stop")
        listener.close.side_effect = lambda: events.append("listener-close")

        with mock.patch.object(
            start_monitoring,
            "file_logger",
            return_value=mock.Mock(spec=logging.Logger),
        ), mock.patch.object(
            start_monitoring,
            "build_service_policies",
            return_value={"RRSM": object()},
        ), mock.patch.object(
            start_monitoring,
            "build_default_selector",
            return_value=object(),
        ), mock.patch.object(
            start_monitoring.seismiclistener,
            "build_emsc_listener",
            return_value=listener,
        ), mock.patch.object(
            start_monitoring,
            "FollowUpScheduler",
            side_effect=original_error,
        ), mock.patch.object(
            start_monitoring.threading,
            "Thread",
        ) as thread_constructor:
            with self.assertRaises(RuntimeError) as raised:
                start_monitoring.start_services(
                    runtime_context=self.runtime_context()
                )

        self.assertIs(raised.exception, original_error)
        thread_constructor.assert_not_called()
        self.assertEqual(events, ["listener-stop", "listener-close"])

    def test_listener_start_failure_drains_scheduler_and_keeps_error_primary(self):
        events = []
        original_error = RuntimeError("listener start failed")
        listener = mock.Mock()
        scheduler = mock.Mock()
        listener.run.side_effect = original_error
        listener.stop.side_effect = lambda: events.append("listener-stop")
        listener.close.side_effect = lambda: events.append("listener-close")
        scheduler.shutdown.side_effect = lambda: events.append(
            "scheduler-shutdown"
        )

        def run_scheduler(*, shutdown_event):
            events.append("scheduler-loop")
            self.assertTrue(shutdown_event.is_set())

        scheduler.run_forever.side_effect = run_scheduler

        with mock.patch.object(
            start_monitoring,
            "file_logger",
            return_value=mock.Mock(spec=logging.Logger),
        ), mock.patch.object(
            start_monitoring,
            "build_service_policies",
            return_value={"RRSM": object()},
        ), mock.patch.object(
            start_monitoring,
            "build_default_selector",
            return_value=object(),
        ), mock.patch.object(
            start_monitoring.seismiclistener,
            "build_emsc_listener",
            return_value=listener,
        ), mock.patch.object(
            start_monitoring,
            "FollowUpScheduler",
            return_value=scheduler,
        ), mock.patch.object(
            start_monitoring.threading,
            "Thread",
            side_effect=lambda target, daemon: ImmediateThread(
                target, daemon, events
            ),
        ), mock.patch.object(
            start_monitoring,
            "_install_signal_handlers",
            return_value={},
        ):
            with self.assertRaises(RuntimeError) as raised:
                start_monitoring.start_services(
                    runtime_context=self.runtime_context()
                )

        self.assertIs(raised.exception, original_error)
        self.assertLess(
            events.index("scheduler-shutdown"),
            events.index("listener-thread-join"),
        )
        self.assertLess(
            events.index("listener-thread-join"),
            events.index("listener-close"),
        )

    def test_cleanup_failure_is_visible_without_replacing_startup_error(self):
        original_error = RuntimeError("scheduler construction failed")
        cleanup_error = OSError("listener stop failed")
        listener = mock.Mock()
        listener.stop.side_effect = cleanup_error

        with mock.patch.object(
            start_monitoring,
            "file_logger",
            return_value=mock.Mock(spec=logging.Logger),
        ), mock.patch.object(
            start_monitoring,
            "build_service_policies",
            return_value={"RRSM": object()},
        ), mock.patch.object(
            start_monitoring,
            "build_default_selector",
            return_value=object(),
        ), mock.patch.object(
            start_monitoring.seismiclistener,
            "build_emsc_listener",
            return_value=listener,
        ), mock.patch.object(
            start_monitoring,
            "FollowUpScheduler",
            side_effect=original_error,
        ):
            with self.assertRaises(RuntimeError) as raised:
                start_monitoring.start_services(
                    runtime_context=self.runtime_context()
                )

        self.assertIs(raised.exception, original_error)
        self.assertTrue(
            any("listener stop failed" in note for note in raised.exception.__notes__)
        )
        listener.close.assert_called_once_with()


class ListenerOwnershipTests(unittest.TestCase):
    def test_stop_interrupts_loop_and_close_is_idempotent(self):
        listener = EMSCListener.__new__(EMSCListener)
        listener._stop_event = threading.Event()
        listener._lifecycle_lock = threading.Lock()
        listener._ioloop = mock.Mock()
        listener._tracker = mock.Mock()
        listener._closed = False

        listener.stop()
        listener.close()
        listener.close()

        self.assertTrue(listener._stop_event.is_set())
        listener._ioloop.add_callback.assert_called_once_with(
            listener._ioloop.stop
        )
        listener._tracker.close.assert_called_once_with()

    def test_requested_stop_prevents_another_read_or_reconnect(self):
        stop_event = threading.Event()
        stop_event.set()
        websocket = mock.Mock()
        processor = mock.Mock()

        read_loop = seismiclistener.listen(
            websocket,
            processor,
            mock.Mock(),
            stop_event,
        )
        with self.assertRaises(StopIteration):
            next(read_loop)

        reconnect_loop = seismiclistener.launch_client(
            "wss://example.invalid",
            30,
            processor,
            mock.Mock(),
            mock.Mock(),
            mock.Mock(),
            mock.Mock(),
            stop_event,
        )
        with self.assertRaises(StopIteration):
            next(reconnect_loop)

        websocket.read_message.assert_not_called()
        processor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
