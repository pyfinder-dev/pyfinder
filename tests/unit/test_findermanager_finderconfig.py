"""Offline tests for FinDerManager computational-configuration ownership."""

import atexit
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


# ParamWS initializes its package logger during import. Keep that dependency
# side effect out of the repository just as the existing ParamWS boundary tests
# do, while leaving its public classes available to FinDerManager.
_PARAMWS_LOG_DIRECTORY = tempfile.TemporaryDirectory(
    prefix="pyfinder-manager-config-unit-"
)
atexit.register(_PARAMWS_LOG_DIRECTORY.cleanup)
_original_paramws_log_file = os.environ.get("PARAMWS_LOG_FILE")
os.environ["PARAMWS_LOG_FILE"] = str(
    Path(_PARAMWS_LOG_DIRECTORY.name) / "paramws.log"
)
try:
    from pyfinder import finderexec, findermanager
    from pyfinder.finderconfigs import GlobalFinderConfigError
finally:
    if _original_paramws_log_file is None:
        os.environ.pop("PARAMWS_LOG_FILE", None)
    else:
        os.environ["PARAMWS_LOG_FILE"] = _original_paramws_log_file


class ExecutableBoundaryReached(RuntimeError):
    """Stop the legacy workflow immediately when execute() is invoked."""


class SelectorDouble:
    """Return one controlled decision while recording direct-call coordinates."""

    def __init__(self, name="global", configuration=None):
        self.configuration = (
            {"DATA_FOLDER": "global-data"}
            if configuration is None
            else configuration
        )
        self.decision = mock.Mock(
            configuration_name=name,
            configuration=self.configuration,
        )
        self.resolve = mock.Mock(return_value=self.decision)


class ProviderEventDouble:
    """Expose provider metadata that must never drive profile selection."""

    def get_origin_time(self):
        return "2026-08-08T12:00:00+00:00"

    def get_longitude(self):
        return 120.0

    def get_latitude(self):
        return -35.0

    def get_magnitude(self):
        return 5.5

    def get_depth(self):
        return 10.0

    def get_magnitude_type(self):
        return "Mw"


