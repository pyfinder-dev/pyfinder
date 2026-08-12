"""Tests for Allen intensity prediction and EMSC felt-data normalization."""

import atexit
import inspect
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np


# Importing the formatter also imports ParamWS. Keep the dependency's import
# log outside the repository when this focused module runs on its own.
_PARAMWS_LOG_DIRECTORY = tempfile.TemporaryDirectory(
    prefix="pyfinder-felt-normalization-unit-")
atexit.register(_PARAMWS_LOG_DIRECTORY.cleanup)
_original_paramws_log_file = os.environ.get("PARAMWS_LOG_FILE")
os.environ["PARAMWS_LOG_FILE"] = str(
    Path(_PARAMWS_LOG_DIRECTORY.name) / "paramws.log")
try:
    from paramws.clients import FeltReportEventData, FeltReportIntensityData
    from pyfinder import findermanager
    from pyfinder.eventcontext import EventContext, ProviderModelAccessError
    from pyfinder.pyfinderconfig import EMSC_FELT_REPORT_SERVICE
    from pyfinder.utils.calculator import Calculator
    import pyfinder.utils.dataformatter.emsc_felt_report as emsc_felt_report
    from pyfinder.utils.dataformatter import EMSCFeltReportDataFormatter
finally:
    if _original_paramws_log_file is None:
        os.environ.pop("PARAMWS_LOG_FILE", None)
    else:
        os.environ["PARAMWS_LOG_FILE"] = _original_paramws_log_file


