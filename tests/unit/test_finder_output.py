"""Offline tests for FinDer output parsing and solution exposure."""

import atexit
from pathlib import Path
import os
import tempfile
import unittest
from unittest import mock


_PARAMWS_LOG_DIRECTORY = tempfile.TemporaryDirectory(
    prefix="pyfinder-output-import-"
)
atexit.register(_PARAMWS_LOG_DIRECTORY.cleanup)
_original_paramws_log_file = os.environ.get("PARAMWS_LOG_FILE")
os.environ["PARAMWS_LOG_FILE"] = str(
    Path(_PARAMWS_LOG_DIRECTORY.name) / "paramws.log"
)
try:
    from pyfinder import finderexec, findermanager
    from pyfinder.finderutils import (
        FinderChannel,
        FinderChannelList,
        FinderSolution,
        read_finder_channels_from_file,
    )
    from pyfinder.pyfinderconfig import ESM_SHAKEMAP_SERVICE
finally:
    if _original_paramws_log_file is None:
        os.environ.pop("PARAMWS_LOG_FILE", None)
    else:
        os.environ["PARAMWS_LOG_FILE"] = _original_paramws_log_file


class ControlledManagerEvent:
    """Supply only the event values the normal manager path consumes."""

    def get_event_id(self):
        return "controlled-event"

    def get_origin_time(self):
        return "controlled-origin"

    def get_longitude(self):
        return 7.0

    def get_latitude(self):
        return 46.0

    def get_magnitude(self):
        return 5.0

    def get_depth(self):
        return 8.0

    def get_magnitude_type(self):
        return "Mw"


class FinDerOutputTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory(
            prefix="pyfinder-output-"
        )
        self.temporary_root = Path(self.temporary_directory.name)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def executable(self, *, use_finder_amplitudes=False):
        logger = mock.Mock()
        executable = finderexec.FinDerExecutable(
            options={"command_line_args": "output-test"},
            configuration={
                "finder-executable": {
                    "path": "/not-executed/finder_run",
                    "output-root-folder": str(self.temporary_root),
                    "finder-live-mode": False,
                    "artificial-point-margin-percent": 1.0,
                },
                "shakemap": {
                    "use-amplitude-from-finder-output": (
                        use_finder_amplitudes
                    ),
                },
            },
            finder_configuration_name="global",
            finder_configuration={"DATA_FOLDER": "unused"},
            logger=logger,
        )
        executable.working_directory = str(self.temporary_root / "workspace")
        executable.finder_event_id = "controlled-finder-id"
        executable.finder_used_channels = FinderChannelList([
            FinderChannel(
                latitude=45.8,
                longitude=7.1,
                sncl="CH.RAW.00.HNZ",
                pga=3.5,
                is_artificial=False,
            ),
        ])
        return executable

    def output_directory(self, executable):
        return (
            Path(executable.working_directory)
            / "temp_data"
            / executable.finder_event_id
        )

    def write_valid_outputs(self, executable):
        output_directory = self.output_directory(executable)
        output_directory.mkdir(parents=True, exist_ok=True)
        (output_directory / "core_info_0").write_text(
            "1700000000\n5.2\n46.0 7.0\n-8.0\n",
            encoding="utf-8",
        )
        (output_directory / "finder_rupture_list_0").write_text(
            "2\n46.0 7.0 8.0\n46.1 7.1 9.0\n",
            encoding="utf-8",
        )
        (output_directory / "data_0").write_text(
            "# controlled header\n"
            "46.2 7.2 CH.PROCESSED.00.HNZ 1 12.5\n",
            encoding="utf-8",
        )
        return output_directory

    def run_manager_with_solution(self, solution):
        """Cross the normal manager executable branch with controlled inputs."""
        workspace = self.temporary_root / "manager-workspace"
        finder_event_id = "controlled-finder-id"
        output_directory = workspace / "temp_data" / finder_event_id
        output_directory.mkdir(parents=True)
        data_path = output_directory / "data_0"
        original_data = (
            b"# controlled header\n"
            b"46.0 7.0 finder.assigned.00.HNZ 1 2.5\n"
        )
        data_path.write_bytes(original_data)

        logger = mock.Mock()
        manager = findermanager.FinDerManager.for_on_demand(
            options={"use_library": False},
            configuration={
                "general": {
                    "services-enabled": [ESM_SHAKEMAP_SERVICE],
                    "services-priority": [ESM_SHAKEMAP_SERVICE],
                },
                "finder-executable": {
                    "finder-temp-data-dir": (
                        "{FINDER_RUN_DIR}/temp_data"
                    ),
                    "finder-temp-dir": "{FINDER_RUN_DIR}/temp",
                    "finder-live-mode": False,
                },
            },
            finder_configuration_name="controlled",
            finder_configuration={"DATA_FOLDER": "unused"},
            logger=logger,
        )

        event_context = ControlledManagerEvent()
        outcome = manager._new_provider_outcome()
        outcome["status_code"] = 200
        outcome["event_context_usable"] = True
        acquired = {
            ESM_SHAKEMAP_SERVICE: {
                "event_context": event_context,
                "scientific_value": object(),
                "outcome": outcome,
            },
        }
        normalized_records = [{"controlled": "normalized-record"}]
        executable = mock.Mock()
        executable.execute.return_value = executable
        executable.get_finder_solution_object.return_value = solution
        executable.get_working_directory.return_value = str(workspace)
        executable.get_finder_event_id.return_value = finder_event_id

        with mock.patch.object(
            manager,
            "_acquire_enabled_providers",
            return_value=acquired,
        ), mock.patch.object(
            manager,
            "_normalize_acquired_providers",
            return_value={ESM_SHAKEMAP_SERVICE: normalized_records},
        ), mock.patch.object(
            manager,
            "_merge_available_results",
            return_value=normalized_records,
        ), mock.patch.object(
            finderexec,
            "FinDerExecutable",
            return_value=executable,
        ):
            result = manager.process_event("controlled-event")

        return {
            "data_path": data_path,
            "executable": executable,
            "finder_event_id": finder_event_id,
            "logger": logger,
            "manager": manager,
            "original_data": original_data,
            "result": result,
            "workspace": workspace,
        }

    def assert_logged_no_solution(self, executable):
        self.assertIsNone(executable.finder_solution)
        self.assertIsNone(executable.get_finder_solution_object())
        executable.logger.error.assert_called()

    def test_artificial_marker_is_callable_and_setter_backed(self):
        real_channel = FinderChannel(is_artificial=False)
        artificial_channel = FinderChannel(is_artificial=True)

        self.assertIs(real_channel.is_artificial(), False)
        self.assertIs(artificial_channel.is_artificial(), True)

        real_channel.set_artificial(True)
        artificial_channel.set_artificial(False)

        self.assertIs(real_channel.is_artificial(), True)
        self.assertIs(artificial_channel.is_artificial(), False)

    def test_processed_reader_skips_malformed_row_and_preserves_later_row(self):
        data_path = self.temporary_root / "data_0"
        data_path.write_text(
            "# controlled header\n"
            "46.0 7.0 CH.FIRST.00.HNZ 1 1.5\n"
            "malformed row\n"
            "46.2 7.2 IV.LATER..HNE 0 2.5\n",
            encoding="utf-8",
        )

        channels = read_finder_channels_from_file(str(data_path))

        self.assertEqual(
            [channel.get_sncl() for channel in channels],
            ["CH.FIRST.00.HNZ", "IV.LATER..HNE"],
        )
        self.assertEqual([channel.get_pga() for channel in channels], [1.5, 2.5])

    def test_zero_valid_processed_channels_exposes_logged_none(self):
        executable = self.executable()
        output_directory = self.write_valid_outputs(executable)
        (output_directory / "data_0").write_text(
            "# controlled header\nmalformed row\n",
            encoding="utf-8",
        )

        executable._collect_finder_output(event_id="controlled-event")

        self.assert_logged_no_solution(executable)

    def test_missing_and_unparseable_event_output_expose_logged_none(self):
        cases = (
            ("missing", None),
            ("incomplete", "1700000000\n"),
            ("invalid-number", "not-an-integer\n5.2\n46.0 7.0\n-8.0\n"),
        )
        for name, event_content in cases:
            with self.subTest(name=name):
                executable = self.executable()
                output_directory = self.write_valid_outputs(executable)
                event_path = output_directory / "core_info_0"
                if event_content is None:
                    event_path.unlink()
                else:
                    event_path.write_text(event_content, encoding="utf-8")

                executable._collect_finder_output(event_id="controlled-event")

                self.assert_logged_no_solution(executable)

    def test_missing_and_unparseable_rupture_output_expose_logged_none(self):
        cases = (
            ("missing", None),
            ("invalid-point", "1\nnot-a-rupture-point\n"),
        )
        for name, rupture_content in cases:
            with self.subTest(name=name):
                executable = self.executable()
                output_directory = self.write_valid_outputs(executable)
                rupture_path = output_directory / "finder_rupture_list_0"
                if rupture_content is None:
                    rupture_path.unlink()
                else:
                    rupture_path.write_text(rupture_content, encoding="utf-8")

                executable._collect_finder_output(event_id="controlled-event")

                self.assert_logged_no_solution(executable)

    def test_configuration_preserves_raw_and_processed_solution_views(self):
        executable = self.executable(use_finder_amplitudes=False)
        self.write_valid_outputs(executable)
        executable._collect_finder_output(event_id="controlled-event")
        processed_solution = executable.finder_solution
        raw_solution = processed_solution.input_solution

        self.assertIs(executable.get_finder_solution_object(), raw_solution)

        executable.configuration["shakemap"][
            "use-amplitude-from-finder-output"
        ] = True
        self.assertIs(
            executable.get_finder_solution_object(),
            processed_solution,
        )

    def test_manager_exposes_executable_none_with_error_diagnostic(self):
        observed = self.run_manager_with_solution(None)

        self.assertIsNone(observed["result"])
        observed["logger"].error.assert_called()

    def test_successful_non_live_manager_keeps_data_without_renamed_copy(self):
        solution = FinderSolution(event_id="controlled-event")

        observed = self.run_manager_with_solution(solution)

        self.assertIs(observed["result"], solution)
        self.assertEqual(
            observed["data_path"].read_bytes(),
            observed["original_data"],
        )
        self.assertFalse(
            observed["data_path"].with_name("data_0_renamed").exists()
        )
        manager = observed["manager"]
        workspace = observed["workspace"]
        self.assertEqual(manager.working_dir, str(workspace))
        self.assertEqual(
            manager.finder_temp_data_dir,
            str(workspace / "temp_data" / observed["finder_event_id"]),
        )
        self.assertEqual(manager.finder_temp_dir, str(workspace / "temp"))


if __name__ == "__main__":
    unittest.main()
