"""Unit tests for query-policy validation and retained interfaces."""

import importlib
import inspect
import logging
import logging.handlers
import math
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from pyfinder.services import querypolicy


EXPECTED_RRSM_SCHEDULE = [0, 5, 15, 60, 180, 360, 1440, 2880]


class QueryPolicyValidationTests(unittest.TestCase):
    @staticmethod
    def make_rrsm_policy(service_name="RRSM", schedule=None):
        class TestRRSMPolicy(querypolicy.RRSMQueryPolicy):
            QUERY_SCHEDULE_MINUTES = (
                list(EXPECTED_RRSM_SCHEDULE)
                if schedule is None
                else schedule
            )

        TestRRSMPolicy.service_name = service_name
        return TestRRSMPolicy()

    def test_valid_rrsm_construction_preserves_identity_and_schedule(self):
        policy = querypolicy.RRSMQueryPolicy()

        self.assertEqual(policy.service_name, "RRSM")
        self.assertEqual(policy.QUERY_SCHEDULE_MINUTES, EXPECTED_RRSM_SCHEDULE)
        self.assertIsInstance(policy.QUERY_SCHEDULE_MINUTES, list)

    def test_invalid_rrsm_service_identity_fails_during_construction(self):
        for service_name in ("", "   ", None, 7):
            with self.subTest(service_name=service_name):
                with self.assertRaises(ValueError):
                    self.make_rrsm_policy(service_name=service_name)

    def test_structurally_valid_alternative_schedules_are_accepted(self):
        for schedule in (
            [0, 10],
            EXPECTED_RRSM_SCHEDULE + [5760],
        ):
            with self.subTest(schedule=schedule):
                policy = self.make_rrsm_policy(schedule=schedule)
                self.assertEqual(policy.QUERY_SCHEDULE_MINUTES, schedule)

    def test_invalid_rrsm_schedules_fail_during_construction(self):
        invalid_schedules = {
            "empty": [],
            "unordered delays": [0, 15, 5],
            "duplicate delay": [0, 5, 15, 60, 180, 360, 1440, 1440],
            "negative delay": [-1, 5, 15, 60, 180, 360, 1440, 2880],
            "boolean delay": [False, 5, 15, 60, 180, 360, 1440, 2880],
            "non-numeric delay": [0, "5", 15, 60, 180, 360, 1440, 2880],
            "NaN": [0, math.nan, 15, 60, 180, 360, 1440, 2880],
            "positive infinity": [
                0, 5, 15, 60, 180, 360, 1440, math.inf
            ],
            "negative infinity": [
                -math.inf, 5, 15, 60, 180, 360, 1440, 2880
            ],
        }
        for name, schedule in invalid_schedules.items():
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    self.make_rrsm_policy(schedule=schedule)

    def test_inactive_policies_keep_empty_schedules_and_service_identities(self):
        policies = (
            (querypolicy.ESMQueryPolicy(), "ESM"),
            (querypolicy.EMSCQueryPolicy(), "EMSC"),
        )
        for policy, expected_service in policies:
            with self.subTest(service=expected_service):
                self.assertEqual(policy.service_name, expected_service)
                self.assertEqual(policy.QUERY_SCHEDULE_MINUTES, [])

    def test_explicit_registry_construction_preserves_keys_and_types(self):
        policies = querypolicy.build_service_policies()

        self.assertEqual(
            list(policies),
            ["RRSM", "ESM", "EMSC"],
        )
        self.assertIsInstance(
            policies["RRSM"],
            querypolicy.RRSMQueryPolicy,
        )
        self.assertIsInstance(
            policies["ESM"],
            querypolicy.ESMQueryPolicy,
        )
        self.assertIsInstance(
            policies["EMSC"],
            querypolicy.EMSCQueryPolicy,
        )

        repeated = querypolicy.build_service_policies()
        for service in policies:
            self.assertIsNot(policies[service], repeated[service])

    def test_validation_has_no_duplicate_expected_schedule(self):
        source = inspect.getsource(querypolicy.RRSMQueryPolicy)

        self.assertFalse(hasattr(querypolicy, "_EXPECTED_SCHEDULE_MINUTES"))
        self.assertFalse(
            hasattr(querypolicy.RRSMQueryPolicy, "_EXPECTED_SCHEDULE_MINUTES")
        )
        self.assertEqual(
            source.count("[0, 5, 15, 60, 180, 360, 1440, 2880]"),
            1,
        )
        self.assertNotIn("tuple(schedule)", source)

    def test_rrsm_retry_rule_remains_unchanged(self):
        policy = querypolicy.RRSMQueryPolicy()

        self.assertTrue(policy.should_retry_on_failure({}))
        self.assertTrue(policy.should_retry_on_failure({"retry_count": 2}))
        self.assertFalse(policy.should_retry_on_failure({"retry_count": 3}))
        self.assertFalse(policy.should_retry_on_failure({"retry_count": 4}))

    def test_obsolete_interfaces_are_absent(self):
        obsolete_names = (
            "should_query",
            "get_next_query_delay_minutes",
            "get_current_query_delay_minutes",
            "is_terminal",
            "ALLOWED_DRIFT_MINUTES",
        )
        policy_types = (
            querypolicy.AbstractPolicy,
            querypolicy.RRSMQueryPolicy,
            querypolicy.ESMQueryPolicy,
            querypolicy.EMSCQueryPolicy,
        )
        policies = (
            querypolicy.RRSMQueryPolicy(),
            querypolicy.ESMQueryPolicy(),
            querypolicy.EMSCQueryPolicy(),
        )

        for name in obsolete_names:
            with self.subTest(name=name):
                for policy_type in policy_types:
                    self.assertFalse(hasattr(policy_type, name))
                for policy in policies:
                    self.assertFalse(hasattr(policy, name))

        for policy in policies:
            self.assertTrue(callable(policy.should_retry_on_failure))


