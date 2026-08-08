"""Offline tests for FinDerExecutable native-configuration materialization."""

import atexit
from copy import deepcopy
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


_PARAMWS_LOG_DIRECTORY = tempfile.TemporaryDirectory(
    prefix="pyfinder-executable-config-unit-"
)
atexit.register(_PARAMWS_LOG_DIRECTORY.cleanup)
_original_paramws_log_file = os.environ.get("PARAMWS_LOG_FILE")
os.environ["PARAMWS_LOG_FILE"] = str(
    Path(_PARAMWS_LOG_DIRECTORY.name) / "paramws.log"
)
try:
    from pyfinder import finderexec
finally:
    if _original_paramws_log_file is None:
        os.environ.pop("PARAMWS_LOG_FILE", None)
    else:
        os.environ["PARAMWS_LOG_FILE"] = _original_paramws_log_file


class UncopyableNativeValue:
    """Fail executable ownership isolation during constructor validation."""

    def __deepcopy__(self, memo):
        raise RuntimeError("cannot copy native value")


class FinDerExecutableConfigurationTests(unittest.TestCase):
    def application_configuration(self):
        return {
            "finder-executable": {
                "path": "/not-executed/finder_run",
            }
        }

    def construct_executable(self, name, configuration):
        return finderexec.FinDerExecutable(
            options={"command_line_args": "offline-test"},
            configuration=self.application_configuration(),
            finder_configuration_name=name,
            finder_configuration=configuration,
        )

    def test_constructor_owns_a_deep_copy_separate_from_application_config(self):
        source = {
            "DATA_FOLDER": "source-data",
            "MODEL": {"name": "regional", "coefficients": [1, 2]},
        }
        original = deepcopy(source)
        application_configuration = self.application_configuration()

        executable = finderexec.FinDerExecutable(
            options={"command_line_args": "offline-test"},
            configuration=application_configuration,
            finder_configuration_name="regional",
            finder_configuration=source,
        )

        self.assertIs(executable.configuration, application_configuration)
        self.assertEqual(executable.finder_configuration_name, "regional")
        self.assertEqual(executable.finder_configuration, original)
        self.assertIsNot(executable.finder_configuration, source)
        self.assertIsNot(
            executable.finder_configuration["MODEL"],
            source["MODEL"],
        )

        executable.finder_configuration["MODEL"]["coefficients"].append(3)
        self.assertEqual(source, original)

    def test_materialization_replaces_only_data_folder_in_mapping_order(self):
        source = {
            "THRESHOLDS": "2 0.1 2.0",
            "DATA_FOLDER": "source-data",
            "MODEL": "regional-specific",
            "MAG_REGRESSION_THRESH": 5.5,
        }
        source_before = deepcopy(source)
        executable = self.construct_executable("regional", source)
        executable_owned_before = deepcopy(executable.finder_configuration)
        executable.logger = mock.Mock()

        with tempfile.TemporaryDirectory(
            prefix="pyfinder-native-config-"
        ) as working_directory, mock.patch.object(
            finderexec.subprocess,
            "Popen",
            autospec=True,
        ) as process_constructor:
            executable.working_directory = working_directory
            executable._write_finder_configuration()
            config_path = Path(executable.finder_file_config_path)
            emitted = config_path.read_text(encoding="utf-8")

        expected = "".join(
            "{} {}\n".format(
                key,
                working_directory if key == "DATA_FOLDER" else value,
            )
            for key, value in source.items()
        )
        self.assertEqual(emitted, expected)
        self.assertIn("MODEL regional-specific\n", emitted)
        self.assertEqual(source, source_before)
        self.assertEqual(
            executable.finder_configuration,
            executable_owned_before,
        )
        executable.logger.info.assert_any_call(
            "Selected FinDer configuration: %s",
            "regional",
        )
        process_constructor.assert_not_called()

    def test_two_executables_cannot_mutate_each_other_or_shared_source(self):
        shared_source = {
            "DATA_FOLDER": "source-data",
            "MODEL": {"coefficients": [1, 2]},
        }
        original = deepcopy(shared_source)

        first = self.construct_executable("first", shared_source)
        second = self.construct_executable("second", shared_source)
        first.finder_configuration["MODEL"]["coefficients"].append(99)

        self.assertEqual(second.finder_configuration, original)
        self.assertEqual(shared_source, original)
        self.assertIsNot(
            first.finder_configuration,
            second.finder_configuration,
        )

    def test_invalid_native_inputs_fail_before_workspace_or_subprocess_work(self):
        valid_configuration = {"DATA_FOLDER": "source-data"}
        cases = (
            ("missing name", None, valid_configuration, ValueError),
            ("empty name", "", valid_configuration, ValueError),
            ("whitespace name", "   ", valid_configuration, ValueError),
            ("missing mapping", "global", None, ValueError),
            ("empty mapping", "global", {}, ValueError),
            ("nonmapping", "global", [], ValueError),
            (
                "uncopyable mapping",
                "global",
                {
                    "DATA_FOLDER": "source-data",
                    "VALUE": UncopyableNativeValue(),
                },
                ValueError,
            ),
        )
        for label, name, configuration, error_type in cases:
            with self.subTest(label=label), mock.patch.object(
                finderexec.os,
                "makedirs",
                autospec=True,
            ) as make_directories, mock.patch.object(
                finderexec.subprocess,
                "Popen",
                autospec=True,
            ) as process_constructor:
                with self.assertRaises(error_type):
                    self.construct_executable(name, configuration)

                make_directories.assert_not_called()
                process_constructor.assert_not_called()

        with self.assertRaises(TypeError):
            finderexec.FinDerExecutable(
                options={"command_line_args": "offline-test"},
                configuration=self.application_configuration(),
            )


if __name__ == "__main__":
    unittest.main()
