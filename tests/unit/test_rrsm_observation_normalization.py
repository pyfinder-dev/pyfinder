"""Offline tests for the accepted RRSM observation-normalization contract."""

import atexit
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch


# ParamWS configures its package logger during import. Keep that dependency
# side effect outside the repository when this focused module runs alone.
_PARAMWS_LOG_DIRECTORY = tempfile.TemporaryDirectory(
    prefix="pyfinder-rrsm-normalization-unit-")
atexit.register(_PARAMWS_LOG_DIRECTORY.cleanup)
_original_paramws_log_file = os.environ.get("PARAMWS_LOG_FILE")
os.environ["PARAMWS_LOG_FILE"] = str(
    Path(_PARAMWS_LOG_DIRECTORY.name) / "paramws.log")
try:
    from pyfinder.eventcontext import EventContext
    from pyfinder.pyfinderconfig import RRSM_PEAK_MOTION_SERVICE
    from paramws.clients import (
        PeakMotionChannelData,
        PeakMotionData,
        PeakMotionStationData,
    )
    from pyfinder.utils.dataformatter import (
        ESMShakeMapDataFormatter,
        RRSM_PEAKMOTION_PGA_MAX,
        RRSM_PEAKMOTION_PGA_MIN,
        RRSMPeakMotionDataFormatter,
    )
    from pyfinder.utils import dataformatter
    from pyfinder.utils.station_merger import (
        RawStationMeasurement,
        StationMerger,
    )
finally:
    if _original_paramws_log_file is None:
        os.environ.pop("PARAMWS_LOG_FILE", None)
    else:
        os.environ["PARAMWS_LOG_FILE"] = _original_paramws_log_file


_MISSING = object()


class _Event:
    """Controlled nested RRSM event exposing the current timestamp getter."""

    def get_origin_time(self):
        return "2026-08-09T12:00:00Z"


def _channel(code="HHE", pga=1.0):
    """Build one public RRSM channel while allowing genuinely absent fields."""
    data = {}
    if code is not _MISSING:
        data["channel-code"] = code
    if pga is not _MISSING:
        data["pga-value"] = pga
    return PeakMotionChannelData(data)


def _station(channels, *, network="NW", station="STA", location="",
             latitude=45.0, longitude=12.0):
    """Build one public RRSM station with controlled provider values."""
    data = {}
    provider_fields = (
        ("network-code", network),
        ("station-code", station),
        ("location-code", location),
        ("station-latitude", latitude),
        ("station-longitude", longitude),
    )
    for field_name, provider_value in provider_fields:
        if provider_value is not _MISSING:
            data[field_name] = provider_value

    station_data = PeakMotionStationData(data)
    for channel in channels:
        station_data.add_channel(channel)
    return station_data


def _peak_motion(stations):
    """Build the public combined RRSM hierarchy used by the manager."""
    peak_motion = PeakMotionData()
    peak_motion.set_event_data(_Event())
    for station in stations:
        peak_motion.add_station(station)
    return peak_motion


