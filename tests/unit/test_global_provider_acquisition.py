"""Offline tests for global provider configuration and service priority."""

import logging
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

from pyfinder import pyfinderconfig as configuration_module
from pyfinder import service_priority
from pyfinder.pyfinderconfig import (
    EMSC_FELT_REPORT_SERVICE,
    ESM_SHAKEMAP_SERVICE,
    RRSM_PEAK_MOTION_SERVICE,
    pyfinderconfig,
)
from pyfinder.service_priority import resolve_service_priority


class GlobalProviderConfigurationTests(unittest.TestCase):
    def test_corrected_felt_constant_exists_without_misspelled_alias(self):
        self.assertEqual(EMSC_FELT_REPORT_SERVICE, "EMSC_FeltReport")
        self.assertFalse(
            hasattr(configuration_module, "EMSC_FEELT_REPORT_SERVICE")
        )


class ServicePriorityResolverTests(unittest.TestCase):
    def test_valid_nonempty_unique_list_is_accepted(self):
        configured_priority = [
            RRSM_PEAK_MOTION_SERVICE,
            ESM_SHAKEMAP_SERVICE,
        ]
        logger = mock.Mock()

        resolved = resolve_service_priority(
            configured_priority,
            logger=logger,
        )

        self.assertEqual(resolved, configured_priority)
        self.assertIsNot(resolved, configured_priority)
        logger.critical.assert_not_called()

    def test_each_malformed_category_logs_critical_and_uses_shipped_order(self):
        # Exercise fallback behavior with test-owned values so editing the
        # project's provider priority does not require changing this test.
        fallback_priority = (
            RRSM_PEAK_MOTION_SERVICE,
            ESM_SHAKEMAP_SERVICE,
        )
        malformed_priorities = {
            "missing": None,
            "empty": [],
            "not-list": fallback_priority,
            "duplicate-containing": [
                ESM_SHAKEMAP_SERVICE,
                RRSM_PEAK_MOTION_SERVICE,
                ESM_SHAKEMAP_SERVICE,
            ],
        }

        for category, configured_priority in malformed_priorities.items():
            with self.subTest(category=category):
                enabled_before = list(
                    pyfinderconfig["general"]["services-enabled"]
                )
                logger = logging.Logger(self.id() + "." + category)

                with (
                    mock.patch.object(
                        service_priority,
                        "SHIPPED_SERVICE_PRIORITY",
                        fallback_priority,
                    ),
                    self.assertLogs(logger, level=logging.CRITICAL) as captured,
                ):
                    if category == "missing":
                        resolved = resolve_service_priority(logger=logger)
                    else:
                        resolved = resolve_service_priority(
                            configured_priority,
                            logger=logger,
                        )

                self.assertEqual(resolved, list(fallback_priority))
                self.assertEqual(
                    pyfinderconfig["general"]["services-enabled"],
                    enabled_before,
                )
                self.assertEqual(len(captured.records), 1)
                self.assertEqual(
                    captured.records[0].levelno,
                    logging.CRITICAL,
                )


class ServicePriorityImportSafetyTests(unittest.TestCase):
    def test_clean_import_loads_no_provider_scheduler_or_merger_module(self):
        repository_root = Path(__file__).resolve().parents[2]
        forbidden_modules = (
            "pyfinder.findermanager",
            "pyfinder.services.scheduler",
            "pyfinder.utils.station_merger",
            "paramws",
            "paramws.clients",
        )
        check = """
import sys
import pyfinder.service_priority

forbidden = {forbidden!r}
loaded = [name for name in forbidden if name in sys.modules]
if loaded:
    raise AssertionError("unexpected operational imports: " + repr(loaded))
""".format(forbidden=forbidden_modules)

        completed = subprocess.run(
            [sys.executable, "-c", check],
            cwd=repository_root,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(
            completed.returncode,
            0,
            msg=completed.stdout + completed.stderr,
        )


if __name__ == "__main__":
    unittest.main()
