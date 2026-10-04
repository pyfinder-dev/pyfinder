"""Read-only caller diagnostics stay separate from runtime and scientific work."""

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from pyfinder.pyfinderconfig import pyfinderconfig
from pyfinder.services.shakemap_settings import check_configuration
from pyfinder.services.shakemap_diagnostics import regional_fallback_policy


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class CallerCheckTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.inputs = Path(self.directory.name).resolve()
        self.environment = {
            "PYFINDER_SHAKEMAP_ENABLED": "true",
            "PYFINDER_SHAKEMAP_URL": "http://service.invalid:8080",
            "PYFINDER_SHAKEMAP_INPUT_DIRECTORY": "/container/mount/inputs",
            "PYFINDER_SHAKEMAP_CONFIGURATION": "switzerland",
        }

    def test_host_mount_mapping_preserves_requested_profile_without_runtime_writes(self):
        configuration = deepcopy(pyfinderconfig)
        before = deepcopy(configuration)
        with mock.patch("pyfinder.runtime.bootstrap_process", side_effect=AssertionError("no bootstrap")), \
             mock.patch("socket.create_connection", side_effect=AssertionError("no network")), \
             mock.patch("sqlite3.connect", side_effect=AssertionError("no database")):
            result = check_configuration(
                configuration,
                environment=self.environment,
                input_directory=self.inputs,
            )

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["settings"]["selected_configuration"], "switzerland")
        self.assertEqual(result["settings"]["input_directory"], str(self.inputs))
        self.assertEqual(configuration, before)
        self.assertEqual(list(self.inputs.iterdir()), [])
        self.assertEqual(self.environment["PYFINDER_SHAKEMAP_INPUT_DIRECTORY"], "/container/mount/inputs")
        self.assertNotIn("service.invalid", json.dumps(result))
        self.assertEqual(result["fallback"]["global_success"], "unverified")

    def test_disabled_full_chain_is_explicitly_blocked(self):
        result = check_configuration(pyfinderconfig, environment={})
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["settings"]["enabled"])
        self.assertIn("disabled", result["checks"][0]["reason"])

    def test_invalid_endpoint_never_echoes_credentials(self):
        environment = {
            **self.environment,
            "PYFINDER_SHAKEMAP_URL": "http://private-user:private-secret@host",
        }
        result = check_configuration(
            pyfinderconfig, environment=environment, input_directory=self.inputs,
        )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["settings"], {})
        self.assertIn("PYFINDER_SHAKEMAP_URL", result["checks"][0]["reason"])
        self.assertNotIn("private-", json.dumps(result))

    def test_missing_input_path_is_not_created_or_reported_ready(self):
        missing = self.inputs / "not-created"
        result = check_configuration(
            pyfinderconfig, environment=self.environment, input_directory=missing,
        )
        self.assertEqual(result["status"], "blocked")
        self.assertIn("PYFINDER_SHAKEMAP_INPUT_DIRECTORY", result["checks"][0]["reason"])
        self.assertFalse(missing.exists())

    def test_fallback_metadata_does_not_treat_unverified_failure_as_permission(self):
        policy = regional_fallback_policy()
        self.assertTrue(policy["requested_configuration_first"])
        self.assertTrue(policy["requires_confirmed_regional_failure"])
        self.assertTrue(policy["requires_matching_accepted_sequence_and_configuration"])
        self.assertEqual(set(policy["eligible_failure_codes"]), {
            "configuration_materialization_failed", "native_configuration_failed",
        })
        self.assertEqual(policy["global_success"], "unverified")

    def test_component_cli_returns_json_without_creating_runtime(self):
        environment = dict(os.environ)
        environment.update(self.environment)
        completed = subprocess.run(
            [sys.executable, "-B", "-m", "pyfinder.services.shakemap_settings",
             "--check", "--input-directory", str(self.inputs)],
            cwd=PROJECT_ROOT, env=environment, capture_output=True, text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["settings"]["selected_configuration"], "switzerland")
        self.assertEqual(list(self.inputs.iterdir()), [])


class ImageVerifierArgumentTests(unittest.TestCase):
    def test_help_and_unknown_arguments_do_not_reach_docker_or_tempfile_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command_log = root / "commands"
            for command in ("docker", "mktemp"):
                executable = root / command
                executable.write_text(
                    '#!/bin/bash\nprintf "%s\\n" "$0" >> "$CALL_LOG"\nexit 99\n'
                )
                executable.chmod(0o755)
            environment = {
                **os.environ,
                "PATH": str(root) + os.pathsep + os.environ["PATH"],
                "CALL_LOG": str(command_log),
            }
            for arguments, expected in ((["--help"], 0), (["-h"], 0),
                                        (["--unknown"], 2), (["--help", "extra"], 2)):
                with self.subTest(arguments=arguments):
                    result = subprocess.run(
                        ["bash", str(PROJECT_ROOT / "scripts/verify-pyfinder-image.sh"), *arguments],
                        env=environment, capture_output=True, text=True, check=False,
                    )
                    self.assertEqual(result.returncode, expected, result.stderr)
                    self.assertFalse(command_log.exists())
                    if expected == 0:
                        self.assertIn("not a read-only check", result.stdout)
                        self.assertIn("make check", result.stdout)
