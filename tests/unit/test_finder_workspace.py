"""Offline tests for augmented FinDer workspace identity and retention."""

import ast
import atexit
import builtins
from copy import deepcopy
import fcntl
import math
import multiprocessing
import os
from pathlib import Path
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
    from pyfinder import finderexec, runtime
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


class FinDerWorkspaceTests(unittest.TestCase):
    SERIALIZED_NUMBER_REL_TOLERANCE = 1e-12
    SERIALIZED_NUMBER_ABS_TOLERANCE = 1e-20
    COMPANION_DISTANCE_ABS_TOLERANCE_KM = 0.05

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

    def service_root(self, name="service"):
        service_root = self.temporary_root / name
        for branch in ("state", "logs", "runs", "playbacks"):
            (service_root / branch).mkdir(parents=True, exist_ok=True)
        return service_root

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

    @staticmethod
    def logged_messages(logger, method_name):
        messages = []
        for call in getattr(logger, method_name).call_args_list:
            message = call.args[0]
            if len(call.args) > 1:
                message = message % call.args[1:]
            messages.append(message)
        return messages

    def assert_serialized_number_close(self, actual_text, expected):
        """Compare one artifact field numerically instead of by text form."""
        actual = float(actual_text)
        self.assertTrue(math.isfinite(actual), actual_text)
        self.assertTrue(
            math.isclose(
                actual,
                expected,
                rel_tol=self.SERIALIZED_NUMBER_REL_TOLERANCE,
                abs_tol=self.SERIALIZED_NUMBER_ABS_TOLERANCE,
            ),
            (actual, expected),
        )

    def assert_non_live_data_0(self, path, *, event_time, expected_rows):
        """Verify materialized non-live data structurally and numerically."""
        lines = path.read_bytes().decode("ascii").splitlines()
        self.assertEqual(lines[0], "# {0} 0".format(event_time))
        self.assertEqual(len(lines), len(expected_rows) + 1)
        for line, expected in zip(lines[1:], expected_rows):
            fields = line.split()
            self.assertEqual(len(fields), 3)
            latitude, longitude, log10_pga = fields
            self.assert_serialized_number_close(latitude, expected["latitude"])
            self.assert_serialized_number_close(longitude, expected["longitude"])
            self.assert_serialized_number_close(
                log10_pga,
                math.log10(expected["pga"]),
            )

    def assert_companion(self, path, expected_rows):
        """Verify materialized companion membership and numeric semantics."""
        lines = path.read_bytes().decode("ascii").splitlines()
        self.assertEqual(lines[0], "# SNCL PGA_CM_S2 EPI_DISTANCE_KM")
        self.assertEqual(len(lines), len(expected_rows) + 1)
        for line, expected in zip(lines[1:], expected_rows):
            fields = line.split()
            self.assertEqual(len(fields), 3)
            sncl, pga, distance = fields
            self.assertEqual(sncl, expected["sncl"])
            self.assert_serialized_number_close(pga, expected["pga"])
            self.assertRegex(distance, r"^-?\d+\.\d$")
            self.assertTrue(
                math.isclose(
                    float(distance),
                    expected["distance_km"],
                    rel_tol=0.0,
                    abs_tol=self.COMPANION_DISTANCE_ABS_TOLERANCE_KM,
                ),
                (distance, expected["distance_km"]),
            )

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

        identity = build_augmented_event_id("event-one", 10)
        for extra_component in ("service", "attempt", "pid", "uuid"):
            self.assertNotIn(extra_component, identity)

    def test_runtime_configuration_selects_operational_and_experimental_roots(self):
        service_root = self.service_root()
        continuous = runtime.build_runtime_context(
            "continuous",
            service_root=service_root,
        )
        playback = runtime.build_runtime_context(
            "playback",
            service_root=service_root,
        )
        on_demand = runtime.build_runtime_context(
            "on-demand",
            service_root=service_root,
        )

        for context, expected_branch in (
            (continuous, "runs"),
            (playback, "playbacks"),
            (on_demand, "playbacks"),
        ):
            with self.subTest(workflow=context.workflow):
                configuration = context.isolated_configuration(pyfinderconfig)
                self.assertEqual(
                    Path(
                        configuration["finder-executable"][
                            "output-root-folder"
                        ]
                    ),
                    service_root / expected_branch,
                )
                self.assertEqual(
                    select_workspace_path(
                        configuration["finder-executable"][
                            "output-root-folder"
                        ],
                        "event-one_t00000",
                    ),
                    service_root / expected_branch / "event-one_t00000",
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

    def test_input_materialization_writes_current_formatter_and_config_output(self):
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
        original_import = builtins.__import__
        original_open = builtins.open
        opened_invocation_inputs = []

        def reject_downstream_import(name, *args, **kwargs):
            if name in (
                "pyfinder.utils.shakemap",
                "pyfinder.services.alert",
                "utils.shakemap",
                "services.alert",
            ):
                raise AssertionError(
                    "input materialization reached a downstream module"
                )
            return original_import(name, *args, **kwargs)

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
            executable,
            "_check_finder_executable",
        ) as check_executable, mock.patch.object(
            executable,
            "_run_finder",
        ) as run_finder, mock.patch.object(
            finderexec.subprocess,
            "Popen",
        ) as process_constructor, mock.patch.object(
            finderexec,
            "read_event_solution_from_file",
        ) as read_event, mock.patch.object(
            finderexec,
            "read_rupture_polygon_from_file",
        ) as read_rupture, mock.patch.object(
            finderexec,
            "read_finder_channels_from_file",
        ) as read_channels, mock.patch.object(
            finderexec,
            "get_epoch_time",
            return_value=1786349730.25,
        ), mock.patch.object(
            finderexec.Calculator,
            "predict_PGA_from_magnitude",
            return_value=1.0,
        ), mock.patch.object(
            builtins,
            "__import__",
            side_effect=reject_downstream_import,
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
        self.assertEqual(len(materialized_paths), 2)
        config_path, data_path = materialized_paths

        self.assertEqual(Path(config_path), workspace / "finder_file.config")
        self.assertEqual(Path(data_path), workspace / "data_0")
        self.assert_non_live_data_0(
            Path(data_path),
            event_time=1786349730,
            expected_rows=[
                {"latitude": 0.0, "longitude": 0.0, "pga": 12.625},
                {"latitude": 0.0, "longitude": 1.0, "pga": 12.5},
            ],
        )
        companion_path = workspace / "pyfinder_amplitudes_to_Finder.txt"
        self.assert_companion(
            companion_path,
            [
                {"sncl": "XX.NONE.00.HNZ", "pga": 12.625,
                 "distance_km": 0.0},
                {"sncl": "CH.TEST.00.HNZ", "pga": 12.5,
                 "distance_km": 6371.0 * math.radians(1.0)},
            ],
        )
        self.assertEqual(
            opened_invocation_inputs,
            [
                "finder_file.config",
                "data_0",
                "pyfinder_amplitudes_to_Finder.txt",
            ],
        )
        self.assertEqual(
            Path(config_path).read_text(encoding="utf-8"),
            "DATA_FOLDER {0}\nMODEL unchanged\n".format(workspace),
        )
        self.assertEqual(
            len(executable.get_finder_used_channels()),
            2,
        )
        self.assertEqual(
            [
                channel.get_sncl()
                for channel in executable.get_finder_used_channels()
            ],
            ["XX.NONE.00.HNZ", "CH.TEST.00.HNZ"],
        )
        self.assertEqual(
            [
                channel.pga
                for channel in executable.get_finder_used_channels()
            ],
            [12.625, 12.5],
        )
        check_executable.assert_not_called()
        run_finder.assert_not_called()
        process_constructor.assert_not_called()
        read_event.assert_not_called()
        read_rupture.assert_not_called()
        read_channels.assert_not_called()
        self.assertFalse(Path(executable.executable_path).exists())

    def test_input_materialization_locks_both_writes_then_releases(self):
        work_root = self.temporary_root / "runs"
        workspace = work_root / "event-one_t00010"
        executable = self.executable(work_root)
        calls = []

        def write_configuration():
            self.assert_workspace_locked(workspace)
            calls.append("configuration")
            executable.finder_file_config_path = str(
                workspace / "finder_file.config"
            )

        def write_data(_amplitudes, _event_data):
            self.assert_workspace_locked(workspace)
            calls.append("data")
            return str(workspace / "data_0"), finderexec.FinderChannelList()

        with mock.patch.object(
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
                augmented_event_id="event-one_t00010",
            )

        self.assertEqual(calls, ["configuration", "data"])
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
        configurations_before = dict(
            finderexec.customlogger._CONFIGURATIONS
        )
        owners_before = dict(finderexec.customlogger._DESTINATION_OWNERS)

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
        self.assertIs(executable.logger, process_logger)
        self.assert_workspace_available(workspace)
        self.assertEqual(
            finderexec.customlogger._CONFIGURATIONS,
            configurations_before,
        )
        self.assertEqual(
            finderexec.customlogger._DESTINATION_OWNERS,
            owners_before,
        )

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
        event_data = mock.Mock()
        event_data.get_event_id.return_value = "event-one"
        calls = []
        process_logger = executable.logger

        def record_locked_call(name):
            self.assert_workspace_locked(workspace)
            calls.append(name)

        def write_data(_amplitudes, _event_data):
            record_locked_call("data")
            executable.logger.info("controlled input diagnostic")
            return str(workspace / "data_0"), finderexec.FinderChannelList()

        def run_finder():
            record_locked_call("run")
            executable._process_finder_output(
                b"Event_ID = controlled-finder-id\n",
                b"controlled finder stderr\n",
            )

        def collect_output(**_kwargs):
            record_locked_call("collect")
            executable.logger.info("controlled output collection diagnostic")

        with mock.patch.object(
            executable,
            "_check_finder_executable",
            side_effect=lambda: calls.append("check"),
        ), mock.patch.object(
            executable,
            "_write_finder_configuration",
            side_effect=lambda: record_locked_call("configuration"),
        ), mock.patch.object(
            executable,
            "_write_data_for_finder",
            side_effect=write_data,
        ), mock.patch.object(
            executable,
            "_run_finder",
            side_effect=run_finder,
        ), mock.patch.object(
            executable,
            "_collect_finder_output",
            side_effect=collect_output,
        ) as collect_output, mock.patch.object(
            finderexec.subprocess,
            "Popen",
        ) as process_constructor:
            result = executable.execute(
                amplitudes=[{"normalized": "record"}],
                event_data=event_data,
                augmented_event_id="event-one_t00010",
            )

        self.assertIs(result, executable)
        self.assertEqual(
            calls,
            ["check", "configuration", "data", "run", "collect"],
        )
        collect_output.assert_called_once_with(event_id="event-one")
        process_constructor.assert_not_called()
        self.assert_workspace_available(workspace)
        self.assertIs(executable.logger, process_logger)
        event_log = (workspace / "pyfinder.log").read_text(encoding="utf-8")
        self.assertIn("controlled input diagnostic", event_log)
        self.assertIn("controlled-finder-id", event_log)
        self.assertIn("controlled finder stderr", event_log)
        self.assertIn("controlled output collection diagnostic", event_log)

    def test_execute_failures_release_workspace_lock(self):
        cases = (
            ("output exception", None, RuntimeError("output read failed")),
            ("finder SystemExit", SystemExit(2), None),
        )
        for index, (label, run_error, collect_error) in enumerate(cases):
            with self.subTest(label=label):
                identity = "event-{0}_t00010".format(index)
                work_root = self.temporary_root / "runs"
                workspace = work_root / identity
                executable = self.executable(work_root)
                process_logger = executable.logger
                event_data = mock.Mock()
                event_data.get_event_id.return_value = "event-{0}".format(index)

                with mock.patch.object(
                    executable,
                    "_check_finder_executable",
                ), mock.patch.object(
                    executable,
                    "_write_finder_configuration",
                ), mock.patch.object(
                    executable,
                    "_write_data_for_finder",
                    return_value=(
                        str(workspace / "data_0"),
                        finderexec.FinderChannelList(),
                    ),
                ), mock.patch.object(
                    executable,
                    "_run_finder",
                    side_effect=run_error,
                ), mock.patch.object(
                    executable,
                    "_collect_finder_output",
                    side_effect=collect_error,
                ):
                    with self.assertRaises(SystemExit):
                        executable.execute(
                            amplitudes=[],
                            event_data=event_data,
                            augmented_event_id=identity,
                        )

                self.assert_workspace_available(workspace)
                self.assertIs(executable.logger, process_logger)

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

    def test_execute_uses_the_supplied_common_event_identity(self):
        executable = self.executable(self.temporary_root / "runs")
        observations = []
        supplied_event = mock.Mock(name="supplied_common_event")
        supplied_event.get_event_id.return_value = "common-event"

        with mock.patch.object(
            executable,
            "_check_finder_executable",
        ), mock.patch.object(
            executable,
            "_write_finder_configuration",
        ), mock.patch.object(
            executable,
            "_write_data_for_finder",
            return_value=("data_0", finderexec.FinderChannelList()),
        ) as write_data, mock.patch.object(
            executable,
            "_run_finder",
        ), mock.patch.object(
            executable,
            "_collect_finder_output",
        ) as collect_output:
            executable.execute(
                amplitudes=observations,
                event_data=supplied_event,
                augmented_event_id="common-event_t00010",
            )

        write_data.assert_called_once_with(
            observations,
            supplied_event,
        )
        collect_output.assert_called_once_with(event_id="common-event")
        supplied_event.get_event_id.assert_called_once_with()

    def test_data_writer_rejects_non_list_observations(self):
        executable = self.executable(self.temporary_root / "runs")
        workspace = self.temporary_root / "runs" / "event-one_t00010"
        workspace.mkdir(parents=True)
        executable.working_directory = str(workspace)

        with self.assertRaisesRegex(
                TypeError, "requires merged normalized observations"):
            executable._write_data_for_finder(
                object(),
                mock.Mock(name="supplied_common_event"),
            )

        self.assertFalse((workspace / "data_0").exists())
        self.assertFalse(
            (workspace / "pyfinder_amplitudes_to_Finder.txt").exists()
        )

    def test_companion_render_or_write_failure_prevents_finder_execution(self):
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

        render_executable = self.executable(self.temporary_root / "render-runs")
        with mock.patch.object(
            render_executable,
            "_check_finder_executable",
        ), mock.patch.object(
            render_executable,
            "_render_amplitude_companion",
            side_effect=ValueError("controlled companion rendering failure"),
        ), mock.patch.object(
            finderexec.Calculator,
            "predict_PGA_from_magnitude",
            return_value=1.0,
        ), mock.patch.object(
            render_executable,
            "_run_finder",
        ) as render_run:
            with self.assertRaisesRegex(ValueError, "companion rendering"):
                render_executable.execute(
                    records,
                    event_data,
                    augmented_event_id="event-one_t00010",
                )
        render_run.assert_not_called()

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

    def test_workspace_code_contains_no_destructive_filesystem_calls(self):
        forbidden_calls = {
            "archive",
            "move",
            "remove",
            "rename",
            "replace",
            "rmtree",
            "unlink",
        }
        called_names = set()
        for module_path in (
            Path(finderexec.__file__),
            Path(select_workspace_path.__code__.co_filename),
        ):
            tree = ast.parse(module_path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if isinstance(node.func, ast.Name):
                    called_names.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    called_names.add(node.func.attr)

        self.assertTrue(forbidden_calls.isdisjoint(called_names), called_names)


if __name__ == "__main__":
    unittest.main()
