"""Offline tests for common FinDer channel assembly and data_0 formatting."""

import atexit
from copy import deepcopy
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


_PARAMWS_LOG_DIRECTORY = tempfile.TemporaryDirectory(
    prefix="pyfinder-input-generation-unit-"
)
atexit.register(_PARAMWS_LOG_DIRECTORY.cleanup)
_original_paramws_log_file = os.environ.get("PARAMWS_LOG_FILE")
os.environ["PARAMWS_LOG_FILE"] = str(
    Path(_PARAMWS_LOG_DIRECTORY.name) / "paramws.log"
)
try:
    from pyfinder import finderexec
    from pyfinder.finderutils import FinderChannel, FinderChannelList
    from pyfinder.utils.dataformatter import FinDerInputFormatter
finally:
    if _original_paramws_log_file is None:
        os.environ.pop("PARAMWS_LOG_FILE", None)
    else:
        os.environ["PARAMWS_LOG_FILE"] = _original_paramws_log_file


class ControlledEvent:
    """Supply one authoritative common event context to the executable."""

    def __init__(
        self,
        *,
        latitude=46.2,
        longitude=7.3,
        magnitude=5.6,
        depth=10.0,
        origin_time="2026-08-10T08:15:30.250000Z",
    ):
        self.latitude = latitude
        self.longitude = longitude
        self.magnitude = magnitude
        self.depth = depth
        self.origin_time = origin_time

    def get_latitude(self):
        return self.latitude

    def get_longitude(self):
        return self.longitude

    def get_magnitude(self):
        return self.magnitude

    def get_depth(self):
        return self.depth

    def get_origin_time(self):
        return self.origin_time