class QueryPolicyImportSafetyTests(unittest.TestCase):
    def test_canonical_import_has_no_operational_resource_side_effects(self):
        original_directory = os.getcwd()
        original_threads = list(threading.enumerate())
        root_handlers = list(logging.getLogger().handlers)
        try:
            with tempfile.TemporaryDirectory() as temporary_directory:
                os.chdir(temporary_directory)
                with mock.patch.object(
                    sqlite3, "connect", autospec=True
                ) as database_connect, mock.patch.object(
                    logging, "FileHandler", autospec=True
                ) as file_handler, mock.patch.object(
                    logging.handlers, "RotatingFileHandler", autospec=True
                ) as rotating_handler, mock.patch.object(
                    threading.Thread, "start", autospec=True
                ) as thread_start, mock.patch.object(
                    socket, "socket", autospec=True
                ) as socket_constructor:
                    imported = importlib.import_module(
                        "pyfinder.services.querypolicy"
                    )
                    importlib.reload(imported)

                database_connect.assert_not_called()
                file_handler.assert_not_called()
                rotating_handler.assert_not_called()
                thread_start.assert_not_called()
                socket_constructor.assert_not_called()
                self.assertEqual(list(Path(temporary_directory).iterdir()), [])
                self.assertFalse(hasattr(imported, "SERVICE_POLICIES"))
                self.assertEqual(
                    [
                        value
                        for value in vars(imported).values()
                        if isinstance(value, imported.AbstractPolicy)
                    ],
                    [],
                )
        finally:
            os.chdir(original_directory)
            importlib.reload(querypolicy)

        self.assertEqual(imported.__name__, "pyfinder.services.querypolicy")
        self.assertEqual(logging.getLogger().handlers, root_handlers)
        self.assertEqual(threading.enumerate(), original_threads)


if __name__ == "__main__":
    unittest.main()
