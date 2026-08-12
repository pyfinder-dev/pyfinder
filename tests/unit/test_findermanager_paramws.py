"""Offline tests for PyFinder's ParamWS public-result adaptation."""

import atexit
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import Mock, patch


# Importing ParamWS initializes its package-owned file logger. Keep that
# dependency side effect in a temporary directory so this unit module never
# creates ``paramws.log`` in the repository or another production path.
_PARAMWS_LOG_DIRECTORY = tempfile.TemporaryDirectory(
    prefix="pyfinder-paramws-unit-")
atexit.register(_PARAMWS_LOG_DIRECTORY.cleanup)
_original_paramws_log_file = os.environ.get("PARAMWS_LOG_FILE")
os.environ["PARAMWS_LOG_FILE"] = str(
    Path(_PARAMWS_LOG_DIRECTORY.name) / "paramws.log")
try:
    from paramws.clients import (
        PeakMotionData,
        ShakeMapStationAmplitudes,
    )
    from pyfinder import finderexec, findermanager
    from pyfinder.utils import dataformatter
finally:
    if _original_paramws_log_file is None:
        os.environ.pop("PARAMWS_LOG_FILE", None)
    else:
        os.environ["PARAMWS_LOG_FILE"] = _original_paramws_log_file


class _EventModel:
    """Small event double exposing only the manager's metadata interface."""

    def __init__(self, name, origin_time=None):
        self.name = name
        self.origin_time = (
            origin_time or "2026-08-10T08:15:30.250000Z"
        )
        self.get_event_data = Mock(
            side_effect=AssertionError(
                "The separate ParamWS event must not be treated as amplitudes"))

    def get_event_id(self):
        return "test-event"

    def get_origin_time(self):
        return self.origin_time

    def get_longitude(self):
        return 12.5

    def get_latitude(self):
        return 45.5

    def get_magnitude(self):
        return 5.6

    def get_depth(self):
        return 10.0

    def get_magnitude_type(self):
        return "Mw"


