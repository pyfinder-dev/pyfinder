"""Offline tests for explicit continuous ShakeMap deployment settings."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from pyfinder import cli
from pyfinder.pyfinderconfig import pyfinderconfig
from pyfinder.runtime import RuntimeBootstrapError
from pyfinder.services.shakemap_settings import continuous_shakemap_configuration


class ShakeMapSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = str(Path(self.temporary.name).resolve())

    def enabled_environment(self):
        return {
            "PYFINDER_SHAKEMAP_ENABLED": "true",
            "PYFINDER_SHAKEMAP_URL": "http://shakemap:8080",
            "PYFINDER_SHAKEMAP_INPUT_DIRECTORY": self.root,
        }

    def test_absence_preserves_disabled_defaults_and_independent_copy(self):
        expected = deepcopy(pyfinderconfig)
        configured = continuous_shakemap_configuration(pyfinderconfig, environment={})
        self.assertEqual(configured, expected)
        self.assertFalse(configured["shakemap"]["service-enabled"])
        configured["shakemap"]["configuration"] = "changed"
        self.assertEqual(pyfinderconfig, expected)

    def test_enablement_reuses_global_and_overwrite_defaults_without_io(self):
        with mock.patch("sqlite3.connect") as database, mock.patch(
            "urllib.request.OpenerDirector.open"
        ) as request:
            configured = continuous_shakemap_configuration(
                pyfinderconfig, environment=self.enabled_environment()
            )
        self.assertTrue(configured["shakemap"]["service-enabled"])
        self.assertEqual(configured["shakemap"]["configuration"], "global")
        self.assertTrue(configured["shakemap"]["overwrite"])
        database.assert_not_called()
        request.assert_not_called()
        self.assertEqual(list(Path(self.root).iterdir()), [])

    def test_explicit_regional_timeout_and_archive_choice_survive_verbatim(self):
        environment = self.enabled_environment() | {
            "PYFINDER_SHAKEMAP_CONFIGURATION": "switzerland",
            "PYFINDER_SHAKEMAP_REQUEST_TIMEOUT_SECONDS": "12.5",
            "PYFINDER_SHAKEMAP_OVERWRITE": "false",
        }
        settings = continuous_shakemap_configuration(
            pyfinderconfig, environment=environment
        )["shakemap"]
        self.assertEqual(settings["configuration"], "switzerland")
        self.assertEqual(settings["request-timeout-seconds"], 12.5)
        self.assertFalse(settings["overwrite"])
        self.assertEqual(settings["input-directory"], self.root)

    def test_bad_explicit_settings_fail_even_when_disabled_without_values(self):
        cases = {
            "PYFINDER_SHAKEMAP_ENABLED": ["", "True", "1"],
            "PYFINDER_SHAKEMAP_OVERWRITE": ["yes", "0"],
            "PYFINDER_SHAKEMAP_REQUEST_TIMEOUT_SECONDS": ["", "nan", "inf", "0", "-1"],
            "PYFINDER_SHAKEMAP_CONFIGURATION": ["", "../global"],
            "PYFINDER_SHAKEMAP_INPUT_DIRECTORY": ["", "relative", self.root + "/missing"],
            "PYFINDER_SHAKEMAP_URL": ["", "ftp://host", "https://user:private-password@host"],
        }
        for variable, values in cases.items():
            for value in values:
                with self.subTest(variable=variable, value=value):
                    with self.assertRaises(RuntimeBootstrapError) as error:
                        continuous_shakemap_configuration(
                            pyfinderconfig, environment={variable: value}
                        )
                    self.assertIn(variable, str(error.exception))
                    self.assertNotIn("private-password", str(error.exception))

    def test_enablement_requires_endpoint_and_shared_root(self):
        for missing in ("PYFINDER_SHAKEMAP_URL", "PYFINDER_SHAKEMAP_INPUT_DIRECTORY"):
            environment = self.enabled_environment()
            del environment[missing]
            with self.subTest(missing=missing), self.assertRaises(RuntimeBootstrapError) as error:
                continuous_shakemap_configuration(pyfinderconfig, environment=environment)
            self.assertIn(missing, str(error.exception))

    def test_symlink_input_root_is_rejected_without_resolving_into_configuration(self):
        alias = Path(self.root) / "alias"
        actual = Path(self.root) / "actual"
        actual.mkdir()
        alias.symlink_to(actual)
        environment = self.enabled_environment()
        environment["PYFINDER_SHAKEMAP_INPUT_DIRECTORY"] = str(alias)
        with self.assertRaises(RuntimeBootstrapError):
            continuous_shakemap_configuration(pyfinderconfig, environment=environment)

    def test_dispatch_leaves_settings_validation_to_workflow_composition(self):
        for command in (["playback", "--list"], ["playback", "--event-ids", "test"]):
            target = mock.Mock(return_value=0)
            module = mock.Mock(run_cli=target)
            with mock.patch.dict("os.environ", {"PYFINDER_SHAKEMAP_ENABLED": "invalid"}):
                result = cli.dispatch(
                    cli.build_parser().parse_args(command),
                    bootstrap=lambda workflow: object(), importer=lambda name: module,
                )
            self.assertEqual(result, 0)
            target.assert_called_once()


if __name__ == "__main__":
    unittest.main()
