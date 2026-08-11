"""Level-1 tests for the fixed host launcher and shell completion."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = PROJECT_ROOT / "scripts/pyfinder"
BASH_COMPLETION = PROJECT_ROOT / "scripts/completion/pyfinder.bash"
ZSH_COMPLETION = PROJECT_ROOT / "scripts/completion/_pyfinder"

CONTAINER_NAME = "pyfinder-docker"
IMAGE_NAME = "pyfinder:dev"
DEPLOYMENT_ROOT = Path(
    "/Users/savas/my-codes/eew/pyfinder-dev/pyfinder-deploy"
)
RUNTIME_ROOT = DEPLOYMENT_ROOT / "runtime"
SERVICE_ROOT = RUNTIME_ROOT / "pyfinder"
CONTAINER_RUNTIME = "/home/sysop/runtime"
REQUIRED_DIRECTORIES = tuple(
    str(SERVICE_ROOT / branch)
    for branch in ("state", "logs", "runs", "playbacks")
)
EXPECTED_MOUNT = "bind|{0}|{1}|true".format(
    RUNTIME_ROOT,
    CONTAINER_RUNTIME,
)
FAKE_DOCKER = r'''#!/bin/bash
set -eu

record_file="${FAKE_RECORD_FILE:?}"
printf 'docker' >> "$record_file"
for argument in "$@"; do
    printf '\t%s' "$argument" >> "$record_file"
done
printf '\n' >> "$record_file"

if [[ "${1:-}" == "container" && "${2:-}" == "ls" ]]; then
    if [[ "${FAKE_CONTAINER_QUERY_FAIL:-0}" == "1" ]]; then
        printf 'fake Docker container query failure\n' >&2
        exit 42
    fi
    if [[ "${FAKE_CONTAINER_STATE:-missing}" != "missing" ]]; then
        printf '%s\n' "${FAKE_CONTAINER_LIST_OUTPUT:-pyfinder-docker}"
    fi
    exit 0
fi

if [[ "${1:-}" == "container" && "${2:-}" == "inspect" ]]; then
    if [[ "${FAKE_CONTAINER_STATE:-missing}" == "missing" ]]; then
        exit 1
    fi
    format="${4:-}"
    case "$format" in
        '{{.Id}}')
            printf '%s\n' "${FAKE_CONTAINER_ID:-container-id}"
            ;;
        *'.Config.Image'*)
            if [[ "${FAKE_CONTAINER_STATE}" == "running" ]]; then
                running=true
            else
                running=false
            fi
            printf '%s|%s|%s|%s|%s|%s\n' \
                "${FAKE_CONFIG_IMAGE:-pyfinder:dev}" \
                "${FAKE_CONTAINER_IMAGE_ID:-sha256:current}" \
                "${FAKE_CONFIG_USER:-1000:1000}" \
                "${FAKE_ENTRYPOINT:-[\"/usr/local/bin/pyfinder-entrypoint\"]}" \
                "${FAKE_COMMAND:-[\"continuous\"]}" \
                "$running"
            ;;
        *'.Mounts'*)
            printf '%s\n' "${FAKE_MOUNTS:-bind|/Users/savas/my-codes/eew/pyfinder-dev/pyfinder-deploy/runtime|/home/sysop/runtime|true}"
            ;;
        '{{.State.Running}}')
            if [[ "${FAKE_CONTAINER_STATE}" == "running" ]]; then
                printf 'true\n'
            else
                printf 'false\n'
            fi
            ;;
        *)
            exit 2
            ;;
    esac
    exit 0
fi

if [[ "${1:-}" == "image" && "${2:-}" == "ls" ]]; then
    if [[ "${FAKE_IMAGE_QUERY_FAIL:-0}" == "1" ]]; then
        printf 'fake Docker image query failure\n' >&2
        exit 43
    fi
    if [[ "${FAKE_IMAGE_PRESENT:-1}" != "1" ]]; then
        exit 0
    fi
    printf '%s|%s\n' \
        "${FAKE_IMAGE_REFERENCE:-pyfinder:dev}" \
        "${FAKE_LOCAL_IMAGE_ID:-sha256:current}"
    exit 0
fi

exit 0
'''


FAKE_MKDIR = r'''#!/bin/bash
set -eu

record_file="${FAKE_RECORD_FILE:?}"
printf 'mkdir' >> "$record_file"
for argument in "$@"; do
    printf '\t%s' "$argument" >> "$record_file"
done
printf '\n' >> "$record_file"
'''


class HostLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory(
            prefix="pyfinder-host-launcher-"
        )
        self.temporary_root = Path(self.temporary_directory.name)
        self.fake_bin = self.temporary_root / "bin"
        self.outside_directory = self.temporary_root / "outside"
        self.record_file = self.temporary_root / "commands.tsv"
        self.fake_bin.mkdir()
        self.outside_directory.mkdir()
        self.write_executable("docker", FAKE_DOCKER)
        self.write_executable("mkdir", FAKE_MKDIR)

        self.environment = os.environ.copy()
        self.environment.update(
            {
                "PATH": os.pathsep.join(
                    (str(self.fake_bin), self.environment["PATH"])
                ),
                "FAKE_RECORD_FILE": str(self.record_file),
                "FAKE_CONTAINER_STATE": "missing",
                "FAKE_IMAGE_PRESENT": "1",
            }
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def write_executable(self, name, content):
        path = self.fake_bin / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    def run_launcher(self, *arguments, **environment_overrides):
        self.record_file.unlink(missing_ok=True)
        environment = self.environment.copy()
        environment.update(environment_overrides)
        return subprocess.run(
            [str(LAUNCHER), *arguments],
            cwd=self.outside_directory,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def records(self):
        if not self.record_file.exists():
            return []
        return [
            line.split("\t")
            for line in self.record_file.read_text(encoding="utf-8").splitlines()
        ]

    @staticmethod
    def lifecycle_records(records):
        lifecycle_actions = {
            "create",
            "exec",
            "pull",
            "rm",
            "run",
            "start",
            "stop",
        }
        return [
            record
            for record in records
            if record[0] == "docker"
            and len(record) > 1
            and record[1] in lifecycle_actions
        ]

    @staticmethod
    def effect_records(records):
        effect_actions = {
            "create",
            "exec",
            "logs",
            "pull",
            "rm",
            "run",
            "start",
            "stop",
        }
        return [
            record
            for record in records
            if record[0] == "docker"
            and len(record) > 1
            and record[1] in effect_actions
        ]

    def assert_no_mutation(self):
        records = self.records()
        self.assertEqual(self.lifecycle_records(records), [])
        self.assertFalse(any(record[0] == "mkdir" for record in records))

    def test_help_uses_no_docker_or_filesystem_command(self):
        for arguments in ((), ("--help",), ("help",)):
            with self.subTest(arguments=arguments):
                result = self.run_launcher(*arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Usage: pyfinder", result.stdout)
                self.assertEqual(self.records(), [])

    def test_missing_continuous_creates_fixed_directories_and_one_exact_run(self):
        result = self.run_launcher(
            "continuous",
            CONTAINER_NAME="other-container",
            IMAGE_NAME="other:image",
            DEPLOYMENT_ROOT="/tmp/other-deployment",
            PYFINDER_CONTAINER_NAME="another-container",
            PYFINDER_IMAGE="another:image",
            PYFINDER_DEPLOYMENT_ROOT="/tmp/another-deployment",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        records = self.records()
        mkdir_records = [record for record in records if record[0] == "mkdir"]
        self.assertEqual(
            mkdir_records,
            [["mkdir", "-p", *REQUIRED_DIRECTORIES]],
        )
        run_records = [record for record in records if record[:2] == ["docker", "run"]]
        self.assertEqual(
            run_records,
            [[
                "docker",
                "run",
                "--detach",
                "--pull=never",
                "--name",
                CONTAINER_NAME,
                "--user",
                "1000:1000",
                "--mount",
                "type=bind,source={0},target={1}".format(
                    RUNTIME_ROOT,
                    CONTAINER_RUNTIME,
                ),
                IMAGE_NAME,
                "continuous",
            ]],
        )
        self.assertTrue(
            any(
                "name=^{0}$".format(CONTAINER_NAME) in record
                for record in records
                if record[:3] == ["docker", "container", "ls"]
            )
        )
        flattened = "\n".join("\t".join(record) for record in records)
        for forbidden in (
            "other-container",
            "other:image",
            "/tmp/other-deployment",
            "another-container",
            "another:image",
            "/tmp/another-deployment",
        ):
            self.assertNotIn(forbidden, flattened)

    def test_successful_empty_image_query_reports_required_image_absent(self):
        result = self.run_launcher(
            "continuous",
            FAKE_IMAGE_PRESENT="0",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "required local development image pyfinder:dev is absent",
            result.stderr,
        )
        self.assertNotIn("could not query Docker images", result.stderr)
        self.assert_no_mutation()

    def test_failed_image_query_reports_docker_access_failure(self):
        result = self.run_launcher(
            "continuous",
            FAKE_IMAGE_QUERY_FAIL="1",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not query Docker images", result.stderr)
        self.assertNotIn("image pyfinder:dev is absent", result.stderr)
        self.assert_no_mutation()

    def test_failed_container_query_blocks_every_container_command(self):
        commands = (
            ("continuous",),
            ("playback", "--list"),
            ("on-demand", "--test"),
            ("status",),
            ("logs",),
            ("stop",),
        )
        for arguments in commands:
            with self.subTest(command=arguments[0]):
                result = self.run_launcher(
                    *arguments,
                    FAKE_CONTAINER_QUERY_FAIL="1",
                    FAKE_CONTAINER_STATE="running",
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    "could not query Docker containers",
                    result.stderr,
                )
                self.assertNotIn("is absent", result.stdout + result.stderr)
                self.assertNotIn(
                    "does not exist",
                    result.stdout + result.stderr,
                )
                records = self.records()
                self.assertEqual(self.effect_records(records), [])
                self.assertFalse(
                    any(record[0] == "mkdir" for record in records)
                )
                self.assertEqual(
                    [record[:3] for record in records],
                    [["docker", "container", "ls"]],
                )

    def test_successful_empty_container_query_is_genuine_absence(self):
        result = self.run_launcher(
            "status",
            FAKE_CONTAINER_STATE="missing",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("pyfinder-docker is absent", result.stdout)
        self.assertNotIn("could not query Docker", result.stderr)
        self.assert_no_mutation()

    def test_running_compatible_continuous_is_preserved(self):
        result = self.run_launcher(
            "continuous",
            FAKE_CONTAINER_STATE="running",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already running and compatible", result.stdout)
        self.assert_no_mutation()

    def test_stopped_compatible_continuous_creates_directories_then_only_starts(self):
        result = self.run_launcher(
            "continuous",
            FAKE_CONTAINER_STATE="stopped",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        records = self.records()
        self.assertEqual(
            [record for record in records if record[0] == "mkdir"],
            [["mkdir", "-p", *REQUIRED_DIRECTORIES]],
        )
        self.assertEqual(
            self.lifecycle_records(records),
            [["docker", "start", CONTAINER_NAME]],
        )

    def test_all_required_incompatibilities_block_continuous(self):
        cases = (
            ("image reference", {"FAKE_CONFIG_IMAGE": "other:dev"}),
            (
                "image ID",
                {"FAKE_CONTAINER_IMAGE_ID": "sha256:stale"},
            ),
            ("user", {"FAKE_CONFIG_USER": "0:0"}),
            ("entrypoint", {"FAKE_ENTRYPOINT": '["other"]'}),
            ("command", {"FAKE_COMMAND": '["playback"]'}),
            (
                "mount type",
                {
                    "FAKE_MOUNTS": (
                        "volume|{0}|/home/sysop/runtime|true".format(
                            RUNTIME_ROOT
                        )
                    )
                },
            ),
            (
                "mount source",
                {
                    "FAKE_MOUNTS": (
                        "bind|/wrong/runtime|/home/sysop/runtime|true"
                    )
                },
            ),
            (
                "mount target",
                {
                    "FAKE_MOUNTS": (
                        "bind|{0}|/wrong/runtime|true".format(RUNTIME_ROOT)
                    )
                },
            ),
            (
                "mount writability",
                {
                    "FAKE_MOUNTS": (
                        "bind|{0}|/home/sysop/runtime|false".format(
                            RUNTIME_ROOT
                        )
                    )
                },
            ),
            (
                "additional mount",
                {"FAKE_MOUNTS": EXPECTED_MOUNT + " extra-mount"},
            ),
        )
        for expected_diagnostic, overrides in cases:
            with self.subTest(property=expected_diagnostic):
                result = self.run_launcher(
                    "continuous",
                    FAKE_CONTAINER_STATE="running",
                    **overrides,
                )
                self.assertNotEqual(result.returncode, 0)
                if (
                    expected_diagnostic.startswith("mount")
                    or expected_diagnostic == "additional mount"
                ):
                    self.assertIn("mounts", result.stderr)
                else:
                    self.assertIn(expected_diagnostic, result.stderr)
                self.assert_no_mutation()

    def test_arguments_cannot_override_fixed_host_identity_or_paths(self):
        continuous = self.run_launcher(
            "continuous",
            "--container",
            "other-container",
        )
        self.assertNotEqual(continuous.returncode, 0)
        self.assertEqual(self.records(), [])

        playback = self.run_launcher(
            "playback",
            "--container",
            "other-container",
            "--image",
            "other:image",
            FAKE_CONTAINER_STATE="running",
        )
        self.assertEqual(playback.returncode, 0, playback.stderr)
        exec_record = self.lifecycle_records(self.records())[-1]
        self.assertEqual(exec_record[2], CONTAINER_NAME)
        self.assertEqual(
            exec_record[3:],
            [
                "pyfinder",
                "playback",
                "--container",
                "other-container",
                "--image",
                "other:image",
            ],
        )

    def test_playback_and_on_demand_forward_arguments_through_one_exec(self):
        cases = (
            (
                "playback",
                ("--event-id", "event-one", "event-two", "--list"),
            ),
            (
                "on-demand",
                ("--event-id", "event-three", "--verbosity", "DEBUG"),
            ),
        )
        for workflow, forwarded in cases:
            with self.subTest(workflow=workflow):
                result = self.run_launcher(
                    workflow,
                    *forwarded,
                    FAKE_CONTAINER_STATE="running",
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    self.lifecycle_records(self.records()),
                    [[
                        "docker",
                        "exec",
                        CONTAINER_NAME,
                        "pyfinder",
                        workflow,
                        *forwarded,
                    ]],
                )
                self.assertFalse(
                    any(record[0] == "mkdir" for record in self.records())
                )

    def test_additional_processes_reject_all_unusable_container_states(self):
        cases = (
            ("missing", {}),
            ("stopped", {"FAKE_CONTAINER_STATE": "stopped"}),
            (
                "stale",
                {
                    "FAKE_CONTAINER_STATE": "running",
                    "FAKE_CONTAINER_IMAGE_ID": "sha256:stale",
                },
            ),
            (
                "incompatible",
                {
                    "FAKE_CONTAINER_STATE": "running",
                    "FAKE_CONFIG_USER": "0:0",
                },
            ),
        )
        for workflow in ("playback", "on-demand"):
            for label, overrides in cases:
                with self.subTest(workflow=workflow, state=label):
                    result = self.run_launcher(
                        workflow,
                        "--event-id",
                        "event-one",
                        **overrides,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assert_no_mutation()

    def test_status_is_read_only_for_absent_stopped_and_running_states(self):
        for state, expected in (
            ("missing", "absent"),
            ("stopped", "stopped"),
            ("running", "running"),
        ):
            with self.subTest(state=state):
                result = self.run_launcher(
                    "status",
                    FAKE_CONTAINER_STATE=state,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(expected, result.stdout)
                self.assert_no_mutation()
                query_record = self.records()[0]
                self.assertEqual(query_record[:3], ["docker", "container", "ls"])
                self.assertIn(
                    "name=^{0}$".format(CONTAINER_NAME),
                    query_record,
                )

    def test_logs_and_stop_target_only_the_canonical_container(self):
        logs_result = self.run_launcher(
            "logs",
            FAKE_CONTAINER_STATE="running",
        )
        self.assertEqual(logs_result.returncode, 0, logs_result.stderr)
        self.assertEqual(
            self.lifecycle_records(self.records()),
            [],
        )
        self.assertEqual(self.records()[-1], ["docker", "logs", CONTAINER_NAME])

        stop_result = self.run_launcher(
            "stop",
            FAKE_CONTAINER_STATE="running",
        )
        self.assertEqual(stop_result.returncode, 0, stop_result.stderr)
        self.assertEqual(
            self.lifecycle_records(self.records()),
            [["docker", "stop", CONTAINER_NAME]],
        )
        self.assertFalse(any("rm" in record for record in self.records()))

    def test_missing_logs_and_stop_are_clear_and_non_destructive(self):
        for command in ("logs", "stop"):
            with self.subTest(command=command):
                result = self.run_launcher(command)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("does not exist", result.stderr)
                self.assert_no_mutation()

    def test_launcher_source_has_only_the_fixed_host_boundary(self):
        source = LAUNCHER.read_text(encoding="utf-8")
        lowered = source.lower()
        for forbidden in (
            "python",
            "paramws",
            "finder_run",
            "shakemap",
            "smtp",
            "$random",
            "uuid",
            "mktemp",
            "$$",
        ):
            self.assertNotIn(forbidden, lowered)
        for forbidden_command in (
            "docker create",
            "docker rm",
            "docker pull",
            "docker build",
        ):
            self.assertNotIn(forbidden_command, lowered)
        self.assertIn(
            'readonly CONTAINER_NAME="pyfinder-docker"',
            source,
        )
        self.assertIn('readonly IMAGE_NAME="pyfinder:dev"', source)
        self.assertNotIn("${PWD}", source)

    def test_launcher_and_completion_files_pass_shell_syntax_checks(self):
        commands = (
            ("/bin/bash", "-n", str(LAUNCHER)),
            ("/bin/bash", "-n", str(BASH_COMPLETION)),
            ("/bin/zsh", "-n", str(LAUNCHER)),
            ("/bin/zsh", "-n", str(ZSH_COMPLETION)),
        )
        for command in commands:
            with self.subTest(command=command):
                result = subprocess.run(
                    command,
                    cwd=self.outside_directory,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_bash_completion_loads_and_exposes_commands_and_options(self):
        script = r'''
source "$1"
COMP_WORDS=(pyfinder "")
COMP_CWORD=1
_pyfinder_completion
printf '%s\n' "${COMPREPLY[@]}"
COMP_WORDS=(pyfinder playback "")
COMP_CWORD=2
_pyfinder_completion
printf '%s\n' "${COMPREPLY[@]}"
COMP_WORDS=(pyfinder on-demand "")
COMP_CWORD=2
_pyfinder_completion
printf '%s\n' "${COMPREPLY[@]}"
COMP_WORDS=(pyfinder on-demand --verbosity "")
COMP_CWORD=3
_pyfinder_completion
printf '%s\n' "${COMPREPLY[@]}"
'''
        result = subprocess.run(
            ["/bin/bash", "-c", script, "bash", str(BASH_COMPLETION)],
            cwd=self.outside_directory,
            env=self.environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        candidates = set(result.stdout.splitlines())
        self.assertTrue(
            {
                "continuous",
                "playback",
                "on-demand",
                "status",
                "logs",
                "stop",
                "help",
                "--help",
                "--event-id",
                "--list",
                "--test",
                "--verbosity",
                "DEBUG",
                "INFO",
                "WARNING",
                "ERROR",
                "CRITICAL",
            }.issubset(candidates)
        )
        self.assertEqual(self.records(), [])

    def test_zsh_completion_loads_and_exposes_commands_and_options(self):
        script = r'''
compdef() { :; }
compadd() {
    local item
    for item in "$@"; do
        [[ "$item" == "--" ]] || print -r -- "$item"
    done
}
source "$1"
words=(pyfinder "")
CURRENT=2
_pyfinder
words=(pyfinder playback "")
CURRENT=3
_pyfinder
words=(pyfinder on-demand "")
CURRENT=3
_pyfinder
words=(pyfinder on-demand --verbosity "")
CURRENT=4
_pyfinder
'''
        result = subprocess.run(
            ["/bin/zsh", "-c", script, "zsh", str(ZSH_COMPLETION)],
            cwd=self.outside_directory,
            env=self.environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        candidates = set(result.stdout.splitlines())
        self.assertTrue(
            {
                "continuous",
                "playback",
                "on-demand",
                "status",
                "logs",
                "stop",
                "help",
                "--help",
                "--event-id",
                "--list",
                "--test",
                "--verbosity",
                "DEBUG",
                "INFO",
                "WARNING",
                "ERROR",
                "CRITICAL",
            }.issubset(candidates)
        )
        self.assertEqual(self.records(), [])


if __name__ == "__main__":
    unittest.main()
