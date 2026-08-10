"""Artifact and command-boundary tests for normal PyFinder installation."""

import configparser
from contextlib import redirect_stderr, redirect_stdout
from email.parser import Parser
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile

from pyfinder import cli
from pyfinder.finderconfigs import profiles


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESOURCE_DIRECTORIES = (
    Path("pyfinder/extern/finder_regional_wkt"),
    Path("pyfinder/extern/ne_110m_admin_0_countries"),
)
OS_METADATA_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}


def _source_resource_names(relative_directory):
    root = PROJECT_ROOT / relative_directory
    return {
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name not in OS_METADATA_NAMES
    }


class BuiltArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary_directory = tempfile.TemporaryDirectory(
            prefix="pyfinder-wheel-test-"
        )
        cls.temporary_root = Path(cls.temporary_directory.name)
        cls.source_root = cls.temporary_root / "source"
        cls.wheelhouse = cls.temporary_root / "wheelhouse"
        cls.installed_target = cls.temporary_root / "installed"
        cls.outside_directory = cls.temporary_root / "outside"
        cls.guard_directory = cls.temporary_root / "guard"
        cls.source_root.mkdir()
        cls.wheelhouse.mkdir()
        cls.outside_directory.mkdir()
        cls.guard_directory.mkdir()

        for filename in ("pyproject.toml", "README.md", "LICENSE"):
            shutil.copy2(PROJECT_ROOT / filename, cls.source_root / filename)
        shutil.copytree(PROJECT_ROOT / "pyfinder", cls.source_root / "pyfinder")

        build_environment = os.environ.copy()
        build_environment.pop("PYTHONPATH", None)
        build_environment["PYTHONNOUSERSITE"] = "1"
        build_result = subprocess.run(
            [
                sys.executable,
                "-m",
                "build",
                "--wheel",
                "--no-isolation",
                "--outdir",
                os.fspath(cls.wheelhouse),
                os.fspath(cls.source_root),
            ],
            cwd=cls.outside_directory,
            env=build_environment,
            capture_output=True,
            text=True,
            check=False,
        )
        if build_result.returncode != 0:
            raise AssertionError(
                "Wheel construction failed:\n{0}\n{1}".format(
                    build_result.stdout,
                    build_result.stderr,
                )
            )

        wheels = tuple(cls.wheelhouse.glob("*.whl"))
        if len(wheels) != 1:
            raise AssertionError(
                "Expected one wheel, found: {0}".format(wheels)
            )
        cls.wheel_path = wheels[0]

        with zipfile.ZipFile(cls.wheel_path) as archive:
            cls.wheel_names = set(archive.namelist())
            metadata_name = next(
                name for name in cls.wheel_names if name.endswith("/METADATA")
            )
            entry_points_name = next(
                name
                for name in cls.wheel_names
                if name.endswith("/entry_points.txt")
            )
            cls.metadata = Parser().parsestr(
                archive.read(metadata_name).decode("utf-8")
            )
            entry_points = configparser.ConfigParser()
            entry_points.read_string(
                archive.read(entry_points_name).decode("utf-8")
            )
            cls.entry_points = entry_points

        install_result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-deps",
                "--target",
                os.fspath(cls.installed_target),
                os.fspath(cls.wheel_path),
            ],
            cwd=cls.outside_directory,
            env=build_environment,
            capture_output=True,
            text=True,
            check=False,
        )
        if install_result.returncode != 0:
            raise AssertionError(
                "Temporary non-editable installation failed:\n{0}\n{1}".format(
                    install_result.stdout,
                    install_result.stderr,
                )
            )
        (cls.guard_directory / "sitecustomize.py").write_text(
            """
import builtins
import importlib.abc
import logging
import logging.handlers
import socket
import sqlite3
import sys
import threading

sys.dont_write_bytecode = True
forbidden_imports = (
    "paramws",
    "pyfinder.start_monitoring",
    "pyfinder.playback",
    "pyfinder.findermanager",
)

class ForbiddenImportFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(
            fullname == name or fullname.startswith(name + ".")
            for name in forbidden_imports
        ):
            raise AssertionError("forbidden operational import: " + fullname)
        return None

def reject_operation(*args, **kwargs):
    raise AssertionError("forbidden operational side effect")

real_open = builtins.open
def guarded_open(file, mode="r", *args, **kwargs):
    if any(flag in mode for flag in ("w", "a", "x", "+")):
        reject_operation()
    return real_open(file, mode, *args, **kwargs)

sys.meta_path.insert(0, ForbiddenImportFinder())
builtins.open = guarded_open
logging.FileHandler = reject_operation
logging.handlers.RotatingFileHandler = reject_operation
socket.socket = reject_operation
sqlite3.connect = reject_operation
threading.Thread.start = reject_operation
""",
            encoding="utf-8",
        )

    @classmethod
    def tearDownClass(cls):
        cls.temporary_directory.cleanup()

    @classmethod
    def installed_environment(cls):
        environment = os.environ.copy()
        environment["PYTHONNOUSERSITE"] = "1"
        environment["PYTHONPATH"] = os.pathsep.join(
            (
                os.fspath(cls.guard_directory),
                os.fspath(cls.installed_target),
            )
        )
        return environment

    def test_distribution_metadata_and_console_entry_point(self):
        self.assertEqual(self.metadata["Name"], "pyfinder")
        self.assertEqual(self.metadata["Version"], "1.0.0")
        self.assertEqual(self.metadata["Requires-Python"], ">=3.12")
        self.assertEqual(
            set(self.metadata.get_all("Requires-Dist", [])),
            {
                "geopandas",
                "numpy",
                "paramws-clients==0.1.0",
                "shapely",
                "tornado",
            },
        )
        self.assertEqual(
            self.entry_points["console_scripts"]["pyfinder"],
            "pyfinder.cli:main",
        )

    def test_wheel_contains_both_complete_non_shakemap_resource_trees(self):
        for relative_directory in RESOURCE_DIRECTORIES:
            with self.subTest(directory=relative_directory.as_posix()):
                expected = _source_resource_names(relative_directory)
                prefix = relative_directory.as_posix() + "/"
                packaged = {
                    name
                    for name in self.wheel_names
                    if name.startswith(prefix)
                }
                self.assertEqual(packaged, expected)

    def test_wheel_excludes_downstream_secret_and_development_artifacts(self):
        forbidden_fragments = (
            "/.agent/",
            "/.git/",
            "/.pytest_cache/",
            "/__pycache__/",
            "/assets/",
            "/legacy/",
            "extern/shakemap-conf-eu/",
        )
        forbidden_suffixes = (
            ".db",
            ".db-shm",
            ".db-wal",
            ".log",
            ".pyc",
            ".sqlite",
            ".sqlite3",
        )
        for name in self.wheel_names:
            normalized_name = "/" + name
            with self.subTest(name=name):
                self.assertNotIn(".DS_Store", name)
                self.assertNotIn(".pyfinder_alert_config", name)
                self.assertFalse(
                    any(
                        fragment in normalized_name
                        for fragment in forbidden_fragments
                    )
                )
                self.assertFalse(name.endswith(forbidden_suffixes))
                self.assertFalse(name.startswith("tests/"))

    def test_unregistered_wkt_files_are_packaged_but_not_activated(self):
        packaged_wkt_names = {
            Path(name).name
            for name in self.wheel_names
            if name.startswith("pyfinder/extern/finder_regional_wkt/")
            and name.endswith(".wkt")
        }
        registered_wkt_names = {
            profile.wkt_filename for profile in profiles.REGIONAL_PROFILES
        }

        self.assertTrue(registered_wkt_names < packaged_wkt_names)
        self.assertIn("australia.wkt", packaged_wkt_names)
        self.assertNotIn("australia.wkt", registered_wkt_names)
        self.assertEqual(
            tuple(profile.name for profile in profiles.REGIONAL_PROFILES),
            (
                "switzerland-alpine",
                "switzerland-foreland",
                "italy",
            ),
        )

    def test_installed_console_help_runs_outside_the_repository(self):
        script_path = self.installed_target / "bin" / "pyfinder"
        self.assertTrue(script_path.is_file())
        environment = self.installed_environment()

        for arguments in (
            ("--help",),
            ("continuous", "--help"),
            ("playback", "--help"),
            ("on-demand", "--help"),
        ):
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    [os.fspath(script_path), *arguments],
                    cwd=self.outside_directory,
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage: pyfinder", result.stdout)

        self.assertEqual(tuple(self.outside_directory.iterdir()), ())

    def test_installed_console_rejects_invalid_grammar_and_missing_runtime(self):
        script_path = self.installed_target / "bin" / "pyfinder"
        environment = self.installed_environment()
        cases = (
            (
                ("continuous",),
                "required runtime directory does not exist: "
                "/home/sysop/runtime/pyfinder",
            ),
            (("on-demand",), "one of the arguments"),
            (
                ("on-demand", "--event-id", "EVENT", "--test"),
                "not allowed with argument",
            ),
            (("on-demand", "--log-file", "anything"), "one of the arguments"),
            (
                (
                    "on-demand",
                    "--event-id",
                    "EVENT",
                    "--log-file",
                    "anything",
                ),
                "unrecognized arguments: --log-file anything",
            ),
        )

        for arguments, diagnostic in cases:
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    [os.fspath(script_path), *arguments],
                    cwd=self.outside_directory,
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(diagnostic, result.stderr)
                self.assertNotIn("forbidden operational", result.stderr)

        help_result = subprocess.run(
            [
                os.fspath(script_path),
                "on-demand",
                "--event-id",
                "EVENT",
                "--help",
            ],
            cwd=self.outside_directory,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertNotIn("--log-file", help_result.stdout)
        self.assertEqual(tuple(self.outside_directory.iterdir()), ())

    def test_artifact_import_and_help_have_no_operational_side_effects(self):
        probe = """
import builtins
import logging
import logging.handlers
from pathlib import Path
import socket
import sqlite3
import sys
import threading
from unittest import mock

installed_target = sys.argv[1]
outside_directory = Path(sys.argv[2])
sys.path.insert(0, installed_target)
initial_handlers = tuple(logging.getLogger().handlers)
initial_loggers = frozenset(logging.Logger.manager.loggerDict)
initial_threads = tuple(threading.enumerate())
real_open = builtins.open

def guarded_open(file, mode="r", *args, **kwargs):
    if any(flag in mode for flag in ("w", "a", "x", "+")):
        raise AssertionError("package import or help attempted a file write")
    return real_open(file, mode, *args, **kwargs)

with mock.patch("builtins.open", side_effect=guarded_open), \
     mock.patch.object(logging, "FileHandler", autospec=True) as file_handler, \
     mock.patch.object(logging.handlers, "RotatingFileHandler", autospec=True) as rotating_handler, \
     mock.patch.object(sqlite3, "connect", autospec=True) as database_connect, \
     mock.patch.object(threading.Thread, "start", autospec=True) as thread_start, \
     mock.patch.object(socket, "socket", autospec=True) as socket_constructor:
    import pyfinder
    from pyfinder import cli
    for arguments in (
        ["--help"],
        ["continuous", "--help"],
        ["playback", "--help"],
        ["on-demand", "--help"],
    ):
        try:
            cli.main(arguments)
        except SystemExit as error:
            if error.code != 0:
                raise

file_handler.assert_not_called()
rotating_handler.assert_not_called()
database_connect.assert_not_called()
thread_start.assert_not_called()
socket_constructor.assert_not_called()
assert tuple(logging.getLogger().handlers) == initial_handlers
assert frozenset(logging.Logger.manager.loggerDict) == initial_loggers
assert tuple(threading.enumerate()) == initial_threads
assert not any(name == "paramws" or name.startswith("paramws.") for name in sys.modules)
assert "pyfinder.start_monitoring" not in sys.modules
assert "pyfinder.playback" not in sys.modules
assert "pyfinder.findermanager" not in sys.modules
assert Path(pyfinder.__file__).is_relative_to(Path(installed_target))
assert tuple(outside_directory.iterdir()) == ()
"""
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                probe,
                os.fspath(self.installed_target),
                os.fspath(self.outside_directory),
            ],
            cwd=self.outside_directory,
            env={"PYTHONNOUSERSITE": "1"},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            "artifact probe failed:\n{0}\n{1}".format(
                result.stdout,
                result.stderr,
            ),
        )


