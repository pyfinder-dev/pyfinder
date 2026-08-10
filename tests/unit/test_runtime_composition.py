"""Tests for runtime paths reaching existing workflow composition seams."""

import atexit
from copy import deepcopy
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


_PARAMWS_LOG_DIRECTORY = tempfile.TemporaryDirectory(
    prefix="pyfinder-runtime-composition-import-"
)
atexit.register(_PARAMWS_LOG_DIRECTORY.cleanup)
_original_paramws_log_file = os.environ.get("PARAMWS_LOG_FILE")
os.environ["PARAMWS_LOG_FILE"] = str(
    Path(_PARAMWS_LOG_DIRECTORY.name) / "paramws.log"
)
try:
    from pyfinder import finderexec, findermanager, runtime, start_monitoring
    from pyfinder.pyfinderconfig import pyfinderconfig
finally:
    if _original_paramws_log_file is None:
        os.environ.pop("PARAMWS_LOG_FILE", None)
    else:
        os.environ["PARAMWS_LOG_FILE"] = _original_paramws_log_file


class ImmediateThread:
    def __init__(self, target, daemon):
        self.target = target
        self.daemon = daemon

    def start(self):
        self.target()


class RuntimeCompositionTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory(
            prefix="pyfinder-runtime-composition-"
        )
        self.service_root = Path(self.temporary_directory.name) / "service"
        for branch in ("state", "logs", "runs", "playbacks"):
            (self.service_root / branch).mkdir(parents=True, exist_ok=True)
        self.runtime_context = runtime.build_runtime_context(
            "continuous",
            service_root=self.service_root,
        )
        self.original_configuration = deepcopy(pyfinderconfig)
        start_monitoring._listener_thread = None
        start_monitoring._scheduler = None
        start_monitoring._launcher_logger = None

    def tearDown(self):
        start_monitoring._listener_thread = None
        start_monitoring._scheduler = None
        start_monitoring._launcher_logger = None
        self.assertEqual(pyfinderconfig, self.original_configuration)
        self.temporary_directory.cleanup()

    def test_continuous_composition_wires_exact_logs_database_and_runs_root(self):
        launcher_logger = mock.Mock(spec=logging.Logger)
        listener_logger = mock.Mock(spec=logging.Logger)
        scheduler_logger = mock.Mock(spec=logging.Logger)
        loggers = {
            self.runtime_context.process_log_path: launcher_logger,
            self.runtime_context.listener_log_path: listener_logger,
            self.runtime_context.scheduler_log_path: scheduler_logger,
        }
        file_logger_calls = []
        policies = {"RRSM": object(), "ESM": object()}
        selector = object()
        scheduler = mock.Mock()

        def configure_logger(path, **kwargs):
            path = Path(path)
            file_logger_calls.append((path, kwargs))
            return loggers[path]

        with mock.patch.object(
            start_monitoring,
            "file_logger",
            side_effect=configure_logger,
        ), mock.patch.object(
            start_monitoring,
            "build_service_policies",
            return_value=policies,
        ), mock.patch.object(
            start_monitoring,
            "build_default_selector",
            return_value=selector,
        ), mock.patch.object(
            start_monitoring.threading,
            "Thread",
            side_effect=ImmediateThread,
        ), mock.patch.object(
            start_monitoring.seismiclistener,
            "start_emsc_listener",
        ) as listener_start, mock.patch.object(
            start_monitoring,
            "FollowUpScheduler",
            return_value=scheduler,
        ) as scheduler_constructor:
            start_monitoring.start_services(
                runtime_context=self.runtime_context
            )

        self.assertEqual(
            [path for path, _ in file_logger_calls],
            [
                self.runtime_context.process_log_path,
                self.runtime_context.listener_log_path,
                self.runtime_context.scheduler_log_path,
            ],
        )
        listener_arguments = listener_start.call_args.kwargs
        self.assertIs(listener_arguments["policy"], policies["RRSM"])
        self.assertEqual(
            listener_arguments["db_path"],
            self.runtime_context.operational_database_path,
        )
        self.assertIs(listener_arguments["logger"], listener_logger)

        scheduler_arguments = scheduler_constructor.call_args.kwargs
        self.assertEqual(
            scheduler_arguments["db_path"],
            self.runtime_context.operational_database_path,
        )
        self.assertIs(scheduler_arguments["logger"], scheduler_logger)
        self.assertIs(
            scheduler_arguments["configuration"],
            listener_arguments["configuration"],
        )
        self.assertIsNot(
            scheduler_arguments["configuration"],
            pyfinderconfig,
        )
        self.assertEqual(
            scheduler_arguments["configuration"]["finder-executable"][
                "output-root-folder"
            ],
            str(self.runtime_context.runs_directory),
        )
        scheduler.run_forever.assert_called_once_with()

    def test_direct_manager_opens_no_shared_file_logger(self):
        configuration = deepcopy(pyfinderconfig)
        logger_name = "pyfinder.findermanager"

        with mock.patch.object(
            findermanager.customlogger,
            "file_logger",
            autospec=True,
        ) as file_logger:
            manager = findermanager.FinDerManager.for_on_demand(
                options={"use_library": False},
                configuration=configuration,
                finder_configuration_name="global",
                finder_configuration={"DATA_FOLDER": "unused"},
            )

        file_logger.assert_not_called()
        self.assertIs(manager.logger, logging.getLogger(logger_name))

    def executable(self, output_root, logger=None):
        configuration = deepcopy(pyfinderconfig)
        configuration["finder-executable"]["output-root-folder"] = str(
            output_root
        )
        return finderexec.FinDerExecutable(
            options={"command_line_args": "runtime-composition-test"},
            configuration=configuration,
            finder_configuration_name="global",
            finder_configuration={"DATA_FOLDER": "unused"},
            logger=logger,
        )

    def test_executable_reuses_injected_manager_logger(self):
        calculation_logger = mock.Mock(spec=logging.Logger)
        executable = self.executable(
            self.runtime_context.runs_directory,
            logger=calculation_logger,
        )

        with mock.patch.object(
            finderexec.customlogger,
            "file_logger",
            autospec=True,
        ) as file_logger, mock.patch.object(
            executable,
            "_write_finder_configuration",
        ):
            executable._prepare_workspace("event-id")

        self.assertIs(executable.logger, calculation_logger)
        file_logger.assert_not_called()

    def test_work_root_failure_propagates_without_home_fallback(self):
        invalid_root = Path(self.temporary_directory.name) / "not-a-directory"
        invalid_root.write_text("occupied", encoding="utf-8")
        calculation_logger = mock.Mock(spec=logging.Logger)
        executable = self.executable(invalid_root, logger=calculation_logger)

        with mock.patch.object(
            finderexec.os.path,
            "expanduser",
            autospec=True,
        ) as expand_home, mock.patch.object(
            finderexec.customlogger,
            "file_logger",
            autospec=True,
        ) as file_logger:
            with self.assertRaises(FileExistsError):
                executable._prepare_workspace("event-id")

        expand_home.assert_not_called()
        file_logger.assert_not_called()
        self.assertIs(executable.logger, calculation_logger)


if __name__ == "__main__":
    unittest.main()