class AllenCalculatorTests(unittest.TestCase):
    """Freeze the legacy defect separately from corrected Allen prediction."""

    EVENT_MAGNITUDE = 5.5
    EVENT_DEPTH_KM = 10.0
    STATION_DISTANCES_KM = np.array([10.0, 100.0])
    EXPECTED_PREDICTIONS = np.array([
        6.190774436496003,
        3.529338055133846,
    ])
    EXPECTED_LEGACY_CUTOFFS = np.array([
        131.47418337028276,
        146.63788216423583,
    ])

    def test_legacy_tuple_and_representative_predictions_are_preserved(self):
        result = Calculator.I_Allen2012_Rhypo_legacy(
            self.EVENT_MAGNITUDE,
            self.EVENT_DEPTH_KM,
            self.STATION_DISTANCES_KM,
        )

        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)
        predictions, cutoffs = result
        np.testing.assert_allclose(
            predictions, self.EXPECTED_PREDICTIONS, rtol=1e-12, atol=0)
        np.testing.assert_allclose(
            cutoffs, self.EXPECTED_LEGACY_CUTOFFS, rtol=1e-12, atol=0)

    def test_legacy_cutoff_remains_station_dependent_for_one_event(self):
        _predictions, cutoffs = Calculator.I_Allen2012_Rhypo_legacy(
            self.EVENT_MAGNITUDE,
            self.EVENT_DEPTH_KM,
            self.STATION_DISTANCES_KM,
        )

        self.assertFalse(np.isclose(cutoffs[0], cutoffs[1]))

    def test_legacy_array_oriented_scalar_failure_is_preserved(self):
        with self.assertRaises(TypeError):
            Calculator.I_Allen2012_Rhypo_legacy(
                self.EVENT_MAGNITUDE,
                self.EVENT_DEPTH_KM,
                25.0,
            )

    def test_legacy_defect_is_independent_of_corrected_prediction(self):
        legacy_predictions, legacy_cutoffs = (
            Calculator.I_Allen2012_Rhypo_legacy(
                self.EVENT_MAGNITUDE,
                self.EVENT_DEPTH_KM,
                self.STATION_DISTANCES_KM,
            )
        )
        corrected_predictions = Calculator.I_Allen2012_Rhypo(
            self.EVENT_MAGNITUDE,
            self.EVENT_DEPTH_KM,
            self.STATION_DISTANCES_KM,
        )

        self.assertIsInstance(corrected_predictions, np.ndarray)
        self.assertNotIsInstance(corrected_predictions, tuple)
        np.testing.assert_allclose(
            corrected_predictions, legacy_predictions, rtol=1e-12, atol=0)
        self.assertFalse(np.isclose(legacy_cutoffs[0], legacy_cutoffs[1]))

    def test_legacy_docstring_documents_defects_and_production_prohibition(self):
        documentation = (
            Calculator.I_Allen2012_Rhypo_legacy.__doc__ or "").lower()

        required_text = (
            "historical",
            "own hypocentral distance",
            "station-dependent",
            "far-distance term",
            "epicentral distance",
            "hypocentral-distance equation",
            "production code must not use",
        )
        for text in required_text:
            with self.subTest(text=text):
                self.assertIn(text, documentation)

    def test_corrected_scalar_input_returns_one_prediction_only(self):
        prediction = Calculator.I_Allen2012_Rhypo(
            self.EVENT_MAGNITUDE,
            self.EVENT_DEPTH_KM,
            25.0,
        )

        self.assertIsInstance(prediction, float)
        self.assertNotIsInstance(prediction, tuple)
        self.assertAlmostEqual(prediction, 5.312518440267115, places=12)

    def test_corrected_signature_has_no_legacy_cutoff_parameter(self):
        parameters = inspect.signature(
            Calculator.I_Allen2012_Rhypo).parameters

        self.assertEqual(
            tuple(parameters),
            (
                "eq_mag",
                "eq_depth",
                "sta_dist",
                "c0",
                "c1",
                "c2",
                "c4",
                "m1",
                "m2",
            ),
        )
        self.assertNotIn("Imin", parameters)

    def test_corrected_array_input_returns_representative_predictions_only(self):
        predictions = Calculator.I_Allen2012_Rhypo(
            self.EVENT_MAGNITUDE,
            self.EVENT_DEPTH_KM,
            self.STATION_DISTANCES_KM,
        )

        self.assertIsInstance(predictions, np.ndarray)
        self.assertEqual(predictions.shape, self.STATION_DISTANCES_KM.shape)
        self.assertNotIsInstance(predictions, tuple)
        np.testing.assert_allclose(
            predictions, self.EXPECTED_PREDICTIONS, rtol=1e-12, atol=0)

    def test_anelastic_term_is_absent_at_exactly_fifty_km(self):
        prediction = Calculator.I_Allen2012_Rhypo(
            self.EVENT_MAGNITUDE,
            0.0,
            50.0,
            c4=float("nan"),
        )
        RM = -0.209 + 2.042 * np.exp(self.EVENT_MAGNITUDE - 5)
        expected_without_anelastic_term = (
            2.085
            + 1.428 * self.EVENT_MAGNITUDE
            - 1.402 * np.log(np.sqrt(50.0 ** 2 + RM ** 2))
        )

        self.assertTrue(np.isfinite(prediction))
        self.assertAlmostEqual(
            prediction, expected_without_anelastic_term, places=12)

    def test_anelastic_term_is_applied_above_fifty_km(self):
        station_distance = 100.0
        prediction = Calculator.I_Allen2012_Rhypo(
            self.EVENT_MAGNITUDE,
            0.0,
            station_distance,
        )
        RM = -0.209 + 2.042 * np.exp(self.EVENT_MAGNITUDE - 5)
        prediction_without_anelastic_term = (
            2.085
            + 1.428 * self.EVENT_MAGNITUDE
            - 1.402
            * np.log(np.sqrt(station_distance ** 2 + RM ** 2))
        )
        expected_anelastic_term = 0.078 * np.log(station_distance / 50)

        self.assertAlmostEqual(
            prediction,
            prediction_without_anelastic_term + expected_anelastic_term,
            places=12,
        )
        self.assertAlmostEqual(
            prediction - prediction_without_anelastic_term,
            expected_anelastic_term,
            places=12,
        )

    def test_worden_lower_branch_retains_beta1_1_557(self):
        log10_pga = Calculator.I_to_PGA_Wordon2012(3.0)

        self.assertAlmostEqual(float(log10_pga), 0.783558124598587, places=12)
        self.assertNotAlmostEqual(
            float(log10_pga), (3.0 - 1.78) / 1.55, places=6)


