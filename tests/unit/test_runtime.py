"""Deterministic tests for fixed process runtime composition."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest import mock

from pyfinder import cli, runtime


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@contextmanager
def without_paramws_modules():
    """Temporarily restore the fresh-process condition required at bootstrap."""
    removed = {
        name: module
        for name, module in tuple(sys.modules.items())
        if name == "paramws" or name.startswith("paramws.")
    }
    for name in removed:
        sys.modules.pop(name, None)
    try:
        yield
    finally:
        for name in tuple(sys.modules):
            if name == "paramws" or name.startswith("paramws."):
                sys.modules.pop(name, None)
        sys.modules.update(removed)


@contextmanager
def current_directory(path):
    """Use one controlled working directory and always restore the caller."""
    original = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(original)


class RuntimeContextTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory(
            prefix="pyfinder-runtime-unit-"
        )
        self.temporary_root = Path(self.temporary_directory.name)
        self.clock_time = datetime(
            2032,
            4,
            5,
            6,
            7,
            8,
            901234,
            tzinfo=timezone.utc,
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def service_root(self, name):
        root = self.temporary_root / name
        for branch in ("state", "logs", "runs", "playbacks"):
            (root / branch).mkdir(parents=True, exist_ok=True)
        return root

    def context(self, workflow, name=None, clock_time=None):
        root = self.service_root(name or workflow)
        selected_time = clock_time or self.clock_time
        return runtime.build_runtime_context(
            workflow,
            service_root=root,
            clock=lambda: selected_time,
        )

    def test_default_root_and_exact_continuous_topology(self):
        self.assertEqual(
            runtime.SERVICE_ROOT,
            Path("/home/sysop/runtime/pyfinder"),
        )
        context = self.context("continuous")
        root = context.service_root

        self.assertEqual(context.state_directory, root / "state")
        self.assertEqual(context.logs_directory, root / "logs")
        self.assertEqual(context.runs_directory, root / "runs")
        self.assertEqual(context.playbacks_directory, root / "playbacks")
        self.assertEqual(
            context.process_log_path,
            root / "logs/continuous/monitoring.log",
        )
        self.assertEqual(
            context.listener_log_path,
            root / "logs/continuous/seismiclistener.log",
        )
        self.assertEqual(
            context.scheduler_log_path,
            root / "logs/continuous/followupscheduler.log",
        )
        self.assertEqual(
            context.paramws_log_path,
            root / "logs/continuous/paramws.log",
        )
        self.assertEqual(
            context.operational_database_path,
            root / "state/scheduled_queries.sqlite3",
        )
        self.assertEqual(context.work_root, root / "runs")

        production_paths = {
            name: runtime.SERVICE_ROOT / path.relative_to(root)
            for name, path in {
                "monitoring": context.process_log_path,
                "listener": context.listener_log_path,
                "scheduler": context.scheduler_log_path,
                "paramws": context.paramws_log_path,
                "database": context.operational_database_path,
                "work": context.work_root,
            }.items()
        }
        self.assertEqual(
            production_paths,
            {
                "monitoring": Path(
                    "/home/sysop/runtime/pyfinder/logs/continuous/monitoring.log"
                ),
                "listener": Path(
                    "/home/sysop/runtime/pyfinder/logs/continuous/"
                    "seismiclistener.log"
                ),
                "scheduler": Path(
                    "/home/sysop/runtime/pyfinder/logs/continuous/"
                    "followupscheduler.log"
                ),
                "paramws": Path(
                    "/home/sysop/runtime/pyfinder/logs/continuous/paramws.log"
                ),
                "database": Path(
                    "/home/sysop/runtime/pyfinder/state/"
                    "scheduled_queries.sqlite3"
                ),
                "work": Path("/home/sysop/runtime/pyfinder/runs"),
            },
        )

    def test_experimental_modes_share_one_captured_trigger_directory(self):
        for workflow in ("playback", "on-demand"):
            with self.subTest(workflow=workflow):
                context = self.context(workflow)
                root = context.service_root
                expected_directory = (
                    root
                    / "logs/playbacks/20320405T060708.901234Z"
                )
                self.assertEqual(
                    context.process_log_directory,
                    expected_directory,
                )
                self.assertEqual(
                    context.process_log_path,
                    expected_directory / "playback.log",
                )
                self.assertEqual(
                    context.scheduler_log_path,
                    expected_directory / "followupscheduler.log",
                )
                self.assertEqual(
                    context.paramws_log_path,
                    expected_directory / "paramws.log",
                )
                self.assertIsNone(context.listener_log_path)
                self.assertIsNone(context.operational_database_path)
                self.assertEqual(context.work_root, root / "playbacks")

    def test_log_destination_validation_matches_each_workflow(self):
        expected_attributes = {
            "continuous": (
                "process_log_path",
                "listener_log_path",
                "scheduler_log_path",
                "paramws_log_path",
            ),
            "playback": (
                "process_log_path",
                "scheduler_log_path",
                "paramws_log_path",
            ),
            "on-demand": (
                "process_log_path",
                "paramws_log_path",
            ),
        }
        original_validation = runtime._validate_log_destination

        for workflow, attributes in expected_attributes.items():
            with self.subTest(workflow=workflow):
                observed_paths = []

                def record_validation(path):
                    observed_paths.append(path)
                    original_validation(path)

                with mock.patch.object(
                    runtime,
                    "_validate_log_destination",
                    side_effect=record_validation,
                ):
                    context = self.context(
                        workflow,
                        name="validation-{0}".format(workflow),
                    )

                self.assertEqual(
                    observed_paths,
                    [getattr(context, attribute) for attribute in attributes],
                )

    def test_separate_experimental_commands_get_separate_trigger_directories(self):
        first = self.context("playback", name="shared")
        second = self.context(
            "on-demand",
            name="shared",
            clock_time=self.clock_time + timedelta(microseconds=1),
        )

        self.assertNotEqual(
            first.process_log_directory,
            second.process_log_directory,
        )
        self.assertTrue(first.process_log_directory.is_dir())
        self.assertTrue(second.process_log_directory.is_dir())

    def test_trigger_collision_recaptures_time_without_sharing_logs(self):
        first = self.context("playback", name="collision")
        later_time = self.clock_time + timedelta(microseconds=1)
        clock_values = iter((self.clock_time, later_time))
        second = runtime.build_runtime_context(
            "on-demand",
            service_root=first.service_root,
            clock=lambda: next(clock_values),
        )

        self.assertEqual(second.trigger_time, later_time)
        self.assertNotEqual(
            first.process_log_directory,
            second.process_log_directory,
        )

    def test_configuration_isolated_by_mode_without_global_mutation(self):
        configuration = {
            "general": {"services-enabled": ["RRSM_PeakMotion"]},
            "finder-executable": {
                "output-root-folder": "/original/output",
                "path": "/usr/local/src/FinDer/finder_run",
            },
        }
        original = deepcopy(configuration)

        continuous = self.context("continuous").isolated_configuration(
            configuration
        )
        on_demand = self.context("on-demand").isolated_configuration(
            configuration
        )

        self.assertEqual(configuration, original)
        self.assertIsNot(continuous, configuration)
        self.assertIsNot(on_demand, configuration)
        self.assertEqual(
            continuous["finder-executable"]["output-root-folder"],
            str(self.temporary_root / "continuous/runs"),
        )
        self.assertEqual(
            on_demand["finder-executable"]["output-root-folder"],
            str(self.temporary_root / "on-demand/playbacks"),
        )
        self.assertEqual(
            continuous["general"],
            configuration["general"],
        )

    def test_playback_databases_are_independent_external_and_self_cleaning(self):
        first = self.context("playback", name="first")
        second = self.context("playback", name="second")

        with first.playback_database() as first_path:
            first_path.write_text("first", encoding="utf-8")
            first_directory = first_path.parent
            with second.playback_database() as second_path:
                second_path.write_text("second", encoding="utf-8")
                second_directory = second_path.parent
                self.assertNotEqual(first_path, second_path)
                for context, database_path in (
                    (first, first_path),
                    (second, second_path),
                ):
                    with self.assertRaises(ValueError):
                        database_path.resolve().relative_to(
                            context.service_root.resolve()
                        )
            self.assertFalse(second_directory.exists())
            self.assertTrue(first_path.exists())

        self.assertFalse(first_directory.exists())

    def test_missing_or_unusable_runtime_fails_before_workflow_import(self):
        cases = []
        missing = self.temporary_root / "missing"
        cases.append(("missing", missing))

        unusable = self.service_root("unusable")
        (unusable / "logs/continuous").mkdir()
        (unusable / "logs/continuous/paramws.log").mkdir()
        cases.append(("unusable log", unusable))

        for label, service_root in cases:
            with self.subTest(label=label), without_paramws_modules():
                importer = mock.Mock()
                arguments = cli.build_parser().parse_args(["continuous"])
                with mock.patch.dict(
                    os.environ,
                    {"PARAMWS_LOG_FILE": "/conflicting/location.log"},
                ):
                    with self.assertRaises(runtime.RuntimeBootstrapError):
                        cli.dispatch(
                            arguments,
                            bootstrap=lambda workflow: (
                                runtime.bootstrap_process(
                                    workflow,
                                    service_root=service_root,
                                    clock=lambda: self.clock_time,
                                )
                            ),
                            importer=importer,
                        )
                    self.assertEqual(
                        os.environ["PARAMWS_LOG_FILE"],
                        "/conflicting/location.log",
                    )
                importer.assert_not_called()

    def test_existing_paramws_import_fails_before_paths_or_environment_change(self):
        service_root = self.service_root("preimported")
        original_value = "/conflicting/location.log"
        with mock.patch.dict(
            sys.modules,
            {"paramws": ModuleType("paramws")},
        ), mock.patch.dict(
            os.environ,
            {"PARAMWS_LOG_FILE": original_value},
        ), mock.patch.object(
            runtime,
            "build_runtime_context",
            autospec=True,
        ) as builder:
            with self.assertRaisesRegex(
                runtime.RuntimeBootstrapError,
                "ParamWS was imported before runtime bootstrap",
            ):
                runtime.bootstrap_process(
                    "continuous",
                    service_root=service_root,
                )

            builder.assert_not_called()
            self.assertEqual(os.environ["PARAMWS_LOG_FILE"], original_value)

    def test_unwritable_paramws_destination_fails_before_import(self):
        service_root = self.service_root("unwritable")
        importer = mock.Mock()
        arguments = cli.build_parser().parse_args(["continuous"])
        original_open = Path.open

        def reject_paramws(path, *args, **kwargs):
            if path.name == "paramws.log":
                raise PermissionError("write denied")
            return original_open(path, *args, **kwargs)

        with without_paramws_modules(), mock.patch.object(
            Path,
            "open",
            autospec=True,
            side_effect=reject_paramws,
        ), mock.patch.dict(
            os.environ,
            {"PARAMWS_LOG_FILE": "/conflicting/location.log"},
        ):
            with self.assertRaisesRegex(
                runtime.RuntimeBootstrapError,
                "required log destination is unusable",
            ):
                cli.dispatch(
                    arguments,
                    bootstrap=lambda workflow: runtime.bootstrap_process(
                        workflow,
                        service_root=service_root,
                        clock=lambda: self.clock_time,
                    ),
                    importer=importer,
                )

            self.assertEqual(
                os.environ["PARAMWS_LOG_FILE"],
                "/conflicting/location.log",
            )
        importer.assert_not_called()

    def test_continuous_component_log_failure_stops_before_import(self):
        service_root = self.service_root("continuous-log-failure")
        process_directory = service_root / "logs/continuous"
        process_directory.mkdir()
        failing_path = process_directory / "followupscheduler.log"
        failing_path.mkdir()
        outside_directory = self.temporary_root / "continuous-outside"
        home_directory = self.temporary_root / "continuous-home"
        outside_directory.mkdir()
        home_directory.mkdir()
        workflow_callable = mock.Mock()
        importer = mock.Mock(
            return_value=SimpleNamespace(start_services=workflow_callable)
        )
        arguments = cli.build_parser().parse_args(["continuous"])
        previous_paramws_log = "/previous/paramws.log"

        with without_paramws_modules(), current_directory(
            outside_directory
        ), mock.patch.dict(
            os.environ,
            {
                "HOME": str(home_directory),
                "PARAMWS_LOG_FILE": previous_paramws_log,
            },
        ):
            with self.assertRaises(runtime.RuntimeBootstrapError) as raised:
                cli.dispatch(
                    arguments,
                    bootstrap=lambda workflow: runtime.bootstrap_process(
                        workflow,
                        service_root=service_root,
                        clock=lambda: self.clock_time,
                    ),
                    importer=importer,
                )

            self.assertIn(str(failing_path), str(raised.exception))
            self.assertEqual(
                os.environ["PARAMWS_LOG_FILE"],
                previous_paramws_log,
            )

        importer.assert_not_called()
        workflow_callable.assert_not_called()
        self.assertFalse((process_directory / "paramws.log").exists())
        self.assertEqual(tuple(outside_directory.iterdir()), ())
        self.assertEqual(tuple(home_directory.iterdir()), ())
        (process_directory / "monitoring.log").unlink()
        (process_directory / "seismiclistener.log").unlink()

    def test_playback_log_failure_stops_before_import(self):
        service_root = self.service_root("playback-log-failure")
        outside_directory = self.temporary_root / "playback-outside"
        home_directory = self.temporary_root / "playback-home"
        outside_directory.mkdir()
        home_directory.mkdir()
        workflow_callable = mock.Mock()
        importer = mock.Mock(
            return_value=SimpleNamespace(run_cli=workflow_callable)
        )
        arguments = cli.build_parser().parse_args(["playback"])
        previous_paramws_log = "/previous/paramws.log"
        original_probe = runtime._probe_directory_write
        process_directories = []

        def add_unusable_playback_log(path):
            original_probe(path)
            path = Path(path)
            if path.parent == service_root / "logs/playbacks":
                (path / "playback.log").mkdir()
                process_directories.append(path)

        with without_paramws_modules(), current_directory(
            outside_directory
        ), mock.patch.object(
            runtime,
            "_probe_directory_write",
            side_effect=add_unusable_playback_log,
        ), mock.patch.dict(
            os.environ,
            {
                "HOME": str(home_directory),
                "PARAMWS_LOG_FILE": previous_paramws_log,
            },
        ):
            with self.assertRaises(runtime.RuntimeBootstrapError) as raised:
                cli.dispatch(
                    arguments,
                    bootstrap=lambda workflow: runtime.bootstrap_process(
                        workflow,
                        service_root=service_root,
                        clock=lambda: self.clock_time,
                    ),
                    importer=importer,
                )

            self.assertEqual(len(process_directories), 1)
            failing_path = process_directories[0] / "playback.log"
            self.assertIn(str(failing_path), str(raised.exception))
            self.assertEqual(
                os.environ["PARAMWS_LOG_FILE"],
                previous_paramws_log,
            )

        importer.assert_not_called()
        workflow_callable.assert_not_called()
        self.assertFalse((process_directories[0] / "paramws.log").exists())
        self.assertEqual(tuple(outside_directory.iterdir()), ())
        self.assertEqual(tuple(home_directory.iterdir()), ())

    def test_bootstrap_replaces_conflicting_environment_before_import(self):
        service_root = self.service_root("ordered")
        arguments = cli.build_parser().parse_args(
            ["on-demand", "--event-id", "EVENT"]
        )
        workflow_callable = mock.Mock(return_value=19)
        module = SimpleNamespace(run_cli=workflow_callable)

        def importer(module_name):
            self.assertEqual(module_name, "pyfinder.findermanager")
            self.assertEqual(
                os.environ["PARAMWS_LOG_FILE"],
                str(observed_context[0].paramws_log_path),
            )
            return module

        def bootstrap(workflow):
            context = runtime.bootstrap_process(
                workflow,
                service_root=service_root,
                clock=lambda: self.clock_time,
            )
            observed_context.append(context)
            return context

        observed_context = []
        with without_paramws_modules(), mock.patch.dict(
            os.environ,
            {"PARAMWS_LOG_FILE": "/conflicting/location.log"},
        ):
            result = cli.dispatch(
                arguments,
                bootstrap=bootstrap,
                importer=importer,
            )

        self.assertEqual(result, 19)
        workflow_callable.assert_called_once_with(
            arguments,
            runtime_context=observed_context[0],
        )

    def test_fresh_process_paramws_handlers_use_each_mode_destination(self):
        probe = r"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, sys.argv[1])
from pyfinder.runtime import bootstrap_process

context = bootstrap_process(
    sys.argv[2],
    service_root=Path(sys.argv[3]),
    clock=lambda: datetime(2032, 4, 5, 6, 7, 8, 901234,
                           tzinfo=timezone.utc),
)
from paramws import clients
from paramws.utils import customlogger

destinations = [
    str(Path(handler.baseFilename).resolve())
    for handler in customlogger.logger.handlers
    if isinstance(handler, logging.FileHandler)
]
print(json.dumps({
    "expected": str(context.paramws_log_path.resolve()),
    "destinations": destinations,
}))
"""
        outside_directory = self.temporary_root / "outside"
        outside_directory.mkdir()

        for index, workflow in enumerate(
            ("continuous", "playback", "on-demand")
        ):
            with self.subTest(workflow=workflow):
                service_root = self.service_root(
                    "fresh-{0}-{1}".format(index, workflow)
                )
                environment = os.environ.copy()
                environment["PARAMWS_LOG_FILE"] = "/conflicting/location.log"
                result = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        probe,
                        str(PROJECT_ROOT),
                        workflow,
                        str(service_root),
                    ],
                    cwd=outside_directory,
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                evidence = json.loads(result.stdout)
                self.assertEqual(
                    evidence["destinations"],
                    [evidence["expected"]],
                )

        self.assertEqual(tuple(outside_directory.iterdir()), ())


if __name__ == "__main__":
    unittest.main()
