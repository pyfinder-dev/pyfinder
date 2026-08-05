"""Smoke tests for importing PyFinder from the repository root."""

import unittest

import pyfinder


class PackageImportTests(unittest.TestCase):
    def test_package_import_exposes_stable_metadata(self):
        self.assertEqual(pyfinder.__name__, "pyfinder")
        self.assertTrue(pyfinder.__version__)


if __name__ == "__main__":
    unittest.main()
