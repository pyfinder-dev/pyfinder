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
from pyfinder.utils.regionlocator import RegionLocator, _country_records
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
        manager.configuration["shakemap"]["regional-configurations"]["tr"] = "italy"
        self.assertEqual(manager.select_shakemap_configuration(solution), "italy")

    def test_actual_packaged_country_identifiers_work_away_from_checkout(self):
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                self.assertEqual(RegionLocator().get_region(46.8, 8.2), "ch")
                self.assertEqual(RegionLocator().get_region(48.8, 2.3), "fr")
                self.assertIsNone(RegionLocator().get_region(0, -140))
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

    def test_boundary_ambiguity_unknown_territory_and_invalid_coordinates(self):
        from shapely.geometry import box

        locator = RegionLocator()
        records = (("ch", box(0, 0, 1, 1)), ("it", box(1, 0, 2, 1)),
                   (None, box(3, 0, 4, 1)))
        with patch("pyfinder.utils.regionlocator._country_records", return_value=records):
            self.assertEqual(locator.get_region(0.5, 0), "ch")
            self.assertIsNone(locator.get_region(0.5, 1))
            self.assertIsNone(locator.get_region(0.5, 3.5))
            self.assertIsNone(locator.get_region(0.5, 8))
        for lat, lon in ((True, 0), (None, 0), (91, 0), (0, 181), (float("nan"), 0)):
            with self.subTest(lat=lat, lon=lon), self.assertRaisesRegex(ValueError, "requires finite"):
                locator.get_region(lat, lon)
        with patch("pyfinder.utils.regionlocator._country_records", side_effect=ValueError("missing country data")):
            with self.assertRaisesRegex(ValueError, "missing country data"):
                locator.get_region(46, 7)

    def test_country_cache_reuses_materialized_data_and_reports_broken_resource(self):
        _country_records.cache_clear()
        self.addCleanup(_country_records.cache_clear)
        import geopandas
        with patch("geopandas.read_file", wraps=geopandas.read_file) as read:
            RegionLocator().get_region(46.8, 8.2)
            RegionLocator().get_region(42, 13)
        read.assert_called_once()
        _country_records.cache_clear()
        with patch("geopandas.read_file", side_effect=OSError("missing companion")):
            with self.assertRaisesRegex(ValueError, "packaged ShakeMap country boundaries"):
                RegionLocator().get_region(46, 7)

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
