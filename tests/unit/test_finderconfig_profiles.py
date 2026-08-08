"""Unit tests for complete FinDer definitions and their static catalog."""

from dataclasses import fields
import builtins
import importlib
import logging
import logging.handlers
import os
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock


FINDERCONFIG_MODULES = (
    "pyfinder.finderconfigs.globalconfig",
    "pyfinder.finderconfigs.switzerland",
    "pyfinder.finderconfigs.italy",
    "pyfinder.finderconfigs.profiles",
)


def load_finderconfig_modules():
    """Import the accepted foundation modules without hiding their identities."""

    return tuple(importlib.import_module(name) for name in FINDERCONFIG_MODULES)


def named_configurations():
    """Return the four source mappings in their accepted runtime-name order."""

    globalconfig, switzerland, italy, profiles = load_finderconfig_modules()
    return (
        (profiles.GLOBAL_PROFILE.name, globalconfig.GLOBAL_CONFIG),
        (
            profiles.REGIONAL_PROFILES[0].name,
            switzerland.SWITZERLAND_ALPINE_CONFIG,
        ),
        (
            profiles.REGIONAL_PROFILES[1].name,
            switzerland.SWITZERLAND_FORELAND_CONFIG,
        ),
        (profiles.REGIONAL_PROFILES[2].name, italy.ITALY_CONFIG),
    )


class FinderConfigImportSafetyTests(unittest.TestCase):
    def test_package_and_catalog_import_do_not_load_wkt_or_start_operations(self):
        target_prefix = "pyfinder.finderconfigs"
        saved_modules = {
            name: module
            for name, module in tuple(sys.modules.items())
            if name == target_prefix or name.startswith(target_prefix + ".")
        }
        for name in saved_modules:
            sys.modules.pop(name, None)

        original_directory = os.getcwd()
        original_threads = list(threading.enumerate())
        root_handlers = list(logging.getLogger().handlers)
        real_open = builtins.open

        def reject_wkt_open(file, *args, **kwargs):
            try:
                path = os.fspath(file)
            except TypeError:
                path = ""
            if str(path).lower().endswith(".wkt"):
                raise AssertionError("finderconfig imports must not open WKT files")
            return real_open(file, *args, **kwargs)

        try:
            with tempfile.TemporaryDirectory() as temporary_directory:
                os.chdir(temporary_directory)
                with mock.patch(
                    "builtins.open", side_effect=reject_wkt_open
                ), mock.patch.object(
                    Path,
                    "read_text",
                    side_effect=AssertionError(
                        "finderconfig imports must not read path content"
                    ),
                ), mock.patch.object(
                    Path,
                    "iterdir",
                    side_effect=AssertionError(
                        "finderconfig imports must not discover directories"
                    ),
                ), mock.patch.object(
                    sqlite3, "connect", autospec=True
                ) as database_connect, mock.patch.object(
                    logging, "FileHandler", autospec=True
                ) as file_handler, mock.patch.object(
                    logging.handlers, "RotatingFileHandler", autospec=True
                ) as rotating_handler, mock.patch.object(
                    threading.Thread, "start", autospec=True
                ) as thread_start, mock.patch.object(
                    socket, "socket", autospec=True
                ) as socket_constructor, mock.patch.dict(
                    sys.modules,
                    {"geopandas": None, "shapely": None},
                ):
                    package = importlib.import_module(target_prefix)
                    profiles = importlib.import_module(
                        target_prefix + ".profiles"
                    )

                database_connect.assert_not_called()
                file_handler.assert_not_called()
                rotating_handler.assert_not_called()
                thread_start.assert_not_called()
                socket_constructor.assert_not_called()
                self.assertEqual(list(Path(temporary_directory).iterdir()), [])
        finally:
            os.chdir(original_directory)
            for name in tuple(sys.modules):
                if name == target_prefix or name.startswith(target_prefix + "."):
                    sys.modules.pop(name, None)
            sys.modules.update(saved_modules)

        self.assertEqual(package.__name__, target_prefix)
        self.assertEqual(profiles.__name__, target_prefix + ".profiles")
        self.assertEqual(logging.getLogger().handlers, root_handlers)
        self.assertEqual(threading.enumerate(), original_threads)


