"""Offline tests for FinDerManager's normalized observation handoff."""

import atexit
from contextlib import ExitStack
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch


# ParamWS configures its package logger during import. Keep that dependency
# side effect outside the repository when this focused module runs alone.
_PARAMWS_LOG_DIRECTORY = tempfile.TemporaryDirectory(
    prefix="pyfinder-manager-handoff-unit-")
atexit.register(_PARAMWS_LOG_DIRECTORY.cleanup)
_original_paramws_log_file = os.environ.get("PARAMWS_LOG_FILE")
os.environ["PARAMWS_LOG_FILE"] = str(
    Path(_PARAMWS_LOG_DIRECTORY.name) / "paramws.log")
try:
    from paramws.clients import PeakMotionData, ShakeMapStationAmplitudes
    from pyfinder import finderexec, findermanager
    from pyfinder.eventcontext import EventContext
    from pyfinder.pyfinderconfig import (
        EMSC_FELT_REPORT_SERVICE,
        ESM_SHAKEMAP_SERVICE,
        RRSM_PEAK_MOTION_SERVICE,
    )
finally:
    if _original_paramws_log_file is None:
        os.environ.pop("PARAMWS_LOG_FILE", None)
    else:
        os.environ["PARAMWS_LOG_FILE"] = _original_paramws_log_file


class _ExecutableBoundaryReached(Exception):
    """Stop a test immediately after the executable receives its inputs."""


class _EventModel:
    """Controlled provider-event double for manager metadata collection."""

    def __init__(self, name):
        self.name = name

    def get_event_id(self):
        return "handoff-event"

    def get_origin_time(self):
        return "2026-08-10T08:15:30.250000Z"

    def get_longitude(self):
        return 12.5

    def get_latitude(self):
        return 45.5

    def get_magnitude(self):
        return 5.6

    def get_depth(self):
        return 10.0

    def get_magnitude_type(self):
        return "Mw"


