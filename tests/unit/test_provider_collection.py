"""Offline tests for configured independent observation collection."""

import atexit
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


_PARAMWS_LOG_DIRECTORY = tempfile.TemporaryDirectory(
    prefix="pyfinder-provider-collection-unit-"
)
atexit.register(_PARAMWS_LOG_DIRECTORY.cleanup)
_original_paramws_log_file = os.environ.get("PARAMWS_LOG_FILE")
os.environ["PARAMWS_LOG_FILE"] = str(
    Path(_PARAMWS_LOG_DIRECTORY.name) / "paramws.log"
)
try:
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


EVENT_ID = "provider-event"


class ProviderEvent:
    """Small complete public provider event model."""

    def __init__(self, *, event_id=EVENT_ID, latitude=46.2, longitude=7.3,
                 origin_time="2026-08-10T08:15:30.250000Z"):
        self.event_id = event_id
        self.latitude = latitude
        self.longitude = longitude
        self.origin_time = origin_time

    def get_event_id(self):
        return self.event_id

    def get_latitude(self):
        return self.latitude

    def get_longitude(self):
        return self.longitude

    def get_magnitude(self):
        return 5.6

    def get_depth(self):
        return 10.0

    def get_origin_time(self):
        return self.origin_time

    def get_magnitude_type(self):
        return "Mw"


