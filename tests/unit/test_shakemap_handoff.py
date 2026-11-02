"""Exercise the manager handoff and input replacement using temporary files."""

from copy import deepcopy
from pathlib import Path
import fcntl
import os
import logging
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch
from xml.etree import ElementTree

from pyfinder.eventcontext import EventContext, EventContextError
from pyfinder.findermanager import FinDerManager
from pyfinder.services.shakemap_inputs import PreparedShakeMapInputs
from pyfinder.pyfinderconfig import pyfinderconfig
from pyfinder.utils.regionlocator import RegionLocator, _region_geometry
from tests.unit.test_shakemap_inputs import make_solution


class ManagerShakeMapHandoffTests(unittest.TestCase):
    def manager(self, origin="2026-08-10T08:15:30.250000Z"):
        # Construction bypasses profile selection, provider clients and logging;
        # the handoff needs only the authoritative context and persisted delay.
        manager = object.__new__(FinDerManager)
        manager.event_context = EventContext(
            "earthquake", 46.0, 7.0, 5.5, 10.0, origin
        )
        manager.metadata = {"current_delay": 5}
        manager.entry_kind = FinDerManager.ALERT_BACKED
        manager.configuration = deepcopy(pyfinderconfig)
        manager.logger = logging.getLogger("test.shakemap.selection")
        return manager

    def test_service_profile_uses_solution_location_independently_of_finder_name(self):
        manager = self.manager()
        solution = make_solution()
        for finder_name in ("switzerland-alpine", "switzerland-foreland", "other-model"):
            manager.finder_configuration_name = finder_name
            self.assertEqual(manager.select_shakemap_configuration(solution), "switzerland")
        # Upstream context remains Swiss while the selected output moves to Italy.
        solution.event.latitude, solution.event.longitude = 42.0, 13.0
        self.assertEqual(manager.select_shakemap_configuration(solution), "italy")
        solution.event.latitude, solution.event.longitude = 37.0, 36.0
        self.assertEqual(manager.select_shakemap_configuration(solution), "global")

    def test_packaged_wkt_selection_works_away_from_checkout_without_country_data(self):
        previous = Path.cwd()
        catalog = pyfinderconfig["shakemap"]["regional-configurations"]
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                with patch("geopandas.read_file", side_effect=AssertionError("no country lookup")):
                    locator = RegionLocator(catalog)
                    self.assertEqual(locator.get_configuration(46.8, 8.2), "switzerland")
                    self.assertEqual(locator.get_configuration(48.8, 2.3), "france")
                    self.assertEqual(locator.get_configuration(0, -140), "global")
            finally:
                os.chdir(previous)

    def test_parallel_solutions_select_without_mutating_shared_configuration(self):
        from concurrent.futures import ThreadPoolExecutor

        swiss, italian = self.manager(), self.manager()
        italian.configuration = swiss.configuration
        before = deepcopy(swiss.configuration)
        solutions = [make_solution(), make_solution()]
        solutions[1].event.latitude, solutions[1].event.longitude = 42.0, 13.0
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda pair: pair[0].select_shakemap_configuration(pair[1]),
                                        zip((swiss, italian), solutions)))
        self.assertEqual(results, ["switzerland", "italy"])
        self.assertEqual(swiss.configuration, before)

    def test_selected_solution_identity_and_physical_time_are_preserved(self):
        manager = self.manager()
        solution = make_solution()
        before = deepcopy(solution.__dict__)

        calculation_id, files = manager.prepare_shakemap(solution)
        event = ElementTree.fromstring(files["event.xml"])

        self.assertEqual(calculation_id, "earthquake_t00005")
        self.assertEqual(event.get("id"), calculation_id)
        self.assertEqual(event.get("time"), "2026-08-10T08:15:30.250000Z")
        self.assertEqual(solution.get_event().origin_time_epoch, 123456789)
        self.assertEqual(solution.get_event_id(), before["event_id"])
        stations = ElementTree.fromstring(files["event_dat.xml"])
        self.assertEqual(len(stations), len(solution.get_channels()))
        self.assertEqual(manager.prepare_shakemap(solution), (calculation_id, files))

    def test_explicit_offset_preserves_the_instant(self):
        manager = self.manager("2026-08-10T10:15:30.250000+02:00")
        _, files = manager.prepare_shakemap(make_solution())
        self.assertEqual(
            ElementTree.fromstring(files["event.xml"]).get("time"),
            "2026-08-10T08:15:30.250000Z",
        )

    def test_unknown_timezone_and_missing_context_do_not_guess_origin(self):
        manager = self.manager("2026-08-10T08:15:30.250000")
        with self.assertRaises(EventContextError):
            manager.prepare_shakemap(make_solution())

        manager.event_context = None
        with self.assertRaises(EventContextError):
            manager.prepare_shakemap(make_solution())

    def test_provider_backed_schedule_keeps_nominal_delay_identity(self):
        manager = self.manager()
        manager.entry_kind = FinDerManager.ON_DEMAND
        identity, _ = manager.prepare_shakemap(make_solution())
        self.assertEqual(identity, "earthquake_t00005")


class RegionalCoverageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.resources = self.root / "extern" / "shakemap_regional_wkt"
        self.resources.mkdir(parents=True)
        patcher = patch("pyfinder.utils.regionlocator.resources.files", return_value=self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        _region_geometry.cache_clear()
        self.addCleanup(_region_geometry.cache_clear)
        self.logger = logging.getLogger("test.shakemap.regional-coverage")

    def write_region(self, filename, geometry):
        (self.resources / filename).write_text(geometry.wkt)

    def test_arbitrary_cross_border_and_disjoint_regions_share_a_profile(self):
        from shapely.geometry import MultiPolygon, box

        self.write_region("central.wkt", box(6, 36, 16, 55))
        self.write_region("disjoint.wkt", MultiPolygon([
            box(-2, -2, 0, 0),
            box(3, 3, 4, 4),
        ]))
        catalog = {
            "CH + Italy + Germany": {
                "wkt": "central.wkt",
                "configuration": "shared-central",
            },
            "Two separate offshore areas": {
                "wkt": "disjoint.wkt",
                "configuration": "shared-central",
            },
        }
        locator = RegionLocator(catalog, logger=self.logger)
        for latitude, longitude in ((46.8, 8.2), (42, 13), (52, 13), (-1, -1), (3.5, 3.5)):
            with self.subTest(latitude=latitude, longitude=longitude):
                self.assertEqual(locator.get_configuration(latitude, longitude), "shared-central")
        self.assertEqual(locator.get_configuration(1, 1), "global")

    def test_split_areas_same_profile_border_and_conflicting_overlap(self):
        from shapely.geometry import box

        self.write_region("west.wkt", box(6, 46, 8, 48))
        self.write_region("east.wkt", box(8, 46, 10, 48))
        catalog = {
            "CH / west": {"wkt": "west.wkt", "configuration": "west-model"},
            "CH / east": {"wkt": "east.wkt", "configuration": "east-model"},
        }
        locator = RegionLocator(catalog, logger=self.logger)
        self.assertEqual(locator.get_configuration(47, 7), "west-model")
        self.assertEqual(locator.get_configuration(47, 9), "east-model")
        with self.assertLogs(self.logger, level="WARNING") as messages:
            self.assertEqual(locator.get_configuration(47, 8), "global")
        self.assertIn("conflicting configurations", " ".join(messages.output))

        catalog["CH / east"]["configuration"] = "west-model"
        self.assertEqual(
            RegionLocator(catalog).get_configuration(47, 8),
            "west-model",
        )
        self.write_region("overlap.wkt", box(7, 46, 9, 48))
        catalog["Central overlapping area"] = {
            "wkt": "overlap.wkt",
            "configuration": "west-model",
        }
        self.assertEqual(RegionLocator(catalog).get_configuration(47, 8), "west-model")
        catalog["Central overlapping area"]["configuration"] = "different-model"
        with self.assertLogs(self.logger, level="WARNING"):
            self.assertEqual(
                RegionLocator(catalog, logger=self.logger).get_configuration(47, 8),
                "global",
            )

    def test_missing_or_corrupt_optional_wkt_warns_global_and_can_be_restored(self):
        from shapely.geometry import box

        self.write_region("valid.wkt", box(0, 0, 2, 2))
        catalog = {
            "Available area": {"wkt": "valid.wkt", "configuration": "regional"},
            "Files supplied later": {"wkt": "later.wkt", "configuration": "other"},
        }
        locator = RegionLocator(catalog, logger=self.logger)
        with self.assertLogs(self.logger, level="WARNING") as messages:
            self.assertEqual(locator.get_configuration(1, 1), "global")
        self.assertIn("Files supplied later", " ".join(messages.output))
        self.assertIn(str(self.resources / "later.wkt"), " ".join(messages.output))

        (self.resources / "later.wkt").write_text("not WKT")
        with self.assertLogs(self.logger, level="WARNING") as messages:
            self.assertEqual(locator.get_configuration(1, 1), "global")
        self.assertIn("unusable WKT", " ".join(messages.output))

        (self.resources / "later.wkt").write_text(
            "POLYGON ((0 0, 1 1, 1 0, 0 1, 0 0))"
        )
        with self.assertLogs(self.logger, level="WARNING"):
            self.assertEqual(locator.get_configuration(1, 1), "global")

        self.write_region("later.wkt", box(5, 5, 6, 6))
        self.assertEqual(locator.get_configuration(1, 1), "regional")

    def test_invalid_solution_coordinates_still_error_and_successes_are_cached(self):
        from shapely.geometry import box

        self.write_region("valid.wkt", box(0, 0, 2, 2))
        locator = RegionLocator({
            "Any human label": {"wkt": "valid.wkt", "configuration": "regional"},
        })
        for lat, lon in ((True, 0), (None, 0), (91, 0), (0, 181), (float("nan"), 0)):
            with self.subTest(lat=lat, lon=lon), self.assertRaisesRegex(ValueError, "requires finite"):
                locator.get_configuration(lat, lon)

        def read_text(path, **kwargs):
            with path.open(encoding=kwargs.get("encoding")) as stream:
                return stream.read()

        # Preserve the actual file read while observing cache use.
        with patch.object(Path, "read_text", autospec=True, side_effect=read_text) as read:
            self.assertEqual(locator.get_configuration(1, 1), "regional")
            self.assertEqual(locator.get_configuration(1.5, 1.5), "regional")
        read.assert_called_once()


class PreparedShakeMapInputsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.prepared = PreparedShakeMapInputs(self.root)
        self.event = self.root / "calculation"
        self.event.mkdir()
        self.files = {"event.xml": b"new event", "event_dat.xml": b"new observations"}
        for name in self.files:
            (self.event / name).write_bytes(b"old required input")

    def test_finite_to_point_removes_only_stale_rupture(self):
        rupture = self.event / "rupture.json"
        rupture.write_bytes(b"old rupture")

        with self.prepared.submission("calculation", self.files):
            self.assertFalse(rupture.exists())
            for name in self.files:
                self.assertEqual((self.event / name).read_bytes(), b"old required input")

        self.assertEqual(set(path.name for path in self.event.iterdir()), set(self.files))

    def test_new_rupture_is_left_for_rest_to_replace(self):
        rupture = self.event / "rupture.json"
        rupture.write_bytes(b"old rupture")
        files = dict(self.files, **{"rupture.json": b"replacement"})

        with self.prepared.submission("calculation", files):
            self.assertEqual(rupture.read_bytes(), b"old rupture")

        rupture.unlink()
        with self.prepared.submission("calculation", files):
            self.assertFalse(rupture.exists())

    def test_unknown_entry_prevents_all_cleanup(self):
        for name in ("rupture.json", "model.conf"):
            (self.event / name).write_bytes(b"keep")

        with self.assertRaises(ValueError):
            with self.prepared.submission("calculation", self.files):
                self.fail("unexpected submission")

        self.assertEqual((self.event / "rupture.json").read_bytes(), b"keep")
        self.assertEqual((self.event / "model.conf").read_bytes(), b"keep")

    def test_input_symlink_is_refused_and_target_untouched(self):
        target = self.root / "outside"
        target.write_bytes(b"outside")
        (self.event / "rupture.json").symlink_to(target)

        with self.assertRaises(ValueError):
            with self.prepared.submission("calculation", self.files):
                self.fail("unexpected submission")

        self.assertEqual(target.read_bytes(), b"outside")
        self.assertTrue((self.event / "rupture.json").is_symlink())

    def test_event_and_lock_symlinks_are_refused(self):
        (self.root / "redirected").symlink_to(self.event, target_is_directory=True)
        with self.assertRaises(OSError):
            with self.prepared.submission("redirected", self.files):
                self.fail("unexpected submission")

        lock_dir = self.root / ".pyfinder-locks"
        target = self.root / "lock-target"
        target.write_bytes(b"keep")
        (lock_dir / "calculation.lock").symlink_to(target)
        with self.assertRaises(OSError):
            with self.prepared.submission("calculation", self.files):
                self.fail("unexpected submission")
        self.assertEqual(target.read_bytes(), b"keep")

    def test_root_must_be_existing_absolute_real_directory(self):
        for root in ("relative", self.root / "missing", self.event / "event.xml"):
            with self.subTest(root=root), self.assertRaises((ValueError, OSError)):
                PreparedShakeMapInputs(root)

        link = self.root / "root-link"
        link.symlink_to(self.event, target_is_directory=True)
        with self.assertRaises(ValueError):
            PreparedShakeMapInputs(link)

    def test_lock_contention_serializes_and_exception_releases(self):
        second = PreparedShakeMapInputs(self.root)
        attempted = threading.Event()
        entered = threading.Event()
        failures = []
        real_flock = fcntl.flock

        def observe_lock(descriptor, operation):
            # Signal at the actual lock boundary so the test needs no sleep.
            self.assertIn(operation, (fcntl.LOCK_EX, fcntl.LOCK_UN))
            attempted.set()
            return real_flock(descriptor, operation)

        def later_submission():
            try:
                with second.submission("calculation", self.files):
                    entered.set()
            except Exception as error:
                failures.append(error)

        thread = threading.Thread(target=later_submission, daemon=True)
        with self.assertRaisesRegex(RuntimeError, "upload failed"):
            with self.prepared.submission("calculation", self.files):
                with patch(
                    "pyfinder.services.shakemap_inputs.fcntl.flock",
                    side_effect=observe_lock,
                ):
                    thread.start()
                    self.assertTrue(attempted.wait(timeout=2))
                    self.assertFalse(entered.is_set())
                    raise RuntimeError("upload failed")

        # The first upload's exception closes its descriptor. The waiting
        # submission then enters normally instead of failing on contention.
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertTrue(entered.is_set())

    def test_cleanup_waits_for_service_directory_lock_and_releases_before_post(self):
        rupture = self.event / "rupture.json"
        rupture.write_bytes(b"current service input")
        descriptor = os.open(self.event, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, descriptor)
        attempted = threading.Event()
        entered = threading.Event()
        failures = []
        real_flock = fcntl.flock

        def observe_lock(candidate, operation):
            if stat.S_ISDIR(os.fstat(candidate).st_mode) and operation == fcntl.LOCK_EX:
                attempted.set()
            return real_flock(candidate, operation)

        def submit_after_service_snapshot():
            try:
                with self.prepared.submission("calculation", self.files):
                    # This stands in for the service acquiring its directory
                    # lock on POST. A retained cleanup lock would fail here.
                    real_flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    real_flock(descriptor, fcntl.LOCK_UN)
                    entered.set()
            except Exception as error:
                failures.append(error)

        thread = threading.Thread(target=submit_after_service_snapshot, daemon=True)
        real_flock(descriptor, fcntl.LOCK_EX)
        try:
            with patch(
                "pyfinder.services.shakemap_inputs.fcntl.flock",
                side_effect=observe_lock,
            ):
                thread.start()
                self.assertTrue(attempted.wait(timeout=2))
                self.assertFalse(entered.is_set())
                self.assertEqual(rupture.read_bytes(), b"current service input")
        finally:
            real_flock(descriptor, fcntl.LOCK_UN)
            thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertTrue(entered.is_set())
        self.assertFalse(rupture.exists())

    def test_new_event_and_independent_ids_are_allowed(self):
        with self.prepared.submission("calculation", self.files):
            with self.prepared.submission("another", self.files):
                self.assertTrue((self.root / "another").is_dir())

    def test_invalid_bundle_or_identity_cannot_remove_old_rupture(self):
        rupture = self.event / "rupture.json"
        rupture.write_bytes(b"keep")
        for event_id in ("../escape", "a/b", "a\\b", ".", "", ".pyfinder-locks"):
            with self.subTest(event_id=event_id), self.assertRaises(ValueError):
                with self.prepared.submission(event_id, self.files):
                    self.fail("invalid identity accepted")

        for files in ({"event.xml": b"only"}, dict(self.files, extra=b"unknown"),
                      {"event.xml": "text", "event_dat.xml": b"data"}):
            with self.subTest(files=files), self.assertRaises(ValueError):
                with self.prepared.submission("calculation", files):
                    self.fail("invalid bundle accepted")
        self.assertEqual(rupture.read_bytes(), b"keep")