class RRSMObservationNormalizationTests(unittest.TestCase):

    def _extract(self, stations, *, selection="maximum-all",
                 live_mode=False, logger=None):
        logger = logger or Mock()
        configuration = {
            "general": {"component-selection": selection},
            "finder-executable": {"finder-live-mode": live_mode},
        }
        formatter = RRSMPeakMotionDataFormatter(
            logger=logger,
            configuration=configuration,
        )
        peak_motion = _peak_motion(stations)
        records = formatter.extract_raw_stations(
            event_data=_Event(),
            amplitudes=peak_motion,
        )
        return records, logger

    @staticmethod
    def _warning_messages(logger):
        return [str(call.args[0]) for call in logger.warning.call_args_list]

    def test_linear_normalization_and_provenance_are_live_mode_independent(self):
        station = _station(
            [_channel("HHE", 2.75)],
            network="CH",
            station="TEST",
            location="01",
            latitude="46.2",
            longitude="7.3",
        )

        live_records, _logger = self._extract([station], live_mode=True)
        offline_records, _logger = self._extract([station], live_mode=False)

        self.assertEqual(live_records, offline_records)
        self.assertEqual(len(live_records), 1)
        record = live_records[0]
        self.assertEqual(record["latitude"], 46.2)
        self.assertEqual(record["longitude"], 7.3)
        self.assertEqual(record["pga"], 2.75)
        self.assertEqual(record["provider_value"], 2.75)
        self.assertEqual(record["provider_unit"], "cm/s^2")
        self.assertEqual(record["source"], "RRSM")
        self.assertTrue(math.isfinite(record["pga"]))
        self.assertGreater(record["pga"], 0)

    def test_context_timestamp_is_used_without_nested_event_access(self):
        context = EventContext.from_alert_mapping(
            {
                "unid": "rrsm-event",
                "lat": 45.0,
                "lon": 12.0,
                "mag": 5.5,
                "depth": 10.0,
                "time": "2026-08-10T10:11:12.250000Z",
            },
            scheduled_event_id="rrsm-event",
        )
        peak_motion = _peak_motion([
            _station([_channel("HNE", 1.0)])
        ])
        formatter = RRSMPeakMotionDataFormatter(
            logger=Mock(),
            configuration={"general": {"component-selection": "maximum-all"}},
        )

        with patch.object(
            peak_motion,
            "get_event_data",
            side_effect=AssertionError("nested RRSM event must not be read"),
        ) as nested_event:
            records = formatter.extract_raw_stations(
                event_data=context,
                amplitudes=peak_motion,
            )

        self.assertEqual(len(records), 1)
        self.assertEqual(
            records[0]["timestamp"],
            dataformatter.get_epoch_time(context.get_origin_time()),
        )
        nested_event.assert_not_called()

    def test_common_provenance_fields_are_required(self):
        self.assertIn(
            "provider_value", RawStationMeasurement.__required_keys__)
        self.assertIn(
            "provider_unit", RawStationMeasurement.__required_keys__)
        self.assertNotIn(
            "provider_value", RawStationMeasurement.__optional_keys__)
        self.assertNotIn(
            "provider_unit", RawStationMeasurement.__optional_keys__)

    def test_maximum_all_can_select_vertical_channel(self):
        records, _logger = self._extract([
            _station([
                _channel("HHE", 2.0),
                _channel("HHN", 3.0),
                _channel("HHZ", 9.0),
            ])
        ])

        self.assertEqual(records[0]["channel"], "HHZ")
        self.assertEqual(records[0]["provider_value"], 9.0)

    def test_maximum_horizontal_excludes_only_uppercase_z_suffix(self):
        records, _logger = self._extract([
            _station([
                _channel("HHZ", 20.0),
                _channel("HHE", 4.0),
                _channel("hhz", 8.0),
                _channel("Z12", 7.0),
            ])
        ], selection="maximum-horizontal")

        self.assertEqual(records[0]["channel"], "hhz")
        self.assertEqual(records[0]["provider_value"], 8.0)

    def test_unsupported_selection_logs_critical_and_uses_maximum_all(self):
        logger = Mock()
        records, _logger = self._extract([
            _station([
                _channel("HHE", 2.0),
                _channel("HHZ", 9.0),
            ])
        ], selection="largest-mystery", logger=logger)

        self.assertEqual(records[0]["channel"], "HHZ")
        logger.critical.assert_called_once()
        message = logger.critical.call_args.args[0]
        self.assertIn("RRSM", message)
        self.assertIn("unsupported", message)
        self.assertIn("maximum-all", message)
        self.assertEqual(
            logger.critical.call_args.args[1], "largest-mystery")

    def test_equal_maxima_keep_first_provider_channel(self):
        records, _logger = self._extract([
            _station([
                _channel("HHE", 4.0),
                _channel("HHN", 4.0),
            ])
        ])

        self.assertEqual(records[0]["channel"], "HHE")

    def test_inclusive_pga_endpoints_are_accepted(self):
        records, _logger = self._extract([
            _station(
                [_channel("HHE", RRSM_PEAKMOTION_PGA_MIN)],
                station="MIN",
            ),
            _station(
                [_channel("HHN", RRSM_PEAKMOTION_PGA_MAX)],
                station="MAX",
                latitude=46.0,
            ),
        ])

        self.assertEqual(len(records), 2)
        self.assertEqual(
            [record["provider_value"] for record in records],
            [RRSM_PEAKMOTION_PGA_MIN, RRSM_PEAKMOTION_PGA_MAX],
        )

    def test_values_immediately_outside_pga_limits_are_rejected(self):
        cases = (
            (
                math.nextafter(RRSM_PEAKMOTION_PGA_MIN, -math.inf),
                "below minimum",
            ),
            (
                math.nextafter(RRSM_PEAKMOTION_PGA_MAX, math.inf),
                "above maximum",
            ),
        )
        for provider_pga, expected_reason in cases:
            with self.subTest(provider_pga=provider_pga):
                records, logger = self._extract([
                    _station(
                        [_channel("HHE", provider_pga)],
                        station="LIMIT",
                    )
                ])

                self.assertEqual(records, [])
                warnings = self._warning_messages(logger)
                self.assertTrue(any(
                    "RRSM" in message
                    and "NW.LIMIT" in message
                    and "HHE" in message
                    and repr(provider_pga) in message
                    and expected_reason in message
                    for message in warnings
                ), warnings)

    def test_each_invalid_provider_pga_is_warned_and_rejected(self):
        cases = (
            (_MISSING, None, "missing PGA"),
            (None, None, "missing PGA"),
            ("not-a-number", "not-a-number", "nonnumeric PGA"),
            ("0.5", "0.5", "nonnumeric PGA"),
            (True, True, "nonnumeric PGA"),
            (float("nan"), float("nan"), "nonfinite PGA"),
            (float("inf"), float("inf"), "nonfinite PGA"),
            (float("-inf"), float("-inf"), "nonfinite PGA"),
            (0, 0, "zero PGA"),
            (-0.5, -0.5, "negative PGA"),
        )
        for provider_pga, diagnostic_value, expected_reason in cases:
            with self.subTest(provider_pga=provider_pga):
                records, logger = self._extract([
                    _station(
                        [_channel("HHE", provider_pga)],
                        station="BAD",
                    )
                ])

                self.assertEqual(records, [])
                warnings = self._warning_messages(logger)
                self.assertTrue(any(
                    "RRSM" in message
                    and "NW.BAD" in message
                    and "HHE" in message
                    and repr(diagnostic_value) in message
                    and expected_reason in message
                    for message in warnings
                ), warnings)

    def test_negative_provider_pga_is_never_made_positive(self):
        records, logger = self._extract([
            _station([_channel("HHE", -3.5)], station="NEGATIVE")
        ])

        self.assertEqual(records, [])
        warnings = self._warning_messages(logger)
        self.assertTrue(any(
            "RRSM" in message
            and "NW.NEGATIVE" in message
            and "HHE" in message
            and "-3.5" in message
            and "negative PGA" in message
            for message in warnings
        ), warnings)

    def test_invalid_component_does_not_discard_valid_sibling(self):
        records, logger = self._extract([
            _station([
                _channel("HHE", "invalid"),
                _channel("HHN", 1.5),
            ], station="SIBLING")
        ])

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["channel"], "HHN")
        self.assertEqual(records[0]["provider_value"], 1.5)
        self.assertTrue(any(
            "RRSM" in message
            and "NW.SIBLING" in message
            and "HHE" in message
            and "nonnumeric" in message
            for message in self._warning_messages(logger)
        ))

    def test_later_valid_station_survives_earlier_invalid_station(self):
        records, logger = self._extract([
            _station(
                [_channel("HHE", None)],
                station="EARLY",
            ),
            _station(
                [_channel("HHN", 2.0)],
                station="LATER",
                latitude=46.0,
            ),
        ])

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["station"], "LATER")
        self.assertTrue(any(
            "RRSM" in message and "NW.EARLY" in message
            for message in self._warning_messages(logger)
        ))

    def test_station_without_eligible_valid_channel_is_warned(self):
        cases = (
            ([], "maximum-all", "channel collection is empty"),
            (
                [_channel("HHE", 0)],
                "maximum-all",
                "all eligible channels are invalid",
            ),
            (
                [_channel("HHZ", 4.0)],
                "maximum-horizontal",
                "no channel is eligible under maximum-horizontal",
            ),
        )
        for channels, selection, expected_reason in cases:
            with self.subTest(selection=selection, reason=expected_reason):
                records, logger = self._extract(
                    [_station(channels, station="EMPTY")],
                    selection=selection,
                )

                self.assertEqual(records, [])
                warnings = self._warning_messages(logger)
                self.assertTrue(any(
                    "RRSM station NW.EMPTY" in message
                    and "no eligible valid component remains" in message
                    and expected_reason in message
                    for message in warnings
                ), warnings)

    def test_invalid_coordinates_reject_only_the_affected_station(self):
        cases = (
            (_MISSING, 12.0, "missing latitude"),
            (None, 12.0, "missing latitude"),
            ("north", 12.0, "nonnumeric latitude"),
            (float("nan"), 12.0, "nonfinite latitude"),
            (float("inf"), 12.0, "nonfinite latitude"),
            (float("-inf"), 12.0, "nonfinite latitude"),
            (90.1, 12.0, "latitude"),
            (-90.1, 12.0, "latitude"),
            (45.0, _MISSING, "missing longitude"),
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
                    _station(
                        [_channel("HHE", 2.0)],
                        station="COORD",
                        latitude=latitude,
                        longitude=longitude,
                    ),
                    _station(
                        [_channel("HHN", 1.0)],
                        station="LATER",
                        latitude=46.0,
                        longitude=13.0,
                    ),
                ])

                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["station"], "LATER")
                warnings = self._warning_messages(logger)
                self.assertTrue(any(
                    "RRSM station NW.COORD" in message
                    and expected_reason in message
                    for message in warnings
                ), warnings)

    def test_coordinate_boundaries_and_numeric_strings_are_accepted(self):
        records, _logger = self._extract([
            _station(
                [_channel("HHE", 1.0)],
                station="NORTHEAST",
                latitude=90,
                longitude=180,
            ),
            _station(
                [_channel("HHN", 1.0)],
                station="SOUTHWEST",
                latitude=-90,
                longitude=-180,
            ),
            _station(
                [_channel("HH1", 1.0)],
                station="STRINGS",
                latitude="45.5",
                longitude="12.5",
            ),
        ])

        self.assertEqual(len(records), 3)
        by_station = {record["station"]: record for record in records}
        self.assertEqual(
            (by_station["NORTHEAST"]["latitude"],
             by_station["NORTHEAST"]["longitude"]),
            (90.0, 180.0),
        )
        self.assertEqual(
            (by_station["SOUTHWEST"]["latitude"],
             by_station["SOUTHWEST"]["longitude"]),
            (-90.0, -180.0),
        )
        self.assertEqual(
            (by_station["STRINGS"]["latitude"],
             by_station["STRINGS"]["longitude"]),
            (45.5, 12.5),
        )

    def test_zero_to_360_longitude_is_rejected_without_rewrite(self):
        records, logger = self._extract([
            _station(
                [_channel("HHE", 1.0)],
                station="NO_REWRITE",
                longitude=270,
            )
        ])

        self.assertEqual(records, [])
        warnings = self._warning_messages(logger)
        self.assertTrue(any(
            "RRSM station NW.NO_REWRITE" in message
            and "longitude 270.0" in message
            and "outside [-180, 180]" in message
            for message in warnings
        ), warnings)

    def test_rrsm_coordinate_validation_is_independent_from_esm_helpers(self):
        with patch.object(
                ESMShakeMapDataFormatter,
                "_coordinate",
                side_effect=AssertionError("ESM coordinate helper was reused")):
            records, logger = self._extract([
                _station(
                    [_channel("HHE", 1.0)],
                    station="INVALID",
                    latitude=91,
                ),
                _station(
                    [_channel("HHN", 2.0)],
                    station="VALID",
                    latitude=46,
                ),
            ])

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["station"], "VALID")
        self.assertTrue(any(
            "RRSM station NW.INVALID" in message and "latitude" in message
            for message in self._warning_messages(logger)
        ))

    def test_provider_codes_are_safely_converted_without_rejection(self):
        cases = (
            (
                "null",
                {"network": None, "station": None, "location": None,
                 "channel": None},
                {"network": "", "station": "", "location": "",
                 "channel": ""},
            ),
            (
                "missing",
                {"network": _MISSING, "station": _MISSING,
                 "location": _MISSING, "channel": _MISSING},
                {"network": "", "station": "", "location": "",
                 "channel": ""},
            ),
            (
                "empty",
                {"network": "", "station": "", "location": "",
                 "channel": ""},
                {"network": "", "station": "", "location": "",
                 "channel": ""},
            ),
            (
                "numeric",
                {"network": 11, "station": 22, "location": 33,
                 "channel": 44},
                {"network": "11", "station": "22", "location": "33",
                 "channel": "44"},
            ),
        )
        for label, provider_codes, expected_codes in cases:
            with self.subTest(label=label):
                records, _logger = self._extract([
                    _station(
                        [_channel(provider_codes["channel"], 1.0)],
                        network=provider_codes["network"],
                        station=provider_codes["station"],
                        location=provider_codes["location"],
                    )
                ], selection="maximum-horizontal")

                self.assertEqual(len(records), 1)
                for field_name, expected_value in expected_codes.items():
                    self.assertEqual(
                        records[0][field_name], expected_value,
                        (label, field_name),
                    )

    def test_station_location_and_direct_compound_channel_are_retained(self):
        records, _logger = self._extract([
            _station(
                [_channel(".00.HHE", 1.0)],
                network=".NW",
                station=".STA",
                location=".01",
            )
        ])

        self.assertEqual(records[0]["network"], "NW")
        self.assertEqual(records[0]["station"], "STA")
        self.assertEqual(records[0]["location"], "01")
        self.assertEqual(records[0]["channel"], "00.HHE")

    def test_duplicate_station_codes_process_each_public_object_once(self):
        records, _logger = self._extract([
            _station(
                [_channel("HHE", 1.0)],
                network="N1",
                station="DUP",
                location="01",
                latitude=45,
                longitude=7,
            ),
            _station(
                [_channel("HHN", 2.0)],
                network="N2",
                station="DUP",
                location="02",
                latitude=46,
                longitude=8,
            ),
        ])

        self.assertEqual(len(records), 2)
        self.assertEqual(
            [(record["network"], record["station"], record["channel"])
             for record in records],
            [("N1", "DUP", "HHE"), ("N2", "DUP", "HHN")],
        )

    def test_provenance_survives_noncolliding_station_merger_handoff(self):
        records, _logger = self._extract([
            _station(
                [_channel("HHE", 3.0)],
                network="NW",
                station="MERGE",
                location="01",
            )
        ])

        merged = StationMerger(
            service_priority=[RRSM_PEAK_MOTION_SERVICE]
        ).merge({RRSM_PEAK_MOTION_SERVICE: list(records)})

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["source"], "RRSM")
        self.assertEqual(merged[0]["provider_value"], 3.0)
        self.assertEqual(merged[0]["provider_unit"], "cm/s^2")


if __name__ == "__main__":
    unittest.main()