class FinderConfigProfileTests(unittest.TestCase):
    def test_expected_configuration_modules_import(self):
        imported = load_finderconfig_modules()

        self.assertEqual(tuple(module.__name__ for module in imported),
                         FINDERCONFIG_MODULES)

    def test_catalog_has_exact_runtime_names_and_ordered_wkt_mappings(self):
        profiles = importlib.import_module("pyfinder.finderconfigs.profiles")

        self.assertEqual(
            (
                profiles.GLOBAL_PROFILE.name,
                *(profile.name for profile in profiles.REGIONAL_PROFILES),
            ),
            (
                "global",
                "switzerland-alpine",
                "switzerland-foreland",
                "italy",
            ),
        )
        self.assertEqual(
            tuple(
                (profile.wkt_filename, profile.name)
                for profile in profiles.REGIONAL_PROFILES
            ),
            (
                ("swiss_alpine.wkt", "switzerland-alpine"),
                ("swiss_foreland.wkt", "switzerland-foreland"),
                ("italy.wkt", "italy"),
            ),
        )
        self.assertIsNone(profiles.GLOBAL_PROFILE.wkt_filename)

    def test_named_configurations_share_the_current_ordered_global_key_set(self):
        configurations = named_configurations()
        expected_keys = tuple(configurations[0][1])

        for name, configuration in configurations:
            with self.subTest(name=name):
                self.assertIsInstance(configuration, dict)
                self.assertTrue(configuration)
                self.assertIn("DATA_FOLDER", configuration)
                self.assertEqual(tuple(configuration), expected_keys)

    def test_transitional_template_is_absent_but_resource_paths_remain(self):
        general_configuration = importlib.import_module(
            "pyfinder.pyfinderconfig"
        )
        transitional_name = "finder_file_" "config_template"

        self.assertFalse(
            hasattr(general_configuration, transitional_name)
        )
        self.assertTrue(general_configuration.finder_resources)
        self.assertTrue(general_configuration.gmt_resources)

    def test_named_configuration_dictionaries_are_distinct_objects(self):
        configurations = [
            configuration for _, configuration in named_configurations()
        ]

        self.assertEqual(
            len({id(configuration) for configuration in configurations}),
            len(configurations),
        )

    def test_mutating_a_test_copy_does_not_change_named_sources(self):
        configurations = [
            configuration for _, configuration in named_configurations()
        ]
        original_items = [tuple(configuration.items())
                          for configuration in configurations]
        test_copy = configurations[0].copy()
        first_key = next(iter(test_copy))

        test_copy[first_key] = object()

        for configuration, expected_items in zip(
            configurations,
            original_items,
        ):
            self.assertEqual(tuple(configuration.items()), expected_items)

    def test_profiles_have_no_service_or_provider_definition(self):
        profiles = importlib.import_module("pyfinder.finderconfigs.profiles")

        self.assertEqual(
            tuple(field.name for field in fields(profiles.FinderConfigProfile)),
            ("name", "configuration", "wkt_filename"),
        )
        forbidden_module_names = {
            name
            for name in vars(profiles)
            if "service" in name.lower() or "provider" in name.lower()
        }
        self.assertEqual(forbidden_module_names, set())

        for profile in (
            profiles.GLOBAL_PROFILE,
            *profiles.REGIONAL_PROFILES,
        ):
            forbidden_configuration_keys = {
                key
                for key in profile.configuration
                if "service" in key.lower() or "provider" in key.lower()
            }
            self.assertEqual(forbidden_configuration_keys, set())

    def test_unrelated_wkt_resources_are_not_registered(self):
        profiles = importlib.import_module("pyfinder.finderconfigs.profiles")
        resource_root = (
            Path(__file__).resolve().parents[2]
            / "pyfinder"
            / "extern"
            / "finder_regional_wkt"
        )
        pool_filenames = {path.name for path in resource_root.glob("*.wkt")}
        registered_filenames = {
            profile.wkt_filename for profile in profiles.REGIONAL_PROFILES
        }
        expected_filenames = {
            "swiss_alpine.wkt",
            "swiss_foreland.wkt",
            "italy.wkt",
        }

        self.assertEqual(registered_filenames, expected_filenames)
        self.assertTrue(expected_filenames.issubset(pool_filenames))
        self.assertTrue(
            (pool_filenames - expected_filenames).isdisjoint(
                registered_filenames
            )
        )


if __name__ == "__main__":
    unittest.main()
