"""Tests that unfinished local downstream operations remain inactive."""

import ast
import atexit
import builtins
import inspect
import logging
import os
from pathlib import Path
import smtplib
import tempfile
import unittest
from unittest import mock


_PARAMWS_LOG_DIRECTORY = tempfile.TemporaryDirectory(
    prefix="pyfinder-inactive-downstream-import-"
)
atexit.register(_PARAMWS_LOG_DIRECTORY.cleanup)
_original_paramws_log_file = os.environ.get("PARAMWS_LOG_FILE")
os.environ["PARAMWS_LOG_FILE"] = str(
    Path(_PARAMWS_LOG_DIRECTORY.name) / "paramws.log"
)
try:
    from pyfinder import findermanager
finally:
    if _original_paramws_log_file is None:
        os.environ.pop("PARAMWS_LOG_FILE", None)
    else:
        os.environ["PARAMWS_LOG_FILE"] = _original_paramws_log_file


class InactiveDownstreamTests(unittest.TestCase):
    def test_manager_has_no_active_local_shakemap_or_email_calls(self):
        tree = ast.parse(inspect.getsource(findermanager))
        forbidden_modules = {
            "utils.shakemap",
            "pyfinder.utils.shakemap",
            "services.alert",
            "pyfinder.services.alert",
        }
        active_imports = []
        forbidden_calls = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module in forbidden_modules:
                    active_imports.append(node.module)
            elif isinstance(node, ast.Import):
                active_imports.extend(
                    alias.name
                    for alias in node.names
                    if alias.name in forbidden_modules
                )
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    call_name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    call_name = node.func.attr
                else:
                    continue
                if call_name in {
                    "ShakeMapExporter",
                    "ShakeMapTrigger",
                    "archive_products",
                    "send_email_with_attachment",
                    "_send_failure_email",
                }:
                    forbidden_calls.append(call_name)

        self.assertEqual(active_imports, [])
        self.assertEqual(forbidden_calls, [])

    def test_failure_notification_method_opens_no_email_or_local_module(self):
        manager = findermanager.FinDerManager.__new__(
            findermanager.FinDerManager
        )
        manager.logger = mock.Mock(spec=logging.Logger)
        manager.metadata = {}
        original_import = builtins.__import__
        attempted_downstream_imports = []

        def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name in {
                "utils.shakemap",
                "pyfinder.utils.shakemap",
                "services.alert",
                "pyfinder.services.alert",
            }:
                attempted_downstream_imports.append(name)
                raise AssertionError("inactive downstream import attempted")
            return original_import(name, globals, locals, fromlist, level)

        with mock.patch.object(
            builtins,
            "__import__",
            side_effect=guarded_import,
        ), mock.patch.object(
            smtplib,
            "SMTP",
            side_effect=AssertionError("SMTP connection attempted"),
        ) as smtp_connection:
            result = manager._send_failure_email("event-1")

        self.assertIsNone(result)
        self.assertEqual(attempted_downstream_imports, [])
        smtp_connection.assert_not_called()


if __name__ == "__main__":
    unittest.main()
