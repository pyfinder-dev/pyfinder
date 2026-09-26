"""Offline checks for native input serialization from real FinDer models."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import math
import unittest
from unittest.mock import patch
from xml.etree import ElementTree

from pyfinder.finderutils import (
    FinderChannel, FinderChannelList, FinderEvent, FinderRupture, FinderSolution,
)
from pyfinder.utils.shakemap import ShakeMapExporter


NS = {"s": "ch.ethz.sed.shakemap.usgs.xml"}
ORIGIN = datetime(2026, 8, 10, 8, 15, 30, 250000, tzinfo=timezone.utc)


def make_solution():
    rupture = FinderRupture()
    # Unequal top/bottom depths describe a finite surface for the native reader.
    for point in [(46.0, 7.0, 1.0), (46.2, 7.2, 1.0),
                  (46.2, 7.2, 12.0), (46.0, 7.0, 12.0)]:
        rupture.add_point(*point)
    return FinderSolution(
        event_id="upstream-id", finder_event_id="internal-id", rupture=rupture,
        event=FinderEvent(origin_time_epoch=123456789, latitude=46.1,
                          longitude=7.1, magnitude=5.6, depth=8.0),
        channels=FinderChannelList([
            FinderChannel(latitude=46.1, longitude=7.1, sncl="XX.NONE.00.HNZ",
                          pga=98.0665, is_artificial=True),
            FinderChannel(latitude=46.2, longitude=7.2, sncl="CH.REAL..HNE", pga=9.80665),
            FinderChannel(latitude=46.3, longitude=7.3, sncl="FF.PSEUDO.00.0", pga=0.00000980665),
        ]),
    )


class ShakeMapInputTests(unittest.TestCase):
    def export(self, solution=None, **kwargs):
        return ShakeMapExporter(solution if solution is not None else make_solution(),
                                kwargs.get("event_id", "caller-id"),
                                kwargs.get("origin_time", ORIGIN)).export_all()

    def test_native_basenames_bytes_and_repeatable_export(self):
        exporter = ShakeMapExporter(make_solution(), "caller-id", ORIGIN)
        files = exporter.export_all()
        self.assertEqual(set(files), {"event.xml", "event_dat.xml", "rupture.json"})
        self.assertTrue(all(isinstance(payload, bytes) for payload in files.values()))
        self.assertEqual(files, exporter.export_all())
        ElementTree.fromstring(files["event.xml"])
        ElementTree.fromstring(files["event_dat.xml"])
        json.loads(files["rupture.json"])

    def test_caller_identity_and_physical_time_leave_finder_values_intact(self):
        solution = make_solution()
        event = ElementTree.fromstring(self.export(solution)["event.xml"])
        self.assertEqual(event.get("id"), "caller-id")
        self.assertEqual(event.get("event_id"), "caller-id")
        self.assertEqual(event.get("time"), "2026-08-10T08:15:30.250000Z")
        for attribute, expected in {"lat": 46.1, "lon": 7.1, "mag": 5.6, "depth": 8.0}.items():
            self.assertEqual(float(event.get(attribute)), expected)
        self.assertEqual(solution.event.origin_time_epoch, 123456789)
        self.assertEqual(solution.get_finder_event_id(), "internal-id")
        self.assertEqual(solution.get_event_id(), "upstream-id")

    def test_aware_time_normalizes_to_utc(self):
        local = ORIGIN.astimezone(timezone(timedelta(hours=2)))
        event = ElementTree.fromstring(self.export(origin_time=local)["event.xml"])
        self.assertEqual(event.get("time"), "2026-08-10T08:15:30.250000Z")

    def test_selected_membership_order_component_direction_and_percent_g(self):
        solution = make_solution()
        other = make_solution()
        other.channels.clear()
        solution.set_input_solution(other)
        stations = ElementTree.fromstring(self.export(solution)["event_dat.xml"]).findall("s:station", NS)
        self.assertEqual([station.get("code") for station in stations], ["NONE", "REAL", "PSEUDO"])
        self.assertEqual([station.get("loc") for station in stations], ["00", "", "00"])
        self.assertEqual([station.find("s:comp", NS).get("name") for station in stations],
                         ["HNZ", "HNE", "0"])
        self.assertTrue(solution.channels[0].is_artificial())
        for station, expected in zip(stations, [10.0, 1.0, 0.000001]):
            amplitude = station.find("s:comp/s:acc", NS)
            self.assertEqual(amplitude.get("units"), "%g")
            # A tight relative tolerance allows only float serialization noise.
            self.assertTrue(math.isclose(float(amplitude.get("value")), expected, rel_tol=1e-14))
            self.assertGreater(float(amplitude.get("value")), 0)
            self.assertIsNone(station.get("name"))

    def test_no_artificial_channel_is_added_when_absent(self):
        solution = make_solution()
        solution.channels.pop(0)
        stations = ElementTree.fromstring(self.export(solution)["event_dat.xml"]).findall("s:station", NS)
        self.assertEqual([station.get("code") for station in stations], ["REAL", "PSEUDO"])

    def test_rupture_order_depth_and_closure_in_generated_copy_only(self):
        solution = make_solution()
        original = solution.rupture.get_points()
        payload = json.loads(self.export(solution)["rupture.json"])
        self.assertEqual(payload["type"], "FeatureCollection")
        self.assertTrue(payload["metadata"]["reference"])
        self.assertEqual(len(payload["features"]), 1)
        geometry = payload["features"][0]["geometry"]
        self.assertEqual(geometry["type"], "MultiPolygon")
        self.assertEqual(geometry["coordinates"], [[[
            [7.0, 46.0, 1.0], [7.2, 46.2, 1.0], [7.2, 46.2, 12.0],
            [7.0, 46.0, 12.0], [7.0, 46.0, 1.0],
        ]]])
        self.assertEqual(solution.rupture.get_points(), original)

    def test_already_closed_rupture_and_equal_depth_are_not_repaired(self):
        solution = make_solution()
        solution.rupture = FinderRupture()
        points = [(46.0, 7.0, 5.0), (46.2, 7.2, 5.0), (46.0, 7.0, 5.0)]
        for point in points:
            solution.rupture.add_point(*point)
        geometry = json.loads(self.export(solution)["rupture.json"])["features"][0]["geometry"]
        self.assertEqual(geometry["coordinates"], [[[
            [7.0, 46.0, 5.0], [7.2, 46.2, 5.0], [7.0, 46.0, 5.0],
        ]]])
        self.assertEqual(solution.rupture.get_points(), points)

    def test_absent_rupture_omits_optional_file(self):
        solution = make_solution()
        solution.rupture = None
        self.assertEqual(set(self.export(solution)), {"event.xml", "event_dat.xml"})

    def test_xml_special_characters_are_escaped_without_changing_identity(self):
        solution = make_solution()
        special = 'A&B<"station">'
        solution.channels[1].set_station_code(special)
        files = self.export(solution, event_id=special)
        self.assertEqual(ElementTree.fromstring(files["event.xml"]).get("id"), special)
        stations = ElementTree.fromstring(files["event_dat.xml"]).findall("s:station", NS)
        self.assertEqual(stations[1].get("code"), special)

    def test_missing_identity_solution_and_physical_time_fail(self):
        for invalid in [None, object()]:
            with self.subTest(solution=invalid), self.assertRaises(TypeError):
                ShakeMapExporter(invalid, "event", ORIGIN)
        for invalid in [None, "", "  ", 1, "bad\x00id"]:
            with self.subTest(event_id=invalid), self.assertRaises(ValueError):
                self.export(event_id=invalid)
        for invalid in [None, "2026-08-10T08:15:30Z", 123456789]:
            with self.subTest(origin_time=invalid), self.assertRaises(TypeError):
                self.export(origin_time=invalid)
        with self.assertRaises(ValueError):
            self.export(origin_time=ORIGIN.replace(tzinfo=None))

    def test_missing_or_invalid_event_values_fail(self):
        solution = make_solution()
        solution.event = None
        with self.assertRaises(ValueError):
            self.export(solution)
        for field in ["latitude", "longitude", "depth", "magnitude"]:
            for invalid in [None, float("nan"), float("inf"), True]:
                solution = make_solution()
                setattr(solution.event, field, invalid)
                with self.subTest(field=field, value=invalid), self.assertRaises(ValueError):
                    self.export(solution)

    def test_missing_or_invalid_channels_fail_instead_of_dropping_them(self):
        for invalid in [None, [], [None]]:
            solution = make_solution()
            solution.channels = invalid
            with self.subTest(channels=invalid), self.assertRaises(ValueError):
                self.export(solution)
        for field, values in {
            "pga": [None, 0, -1, True, float("nan"), float("inf"), "1"],
            "latitude": [None, float("nan")], "longitude": [None, float("inf")],
            "network": [None, ""], "station": [None, ""],
            "channel": [None, ""], "location": [None],
        }.items():
            for value in values:
                solution = make_solution()
                setattr(solution.channels[1], field, value)
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    self.export(solution)

    def test_location_variants_preserve_both_observations_in_input_order(self):
        # The previous writer accepted blank and populated locations for the
        # same station/component. Preserve both amplitudes in the exported XML;
        # any later native grouping is outside this serializer's responsibility.
        solution = make_solution()
        first = solution.channels[1]
        first.set_pga(9.80665)
        second = deepcopy(first)
        second.set_location_code("00")
        second.set_pga(19.6133)
        solution.channels = FinderChannelList([first, second])
        before = deepcopy(solution.channels)

        for channels, locations, amplitudes in (
            ([first, second], ["", "00"], [1.0, 2.0]),
            ([second, first], ["00", ""], [2.0, 1.0]),
        ):
            with self.subTest(locations=locations):
                solution.channels = FinderChannelList(channels)
                xml = self.export(solution)["event_dat.xml"]
                stations = ElementTree.fromstring(xml).findall("s:station", NS)

                self.assertEqual(len(stations), 2)
                self.assertEqual([node.get("code") for node in stations], ["REAL"] * 2)
                self.assertEqual([node.get("netid") for node in stations], ["CH"] * 2)
                self.assertEqual([node.get("loc") for node in stations], locations)

                for node, expected in zip(stations, amplitudes):
                    component = node.find("s:comp", NS)
                    self.assertEqual(component.get("name"), "HNE")
                    amplitude = component.find("s:acc", NS)
                    self.assertEqual(amplitude.get("units"), "%g")
                    self.assertAlmostEqual(float(amplitude.get("value")), expected, places=12)

        self.assertEqual([vars(first), vars(second)], [vars(item) for item in before])

    def test_same_station_components_keep_supplied_coordinates(self):
        # Accepted native coordinate merging must not prompt the exporter to
        # relocate observations, rename stations, or reject the whole input.
        solution = make_solution()
        solution.channels = FinderChannelList([
            FinderChannel(latitude=46.0, longitude=7.0, sncl="CH.REAL.00.HNE", pga=9.80665),
            FinderChannel(latitude=47.0, longitude=8.0, sncl="CH.REAL.10.HNN", pga=19.6133),
        ])

        xml = self.export(solution)["event_dat.xml"]
        stations = ElementTree.fromstring(xml).findall("s:station", NS)
        self.assertEqual(len(stations), 2)

        for node, location, component, latitude, longitude in (
            (stations[0], "00", "HNE", 46.0, 7.0),
            (stations[1], "10", "HNN", 47.0, 8.0),
        ):
            self.assertEqual(node.get("code"), "REAL")
            self.assertEqual(node.get("netid"), "CH")
            self.assertEqual(node.get("loc"), location)
            self.assertEqual(node.find("s:comp", NS).get("name"), component)
            self.assertAlmostEqual(float(node.get("lat")), latitude, places=12)
            self.assertAlmostEqual(float(node.get("lon")), longitude, places=12)

    def test_missing_rupture_coordinates_fail_without_inventing_depth(self):
        for invalid in [FinderRupture(), object()]:
            solution = make_solution()
            solution.rupture = invalid
            with self.assertRaises(ValueError):
                self.export(solution)
        solution = make_solution()
        solution.rupture.depths.pop()
        with self.assertRaises(ValueError):
            self.export(solution)
        solution = make_solution()
        solution.rupture.depths[0] = float("nan")
        with self.assertRaises(ValueError):
            self.export(solution)

    def test_export_does_not_mutate_inputs_or_use_external_resources(self):
        solution = make_solution()
        solution.event.latitude = "46.1"
        before = deepcopy(solution)
        with patch("builtins.open", side_effect=AssertionError("filesystem access")), \
             patch("os.makedirs", side_effect=AssertionError("directory creation")), \
             patch("subprocess.run", side_effect=AssertionError("process execution")), \
             patch("socket.create_connection", side_effect=AssertionError("network access")):
            self.export(solution)
        self.assertEqual(vars(solution.event), vars(before.event))
        self.assertEqual(vars(solution.rupture), vars(before.rupture))
        self.assertEqual([vars(channel) for channel in solution.channels],
                         [vars(channel) for channel in before.channels])
        self.assertEqual(solution.get_event_id(), before.get_event_id())
        self.assertEqual(solution.get_finder_event_id(), before.get_finder_event_id())


if __name__ == "__main__":
    unittest.main()
