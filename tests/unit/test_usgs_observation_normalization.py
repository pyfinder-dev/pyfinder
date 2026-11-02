"""Offline scientific checks of USGS station and DYFI normalization."""

import atexit
from copy import deepcopy
import math
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch


# ParamWS configures logging when imported. Confine that import side effect to
# a temporary directory, just as the existing provider normalization tests do.
_LOG_DIRECTORY = tempfile.TemporaryDirectory(prefix="pyfinder-usgs-normalization-")
atexit.register(_LOG_DIRECTORY.cleanup)
_previous_log = os.environ.get("PARAMWS_LOG_FILE")
os.environ["PARAMWS_LOG_FILE"] = str(Path(_LOG_DIRECTORY.name) / "paramws.log")
try:
    from paramws.clients import (
        FeltReportIntensityData,
        ShakeMapComponentNode,
        ShakeMapStationAmplitudes,
        ShakeMapStationNode,
    )
    from pyfinder.eventcontext import EventContext, ProviderModelAccessError
    from pyfinder.utils.timeutils import get_epoch_time
    from pyfinder.pyfinderconfig import USGS_DYFI_SERVICE, USGS_SHAKEMAP_SERVICE
    from pyfinder.utils.dataformatter import (
        USGSDYFIDataFormatter,
        USGSShakeMapDataFormatter,
    )
    from pyfinder.utils.dataformatter.usgs_dyfi import _representative_position
finally:
    if _previous_log is None:
        os.environ.pop("PARAMWS_LOG_FILE", None)
    else:
        os.environ["PARAMWS_LOG_FILE"] = _previous_log


CONFIGURATION = {
    "general": {"component-selection": "maximum-all"},
    "finder-executable": {"felt-report-component-code": "MMI"},
}
CONTEXT = EventContext("emsc-original", 0.0, 45.0, 5.0, 10.0,
                       "2026-10-20T10:00:00Z")


def channel(value, units="%g", name="00.HNE"):
    return ShakeMapComponentNode({"name": name, "acc": value, "accunits": units})


def station(components, **overrides):
    values = {
        "id": "CI.TEST", "netid": "CI", "code": "TEST",
        "lat": 0.0, "lon": 45.0, "station_type": "seismic",
        "components": components,
    }
    values.update(overrides)
    result = ShakeMapStationNode()
    for key, value in values.items():
        result.add_field(key, value)
    return result


def cell_row(intensity=5.8, **overrides):
    # Closed non-square rectangle: its centroid is (45, 0), not the average
    # of the five listed vertices, which would count the first corner twice.
    values = {
        "id": "cell-1", "cdi": intensity, "nresp": 10,
        "geometry": {"type": "Polygon", "coordinates": [[
            [44.99, -0.005], [45.01, -0.005], [45.01, 0.005],
            [44.99, 0.005], [44.99, -0.005],
        ]]},
    }
    values.update(overrides)
    return values


