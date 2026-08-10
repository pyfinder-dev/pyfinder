"""Offline tests for augmented FinDer workspace identity and retention."""

import ast
import atexit
import builtins
from copy import deepcopy
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


class FinDerWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory(
            prefix="pyfinder-workspace-"
        )
        self.temporary_root = Path(self.temporary_directory.name)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def executable(self, work_root):
        configuration = deepcopy(pyfinderconfig)
        configuration["finder-executable"]["path"] = "/not-invoked/finder_run"
        configuration["finder-executable"]["output-root-folder"] = str(
            work_root
        )
        return finderexec.FinDerExecutable(
            options={"command_line_args": "workspace-test"},
            configuration=configuration,
            finder_configuration_name="global",
            finder_configuration={
                "DATA_FOLDER": "unused",
                "MODEL": "unchanged",
            },
            logger=mock.Mock(),
        )

    def service_root(self, name="service"):
        service_root = self.temporary_root / name
        for branch in ("state", "logs", "runs", "playbacks"):
            (service_root / branch).mkdir(parents=True, exist_ok=True)
        return service_root

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

    def test_repeated_preparation_reuses_directory_and_preserves_all_content(self):
        work_root = self.temporary_root / "runs"
        executable = self.executable(work_root)
        identity = build_augmented_event_id("event-one", 10)

        executable._prepare_workspace(identity)
        first_workspace = Path(executable.get_working_directory())
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

        executable._prepare_workspace(identity)
        second_workspace = Path(executable.get_working_directory())

        self.assertEqual(second_workspace, first_workspace)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "retain me")
        self.assertEqual(
            finder_output.read_text(encoding="utf-8"),
            "finder output",
        )
        self.assertTrue((second_workspace / "finder_file.config").is_file())

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
        event_data.get_latitude.return_value = 46.2
        event_data.get_longitude.return_value = 7.3
        event_data.get_depth.return_value = 10.0
        event_data.get_magnitude.return_value = 5.6
        event_data.get_origin_time.return_value = (
            "2026-08-10T08:15:30.250000Z"
        )
        records = [
            {
                "latitude": 46.1,
                "longitude": 7.2,
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
        expected_data, expected_channels = (
            finderexec.FinDerFormatterFromRawList.format(
                event_lat=event_data.get_latitude(),
                event_lon=event_data.get_longitude(),
                event_depth_km=event_data.get_depth(),
                event_mag=event_data.get_magnitude(),
                event_time_epoch=finderexec.get_epoch_time(
                    event_data.get_origin_time()
                ),
                station_list=records,
            )
        )
        identity = "event-one_t00010"
        original_import = builtins.__import__

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
            builtins,
            "__import__",
            side_effect=reject_downstream_import,
        ):
            config_path, data_path = executable.materialize_inputs(
                records,
                event_data,
                augmented_event_id=identity,
            )

        workspace = work_root / identity
        self.assertEqual(Path(config_path), workspace / "finder_file.config")
        self.assertEqual(Path(data_path), workspace / "data_0")
        self.assertEqual(Path(data_path).read_bytes(), expected_data)
        self.assertEqual(
            Path(config_path).read_text(encoding="utf-8"),
            "DATA_FOLDER {0}\nMODEL unchanged\n".format(workspace),
        )
        self.assertEqual(
            len(executable.get_finder_used_channels()),
            len(expected_channels),
        )
        check_executable.assert_not_called()
        run_finder.assert_not_called()
        process_constructor.assert_not_called()
        read_event.assert_not_called()
        read_rupture.assert_not_called()
        read_channels.assert_not_called()
        self.assertFalse(Path(executable.executable_path).exists())

    def test_execute_checks_binary_then_uses_shared_materialization_path(self):
        executable = self.executable(self.temporary_root / "runs")
        event_data = mock.Mock()
        event_data.get_event_id.return_value = "event-one"
        calls = []

        with mock.patch.object(
            executable,
            "_check_finder_executable",
            side_effect=lambda: calls.append("check"),
        ), mock.patch.object(
            executable,
            "materialize_inputs",
            side_effect=lambda *args, **kwargs: calls.append("materialize"),
        ) as materialize, mock.patch.object(
            executable,
            "_run_finder",
            side_effect=lambda: calls.append("run"),
        ), mock.patch.object(
            executable,
            "_collect_finder_output",
            side_effect=lambda **kwargs: calls.append("collect"),
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
        self.assertEqual(calls, ["check", "materialize", "run", "collect"])
        materialize.assert_called_once_with(
            [{"normalized": "record"}],
            event_data,
            augmented_event_id="event-one_t00010",
        )
        collect_output.assert_called_once_with(event_id="event-one")
        process_constructor.assert_not_called()

    def test_execute_preserves_rrsm_nested_event_identity(self):
        executable = self.executable(self.temporary_root / "runs")
        peak_motion = finderexec.PeakMotionData(data_dict={})
        nested_event = mock.Mock()
        nested_event.get_event_id.return_value = "rrsm-event"
        peak_motion.set_event_data(nested_event)
        supplied_event = mock.Mock(name="supplied_event")

        with mock.patch.object(
            peak_motion,
            "get_event_data",
            wraps=peak_motion.get_event_data,
        ) as get_event_data, mock.patch.object(
            executable,
            "_check_finder_executable",
        ), mock.patch.object(
            executable,
            "materialize_inputs",
        ) as materialize, mock.patch.object(
            executable,
            "_run_finder",
        ), mock.patch.object(
            executable,
            "_collect_finder_output",
        ) as collect_output:
            executable.execute(
                amplitudes=peak_motion,
                event_data=supplied_event,
                augmented_event_id="rrsm-event_t00010",
            )

        get_event_data.assert_called_once_with()
        materialize.assert_called_once_with(
            peak_motion,
            nested_event,
            augmented_event_id="rrsm-event_t00010",
        )
        collect_output.assert_called_once_with(event_id="rrsm-event")
        supplied_event.get_event_id.assert_not_called()

    def test_input_materialization_preserves_rrsm_formatter_event_behavior(self):
        executable = self.executable(self.temporary_root / "runs")
        peak_motion = finderexec.PeakMotionData(data_dict={})
        supplied_event = mock.Mock(name="supplied_event")
        channels = finderexec.FinderChannelList()

        with mock.patch.object(
            finderexec,
            "RRSMPeakMotionDataFormatter",
        ) as formatter_type, mock.patch.object(
            executable,
            "_check_finder_executable",
        ) as check_executable, mock.patch.object(
            executable,
            "_run_finder",
        ) as run_finder:
            formatter_type.return_value.format_data.return_value = (
                b"existing-rrsm-data",
                channels,
            )
            _config_path, data_path = executable.materialize_inputs(
                peak_motion,
                supplied_event,
                augmented_event_id="rrsm-event_t00010",
            )

        formatter_type.assert_called_once_with(logger=executable.logger)
        formatter_type.return_value.format_data.assert_called_once_with(
            amplitudes=peak_motion,
            event_data=peak_motion,
        )
        self.assertEqual(Path(data_path).read_bytes(), b"existing-rrsm-data")
        self.assertIs(executable.get_finder_used_channels(), channels)
        check_executable.assert_not_called()
        run_finder.assert_not_called()

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
