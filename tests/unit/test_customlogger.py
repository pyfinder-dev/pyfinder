"""Minimal characterization tests for the current logger module."""

import logging
import unittest

from pyfinder.utils import customlogger


class CustomLoggerFrameworkTests(unittest.TestCase):
    def test_module_is_importable_through_package(self):
        self.assertEqual(customlogger.__name__, "pyfinder.utils.customlogger")

    def test_ok_level_is_between_info_and_warning(self):
        self.assertGreater(customlogger.OK_LOG_LEVEL, logging.INFO)
        self.assertLess(customlogger.OK_LOG_LEVEL, logging.WARNING)

    def test_file_formatter_formats_a_record_without_a_file_handler(self):
        record = logging.LogRecord(
            name="pyfinder.tests",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="framework smoke",
            args=(),
            exc_info=None,
        )

        rendered = customlogger.FileLoggingFormatter().format(record)

        self.assertIn("INFO", rendered)
        self.assertIn("framework smoke", rendered)


if __name__ == "__main__":
    unittest.main()