class USGSStationNormalizationTests(unittest.TestCase):
    def extract(self, stations, selection="maximum-all"):
        configuration = deepcopy(CONFIGURATION)
        configuration["general"]["component-selection"] = selection
        logger = Mock()
        formatter = USGSShakeMapDataFormatter(logger, configuration)
        amplitudes = ShakeMapStationAmplitudes()
        amplitudes.add_field("stations", stations)
        result = formatter.extract_raw_stations(CONTEXT, amplitudes)
        return result, logger

    def test_declared_units_have_independently_calculated_linear_values(self):
        for value, unit, expected in (
                (2, "%g", 19.6133), (0.2, "g", 196.133),
                (2, "cm/s/s", 2), (2, "cm/s^2", 2), (2, "gal", 2),
                (2, "m/s^2", 200), (2, "m/s/s", 200)):
            with self.subTest(unit=unit):
                records, _ = self.extract([station([channel(value, unit)])])
                self.assertAlmostEqual(records[0]["pga"], expected)
                self.assertEqual(records[0]["provider_value"], value)
                self.assertEqual(records[0]["provider_unit"], unit)
                self.assertEqual(records[0]["source"], USGS_SHAKEMAP_SERVICE)
                self.assertEqual(records[0]["location"], "00")
                self.assertEqual(records[0]["channel"], "HNE")
                # Compatibility with other adapters, not absolute UTC proof:
                # the shared timestamp helper has a separately reported gap.
                self.assertEqual(
                    records[0]["timestamp"],
                    get_epoch_time(CONTEXT.get_origin_time()),
                )

    def test_common_context_timestamp_under_explicit_utc_runtime(self):
        # The scientific value has an independent UTC reference here. Keep the
        # environment change local to this test and restore the host timezone.
        try:
            with patch.dict(os.environ, {"TZ": "UTC"}):
                time.tzset()
                records, _ = self.extract([station([channel(1)])])
                self.assertEqual(records[0]["timestamp"], 1792490400.0)
        finally:
            time.tzset()

    def test_maximum_compares_normalized_units_and_keeps_first_tie(self):
        records, _ = self.extract([station([
            channel(10, "cm/s/s", "HNE"), channel(2, "%g", "HNN"),
            channel(19.6133, "cm/s/s", "HNZ"),
        ])])
        self.assertEqual(records[0]["channel"], "HNN")
        self.assertEqual(records[0]["provider_value"], 2)

    def test_horizontal_selection_and_invalid_policy_fallback(self):
        stations = [station([channel(1, name="HNE"), channel(2, name="HNZ")])]
        records, _ = self.extract(stations, "maximum-horizontal")
        self.assertEqual(records[0]["channel"], "HNE")
        records, logger = self.extract(stations, "unsupported")
        self.assertEqual(records[0]["channel"], "HNZ")
        logger.critical.assert_called_once()

    def test_missing_or_unsupported_unit_does_not_discard_good_sibling(self):
        for unit in (None, "", "counts", "cm/s", "MMI"):
            with self.subTest(unit=unit):
                records, logger = self.extract([station([
                    channel(100, unit, "HNZ"), channel(1, name="HNE"),
                ])])
                self.assertEqual(records[0]["channel"], "HNE")
                logger.warning.assert_called()

    def test_invalid_pga_and_conversion_overflow_leave_later_station_usable(self):
        for value, unit in (
                (None, "%g"), (True, "%g"), ("bad", "%g"),
                (0, "%g"), (-1, "%g"), (math.nan, "%g"),
                (math.inf, "%g"), (1e308, "g")):
            with self.subTest(value=value):
                records, logger = self.extract([
                    station([channel(value, unit)]),
                    station([channel(1)], code="GOOD"),
                ])
                self.assertEqual([row["station"] for row in records], ["GOOD"])
                logger.warning.assert_called()

    def test_bad_coordinates_and_noninstrumental_rows_are_not_observations(self):
        records, logger = self.extract([
            station([channel(10)], lat=91),
            station([channel(10)], lon=181),
            station([channel(10)], lat=True),
            station([channel(10)], station_type="macroseismic"),
            station([channel(10)], predicted=True),
            station([channel(1)]),
        ])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["provider_value"], 1)
        logger.warning.assert_called()

    def test_empty_components_and_all_vertical_selection_return_no_data(self):
        records, logger = self.extract([
            station([]), station([channel(1, name="HNZ")]),
        ], "maximum-horizontal")
        self.assertEqual(records, [])
        logger.error.assert_called_once()

    def test_public_model_shape_failure_is_not_an_empty_success(self):
        with self.assertRaises(ProviderModelAccessError):
            USGSShakeMapDataFormatter(configuration=CONFIGURATION).extract_raw_stations(
                CONTEXT, ShakeMapStationAmplitudes({"stations": {}}))


