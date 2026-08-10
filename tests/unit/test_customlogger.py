"""Unit tests for PyFinder's isolated custom file logger."""

import importlib
import inspect
import logging
import logging.handlers
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from pyfinder.utils import customlogger


class CustomLoggerTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary_directory.name)
        self.loggers = []

    def tearDown(self):
        for logger in set(self.loggers):
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()
        self.temporary_directory.cleanup()

    def make_logger(self, filename="test.log", module_name=None, **kwargs):
        logger = customlogger.file_logger(
            self.directory / filename,
            module_name=module_name or self.id(),
            **kwargs,
        )
        self.loggers.append(logger)
        return logger

    @staticmethod
    def flush(logger):
        for handler in logger.handlers:
            handler.flush()

    def read(self, filename="test.log"):
        return (self.directory / filename).read_text(encoding="utf-8")

    def test_import_has_no_file_or_handler_side_effects(self):
        root = logging.getLogger()
        root_handlers = list(root.handlers)
        root_level = root.level
        logger_class = logging.getLoggerClass()

        with mock.patch("logging.FileHandler") as file_handler, mock.patch(
            "logging.handlers.RotatingFileHandler"
        ) as rotating_handler, mock.patch.object(
            logging.Logger, "addHandler", autospec=True
        ) as add_handler, mock.patch("builtins.open") as open_file:
            importlib.reload(customlogger)

        file_handler.assert_not_called()
        rotating_handler.assert_not_called()
        add_handler.assert_not_called()
        open_file.assert_not_called()
        self.assertEqual(root.handlers, root_handlers)
        self.assertEqual(root.level, root_level)
        self.assertIs(logging.getLoggerClass(), logger_class)

    def test_console_api_and_console_implementation_are_absent(self):
        self.assertFalse(hasattr(customlogger, "console_logger"))
        self.assertFalse(hasattr(customlogger, "LoggingFormatter"))
        for colour_name in (
            "grey",
            "yellow",
            "red",
            "bold_red",
            "magenta",
            "green",
            "reset",
        ):
            self.assertFalse(hasattr(customlogger, colour_name))

        source = inspect.getsource(customlogger)
        self.assertNotIn("StreamHandler", source)
        self.assertNotIn("__main__", source)

    def test_global_logging_state_is_not_extended_or_reconfigured(self):
        root = logging.getLogger()
        root_handlers = list(root.handlers)
        root_level = root.level
        logger_class = logging.getLoggerClass()

        logger = self.make_logger()

        for method_name in ("ok", "OK", "finder", "FINDER"):
            self.assertFalse(hasattr(logging, method_name))
            self.assertFalse(hasattr(logging.getLogger("unrelated"), method_name))
            self.assertTrue(hasattr(logger, method_name))
        self.assertEqual(root.handlers, root_handlers)
        self.assertEqual(root.level, root_level)
        self.assertIs(logging.getLoggerClass(), logger_class)

    def test_file_logger_returns_non_root_logger_with_propagation_disabled(self):
        explicit = self.make_logger("explicit.log")
        default = customlogger.file_logger(self.directory / "default.log")
        self.loggers.append(default)

        for logger in (explicit, default):
            self.assertIsInstance(logger, logging.Logger)
            self.assertIsNot(logger, logging.getLogger())
            self.assertFalse(logger.propagate)

    def test_all_custom_methods_emit_their_corresponding_levels(self):
        logger = self.make_logger()
        logger.ok("lower OK")
        logger.OK("upper OK")
        logger.finder("lower finder")
        logger.FINDER("upper finder")
        self.flush(logger)

        records = self.read().splitlines()
        self.assertEqual(len(records), 4)
        self.assertIn("OK", records[0])
        self.assertIn("OK", records[1])
        self.assertIn("FinDer", records[2])
        self.assertIn("FinDer", records[3])

    def test_custom_methods_accept_formatting_and_logging_keywords(self):
        logger = self.make_logger()
        logger.ok("value %s", 7, extra={"accepted": True})
        try:
            raise RuntimeError("trace detail")
        except RuntimeError:
            logger.finder("failed %s", "cleanly", exc_info=True)
        self.flush(logger)

        content = self.read()
        self.assertIn("value 7", content)
        self.assertIn("failed cleanly", content)
        self.assertIn("RuntimeError: trace detail", content)

    def test_custom_methods_attribute_default_and_explicit_stacklevels(self):
        logger = self.make_logger()

        default_line = inspect.currentframe().f_lineno + 1
        logger.ok("default caller")

        def wrapper():
            logger.finder("explicit caller", stacklevel=2)

        explicit_line = inspect.currentframe().f_lineno + 1
        wrapper()
        self.flush(logger)

        content = self.read()
        self.assertIn(
            "default caller ({0}:{1})".format(os.path.basename(__file__), default_line),
            content,
        )
        self.assertIn(
            "explicit caller ({0}:{1})".format(
                os.path.basename(__file__), explicit_line
            ),
            content,
        )

    def test_repeated_equivalent_setup_reuses_logger_and_handler(self):
        path = self.directory / "same.log"
        logger = customlogger.file_logger(path, module_name=self.id())
        self.loggers.append(logger)
        original_handler = logger.handlers[0]

        equivalent_path = os.path.join(
            str(self.directory), "unused-segment", "..", "same.log"
        )
        with mock.patch.object(
            customlogger, "_new_handler", wraps=customlogger._new_handler
        ) as candidate_handler:
            repeated = customlogger.file_logger(
                equivalent_path, module_name=self.id()
            )
        self.loggers.append(repeated)
        repeated.info("one record")
        self.flush(repeated)

        candidate_handler.assert_not_called()
        self.assertIs(repeated, logger)
        self.assertEqual(repeated.handlers, [original_handler])
        self.assertEqual(self.read("same.log").count("one record"), 1)

    def test_repeated_setup_changes_level_without_replacing_handler(self):
        logger = self.make_logger(level=logging.WARNING)
        handler = logger.handlers[0]
        repeated = customlogger.file_logger(
            self.directory / "test.log",
            module_name=self.id(),
            level=logging.DEBUG,
        )
        self.loggers.append(repeated)

        self.assertIs(repeated.handlers[0], handler)
        self.assertEqual(repeated.level, logging.DEBUG)
        repeated.debug("now visible")
        self.flush(repeated)
        self.assertIn("now visible", self.read())

    def test_repeated_overwrite_does_not_retruncate(self):
        for rotate in (False, True):
            with self.subTest(rotate=rotate):
                filename = "repeated-{0}.log".format(rotate)
                logical_name = "{0}.{1}".format(self.id(), rotate)
                logger = self.make_logger(
                    filename,
                    module_name=logical_name,
                    overwrite=True,
                    rotate=rotate,
                )
                logger.info("first content")
                self.flush(logger)

                repeated = customlogger.file_logger(
                    self.directory / filename,
                    module_name=logical_name,
                    overwrite=True,
                    rotate=rotate,
                )
                self.loggers.append(repeated)
                repeated.info("second content")
                self.flush(repeated)

                content = self.read(filename)
                self.assertIn("first content", content)
                self.assertIn("second content", content)

    def test_same_logical_name_has_isolated_destinations(self):
        logical_name = self.id()
        first = self.make_logger("first.log", module_name=logical_name)
        second = self.make_logger("second.log", module_name=logical_name)
        first.ok("first only")
        second.finder("second only")
        self.flush(first)
        self.flush(second)

        first_content = self.read("first.log")
        second_content = self.read("second.log")
        self.assertIn("first only", first_content)
        self.assertNotIn("second only", first_content)
        self.assertIn("second only", second_content)
        self.assertNotIn("first only", second_content)

    def test_different_logical_names_cannot_share_a_destination(self):
        self.make_logger(module_name=self.id() + ".first")
        with self.assertRaises(ValueError):
            customlogger.file_logger(
                self.directory / "test.log",
                module_name=self.id() + ".second",
            )

    def test_conflicting_rotation_mode_is_rejected(self):
        logger = self.make_logger(rotate=False)
        handler = logger.handlers[0]
        with self.assertRaises(ValueError):
            customlogger.file_logger(
                self.directory / "test.log",
                module_name=self.id(),
                rotate=True,
            )
        self.assertEqual(logger.handlers, [handler])

    def test_append_and_overwrite_for_non_rotating_handler(self):
        append_path = self.directory / "append.log"
        append_path.write_text("existing append\n", encoding="utf-8")
        append_logger = self.make_logger("append.log", overwrite=False)
        append_logger.info("new append")
        self.flush(append_logger)
        self.assertIn("existing append", self.read("append.log"))
        self.assertIn("new append", self.read("append.log"))

        overwrite_path = self.directory / "overwrite.log"
        overwrite_path.write_text("remove overwrite\n", encoding="utf-8")
        overwrite_logger = self.make_logger("overwrite.log", overwrite=True)
        overwrite_logger.info("new overwrite")
        self.flush(overwrite_logger)
        self.assertNotIn("remove overwrite", self.read("overwrite.log"))
        self.assertIn("new overwrite", self.read("overwrite.log"))

    def test_append_and_overwrite_for_rotating_handler(self):
        append_path = self.directory / "rotate-append.log"
        append_path.write_text("existing rotating append\n", encoding="utf-8")
        append_logger = self.make_logger(
            "rotate-append.log", overwrite=False, rotate=True
        )
        append_logger.info("new rotating append")
        self.flush(append_logger)
        self.assertIn("existing rotating append", self.read("rotate-append.log"))
        self.assertIn("new rotating append", self.read("rotate-append.log"))

        overwrite_path = self.directory / "rotate-overwrite.log"
        overwrite_path.write_text("remove rotating overwrite\n", encoding="utf-8")
        overwrite_logger = self.make_logger(
            "rotate-overwrite.log", overwrite=True, rotate=True
        )
        overwrite_logger.info("new rotating overwrite")
        self.flush(overwrite_logger)
        self.assertNotIn(
            "remove rotating overwrite", self.read("rotate-overwrite.log")
        )
        self.assertIn("new rotating overwrite", self.read("rotate-overwrite.log"))

    def test_rotation_is_bounded_with_backups(self):
        with mock.patch.object(
            customlogger, "_ROTATION_MAX_BYTES", 200
        ), mock.patch.object(customlogger, "_ROTATION_BACKUP_COUNT", 2):
            logger = self.make_logger(rotate=True)
        handler = logger.handlers[0]
        self.assertIsInstance(handler, logging.handlers.RotatingFileHandler)
        self.assertGreater(handler.maxBytes, 0)
        self.assertGreaterEqual(handler.backupCount, 1)

        for record_number in range(20):
            logger.info("rotation record %02d %s", record_number, "x" * 80)
        self.flush(logger)

        backups = list(self.directory.glob("test.log.*"))
        self.assertGreaterEqual(len(backups), 1)
        self.assertLessEqual(len(backups), handler.backupCount)

    def test_rotating_handler_failure_propagates_without_fallback(self):
        path = self.directory / "failure.log"
        with mock.patch.object(
            customlogger.logging.handlers,
            "RotatingFileHandler",
            side_effect=OSError("cannot rotate"),
        ) as rotating_handler, mock.patch.object(
            customlogger.logging, "FileHandler"
        ) as file_handler:
            with self.assertRaisesRegex(OSError, "cannot rotate"):
                customlogger.file_logger(
                    path,
                    module_name=self.id(),
                    rotate=True,
                )

        rotating_handler.assert_called_once()
        file_handler.assert_not_called()

    def test_file_output_contains_no_ansi_sequences(self):
        logger = self.make_logger()
        logger.ok("plain output")
        self.flush(logger)
        self.assertNotIn("\x1b", self.read())

    def test_threaded_multiline_records_remain_complete_and_contiguous(self):
        logger = self.make_logger()
        thread_count = 6
        records_per_thread = 20

        def write_records(thread_number):
            for record_number in range(records_per_thread):
                marker = "T{0:02d}-R{1:02d}".format(
                    thread_number, record_number
                )
                logger.info("%s-start\n%s-end", marker, marker)

        threads = [
            threading.Thread(target=write_records, args=(thread_number,))
            for thread_number in range(thread_count)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.flush(logger)

        lines = self.read().splitlines()
        self.assertEqual(len(lines), thread_count * records_per_thread * 2)
        for line_number in range(0, len(lines), 2):
            start_marker = lines[line_number].split("-start", 1)[0].split()[-1]
            self.assertIn(start_marker + "-end", lines[line_number + 1])

    def test_transient_file_logger_appends_without_registry_or_handler_leaks(self):
        path = self.directory / "transient.log"
        path.write_text("existing content\n", encoding="utf-8")
        configurations_before = dict(customlogger._CONFIGURATIONS)
        owners_before = dict(customlogger._DESTINATION_OWNERS)

        with customlogger.transient_file_logger(path) as logger:
            handler = logger.handlers[0]
            logger.ok("transient custom method")
            logger.finder("transient finder output")
            self.flush(logger)
            self.assertIsNotNone(handler.stream)

        self.assertEqual(logger.handlers, [])
        self.assertIsNone(handler.stream)
        self.assertEqual(customlogger._CONFIGURATIONS, configurations_before)
        self.assertEqual(customlogger._DESTINATION_OWNERS, owners_before)
        content = path.read_text(encoding="utf-8")
        self.assertIn("existing content", content)
        self.assertIn("transient custom method", content)
        self.assertIn("transient finder output", content)


if __name__ == "__main__":
    unittest.main()