class CommandDispatchTests(unittest.TestCase):
    def test_invalid_on_demand_grammar_stops_before_bootstrap(self):
        cases = (
            ["on-demand"],
            ["on-demand", "--event-id", "EVENT", "--test"],
            ["on-demand", "--log-file", "anything"],
            [
                "on-demand",
                "--event-id",
                "EVENT",
                "--log-file",
                "anything",
            ],
        )
        for arguments in cases:
            with self.subTest(arguments=arguments), mock.patch.object(
                cli,
                "bootstrap_runtime",
            ) as bootstrap, mock.patch.object(
                cli.importlib,
                "import_module",
            ) as importer, redirect_stdout(io.StringIO()), redirect_stderr(
                io.StringIO()
            ):
                with self.assertRaises(SystemExit) as raised:
                    cli.main(arguments)

                self.assertEqual(raised.exception.code, 2)
                bootstrap.assert_not_called()
                importer.assert_not_called()

    def test_on_demand_help_succeeds_before_bootstrap(self):
        with mock.patch.object(
            cli,
            "bootstrap_runtime",
        ) as bootstrap, redirect_stdout(io.StringIO()), redirect_stderr(
            io.StringIO()
        ):
            with self.assertRaises(SystemExit) as raised:
                cli.main(["on-demand", "--event-id", "EVENT", "--help"])

        self.assertEqual(raised.exception.code, 0)
        bootstrap.assert_not_called()

    def test_default_dispatch_fails_before_workflow_import(self):
        arguments = cli.build_parser().parse_args(["continuous"])
        importer = mock.Mock()

        with self.assertRaises(cli.RuntimeBootstrapUnavailable):
            cli.dispatch(arguments, importer=importer)

        importer.assert_not_called()

    def test_each_dispatch_bootstraps_before_import_and_invocation(self):
        cases = (
            (
                "continuous",
                ["continuous"],
                "pyfinder.start_monitoring",
                "start_services",
            ),
            (
                "playback",
                ["playback"],
                "pyfinder.playback",
                "run_cli",
            ),
            (
                "on-demand",
                ["on-demand", "--event-id", "EVENT"],
                "pyfinder.findermanager",
                "run_cli",
            ),
        )
        for command, command_line, module_name, callable_name in cases:
            with self.subTest(command=command):
                operations = []
                workflow_callable = mock.Mock(return_value=17)
                module = SimpleNamespace(
                    **{callable_name: workflow_callable}
                )

                runtime_context = object()

                def bootstrap(workflow):
                    operations.append(("bootstrap", workflow))
                    return runtime_context

                def importer(requested_module):
                    operations.append(("import", requested_module))
                    self.assertEqual(
                        operations[0],
                        ("bootstrap", command),
                    )
                    return module

                arguments = cli.build_parser().parse_args(command_line)
                result = cli.dispatch(
                    arguments,
                    bootstrap=bootstrap,
                    importer=importer,
                )

                self.assertEqual(result, 17)
                self.assertEqual(
                    operations,
                    [
                        ("bootstrap", command),
                        ("import", module_name),
                    ],
                )
                if command == "continuous":
                    workflow_callable.assert_called_once_with(
                        runtime_context=runtime_context
                    )
                else:
                    workflow_callable.assert_called_once_with(
                        arguments,
                        runtime_context=runtime_context,
                    )


