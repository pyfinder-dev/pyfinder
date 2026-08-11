"""Host-side requirements for the buildable PyFinder image foundation."""

import os
from pathlib import Path, PurePosixPath
import subprocess
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = PROJECT_ROOT / "Dockerfile"
DOCKERIGNORE = PROJECT_ROOT / ".dockerignore"
ENTRYPOINT = PROJECT_ROOT / "scripts/pyfinder-entrypoint"
BASE_IMAGE = "ghcr.io/sceylan/finder-base:gmt5"
RESOURCE_DIRECTORIES = (
    Path("pyfinder/extern/finder_regional_wkt"),
    Path("pyfinder/extern/ne_110m_admin_0_countries"),
)
OS_METADATA_NAMES = frozenset((".DS_Store", "Thumbs.db", "desktop.ini"))


def _dockerignore_rules():
    rules = []
    for raw_line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#"):
            rules.append(line)
    return rules


def _rule_matches_file(relative_path, rule):
    """Match the simple rooted patterns used by this repository's allowlist."""
    pattern = rule[1:] if rule.startswith("!") else rule
    if pattern == "**":
        return True
    if pattern.endswith("/"):
        return False
    return PurePosixPath(relative_path).match(pattern)


def _is_ignored(relative_path, rules):
    ignored = False
    for rule in rules:
        if _rule_matches_file(relative_path, rule):
            ignored = not rule.startswith("!")
    return ignored


def _repository_files():
    return {
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in PROJECT_ROOT.rglob("*")
        if path.is_file()
    }


def _expected_context_files():
    expected = {
        "Dockerfile",
        "pyproject.toml",
        "README.md",
        "LICENSE",
        "scripts/pyfinder-entrypoint",
    }
    expected.update(
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in (PROJECT_ROOT / "pyfinder").rglob("*.py")
        if path.is_file()
    )
    for relative_directory in RESOURCE_DIRECTORIES:
        expected.update(
            path.relative_to(PROJECT_ROOT).as_posix()
            for path in (PROJECT_ROOT / relative_directory).rglob("*")
            if path.is_file() and path.name not in OS_METADATA_NAMES
        )
    return expected


class DockerfileRequirementsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contents = DOCKERFILE.read_text(encoding="utf-8")
        cls.lines = tuple(
            line.strip() for line in cls.contents.splitlines()
        )

    def test_every_stage_and_the_final_image_use_the_mandatory_base(self):
        from_lines = [
            line.strip()
            for line in self.contents.splitlines()
            if line.startswith("FROM ")
        ]
        self.assertEqual(len(from_lines), 2)
        self.assertTrue(
            all(line.split()[1] == BASE_IMAGE for line in from_lines)
        )
        self.assertEqual(from_lines[-1], "FROM " + BASE_IMAGE)

    def test_python_312_is_checksum_built_without_a_python_39_path(self):
        normalized = self.contents.lower()
        self.assertRegex(
            normalized,
            r"(?m)^arg python_version=3\.12\.[0-9]+$",
        )
        self.assertRegex(
            normalized,
            r"python_source_sha256=[0-9a-f]{64}",
        )
        self.assertIn("sha256sum --check --strict", normalized)
        self.assertIn("/opt/python-3.12", normalized)
        self.assertNotIn("/opt/python-3.9", normalized)
        self.assertNotIn("python_version=3.9", normalized)

    def test_normal_wheels_own_both_installed_distributions(self):
        normalized = self.contents.lower()
        self.assertIn(
            "https://github.com/pyfinder-dev/paramws-clients.git",
            normalized,
        )
        self.assertIn("--branch master", normalized)
        self.assertIn("git -c /build/paramws-clients rev-parse head", normalized)
        self.assertIn("python3.12 -m pip wheel", normalized)
        self.assertIn("python3.12 -m pip install", normalized)
        self.assertRegex(normalized, r"(?m)^\s*pyfinder\s*\\?$")
        self.assertRegex(normalized, r"(?m)^\s*paramws-clients\s*\\?$")
        self.assertNotIn("pyfinder==", normalized)
        self.assertNotIn("paramws-clients==", normalized)
        self.assertNotIn("--editable", normalized)
        self.assertNotRegex(
            normalized,
            r"pip\s+(?:install|wheel)(?:\s|\\)*-e(?:\s|\\)",
        )
        self.assertNotIn("pythonpath", normalized)
        self.assertIn("site-packages", normalized)
        self.assertIn("build-info.json", normalized)

    def test_final_image_uses_user_1000_entrypoint_and_continuous_command(self):
        self.assertIn("USER 1000:1000", self.lines)
        self.assertIn(
            'ENTRYPOINT ["/usr/local/bin/pyfinder-entrypoint"]',
            self.lines,
        )
        self.assertIn('CMD ["continuous"]', self.lines)

    def test_base_digest_is_required_and_used_for_label_and_build_information(self):
        normalized = self.contents.lower()
        self.assertIn("arg pyfinder_base_digest", normalized)
        self.assertNotRegex(normalized, r"arg pyfinder_base_digest\s*=")
        self.assertIn(
            'io.pyfinder.base.digest="${pyfinder_base_digest}"',
            normalized,
        )
        self.assertIn(
            '"base_digest": os.environ["pyfinder_base_digest"]',
            normalized,
        )
        self.assertIn('[ -z "${pyfinder_base_digest}" ]', normalized)
        self.assertIn("platform.freedesktop_os_release()", normalized)

    def test_build_checks_cover_durable_installed_image_requirements(self):
        normalized = self.contents.lower()
        required_fragments = (
            "getent passwd 1000",
            "getent group 1000",
            "command -v pyfinder",
            "pyfinder --help",
            "/usr/local/src/finder/finder_run",
            "/usr/local/src/finder/finder_create_mask",
            "extern/finder_regional_wkt",
            "extern/ne_110m_admin_0_countries",
            "extern/shakemap-conf-eu",
            "paramws-commit",
            "distribution_origin",
            "module_origin",
            "paramws_log_file=/tmp/pyfinder-build-paramws.log",
            "from paramws.clients import",
            "from paramws.utils import customlogger",
            "from pyfinder import cli, finderexec, findermanager, runtime",
            "import geopandas",
            "import shapely",
            "import tornado",
        )
        for fragment in required_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, normalized)


class EntrypointRequirementsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contents = ENTRYPOINT.read_text(encoding="utf-8")

    def test_entrypoint_requires_identity_mount_and_exact_runtime_directories(self):
        required_fragments = (
            "id -u",
            "id -g",
            "mountpoint -q",
            "/home/sysop/runtime",
            "/home/sysop/runtime/pyfinder/state",
            "/home/sysop/runtime/pyfinder/logs",
            "/home/sysop/runtime/pyfinder/runs",
            "/home/sysop/runtime/pyfinder/playbacks",
            "mktemp",
            'readonly REQUIRED_UID="1000"',
            'readonly REQUIRED_GID="1000"',
            "required runtime identity:",
            "observed ownership:",
            "correct the host path",
            'exec pyfinder "$@"',
        )
        for fragment in required_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.contents)

    def test_entrypoint_does_not_repair_or_fallback(self):
        normalized = self.contents.lower()
        for forbidden in ("chown", "chmod", "mkdir", "/home/sysop/pyfinder"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, normalized)

    def test_term_during_mount_check_stops_before_later_validation(self):
        with tempfile.TemporaryDirectory(
            prefix="pyfinder-entrypoint-signal-"
        ) as temporary_directory:
            temporary_root = Path(temporary_directory)
            fake_bin = temporary_root / "bin"
            record_file = temporary_root / "commands.log"
            pyfinder_record = temporary_root / "pyfinder-reached"
            fake_bin.mkdir()

            fake_commands = {
                "id": """#!/bin/bash
case "${1:-}" in
    -u|-g) printf '1000\\n' ;;
    *) exit 2 ;;
esac
""",
                "mountpoint": """#!/bin/bash
printf 'mountpoint\\n' >> "${FAKE_RECORD_FILE:?}"
kill -TERM "$PPID"
exit 0
""",
                "pyfinder": """#!/bin/bash
printf 'reached\\n' > "${FAKE_PYFINDER_RECORD:?}"
""",
            }
            for name, contents in fake_commands.items():
                path = fake_bin / name
                path.write_text(contents, encoding="utf-8")
                path.chmod(0o755)

            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": os.pathsep.join(
                        (str(fake_bin), environment["PATH"])
                    ),
                    "FAKE_RECORD_FILE": str(record_file),
                    "FAKE_PYFINDER_RECORD": str(pyfinder_record),
                }
            )
            result = subprocess.run(
                [str(ENTRYPOINT), "continuous"],
                cwd=temporary_root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 143, result.stderr)
            self.assertEqual(
                record_file.read_text(encoding="utf-8").splitlines(),
                ["mountpoint"],
            )
            self.assertFalse(pyfinder_record.exists())
            self.assertNotIn("required runtime directory", result.stderr)


class EffectiveBuildContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules = _dockerignore_rules()
        cls.repository_files = _repository_files()
        cls.effective_files = {
            path
            for path in cls.repository_files
            if not _is_ignored(path, cls.rules)
        }

    def test_effective_context_is_only_the_wheel_source_allowlist(self):
        self.assertEqual(self.effective_files, _expected_context_files())

    def test_required_copy_inputs_and_complete_resource_trees_are_included(self):
        for path in _expected_context_files():
            with self.subTest(path=path):
                self.assertIn(path, self.effective_files)

    def test_repository_hazards_are_excluded_from_the_effective_context(self):
        forbidden_parts = {
            ".agent",
            ".git",
            ".pytest_cache",
            "__pycache__",
            "assets",
            "legacy",
            "tests",
        }
        forbidden_suffixes = (
            ".db",
            ".db-shm",
            ".db-wal",
            ".log",
            ".pyc",
            ".sqlite",
            ".sqlite3",
        )
        for path in self.effective_files:
            parts = set(PurePosixPath(path).parts)
            with self.subTest(path=path):
                self.assertTrue(parts.isdisjoint(forbidden_parts))
                self.assertFalse(path.endswith(forbidden_suffixes))
                self.assertNotIn(".pyfinder_alert_config", path)
                self.assertNotIn("shakemap-conf-eu", parts)
                self.assertNotIn(PurePosixPath(path).name, OS_METADATA_NAMES)

    def test_deny_all_rule_precedes_each_explicit_source_exception(self):
        self.assertEqual(self.rules[0], "**")
        self.assertTrue(any(rule == "!Dockerfile" for rule in self.rules))
        self.assertTrue(
            any(rule == "!scripts/pyfinder-entrypoint" for rule in self.rules)
        )
        self.assertTrue(
            any(
                rule == "!pyfinder/extern/finder_regional_wkt/**"
                for rule in self.rules
            )
        )
        self.assertTrue(
            any(
                rule
                == "!pyfinder/extern/ne_110m_admin_0_countries/**"
                for rule in self.rules
            )
        )


if __name__ == "__main__":
    unittest.main()
