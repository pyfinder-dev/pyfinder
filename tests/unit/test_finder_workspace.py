"""Offline tests for augmented FinDer workspace identity and retention."""

import atexit
import builtins
from copy import deepcopy
import fcntl
import json
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


_PARAMWS_LOG_DIRECTORY = tempfile.TemporaryDirectory(
    prefix="pyfinder-workspace-import-"
)
atexit.register(_PARAMWS_LOG_DIRECTORY.cleanup)
_original_paramws_log_file = os.environ.get("PARAMWS_LOG_FILE")
os.environ["PARAMWS_LOG_FILE"] = str(
    Path(_PARAMWS_LOG_DIRECTORY.name) / "paramws.log"
)
try:
    from pyfinder import finderexec
    from pyfinder.pyfinderconfig import pyfinderconfig
    from pyfinder.workspace import (
        WorkspaceIdentityError,
        build_augmented_event_id,
        select_workspace_path,
    )
finally:
    if _original_paramws_log_file is None:
        os.environ.pop("PARAMWS_LOG_FILE", None)
    else:
        os.environ["PARAMWS_LOG_FILE"] = _original_paramws_log_file


def _hold_lock_until_released(lock_file_path, connection):
    """Hold one workspace lock in a separate interpreter process."""
    try:
        with open(lock_file_path, "a", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            connection.send("locked")
            connection.recv()
    finally:
        connection.close()


def _write_controlled_executable(
    path,
    *,
    stdout,
    stderr,
    returncode,
):
    """Create a real child that records the FinDer arguments it receives."""
    source = """#!{python}
import json
from pathlib import Path
import sys

workspace = Path(sys.argv[2])
(workspace / "controlled-child-arguments.json").write_text(
    json.dumps(sys.argv[1:]),
    encoding="utf-8",
)
sys.stdout.buffer.write({stdout!r})
sys.stdout.buffer.flush()
sys.stderr.buffer.write({stderr!r})
sys.stderr.buffer.flush()
raise SystemExit({returncode})
""".format(
        python=sys.executable,
        stdout=stdout,
        stderr=stderr,
        returncode=returncode,
    )
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


class FinDerWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory(
            prefix="pyfinder-workspace-"
        )
        self.temporary_root = Path(self.temporary_directory.name)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def executable(self, work_root, logger=None):
        configuration = deepcopy(pyfinderconfig)
        configuration["finder-executable"]["path"] = "/not-invoked/finder_run"
        configuration["finder-executable"]["output-root-folder"] = str(
            work_root
        )
        process_logger = logger
        if process_logger is None:
            process_logger = mock.Mock()
            process_logger.name = "workspace-test-process"
        return finderexec.FinDerExecutable(
            options={"command_line_args": "workspace-test"},
            configuration=configuration,
            finder_configuration_name="global",
            finder_configuration={
                "DATA_FOLDER": "unused",
                "MODEL": "unchanged",
            },
            logger=process_logger,
        )

    def assert_workspace_locked(self, workspace):
        lock_file_path = Path(workspace) / ".pyfinder.lock"
        with open(lock_file_path, "a", encoding="utf-8") as probe:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(
                    probe.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )

    def assert_workspace_available(self, workspace):
        lock_file_path = Path(workspace) / ".pyfinder.lock"
        with open(lock_file_path, "a", encoding="utf-8") as probe:
            fcntl.flock(
                probe.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
            fcntl.flock(probe.fileno(), fcntl.LOCK_UN)

    def assert_execution_cleanup(
        self,
        executable,
        workspace,
        process_logger,
        transient_handlers,
    ):
        """Verify the observable logger and lock cleanup for one invocation."""
        self.assertIs(executable.logger, process_logger)
        self.assert_workspace_available(workspace)
        self.assertEqual(len(transient_handlers), 1)
        self.assertIsNone(transient_handlers[0].stream)

    @staticmethod
    def logged_messages(logger, method_name):
        messages = []
        for call in getattr(logger, method_name).call_args_list:
            message = call.args[0]
            if len(call.args) > 1:
                message = message % call.args[1:]
            messages.append(message)
        return messages

    def test_augmented_identity_uses_only_event_and_five_digit_delay(self):
        cases = (
            ("event-one", 10, "event-one_t00010"),
            ("event-one", 0, "event-one_t00000"),
            ("event-one", None, "event-one_t00000"),
            ("event-one", "15", "event-one_t00015"),
        )
        for event_id, delay, expected in cases:
            with self.subTest(delay=delay):
                self.assertEqual(
                    build_augmented_event_id(event_id, delay),
                    expected,
                )

    def test_safe_selection_preserves_the_exact_valid_identity(self):
        work_root = self.temporary_root / "runs"
        work_root.mkdir()
        identity = "Event.One-2_2026_t00010"

        workspace = select_workspace_path(work_root, identity)

        self.assertEqual(workspace, work_root / identity)
        self.assertEqual(workspace.name, identity)

    def test_unsafe_identities_fail_without_workspace_creation(self):
        work_root = self.temporary_root / "runs"
        cases = (
            "",
            "/absolute_t00000",
            "../escape_t00000",
            "nested/event_t00000",
            r"nested\event_t00000",
            ".",
            "..",
            "C:\\outside_t00000",
            "event\x00_t00000",
        )
        for identity in cases:
            with self.subTest(identity=repr(identity)):
                with self.assertRaises(WorkspaceIdentityError):
                    select_workspace_path(work_root, identity)
                self.assertFalse(work_root.exists())

    def test_existing_workspace_symlink_cannot_escape_the_work_root(self):
        work_root = self.temporary_root / "runs"
        outside = self.temporary_root / "outside"
        work_root.mkdir()
        outside.mkdir()
        link = work_root / "event-one_t00010"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (NotImplementedError, OSError) as error:
            self.skipTest("directory symlinks are unavailable: {0}".format(error))

        with self.assertRaisesRegex(
            WorkspaceIdentityError,
            "escapes the configured work root",
        ):
            select_workspace_path(work_root, link.name)

    def test_workspace_lock_is_process_exclusive_and_path_local(self):
        first_workspace = self.temporary_root / "runs" / "first_t00010"
        second_workspace = self.temporary_root / "runs" / "second_t00010"
        first_workspace.mkdir(parents=True)
        second_workspace.mkdir(parents=True)
        first_lock = first_workspace / ".pyfinder.lock"
        second_lock = second_workspace / ".pyfinder.lock"
        process_context = multiprocessing.get_context("spawn")
        parent_connection, child_connection = process_context.Pipe()
        holder = process_context.Process(
            target=_hold_lock_until_released,
            args=(str(first_lock), child_connection),
        )
        released = False
        holder.start()
        child_connection.close()

        try:
            self.assertTrue(
                parent_connection.poll(5),
                "child process did not acquire the workspace lock",
            )
            self.assertEqual(parent_connection.recv(), "locked")
            self.assert_workspace_locked(first_workspace)
            self.assert_workspace_available(second_workspace)

            parent_connection.send("release")
            released = True
            holder.join(5)
            self.assertFalse(
                holder.is_alive(),
                "child process did not release the workspace lock",
            )
            self.assertEqual(holder.exitcode, 0)
            self.assert_workspace_available(first_workspace)
        finally:
            if not released and holder.is_alive():
                parent_connection.send("release")
                holder.join(1)
            if holder.is_alive():
                holder.terminate()
                holder.join(1)
            parent_connection.close()

    def test_repeated_preparation_reuses_directory_and_preserves_all_content(self):
        work_root = self.temporary_root / "runs"
        executable = self.executable(work_root)
        identity = build_augmented_event_id("event-one", 10)

        def write_data(_amplitudes, _event_data):
            attempt = write_data.attempt
            write_data.attempt += 1
            executable.logger.info("retry marker %s", attempt)
            data_path = Path(executable.get_working_directory()) / "data_0"
            data_path.write_bytes(b"current input")
            companion_path = (
                Path(executable.get_working_directory())
                / "pyfinder_amplitudes_to_Finder.txt"
            )
            companion_path.write_bytes(
                "companion attempt {0}".format(attempt).encode("ascii")
            )
            return str(data_path), finderexec.FinderChannelList()

        write_data.attempt = 1

        with mock.patch.object(
            executable,
            "_write_data_for_finder",
            side_effect=write_data,
        ):
            executable.materialize_inputs(
                [],
                mock.Mock(),
                augmented_event_id=identity,
            )
            first_workspace = Path(executable.get_working_directory())
            companion = (
                first_workspace / "pyfinder_amplitudes_to_Finder.txt"
            )
            self.assertEqual(
                companion.read_bytes(),
                b"companion attempt 1",
            )
            sentinel = first_workspace / "operator-sentinel.txt"
            finder_output = (
                first_workspace
                / "temp_data"
                / "returned-finder-id"
                / "core_info_0"
            )
            sentinel.write_text("retain me", encoding="utf-8")
            finder_output.parent.mkdir(parents=True)
            finder_output.write_text("finder output", encoding="utf-8")

            executable.materialize_inputs(
                [],
                mock.Mock(),
                augmented_event_id=identity,
            )
        second_workspace = Path(executable.get_working_directory())

        self.assertEqual(second_workspace, first_workspace)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "retain me")
        self.assertEqual(
            finder_output.read_text(encoding="utf-8"),
            "finder output",
        )
        self.assertEqual(companion.read_bytes(), b"companion attempt 2")
        self.assertTrue((second_workspace / "finder_file.config").is_file())
        self.assertTrue((second_workspace / ".pyfinder.lock").is_file())
        event_log = (second_workspace / "pyfinder.log").read_text(
            encoding="utf-8"
        )
        self.assertIn("retry marker 1", event_log)
        self.assertIn("retry marker 2", event_log)

    def test_different_delays_select_different_event_directories(self):
        work_root = self.temporary_root / "runs"
        executable = self.executable(work_root)

        executable._prepare_workspace(
            build_augmented_event_id("event-one", 0)
        )
        zero_delay = Path(executable.get_working_directory())
        executable._prepare_workspace(
            build_augmented_event_id("event-one", 10)
        )
        ten_minute_delay = Path(executable.get_working_directory())

        self.assertEqual(zero_delay.name, "event-one_t00000")
        self.assertEqual(ten_minute_delay.name, "event-one_t00010")
        self.assertNotEqual(zero_delay, ten_minute_delay)
        self.assertTrue(zero_delay.is_dir())
        self.assertTrue(ten_minute_delay.is_dir())

    def test_unsafe_identity_fails_before_resource_config_or_subprocess_work(self):
        work_root = self.temporary_root / "runs"
        executable = self.executable(work_root)
        event_data = mock.Mock()

        with mock.patch.object(
            executable,
            "_check_finder_executable",
        ) as check_executable, mock.patch.object(
            executable,
            "_prepare_workspace",
        ) as prepare_workspace, mock.patch.object(
            executable,
            "_write_finder_configuration",
        ) as write_configuration, mock.patch.object(
            finderexec.subprocess,
            "Popen",
        ) as process_constructor, mock.patch.object(
            finderexec.os,
            "makedirs",
        ) as make_directories:
            with self.assertRaises(WorkspaceIdentityError):
                executable.execute(
                    amplitudes=[],
                    event_data=event_data,
                    augmented_event_id="../escape_t00000",
                )

        check_executable.assert_not_called()
        prepare_workspace.assert_not_called()
        write_configuration.assert_not_called()
        process_constructor.assert_not_called()
        make_directories.assert_not_called()
        event_data.get_event_id.assert_not_called()

    def test_input_materialization_opens_all_invocation_artifacts_under_lock(self):
        work_root = self.temporary_root / "runs"
        executable = self.executable(work_root)
        event_data = mock.Mock()
        event_data.get_latitude.return_value = 0.0
        event_data.get_longitude.return_value = 0.0
        event_data.get_depth.return_value = 10.0
        event_data.get_magnitude.return_value = 5.6
        event_data.get_origin_time.return_value = (
            "2026-08-10T08:15:30.250000Z"
        )
        records = [
            {
                "latitude": 0.0,
                "longitude": 1.0,
                "network": "CH",
                "station": "TEST",
                "location": "00",
                "channel": "HNZ",
                "pga": 12.5,
                "timestamp": 1786349730.25,
                "source": "RRSM",
                "provider_value": 12.5,
                "provider_unit": "cm/s^2",
            }
        ]
        identity = "event-one_t00010"
        workspace = work_root / identity
        original_open = builtins.open
        opened_invocation_inputs = []

        def assert_input_open_under_lock(file, *args, **kwargs):
            try:
                opened_path = Path(file)
            except TypeError:
                return original_open(file, *args, **kwargs)
            if (
                opened_path.parent == workspace
                and opened_path.name in {
                    "finder_file.config",
                    "data_0",
                    "pyfinder_amplitudes_to_Finder.txt",
                }
            ):
                self.assert_workspace_locked(workspace)
                opened_invocation_inputs.append(opened_path.name)
            return original_open(file, *args, **kwargs)

        with mock.patch.object(
            finderexec,
            "get_epoch_time",
            return_value=1786349730.25,
        ), mock.patch.object(
            finderexec.Calculator,
            "predict_PGA_from_magnitude",
            return_value=1.0,
        ), mock.patch.object(
            builtins,
            "open",
            side_effect=assert_input_open_under_lock,
        ):
            materialized_paths = executable.materialize_inputs(
                records,
                event_data,
                augmented_event_id=identity,
            )

        config_path, data_path = materialized_paths
        self.assertEqual(
            opened_invocation_inputs,
            [
                "finder_file.config",
                "data_0",
                "pyfinder_amplitudes_to_Finder.txt",
            ],
        )
        self.assertEqual(Path(config_path), workspace / "finder_file.config")
        self.assertEqual(Path(data_path), workspace / "data_0")
        self.assertTrue(
            (workspace / "pyfinder_amplitudes_to_Finder.txt").is_file()
        )
        self.assert_workspace_available(workspace)

    def test_workspace_logger_lifetime_and_process_handover(self):
        identity = "event-one_t00010"
        work_root = self.temporary_root / "runs"
        workspace = work_root / identity
        event_log_path = workspace / "pyfinder.log"
        process_logger = mock.Mock()
        process_logger.name = "continuous.manager"
        executable = self.executable(work_root, logger=process_logger)
        original_new_handler = finderexec.customlogger._new_handler
        lifecycle = []
        workspace_loggers = []
        opened_handlers = []

        def open_handler(destination, overwrite, rotate):
            self.assertEqual(Path(destination), event_log_path)
            self.assertFalse(event_log_path.exists())
            self.assert_workspace_locked(workspace)
            lifecycle.append("open")
            handler = original_new_handler(destination, overwrite, rotate)
            original_close = handler.close

            def close_handler():
                self.assert_workspace_locked(workspace)
                lifecycle.append("close")
                original_close()

            handler.close = close_handler
            handler.original_close = original_close
            opened_handlers.append(handler)
            return handler

        def record_workspace_diagnostic(message):
            self.assert_workspace_locked(workspace)
            self.assertIsNot(executable.logger, process_logger)
            workspace_loggers.append(executable.logger)
            executable.logger.info(message)

        def write_configuration():
            record_workspace_diagnostic(
                "controlled configuration diagnostic"
            )
            executable.finder_file_config_path = str(
                workspace / "finder_file.config"
            )

        def write_data(_amplitudes, _event_data):
            record_workspace_diagnostic("controlled input diagnostic")
            return str(workspace / "data_0"), finderexec.FinderChannelList()

        with mock.patch.object(
            finderexec.customlogger,
            "_new_handler",
            side_effect=open_handler,
        ), mock.patch.object(
            executable,
            "_write_finder_configuration",
            side_effect=write_configuration,
        ), mock.patch.object(
            executable,
            "_write_data_for_finder",
            side_effect=write_data,
        ):
            executable.materialize_inputs(
                [],
                mock.Mock(),
                augmented_event_id=identity,
            )

        transient_logger = workspace_loggers[0]
        self.assertEqual(lifecycle, ["open", "close"])
        self.assertTrue(
            all(logger is transient_logger for logger in workspace_loggers)
        )
        self.assertEqual(transient_logger.handlers, [])
        self.assertTrue(
            all(handler.stream is None for handler in opened_handlers)
        )
        self.assertIs(executable.logger, process_logger)
        self.assert_workspace_available(workspace)

        self.assertTrue(process_logger.info.called)
        self.assertFalse(
            any(
                "controlled input diagnostic" in call.args
                for call in process_logger.info.call_args_list
            )
        )

        event_log = event_log_path.read_text(encoding="utf-8")
        self.assertIn(identity, event_log)
        self.assertIn("continuous.manager", event_log)
        self.assertIn("controlled configuration diagnostic", event_log)
        self.assertIn("controlled input diagnostic", event_log)

        # Restore the real close method because logging keeps weak references
        # to every constructed handler until interpreter shutdown.
        for handler in opened_handlers:
            handler.close = handler.original_close

    def test_materialization_exception_releases_workspace_lock(self):
        work_root = self.temporary_root / "runs"
        workspace = work_root / "event-one_t00010"
        executable = self.executable(work_root)
        process_logger = executable.logger

        def fail_data_write(_amplitudes, _event_data):
            self.assert_workspace_locked(workspace)
            raise ValueError("controlled formatting failure")

        with mock.patch.object(
            executable,
            "_write_data_for_finder",
            side_effect=fail_data_write,
        ):
            with self.assertRaisesRegex(ValueError, "formatting failure"):
                executable.materialize_inputs(
                    [],
                    mock.Mock(),
                    augmented_event_id="event-one_t00010",
                )

        self.assert_workspace_available(workspace)
        self.assertIs(executable.logger, process_logger)
        failure_messages = self.logged_messages(process_logger, "error")
        self.assertTrue(
            any("event-one_t00010" in message for message in failure_messages)
        )
        self.assertTrue(
            any(
                str(workspace / "pyfinder.log") in message
                for message in failure_messages
            )
        )
        event_log = (workspace / "pyfinder.log").read_text(encoding="utf-8")
        self.assertIn("controlled formatting failure", event_log)

    def test_execute_holds_workspace_lock_through_output_collection(self):
        work_root = self.temporary_root / "runs"
        workspace = work_root / "event-one_t00010"
        executable = self.executable(work_root)
        controlled_child = self.temporary_root / "controlled-success-finder"
        stdout = (
            b"CONTROLLED EXECUTE STDOUT\n"
            b"Event_ID = controlled-finder-id\n"
        )
        stderr = b"CONTROLLED EXECUTE STDERR\n"
        _write_controlled_executable(
            controlled_child,
            stdout=stdout,
            stderr=stderr,
            returncode=0,
        )
        executable.executable_path = str(controlled_child)
        event_data = mock.Mock()
        event_data.get_event_id.return_value = "event-one"
        calls = []
        process_logger = executable.logger
        transient_handlers = []
        original_new_handler = finderexec.customlogger._new_handler

        def record_handler(*args, **kwargs):
            handler = original_new_handler(*args, **kwargs)
            transient_handlers.append(handler)
            return handler

        def record_locked_call(name):
            self.assert_workspace_locked(workspace)
            calls.append(name)

        def write_data(_amplitudes, _event_data):
            record_locked_call("data")
            executable.logger.info("controlled input diagnostic")
            return str(workspace / "data_0"), finderexec.FinderChannelList()

        def write_configuration():
            record_locked_call("configuration")
            executable.finder_file_config_path = str(
                workspace / "finder_file.config"
            )

        def collect_output(**_kwargs):
            record_locked_call("collect")
            executable.logger.info("controlled output collection diagnostic")

        with mock.patch.object(
            finderexec.customlogger,
            "_new_handler",
            side_effect=record_handler,
        ), mock.patch.object(
            executable,
            "_write_finder_configuration",
            side_effect=write_configuration,
        ), mock.patch.object(
            executable,
            "_write_data_for_finder",
            side_effect=write_data,
        ), mock.patch.object(
            executable,
            "_collect_finder_output",
            side_effect=collect_output,
        ) as collect_output:
            result = executable.execute(
                amplitudes=[{"normalized": "record"}],
                event_data=event_data,
                augmented_event_id="event-one_t00010",
            )

        self.assertIs(result, executable)
        self.assertEqual(
            calls,
            ["configuration", "data", "collect"],
        )
        collect_output.assert_called_once_with(event_id="event-one")
        self.assert_execution_cleanup(
            executable,
            workspace,
            process_logger,
            transient_handlers,
        )
        self.assertEqual(
            json.loads(
                (workspace / "controlled-child-arguments.json").read_text(
                    encoding="utf-8"
                )
            ),
            [
                str(workspace / "finder_file.config"),
                str(workspace),
                "0",
                "0",
                "yes" if executable.is_live_mode else "no",
            ],
        )
        event_log = (workspace / "pyfinder.log").read_text(encoding="utf-8")
        self.assertIn("controlled input diagnostic", event_log)
        self.assertIn("CONTROLLED EXECUTE STDOUT", event_log)
        self.assertIn("controlled-finder-id", event_log)
        self.assertIn("CONTROLLED EXECUTE STDERR", event_log)
        self.assertIn("controlled output collection diagnostic", event_log)

    def test_nonzero_controlled_subprocess_retains_result_and_cleans_up(self):
        identity = "event-nonzero_t00010"
        work_root = self.temporary_root / "runs"
        workspace = work_root / identity
        controlled_child = self.temporary_root / "controlled-nonzero-finder"
        stdout = b"CONTROLLED NONZERO STDOUT\n"
        stderr = b"CONTROLLED NONZERO STDERR\n"
        returncode = 37
        _write_controlled_executable(
            controlled_child,
            stdout=stdout,
            stderr=stderr,
            returncode=returncode,
        )
        executable = self.executable(work_root)
        executable.executable_path = str(controlled_child)
        process_logger = executable.logger
        event_data = mock.Mock()
        event_data.get_event_id.return_value = "event-nonzero"
        transient_handlers = []
        original_new_handler = finderexec.customlogger._new_handler

        def record_handler(*args, **kwargs):
            handler = original_new_handler(*args, **kwargs)
            transient_handlers.append(handler)
            return handler

        def write_configuration():
            self.assert_workspace_locked(workspace)
            executable.finder_file_config_path = str(
                workspace / "finder_file.config"
            )

        with mock.patch.object(
            finderexec.customlogger,
            "_new_handler",
            side_effect=record_handler,
        ), mock.patch.object(
            executable,
            "_write_finder_configuration",
            side_effect=write_configuration,
        ), mock.patch.object(
            executable,
            "_write_data_for_finder",
            return_value=(
                str(workspace / "data_0"),
                finderexec.FinderChannelList(),
            ),
        ), mock.patch.object(
            executable,
            "_collect_finder_output",
        ) as collect_output:
            with self.assertRaises(finderexec.FinDerExecutionError) as raised:
                executable.execute(
                    amplitudes=[],
                    event_data=event_data,
                    augmented_event_id=identity,
                )

        error = raised.exception
        self.assertIsInstance(error, Exception)
        self.assertNotIsInstance(error, SystemExit)
        self.assertEqual(error.returncode, returncode)
        self.assertEqual(error.stdout, stdout)
        self.assertEqual(error.stderr, stderr)
        self.assertEqual(error.executable_path, str(controlled_child))
        self.assertEqual(error.working_directory, str(workspace))
        self.assertEqual(error.reason, "nonzero-return-code")
        self.assertEqual(
            error.command,
            (
                str(controlled_child),
                str(workspace / "finder_file.config"),
                str(workspace),
                "0",
                "0",
                "yes" if executable.is_live_mode else "no",
            ),
        )
        collect_output.assert_not_called()
        self.assert_execution_cleanup(
            executable,
            workspace,
            process_logger,
            transient_handlers,
        )
        self.assertEqual(
            json.loads(
                (workspace / "controlled-child-arguments.json").read_text(
                    encoding="utf-8"
                )
            ),
            list(error.command[1:]),
        )
        event_log = (workspace / "pyfinder.log").read_text(encoding="utf-8")
        self.assertIn("CONTROLLED NONZERO STDOUT", event_log)
        self.assertIn("CONTROLLED NONZERO STDERR", event_log)

    def test_zero_return_without_identifier_fails_before_output_collection(self):
        identity = "event-missing-id_t00010"
        work_root = self.temporary_root / "runs"
        workspace = work_root / identity
        controlled_child = self.temporary_root / "controlled-missing-id-finder"
        stdout = b"CONTROLLED MISSING-ID STDOUT\n"
        stderr = b"CONTROLLED MISSING-ID STDERR\n"
        _write_controlled_executable(
            controlled_child,
            stdout=stdout,
            stderr=stderr,
            returncode=0,
        )
        executable = self.executable(work_root)
        executable.executable_path = str(controlled_child)
        process_logger = executable.logger
        event_data = mock.Mock()
        event_data.get_event_id.return_value = "event-missing-id"
        transient_handlers = []
        original_new_handler = finderexec.customlogger._new_handler

        def record_handler(*args, **kwargs):
            handler = original_new_handler(*args, **kwargs)
            transient_handlers.append(handler)
            return handler

        def write_configuration():
            self.assert_workspace_locked(workspace)
            executable.finder_file_config_path = str(
                workspace / "finder_file.config"
            )

        with mock.patch.object(
            finderexec.customlogger,
            "_new_handler",
            side_effect=record_handler,
        ), mock.patch.object(
            executable,
            "_write_finder_configuration",
            side_effect=write_configuration,
        ), mock.patch.object(
            executable,
            "_write_data_for_finder",
            return_value=(
                str(workspace / "data_0"),
                finderexec.FinderChannelList(),
            ),
        ), mock.patch.object(
            executable,
            "_collect_finder_output",
        ) as collect_output, mock.patch.object(
            finderexec,
            "read_event_solution_from_file",
        ) as read_event, mock.patch.object(
            finderexec,
            "read_rupture_polygon_from_file",
        ) as read_rupture, mock.patch.object(
            finderexec,
            "read_finder_channels_from_file",
        ) as read_channels:
            with self.assertRaises(finderexec.FinDerExecutionError) as raised:
                executable.execute(
                    amplitudes=[],
                    event_data=event_data,
                    augmented_event_id=identity,
                )

        error = raised.exception
        self.assertIsInstance(error, Exception)
        self.assertNotIsInstance(error, SystemExit)
        self.assertEqual(error.reason, "missing-event-id")
        self.assertEqual(error.returncode, 0)
        self.assertEqual(error.stdout, stdout)
        self.assertEqual(error.stderr, stderr)
        self.assertEqual(error.executable_path, str(controlled_child))
        self.assertEqual(error.working_directory, str(workspace))
        self.assertEqual(
            error.command,
            (
                str(controlled_child),
                str(workspace / "finder_file.config"),
                str(workspace),
                "0",
                "0",
                "yes" if executable.is_live_mode else "no",
            ),
        )
        self.assertEqual(
            json.loads(
                (workspace / "controlled-child-arguments.json").read_text(
                    encoding="utf-8"
                )
            ),
            list(error.command[1:]),
        )
        collect_output.assert_not_called()
        read_event.assert_not_called()
        read_rupture.assert_not_called()
        read_channels.assert_not_called()
        self.assert_execution_cleanup(
            executable,
            workspace,
            process_logger,
            transient_handlers,
        )
        event_log = (workspace / "pyfinder.log").read_text(encoding="utf-8")
        self.assertIn("CONTROLLED MISSING-ID STDOUT", event_log)
        self.assertIn("CONTROLLED MISSING-ID STDERR", event_log)

    def test_launch_failure_propagates_original_exception_and_cleans_up(self):
        identity = "event-launch_t00010"
        work_root = self.temporary_root / "runs"
        workspace = work_root / identity
        controlled_child = self.temporary_root / "controlled-launch-finder"
        _write_controlled_executable(
            controlled_child,
            stdout=b"",
            stderr=b"",
            returncode=0,
        )
        executable = self.executable(work_root)
        executable.executable_path = str(controlled_child)
        process_logger = executable.logger
        event_data = mock.Mock()
        event_data.get_event_id.return_value = "event-launch"
        launch_error = OSError("controlled launch failure")
        transient_handlers = []
        original_new_handler = finderexec.customlogger._new_handler

        def record_handler(*args, **kwargs):
            handler = original_new_handler(*args, **kwargs)
            transient_handlers.append(handler)
            return handler

        def write_configuration():
            executable.finder_file_config_path = str(
                workspace / "finder_file.config"
            )

        with mock.patch.object(
            finderexec.customlogger,
            "_new_handler",
            side_effect=record_handler,
        ), mock.patch.object(
            executable,
            "_write_finder_configuration",
            side_effect=write_configuration,
        ), mock.patch.object(
            executable,
            "_write_data_for_finder",
            return_value=(
                str(workspace / "data_0"),
                finderexec.FinderChannelList(),
            ),
        ), mock.patch.object(
            finderexec.subprocess,
            "Popen",
            side_effect=launch_error,
        ), mock.patch.object(
            executable,
            "_collect_finder_output",
        ) as collect_output:
            with self.assertRaises(OSError) as raised:
                executable.execute(
                    amplitudes=[],
                    event_data=event_data,
                    augmented_event_id=identity,
                )

        self.assertIs(raised.exception, launch_error)
        self.assertIsInstance(raised.exception, Exception)
        self.assertNotIsInstance(raised.exception, SystemExit)
        collect_output.assert_not_called()
        self.assert_execution_cleanup(
            executable,
            workspace,
            process_logger,
            transient_handlers,
        )

    def test_output_reader_failure_propagates_original_exception_and_cleans_up(self):
        identity = "event-output_t00010"
        work_root = self.temporary_root / "runs"
        workspace = work_root / identity
        controlled_child = self.temporary_root / "controlled-output-finder"
        _write_controlled_executable(
            controlled_child,
            stdout=(
                b"CONTROLLED OUTPUT-HANDOFF STDOUT\n"
                b"Event_ID = controlled-output-id\n"
            ),
            stderr=b"CONTROLLED OUTPUT-HANDOFF STDERR\n",
            returncode=0,
        )
        executable = self.executable(work_root)
        executable.executable_path = str(controlled_child)
        process_logger = executable.logger
        event_data = mock.Mock()
        event_data.get_event_id.return_value = "event-output"
        reader_error = OSError("controlled output reader failure")
        transient_handlers = []
        original_new_handler = finderexec.customlogger._new_handler

        def record_handler(*args, **kwargs):
            handler = original_new_handler(*args, **kwargs)
            transient_handlers.append(handler)
            return handler

        def write_configuration():
            executable.finder_file_config_path = str(
                workspace / "finder_file.config"
            )

        with mock.patch.object(
            finderexec.customlogger,
            "_new_handler",
            side_effect=record_handler,
        ), mock.patch.object(
            executable,
            "_write_finder_configuration",
            side_effect=write_configuration,
        ), mock.patch.object(
            executable,
            "_write_data_for_finder",
            return_value=(
                str(workspace / "data_0"),
                finderexec.FinderChannelList(),
            ),
        ), mock.patch.object(
            finderexec,
            "read_event_solution_from_file",
            side_effect=reader_error,
        ) as read_event, mock.patch.object(
            finderexec,
            "read_rupture_polygon_from_file",
        ) as read_rupture, mock.patch.object(
            finderexec,
            "read_finder_channels_from_file",
        ) as read_channels:
            with self.assertRaises(OSError) as raised:
                executable.execute(
                    amplitudes=[],
                    event_data=event_data,
                    augmented_event_id=identity,
                )

        self.assertIs(raised.exception, reader_error)
        self.assertIsInstance(raised.exception, Exception)
        self.assertNotIsInstance(raised.exception, SystemExit)
        read_event.assert_called_once_with(
            str(
                workspace
                / "temp_data"
                / "controlled-output-id"
                / "core_info_0"
            )
        )
        read_rupture.assert_not_called()
        read_channels.assert_not_called()
        self.assert_execution_cleanup(
            executable,
            workspace,
            process_logger,
            transient_handlers,
        )

    def test_workspace_logger_initialization_failure_stops_execution(self):
        identity = "event-one_t00010"
        work_root = self.temporary_root / "runs"
        workspace = work_root / identity
        event_log_path = workspace / "pyfinder.log"
        executable = self.executable(work_root)
        process_logger = executable.logger
        event_data = mock.Mock()
        event_data.get_event_id.return_value = "event-one"

        with mock.patch.object(
            executable,
            "_check_finder_executable",
        ), mock.patch.object(
            finderexec.customlogger,
            "transient_file_logger",
            side_effect=OSError("cannot open event log"),
        ), mock.patch.object(
            executable,
            "_write_finder_configuration",
        ) as write_configuration, mock.patch.object(
            executable,
            "_write_data_for_finder",
        ) as write_data, mock.patch.object(
            executable,
            "_run_finder",
        ) as run_finder, mock.patch.object(
            finderexec.subprocess,
            "Popen",
        ) as process_constructor:
            with self.assertRaisesRegex(OSError, "cannot open event log"):
                executable.execute(
                    amplitudes=[],
                    event_data=event_data,
                    augmented_event_id=identity,
                )

        write_configuration.assert_not_called()
        write_data.assert_not_called()
        run_finder.assert_not_called()
        process_constructor.assert_not_called()
        self.assertIs(executable.logger, process_logger)
        self.assert_workspace_available(workspace)
        failure_messages = self.logged_messages(process_logger, "error")
        self.assertTrue(
            any(identity in message for message in failure_messages)
        )
        self.assertTrue(
            any(str(event_log_path) in message for message in failure_messages)
        )
        self.assertTrue(
            any("cannot open event log" in message for message in failure_messages)
        )

    def test_companion_write_failure_prevents_finder_execution(self):
        event_data = mock.Mock()
        event_data.get_event_id.return_value = "event-one"
        event_data.get_latitude.return_value = 0.0
        event_data.get_longitude.return_value = 0.0
        event_data.get_depth.return_value = 10.0
        event_data.get_magnitude.return_value = 5.6
        event_data.get_origin_time.return_value = (
            "2026-08-10T08:15:30.250000Z"
        )
        records = [
            {
                "latitude": 0.0,
                "longitude": 1.0,
                "network": "CH",
                "station": "TEST",
                "location": "00",
                "channel": "HNZ",
                "pga": 12.5,
                "timestamp": 1786349730.25,
                "source": "RRSM",
                "provider_value": 12.5,
                "provider_unit": "cm/s^2",
            }
        ]

        write_executable = self.executable(self.temporary_root / "write-runs")
        original_open = builtins.open

        def fail_companion_write(file, *args, **kwargs):
            if Path(file).name == "pyfinder_amplitudes_to_Finder.txt":
                raise OSError("controlled companion write failure")
            return original_open(file, *args, **kwargs)

        with mock.patch.object(
            write_executable,
            "_check_finder_executable",
        ), mock.patch.object(
            finderexec.Calculator,
            "predict_PGA_from_magnitude",
            return_value=1.0,
        ), mock.patch.object(
            write_executable,
            "_run_finder",
        ) as write_run, mock.patch.object(
            builtins,
            "open",
            side_effect=fail_companion_write,
        ):
            with self.assertRaisesRegex(OSError, "companion write failure"):
                write_executable.execute(
                    records,
                    event_data,
                    augmented_event_id="event-one_t00010",
                )
        write_run.assert_not_called()

    def test_output_lookup_uses_only_the_returned_finder_identifier(self):
        executable = self.executable(self.temporary_root / "runs")
        executable.working_directory = str(
            self.temporary_root / "runs" / "event-one_t00010"
        )
        executable.finder_event_id = "returned-finder-id"
        executable.finder_used_channels = []
        expected_output = (
            Path(executable.working_directory)
            / "temp_data"
            / "returned-finder-id"
        )

        with mock.patch.object(
            finderexec,
            "read_event_solution_from_file",
            return_value=mock.Mock(),
        ) as read_event, mock.patch.object(
            finderexec,
            "read_rupture_polygon_from_file",
            return_value=mock.Mock(),
        ) as read_rupture, mock.patch.object(
            finderexec,
            "read_finder_channels_from_file",
            return_value=mock.Mock(),
        ) as read_channels:
            executable._collect_finder_output(event_id="event-one")

        read_event.assert_called_once_with(str(expected_output / "core_info_0"))
        read_rupture.assert_called_once_with(
            str(expected_output / "finder_rupture_list_0")
        )
        read_channels.assert_called_once_with(str(expected_output / "data_0"))


if __name__ == "__main__":
    unittest.main()
