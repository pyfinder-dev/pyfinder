"""Offline tests for FinDerExecutable configuration ownership and use."""

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


class UncopyableConfigurationValue:
    """Fail executable ownership isolation during constructor validation."""

    def __deepcopy__(self, memo):
        raise RuntimeError("cannot copy configuration value")


class FinDerExecutableConfigurationTests(unittest.TestCase):
    def application_configuration(
        self,
        *,
        live_mode=False,
        felt_component="HNZ",
        margin_percent=1.0,
    ):
        return {
            "finder-executable": {
                "path": "/not-executed/finder_run",
                "output-root-folder": "/not-created/finder-output",
                "finder-live-mode": live_mode,
                "felt-report-component-code": felt_component,
                "artificial-point-margin-percent": margin_percent,
            },
            "nested-application-setting": {
                "values": ["retained", "independently"],
            },
        }

    def construct_executable(
        self,
        name,
        configuration,
        application_configuration=...,
    ):
        if application_configuration is ...:
            application_configuration = self.application_configuration()
        return finderexec.FinDerExecutable(
            options={"command_line_args": "offline-test"},
            configuration=application_configuration,
            finder_configuration_name=name,
            finder_configuration=configuration,
        )

    def assert_application_configuration_rejected(
        self,
        application_configuration,
        expected_message,
    ):
        with mock.patch.object(
            finderexec.os,
            "makedirs",
            autospec=True,
        ) as make_directories, mock.patch.object(
            finderexec.subprocess,
            "Popen",
            autospec=True,
        ) as process_constructor:
            with self.assertRaises(ValueError) as raised:
                self.construct_executable(
                    "global",
                    {"DATA_FOLDER": "source-data"},
                    application_configuration,
                )

        self.assertIn(expected_message, str(raised.exception))
        make_directories.assert_not_called()
        process_constructor.assert_not_called()

    def test_constructor_owns_independent_deep_configuration_copies(self):
        source = {
            "DATA_FOLDER": "source-data",
            "MODEL": {"name": "regional", "coefficients": [1, 2]},
        }
        original = deepcopy(source)
        application_configuration = self.application_configuration()
        original_application_configuration = deepcopy(
            application_configuration
        )

        executable = finderexec.FinDerExecutable(
            options={"command_line_args": "offline-test"},
            configuration=application_configuration,
            finder_configuration_name="regional",
            finder_configuration=source,
        )

        self.assertEqual(
            executable.configuration,
            original_application_configuration,
        )
        self.assertIsNot(executable.configuration, application_configuration)
        self.assertIsNot(
            executable.configuration["finder-executable"],
            application_configuration["finder-executable"],
        )
        self.assertIsNot(
            executable.configuration["nested-application-setting"]["values"],
            application_configuration["nested-application-setting"]["values"],
        )
        self.assertEqual(executable.finder_configuration_name, "regional")
        self.assertEqual(executable.finder_configuration, original)
        self.assertIsNot(executable.finder_configuration, source)
        self.assertIsNot(
            executable.finder_configuration["MODEL"],
            source["MODEL"],
        )

        executable.finder_configuration["MODEL"]["coefficients"].append(3)
        self.assertEqual(source, original)

    def test_caller_mutation_cannot_change_the_invocation_configuration(self):
        application_configuration = self.application_configuration(
            live_mode="YeS",
            felt_component="HG2",
            margin_percent=2,
        )
        executable = self.construct_executable(
            "global",
            {"DATA_FOLDER": "source-data"},
            application_configuration,
        )

        application_configuration["finder-executable"][
            "finder-live-mode"
        ] = "no"
        application_configuration["finder-executable"][
            "path"
        ] = "/caller-changed/finder_run"
        application_configuration["finder-executable"][
            "felt-report-component-code"
        ] = "BAD.VALUE"
        application_configuration["finder-executable"][
            "artificial-point-margin-percent"
        ] = -10
        application_configuration["nested-application-setting"][
            "values"
        ].append("caller-change")

        self.assertEqual(
            executable.configuration["finder-executable"][
                "finder-live-mode"
            ],
            "YeS",
        )
        self.assertEqual(
            executable.configuration["nested-application-setting"]["values"],
            ["retained", "independently"],
        )
        self.assertEqual(executable.executable_path, "/not-executed/finder_run")
        self.assertIs(executable.is_live_mode, True)
        self.assertEqual(executable.artificial_point_margin_percent, 2.0)

    def test_supported_live_mode_values_are_resolved_once(self):
        cases = (
            (True, True),
            (False, False),
            ("yes", True),
            ("YES", True),
            ("YeS", True),
            ("no", False),
            ("NO", False),
            ("nO", False),
        )
        for configured_value, expected in cases:
            with self.subTest(configured_value=configured_value), mock.patch.object(
                finderexec.FinDerExecutable,
                "_resolve_live_mode",
                wraps=finderexec.FinDerExecutable._resolve_live_mode,
            ) as resolve_live_mode:
                executable = self.construct_executable(
                    "global",
                    {"DATA_FOLDER": "source-data"},
                    self.application_configuration(live_mode=configured_value),
                )

            self.assertIs(executable.is_live_mode, expected)
            resolve_live_mode.assert_called_once_with(configured_value)

    def test_unsupported_live_mode_values_fail_visibly(self):
        invalid_values = (
            "true",
            "false",
            "on",
            "off",
            " yes ",
            "",
            0,
            1,
            None,
            [],
        )
        for configured_value in invalid_values:
            with self.subTest(configured_value=configured_value):
                self.assert_application_configuration_rejected(
                    self.application_configuration(
                        live_mode=configured_value
                    ),
                    "finder-executable.finder-live-mode",
                )

    def test_felt_component_is_not_required_by_the_executable(self):
        application_configuration = self.application_configuration()
        del application_configuration["finder-executable"][
            "felt-report-component-code"
        ]

        executable = self.construct_executable(
            "global",
            {"DATA_FOLDER": "source-data"},
            application_configuration,
        )

        self.assertFalse(hasattr(executable, "felt_report_component_code"))

    def test_felt_component_is_not_validated_or_stored_by_the_executable(self):
        executable = self.construct_executable(
            "global",
            {"DATA_FOLDER": "source-data"},
            self.application_configuration(felt_component="invalid.value"),
        )

        self.assertFalse(hasattr(executable, "felt_report_component_code"))

    def test_valid_artificial_margins_are_normalized_to_float(self):
        for configured_value, expected in ((0, 0.0), (3, 3.0), (2.75, 2.75)):
            with self.subTest(configured_value=configured_value):
                executable = self.construct_executable(
                    "global",
                    {"DATA_FOLDER": "source-data"},
                    self.application_configuration(
                        margin_percent=configured_value
                    ),
                )

                self.assertIsInstance(
                    executable.artificial_point_margin_percent,
                    float,
                )
                self.assertEqual(
                    executable.artificial_point_margin_percent,
                    expected,
                )

    def test_invalid_artificial_margins_fail_visibly(self):
        invalid_values = (
            True,
            False,
            -0.01,
            float("inf"),
            float("-inf"),
            float("nan"),
            "1.0",
            None,
            1 + 0j,
        )
        for configured_value in invalid_values:
            with self.subTest(configured_value=configured_value):
                self.assert_application_configuration_rejected(
                    self.application_configuration(
                        margin_percent=configured_value
                    ),
                    "finder-executable.artificial-point-margin-percent",
                )

    def test_run_finder_uses_the_stored_live_mode_decision(self):
        application_configuration = self.application_configuration(
            live_mode="YeS"
        )
        executable = self.construct_executable(
            "global",
            {"DATA_FOLDER": "source-data"},
            application_configuration,
        )
        application_configuration["finder-executable"][
            "finder-live-mode"
        ] = "no"
        executable.configuration["finder-executable"][
            "finder-live-mode"
        ] = "no"
        executable.finder_file_config_path = "/not-created/finder_file.config"
        executable.working_directory = "/not-created/workspace"
        executable.logger = mock.Mock()
        process = mock.Mock()
        process.communicate.return_value = (b"", b"")
        process.returncode = 0

        with mock.patch.object(
            finderexec.subprocess,
            "Popen",
            return_value=process,
        ) as process_constructor, mock.patch.object(
            executable,
            "_process_finder_output",
        ) as process_output:
            result = executable._run_finder()

        process_constructor.assert_called_once_with(
            [
                "/not-executed/finder_run",
                "/not-created/finder_file.config",
                "/not-created/workspace",
                "0",
                "0",
                "yes",
            ],
            stdout=finderexec.subprocess.PIPE,
            stderr=finderexec.subprocess.PIPE,
        )
        process_output.assert_called_once_with(b"", b"")
        self.assertEqual(result, (b"", b"", 0))

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
                    "VALUE": UncopyableConfigurationValue(),
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

    def test_invalid_application_inputs_fail_before_workspace_or_subprocess_work(self):
        base = self.application_configuration()
        missing_live_mode = deepcopy(base)
        del missing_live_mode["finder-executable"]["finder-live-mode"]
        missing_margin = deepcopy(base)
        del missing_margin["finder-executable"][
            "artificial-point-margin-percent"
        ]
        uncopyable = self.application_configuration()
        uncopyable["uncopyable"] = UncopyableConfigurationValue()
        cases = (
            (None, "application configuration must be a mapping"),
            ([], "application configuration must be a mapping"),
            (uncopyable, "cannot be isolated"),
            ({}, "finder-executable must be a mapping"),
            (
                {"finder-executable": []},
                "finder-executable must be a mapping",
            ),
            (missing_live_mode, "finder-live-mode is required"),
            (
                missing_margin,
                "artificial-point-margin-percent is required",
            ),
        )
        for application_configuration, expected_message in cases:
            with self.subTest(expected_message=expected_message):
                self.assert_application_configuration_rejected(
                    application_configuration,
                    expected_message,
                )

        with self.assertRaises(TypeError):
            finderexec.FinDerExecutable(
                options={"command_line_args": "offline-test"},
                configuration=self.application_configuration(),
            )


if __name__ == "__main__":
    unittest.main()
