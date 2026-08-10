"""Offline tests for the accepted ESM observation-normalization contract."""

import atexit
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock


# ParamWS configures its package logger during import. Keep that dependency
# side effect outside the repository when this focused module runs alone.
_PARAMWS_LOG_DIRECTORY = tempfile.TemporaryDirectory(
    prefix="pyfinder-esm-normalization-unit-")
atexit.register(_PARAMWS_LOG_DIRECTORY.cleanup)
_original_paramws_log_file = os.environ.get("PARAMWS_LOG_FILE")
os.environ["PARAMWS_LOG_FILE"] = str(
    Path(_PARAMWS_LOG_DIRECTORY.name) / "paramws.log")
try:
    from pyfinder.eventcontext import EventContext
    from pyfinder.pyfinderconfig import pyfinderconfig
    from pyfinder.utils.calculator import Calculator
    from pyfinder.utils import dataformatter
    from pyfinder.utils.dataformatter import ESMShakeMapDataFormatter
    from pyfinder.utils.station_merger import StationMerger
finally:
    if _original_paramws_log_file is None:
        os.environ.pop("PARAMWS_LOG_FILE", None)
    else:
        os.environ["PARAMWS_LOG_FILE"] = _original_paramws_log_file


class _Component:
    """Controlled double for the public ESM component getters."""

    def __init__(self, name, acceleration):
        self.name = name
        self.acceleration = acceleration

    def get_component_name(self):
        return self.name

    def get_acceleration(self):
        return self.acceleration


class _Station:
    """Controlled double for the public ESM station getters."""

    def __init__(self, components, *, network="NW", station="STA",
                 latitude=45.0, longitude=12.0):
        self.components = components
        self.network = network
        self.station = station
        self.latitude = latitude
        self.longitude = longitude

    def get_components(self):
        return self.components

    def get_network_code(self):
        return self.network

    def get_station_code(self):
        return self.station

    def get_latitude(self):
        return self.latitude

    def get_longitude(self):
        return self.longitude


class _Amplitudes:
    """Controlled double for the public ESM amplitude collection getter."""

    def __init__(self, stations):
        self.stations = stations

    def get_stations(self):
        return self.stations


class _Event:
    """Controlled double for the public event timestamp getter."""

    def get_origin_time(self):
        return "2026-08-09T12:00:00Z"