class FeltReportStationCodeTests(unittest.TestCase):
    """Verify the accepted finite FeltReport identity namespace by index."""

    def test_first_last_and_exact_namespace_exhaustion(self):
        self.assertEqual(emsc_felt_report._felt_report_station_code(0), "A001")
        self.assertEqual(
            emsc_felt_report._felt_report_station_code(1_212_353),
            "9ZZZ",
        )
        self.assertEqual(
            emsc_felt_report._FELT_REPORT_STATION_CODE_COUNT,
            1_212_354,
        )

        with self.assertRaisesRegex(ValueError, "namespace exhausted"):
            emsc_felt_report._felt_report_station_code(1_212_354)

    def test_every_pattern_transition_uses_the_accepted_sequence(self):
        transitions = (
            (25_973, "Z999", "0A01"),
            (51_713, "9Z99", "00A0"),
            (77_713, "99Z9", "000A"),
            (103_713, "999Z", "AA00"),
            (171_313, "ZZ99", "A0A0"),
            (238_913, "Z9Z9", "A00A"),
            (306_513, "Z99Z", "0AA0"),
            (374_113, "9ZZ9", "0A0A"),
            (441_713, "9Z9Z", "00AA"),
            (509_313, "99ZZ", "AAA0"),
            (685_073, "ZZZ9", "AA0A"),
            (860_833, "ZZ9Z", "A0AA"),
            (1_036_593, "Z9ZZ", "0AAA"),
        )
        for last_index, expected_last, expected_next in transitions:
            with self.subTest(last_index=last_index):
                self.assertEqual(
                    emsc_felt_report._felt_report_station_code(last_index),
                    expected_last,
                )
                self.assertEqual(
                    emsc_felt_report._felt_report_station_code(last_index + 1),
                    expected_next,
                )

    def test_representative_one_two_and_three_letter_codes(self):
        expected_codes = (
            (998, "A999"),
            (999, "B001"),
            (26_072, "0A99"),
            (26_073, "0B01"),
            (51_724, "00B0"),
            (77_740, "001A"),
            (103_837, "AB23"),
            (172_118, "A3C4"),
            (509_601, "ABC7"),
        )
        for index, expected_code in expected_codes:
            with self.subTest(index=index):
                self.assertEqual(
                    emsc_felt_report._felt_report_station_code(index),
                    expected_code,
                )


_MISSING = object()


def _event(*, event_id="event-one", latitude=45.0, longitude=10.0,
           magnitude=5.5, depth=10.0,
           event_time="2026-08-09T12:00:00Z"):
    """Build one public EMSC event while allowing genuinely absent fields."""
    data = {}
    provider_fields = (
        ("ev_id", event_id),
        ("ev_latitude", latitude),
        ("ev_longitude", longitude),
        ("ev_mag_value", magnitude),
        ("ev_depth", depth),
        ("ev_event_time", event_time),
    )
    for field_name, provider_value in provider_fields:
        if provider_value is not _MISSING:
            data[field_name] = provider_value
    return FeltReportEventData(data)


def _row(*, latitude=45.0, longitude=10.0, raw=5.0, corrected=6.0):
    """Build one public felt-intensity row with controlled provider values."""
    data = {}
    provider_fields = (
        ("lat", latitude),
        ("lon", longitude),
        ("raw", raw),
        ("corrected", corrected),
    )
    for field_name, provider_value in provider_fields:
        if provider_value is not _MISSING:
            data[field_name] = provider_value
    return data


def _felt_reports(rows, event_id="event-one"):
    """Build the requested event view returned by get_feltreports()."""
    return FeltReportIntensityData({
        "unid": event_id,
        "intensities": list(rows),
        "comments": "#provider comments ",
    })