class FinDerManagerNormalizedHandoffTests(unittest.TestCase):

    event_id = "handoff-event"

    @staticmethod
    def _manager():
        # Bypass construction because logger/output setup and configuration
        # selection are independent of the observation-handoff boundary.
        manager = object.__new__(findermanager.FinDerManager)
        manager.options = {"use_library": False}
        manager.configuration = {
            "general": {
                "services-enabled": ["ESM_ShakeMap", "RRSM_PeakMotion"],
                "services-priority": ["ESM_ShakeMap", "RRSM_PeakMotion"],
                "component-selection": "maximum-all",
            }
        }
        manager.finder_configuration_name = "offline-test"
        manager.finder_configuration = {"DATA_FOLDER": "offline-test"}
        manager.metadata = {}
        manager.logger = Mock()
        manager.entry_kind = manager.ON_DEMAND
        manager.event_context = None
        manager.context_diagnostic = None
        return manager

    @staticmethod
    def _provider_models():
        """Build truthy objects from the public ParamWS model namespace."""
        return (
            PeakMotionData(data_dict={}),
            ShakeMapStationAmplitudes(data_dict={"stations": []}),
        )

    def _exercise(self, *, rrsm_event, esm_event, rrsm_provider,
                  esm_provider, rrsm_records, esm_records, merged_records,
                  expect_execution=True, enabled=None, rrsm_code=200,
                  esm_code=200, entry_kind=None, event_context=None):
        manager = self._manager()
        if enabled is not None:
            manager.configuration["general"]["services-enabled"] = enabled
        if entry_kind is not None:
            manager.entry_kind = entry_kind
            manager.event_context = event_context
        if (
            rrsm_event is not None
            and hasattr(rrsm_provider, "set_event_data")
            and rrsm_provider.get_event_data() is None
        ):
            rrsm_provider.set_event_data(rrsm_event)

        with ExitStack() as stack:
            rrsm_type = stack.enter_context(patch.object(
                findermanager, "RRSMPeakMotionClient"))
            esm_type = stack.enter_context(patch.object(
                findermanager, "ESMShakeMapClient"))
            rrsm_extract = stack.enter_context(patch.object(
                findermanager.RRSMPeakMotionDataFormatter,
                "extract_raw_stations",
                return_value=rrsm_records,
            ))
            esm_extract = stack.enter_context(patch.object(
                findermanager.ESMShakeMapDataFormatter,
                "extract_raw_stations",
                return_value=esm_records,
            ))
            rrsm_direct_format = stack.enter_context(patch.object(
                findermanager.RRSMPeakMotionDataFormatter,
                "format_data",
            ))
            esm_direct_format = stack.enter_context(patch.object(
                findermanager.ESMShakeMapDataFormatter,
                "format_data",
            ))
            artificial_point_format = stack.enter_context(patch.object(
                findermanager.FinDerFormatterFromRawList,
                "format",
            ))
            merger_type = stack.enter_context(patch.object(
                findermanager, "StationMerger"))
            priority_resolver = stack.enter_context(patch.object(
                findermanager,
                "resolve_service_priority",
                wraps=findermanager.resolve_service_priority,
            ))
            context_selector = stack.enter_context(patch.object(
                manager,
                "_select_on_demand_context",
                wraps=manager._select_on_demand_context,
            ))
            handoff = stack.enter_context(patch.object(
                manager,
                "_merge_available_results",
                wraps=manager._merge_available_results,
            ))
            executable_type = stack.enter_context(patch.object(
                finderexec, "FinDerExecutable"))

            rrsm_type.return_value.query.return_value = (
                rrsm_code,
                rrsm_event,
                {"peak_motion": rrsm_provider},
            )
            esm_type.return_value.query.return_value = (
                esm_code,
                esm_event,
                {"station_amplitudes": esm_provider},
            )
            merger_type.return_value.merge.return_value = merged_records
            executable = executable_type.return_value
            executable.execute.side_effect = _ExecutableBoundaryReached(
                "normal manager handoff reached the executable")

            if expect_execution:
                with self.assertRaises(_ExecutableBoundaryReached):
                    manager.process_event(self.event_id)
                result = None
            else:
                result = manager.process_event(self.event_id)

        return {
            "manager": manager,
            "result": result,
            "rrsm_extract": rrsm_extract,
            "esm_extract": esm_extract,
            "rrsm_direct_format": rrsm_direct_format,
            "esm_direct_format": esm_direct_format,
            "artificial_point_format": artificial_point_format,
            "merger_type": merger_type,
            "priority_resolver": priority_resolver,
            "context_selector": context_selector,
            "handoff": handoff,
            "executable_type": executable_type,
            "executable": executable,
        }

    @staticmethod
    def _messages(log_method):
        return [str(call.args[0]) for call in log_method.call_args_list]

    def _assert_mapping_merger(self, observed):
        mapping = observed["manager"].available_results
        handoff_mapping, handoff_priority = observed["handoff"].call_args.args
        self.assertIs(handoff_mapping, mapping)
        observed["merger_type"].assert_called_once_with(
            service_priority=handoff_priority,
            logger=observed["manager"].logger,
        )
        observed["merger_type"].return_value.merge.assert_called_once_with(
            mapping
        )
        self.assertEqual(
            handoff_priority,
            observed["manager"].configuration["general"]["services-priority"],
        )
        return mapping, handoff_priority

    def test_both_normalized_lists_reach_merger_and_only_merge_reaches_finder(self):
        rrsm_event = _EventModel("rrsm")
        esm_event = _EventModel("esm")
        rrsm_provider, esm_provider = self._provider_models()
        rrsm_records = [{"source": "RRSM"}]
        esm_records = [{"source": "ESM"}]
        merged_records = [{"source": "ESM"}, {"source": "RRSM"}]

        observed = self._exercise(
            rrsm_event=rrsm_event,
            esm_event=esm_event,
            rrsm_provider=rrsm_provider,
            esm_provider=esm_provider,
            rrsm_records=rrsm_records,
            esm_records=esm_records,
            merged_records=merged_records,
        )

        handoff_mapping, handoff_priority = self._assert_mapping_merger(
            observed
        )
        self.assertIs(handoff_mapping, observed["manager"].available_results)
        self.assertIs(handoff_mapping[ESM_SHAKEMAP_SERVICE], esm_records)
        self.assertIs(handoff_mapping[RRSM_PEAK_MOTION_SERVICE], rrsm_records)
        observed["priority_resolver"].assert_called_once_with(
            observed["manager"].configuration["general"]["services-priority"],
            logger=observed["manager"].logger,
        )
        self.assertIs(
            observed["context_selector"].call_args.args[1],
            handoff_priority,
        )
        observed["executable"].execute.assert_called_once_with(
            event_data=observed["manager"].event_context,
            amplitudes=merged_records,
            augmented_event_id="handoff-event_t00000",
        )
        self.assertIsNot(observed["manager"].event_context, esm_event)
        executable_amplitudes = (
            observed["executable"].execute.call_args.kwargs["amplitudes"])
        self.assertIsInstance(executable_amplitudes, list)
        self.assertIsNot(executable_amplitudes, esm_provider)
        self.assertIsNot(executable_amplitudes, rrsm_provider)

    def test_zero_esm_logs_error_and_usable_rrsm_still_reaches_finder(self):
        rrsm_event = _EventModel("rrsm")
        esm_event = _EventModel("esm")
        rrsm_provider, esm_provider = self._provider_models()
        rrsm_records = [{"source": "RRSM"}]
        merged_records = list(rrsm_records)

        observed = self._exercise(
            rrsm_event=rrsm_event,
            esm_event=esm_event,
            rrsm_provider=rrsm_provider,
            esm_provider=esm_provider,
            rrsm_records=rrsm_records,
            esm_records=[],
            merged_records=merged_records,
        )

        errors = self._messages(observed["manager"].logger.error)
        self.assertTrue(any(
            "ESM" in message
            and "zero usable normalized observations" in message
            for message in errors
        ), errors)
        self._assert_mapping_merger(observed)
        observed["executable"].execute.assert_called_once_with(
            event_data=observed["manager"].event_context,
            amplitudes=merged_records,
            augmented_event_id="handoff-event_t00000",
        )
        self.assertIsNot(
            observed["executable"].execute.call_args.kwargs["amplitudes"],
            esm_provider,
        )

    def test_usable_esm_and_zero_rrsm_continues_without_esm_error(self):
        rrsm_event = _EventModel("rrsm")
        esm_event = _EventModel("esm")
        rrsm_provider, esm_provider = self._provider_models()
        esm_records = [{"source": "ESM"}]
        merged_records = list(esm_records)

        observed = self._exercise(
            rrsm_event=rrsm_event,
            esm_event=esm_event,
            rrsm_provider=rrsm_provider,
            esm_provider=esm_provider,
            rrsm_records=[],
            esm_records=esm_records,
            merged_records=merged_records,
        )

        self._assert_mapping_merger(observed)
        observed["executable"].execute.assert_called_once_with(
            event_data=observed["manager"].event_context,
            amplitudes=merged_records,
            augmented_event_id="handoff-event_t00000",
        )
        errors = self._messages(observed["manager"].logger.error)
        self.assertFalse(any(
            "ESM" in message
            and "zero usable normalized observations" in message
            for message in errors
        ), errors)
        self.assertTrue(any(
            "RRSM" in message
            and "zero usable normalized observations" in message
            for message in errors
        ), errors)

    def test_absent_rrsm_records_invalid_outcome_and_esm_reaches_finder(self):
        esm_event = _EventModel("esm")
        _rrsm_provider, esm_provider = self._provider_models()
        esm_records = [{"source": "ESM"}]
        merged_records = list(esm_records)

        observed = self._exercise(
            rrsm_event=None,
            esm_event=esm_event,
            rrsm_provider=None,
            esm_provider=esm_provider,
            rrsm_records=[],
            esm_records=esm_records,
            merged_records=merged_records,
        )

        observed["rrsm_extract"].assert_not_called()
        self._assert_mapping_merger(observed)
        observed["executable"].execute.assert_called_once_with(
            event_data=observed["manager"].event_context,
            amplitudes=merged_records,
            augmented_event_id="handoff-event_t00000",
        )
        self.assertEqual(
            observed["manager"].metadata["provider_outcomes"][
                "RRSM_PeakMotion"
            ]["failure_kind"],
            "invalid-result",
        )

    def test_truthy_provider_models_cannot_replace_empty_normalized_lists(self):
        rrsm_event = _EventModel("rrsm")
        esm_event = _EventModel("esm")
        rrsm_provider, esm_provider = self._provider_models()

        self.assertTrue(rrsm_provider)
        self.assertTrue(esm_provider)

        observed = self._exercise(
            rrsm_event=rrsm_event,
            esm_event=esm_event,
            rrsm_provider=rrsm_provider,
            esm_provider=esm_provider,
            rrsm_records=[],
            esm_records=[],
            merged_records=[],
            expect_execution=False,
        )

        self.assertIsNone(observed["result"])
        observed["handoff"].assert_not_called()
        observed["merger_type"].assert_not_called()
        observed["executable_type"].assert_not_called()
        observed["executable"].execute.assert_not_called()
        observed["esm_direct_format"].assert_not_called()
        observed["rrsm_direct_format"].assert_not_called()
        observed["artificial_point_format"].assert_not_called()
        outcomes = observed["manager"].metadata["provider_outcomes"]
        self.assertEqual(
            set(outcomes),
            {ESM_SHAKEMAP_SERVICE, RRSM_PEAK_MOTION_SERVICE},
        )
        self.assertTrue(all(
            outcome["normalized_count"] == 0
            for outcome in outcomes.values()
        ))

        errors = self._messages(observed["manager"].logger.error)
        self.assertTrue(any(
            "ESM" in message
            and "zero usable normalized observations" in message
            for message in errors
        ), errors)
        self.assertTrue(any(
            "RRSM" in message
            and "zero usable normalized observations" in message
            for message in errors
        ), errors)
        self.assertTrue(any(
            "All normalized observation sources" in message
            and "zero usable records" in message
            for message in errors
        ), errors)

    def test_absent_esm_result_is_empty_with_invalid_result_outcome(self):
        rrsm_event = _EventModel("rrsm")
        rrsm_provider = Mock(name="rrsm_provider")
        rrsm_provider.get_event_data.return_value = rrsm_event
        rrsm_records = [{"source": "RRSM"}]
        merged_records = list(rrsm_records)

        observed = self._exercise(
            rrsm_event=rrsm_event,
            esm_event=None,
            rrsm_provider=rrsm_provider,
            esm_provider=None,
            rrsm_records=rrsm_records,
            esm_records=[],
            merged_records=merged_records,
        )

        observed["esm_extract"].assert_not_called()
        self._assert_mapping_merger(observed)
        observed["executable"].execute.assert_called_once_with(
            event_data=observed["manager"].event_context,
            amplitudes=merged_records,
            augmented_event_id="handoff-event_t00000",
        )
        self.assertEqual(
            observed["manager"].metadata["provider_outcomes"]["ESM_ShakeMap"][
                "failure_kind"
            ],
            "invalid-result",
        )

    def test_disabled_service_is_absent_and_attempted_empty_service_is_kept(self):
        _rrsm_provider, esm_provider = self._provider_models()
        esm_records = [{"source": "ESM"}]

        observed = self._exercise(
            rrsm_event=None,
            esm_event=_EventModel("esm"),
            rrsm_provider=None,
            esm_provider=esm_provider,
            rrsm_records=[],
            esm_records=esm_records,
            merged_records=list(esm_records),
            enabled=[ESM_SHAKEMAP_SERVICE],
        )

        mapping, _priority = self._assert_mapping_merger(observed)
        self.assertEqual(list(mapping), [ESM_SHAKEMAP_SERVICE])
        self.assertIs(mapping[ESM_SHAKEMAP_SERVICE], esm_records)
        self.assertNotIn(RRSM_PEAK_MOTION_SERVICE, mapping)

        observed = self._exercise(
            rrsm_event=_EventModel("rrsm"),
            esm_event=_EventModel("esm"),
            rrsm_provider=None,
            esm_provider=esm_provider,
            rrsm_records=[],
            esm_records=esm_records,
            merged_records=list(esm_records),
        )

        mapping, _priority = self._assert_mapping_merger(observed)
        self.assertIn(RRSM_PEAK_MOTION_SERVICE, mapping)
        self.assertIs(mapping[RRSM_PEAK_MOTION_SERVICE],
                      observed["manager"].available_results[
                          RRSM_PEAK_MOTION_SERVICE])
        self.assertEqual(mapping[RRSM_PEAK_MOTION_SERVICE], [])

    def test_usable_non_200_provider_result_reaches_downstream_execution(self):
        rrsm_provider, _esm_provider = self._provider_models()
        rrsm_records = [{"source": "RRSM"}]

        observed = self._exercise(
            rrsm_event=_EventModel("rrsm"),
            esm_event=None,
            rrsm_provider=rrsm_provider,
            esm_provider=None,
            rrsm_records=rrsm_records,
            esm_records=[],
            merged_records=list(rrsm_records),
            rrsm_code=503,
        )

        self._assert_mapping_merger(observed)
        observed["executable"].execute.assert_called_once()
        outcome = observed["manager"].metadata["provider_outcomes"][
            RRSM_PEAK_MOTION_SERVICE
        ]
        self.assertEqual(outcome["status_code"], 503)
        self.assertEqual(outcome["normalized_count"], 1)
        self.assertIsNone(outcome["failure_kind"])

    def test_merger_receives_exact_full_mapping_and_resolved_priority(self):
        manager = self._manager()
        esm_records = [{"source": "ESM"}]
        rrsm_records = [{"source": "RRSM"}]
        felt_records = [{"source": "EMSC"}]
        available_results = {
            ESM_SHAKEMAP_SERVICE: esm_records,
            RRSM_PEAK_MOTION_SERVICE: rrsm_records,
            EMSC_FELT_REPORT_SERVICE: felt_records,
        }

        service_priority = [
            RRSM_PEAK_MOTION_SERVICE,
            ESM_SHAKEMAP_SERVICE,
            EMSC_FELT_REPORT_SERVICE,
        ]
        with patch.object(findermanager, "StationMerger") as merger_type:
            merged = object()
            merger_type.return_value.merge.return_value = merged

            result = manager._merge_available_results(
                available_results,
                service_priority,
            )

        self.assertIs(result, merged)
        merger_type.assert_called_once_with(
            service_priority=service_priority,
            logger=manager.logger,
        )
        merger_type.return_value.merge.assert_called_once_with(available_results)
        self.assertIs(
            merger_type.return_value.merge.call_args.args[0],
            available_results,
        )

    def test_alert_and_on_demand_entries_use_the_same_mapping_handoff(self):
        for source_kind, entry_kind in (
            ("continuous", findermanager.FinDerManager.ALERT_BACKED),
            ("playback", findermanager.FinDerManager.ALERT_BACKED),
            ("on-demand", findermanager.FinDerManager.ON_DEMAND),
        ):
            with self.subTest(source_kind=source_kind):
                rrsm_provider, esm_provider = self._provider_models()
                rrsm_records = [{"source": "RRSM"}]
                esm_records = [{"source": "ESM"}]
                persisted_context = None
                if entry_kind == findermanager.FinDerManager.ALERT_BACKED:
                    persisted_context = EventContext.from_provider_model(
                        _EventModel("persisted"),
                        requested_event_id=self.event_id,
                    )

                observed = self._exercise(
                    rrsm_event=_EventModel("rrsm"),
                    esm_event=_EventModel("esm"),
                    rrsm_provider=rrsm_provider,
                    esm_provider=esm_provider,
                    rrsm_records=rrsm_records,
                    esm_records=esm_records,
                    merged_records=esm_records + rrsm_records,
                    entry_kind=entry_kind,
                    event_context=persisted_context,
                )

                mapping, _priority = self._assert_mapping_merger(observed)
                self.assertEqual(
                    observed["priority_resolver"].call_count,
                    1,
                )
                self.assertIs(mapping, observed["manager"].available_results)
                self.assertIs(mapping[ESM_SHAKEMAP_SERVICE], esm_records)
                self.assertIs(mapping[RRSM_PEAK_MOTION_SERVICE], rrsm_records)
                observed["executable"].execute.assert_called_once_with(
                    event_data=observed["manager"].event_context,
                    amplitudes=esm_records + rrsm_records,
                    augmented_event_id="handoff-event_t00000",
                )
                if persisted_context is not None:
                    self.assertIs(
                        observed["manager"].event_context,
                        persisted_context,
                    )


if __name__ == "__main__":
    unittest.main()