class ESMObservationNormalizationTests(unittest.TestCase):

    def _extract(self, stations, *, selection="maximum-all",
                 live_mode=False, logger=None):
        logger = logger or Mock()
        configuration = {
            "general": {"component-selection": selection},
            "finder-executable": {"finder-live-mode": live_mode},
        }
        formatter = ESMShakeMapDataFormatter(
            logger=logger,
            configuration=configuration,
        )
        records = formatter.extract_raw_stations(
            event_data=_Event(),
            amplitudes=_Amplitudes(stations),
        )
        return records, logger

    @staticmethod
    def _warning_messages(logger):
        return [str(call.args[0]) for call in logger.warning.call_args_list]

    def test_shipped_component_selection_is_maximum_all(self):
        self.assertEqual(
            pyfinderconfig["general"]["component-selection"],
            "maximum-all",
        )

    def test_timestamp_comes_from_authoritative_event_context(self):
        context = EventContext.from_alert_mapping(
            {
                "unid": "esm-event",
                "lat": 45.0,
                "lon": 12.0,
                "mag": 5.5,
                "depth": 10.0,
                "time": "2026-08-10T10:11:12.250000Z",
            },
            scheduled_event_id="esm-event",
        )
        formatter = ESMShakeMapDataFormatter(
            logger=Mock(),
            configuration={"general": {"component-selection": "maximum-all"}},
        )

        records = formatter.extract_raw_stations(
            event_data=context,
            amplitudes=_Amplitudes([
                _Station([_Component("HNE", 1.0)])
            ]),
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(
            records[0]["timestamp"],
            dataformatter.get_epoch_time(context.get_origin_time()),
        )

    def test_maximum_all_can_select_vertical_and_records_provenance(self):
        records, _logger = self._extract([
            _Station([
                _Component("HNE", 1.25),
                _Component("HNZ", 3.5),
            ])
        ])

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["channel"], "HNZ")
        self.assertEqual(record["source"], "ESM")
        self.assertEqual(record["provider_value"], 3.5)
        self.assertEqual(record["provider_unit"], "%g")
        self.assertEqual(
            record["pga"],
            Calculator.percent_g_to_cm_s2(3.5),
        )
        self.assertTrue(math.isfinite(record["pga"]))
        self.assertGreater(record["pga"], 0)

    def test_maximum_horizontal_excludes_only_codes_ending_in_z(self):
        records, _logger = self._extract([
            _Station([
                _Component("HNZ", 20.0),
                _Component("HNE", 4.0),
                _Component("HN1", 5.0),
                _Component("Z12", 6.0),
            ])
        ], selection="maximum-horizontal")

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["channel"], "Z12")
        self.assertEqual(records[0]["provider_value"], 6.0)

    def test_equal_maxima_keep_first_provider_component(self):
        records, _logger = self._extract([
            _Station([
                _Component("00.HNE", 4.0),
                _Component("10.HNN", 4.0),
            ])
        ])

        self.assertEqual(records[0]["location"], "00")
        self.assertEqual(records[0]["channel"], "HNE")

    def test_supported_component_location_and_channel_parsing(self):
        cases = (
            (".HNE", "", "HNE"),
            ("00.HNE", "00", "HNE"),
        )
        for component_name, expected_location, expected_channel in cases:
            with self.subTest(component_name=component_name):
                records, _logger = self._extract([
                    _Station([_Component(component_name, 2.0)])
                ])
                self.assertEqual(records[0]["location"], expected_location)
                self.assertEqual(records[0]["channel"], expected_channel)

    def test_unsupported_selection_logs_critical_and_uses_maximum_all(self):
        logger = Mock()
        records, _logger = self._extract([
            _Station([
                _Component("HNE", 2.0),
                _Component("HNZ", 9.0),
            ])
        ], selection="largest-mystery", logger=logger)

        self.assertEqual(records[0]["channel"], "HNZ")
        logger.critical.assert_called_once()
        message = logger.critical.call_args.args[0]
        self.assertIn("ESM", message)
        self.assertIn("unsupported", message)
        self.assertIn("maximum-all", message)
        self.assertEqual(
            logger.critical.call_args.args[1],
            "largest-mystery",
        )

    def test_normalization_is_independent_of_finder_live_mode(self):
        station = _Station([_Component("HNE", 2.75)])
        live_records, _logger = self._extract([station], live_mode=True)
        offline_records, _logger = self._extract([station], live_mode=False)

        self.assertEqual(live_records, offline_records)
        self.assertEqual(
            live_records[0]["pga"],
            2.75 * 0.01 * 980.665,
        )

    def test_each_invalid_component_is_warned_and_rejected(self):
        cases = (
            (None, "missing acceleration"),
            ("not-a-number", "nonnumeric acceleration"),
            (float("nan"), "nonfinite acceleration"),
            (float("inf"), "nonfinite acceleration"),
            (float("-inf"), "nonfinite acceleration"),
            (0, "zero acceleration"),
            (-1.0, "negative acceleration"),
        )
        for acceleration, expected_reason in cases:
            with self.subTest(acceleration=acceleration):
                records, logger = self._extract([
                    _Station(
                        [_Component("HNE", acceleration)],
                        station="BAD",
                    )
                ])
                self.assertEqual(records, [])
                warnings = self._warning_messages(logger)
                self.assertTrue(any(
                    "ESM" in message
                    and "NW.BAD" in message
                    and "HNE" in message
                    and expected_reason in message
                    for message in warnings
                ), warnings)

    def test_invalid_component_does_not_discard_valid_sibling(self):
        records, logger = self._extract([
            _Station([
                _Component("HNE", "invalid"),
                _Component("HNN", 1.5),
            ])
        ])

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["channel"], "HNN")
        self.assertTrue(any(
            "ESM" in message and "HNE" in message
            and "nonnumeric" in message
            for message in self._warning_messages(logger)
        ))

    def test_invalid_converted_pga_is_warned_and_station_is_rejected(self):
        cases = (
            (1e308, "OVERFLOW", "HNE", "nonfinite"),
            (5e-324, "UNDERFLOW", "HNN", "nonpositive"),
        )
        for provider_value, station_code, component_code, reason in cases:
            with self.subTest(reason=reason):
                converted_pga = Calculator.percent_g_to_cm_s2(provider_value)
                if reason == "nonfinite":
                    self.assertFalse(math.isfinite(converted_pga))
                else:
                    self.assertEqual(converted_pga, 0.0)

                records, logger = self._extract([
                    _Station(
                        [_Component(component_code, provider_value)],
                        station=station_code,
                    )
                ])

                self.assertEqual(records, [])
                warnings = self._warning_messages(logger)
                self.assertTrue(any(
                    "ESM" in message
                    and f"NW.{station_code}" in message
                    and component_code in message
                    and "conversion" in message
                    and reason in message
                    and repr(provider_value) in message
                    for message in warnings
                ), warnings)
                self.assertTrue(any(
                    f"ESM station NW.{station_code}" in message
                    and "no eligible valid component remains" in message
                    and "all eligible components are invalid" in message
                    for message in warnings
                ), warnings)

    def test_invalid_converted_pga_does_not_discard_valid_sibling(self):
        cases = (
            (1e308, "HNE", "nonfinite"),
            (5e-324, "HNN", "nonpositive"),
        )
        valid_provider_value = 2.5
        for invalid_provider_value, invalid_component, reason in cases:
            with self.subTest(reason=reason):
                records, logger = self._extract([
                    _Station([
                        _Component(invalid_component, invalid_provider_value),
                        _Component("HN1", valid_provider_value),
                    ], station="SIBLING")
                ])

                self.assertEqual(len(records), 1)
                record = records[0]
                self.assertEqual(record["channel"], "HN1")
                self.assertEqual(
                    record["provider_value"], valid_provider_value)
                self.assertEqual(
                    record["pga"],
                    Calculator.percent_g_to_cm_s2(valid_provider_value),
                )
                self.assertTrue(math.isfinite(record["pga"]))
                self.assertGreater(record["pga"], 0)
                self.assertTrue(any(
                    "ESM" in message
                    and "NW.SIBLING" in message
                    and invalid_component in message
                    and "conversion" in message
                    and reason in message
                    for message in self._warning_messages(logger)
                ))

    def test_station_without_eligible_valid_component_is_warned(self):
        cases = (
            ([], "maximum-all", "component collection is empty"),
            ([_Component("HNE", 0)], "maximum-all",
             "all eligible components are invalid"),
            ([_Component("HNZ", 4.0)], "maximum-horizontal",
             "no component is eligible under maximum-horizontal"),
        )
        for components, selection, expected_reason in cases:
            with self.subTest(selection=selection, reason=expected_reason):
                records, logger = self._extract(
                    [_Station(components, station="EMPTY")],
                    selection=selection,
                )
                self.assertEqual(records, [])
                warnings = self._warning_messages(logger)
                self.assertTrue(any(
                    "ESM station NW.EMPTY" in message
                    and "no eligible valid component remains" in message
                    and expected_reason in message
                    for message in warnings
                ), warnings)

    def test_invalid_coordinates_reject_only_the_affected_station(self):
        cases = (
            (None, 12.0, "missing latitude"),
            ("north", 12.0, "nonnumeric latitude"),
            (float("nan"), 12.0, "nonfinite latitude"),
            (float("inf"), 12.0, "nonfinite latitude"),
            (float("-inf"), 12.0, "nonfinite latitude"),
            (90.1, 12.0, "latitude"),
            (-90.1, 12.0, "latitude"),
            (45.0, None, "missing longitude"),
            (45.0, "east", "nonnumeric longitude"),
            (45.0, float("nan"), "nonfinite longitude"),
            (45.0, float("inf"), "nonfinite longitude"),
            (45.0, float("-inf"), "nonfinite longitude"),
            (45.0, 180.1, "longitude"),
            (45.0, -180.1, "longitude"),
        )
        for latitude, longitude, expected_reason in cases:
            with self.subTest(latitude=latitude, longitude=longitude):
                records, logger = self._extract([
                    _Station(
                        [_Component("HNE", 2.0)],
                        station="COORD",
                        latitude=latitude,
                        longitude=longitude,
                    )
                ])
                self.assertEqual(records, [])
                warnings = self._warning_messages(logger)
                self.assertTrue(any(
                    "ESM station NW.COORD" in message
                    and expected_reason in message
                    for message in warnings
                ), warnings)

    def test_later_valid_station_survives_earlier_station_rejection(self):
        records, logger = self._extract([
            _Station(
                [_Component("HNE", 10.0)],
                station="EARLY",
                latitude=91.0,
            ),
            _Station(
                [_Component("00.HNN", 2.0)],
                station="LATER",
            ),
        ])

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["station"], "LATER")
        self.assertTrue(any(
            "ESM station NW.EARLY" in message and "latitude" in message
            for message in self._warning_messages(logger)
        ))

    def test_provenance_survives_list_and_station_merger_seam(self):
        records, _logger = self._extract([
            _Station([_Component("00.HNE", 3.0)])
        ])
        merged = StationMerger().merge(esm_data=list(records), rrsm_data=[])

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["source"], "ESM")
        self.assertEqual(merged[0]["provider_value"], 3.0)
        self.assertEqual(merged[0]["provider_unit"], "%g")


if __name__ == "__main__":
    unittest.main()
