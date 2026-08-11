"""Host-side safety tests for installed PyFinder image verification."""

import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
VERIFIER = PROJECT_ROOT / "scripts/verify-pyfinder-image.sh"
HELPER = PROJECT_ROOT / "tests/container/verify_installed_image.py"
DOCKERIGNORE = PROJECT_ROOT / ".dockerignore"
CONTAINER_NAME = "pyfinder-docker"
IMAGE_NAME = "pyfinder:dev"
OWNERSHIP_LABEL = "io.pyfinder.verification=installed-image"
OWNED_CONTAINER_ID = "a" * 64
OTHER_CONTAINER_ID = "b" * 64
OBSERVED_IMAGE_ID = "sha256:" + "c" * 64
OBSERVED_BASE_DIGEST = "sha256:" + "d" * 64
OBSERVED_PYTHON_VERSION = "3.12.99"


def dockerignore_rules():
    return tuple(
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def rule_matches(relative_path, rule):
    pattern = rule[1:] if rule.startswith("!") else rule
    if pattern == "**":
        return True
    if pattern.endswith("/"):
        return False
    return PurePosixPath(relative_path).match(pattern)


def is_ignored(relative_path):
    ignored = False
    for rule in dockerignore_rules():
        if rule_matches(relative_path, rule):
            ignored = not rule.startswith("!")
    return ignored


class ImageVerifierSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.verifier_contents = VERIFIER.read_text(encoding="utf-8")
        cls.helper_contents = HELPER.read_text(encoding="utf-8")
        cls.test_contents = Path(__file__).read_text(encoding="utf-8")

    def run_fake_docker(self, scenario):
        with tempfile.TemporaryDirectory(
            prefix="pyfinder-image-verifier-docker-"
        ) as temporary_directory:
            temporary_root = Path(temporary_directory)
            fake_bin = temporary_root / "bin"
            fake_state = temporary_root / "state"
            command_log = temporary_root / "docker-commands.log"
            fake_bin.mkdir()
            fake_state.mkdir()
            fake_docker = fake_bin / "docker"
            fake_docker.write_text(
                """#!/bin/bash
set -eu

printf '%s\\n' "$*" >> "${FAKE_DOCKER_LOG:?}"

if [[ "${1:-}" == "container" && "${2:-}" == "ls" ]]; then
    if [[ "${FAKE_DOCKER_SCENARIO:?}" == "preexisting" ]]; then
        printf 'pyfinder-docker\\n'
    elif [[ "${FAKE_DOCKER_SCENARIO}" == "missing-cid" \
        && -e "${FAKE_DOCKER_STATE:?}/race-container" ]]; then
        printf 'pyfinder-docker\\n'
    fi
    exit 0
fi

if [[ "${1:-}" == "image" && "${2:-}" == "inspect" ]]; then
    printf '%s|linux|amd64|1000:1000|["/usr/local/bin/pyfinder-entrypoint"]|["continuous"]|ghcr.io/sceylan/finder-base:gmt5|%s|%s\\n' \
        "${FAKE_IMAGE_ID:?}" \
        "${FAKE_BASE_DIGEST:?}" \
        "${FAKE_PYTHON_VERSION:?}"
    exit 0
fi

if [[ "${1:-}" == "run" ]]; then
    cidfile=""
    previous=""
    for argument in "$@"; do
        if [[ "$previous" == "--cidfile" ]]; then
            cidfile="$argument"
            break
        fi
        previous="$argument"
    done
    [[ -n "$cidfile" ]] || exit 91

    if [[ "${FAKE_DOCKER_SCENARIO}" == "missing-cid" ]]; then
        : > "${FAKE_DOCKER_STATE}/race-container"
        kill -TERM "$PPID"
        sleep 0.1
        exit 143
    fi

    printf '%s\\n' "${FAKE_OWNED_CONTAINER_ID:?}" > "$cidfile"
    kill -TERM "$PPID"
    sleep 0.1
    exit 143
fi

if [[ "${1:-}" == "container" && "${2:-}" == "inspect" ]]; then
    case "${FAKE_DOCKER_SCENARIO:?}" in
        different-id)
            printf '%s|installed-image\\n' "${FAKE_OTHER_CONTAINER_ID:?}"
            ;;
        different-label)
            printf '%s|different-owner\\n' "${FAKE_OWNED_CONTAINER_ID:?}"
            ;;
        matching-owner)
            printf '%s|installed-image\\n' "${FAKE_OWNED_CONTAINER_ID:?}"
            ;;
        *)
            exit 92
            ;;
    esac
    exit 0
fi

if [[ "${1:-}" == "container" && "${2:-}" == "rm" ]]; then
    exit 0
fi

printf 'unexpected fake Docker command: %s\\n' "$*" >&2
exit 90
""",
                encoding="utf-8",
            )
            fake_docker.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "FAKE_DOCKER_LOG": str(command_log),
                    "FAKE_DOCKER_SCENARIO": scenario,
                    "FAKE_DOCKER_STATE": str(fake_state),
                    "FAKE_IMAGE_ID": OBSERVED_IMAGE_ID,
                    "FAKE_BASE_DIGEST": OBSERVED_BASE_DIGEST,
                    "FAKE_PYTHON_VERSION": OBSERVED_PYTHON_VERSION,
                    "FAKE_OWNED_CONTAINER_ID": OWNED_CONTAINER_ID,
                    "FAKE_OTHER_CONTAINER_ID": OTHER_CONTAINER_ID,
                    "PATH": os.pathsep.join(
                        (str(fake_bin), environment["PATH"])
                    ),
                }
            )

            completed = subprocess.run(
                [str(VERIFIER)],
                cwd=temporary_root,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            commands = command_log.read_text(encoding="utf-8").splitlines()
        return completed, commands

    def test_preexisting_canonical_container_blocks_before_docker_run(self):
        completed, commands = self.run_fake_docker("preexisting")

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("already exists; leaving it untouched", completed.stderr)
        self.assertEqual(len(commands), 1)
        self.assertIn("container ls --all", commands[0])
        self.assertIn("name=^pyfinder-docker$", commands[0])
        self.assertFalse(any(command.startswith("run ") for command in commands))
        self.assertFalse(any(command.startswith("container rm ") for command in commands))

    def test_interruption_before_cidfile_never_inspects_or_removes(self):
        completed, commands = self.run_fake_docker("missing-cid")

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(sum(command.startswith("run ") for command in commands), 1)
        self.assertFalse(any(command.startswith("container inspect ") for command in commands))
        self.assertFalse(any(command.startswith("container rm ") for command in commands))

    def test_private_container_id_must_match_canonical_container_id(self):
        completed, commands = self.run_fake_docker("different-id")

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("ID does not match the private container ID", completed.stderr)
        self.assertTrue(any(command.startswith("container inspect ") for command in commands))
        self.assertFalse(any(command.startswith("container rm ") for command in commands))

    def test_expected_label_is_required_after_container_id_matches(self):
        completed, commands = self.run_fake_docker("different-label")

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("label does not match", completed.stderr)
        self.assertFalse(any(command.startswith("container rm ") for command in commands))

    def test_matching_private_id_canonical_id_and_label_authorize_removal(self):
        completed, commands = self.run_fake_docker("matching-owner")

        self.assertNotEqual(completed.returncode, 0)
        removal_commands = [
            command
            for command in commands
            if command.startswith("container rm ")
        ]
        self.assertEqual(
            removal_commands,
            ["container rm --force {0}".format(OWNED_CONTAINER_ID)],
        )
        self.assertNotIn(CONTAINER_NAME, removal_commands[0])

    def test_every_run_uses_the_inspected_immutable_image_id(self):
        self.assertEqual(self.verifier_contents.count("docker run"), 1)
        self.assertEqual(self.verifier_contents.count("docker image inspect"), 1)
        self.assertIn(
            'command+=("$OBSERVED_IMAGE_ID" "$@")',
            self.verifier_contents,
        )
        self.assertNotIn(
            'command+=("$IMAGE_NAME" "$@")',
            self.verifier_contents,
        )

        for scenario in (
            "missing-cid",
            "different-id",
            "different-label",
            "matching-owner",
        ):
            with self.subTest(scenario=scenario):
                _completed, commands = self.run_fake_docker(scenario)
                run_commands = [
                    command for command in commands if command.startswith("run ")
                ]
                self.assertEqual(len(run_commands), 1)
                self.assertIn(OBSERVED_IMAGE_ID, run_commands[0])
                self.assertNotIn(IMAGE_NAME, run_commands[0])
                self.assertIn(
                    "PYFINDER_IMAGE_BASE_DIGEST={0}".format(
                        OBSERVED_BASE_DIGEST
                    ),
                    run_commands[0],
                )
                self.assertIn(
                    "PYFINDER_IMAGE_PYTHON_VERSION={0}".format(
                        OBSERVED_PYTHON_VERSION
                    ),
                    run_commands[0],
                )

    def test_container_run_keeps_fixed_identity_and_isolation(self):
        self.assertIn('readonly CONTAINER_NAME="pyfinder-docker"', self.verifier_contents)
        self.assertIn('readonly IMAGE_NAME="pyfinder:dev"', self.verifier_contents)
        for fragment in (
            "--rm",
            "--interactive",
            '--name "$CONTAINER_NAME"',
            '--cidfile "$CONTAINER_CID_FILE"',
            '--label "${OWNERSHIP_LABEL_KEY}=${OWNERSHIP_LABEL_VALUE}"',
            "--network none",
            "--platform linux/amd64",
            "--pull=never",
            '--user "$CONTAINER_USER"',
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.verifier_contents)
        ownership_key, ownership_value = OWNERSHIP_LABEL.split("=", 1)
        self.assertIn(
            'readonly OWNERSHIP_LABEL_KEY="{0}"'.format(ownership_key),
            self.verifier_contents,
        )
        self.assertIn(
            'readonly OWNERSHIP_LABEL_VALUE="{0}"'.format(ownership_value),
            self.verifier_contents,
        )

    def test_no_stored_image_id_or_result_field_remains(self):
        combined = "\n".join(
            (self.verifier_contents, self.helper_contents, self.test_contents)
        )
        self.assertNotIn("EXPECTED_" + "IMAGE_ID", combined)
        self.assertNotIn('"expected_' + 'image_id"', combined)
        self.assertNotIn("sha256:" + "42fe", combined)

    def test_no_alternate_name_service_start_or_deployment_root_is_present(self):
        self.assertEqual(
            self.verifier_contents.count("readonly CONTAINER_NAME="),
            1,
        )
        for forbidden in (
            "uuidgen",
            "RANDOM",
            "container_name=",
            "pyfinder-docker-",
            "/Users/savas/my-codes/eew/pyfinder-dev/pyfinder-deploy",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.verifier_contents)
                self.assertNotIn(forbidden, self.helper_contents)
        self.assertIsNone(
            re.search(r"run_image[^\n]*\bcontinuous\b", self.verifier_contents)
        )
        self.assertIn('[pyfinder_command, *command, "--help"]', self.helper_contents)

    def test_materialization_helper_blocks_external_actions(self):
        required_guards = (
            '"_run_finder"',
            'finderexec.subprocess, "Popen"',
            '"read_event_solution_from_file"',
            '"read_rupture_polygon_from_file"',
            '"read_finder_channels_from_file"',
            'BaseWebServiceConnector, "query"',
            'BaseWebServiceConnector, "open_url"',
            'socket, "socket"',
            'smtplib, "SMTP"',
            'smtplib, "SMTP_SSL"',
            '"pyfinder.utils.shakemap"',
            '"pyfinder.services.alert"',
        )
        for guard in required_guards:
            with self.subTest(guard=guard):
                self.assertIn(guard, self.helper_contents)
        self.assertIn("executable.materialize_inputs(", self.helper_contents)
        self.assertNotIn("executable.execute(", self.helper_contents)

    def test_verifier_and_helper_are_excluded_from_the_image_context(self):
        self.assertTrue(is_ignored("scripts/verify-pyfinder-image.sh"))
        self.assertTrue(is_ignored("tests/container/verify_installed_image.py"))

    def test_verifier_has_valid_bash_syntax(self):
        completed = subprocess.run(
            ["bash", "-n", str(VERIFIER)],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