class RetiredStartupScriptTests(unittest.TestCase):
    def test_startup_script_only_directs_callers_to_installed_command(self):
        script_path = PROJECT_ROOT / "pyfinder" / "startMonitoring.sh"
        source = script_path.read_text(encoding="utf-8")

        self.assertIn("pyfinder continuous", source)
        self.assertNotIn("nohup", source)
        self.assertNotIn("python3.9", source)
        self.assertNotIn("start_monitoring.py", source)

        result = subprocess.run(
            ["/bin/sh", os.fspath(script_path)],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("pyfinder continuous", result.stderr)

    def test_readme_records_current_execution_boundaries(self):
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        normalized = readme.casefold()
        compact = " ".join(normalized.split())

        self.assertIn("pyfinder continuous", readme)
        self.assertIn("pyfinder playback --list", readme)
        self.assertIn("pyfinder on-demand --event-id EVENT_ID", readme)
        self.assertIn("forthcoming pyfinder container", compact)
        self.assertIn(
            "shakemap and email execution are currently inactive",
            compact,
        )
        self.assertIn("host controller", compact)
        self.assertNotIn("./startMonitoring.sh", readme)
        self.assertNotIn("start_monitoring.py", readme)
        self.assertNotIn("python3.9", readme)
        self.assertNotIn("nohup", readme)
        self.assertNotIn("standalone (no docker", normalized)
        self.assertNotIn("pyfinder-docker", normalized)
        self.assertNotIn("alerts are optional", normalized)
        self.assertNotIn(".pyfinder_alert_config", normalized)
        self.assertNotIn("docker run", normalized)
        self.assertNotIn("$host_out", normalized)
        self.assertNotIn("pyfinder/pyfinder/output", normalized)
        self.assertNotIn("fully reproducible setup", normalized)


if __name__ == "__main__":
    unittest.main()
