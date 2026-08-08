"""Offline tests for computational-region FinDer configuration selection."""

from copy import deepcopy
import importlib
import logging
import math
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

import pyfinder.finderconfigs as finderconfigs
from pyfinder.finderconfigs import (
    FinderConfigSelector,
    GlobalFinderConfigError,
    build_default_selector,
)
from pyfinder.finderconfigs.profiles import FinderConfigProfile


DEFAULT_CONFIGURATION = object()


class UnrenderableNativeValue:
    """Raise when selector validation tries to form a native value string."""

    def __str__(self):
        raise RuntimeError("cannot render native value")


class CountingResource:
    """Count WKT reads while retaining normal pathlib UTF-8 behavior."""

    def __init__(self, path, read_counts):
        self.path = path
        self.read_counts = read_counts

    def read_text(self, encoding=None):
        self.read_counts[self.path.name] = (
            self.read_counts.get(self.path.name, 0) + 1
        )
        return self.path.read_text(encoding=encoding)


class UnreadableResource:
    """Represent a resource that resolves but cannot be read."""

    def read_text(self, encoding=None):
        raise PermissionError("test resource is unreadable")


class FinderConfigSelectorTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.resource_root = Path(self.temporary_directory.name)
        self.logger = logging.Logger(self.id(), level=logging.DEBUG)
        self.global_configuration = {
            "DATA_FOLDER": "<PATH>",
            "MODEL": {"name": "global", "coefficients": [1, 2]},
            "MODE": "global-mode",
        }
        self.global_profile = FinderConfigProfile(
            name="global",
            configuration=self.global_configuration,
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def write_wkt(self, filename, content):
        path = self.resource_root / filename
        path.write_text(content, encoding="utf-8")
        return path

    def regional_configuration(self, name):
        return {
            "DATA_FOLDER": "<PATH>",
            "MODEL": {"name": name, "coefficients": [3, 4]},
            "MODE": name + "-mode",
        }

    def regional_profile(
        self,
        name,
        filename,
        configuration=DEFAULT_CONFIGURATION,
    ):
        if configuration is DEFAULT_CONFIGURATION:
            configuration = self.regional_configuration(name)
        return FinderConfigProfile(
            name=name,
            configuration=configuration,
            wkt_filename=filename,
        )

    def make_selector(
        self,
        *regional_profiles,
        global_profile=DEFAULT_CONFIGURATION,
        resource_resolver=None,
    ):
        if global_profile is DEFAULT_CONFIGURATION:
            global_profile = self.global_profile
        return FinderConfigSelector(
            global_profile,
            regional_profiles,
            resource_root=self.resource_root,
            resource_resolver=resource_resolver,
            logger=self.logger,
        )

    def assert_critical_global_fallback(
        self,
        selector,
        latitude,
        longitude,
    ):
        with self.assertLogs(self.logger, level=logging.CRITICAL) as captured:
            decision = selector.resolve(latitude, longitude)

        self.assertEqual(decision.configuration_name, "global")
        self.assertEqual(decision.configuration, self.global_configuration)
        self.assertTrue(captured.records)
        self.assertTrue(
            all(record.levelno == logging.CRITICAL
                for record in captured.records)
        )
        return decision, captured

    def test_package_import_exposes_boundary_without_constructing_selector(self):
        selector_module = importlib.import_module(
            "pyfinder.finderconfigs.selector"
        )

        with mock.patch.object(
            selector_module.resources,
            "files",
            side_effect=AssertionError(
                "package import must not resolve WKT resources"
            ),
        ), mock.patch.object(
            selector_module.FinderConfigSelector,
            "__init__",
            autospec=True,
        ) as selector_construction:
            reloaded = importlib.reload(finderconfigs)

        selector_construction.assert_not_called()
        self.assertEqual(
            reloaded.__all__,
            (
                "FinderConfigSelector",
                "GlobalFinderConfigError",
                "ResolvedFinderConfig",
                "build_default_selector",
            ),
        )
        self.assertFalse(hasattr(reloaded, "GLOBAL_CONFIG"))
        self.assertFalse(hasattr(reloaded, "REGIONAL_PROFILES"))

    def test_valid_polygon_uses_lon_lat_axis_order_and_covers_boundary(self):
        import geopandas

        self.write_wkt(
            "axis.wkt",
            "POLYGON ((20 5, 40 5, 40 15, 20 15, 20 5))",
        )
        profile = self.regional_profile("axis-region", "axis.wkt")

        with mock.patch.object(
            geopandas.GeoSeries,
            "to_crs",
            side_effect=AssertionError("selector must not reproject WKT"),
        ) as reproject:
            selector = self.make_selector(profile)

        reproject.assert_not_called()
        inside = selector.resolve(latitude=10, longitude=30)
        boundary = selector.resolve(latitude=5, longitude=20)
        _profile, geometry, crs = selector._regional_geometries[0]

        self.assertEqual(inside.configuration_name, "axis-region")
        self.assertEqual(boundary.configuration_name, "axis-region")
        self.assertEqual(geometry.geom_type, "Polygon")
        self.assertEqual(crs.to_epsg(), 4326)
        self.assert_critical_global_fallback(
            selector,
            latitude=30,
            longitude=10,
        )

    def test_valid_multipolygon_selects_a_component(self):
        self.write_wkt(
            "multi.wkt",
            "MULTIPOLYGON (((0 0, 2 0, 2 2, 0 2, 0 0)), "
            "((20 20, 22 20, 22 22, 20 22, 20 20)))",
        )
        selector = self.make_selector(
            self.regional_profile("multi-region", "multi.wkt")
        )

        decision = selector.resolve(latitude=21, longitude=21)

        self.assertEqual(decision.configuration_name, "multi-region")
        self.assertEqual(
            selector._regional_geometries[0][1].geom_type,
            "MultiPolygon",
        )

    def test_polygon_interior_hole_is_not_covered(self):
        self.write_wkt(
            "hole.wkt",
            "POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0), "
            "(3 3, 7 3, 7 7, 3 7, 3 3))",
        )
        selector = self.make_selector(
            self.regional_profile("hole-region", "hole.wkt")
        )

        shell_decision = selector.resolve(latitude=1, longitude=1)

        self.assertEqual(shell_decision.configuration_name, "hole-region")
        self.assert_critical_global_fallback(
            selector,
            latitude=5,
            longitude=5,
        )

    def test_first_registered_covering_geometry_wins_overlap(self):
        self.write_wkt(
            "z-first.wkt",
            "POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0))",
        )
        self.write_wkt(
            "a-second.wkt",
            "POLYGON ((5 5, 15 5, 15 15, 5 15, 5 5))",
        )
        selector = self.make_selector(
            self.regional_profile("first", "z-first.wkt"),
            self.regional_profile("second", "a-second.wkt"),
        )

        decision = selector.resolve(latitude=7, longitude=7)

        self.assertEqual(decision.configuration_name, "first")

    def test_valid_point_outside_all_regions_logs_critical_global(self):
        self.write_wkt(
            "small.wkt",
            "POLYGON ((0 0, 1 0, 1 1, 0 1, 0 0))",
        )
        selector = self.make_selector(
            self.regional_profile("small", "small.wkt")
        )

        self.assert_critical_global_fallback(
            selector,
            latitude=40,
            longitude=40,
        )

    def test_empty_injected_regional_catalog_logs_critical_global(self):
        selector = self.make_selector()

        self.assert_critical_global_fallback(
            selector,
            latitude=0,
            longitude=0,
        )

    def test_numeric_string_coordinates_are_normalized(self):
        self.write_wkt(
            "strings.wkt",
            "POLYGON ((20 5, 40 5, 40 15, 20 15, 20 5))",
        )
        selector = self.make_selector(
            self.regional_profile("strings", "strings.wkt")
        )

        decision = selector.resolve(latitude="10", longitude="30.0")

        self.assertEqual(decision.configuration_name, "strings")

    def test_invalid_coordinates_each_log_critical_global(self):
        self.write_wkt(
            "coordinates.wkt",
            "POLYGON ((-2 -2, 2 -2, 2 2, -2 2, -2 -2))",
        )
        selector = self.make_selector(
            self.regional_profile("coordinates", "coordinates.wkt")
        )
        invalid_coordinates = (
            ("missing latitude", None, 0),
            ("missing longitude", 0, None),
            ("boolean latitude", True, 0),
            ("boolean longitude", 0, False),
            ("nonnumeric latitude", "north", 0),
            ("nonnumeric longitude", 0, object()),
            ("NaN latitude", math.nan, 0),
            ("infinite latitude", math.inf, 0),
            ("infinite longitude", 0, -math.inf),
            ("latitude below range", -91, 0),
            ("latitude above range", 91, 0),
            ("longitude below range", 0, -181),
            ("longitude above range", 0, 181),
        )

        for name, latitude, longitude in invalid_coordinates:
            with self.subTest(name=name):
                self.assert_critical_global_fallback(
                    selector,
                    latitude,
                    longitude,
                )

    def test_registered_wkt_is_read_once_for_reused_selector(self):
        self.write_wkt(
            "counted.wkt",
            "POLYGON ((0 0, 2 0, 2 2, 0 2, 0 0))",
        )
        read_counts = {}
        resolved_filenames = []

        def resolver(root, filename):
            resolved_filenames.append(filename)
            return CountingResource(root / filename, read_counts)

        selector = self.make_selector(
            self.regional_profile("counted", "counted.wkt"),
            resource_resolver=resolver,
        )
        selector.resolve(latitude=1, longitude=1)
        self.assert_critical_global_fallback(
            selector,
            latitude=10,
            longitude=10,
        )

        self.assertEqual(resolved_filenames, ["counted.wkt"])
        self.assertEqual(read_counts, {"counted.wkt": 1})

    def test_unregistered_wkt_file_is_inert(self):
        self.write_wkt(
            "registered.wkt",
            "POLYGON ((0 0, 2 0, 2 2, 0 2, 0 0))",
        )
        self.write_wkt("unregistered.wkt", "this is not valid WKT")
        resolved_filenames = []

        def resolver(root, filename):
            resolved_filenames.append(filename)
            return root / filename

        selector = self.make_selector(
            self.regional_profile("registered", "registered.wkt"),
            resource_resolver=resolver,
        )

        decision = selector.resolve(latitude=1, longitude=1)

        self.assertEqual(decision.configuration_name, "registered")
        self.assertEqual(resolved_filenames, ["registered.wkt"])

    def test_bad_registered_wkt_forms_poison_regional_selection(self):
        invalid_wkt = (
            ("empty text", "   \n"),
            ("malformed", "not a WKT geometry"),
            ("empty polygon", "POLYGON EMPTY"),
            (
                "self-intersecting polygon",
                "POLYGON ((0 0, 2 2, 0 2, 2 0, 0 0))",
            ),
            ("unsupported line", "LINESTRING (0 0, 2 2)"),
            (
                "longitude outside range",
                "POLYGON ((181 0, 182 0, 182 1, 181 1, 181 0))",
            ),
            (
                "latitude outside range",
                "POLYGON ((0 91, 1 91, 1 92, 0 92, 0 91))",
            ),
            (
                "non-finite bounds",
                "POLYGON ((0 0, 2 0, 2 Inf, 0 2, 0 0))",
            ),
        )

        for name, content in invalid_wkt:
            with self.subTest(name=name):
                self.write_wkt("invalid.wkt", content)
                selector = self.make_selector(
                    self.regional_profile("invalid", "invalid.wkt")
                )
                self.assert_critical_global_fallback(
                    selector,
                    latitude=1,
                    longitude=1,
                )

    def test_missing_and_unreadable_registered_wkt_poison_selection(self):
        missing_selector = self.make_selector(
            self.regional_profile("missing", "missing.wkt")
        )
        self.assert_critical_global_fallback(
            missing_selector,
            latitude=1,
            longitude=1,
        )

        def unreadable_resolver(root, filename):
            return UnreadableResource()

        unreadable_selector = self.make_selector(
            self.regional_profile("unreadable", "unreadable.wkt"),
            resource_resolver=unreadable_resolver,
        )
        self.assert_critical_global_fallback(
            unreadable_selector,
            latitude=1,
            longitude=1,
        )

    def test_all_registered_resources_are_attempted_and_one_failure_poisoned(self):
        self.write_wkt("malformed.wkt", "not WKT")
        self.write_wkt(
            "later-covering.wkt",
            "POLYGON ((0 0, 2 0, 2 2, 0 2, 0 0))",
        )
        resolved_filenames = []

        def resolver(root, filename):
            resolved_filenames.append(filename)
            return root / filename

        selector = self.make_selector(
            self.regional_profile("missing", "missing.wkt"),
            self.regional_profile("malformed", "malformed.wkt"),
            self.regional_profile("later", "later-covering.wkt"),
            resource_resolver=resolver,
        )

        decision, captured = self.assert_critical_global_fallback(
            selector,
            latitude=1,
            longitude=1,
        )

        self.assertEqual(decision.configuration_name, "global")
        self.assertEqual(
            resolved_filenames,
            ["missing.wkt", "malformed.wkt", "later-covering.wkt"],
        )
        diagnostic = "\n".join(captured.output)
        self.assertIn("missing.wkt", diagnostic)
        self.assertIn("malformed.wkt", diagnostic)

    def test_selected_unusable_regional_configurations_use_whole_global(self):
        self.write_wkt(
            "selected.wkt",
            "POLYGON ((0 0, 2 0, 2 2, 0 2, 0 0))",
        )
        invalid_profiles = (
            SimpleNamespace(name="missing", wkt_filename="selected.wkt"),
            self.regional_profile("nonmapping", "selected.wkt", []),
            self.regional_profile("empty", "selected.wkt", {}),
            self.regional_profile(
                "incomplete",
                "selected.wkt",
                {"DATA_FOLDER": "<PATH>"},
            ),
            self.regional_profile(
                "extra-key",
                "selected.wkt",
                {
                    **self.regional_configuration("extra-key"),
                    "UNEXPECTED": "value",
                },
            ),
        )

        for profile in invalid_profiles:
            with self.subTest(name=profile.name):
                selector = self.make_selector(profile)
                decision, _captured = self.assert_critical_global_fallback(
                    selector,
                    latitude=1,
                    longitude=1,
                )
                self.assertEqual(
                    tuple(decision.configuration),
                    tuple(self.global_configuration),
                )

    def test_invalid_unselected_configuration_does_not_affect_valid_region(self):
        self.write_wkt(
            "selected-valid.wkt",
            "POLYGON ((0 0, 2 0, 2 2, 0 2, 0 0))",
        )
        self.write_wkt(
            "unselected-invalid.wkt",
            "POLYGON ((10 10, 12 10, 12 12, 10 12, 10 10))",
        )
        selected_configuration = self.regional_configuration("selected")
        unselected_configuration = self.regional_configuration("unselected")
        unselected_configuration["MODE"] = "invalid\nsecond line"
        selector = self.make_selector(
            self.regional_profile(
                "selected",
                "selected-valid.wkt",
                selected_configuration,
            ),
            self.regional_profile(
                "unselected",
                "unselected-invalid.wkt",
                unselected_configuration,
            ),
        )

        decision = selector.resolve(latitude=1, longitude=1)

        self.assertEqual(decision.configuration_name, "selected")
        self.assertEqual(decision.configuration, selected_configuration)

    def test_fatal_global_profile_validation(self):
        invalid_global_profiles = (
            ("missing profile", None),
            (
                "wrong name",
                FinderConfigProfile(
                    name="not-global",
                    configuration=deepcopy(self.global_configuration),
                ),
            ),
            (
                "global WKT",
                FinderConfigProfile(
                    name="global",
                    configuration=deepcopy(self.global_configuration),
                    wkt_filename="global.wkt",
                ),
            ),
            (
                "nonmapping",
                FinderConfigProfile(name="global", configuration=[]),
            ),
            (
                "empty",
                FinderConfigProfile(name="global", configuration={}),
            ),
            (
                "missing DATA_FOLDER",
                FinderConfigProfile(
                    name="global",
                    configuration={"MODEL": "global"},
                ),
            ),
        )

        for name, global_profile in invalid_global_profiles:
            with self.subTest(name=name):
                with self.assertRaises(GlobalFinderConfigError):
                    self.make_selector(global_profile=global_profile)

    def test_global_rejects_native_line_structural_defects(self):
        cases = (
            ("empty field name", "", "value"),
            ("whitespace field name", "BAD FIELD", "value"),
            ("line-breaking field name", "BAD\nFIELD", "value"),
            ("non-string field name", 17, "value"),
            ("value conversion failure", "MODE", UnrenderableNativeValue()),
            ("newline value", "MODE", "first\nsecond"),
            ("carriage-return value", "MODE", "first\rsecond"),
        )
        for label, field_name, value in cases:
            with self.subTest(label=label):
                configuration = {
                    "DATA_FOLDER": "<PATH>",
                    field_name: value,
                }
                profile = FinderConfigProfile(
                    name="global",
                    configuration=configuration,
                )

                with self.assertRaises(GlobalFinderConfigError):
                    self.make_selector(global_profile=profile)

    def test_selected_regional_native_line_defects_use_whole_global(self):
        self.write_wkt(
            "native-line.wkt",
            "POLYGON ((0 0, 2 0, 2 2, 0 2, 0 0))",
        )
        cases = (
            ("empty field name", "", "value"),
            ("whitespace field name", "BAD FIELD", "value"),
            ("line-breaking field name", "BAD\nFIELD", "value"),
            ("non-string field name", 17, "value"),
            ("value conversion failure", "MODE", UnrenderableNativeValue()),
            ("newline value", "MODE", "first\nsecond"),
            ("carriage-return value", "MODE", "first\rsecond"),
        )
        for label, field_name, value in cases:
            with self.subTest(label=label):
                configuration = self.regional_configuration("invalid")
                if field_name != "MODE":
                    configuration.pop("MODE")
                configuration[field_name] = value
                selector = self.make_selector(
                    self.regional_profile(
                        "invalid",
                        "native-line.wkt",
                        configuration,
                    )
                )

                decision, _captured = self.assert_critical_global_fallback(
                    selector,
                    latitude=1,
                    longitude=1,
                )
                self.assertEqual(
                    tuple(decision.configuration.items()),
                    tuple(self.global_configuration.items()),
                )

    def test_returned_dictionaries_are_deeply_isolated_from_all_sources(self):
        self.write_wkt(
            "isolation.wkt",
            "POLYGON ((0 0, 2 0, 2 2, 0 2, 0 0))",
        )
        regional_configuration = self.regional_configuration("isolation")
        profile = self.regional_profile(
            "isolation",
            "isolation.wkt",
            regional_configuration,
        )
        original_global = deepcopy(self.global_configuration)
        original_regional = deepcopy(regional_configuration)
        selector = self.make_selector(profile)

        first_regional = selector.resolve(latitude=1, longitude=1)
        second_regional = selector.resolve(latitude=1, longitude=1)
        first_regional.configuration["MODEL"]["coefficients"].append(99)
        first_regional.configuration["MODE"] = "mutated"

        self.assertIsNot(
            first_regional.configuration,
            second_regional.configuration,
        )
        self.assertEqual(second_regional.configuration, original_regional)
        self.assertEqual(regional_configuration, original_regional)
        self.assertEqual(profile.configuration, original_regional)

        first_global, _captured = self.assert_critical_global_fallback(
            selector,
            latitude=20,
            longitude=20,
        )
        second_global, _captured = self.assert_critical_global_fallback(
            selector,
            latitude=20,
            longitude=20,
        )
        first_global.configuration["MODEL"]["coefficients"].append(100)

        self.assertIsNot(first_global.configuration, second_global.configuration)
        self.assertEqual(second_global.configuration, original_global)
        self.assertEqual(self.global_configuration, original_global)
        self.assertEqual(self.global_profile.configuration, original_global)

    def test_default_builder_uses_only_explicit_catalog_entries(self):
        accepted_wkt = {
            "swiss_alpine.wkt":
                "POLYGON ((0 0, 1 0, 1 1, 0 1, 0 0))",
            "swiss_foreland.wkt":
                "POLYGON ((2 2, 3 2, 3 3, 2 3, 2 2))",
            "italy.wkt":
                "POLYGON ((4 4, 5 4, 5 5, 4 5, 4 4))",
        }
        for filename, content in accepted_wkt.items():
            self.write_wkt(filename, content)
        self.write_wkt("unregistered.wkt", "not WKT")

        with mock.patch.object(
            Path,
            "iterdir",
            side_effect=AssertionError("selector must not scan WKT directory"),
        ), mock.patch.object(
            Path,
            "glob",
            side_effect=AssertionError("selector must not glob WKT directory"),
        ), mock.patch.object(
            Path,
            "rglob",
            side_effect=AssertionError("selector must not recurse WKT directory"),
        ), mock.patch.object(
            os,
            "scandir",
            side_effect=AssertionError("selector must not scan WKT directory"),
        ), mock.patch.object(
            os,
            "listdir",
            side_effect=AssertionError("selector must not list WKT directory"),
        ):
            selector = build_default_selector(
                resource_root=self.resource_root,
                logger=self.logger,
            )

        decision = selector.resolve(latitude=0.5, longitude=0.5)

        self.assertEqual(decision.configuration_name, "switzerland-alpine")
        self.assertEqual(
            tuple(
                profile.wkt_filename
                for profile, _geometry, _crs in selector._regional_geometries
            ),
            (
                "swiss_alpine.wkt",
                "swiss_foreland.wkt",
                "italy.wkt",
            ),
        )

    def test_selector_does_not_use_operational_or_network_resources(self):
        self.write_wkt(
            "offline.wkt",
            "POLYGON ((0 0, 2 0, 2 2, 0 2, 0 0))",
        )

        with mock.patch.object(
            sqlite3, "connect", autospec=True
        ) as database_connect, mock.patch.object(
            socket, "socket", autospec=True
        ) as socket_constructor, mock.patch.object(
            subprocess, "Popen", autospec=True
        ) as process_constructor, mock.patch.object(
            subprocess, "run", autospec=True
        ) as process_run, mock.patch.object(
            threading.Thread, "start", autospec=True
        ) as thread_start:
            selector = self.make_selector(
                self.regional_profile("offline", "offline.wkt")
            )
            decision = selector.resolve(latitude=1, longitude=1)

        self.assertEqual(decision.configuration_name, "offline")
        database_connect.assert_not_called()
        socket_constructor.assert_not_called()
        process_constructor.assert_not_called()
        process_run.assert_not_called()
        thread_start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
