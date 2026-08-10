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
    from pyfinder.eventcontext import EventContext
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

    def get_event_id(self):
        return "event-1"

    def get_origin_time(self):
        return "2026-08-08T12:00:00.000000Z"

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
    def event_context(self):
        return EventContext.from_alert_mapping(
            {
                "unid": "event-1",
                "lat": 46.2,
                "lon": 7.3,
                "mag": 6.1,
                "depth": 9.5,
                "time": "2026-08-10T08:15:30.250000Z",
                "magtype": "Mw",
            },
            scheduled_event_id="event-1",
        )

    def application_configuration(self):
        return {
            "general": {
                "services-enabled": ["RRSM_PeakMotion", "ESM_ShakeMap"],
                "services-priority": ["ESM_ShakeMap", "RRSM_PeakMotion"],
                "component-selection": "maximum-all",
            },
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
        event_context=None,
        alert_backed=False,
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
            arguments = {
                "options": {"use_library": False},
                "configuration": self.application_configuration(),
                "logger": logger,
                "metadata": metadata,
                "finder_configuration_name": finder_configuration_name,
                "finder_configuration": finder_configuration,
            }
            if alert_backed:
                return findermanager.FinDerManager.for_alert_context(
                    event_context=event_context,
                    **arguments,
                )
            return findermanager.FinDerManager.for_on_demand(**arguments)

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

    def test_omitted_on_demand_decision_defers_until_provider_context(self):
        logger = mock.Mock()
        selector = SelectorDouble(
            name="regional",
            configuration={"DATA_FOLDER": "regional-data"},
        )

        with mock.patch.object(
            findermanager.customlogger,
            "file_logger",
            autospec=True,
        ) as file_logger, mock.patch.object(
            findermanager,
            "build_default_selector",
            return_value=selector,
        ), mock.patch.object(
            findermanager,
            "RRSMPeakMotionClient",
        ) as rrsm_client, mock.patch.object(
            finderexec,
            "FinDerExecutable",
        ) as executable_type:
            manager = findermanager.FinDerManager.for_on_demand(
                options={"use_library": False},
                configuration=self.application_configuration(),
                logger=logger,
                metadata={"emsc_latitude": "46.2", "emsc_longitude": "7.3"},
            )

        file_logger.assert_not_called()
        self.assertIs(manager.logger, logger)
        selector.resolve.assert_not_called()
        self.assertIsNone(manager.finder_configuration_name)
        self.assertIsNone(manager.finder_configuration)
        rrsm_client.assert_not_called()
        executable_type.assert_not_called()

    def test_omitted_coordinates_do_not_create_global_on_demand_decision(self):
        logger = mock.Mock()
        selector = SelectorDouble()
        selector_builder = mock.Mock(return_value=selector)

        manager = self.construct_manager(
            logger=logger,
            selector_builder=selector_builder,
        )

        selector_builder.assert_not_called()
        selector.resolve.assert_not_called()
        self.assertIsNone(manager.finder_configuration_name)
        self.assertIsNone(manager.finder_configuration)

    def test_alert_context_populates_metadata_and_selects_its_epicenter_once(self):
        context = self.event_context()
        logger = mock.Mock()
        selector = SelectorDouble(
            name="regional",
            configuration={"DATA_FOLDER": "regional-data"},
        )

        manager = self.construct_manager(
            logger=logger,
            selector_builder=mock.Mock(return_value=selector),
            event_context=context,
            alert_backed=True,
        )

        selector.resolve.assert_called_once_with(latitude=46.2, longitude=7.3)
        self.assertIs(manager.event_context, context)
        self.assertEqual(
            manager.metadata,
            {
                "origin_time": "2026-08-10T08:15:30.250000Z",
                "longitude": 7.3,
                "latitude": 46.2,
                "magnitude": 6.1,
                "depth": 9.5,
                "magnitude_type": "Mw",
            },
        )

    def test_missing_alert_context_fails_critically_before_provider_construction(self):
        logger = mock.Mock()
        selector_builder = mock.Mock()
        manager = self.construct_manager(
            logger=logger,
            selector_builder=selector_builder,
            event_context=None,
            alert_backed=True,
        )

        with mock.patch.object(
            findermanager,
            "RRSMPeakMotionClient",
        ) as rrsm_client, mock.patch.object(
            findermanager,
            "ESMShakeMapClient",
        ) as esm_client:
            result = manager.run(event_id="event-1")

        self.assertIsNone(result)
        logger.critical.assert_called_once()
        self.assertIn(
            "authoritative EMSC alert context",
            logger.critical.call_args.args[0],
        )
        selector_builder.assert_not_called()
        rrsm_client.assert_not_called()
        esm_client.assert_not_called()

    def test_missing_context_cannot_select_on_demand_implicitly(self):
        logger = mock.Mock()
        manager = self.construct_manager(
            logger=logger,
            selector_builder=mock.Mock(),
            finder_configuration_name="global",
            finder_configuration={"DATA_FOLDER": "global-data"},
            event_context=None,
            alert_backed=True,
        )

        self.assertEqual(manager.entry_kind, manager.ALERT_BACKED)
        self.assertIsNone(manager.process_event("event-1"))
        logger.critical.assert_called_once()

    def test_explicit_on_demand_entry_remains_reachable(self):
        manager = self.construct_manager(
            logger=mock.Mock(),
            selector_builder=mock.Mock(),
            finder_configuration_name="global",
            finder_configuration={"DATA_FOLDER": "global-data"},
        )

        self.assertEqual(manager.entry_kind, manager.ON_DEMAND)
        self.assertIsNone(manager.event_context)

    def test_partial_handoff_is_ignored_and_resolution_remains_deferred(self):
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

                selector_builder.assert_not_called()
                selector.resolve.assert_not_called()
                logger.critical.assert_called_once()
                self.assertIsNone(manager.finder_configuration_name)
                self.assertIsNone(manager.finder_configuration)

    def test_deferred_global_validation_error_is_logged_and_reraised(self):
        logger = mock.Mock()
        error = GlobalFinderConfigError("global unusable")
        selector_builder = mock.Mock(side_effect=error)
        manager = self.construct_manager(
            logger=logger,
            selector_builder=mock.Mock(),
        )

        with mock.patch.object(
            findermanager,
            "build_default_selector",
            selector_builder,
        ):
            with self.assertRaises(GlobalFinderConfigError) as raised:
                manager._resolve_finder_configuration_from_context(
                    self.event_context()
                )

        self.assertIs(raised.exception, error)
        logger.critical.assert_called_once()
        self.assertIs(logger.critical.call_args.kwargs["exc_info"], True)

    def exercise_executable_boundary(self, manager):
        provider_event = ProviderEventDouble()
        peak_motion = mock.Mock(name="peak_motion")
        peak_motion.get_event_data.return_value = provider_event
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
            logger=manager.logger,
        )
        executable_instance.execute.assert_called_once_with(
            event_data=manager.event_context,
            amplitudes=merged_amplitudes,
            augmented_event_id="event-1_t00000",
        )
        self.assertIsNot(manager.event_context, provider_event)
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

    def test_on_demand_provider_context_triggers_one_regional_selection(self):
        logger = mock.Mock()
        selector = SelectorDouble(
            name="regional",
            configuration={"DATA_FOLDER": "regional-data"},
        )
        selector_builder = mock.Mock(return_value=selector)
        manager = self.construct_manager(
            logger=logger,
            selector_builder=selector_builder,
            metadata={
                "emsc_latitude": 1.0,
                "emsc_longitude": 2.0,
                "current_delay": 99,
            },
        )

        with mock.patch.object(
            findermanager,
            "build_default_selector",
            return_value=selector,
        ):
            self.exercise_executable_boundary(manager)

        selector.resolve.assert_called_once_with(latitude=-35.0, longitude=120.0)

    def test_alert_context_replaces_contradictory_provider_models_everywhere(self):
        context = self.event_context()
        manager = self.construct_manager(
            logger=mock.Mock(),
            selector_builder=mock.Mock(),
            metadata={"current_delay": 10},
            finder_configuration_name="regional",
            finder_configuration={"DATA_FOLDER": "regional-data"},
            event_context=context,
            alert_backed=True,
        )
        rrsm_event = mock.Mock(name="contradictory_rrsm_event")
        esm_event = mock.Mock(name="contradictory_esm_event")
        rrsm_amplitudes = mock.Mock(name="rrsm_amplitudes")
        rrsm_amplitudes.get_event_data.side_effect = AssertionError(
            "alert-backed RRSM must not read nested provider event metadata"
        )
        esm_amplitudes = object()
        executable = mock.Mock()
        executable.execute.side_effect = ExecutableBoundaryReached()

        with mock.patch.object(
            findermanager,
            "RRSMPeakMotionClient",
        ) as rrsm_type, mock.patch.object(
            findermanager,
            "ESMShakeMapClient",
        ) as esm_type, mock.patch.object(
            findermanager.RRSMPeakMotionDataFormatter,
            "extract_raw_stations",
            return_value=["rrsm"],
        ) as rrsm_extract, mock.patch.object(
            findermanager.ESMShakeMapDataFormatter,
            "extract_raw_stations",
            return_value=["esm"],
        ) as esm_extract, mock.patch.object(
            findermanager,
            "StationMerger",
        ) as merger_type, mock.patch.object(
            finderexec,
            "FinDerExecutable",
            return_value=executable,
        ):
            rrsm_type.return_value.query.return_value = (
                200,
                rrsm_event,
                {"peak_motion": rrsm_amplitudes},
            )
            esm_type.return_value.query.return_value = (
                200,
                esm_event,
                {"station_amplitudes": esm_amplitudes},
            )
            merger_type.return_value.merge.return_value = ["merged"]

            with self.assertRaises(ExecutableBoundaryReached):
                manager.process_event("event-1")

        rrsm_extract.assert_called_once_with(
            event_data=context,
            amplitudes=rrsm_amplitudes,
        )
        esm_extract.assert_called_once_with(
            event_data=context,
            amplitudes=esm_amplitudes,
        )
        executable.execute.assert_called_once_with(
            event_data=context,
            amplitudes=["merged"],
            augmented_event_id="event-1_t00010",
        )
        self.assertEqual(manager.metadata["origin_time"], context.get_origin_time())
        self.assertEqual(manager.metadata["latitude"], context.get_latitude())
        self.assertEqual(manager.metadata["longitude"], context.get_longitude())
        self.assertEqual(manager.metadata["magnitude"], context.get_magnitude())
        self.assertEqual(manager.metadata["depth"], context.get_depth())
        self.assertEqual(
            manager.metadata["magnitude_type"],
            context.get_magnitude_type(),
        )
        # Provider metadata is inspected for outcome diagnostics, but it does
        # not replace the alert context used by any scientific consumer.
        rrsm_amplitudes.get_event_data.assert_not_called()


if __name__ == "__main__":
    unittest.main()