class ProviderCollectionTests(unittest.TestCase):
    def manager(self, *, enabled=None, priority=None, entry_kind=None,
                event_context=None):
        manager = object.__new__(findermanager.FinDerManager)
        manager.options = {"use_library": False}
        manager.configuration = {
            "general": {
                "services-enabled": (
                    [ESM_SHAKEMAP_SERVICE, RRSM_PEAK_MOTION_SERVICE]
                    if enabled is None else list(enabled)
                ),
                "services-priority": (
                    [
                        ESM_SHAKEMAP_SERVICE,
                        RRSM_PEAK_MOTION_SERVICE,
                        EMSC_FELT_REPORT_SERVICE,
                    ]
                    if priority is None else list(priority)
                ),
                "component-selection": "maximum-all",
            }
        }
        manager.finder_configuration_name = "offline"
        manager.finder_configuration = {"DATA_FOLDER": "offline"}
        manager.metadata = {}
        manager.logger = mock.Mock()
        manager.entry_kind = (
            findermanager.FinDerManager.ON_DEMAND
            if entry_kind is None else entry_kind
        )
        manager.event_context = event_context
        manager.context_diagnostic = None
        return manager

    @staticmethod
    def usable_result(service_name, *, code=200, event=None, value=None):
        event = ProviderEvent() if event is None else event
        value = object() if value is None else value
        if service_name == ESM_SHAKEMAP_SERVICE:
            return code, event, {"station_amplitudes": value}
        return code, event, {"peak_motion": value}

    def test_dispatch_uses_unique_enabled_membership_and_skips_unsupported(self):
        manager = self.manager(enabled=[
            RRSM_PEAK_MOTION_SERVICE,
            "unsupported",
            ESM_SHAKEMAP_SERVICE,
            RRSM_PEAK_MOTION_SERVICE,
        ])
        with mock.patch.object(
            findermanager, "RRSMPeakMotionClient"
        ) as rrsm_type, mock.patch.object(
            findermanager, "ESMShakeMapClient"
        ) as esm_type, mock.patch.object(
            findermanager, "EMSCFeltReportClient"
        ) as felt_type:
            rrsm_type.return_value.query.return_value = self.usable_result(
                RRSM_PEAK_MOTION_SERVICE
            )
            esm_type.return_value.query.return_value = self.usable_result(
                ESM_SHAKEMAP_SERVICE
            )

            acquired = manager._acquire_enabled_providers(EVENT_ID)

        self.assertEqual(
            list(acquired),
            [RRSM_PEAK_MOTION_SERVICE, ESM_SHAKEMAP_SERVICE],
        )
        rrsm_type.assert_called_once_with()
        esm_type.assert_called_once_with()
        rrsm_type.return_value.query.assert_called_once_with(event_id=EVENT_ID)
        esm_type.return_value.query.assert_called_once_with(event_id=EVENT_ID)
        felt_type.assert_not_called()
        manager.logger.critical.assert_called_once()

    def test_priority_changes_selection_without_changing_queried_membership(self):
        manager = self.manager(
            enabled=[RRSM_PEAK_MOTION_SERVICE, ESM_SHAKEMAP_SERVICE],
            priority=[ESM_SHAKEMAP_SERVICE, RRSM_PEAK_MOTION_SERVICE],
        )
        rrsm_context = EventContext.from_provider_model(
            ProviderEvent(latitude=40.0), requested_event_id=EVENT_ID
        )
        esm_context = EventContext.from_provider_model(
            ProviderEvent(latitude=50.0), requested_event_id=EVENT_ID
        )
        acquired = {
            RRSM_PEAK_MOTION_SERVICE: {"event_context": rrsm_context},
            ESM_SHAKEMAP_SERVICE: {"event_context": esm_context},
        }

        selected = manager._select_on_demand_context(
            acquired,
            manager.configuration["general"]["services-priority"],
        )

        self.assertIs(selected, esm_context)
        self.assertEqual(
            manager._configured_enabled_services(),
            [RRSM_PEAK_MOTION_SERVICE, ESM_SHAKEMAP_SERVICE],
        )

    def test_explicit_felt_uses_only_single_event_view_once(self):
        manager = self.manager(enabled=[EMSC_FELT_REPORT_SERVICE])
        multi_event_dataset = object()
        single_event_view = object()
        with mock.patch.object(
            findermanager, "EMSCFeltReportClient"
        ) as felt_type, mock.patch.object(
            findermanager, "ESMShakeMapClient"
        ) as esm_type, mock.patch.object(
            findermanager, "RRSMPeakMotionClient"
        ) as rrsm_type, mock.patch.object(
            manager,
            "_normalize_provider",
            return_value=[{"source": "EMSC"}],
        ) as normalize:
            client = felt_type.return_value
            client.query.return_value = (
                200,
                ProviderEvent(),
                {"felt_intensities": multi_event_dataset},
            )
            client.get_feltreports.return_value = single_event_view

            acquired = manager._acquire_enabled_providers(EVENT_ID)
            context = acquired[EMSC_FELT_REPORT_SERVICE]["event_context"]
            available = manager._normalize_acquired_providers(
                acquired,
                context,
                EVENT_ID,
            )
            manager._store_provider_outcomes(acquired)

        client.query.assert_called_once_with(event_id=EVENT_ID)
        client.get_feltreports.assert_called_once_with()
        esm_type.assert_not_called()
        rrsm_type.assert_not_called()
        self.assertIs(
            acquired[EMSC_FELT_REPORT_SERVICE]["scientific_value"],
            single_event_view,
        )
        self.assertIsNot(
            acquired[EMSC_FELT_REPORT_SERVICE]["scientific_value"],
            multi_event_dataset,
        )
        normalize.assert_called_once_with(
            EMSC_FELT_REPORT_SERVICE,
            context,
            single_event_view,
        )
        self.assertEqual(
            available,
            {EMSC_FELT_REPORT_SERVICE: [{"source": "EMSC"}]},
        )
        outcome = manager.metadata["provider_outcomes"][
            EMSC_FELT_REPORT_SERVICE
        ]
        self.assertEqual(
            set(outcome),
            {
                "status_code",
                "normalized_count",
                "failure_kind",
                "diagnostic",
                "event_context_usable",
                "context_diagnostic",
            },
        )
        self.assertEqual(outcome["normalized_count"], 1)

    def test_felt_only_records_reach_merger_and_downstream_boundary(self):
        manager = self.manager(enabled=[EMSC_FELT_REPORT_SERVICE])
        felt_provider = object()
        felt_records = [{"source": "EMSC"}]
        with mock.patch.object(
            findermanager, "EMSCFeltReportClient"
        ) as felt_type, mock.patch.object(
            manager,
            "_normalize_provider",
            return_value=felt_records,
        ), mock.patch.object(
            manager,
            "_merge_available_results",
            wraps=manager._merge_available_results,
        ) as handoff, mock.patch.object(
            findermanager, "StationMerger"
        ) as merger_type, mock.patch.object(
            finderexec, "FinDerExecutable"
        ) as executable_type:
            felt_type.return_value.query.return_value = (
                200,
                ProviderEvent(),
                {"felt_intensities": object()},
            )
            felt_type.return_value.get_feltreports.return_value = felt_provider
            merger_type.return_value.merge.return_value = felt_records
            executable_type.return_value.execute.side_effect = RuntimeError(
                "downstream boundary reached"
            )

            with self.assertRaisesRegex(
                RuntimeError, "downstream boundary reached"
            ):
                manager.process_event(EVENT_ID)

        self.assertEqual(
            manager.available_results,
            {EMSC_FELT_REPORT_SERVICE: felt_records},
        )
        self.assertIs(
            manager.available_results[EMSC_FELT_REPORT_SERVICE],
            felt_records,
        )
        outcome = manager.metadata["provider_outcomes"][
            EMSC_FELT_REPORT_SERVICE
        ]
        self.assertEqual(outcome["normalized_count"], 1)
        self.assertIsNone(outcome["failure_kind"])
        mapping, priority = handoff.call_args.args
        self.assertIs(mapping, manager.available_results)
        merger_type.assert_called_once_with(
            service_priority=priority,
            logger=manager.logger,
        )
        merger_type.return_value.merge.assert_called_once_with(mapping)
        executable_type.return_value.execute.assert_called_once_with(
            event_data=manager.event_context,
            amplitudes=felt_records,
            augmented_event_id="provider-event_t00000",
        )

    def test_instrumental_and_felt_mapping_reaches_handoff_unchanged(self):
        manager = self.manager(enabled=[
            ESM_SHAKEMAP_SERVICE,
            EMSC_FELT_REPORT_SERVICE,
        ])
        esm_provider = object()
        felt_provider = object()
        esm_records = [{"source": "ESM"}]
        felt_records = [{"source": "EMSC"}]
        merged_records = esm_records + felt_records

        def normalize(service_name, event_context, value):
            if service_name == ESM_SHAKEMAP_SERVICE:
                self.assertIs(value, esm_provider)
                return esm_records
            self.assertIs(value, felt_provider)
            return felt_records

        with mock.patch.object(
            findermanager, "ESMShakeMapClient"
        ) as esm_type, mock.patch.object(
            findermanager, "EMSCFeltReportClient"
        ) as felt_type, mock.patch.object(
            manager,
            "_normalize_provider",
            side_effect=normalize,
        ), mock.patch.object(
            findermanager, "StationMerger"
        ) as merger_type, mock.patch.object(
            manager,
            "_merge_available_results",
            wraps=manager._merge_available_results,
        ) as handoff, mock.patch.object(
            finderexec, "FinDerExecutable"
        ) as executable_type:
            esm_type.return_value.query.return_value = (
                200,
                ProviderEvent(),
                {"station_amplitudes": esm_provider},
            )
            felt_type.return_value.query.return_value = (
                200,
                ProviderEvent(),
                {"felt_intensities": object()},
            )
            felt_type.return_value.get_feltreports.return_value = felt_provider
            merger_type.return_value.merge.return_value = merged_records
            executable_type.return_value.execute.side_effect = RuntimeError(
                "downstream boundary reached"
            )

            with self.assertRaisesRegex(
                RuntimeError, "downstream boundary reached"
            ):
                manager.process_event(EVENT_ID)

        mapping, priority = handoff.call_args.args
        self.assertIs(mapping, manager.available_results)
        self.assertEqual(
            list(mapping),
            [ESM_SHAKEMAP_SERVICE, EMSC_FELT_REPORT_SERVICE],
        )
        self.assertIs(mapping[ESM_SHAKEMAP_SERVICE], esm_records)
        self.assertIs(mapping[EMSC_FELT_REPORT_SERVICE], felt_records)
        merger_type.assert_called_once_with(
            service_priority=priority,
            logger=manager.logger,
        )
        merger_type.return_value.merge.assert_called_once_with(mapping)
        executable_type.return_value.execute.assert_called_once_with(
            event_data=manager.event_context,
            amplitudes=merged_records,
            augmented_event_id="provider-event_t00000",
        )
        self.assertEqual(
            set(manager.metadata["provider_outcomes"]),
            {ESM_SHAKEMAP_SERVICE, EMSC_FELT_REPORT_SERVICE},
        )

    def test_query_exception_is_contained_and_later_provider_is_attempted(self):
        manager = self.manager(enabled=[
            RRSM_PEAK_MOTION_SERVICE,
            ESM_SHAKEMAP_SERVICE,
        ])
        with mock.patch.object(
            findermanager, "RRSMPeakMotionClient"
        ) as rrsm_type, mock.patch.object(
            findermanager, "ESMShakeMapClient"
        ) as esm_type:
            rrsm_type.return_value.query.side_effect = ConnectionError(
                "transport exhausted"
            )
            esm_type.return_value.query.return_value = self.usable_result(
                ESM_SHAKEMAP_SERVICE
            )

            acquired = manager._acquire_enabled_providers(EVENT_ID)

        rrsm_type.return_value.query.assert_called_once_with(event_id=EVENT_ID)
        esm_type.return_value.query.assert_called_once_with(event_id=EVENT_ID)
        self.assertEqual(
            acquired[RRSM_PEAK_MOTION_SERVICE]["outcome"]["failure_kind"],
            "exception",
        )
        self.assertIsNotNone(
            acquired[ESM_SHAKEMAP_SERVICE]["scientific_value"]
        )

    def test_non_200_usable_data_normalizes_and_retains_aggregate_status(self):
        manager = self.manager(enabled=[ESM_SHAKEMAP_SERVICE])
        with mock.patch.object(
            findermanager, "ESMShakeMapClient"
        ) as esm_type, mock.patch.object(
            manager,
            "_normalize_provider",
            return_value=[{"source": "ESM"}],
        ):
            esm_type.return_value.query.return_value = self.usable_result(
                ESM_SHAKEMAP_SERVICE,
                code=503,
            )
            acquired = manager._acquire_enabled_providers(EVENT_ID)
            context = acquired[ESM_SHAKEMAP_SERVICE]["event_context"]
            available = manager._normalize_acquired_providers(
                acquired, context, EVENT_ID
            )

        outcome = acquired[ESM_SHAKEMAP_SERVICE]["outcome"]
        self.assertEqual(available[ESM_SHAKEMAP_SERVICE], [{"source": "ESM"}])
        self.assertEqual(outcome["status_code"], 503)
        self.assertEqual(outcome["normalized_count"], 1)
        self.assertIsNone(outcome["failure_kind"])
        self.assertTrue(outcome["event_context_usable"])

    def test_malformed_observation_results_preserve_independent_context(self):
        malformed = {
            "non-mapping": (200, ProviderEvent(), None),
            "missing-key": (200, ProviderEvent(), {}),
            "missing-view": (200, ProviderEvent(), {"station_amplitudes": None}),
        }
        for label, query_result in malformed.items():
            with self.subTest(label=label):
                manager = self.manager(enabled=[ESM_SHAKEMAP_SERVICE])
                with mock.patch.object(
                    findermanager, "ESMShakeMapClient"
                ) as esm_type:
                    esm_type.return_value.query.return_value = query_result
                    acquired = manager._acquire_enabled_providers(EVENT_ID)

                provider = acquired[ESM_SHAKEMAP_SERVICE]
                self.assertIsNotNone(provider["event_context"])
                self.assertIsNone(provider["scientific_value"])
                self.assertEqual(
                    provider["outcome"]["failure_kind"],
                    "invalid-result",
                )
                self.assertTrue(
                    provider["outcome"]["event_context_usable"]
                )

    def test_empty_dataset_keeps_context_and_becomes_zero_result(self):
        manager = self.manager(enabled=[ESM_SHAKEMAP_SERVICE])
        with mock.patch.object(
            findermanager, "ESMShakeMapClient"
        ) as esm_type, mock.patch.object(
            manager, "_normalize_provider", return_value=[]
        ):
            esm_type.return_value.query.return_value = (
                200,
                ProviderEvent(),
                {"station_amplitudes": []},
            )
            acquired = manager._acquire_enabled_providers(EVENT_ID)
            provider = acquired[ESM_SHAKEMAP_SERVICE]
            available = manager._normalize_acquired_providers(
                acquired,
                provider["event_context"],
                EVENT_ID,
            )

        self.assertIsNotNone(provider["event_context"])
        self.assertIs(provider["scientific_value"],
                      esm_type.return_value.query.return_value[2][
                          "station_amplitudes"
                      ])
        self.assertEqual(available, {ESM_SHAKEMAP_SERVICE: []})
        self.assertIsNone(provider["outcome"]["failure_kind"])
        self.assertEqual(provider["outcome"]["normalized_count"], 0)

    def test_malformed_tuple_and_missing_felt_view_have_visible_outcomes(self):
        cases = (
            (ESM_SHAKEMAP_SERVICE, (200, ProviderEvent())),
            (EMSC_FELT_REPORT_SERVICE,
             (200, ProviderEvent(), {"felt_intensities": object()})),
        )
        for service_name, query_result in cases:
            with self.subTest(service_name=service_name):
                manager = self.manager(enabled=[service_name])
                client_name = (
                    "ESMShakeMapClient"
                    if service_name == ESM_SHAKEMAP_SERVICE
                    else "EMSCFeltReportClient"
                )
                with mock.patch.object(
                    findermanager, client_name
                ) as client_type:
                    client_type.return_value.query.return_value = query_result
                    if service_name == EMSC_FELT_REPORT_SERVICE:
                        client_type.return_value.get_feltreports.return_value = None
                    acquired = manager._acquire_enabled_providers(EVENT_ID)

                outcome = acquired[service_name]["outcome"]
                self.assertEqual(outcome["failure_kind"], "invalid-result")
                self.assertEqual(outcome["normalized_count"], 0)

    def test_dependency_model_failure_does_not_discard_another_provider(self):
        manager = self.manager()
        malformed_esm_model = mock.Mock()
        malformed_esm_model.get_stations.side_effect = AttributeError(
            "malformed ESM station collection"
        )
        acquired = {
            ESM_SHAKEMAP_SERVICE: {
                "scientific_value": malformed_esm_model,
                "outcome": manager._new_provider_outcome(),
            },
            RRSM_PEAK_MOTION_SERVICE: {
                "scientific_value": object(),
                "outcome": manager._new_provider_outcome(),
            },
        }
        context = EventContext.from_provider_model(
            ProviderEvent(), requested_event_id=EVENT_ID
        )
        with mock.patch.object(
            findermanager.RRSMPeakMotionDataFormatter,
            "extract_raw_stations",
            return_value=["rrsm"],
        ):
            available = manager._normalize_acquired_providers(
                acquired, context, EVENT_ID
            )

        self.assertEqual(available[ESM_SHAKEMAP_SERVICE], [])
        self.assertEqual(available[RRSM_PEAK_MOTION_SERVICE], ["rrsm"])
        self.assertEqual(
            acquired[ESM_SHAKEMAP_SERVICE]["outcome"]["failure_kind"],
            "exception",
        )

    def test_zero_result_is_empty_without_becoming_provider_failure(self):
        manager = self.manager(enabled=[ESM_SHAKEMAP_SERVICE])
        context = EventContext.from_provider_model(
            ProviderEvent(), requested_event_id=EVENT_ID
        )
        acquired = {
            ESM_SHAKEMAP_SERVICE: {
                "scientific_value": object(),
                "outcome": manager._new_provider_outcome(),
            }
        }
        with mock.patch.object(manager, "_normalize_provider", return_value=[]):
            available = manager._normalize_acquired_providers(
                acquired, context, EVENT_ID
            )

        self.assertEqual(available, {ESM_SHAKEMAP_SERVICE: []})
        self.assertEqual(
            acquired[ESM_SHAKEMAP_SERVICE]["outcome"]["normalized_count"],
            0,
        )
        self.assertIsNone(
            acquired[ESM_SHAKEMAP_SERVICE]["outcome"]["failure_kind"]
        )
        manager.logger.error.assert_called()

    def test_project_owned_type_error_from_normalization_propagates(self):
        manager = self.manager(enabled=[ESM_SHAKEMAP_SERVICE])
        context = EventContext.from_provider_model(
            ProviderEvent(), requested_event_id=EVENT_ID
        )
        outcome = manager._new_provider_outcome()
        acquired = {
            ESM_SHAKEMAP_SERVICE: {
                "scientific_value": object(),
                "outcome": outcome,
            }
        }
        defect = TypeError("project-owned defect")

        with mock.patch.object(
            manager,
            "_normalize_provider",
            side_effect=defect,
        ):
            with self.assertRaises(TypeError) as raised:
                manager._normalize_acquired_providers(
                    acquired, context, EVENT_ID
                )

        self.assertIs(raised.exception, defect)
        self.assertIsNone(outcome["failure_kind"])

    def test_provider_event_accessor_failure_is_contained_as_bad_context(self):
        manager = self.manager(enabled=[ESM_SHAKEMAP_SERVICE])
        event_candidate = ProviderEvent()
        event_candidate.get_latitude = mock.Mock(
            side_effect=TypeError("malformed dependency accessor")
        )
        scientific_value = object()
        with mock.patch.object(
            findermanager,
            "ESMShakeMapClient",
        ) as esm_type:
            esm_type.return_value.query.return_value = (
                200,
                event_candidate,
                {"station_amplitudes": scientific_value},
            )
            acquired = manager._acquire_enabled_providers(EVENT_ID)

        provider = acquired[ESM_SHAKEMAP_SERVICE]
        self.assertIsNone(provider["event_context"])
        self.assertIs(provider["scientific_value"], scientific_value)
        self.assertFalse(provider["outcome"]["event_context_usable"])
        self.assertIsNotNone(provider["outcome"]["context_diagnostic"])
        self.assertIsNone(provider["outcome"]["failure_kind"])

    def test_project_owned_event_context_type_error_propagates(self):
        manager = self.manager(enabled=[ESM_SHAKEMAP_SERVICE])
        defect = TypeError("project-owned context defect")
        with mock.patch.object(
            findermanager,
            "ESMShakeMapClient",
        ) as esm_type, mock.patch.object(
            EventContext,
            "_from_values",
            side_effect=defect,
        ):
            esm_type.return_value.query.return_value = (
                200,
                ProviderEvent(),
                {"station_amplitudes": object()},
            )
            with self.assertRaises(TypeError) as raised:
                manager._acquire_enabled_providers(EVENT_ID)

        self.assertIs(raised.exception, defect)

    def test_alert_context_controls_all_normalizers_and_metadata(self):
        alert_context = EventContext.from_provider_model(
            ProviderEvent(latitude=42.0, longitude=13.0),
            requested_event_id=EVENT_ID,
        )
        manager = self.manager(
            entry_kind=findermanager.FinDerManager.ALERT_BACKED,
            event_context=alert_context,
        )
        contradictory = ProviderEvent(latitude=-35.0, longitude=120.0)
        with mock.patch.object(
            findermanager, "ESMShakeMapClient"
        ) as esm_type, mock.patch.object(
            findermanager, "RRSMPeakMotionClient"
        ) as rrsm_type, mock.patch.object(
            manager,
            "_normalize_provider",
            side_effect=lambda name, context, value: [context.get_latitude()],
        ) as normalize, mock.patch.object(
            findermanager, "StationMerger"
        ) as merger_type:
            esm_type.return_value.query.return_value = self.usable_result(
                ESM_SHAKEMAP_SERVICE, event=contradictory
            )
            rrsm_type.return_value.query.return_value = self.usable_result(
                RRSM_PEAK_MOTION_SERVICE, event=contradictory
            )
            merger_type.return_value.merge.return_value = []

            self.assertIsNone(manager.process_event(EVENT_ID))

        self.assertEqual(normalize.call_count, 2)
        self.assertTrue(
            all(call.args[1] is alert_context for call in normalize.call_args_list)
        )
        self.assertEqual(manager.metadata["latitude"], 42.0)
        self.assertIs(manager.event_context, alert_context)

    def test_on_demand_uses_priority_context_with_lower_priority_observations(self):
        manager = self.manager(
            enabled=[RRSM_PEAK_MOTION_SERVICE, ESM_SHAKEMAP_SERVICE],
            priority=[ESM_SHAKEMAP_SERVICE, RRSM_PEAK_MOTION_SERVICE],
        )
        manager.finder_configuration_name = None
        manager.finder_configuration = None
        esm_event = ProviderEvent(latitude=47.0, longitude=8.0)
        rrsm_event = ProviderEvent(latitude=40.0, longitude=20.0)
        with mock.patch.object(
            findermanager, "ESMShakeMapClient"
        ) as esm_type, mock.patch.object(
            findermanager, "RRSMPeakMotionClient"
        ) as rrsm_type, mock.patch.object(
            manager,
            "_normalize_provider",
            return_value=[{"timestamp-source": "common"}],
        ) as normalize, mock.patch.object(
            manager, "_resolve_finder_configuration_from_context"
        ) as resolve_region, mock.patch.object(
            findermanager, "StationMerger"
        ) as merger_type:
            esm_type.return_value.query.return_value = (
                200, esm_event, {"station_amplitudes": None}
            )
            rrsm_type.return_value.query.return_value = self.usable_result(
                RRSM_PEAK_MOTION_SERVICE, event=rrsm_event
            )
            merger_type.return_value.merge.return_value = []

            self.assertIsNone(manager.process_event(EVENT_ID))

        self.assertEqual(manager.event_context.get_latitude(), 47.0)
        normalize.assert_called_once()
        self.assertIs(normalize.call_args.args[1], manager.event_context)
        resolve_region.assert_called_once_with(manager.event_context)
        self.assertEqual(
            manager.available_results[RRSM_PEAK_MOTION_SERVICE],
            [{"timestamp-source": "common"}],
        )
        self.assertEqual(
            manager.metadata["provider_outcomes"][ESM_SHAKEMAP_SERVICE][
                "failure_kind"
            ],
            "invalid-result",
        )

    def test_unusable_high_priority_context_falls_through_but_data_survives(self):
        manager = self.manager()
        esm_value = object()
        rrsm_value = object()
        with mock.patch.object(
            findermanager, "ESMShakeMapClient"
        ) as esm_type, mock.patch.object(
            findermanager, "RRSMPeakMotionClient"
        ) as rrsm_type, mock.patch.object(
            manager,
            "_normalize_provider",
            side_effect=[["esm"], ["rrsm"]],
        ), mock.patch.object(
            findermanager, "StationMerger"
        ) as merger_type:
            esm_type.return_value.query.return_value = self.usable_result(
                ESM_SHAKEMAP_SERVICE,
                event=ProviderEvent(event_id="wrong-event"),
                value=esm_value,
            )
            rrsm_type.return_value.query.return_value = self.usable_result(
                RRSM_PEAK_MOTION_SERVICE,
                value=rrsm_value,
            )
            merger_type.return_value.merge.return_value = []

            self.assertIsNone(manager.process_event(EVENT_ID))

        self.assertEqual(manager.event_context.get_event_id(), EVENT_ID)
        self.assertEqual(manager.available_results[ESM_SHAKEMAP_SERVICE], ["esm"])
        esm_outcome = manager.metadata["provider_outcomes"][
            ESM_SHAKEMAP_SERVICE
        ]
        self.assertFalse(esm_outcome["event_context_usable"])
        self.assertIsNotNone(esm_outcome["context_diagnostic"])

    def test_no_on_demand_context_stops_before_normalization_and_region(self):
        manager = self.manager()
        manager.finder_configuration_name = None
        manager.finder_configuration = None
        with mock.patch.object(
            findermanager, "ESMShakeMapClient"
        ) as esm_type, mock.patch.object(
            findermanager, "RRSMPeakMotionClient"
        ) as rrsm_type, mock.patch.object(
            manager, "_normalize_provider"
        ) as normalize, mock.patch.object(
            manager, "_resolve_finder_configuration_from_context"
        ) as resolve_region, mock.patch.object(
            findermanager, "StationMerger"
        ) as merger_type:
            esm_type.return_value.query.return_value = (
                200, None, {"station_amplitudes": object()}
            )
            rrsm_type.return_value.query.return_value = (
                200, None, {"peak_motion": object()}
            )

            self.assertIsNone(manager.process_event(EVENT_ID))

        normalize.assert_not_called()
        resolve_region.assert_not_called()
        merger_type.assert_not_called()
        self.assertEqual(
            manager.available_results,
            {ESM_SHAKEMAP_SERVICE: [], RRSM_PEAK_MOTION_SERVICE: []},
        )


if __name__ == "__main__":
    unittest.main()
