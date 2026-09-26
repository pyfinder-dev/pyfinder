"""Opt-in parser checks against the existing canonical ShakeMap container.

These checks parse generated inputs in a disposable container /tmp directory.
They do not submit calculations, run FinDer, change container lifecycle, or
write to the mounted operator runtime. They establish input compatibility,
not successful model execution or deployment readiness.
"""

import base64
from datetime import datetime, timezone
import json
import math
import os
import subprocess
import unittest

from pyfinder.finderutils import (
    FinderChannel, FinderChannelList, FinderEvent, FinderRupture, FinderSolution,
)
from pyfinder.utils.shakemap import ShakeMapExporter


# Native imports deliberately live in the container, not in the host environment.
# Passing files through stdin also avoids adding a source or fixture mount.
_NATIVE_READER = r'''
import base64
import json
from pathlib import Path
import sys
import tempfile
from esi_shakelib.rupture.factory import rupture_from_dict_and_origin
from esi_shakelib.rupture.origin import Origin
from esi_shakelib.station import StationList

files = json.load(sys.stdin)
with tempfile.TemporaryDirectory(prefix="pyfinder-native-inputs-", dir="/tmp") as tmp:
    root = Path(tmp)
    for name, content in files.items():
        (root / name).write_bytes(base64.b64decode(content))
    origin = Origin.fromFile(str(root / "event.xml"))
    stations = StationList.loadFromFiles([str(root / "event_dat.xml")])
    amplitudes = stations.db.execute(
        "SELECT station_id, original_channel, orientation, amp "
        "FROM amp ORDER BY station_id, original_channel"
    ).fetchall()
    result = {
        "event_id": origin.id,
        "time": origin.time.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "amplitudes": amplitudes,
        "stations": stations.db.execute(
            "SELECT id, lat, lon FROM station ORDER BY id"
        ).fetchall(),
    }
    # Keep native geometry rejection observable. The adapter must not reshape
    # an unsupported FinDer polygon merely to make this parser accept it.
    try:
        rupture = rupture_from_dict_and_origin(
            json.loads((root / "rupture.json").read_text()), origin
        )
    except Exception as error:
        result["rupture_error"] = str(error)
    else:
        result["rupture_class"] = type(rupture).__name__
    print(json.dumps(result))
'''


def _solution(*, equal_depth=False):
    """Supply a small vertical finite rupture and distinct component meanings."""
    rupture = FinderRupture()
    bottom = 1.0 if equal_depth else 10.0
    for point in ((42.0, 13.0, 1.0), (42.1, 13.0, 1.0),
                  (42.1, 13.0, bottom), (42.0, 13.0, bottom)):
        rupture.add_point(*point)
    channels = FinderChannelList([
        FinderChannel(latitude=42.02, longitude=13.02, network_code="XX",
                      station_code=station, location_code="00",
                      channel_code=component, pga=98.0665,
                      is_artificial=artificial)
        for station, component, artificial in (
            ("EAST", "HNE", False), ("EAST", "HNN", False),
            ("VERT", "HNZ", False),
            ("PSEUDO", "0123", False), ("ART", "UNK", True),
        )
    ])
    return FinderSolution(
        event_id="upstream-id", finder_event_id="internal-id",
        event=FinderEvent(latitude=42.05, longitude=13.0, depth=6.0,
                          magnitude=5.5, origin_time_epoch=1),
        rupture=rupture, channels=channels,
    )