class FinDerInputGenerationTests(unittest.TestCase):
    # ``data_0`` preserves supplied finite floats with enough significant
    # digits for a normal double-precision round trip. A very small absolute
    # tolerance protects low positive amplitudes without making a material
    # scientific difference invisible.
    SERIALIZED_NUMBER_REL_TOLERANCE = 1e-12
    SERIALIZED_NUMBER_ABS_TOLERANCE = 1e-20

    # Companion distances are intentionally serialized to one decimal place.
    # Half of that display unit is therefore the largest legitimate difference
    # between the independent full-precision expectation and the parsed text.
    COMPANION_DISTANCE_ABS_TOLERANCE_KM = 0.05

    def executable(self, *, live_mode=False, margin_percent=1.0):
        return finderexec.FinDerExecutable(
            options={"command_line_args": "input-generation-test"},
            configuration={
                "finder-executable": {
                    "path": "/not-executed/finder_run",
                    "output-root-folder": "/not-created/finder-output",
                    "finder-live-mode": live_mode,
                    "artificial-point-margin-percent": margin_percent,
                }
            },
            finder_configuration_name="global",
            finder_configuration={"DATA_FOLDER": "unused"},
            logger=mock.Mock(),
        )

    @staticmethod
    def observations():
        return [
            {
                "latitude": 46.10000000000001,
                "longitude": 7.2,
                "network": "CH",
                "station": "FIRST",
                "location": "",
                "channel": "HHE",
                "pga": 12.5,
                "timestamp": 111.25,
                "source": "ESM",
                "provider_value": 1.25,
                "provider_unit": "%g",
            },
            {
                "latitude": 45.9,
                "longitude": 8.4,
                "network": "IV",
                "station": "SECOND",
                "location": "01",
                "channel": "HNZ",
                "pga": 0.00000001,
                "timestamp": 999999.75,
                "source": "unrecognized-provenance-is-irrelevant",
                "provider_value": 0.00000001,
                "provider_unit": "cm/s^2",
            },
        ]

    @staticmethod
    def channels(*, pgas=(12.5, 0.00000001)):
        return FinderChannelList([
            FinderChannel(
                latitude=46.10000000000001,
                longitude=7.2,
                network_code="CH",
                station_code="FIRST",
                location_code="",
                channel_code="HHE",
                pga=pgas[0],
                is_artificial=False,
            ),
            FinderChannel(
                latitude=45.9,
                longitude=8.4,
                network_code="IV",
                station_code="SECOND",
                location_code="01",
                channel_code="HNZ",
                pga=pgas[1],
                is_artificial=False,
            ),
        ])

    def assert_serialized_number_close(self, actual_text, expected):
        """Compare one serialized finite number by value, not text spelling."""
        actual = float(actual_text)
        self.assertTrue(math.isfinite(actual), actual_text)
        self.assertTrue(
            math.isclose(
                actual,
                expected,
                rel_tol=self.SERIALIZED_NUMBER_REL_TOLERANCE,
                abs_tol=self.SERIALIZED_NUMBER_ABS_TOLERANCE,
            ),
            (actual, expected),
        )

    def assert_live_data_0(self, rendered, *, event_time, expected_rows):
        """Verify live ``data_0`` structure and parsed scientific values."""
        lines = rendered.decode("ascii").splitlines()
        self.assertEqual(lines[0], "# {0} 0".format(event_time))
        self.assertEqual(len(lines), len(expected_rows) + 1)

        for line, expected in zip(lines[1:], expected_rows):
            fields = line.split()
            self.assertEqual(len(fields), 5)
            latitude, longitude, sncl, timestamp, pga = fields
            self.assert_serialized_number_close(latitude, expected["latitude"])
            self.assert_serialized_number_close(longitude, expected["longitude"])
            self.assertEqual(sncl, expected["sncl"])
            self.assertEqual(timestamp, str(event_time))
            self.assert_serialized_number_close(pga, expected["pga"])

    def assert_non_live_data_0(self, rendered, *, event_time, expected_rows):
        """Verify non-live structure and independently calculated logarithms."""
        lines = rendered.decode("ascii").splitlines()
        self.assertEqual(lines[0], "# {0} 0".format(event_time))
        self.assertEqual(len(lines), len(expected_rows) + 1)

        for line, expected in zip(lines[1:], expected_rows):
            fields = line.split()
            self.assertEqual(len(fields), 3)
            latitude, longitude, log10_pga = fields
            self.assert_serialized_number_close(latitude, expected["latitude"])
            self.assert_serialized_number_close(longitude, expected["longitude"])
            self.assert_serialized_number_close(
                log10_pga,
                math.log10(expected["pga"]),
            )

    def assert_companion(self, rendered, expected_rows):
        """Verify companion structure, ordering, and parsed numeric meaning."""
        lines = rendered.decode("ascii").splitlines()
        self.assertEqual(lines[0], "# SNCL PGA_CM_S2 EPI_DISTANCE_KM")
        self.assertEqual(len(lines), len(expected_rows) + 1)

        for line, expected in zip(lines[1:], expected_rows):
            fields = line.split()
            self.assertEqual(len(fields), 3)
            sncl, pga, distance = fields
            self.assertEqual(sncl, expected["sncl"])
            self.assert_serialized_number_close(pga, expected["pga"])
            self.assertRegex(distance, r"^-?\d+\.\d$")
            self.assertTrue(
                math.isclose(
                    float(distance),
                    expected["distance_km"],
                    rel_tol=0.0,
                    abs_tol=self.COMPANION_DISTANCE_ABS_TOLERANCE_KM,
                ),
                (distance, expected["distance_km"]),
            )

    def test_real_channel_assembly_preserves_membership_order_values_and_input(self):
        observations = self.observations()
        original = deepcopy(observations)

        channels = self.executable()._build_real_finder_channels(observations)

        self.assertIsInstance(channels, FinderChannelList)
        self.assertEqual(len(channels), 2)
        self.assertEqual(
            [channel.get_sncl() for channel in channels],
            ["CH.FIRST..HHE", "IV.SECOND.01.HNZ"],
        )
        self.assertEqual(
            [channel.pga for channel in channels],
            [12.5, 0.00000001],
        )
        self.assertTrue(all(channel.is_artificial is False
                            for channel in channels))
        self.assertEqual(observations, original)

    def test_source_never_selects_channel_assembly_behavior(self):
        observations = self.observations()
        observations[0]["source"] = "RRSM"
        observations[1]["source"] = "EMSC-FELT"

        channels = self.executable()._build_real_finder_channels(observations)

        self.assertEqual(
            [channel.get_sncl() for channel in channels],
            ["CH.FIRST..HHE", "IV.SECOND.01.HNZ"],
        )
        self.assertEqual([channel.pga for channel in channels], [12.5, 1e-8])

    def test_real_channel_assembly_rejects_empty_non_list_and_invalid_records(self):
        executable = self.executable()
        with self.assertRaisesRegex(TypeError, "as a list"):
            executable._build_real_finder_channels(tuple(self.observations()))
        with self.assertRaisesRegex(ValueError, "at least one"):
            executable._build_real_finder_channels([])
        with self.assertRaisesRegex(TypeError, "must be a mapping"):
            executable._build_real_finder_channels([object()])

    def test_duplicate_and_malformed_final_sncl_fail_at_formatter_boundary(self):
        duplicate = self.observations()
        duplicate[1].update({
            "network": "CH",
            "station": "FIRST",
            "location": "",
            "channel": "HHE",
        })
        duplicate_channels = self.executable()._build_real_finder_channels(
            duplicate
        )
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            FinDerInputFormatter.format(duplicate_channels, 1000.0, False)

        cases = (
            ("network", ""),
            ("station", "BAD.CODE"),
            ("location", "é"),
            ("channel", "HN Z"),
            ("channel", "HN\x00"),
            ("station", "BAD/CODE"),
        )
        for field_name, value in cases:
            with self.subTest(field_name=field_name, value=value):
                observations = self.observations()[:1]
                observations[0][field_name] = value
                channels = self.executable()._build_real_finder_channels(
                    observations
                )
                with self.assertRaises(ValueError):
                    FinDerInputFormatter.format(channels, 1000.0, False)
                self.assertEqual(observations[0][field_name], value)

    def test_artificial_point_uses_stored_margin_and_observed_maximum(self):
        real_channels = self.channels(pgas=(10.0, 2.0))
        original_channels = list(real_channels)
        executable = self.executable(margin_percent=7.5)

        with mock.patch.object(
            finderexec.Calculator,
            "predict_PGA_from_magnitude",
            return_value=3.0,
        ) as predict:
            completed = executable.add_artificial_observation_point(
                real_channels,
                ControlledEvent(magnitude=5.0, depth=8.0),
            )

        predict.assert_called_once_with(5.0, 8.0, log_scale=False)
        self.assertEqual(executable.artificial_point_margin_percent, 7.5)
        self.assertIsNot(completed, real_channels)
        self.assertEqual(list(real_channels), original_channels)
        self.assertEqual(completed[1:], original_channels)
        artificial = completed[0]
        self.assertEqual(artificial.get_sncl(), "XX.NONE.00.HNZ")
        self.assertEqual(artificial.latitude, 46.2)
        self.assertEqual(artificial.longitude, 7.3)
        self.assertEqual(artificial.pga, 10.75)
        self.assertTrue(artificial.is_artificial)

    def test_zero_margin_and_prediction_dominant_values_remain_linear(self):
        cases = (
            (0.0, 2.0, 10.0, 10.0),
            (4.0, 20.0, 10.0, 20.0),
        )
        for margin, prediction, observed, expected in cases:
            with self.subTest(margin=margin, prediction=prediction):
                executable = self.executable(margin_percent=margin)
                real_channels = self.channels(pgas=(observed, 1.0))
                with mock.patch.object(
                    finderexec.Calculator,
                    "predict_PGA_from_magnitude",
                    return_value=prediction,
                ):
                    completed = executable.add_artificial_observation_point(
                        real_channels,
                        ControlledEvent(),
                    )
                self.assertEqual(completed[0].pga, expected)
                self.assertEqual([channel.pga for channel in real_channels],
                                 [observed, 1.0])

    def test_invalid_predictions_fail_visibly(self):
        for prediction in (None, True, "12", 0.0, -1.0, math.nan, math.inf):
            with self.subTest(prediction=prediction):
                with mock.patch.object(
                    finderexec.Calculator,
                    "predict_PGA_from_magnitude",
                    return_value=prediction,
                ):
                    with self.assertRaises(ValueError):
                        self.executable().add_artificial_observation_point(
                            self.channels(),
                            ControlledEvent(),
                        )

    def test_invalid_observed_margin_result_fails_visibly(self):
        with mock.patch.object(
            finderexec.Calculator,
            "predict_PGA_from_magnitude",
            return_value=1.0,
        ):
            with self.assertRaisesRegex(ValueError, "observed-margin"):
                self.executable(
                    margin_percent=100.0,
                ).add_artificial_observation_point(
                    self.channels(pgas=(1e308, 1.0)),
                    ControlledEvent(),
                )

    def test_artificial_sncl_collision_fails_at_formatter_boundary(self):
        real_channels = FinderChannelList([
            FinderChannel(
                latitude=45.0,
                longitude=8.0,
                sncl="XX.NONE.00.HNZ",
                pga=2.0,
                is_artificial=False,
            )
        ])
        with mock.patch.object(
            finderexec.Calculator,
            "predict_PGA_from_magnitude",
            return_value=3.0,
        ):
            completed = self.executable().add_artificial_observation_point(
                real_channels,
                ControlledEvent(),
            )
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                FinDerInputFormatter.format(completed, 1000.0, False)

    def test_invalid_final_data_fails_before_data_0_is_written(self):
        cases = (
            ("duplicate SNCL", {"duplicate": True}),
            ("malformed SNCL", {"station": "BAD.CODE"}),
            ("invalid coordinates", {"latitude": 91.0}),
            ("invalid PGA", {"pga": 0.0}),
        )
        for label, changes in cases:
            with self.subTest(label=label):
                executable = self.executable()
                temporary_directory = tempfile.TemporaryDirectory(
                    prefix="pyfinder-invalid-final-input-unit-"
                )
                self.addCleanup(temporary_directory.cleanup)
                executable.working_directory = temporary_directory.name
                observations = self.observations()
                if changes.get("duplicate", False):
                    observations[1].update({
                        "network": observations[0]["network"],
                        "station": observations[0]["station"],
                        "location": observations[0]["location"],
                        "channel": observations[0]["channel"],
                    })
                else:
                    observations[0].update(changes)

                with mock.patch.object(
                    finderexec.Calculator,
                    "predict_PGA_from_magnitude",
                    return_value=3.0,
                ):
                    with self.assertRaises(ValueError):
                        executable._write_data_for_finder(
                            observations,
                            ControlledEvent(),
                        )

                self.assertFalse(
                    (Path(temporary_directory.name) / "data_0").exists()
                )
                self.assertFalse(
                    (
                        Path(temporary_directory.name)
                        / "pyfinder_amplitudes_to_Finder.txt"
                    ).exists()
                )

    def test_production_writer_calls_explicit_artificial_method_once(self):
        executable = self.executable(live_mode=True)
        temporary_directory = tempfile.TemporaryDirectory(
            prefix="pyfinder-input-writer-unit-"
        )
        self.addCleanup(temporary_directory.cleanup)
        executable.working_directory = temporary_directory.name
        observations = self.observations()
        real_channels = self.channels()
        completed_channels = FinderChannelList([
            FinderChannel(
                latitude=46.2,
                longitude=7.3,
                sncl="XX.NONE.00.HNZ",
                pga=20.0,
                is_artificial=True,
            ),
            *real_channels,
        ])

        event_data = ControlledEvent()
        with mock.patch.object(
            executable,
            "_build_real_finder_channels",
            return_value=real_channels,
        ) as build, mock.patch.object(
            executable,
            "add_artificial_observation_point",
            return_value=completed_channels,
        ) as add_artificial, mock.patch.object(
            finderexec,
            "get_epoch_time",
            return_value=1000.75,
        ), mock.patch.object(
            finderexec.FinDerInputFormatter,
            "format",
            return_value=b"controlled-data",
        ) as format_data, mock.patch.object(
            executable,
            "_render_amplitude_companion",
            return_value=b"controlled-companion",
        ) as render_companion:
            data_path, returned_channels = executable._write_data_for_finder(
                observations,
                event_data,
            )

        build.assert_called_once_with(observations)
        add_artificial.assert_called_once_with(real_channels, mock.ANY)
        format_data.assert_called_once_with(
            finder_channels=completed_channels,
            event_time_epoch=1000.75,
            is_live_mode=True,
        )
        render_companion.assert_called_once_with(
            finder_channels=completed_channels,
            event_latitude=46.2,
            event_longitude=7.3,
        )
        self.assertIs(returned_channels, completed_channels)
        self.assertEqual(Path(data_path).read_bytes(), b"controlled-data")
        companion_path = (
            Path(temporary_directory.name)
            / "pyfinder_amplitudes_to_Finder.txt"
        )
        self.assertEqual(companion_path.read_bytes(), b"controlled-companion")

    def test_production_writer_materializes_non_live_data(self):
        executable = self.executable(live_mode=False)
        temporary_directory = tempfile.TemporaryDirectory(
            prefix="pyfinder-input-non-live-unit-"
        )
        self.addCleanup(temporary_directory.cleanup)
        executable.working_directory = temporary_directory.name

        with mock.patch.object(
            finderexec.Calculator,
            "predict_PGA_from_magnitude",
            return_value=20.0,
        ), mock.patch.object(
            finderexec,
            "get_epoch_time",
            return_value=1000.75,
        ):
            data_path, completed_channels = executable._write_data_for_finder(
                self.observations(),
                ControlledEvent(),
            )

        self.assert_non_live_data_0(
            Path(data_path).read_bytes(),
            event_time=1000,
            expected_rows=[
                {"latitude": 46.2, "longitude": 7.3, "pga": 20.0},
                {"latitude": 46.10000000000001, "longitude": 7.2,
                 "pga": 12.5},
                {"latitude": 45.9, "longitude": 8.4, "pga": 1e-8},
            ],
        )
        self.assertEqual(len(completed_channels), 3)
        self.assertEqual(completed_channels[0].get_sncl(), "XX.NONE.00.HNZ")
        self.assertTrue(
            (Path(temporary_directory.name)
             / "pyfinder_amplitudes_to_Finder.txt").is_file()
        )

    def test_companion_values_sort_stably_without_mutating_channels_or_data(self):
        channels = FinderChannelList([
            FinderChannel(
                latitude=0.0,
                longitude=0.0,
                sncl="XX.NONE.00.HNZ",
                pga=12.5,
                is_artificial=True,
            ),
            FinderChannel(
                latitude=0.0,
                longitude=0.0,
                sncl="IV.LOW.00.HNZ",
                pga=1e-12,
                is_artificial=False,
            ),
            FinderChannel(
                latitude=0.0,
                longitude=1.0,
                sncl="CH.FAR.00.HNZ",
                pga=12.5,
                is_artificial=False,
            ),
            FinderChannel(
                latitude=0.0,
                longitude=0.5,
                sncl="FR.MID.00.HNZ",
                pga=2.0,
                is_artificial=False,
            ),
        ])
        original_channels = list(channels)
        original_state = [vars(channel).copy() for channel in channels]
        original_data = FinDerInputFormatter.format(channels, 1000.0, True)

        rendered = self.executable()._render_amplitude_companion(
            finder_channels=channels,
            event_latitude=0.0,
            event_longitude=0.0,
        )

        self.assert_companion(
            rendered,
            [
                {"sncl": "XX.NONE.00.HNZ", "pga": 12.5,
                 "distance_km": 0.0},
                {"sncl": "CH.FAR.00.HNZ", "pga": 12.5,
                 "distance_km": 6371.0 * math.radians(1.0)},
                {"sncl": "FR.MID.00.HNZ", "pga": 2.0,
                 "distance_km": 6371.0 * math.radians(0.5)},
                {"sncl": "IV.LOW.00.HNZ", "pga": 1e-12,
                 "distance_km": 0.0},
            ],
        )
        self.assertEqual(list(channels), original_channels)
        self.assertEqual(
            [vars(channel).copy() for channel in channels],
            original_state,
        )
        expected_data_rows = [
            {"latitude": 0.0, "longitude": 0.0,
             "sncl": "XX.NONE.00.HNZ", "pga": 12.5},
            {"latitude": 0.0, "longitude": 0.0,
             "sncl": "IV.LOW.00.HNZ", "pga": 1e-12},
            {"latitude": 0.0, "longitude": 1.0,
             "sncl": "CH.FAR.00.HNZ", "pga": 12.5},
            {"latitude": 0.0, "longitude": 0.5,
             "sncl": "FR.MID.00.HNZ", "pga": 2.0},
        ]
        self.assert_live_data_0(
            original_data,
            event_time=1000,
            expected_rows=expected_data_rows,
        )
        self.assert_live_data_0(
            FinDerInputFormatter.format(channels, 1000.0, True),
            event_time=1000,
            expected_rows=expected_data_rows,
        )
        self.assertEqual(
            [channel.get_sncl() for channel in channels],
            [
                "XX.NONE.00.HNZ",
                "IV.LOW.00.HNZ",
                "CH.FAR.00.HNZ",
                "FR.MID.00.HNZ",
            ],
        )

    def test_companion_renders_supplied_real_only_membership_in_both_modes(self):
        real_channels = FinderChannelList([
            FinderChannel(
                latitude=0.0,
                longitude=1.0,
                sncl="CH.FIRST.00.HNZ",
                pga=2.0,
                is_artificial=False,
            ),
            FinderChannel(
                latitude=0.0,
                longitude=0.0,
                sncl="IV.SECOND.00.HNZ",
                pga=4.0,
                is_artificial=False,
            ),
        ])
        non_live = self.executable(live_mode=False)
        live = self.executable(live_mode=True)

        non_live_rendered = non_live._render_amplitude_companion(
            real_channels,
            0.0,
            0.0,
        )
        live_rendered = live._render_amplitude_companion(
            real_channels,
            0.0,
            0.0,
        )

        expected_rows = [
            {"sncl": "IV.SECOND.00.HNZ", "pga": 4.0,
             "distance_km": 0.0},
            {"sncl": "CH.FIRST.00.HNZ", "pga": 2.0,
             "distance_km": 6371.0 * math.radians(1.0)},
        ]
        self.assert_companion(non_live_rendered, expected_rows)
        self.assert_companion(live_rendered, expected_rows)
        self.assertNotIn(b"XX.NONE.00.HNZ", non_live_rendered)

    def test_invalid_companion_distance_fails_visibly(self):
        invalid_results = (None, True, "12.0", math.nan, math.inf, -0.1)
        for result in invalid_results:
            with self.subTest(result=result), mock.patch.object(
                finderexec.Calculator,
                "haversine",
                return_value=result,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "FinDer amplitude companion distance",
                ):
                    self.executable()._render_amplitude_companion(
                        self.channels(),
                        0.0,
                        0.0,
                    )

        with mock.patch.object(
            finderexec.Calculator,
            "haversine",
            side_effect=TypeError("controlled Haversine failure"),
        ):
            with self.assertRaisesRegex(
                ValueError,
                "distance calculation failed",
            ):
                self.executable()._render_amplitude_companion(
                    self.channels(),
                    0.0,
                    0.0,
                )

    def test_companion_render_failure_does_not_refresh_either_data_artifact(self):
        executable = self.executable()
        temporary_directory = tempfile.TemporaryDirectory(
            prefix="pyfinder-companion-render-failure-unit-"
        )
        self.addCleanup(temporary_directory.cleanup)
        executable.working_directory = temporary_directory.name
        data_path = Path(temporary_directory.name) / "data_0"
        companion_path = (
            Path(temporary_directory.name)
            / "pyfinder_amplitudes_to_Finder.txt"
        )
        data_path.write_bytes(b"stale-data")
        companion_path.write_bytes(b"stale-companion")

        with mock.patch.object(
            finderexec.Calculator,
            "predict_PGA_from_magnitude",
            return_value=20.0,
        ), mock.patch.object(
            finderexec.Calculator,
            "haversine",
            return_value=math.nan,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "FinDer amplitude companion distance",
            ):
                executable._write_data_for_finder(
                    self.observations(),
                    ControlledEvent(),
                )

        self.assertEqual(data_path.read_bytes(), b"stale-data")
        self.assertEqual(companion_path.read_bytes(), b"stale-companion")

    def test_formatter_serializes_supplied_real_only_list_without_artificial_point(self):
        real_channels = self.channels(pgas=(12.5, 1e-8))

        rendered = FinDerInputFormatter.format(
            finder_channels=real_channels,
            event_time_epoch=1000.75,
            is_live_mode=True,
        )

        self.assert_live_data_0(
            rendered,
            event_time=1000,
            expected_rows=[
                {"latitude": 46.10000000000001, "longitude": 7.2,
                 "sncl": "CH.FIRST..HHE", "pga": 12.5},
                {"latitude": 45.9, "longitude": 8.4,
                 "sncl": "IV.SECOND.01.HNZ", "pga": 1e-8},
            ],
        )
        self.assertEqual(len(rendered.splitlines()), 3)
        self.assertNotIn(b"XX.NONE.00.HNZ", rendered)
        self.assertEqual([channel.pga for channel in real_channels],
                         [12.5, 1e-8])

    def test_live_formatter_uses_common_timestamp_linear_pga_and_exact_order(self):
        channels = self.channels(pgas=(12.5, 1e-8))

        rendered = FinDerInputFormatter.format(
            finder_channels=channels,
            event_time_epoch=2000.99,
            is_live_mode=True,
        )

        self.assert_live_data_0(
            rendered,
            event_time=2000,
            expected_rows=[
                {"latitude": 46.10000000000001, "longitude": 7.2,
                 "sncl": "CH.FIRST..HHE", "pga": 12.5},
                {"latitude": 45.9, "longitude": 8.4,
                 "sncl": "IV.SECOND.01.HNZ", "pga": 1e-8},
            ],
        )
        self.assertEqual([channel.get_sncl() for channel in channels],
                         ["CH.FIRST..HHE", "IV.SECOND.01.HNZ"])
        self.assertEqual([channel.pga for channel in channels], [12.5, 1e-8])

    def test_non_live_formatter_calculates_log10_only_in_output(self):
        channels = self.channels(pgas=(100.0, 0.01))

        rendered = FinDerInputFormatter.format(
            finder_channels=channels,
            event_time_epoch=3000.25,
            is_live_mode=False,
        )

        self.assert_non_live_data_0(
            rendered,
            event_time=3000,
            expected_rows=[
                {"latitude": 46.10000000000001, "longitude": 7.2,
                 "pga": 100.0},
                {"latitude": 45.9, "longitude": 8.4, "pga": 0.01},
            ],
        )
        self.assertEqual([channel.pga for channel in channels], [100.0, 0.01])

    def test_formatter_rejects_invalid_channel_values_and_duplicate_sncl(self):
        invalid_values = (
            ("latitude", True),
            ("latitude", 91.0),
            ("longitude", -181.0),
            ("longitude", math.nan),
            ("pga", "1.0"),
            ("pga", 0.0),
            ("pga", -1.0),
            ("pga", math.inf),
        )
        for field_name, value in invalid_values:
            with self.subTest(field_name=field_name, value=value):
                channels = self.channels()
                setattr(channels[0], field_name, value)
                with self.assertRaises(ValueError):
                    FinDerInputFormatter.format(channels, 1000.0, False)

        channels = self.channels()
        channels[1].set_sncl("CH.FIRST..HHE")
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            FinDerInputFormatter.format(channels, 1000.0, False)

    def test_formatter_rejects_invalid_timestamp_mode_list_and_sncl(self):
        for timestamp in (None, True, "1000", math.nan, math.inf):
            with self.subTest(timestamp=timestamp):
                with self.assertRaises(ValueError):
                    FinDerInputFormatter.format(
                        self.channels(), timestamp, False
                    )
        with self.assertRaises(ValueError):
            FinDerInputFormatter.format(self.channels(), 1000.0, "no")
        with self.assertRaises(TypeError):
            FinDerInputFormatter.format(list(self.channels()), 1000.0, False)

        malformed = self.channels()
        malformed[0].network = "C.H"
        with self.assertRaises(ValueError):
            FinDerInputFormatter.format(malformed, 1000.0, False)

if __name__ == "__main__":
    unittest.main()