class USGSDYFINormalizationTests(unittest.TestCase):
    def extract(self, rows, context=CONTEXT):
        logger = Mock()
        formatter = USGSDYFIDataFormatter(logger, CONFIGURATION)
        records = formatter.extract_raw_stations(
            context, FeltReportIntensityData({"intensities": rows}))
        return records, logger

    def test_cell_centroid_preserves_area_and_independent_worden_conversion(self):
        row = cell_row()
        original = deepcopy(row)
        records, _ = self.extract([row])
        record = records[0]
        self.assertAlmostEqual(record["latitude"], 0)
        self.assertAlmostEqual(record["longitude"], 45)
        # High Worden branch: (5.8 + 1.6) / 3.7 = 2; 10**2 = 100 cm/s².
        self.assertAlmostEqual(record["pga"], 100)
        self.assertEqual(record["provider_value"], 5.8)
        self.assertEqual(record["provider_unit"], "MMI")
        self.assertEqual(record["source"], USGS_DYFI_SERVICE)
        self.assertEqual(record["coordinate_method"], "cell_centroid")
        self.assertEqual(record["provider_geometry"], original["geometry"])
        self.assertEqual(record["provider_observation_id"], "cell-1")
        self.assertEqual(row, original)
        self.assertEqual((record["network"], record["station"], record["location"],
                          record["channel"]), ("DY", "A001", "00", "MMI"))

    def test_lower_worden_branch_is_exponentiated_only_once(self):
        records, _ = self.extract([cell_row(3.337)])
        # Low branch: (3.337 - 1.78) / 1.557 = 1; 10**1 = 10 cm/s².
        self.assertAlmostEqual(records[0]["pga"], 10)

    def test_point_uses_provider_coordinate_and_keeps_independent_rows(self):
        point = {"type": "Point", "coordinates": [45, 0]}
        records, _ = self.extract([
            cell_row(geometry=point), cell_row(geometry=point),
        ])
        self.assertEqual([row["station"] for row in records], ["A001", "A002"])
        self.assertTrue(all(row["coordinate_method"] == "provider_point"
                            for row in records))

    def test_bad_intensity_or_geometry_isolated_from_later_valid_cell(self):
        bad_rows = [cell_row(value) for value in (
            None, True, "5", math.nan, math.inf, 0, -1, 11)]
        bad_rows.extend([
            cell_row(geometry=None),
            cell_row(geometry={"type": "Point", "coordinates": [181, 0]}),
            cell_row(geometry={"type": "MultiPolygon", "coordinates": []}),
        ])
        records, logger = self.extract(bad_rows + [cell_row()])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["station"], "A001")
        self.assertEqual(logger.warning.call_count, len(bad_rows))

    def test_allen_reach_and_residual_keep_existing_strict_boundaries(self):
        with patch("pyfinder.utils.dataformatter.usgs_dyfi.Calculator.I_Allen2012_Rhypo",
                   return_value=3):
            records, _ = self.extract([cell_row(3)])
            self.assertEqual(records, [])
        with patch("pyfinder.utils.dataformatter.usgs_dyfi.Calculator.I_Allen2012_Rhypo",
                   return_value=5):
            records, _ = self.extract([cell_row(8), cell_row(8.01)])
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["provider_value"], 8)

    def test_far_cell_and_invalid_context_return_no_fabricated_data(self):
        records, logger = self.extract([
            cell_row(geometry={"type": "Point", "coordinates": [-120, 60]})])
        self.assertEqual(records, [])
        logger.error.assert_called_once()
        records, logger = self.extract([cell_row()],
            EventContext("x", 91, 45, 5, 10, "2026-10-20T10:00:00Z"))
        self.assertEqual(records, [])
        logger.error.assert_called_once()

    def test_antimeridian_cell_centroid_stays_at_dateline(self):
        geometry = {"type": "Polygon", "coordinates": [[
            [179.99, -0.01], [-179.99, -0.01], [-179.99, 0.01],
            [179.99, 0.01], [179.99, -0.01],
        ]]}
        longitude, latitude, method = _representative_position(geometry)
        self.assertAlmostEqual(abs(longitude), 180)
        self.assertAlmostEqual(latitude, 0)
        self.assertEqual(method, "cell_centroid")

    def test_polygon_area_centroid_handles_hole_and_orientation(self):
        # Subtract the interior hole using its area and first moments.
        shell = [[0, 0], [4, 0], [4, 2], [0, 2], [0, 0]]
        hole = [[3, 1], [3.9, 1], [3.9, 1.9], [3, 1.9], [3, 1]]
        # Use a strictly interior 0.9x0.9 hole so the topology is valid.
        expected_x = (8 * 2 - 0.81 * 3.45) / 7.19
        expected_y = (8 * 1 - 0.81 * 1.45) / 7.19
        for rings in ([shell, hole], [shell[::-1], hole[::-1]]):
            longitude, latitude, _ = _representative_position(
                {"type": "Polygon", "coordinates": rings})
            self.assertAlmostEqual(longitude, expected_x)
            self.assertAlmostEqual(latitude, expected_y)

    def test_invalid_polygon_is_rejected_without_repair(self):
        for ring in (
                [[0, 0], [1, 0], [1, 1]],
                [[0, 0], [1, 0], [1, 1], [0, 1]],
                [[0, 0], [1, 1], [0, 1], [1, 0], [0, 0]],
                [[0, 0], [1, 0], [2, 0], [0, 0]],
                [[0, 0], [math.nan, 0], [1, 1], [0, 0]]):
            with self.subTest(ring=ring), self.assertRaises(ValueError):
                _representative_position({"type": "Polygon", "coordinates": [ring]})


if __name__ == "__main__":
    unittest.main()