@unittest.skipUnless(
    os.environ.get("PYFINDER_RUN_SHAKEMAP_NATIVE") == "1",
    "set PYFINDER_RUN_SHAKEMAP_NATIVE=1 for canonical-container parser checks",
)
class ShakeMapNativeInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Refuse absent/stopped/stale canonical resources; verification never
        # creates replacement containers or changes the running service.
        inspected = subprocess.run(
            ["docker", "container", "inspect", "shakemap-docker"],
            capture_output=True, text=True, check=True, timeout=30,
        )
        container = json.loads(inspected.stdout)[0]
        image = subprocess.run(
            ["docker", "image", "inspect", "shakemap-docker:latest"],
            capture_output=True, text=True, check=True, timeout=30,
        )
        if not container["State"]["Running"]:
            raise AssertionError("canonical ShakeMap container is not running")
        if container["Image"] != json.loads(image.stdout)[0]["Id"]:
            raise AssertionError("canonical container does not match its image tag")

    def _parse(self, solution):
        files = ShakeMapExporter(
            solution, event_id="native-parser_t00000",
            origin_time=datetime(2026, 9, 26, 12, 30, 15, 250000,
                                 tzinfo=timezone.utc),
        ).export_all()
        completed = subprocess.run(
            ["docker", "exec", "-i", "shakemap-docker",
             "/usr/local/bin/python", "-c", _NATIVE_READER],
            input=json.dumps({name: base64.b64encode(content).decode("ascii")
                              for name, content in files.items()}),
            capture_output=True, text=True, check=False, timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_generated_inputs_keep_native_values_and_component_directions(self):
        result = self._parse(_solution())
        self.assertEqual(result["event_id"], "native-parser_t00000")
        self.assertEqual(result["time"], "2026-09-26T12:30:15.250000Z")
        self.assertNotIn("rupture_error", result)
        self.assertIn(result["rupture_class"], ("QuadRupture", "EdgeRupture"))
        # Repeated station elements must retain distinct components in the
        # native database; counting XML nodes alone would miss native merging.
        self.assertEqual(len(result["amplitudes"]), 5)
        amplitudes = {(row[0], row[1]): row[2:] for row in result["amplitudes"]}
        self.assertEqual(set(amplitudes), {
            ("XX.EAST", "HNE"), ("XX.EAST", "HNN"), ("XX.VERT", "HNZ"),
            ("XX.PSEUDO", "0123"), ("XX.ART", "UNK"),
        })
        for station, component, orientation in (
            ("XX.EAST", "HNE", "E"), ("XX.EAST", "HNN", "N"),
            ("XX.VERT", "HNZ", "Z"),
            ("XX.PSEUDO", "0123", "U"), ("XX.ART", "UNK", "H"),
        ):
            with self.subTest(station=station):
                self.assertEqual(amplitudes[station, component][0], orientation)
                self.assertAlmostEqual(amplitudes[station, component][1], math.log(0.1), places=12)

    def test_location_variants_follow_native_grouping_without_export_rejection(self):
        # Characterize the accepted library behavior using exported bytes, not
        # a second implementation of its station-key logic. This deliberately
        # demonstrates native information loss; it does not certify model use.
        for second_component in ("HNE", "HNN"):
            with self.subTest(second_component=second_component):
                solution = _solution()
                solution.channels = FinderChannelList([
                    FinderChannel(
                        latitude=46.0,
                        longitude=7.0,
                        sncl="CH.REAL..HNE",
                        pga=98.0665,
                    ),
                    FinderChannel(
                        latitude=47.0,
                        longitude=8.0,
                        sncl=f"CH.REAL.00.{second_component}",
                        pga=196.133,
                    ),
                ])
                result = self._parse(solution)

                # Both locations share one native station and its final
                # coordinates, even when their component amplitudes survive.
                self.assertEqual(len(result["stations"]), 1)
                station, latitude, longitude = result["stations"][0]
                self.assertEqual(station, "CH.REAL")
                self.assertAlmostEqual(latitude, 47.0, places=12)
                self.assertAlmostEqual(longitude, 8.0, places=12)

                amplitudes = {
                    (row[0], row[1]): row[3] for row in result["amplitudes"]
                }
                expected = {("CH.REAL", second_component): math.log(0.2)}
                if second_component != "HNE":
                    expected["CH.REAL", "HNE"] = math.log(0.1)

                self.assertEqual(set(amplitudes), set(expected))
                for identity, amplitude in expected.items():
                    # Native PGA storage is ln(g). These inputs are 0.1 g and
                    # 0.2 g; tolerance allows only floating-point roundoff.
                    self.assertAlmostEqual(amplitudes[identity], amplitude, places=12)

    def test_unsupported_depth_geometry_is_rejected_without_repair(self):
        solution = _solution(equal_depth=True)
        original_points = solution.get_rupture().get_points()
        result = self._parse(solution)
        self.assertNotIn("rupture_class", result)
        self.assertTrue(result.get("rupture_error"))
        self.assertEqual(solution.get_rupture().get_points(), original_points)


if __name__ == "__main__":
    unittest.main()