class EMSCFeltReportNormalizationTests(unittest.TestCase):
    """Test the independent EMSC felt-intensity normalization adapter."""

    def _extract(self, rows=None, *, event_data=_MISSING,
                 felt_reports=_MISSING, logger=None, configuration=None):
        logger = logger or Mock()
        if event_data is _MISSING:
            event_data = _event()
        if felt_reports is _MISSING:
            if rows is None:
                rows = [_row()]
            felt_reports = _felt_reports(rows)

        formatter = EMSCFeltReportDataFormatter(
            logger=logger,
            configuration=configuration,
        )
        records = formatter.extract_raw_stations(
            event_data=event_data,
            felt_reports=felt_reports,
        )
        return records, logger

    @staticmethod
    def _warning_messages(logger):
        return [str(call.args[0]) for call in logger.warning.call_args_list]

    @staticmethod
    def _error_messages(logger):
        return [str(call.args[0]) for call in logger.error.call_args_list]

    def test_public_event_view_normalizes_corrected_value_and_provenance(self):
        event_data = _event()
        felt_reports = _felt_reports([
            _row(latitude=45.25, longitude=10.5, raw=4.0, corrected=6.25)
        ])

        records, logger = self._extract(
            event_data=event_data,
            felt_reports=felt_reports,
        )

        self.assertIsInstance(event_data, FeltReportEventData)
        self.assertIsInstance(felt_reports, FeltReportIntensityData)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["latitude"], 45.25)
        self.assertEqual(record["longitude"], 10.5)
        self.assertEqual(record["source"], "EMSC")
        self.assertEqual(record["provider_value"], 6.25)
        self.assertEqual(record["provider_unit"], "EMS-98")
        self.assertEqual(record["network"], "FR")
        self.assertEqual(record["station"], "A001")
        self.assertEqual(record["location"], "00")
        self.assertEqual(record["channel"], "HNZ")
        self.assertEqual(
            record["pga"],
            float(10 ** Calculator.I_to_PGA_Wordon2012(6.25)),
        )
        self.assertTrue(math.isfinite(record["pga"]))
        self.assertGreater(record["pga"], 0)
        self.assertEqual(logger.warning.call_count, 0)
        self.assertEqual(logger.error.call_count, 0)

    def test_authoritative_event_context_supplies_felt_event_values(self):
        context = EventContext.from_alert_mapping(
            {
                "unid": "event-one",
                "lat": 44.5,
                "lon": 9.5,
                "mag": 5.75,
                "depth": 12.0,
                "time": "2026-08-10T10:11:12.250000Z",
            },
            scheduled_event_id="event-one",
        )

        with patch.object(
                Calculator, "haversine", return_value=25.0) as haversine, \
                patch.object(
                    Calculator, "I_Allen2012_Rhypo",
                    return_value=6.0) as allen:
            records, _logger = self._extract(
                event_data=context,
                felt_reports=_felt_reports([_row(corrected=6.0)]),
            )

        self.assertEqual(len(records), 1)
        self.assertEqual(
            records[0]["timestamp"],
            emsc_felt_report.get_epoch_time(context.get_origin_time()),
        )
        haversine.assert_called_once_with(44.5, 9.5, 45.0, 10.0)
        allen.assert_called_once_with(5.75, 12.0, 25.0)

    def test_missing_and_none_corrected_values_use_raw_with_warning(self):
        records, logger = self._extract([
            _row(raw=4.0, corrected=_MISSING),
            _row(raw=5.0, corrected=None),
        ])

        self.assertEqual(
            [record["provider_value"] for record in records],
            [4.0, 5.0],
        )
        fallback_warnings = [
            message for message in self._warning_messages(logger)
            if "raw intensity fallback" in message
        ]
        self.assertEqual(len(fallback_warnings), 2)
        self.assertIn("row 0", fallback_warnings[0])
        self.assertIn("4.0", fallback_warnings[0])
        self.assertIn("missing", fallback_warnings[0])
        self.assertIn("row 1", fallback_warnings[1])
        self.assertIn("5.0", fallback_warnings[1])
        self.assertIn("None", fallback_warnings[1])

    def test_present_invalid_corrected_value_never_retries_raw(self):
        records, logger = self._extract([
            _row(raw=6.0, corrected="invalid-corrected")
        ])

        self.assertEqual(records, [])
        warnings = self._warning_messages(logger)
        self.assertTrue(any(
            "EMSC" in message
            and "row 0" in message
            and "invalid selected intensity" in message
            and "invalid-corrected" in message
            for message in warnings
        ), warnings)
        self.assertFalse(any(
            "raw intensity fallback" in message for message in warnings))
        self.assertTrue(any(
            "EMSC" in message and "zero usable" in message
            for message in self._error_messages(logger)))

    def test_invalid_selected_intensities_are_warned_and_rejected(self):
        cases = (
            ("missing", _row(raw=_MISSING, corrected=_MISSING), None),
            ("boolean", _row(corrected=True), True),
            ("nonnumeric", _row(corrected="six"), "six"),
            ("nan", _row(corrected=float("nan")), float("nan")),
            ("positive infinity", _row(corrected=float("inf")), float("inf")),
            ("negative infinity", _row(corrected=float("-inf")), float("-inf")),
            ("below one", _row(corrected=0.999), 0.999),
            ("rounded above ten", _row(corrected=10.5001), 10.5001),
        )
        for label, row, original_value in cases:
            with self.subTest(label=label):
                records, logger = self._extract([row])

                self.assertEqual(records, [])
                warnings = self._warning_messages(logger)
                self.assertTrue(any(
                    "EMSC" in message
                    and "row 0" in message
                    and "invalid selected intensity" in message
                    and repr(original_value) in message
                    for message in warnings
                ), warnings)
                self.assertTrue(any(
                    "zero usable" in message
                    for message in self._error_messages(logger)))

    def test_intensity_boundaries_preserve_numpy_rounding_and_exact_value(self):
        cases = (
            (1.0, 4.0),
            (10.5, 7.5),
        )
        for selected_intensity, predicted_intensity in cases:
            with self.subTest(selected_intensity=selected_intensity):
                with patch.object(
                        Calculator, "I_Allen2012_Rhypo",
                        return_value=predicted_intensity), patch.object(
                        Calculator, "I_to_PGA_Wordon2012", return_value=0.0):
                    records, _logger = self._extract([
                        _row(corrected=selected_intensity)
                    ])

                self.assertEqual(len(records), 1)
                self.assertEqual(
                    records[0]["provider_value"], selected_intensity)

        with patch.object(Calculator, "I_Allen2012_Rhypo") as allen:
            records, logger = self._extract([_row(corrected=10.5001)])

        self.assertEqual(records, [])
        allen.assert_not_called()
        self.assertTrue(any(
            "NumPy-rounded value" in message and "above 10" in message
            for message in self._warning_messages(logger)))

    def test_every_invalid_event_context_category_returns_before_rows(self):
        common_invalid_values = (
            ("missing", _MISSING),
            ("boolean", True),
            ("nonnumeric", "not-a-number"),
            ("nan", float("nan")),
            ("positive infinity", float("inf")),
            ("negative infinity", float("-inf")),
        )
        cases = []
        for field_name in ("latitude", "longitude", "magnitude", "depth"):
            for label, value in common_invalid_values:
                cases.append((field_name, label, value))
        cases.extend((
            ("latitude", "below range", -90.1),
            ("latitude", "above range", 90.1),
            ("longitude", "below range", -180.1),
            ("longitude", "above range", 180.1),
            ("depth", "negative", -0.1),
        ))

        for field_name, label, value in cases:
            with self.subTest(field=field_name, label=label):
                event_data = _event(**{field_name: value})
                felt_reports = Mock()
                logger = Mock()
                formatter = EMSCFeltReportDataFormatter(logger=logger)

                records = formatter.extract_raw_stations(
                    event_data=event_data,
                    felt_reports=felt_reports,
                )

                self.assertEqual(records, [])
                felt_reports.get_intensities.assert_not_called()
                logger.error.assert_called_once()
                error_message = str(logger.error.call_args.args[0])
                self.assertIn("EMSC", error_message)
                self.assertIn("event-one", error_message)
                self.assertIn(field_name, error_message)
                self.assertIn("no usable felt records", error_message)

    def test_zero_coordinates_depth_and_unrestricted_magnitude_are_accepted(self):
        event_data = _event(
            latitude=0,
            longitude=0,
            magnitude=-2.0,
            depth=0,
        )
        with patch.object(
                Calculator, "I_Allen2012_Rhypo", return_value=4.0), \
                patch.object(
                    Calculator, "I_to_PGA_Wordon2012", return_value=0.0):
            records, logger = self._extract(
                [_row(latitude=0, longitude=0, corrected=4.0)],
                event_data=event_data,
            )

        self.assertEqual(len(records), 1)
        self.assertEqual(logger.error.call_count, 0)

    def test_every_invalid_row_coordinate_is_warned_and_rejected(self):
        cases = (
            ("latitude", "latitude", _MISSING),
            ("latitude", "latitude", True),
            ("latitude", "latitude", "north"),
            ("latitude", "latitude", float("nan")),
            ("latitude", "latitude", float("inf")),
            ("latitude", "latitude", float("-inf")),
            ("latitude", "latitude", -90.1),
            ("latitude", "latitude", 90.1),
            ("longitude", "longitude", _MISSING),
            ("longitude", "longitude", True),
            ("longitude", "longitude", "east"),
            ("longitude", "longitude", float("nan")),
            ("longitude", "longitude", float("inf")),
            ("longitude", "longitude", float("-inf")),
            ("longitude", "longitude", -180.1),
            ("longitude", "longitude", 180.1),
        )
        for coordinate_label, row_field, value in cases:
            with self.subTest(coordinate=coordinate_label, value=value):
                records, logger = self._extract([
                    _row(**{row_field: value})
                ])

                self.assertEqual(records, [])
                warnings = self._warning_messages(logger)
                self.assertTrue(any(
                    "EMSC" in message
                    and "row 0" in message
                    and f"invalid {coordinate_label}" in message
                    and repr(None if value is _MISSING else value) in message
                    for message in warnings
                ), warnings)

    def test_coordinates_are_validated_independently(self):
        records, logger = self._extract([
            _row(latitude=None, longitude=None)
        ])

        self.assertEqual(records, [])
        warnings = self._warning_messages(logger)
        self.assertTrue(any("invalid latitude" in message for message in warnings))
        self.assertTrue(any("invalid longitude" in message for message in warnings))

    def test_longitude_181_is_rejected_without_rewriting(self):
        with patch.object(Calculator, "haversine") as haversine:
            records, logger = self._extract([_row(longitude=181)])

        self.assertEqual(records, [])
        haversine.assert_not_called()
        warnings = self._warning_messages(logger)
        self.assertTrue(any(
            "invalid longitude" in message and "181" in message
            for message in warnings))
        self.assertFalse(any("-179" in message for message in warnings))

    def test_latitude_first_distance_and_corrected_allen_boundary_are_used(self):
        with patch.object(
                Calculator, "haversine", return_value=12.5) as haversine, \
                patch.object(
                    Calculator, "I_Allen2012_Rhypo",
                    return_value=6.0) as allen, \
                patch.object(
                    Calculator, "I_Allen2012_Rhypo_legacy",
                    side_effect=AssertionError("legacy Allen must not run")) \
                as legacy_allen, patch.object(
                    Calculator, "I_to_PGA_Wordon2012", return_value=0.0):
            records, _logger = self._extract([
                _row(latitude=46.0, longitude=11.0, corrected=6.0)
            ])

        self.assertEqual(len(records), 1)
        haversine.assert_called_once_with(45.0, 10.0, 46.0, 11.0)
        allen.assert_called_once_with(5.5, 10.0, 12.5)
        legacy_allen.assert_not_called()

    def test_allen_reach_is_strictly_greater_than_three(self):
        cases = (
            (3.0, False),
            (3.0001, True),
        )
        for predicted_intensity, should_retain in cases:
            with self.subTest(predicted_intensity=predicted_intensity):
                with patch.object(
                        Calculator, "I_Allen2012_Rhypo",
                        return_value=predicted_intensity), patch.object(
                        Calculator, "I_to_PGA_Wordon2012",
                        return_value=0.0) as worden:
                    records, logger = self._extract([
                        _row(corrected=6.0)
                    ])

                self.assertEqual(bool(records), should_retain)
                if should_retain:
                    worden.assert_called_once_with(6.0)
                else:
                    worden.assert_not_called()
                    self.assertTrue(any(
                        "Allen reach" in message
                        and "3.0" in message
                        and "greater than 3" in message
                        for message in self._warning_messages(logger)))

    def test_allen_residual_three_is_inclusive(self):
        cases = (
            (9.0, True),
            (9.0001, False),
        )
        for selected_intensity, should_retain in cases:
            with self.subTest(selected_intensity=selected_intensity):
                with patch.object(
                        Calculator, "I_Allen2012_Rhypo",
                        return_value=6.0), patch.object(
                        Calculator, "I_to_PGA_Wordon2012", return_value=0.0):
                    records, logger = self._extract([
                        _row(corrected=selected_intensity)
                    ])

                self.assertEqual(bool(records), should_retain)
                if not should_retain:
                    self.assertTrue(any(
                        "Allen residual" in message
                        and repr(selected_intensity) in message
                        and "above 3" in message
                        for message in self._warning_messages(logger)))

    def test_worden_exact_value_is_exponentiated_once_without_g_conversion(self):
        selected_intensity = 6.25
        with patch.object(
                Calculator, "I_Allen2012_Rhypo", return_value=6.0), \
                patch.object(
                    Calculator, "I_to_PGA_Wordon2012", return_value=2.0) \
                as worden, patch.object(
                    Calculator, "percent_g_to_cm_s2",
                    side_effect=AssertionError("percent-g conversion used")) \
                as percent_g:
            records, _logger = self._extract([
                _row(corrected=selected_intensity)
            ])

        worden.assert_called_once_with(selected_intensity)
        percent_g.assert_not_called()
        self.assertEqual(len(records), 1)
        self.assertIsInstance(records[0]["pga"], float)
        self.assertEqual(records[0]["pga"], 100.0)
        self.assertNotEqual(records[0]["pga"], 100.0 * 980.665)

    def test_invalid_converted_pga_is_warned_and_rejected(self):
        cases = (
            ("underflow", float("-inf")),
            ("nan", float("nan")),
            ("infinity", float("inf")),
            ("overflow", 400.0),
        )
        for label, log10_pga in cases:
            with self.subTest(label=label):
                with patch.object(
                        Calculator, "I_Allen2012_Rhypo", return_value=6.0), \
                        patch.object(
                            Calculator, "I_to_PGA_Wordon2012",
                            return_value=log10_pga):
                    records, logger = self._extract([
                        _row(corrected=6.0)
                    ])

                self.assertEqual(records, [])
                warnings = self._warning_messages(logger)
                self.assertTrue(any(
                    "EMSC" in message
                    and "row 0" in message
                    and "invalid converted PGA" in message
                    and "6.0" in message
                    for message in warnings
                ), warnings)

    def test_invalid_conversion_is_row_local(self):
        with patch.object(
                Calculator, "I_Allen2012_Rhypo", return_value=6.0), \
                patch.object(
                    Calculator, "I_to_PGA_Wordon2012",
                    side_effect=[float("nan"), 2.0]):
            records, logger = self._extract([
                _row(corrected=6.0),
                _row(corrected=7.0),
            ])

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["provider_value"], 7.0)
        self.assertEqual(records[0]["pga"], 100.0)
        self.assertTrue(any(
            "row 0" in message and "invalid converted PGA" in message
            for message in self._warning_messages(logger)))

    def test_colocated_rows_remain_separate_and_in_provider_order(self):
        with patch.object(
                Calculator, "I_Allen2012_Rhypo", return_value=6.0):
            records, _logger = self._extract([
                _row(corrected=4.0),
                _row(corrected=5.0),
                _row(corrected=6.0),
            ])

        self.assertEqual(len(records), 3)
        self.assertEqual(
            [record["provider_value"] for record in records],
            [4.0, 5.0, 6.0],
        )
        self.assertEqual(
            [record["station"] for record in records],
            ["A001", "A002", "A003"],
        )
        self.assertEqual(
            {
                ".".join((
                    record["network"],
                    record["station"],
                    record["location"],
                    record["channel"],
                ))
                for record in records
            },
            {
                "FR.A001.00.HNZ",
                "FR.A002.00.HNZ",
                "FR.A003.00.HNZ",
            },
        )
        for record in records:
            self.assertEqual(record["latitude"], 45.0)
            self.assertEqual(record["longitude"], 10.0)
            self.assertEqual(record["network"], "FR")
            self.assertEqual(record["location"], "00")
            self.assertEqual(record["channel"], "HNZ")
            self.assertEqual(record["source"], "EMSC")
            self.assertEqual(record["provider_unit"], "EMS-98")
        self.assertEqual(len({record["timestamp"] for record in records}), 1)

    def test_rejected_rows_do_not_consume_codes_and_calls_restart(self):
        rows = [
            _row(corrected="rejected"),
            _row(corrected=5.0),
            _row(latitude=91.0, corrected=6.0),
            _row(corrected=7.0),
        ]
        with patch.object(
                Calculator, "I_Allen2012_Rhypo", return_value=6.0):
            first_records, _logger = self._extract(rows)
            second_records, _logger = self._extract([_row(corrected=6.0)])

        self.assertEqual(
            [record["station"] for record in first_records],
            ["A001", "A002"],
        )
        self.assertEqual(second_records[0]["station"], "A001")

    def test_namespace_exhaustion_aborts_the_normalization_call(self):
        rows = [
            _row(corrected=5.0),
            _row(corrected=6.0),
            _row(corrected=7.0),
        ]
        with patch.object(
                Calculator, "I_Allen2012_Rhypo", return_value=6.0), \
                patch.object(
                    emsc_felt_report,
                    "_FELT_REPORT_STATION_CODE_COUNT",
                    2,
                ):
            with self.assertRaisesRegex(ValueError, "namespace exhausted"):
                self._extract(rows)

    def test_alternate_valid_component_is_used_exactly(self):
        configuration = {
            "finder-executable": {"felt-report-component-code": "HG2"}
        }

        records, _logger = self._extract(configuration=configuration)

        self.assertEqual(records[0]["channel"], "HG2")
        self.assertEqual(records[0]["station"], "A001")

    def test_invalid_component_configurations_fail_visibly(self):
        cases = (
            ("missing section", {}),
            ("missing setting", {"finder-executable": {}}),
            ("empty", ""),
            ("lowercase", "hnz"),
            ("dotted", "HN.Z"),
            ("space", "HN Z"),
            ("forward path separator", "HN/Z"),
            ("backward path separator", "HN\\Z"),
            ("control character", "HN\nZ"),
            ("non-ASCII", "HNÉ"),
        )
        for label, configured_value in cases:
            with self.subTest(label=label):
                configuration = configured_value
                if not isinstance(configured_value, dict):
                    configuration = {
                        "finder-executable": {
                            "felt-report-component-code": configured_value,
                        }
                    }
                felt_reports = Mock()

                with self.assertRaises(ValueError) as raised:
                    self._extract(
                        felt_reports=felt_reports,
                        configuration=configuration,
                    )

                self.assertIn(
                    "felt-report-component-code", str(raised.exception))
                felt_reports.get_intensities.assert_not_called()

    def test_manager_passes_active_configuration_to_felt_formatter(self):
        manager = object.__new__(findermanager.FinDerManager)
        manager.logger = Mock()
        manager.configuration = {
            "finder-executable": {"felt-report-component-code": "HG2"}
        }
        event_context = object()
        felt_reports = object()
        normalized = [object()]

        with patch.object(
                findermanager,
                "EMSCFeltReportDataFormatter",
        ) as formatter_type:
            formatter_type.return_value.extract_raw_stations.return_value = (
                normalized)

            result = manager._normalize_provider(
                EMSC_FELT_REPORT_SERVICE,
                event_context,
                felt_reports,
            )

        self.assertIs(result, normalized)
        formatter_type.assert_called_once_with(
            logger=manager.logger,
            configuration=manager.configuration,
        )
        formatter_type.return_value.extract_raw_stations.assert_called_once_with(
            event_data=event_context,
            felt_reports=felt_reports,
        )

    def test_invalid_component_is_not_classified_as_provider_outage(self):
        manager = object.__new__(findermanager.FinDerManager)
        manager.logger = Mock()
        manager.configuration = {
            "finder-executable": {"felt-report-component-code": "hnz"}
        }
        outcome = manager._new_provider_outcome()
        acquired = {
            EMSC_FELT_REPORT_SERVICE: {
                "scientific_value": _felt_reports([_row()]),
                "outcome": outcome,
            }
        }

        with self.assertRaises(ValueError):
            manager._normalize_acquired_providers(
                acquired,
                _event(),
                "event-one",
            )

        self.assertIsNone(outcome["failure_kind"])
        self.assertEqual(outcome["normalized_count"], 0)

    def test_event_timestamp_and_felt_rows_are_each_read_once(self):
        event_data = Mock(wraps=_event())
        felt_reports = Mock(wraps=_felt_reports([
            _row(corrected=5.0),
            _row(corrected=6.0),
        ]))
        with patch(
                "pyfinder.utils.dataformatter.emsc_felt_report.get_epoch_time",
                return_value=123.0) as epoch_time, patch.object(
                    Calculator, "I_Allen2012_Rhypo", return_value=6.0), \
                patch.object(
                    Calculator, "I_to_PGA_Wordon2012", return_value=0.0):
            records, _logger = self._extract(
                event_data=event_data,
                felt_reports=felt_reports,
            )

        self.assertEqual(len(records), 2)
        self.assertEqual(
            [record["timestamp"] for record in records], [123.0, 123.0])
        event_data.get_latitude.assert_called_once_with()
        event_data.get_longitude.assert_called_once_with()
        event_data.get_magnitude.assert_called_once_with()
        event_data.get_depth.assert_called_once_with()
        event_data.get_event_time.assert_called_once_with()
        epoch_time.assert_called_once_with("2026-08-09T12:00:00Z")
        felt_reports.get_intensities.assert_called_once_with()

    def test_empty_event_view_emits_zero_result_error(self):
        records, logger = self._extract([])

        self.assertEqual(records, [])
        logger.error.assert_called_once()
        error_message = str(logger.error.call_args.args[0])
        self.assertIn("EMSC", error_message)
        self.assertIn("zero usable records", error_message)
        self.assertIn("event-one", error_message)

    def test_malformed_public_model_access_remains_visible(self):
        logger = Mock()
        formatter = EMSCFeltReportDataFormatter(logger=logger)

        with self.assertRaises(AttributeError):
            formatter.extract_raw_stations(
                event_data=object(),
                felt_reports=_felt_reports([_row()]),
            )
        logger.error.assert_not_called()

        with self.assertRaises(ProviderModelAccessError):
            formatter.extract_raw_stations(
                event_data=_event(),
                felt_reports=None,
            )
        logger.error.assert_not_called()

        multi_event_dataset = FeltReportIntensityData({
            "event-one": {
                "unid": "event-one",
                "intensities": [_row()],
            },
        })
        with self.assertRaises(ProviderModelAccessError):
            formatter.extract_raw_stations(
                event_data=_event(),
                felt_reports=multi_event_dataset,
            )
        logger.error.assert_not_called()

        with self.assertRaises(ProviderModelAccessError):
            formatter.extract_raw_stations(
                event_data=_event(),
                felt_reports=_felt_reports([object()]),
            )
        logger.error.assert_not_called()


if __name__ == "__main__":
    unittest.main()