class FinDerManagerFinderConfigTests(unittest.TestCase):
    def application_configuration(self):
        return {
            "finder-executable": {
                "finder-temp-data-dir": "/tmp/finder-data",
                "finder-temp-dir": "/tmp/finder",
            }
        }

    def construct_manager(
        self,
        *,
        logger,
        selector_builder,
        metadata=None,
        finder_configuration_name=None,
        finder_configuration=None,
    ):
        with mock.patch.object(
            findermanager.customlogger,
            "file_logger",
            return_value=logger,
        ), mock.patch.object(
            findermanager,
            "build_default_selector",
            selector_builder,
        ):
            return findermanager.FinDerManager(
                options={"use_library": False},
                configuration=self.application_configuration(),
                metadata=metadata,
                finder_configuration_name=finder_configuration_name,
                finder_configuration=finder_configuration,
            )

    def test_complete_supplied_decision_bypasses_selector_and_operations(self):
        logger = mock.Mock()
        selector_builder = mock.Mock()
        selected_configuration = {"DATA_FOLDER": "regional-data"}

        with mock.patch.object(
            findermanager,
            "RRSMPeakMotionClient",
        ) as rrsm_client, mock.patch.object(
            findermanager,
            "ESMShakeMapClient",
        ) as esm_client, mock.patch.object(
            finderexec,
            "FinDerExecutable",
        ) as executable_type:
            manager = self.construct_manager(
                logger=logger,
                selector_builder=selector_builder,
                finder_configuration_name="regional",
                finder_configuration=selected_configuration,
            )

        selector_builder.assert_not_called()
        self.assertEqual(manager.finder_configuration_name, "regional")
        self.assertIs(manager.finder_configuration, selected_configuration)
        rrsm_client.assert_not_called()
        esm_client.assert_not_called()
        executable_type.assert_not_called()

    def test_omitted_decision_builds_after_logger_and_resolves_emsc_once(self):
        events = []
        logger = mock.Mock()
        selector = SelectorDouble(
            name="regional",
            configuration={"DATA_FOLDER": "regional-data"},
        )

        def configure_logger(**kwargs):
            events.append("logger")
            return logger

        def build_selector(*, logger):
            events.append("selector")
            self.assertIs(logger, expected_logger)
            return selector

        expected_logger = logger
        with mock.patch.object(
            findermanager.customlogger,
            "file_logger",
            side_effect=configure_logger,
        ), mock.patch.object(
            findermanager,
            "build_default_selector",
            side_effect=build_selector,
        ) as selector_builder, mock.patch.object(
            findermanager,
            "RRSMPeakMotionClient",
        ) as rrsm_client, mock.patch.object(
            finderexec,
            "FinDerExecutable",
        ) as executable_type:
            manager = findermanager.FinDerManager(
                options={"use_library": False},
                configuration=self.application_configuration(),
                metadata={"emsc_latitude": "46.2", "emsc_longitude": "7.3"},
            )

        self.assertEqual(events, ["logger", "selector"])
        selector_builder.assert_called_once_with(logger=logger)
        selector.resolve.assert_called_once_with(
            latitude="46.2",
            longitude="7.3",
        )
        self.assertEqual(manager.finder_configuration_name, "regional")
        self.assertIs(
            manager.finder_configuration,
            selector.configuration,
        )
        rrsm_client.assert_not_called()
        executable_type.assert_not_called()

    def test_omitted_coordinates_use_selector_global_decision(self):
        logger = mock.Mock()
        selector = SelectorDouble()
        selector_builder = mock.Mock(return_value=selector)

        manager = self.construct_manager(
            logger=logger,
            selector_builder=selector_builder,
        )

        selector.resolve.assert_called_once_with(latitude=None, longitude=None)
        self.assertEqual(manager.finder_configuration_name, "global")
        self.assertIs(manager.finder_configuration, selector.configuration)

    def test_partial_handoff_is_ignored_and_always_resolves_global(self):
        supplied_mapping = {"DATA_FOLDER": "must-not-be-used"}
        cases = (
            ("name only", "regional", None),
            ("mapping only", None, supplied_mapping),
        )
        for label, supplied_name, supplied_configuration in cases:
            with self.subTest(label=label):
                logger = mock.Mock()
                selector = SelectorDouble()
                selector_builder = mock.Mock(return_value=selector)

                manager = self.construct_manager(
                    logger=logger,
                    selector_builder=selector_builder,
                    metadata={"emsc_latitude": 46.2, "emsc_longitude": 7.3},
                    finder_configuration_name=supplied_name,
                    finder_configuration=supplied_configuration,
                )

                selector_builder.assert_called_once_with(logger=logger)
                selector.resolve.assert_called_once_with(
                    latitude=None,
                    longitude=None,
                )
                logger.critical.assert_called_once()
                self.assertEqual(manager.finder_configuration_name, "global")
                self.assertIs(
                    manager.finder_configuration,
                    selector.configuration,
                )
                self.assertIsNot(
                    manager.finder_configuration,
                    supplied_configuration,
                )

    def test_global_validation_error_is_logged_and_reraised(self):
        logger = mock.Mock()
        error = GlobalFinderConfigError("global unusable")
        selector_builder = mock.Mock(side_effect=error)

        with self.assertRaises(GlobalFinderConfigError) as raised:
            self.construct_manager(
                logger=logger,
                selector_builder=selector_builder,
            )

        self.assertIs(raised.exception, error)
        logger.critical.assert_called_once()
        self.assertIs(logger.critical.call_args.kwargs["exc_info"], True)

    def exercise_executable_boundary(self, manager):
        provider_event = ProviderEventDouble()
        peak_motion = object()
        merged_amplitudes = ["merged-normalized"]
        executable_instance = mock.Mock()
        executable_instance.execute.side_effect = ExecutableBoundaryReached(
            "stop after executable invocation"
        )

        with mock.patch.object(
            findermanager,
            "RRSMPeakMotionClient",
        ) as rrsm_type, mock.patch.object(
            findermanager,
            "ESMShakeMapClient",
        ) as esm_type, mock.patch.object(
            findermanager.RRSMPeakMotionDataFormatter,
            "extract_raw_stations",
            return_value=["rrsm-raw"],
        ), mock.patch.object(
            findermanager.ESMShakeMapDataFormatter,
            "extract_raw_stations",
        ) as esm_formatter, mock.patch.object(
            findermanager,
            "StationMerger",
        ) as merger_type, mock.patch.object(
            finderexec,
            "FinDerExecutable",
            return_value=executable_instance,
        ) as executable_type:
            rrsm_type.return_value.query.return_value = (
                200,
                provider_event,
                {"peak_motion": peak_motion},
            )
            esm_type.return_value.query.return_value = (
                404,
                None,
                {"station_amplitudes": None},
            )
            merger_type.return_value.merge.return_value = merged_amplitudes

            with self.assertRaises(ExecutableBoundaryReached):
                manager.process_event("event-1")

        esm_formatter.assert_not_called()
        executable_type.assert_called_once_with(
            options=manager.options,
            configuration=manager.configuration,
            finder_configuration_name=manager.finder_configuration_name,
            finder_configuration=manager.finder_configuration,
        )
        executable_instance.execute.assert_called_once_with(
            event_data=provider_event,
            amplitudes=merged_amplitudes,
        )
        return executable_type

    def test_regional_and_global_decisions_each_use_one_executable(self):
        cases = (
            ("regional", {"DATA_FOLDER": "regional-data"}),
            ("global", {"DATA_FOLDER": "global-data"}),
        )
        for name, configuration in cases:
            with self.subTest(name=name):
                selector_builder = mock.Mock()
                manager = self.construct_manager(
                    logger=mock.Mock(),
                    selector_builder=selector_builder,
                    finder_configuration_name=name,
                    finder_configuration=configuration,
                )

                executable_type = self.exercise_executable_boundary(manager)

                selector_builder.assert_not_called()
                self.assertEqual(executable_type.call_count, 1)

    def test_provider_coordinates_never_trigger_another_selection(self):
        logger = mock.Mock()
        selector = SelectorDouble(
            name="regional",
            configuration={"DATA_FOLDER": "regional-data"},
        )
        selector_builder = mock.Mock(return_value=selector)
        manager = self.construct_manager(
            logger=logger,
            selector_builder=selector_builder,
            metadata={"emsc_latitude": 1.0, "emsc_longitude": 2.0},
        )

        self.exercise_executable_boundary(manager)

        selector.resolve.assert_called_once_with(latitude=1.0, longitude=2.0)


if __name__ == "__main__":
    unittest.main()
