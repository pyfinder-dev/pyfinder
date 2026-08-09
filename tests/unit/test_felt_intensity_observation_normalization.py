"""Calculator tests for Batch 1 of the accepted felt-intensity contract."""

import inspect
import unittest

import numpy as np

from pyfinder.utils.calculator import Calculator


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


if __name__ == "__main__":
    unittest.main()