class FinDerManagerParamWSBoundaryTests(unittest.TestCase):

    event_id = "test-event"

    def _manager(self):
        # Bypass __init__ because logger and output-directory setup are not
        # part of this dependency-boundary test.
        manager = object.__new__(findermanager.FinDerManager)
        manager.options = {"use_library": False}
        manager.configuration = {
            "general": {
                "services-enabled": ["RRSM_PeakMotion", "ESM_ShakeMap"],
                "services-priority": ["ESM_ShakeMap", "RRSM_PeakMotion"],
                "component-selection": "maximum-all",
            }
        }
        manager.finder_configuration_name = "offline-test"
        manager.finder_configuration = {"DATA_FOLDER": "offline-test"}
        manager.metadata = {}
        manager.logger = Mock()
        manager.entry_kind = manager.ON_DEMAND
        manager.event_context = None
        manager.context_diagnostic = None
        return manager

    def _exercise(self, rrsm_result, esm_result, *, rrsm_raw=None,
                  esm_raw=None):
        manager = self._manager()
        rrsm_raw = [] if rrsm_raw is None else rrsm_raw
        esm_raw = [] if esm_raw is None else esm_raw

        with patch.object(findermanager, "RRSMPeakMotionClient") as rrsm_type, \
                patch.object(findermanager, "ESMShakeMapClient") as esm_type, \
                patch.object(
                    findermanager,
                    "RRSMPeakMotionDataFormatter",
                ) as rrsm_formatter_type, \
                patch.object(
                    findermanager,
                    "ESMShakeMapDataFormatter",
                ) as esm_formatter_type, \
                patch.object(findermanager, "StationMerger") as merger_type:
            rrsm_type.return_value.query.return_value = rrsm_result
            esm_type.return_value.query.return_value = esm_result
            rrsm_formatter = (
                rrsm_formatter_type.return_value.extract_raw_stations)
            rrsm_formatter.return_value = rrsm_raw
            esm_formatter = (
                esm_formatter_type.return_value.extract_raw_stations)
            esm_formatter.return_value = esm_raw

            # A false merged value stops the legacy workflow immediately after
            # the values under test reach their current measurement consumers.
            merger_type.return_value.merge.return_value = []
            result = manager.process_event(self.event_id)

        return {
            "manager": manager,
            "result": result,
            "rrsm_type": rrsm_type,
            "rrsm_client": rrsm_type.return_value,
            "rrsm_formatter_type": rrsm_formatter_type,
            "rrsm_formatter": rrsm_formatter,
            "esm_type": esm_type,
            "esm_client": esm_type.return_value,
            "esm_formatter_type": esm_formatter_type,
            "esm_formatter": esm_formatter,
            "merger_type": merger_type,
        }

    def test_public_datasets_remain_separate_from_event_candidates(self):
        rrsm_event = _EventModel("rrsm")
        esm_event = _EventModel("esm")
        peak_motion = Mock(name="peak_motion")
        station_amplitudes = object()

        observed = self._exercise(
            (200, rrsm_event, {"peak_motion": peak_motion}),
            (200, esm_event, {"station_amplitudes": station_amplitudes}),
            rrsm_raw=["rrsm-raw"],
            esm_raw=["esm-raw"],
        )

        observed["rrsm_client"].query.assert_called_once_with(
            event_id=self.event_id)
        observed["esm_client"].query.assert_called_once_with(
            event_id=self.event_id)
        common_context = observed["manager"].event_context
        observed["rrsm_formatter"].assert_called_once_with(
            event_data=common_context,
            amplitudes=peak_motion,
        )
        observed["esm_formatter"].assert_called_once_with(
            event_data=common_context,
            amplitudes=station_amplitudes,
        )
        rrsm_event.get_event_data.assert_not_called()
        esm_event.get_event_data.assert_not_called()

    def test_non_200_usable_results_retain_status_and_normalize(self):
        observed = self._exercise(
            (503, _EventModel("rrsm"), {"peak_motion": object()}),
            (502, _EventModel("esm"), {"station_amplitudes": object()}),
            rrsm_raw=["rrsm-raw"],
            esm_raw=["esm-raw"],
        )

        self.assertEqual(observed["manager"].metadata["RRSM_status"],
                         "Failed with HTTP 503")
        self.assertEqual(observed["manager"].metadata["ESM_status"],
                         "Failed with HTTP 502")
        self.assertEqual(
            observed["manager"].metadata["provider_outcomes"][
                "RRSM_PeakMotion"
            ]["normalized_count"],
            1,
        )
        self.assertEqual(
            observed["manager"].metadata["provider_outcomes"][
                "ESM_ShakeMap"
            ]["normalized_count"],
            1,
        )

    def test_malformed_dataset_is_contained_and_other_provider_continues(self):
        observed = self._exercise(
            (200, _EventModel("rrsm"), None),
            (200, _EventModel("esm"), {"station_amplitudes": object()}),
            esm_raw=["esm-raw"],
        )

        observed["rrsm_formatter"].assert_not_called()
        observed["esm_formatter"].assert_called_once()
        self.assertEqual(
            observed["manager"].metadata["provider_outcomes"][
                "RRSM_PeakMotion"
            ]["failure_kind"],
            "invalid-result",
        )

    def test_query_failure_is_contained_without_retry(self):
        manager = self._manager()
        with patch.object(
                findermanager, "RRSMPeakMotionClient") as rrsm_type, \
                patch.object(
                    findermanager, "ESMShakeMapClient") as esm_type, \
                patch.object(
                    findermanager.ESMShakeMapDataFormatter,
                    "extract_raw_stations",
                    return_value=["esm"],
                ), \
                patch.object(findermanager, "StationMerger") as merger_type:
            rrsm_type.return_value.query.side_effect = ConnectionError(
                "transport exhausted"
            )
            esm_type.return_value.query.return_value = (
                200,
                _EventModel("esm"),
                {"station_amplitudes": object()},
            )
            merger_type.return_value.merge.return_value = []

            self.assertIsNone(manager.process_event(self.event_id))

        rrsm_type.return_value.query.assert_called_once_with(
            event_id=self.event_id)
        esm_type.return_value.query.assert_called_once_with(
            event_id=self.event_id)
        self.assertEqual(
            manager.metadata["provider_outcomes"]["RRSM_PeakMotion"][
                "failure_kind"
            ],
            "exception",
        )

    def test_service_normalizers_accept_public_paramws_models(self):
        event_data = _EventModel("common")
        peak_motion = PeakMotionData(data_dict={})
        station_amplitudes = ShakeMapStationAmplitudes(
            data_dict={"stations": []},
        )

        rrsm_formatter = dataformatter.RRSMPeakMotionDataFormatter(
            logger=Mock(),
            configuration=self._manager().configuration,
        )
        esm_formatter = dataformatter.ESMShakeMapDataFormatter(
            logger=Mock(),
            configuration=self._manager().configuration,
        )

        self.assertEqual(
            rrsm_formatter.extract_raw_stations(event_data, peak_motion),
            [],
        )
        self.assertEqual(
            esm_formatter.extract_raw_stations(event_data, station_amplitudes),
            [],
        )

    def test_finder_executable_does_not_import_provider_models(self):
        self.assertFalse(hasattr(finderexec, "PeakMotionData"))
        self.assertFalse(hasattr(finderexec, "ShakeMapStationAmplitudes"))

    def test_package_import_is_offline_and_has_no_operational_side_effects(self):
        repository_root = Path(__file__).resolve().parents[2]
        production_logs = (
            repository_root / "paramws.log",
            repository_root / "finder_manager.log",
        )
        initial_log_state = {
            path: path.stat() if path.exists() else None
            for path in production_logs
        }

        script = textwrap.dedent("""
            import sqlite3
            import subprocess
            import threading
            import urllib.request
            from unittest.mock import patch

            forbidden = AssertionError("operational work occurred at import")
            with patch.object(urllib.request, "urlopen", side_effect=forbidden), \\
                    patch.object(sqlite3, "connect", side_effect=forbidden), \\
                    patch.object(subprocess, "Popen", side_effect=forbidden), \\
                    patch.object(threading.Thread, "start", side_effect=forbidden):
                import pyfinder.findermanager

            print(pyfinder.findermanager.FinDerManager.__name__)
        """)

        with tempfile.TemporaryDirectory(
                prefix="pyfinder-import-safety-") as temporary_directory:
            environment = os.environ.copy()
            environment["PARAMWS_LOG_FILE"] = str(
                Path(temporary_directory) / "paramws.log")
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            completed = subprocess.run(
                [sys.executable, "-c", script],
                cwd=repository_root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "FinDerManager")
        for path, initial_stat in initial_log_state.items():
            current_stat = path.stat() if path.exists() else None
            self.assertEqual(current_stat, initial_stat)


if __name__ == "__main__":
    unittest.main()
