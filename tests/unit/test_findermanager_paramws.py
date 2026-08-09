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
    from paramws.clients import (PeakMotionData, ShakeMapEventData,
                                 ShakeMapStationAmplitudes)
    from pyfinder import finderexec, findermanager
    from pyfinder.utils import dataformatter
finally:
    if _original_paramws_log_file is None:
        os.environ.pop("PARAMWS_LOG_FILE", None)
    else:
        os.environ["PARAMWS_LOG_FILE"] = _original_paramws_log_file


class _EventModel:
    """Small event double exposing only the manager's metadata interface."""

    def __init__(self, name):
        self.name = name
        self.get_event_data = Mock(
            side_effect=AssertionError(
                "The separate ParamWS event must not be treated as amplitudes"))

    def get_origin_time(self):
        return f"{self.name}-origin"

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
        manager.configuration = {}
        manager.finder_configuration_name = "offline-test"
        manager.finder_configuration = {"DATA_FOLDER": "offline-test"}
        manager.metadata = {}
        manager.logger = Mock()
        return manager

    def _exercise(self, rrsm_result, esm_result, *, rrsm_raw=None,
                  esm_raw=None):
        manager = self._manager()
        rrsm_raw = [] if rrsm_raw is None else rrsm_raw
        esm_raw = [] if esm_raw is None else esm_raw

        with patch.object(findermanager, "RRSMPeakMotionClient") as rrsm_type, \
                patch.object(findermanager, "ESMShakeMapClient") as esm_type, \
                patch.object(
                    findermanager.RRSMPeakMotionDataFormatter,
                    "extract_raw_stations",
                    return_value=rrsm_raw,
                ) as rrsm_formatter, \
                patch.object(
                    findermanager,
                    "ESMShakeMapDataFormatter",
                ) as esm_formatter_type, \
                patch.object(findermanager, "StationMerger") as merger_type:
            rrsm_type.return_value.query.return_value = rrsm_result
            esm_type.return_value.query.return_value = esm_result
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
            "rrsm_formatter": rrsm_formatter,
            "esm_type": esm_type,
            "esm_client": esm_type.return_value,
            "esm_formatter_type": esm_formatter_type,
            "esm_formatter": esm_formatter,
            "merger_type": merger_type,
        }

    def test_rrsm_keeps_tuple_event_separate_from_peak_motion_dataset(self):
        rrsm_event = _EventModel("rrsm")
        peak_motion = object()
        rrsm_datasets = {"peak_motion": peak_motion}

        observed = self._exercise(
            (200, rrsm_event, rrsm_datasets),
            (404, None, {"station_amplitudes": None}),
            rrsm_raw=["rrsm-raw"],
        )

        observed["rrsm_type"].assert_called_once_with()
        observed["rrsm_client"].query.assert_called_once_with(
            event_id=self.event_id)
        rrsm_event.get_event_data.assert_not_called()
        observed["rrsm_formatter"].assert_called_once_with(
            event_data=peak_motion,
            amplitudes=peak_motion,
        )
        self.assertIsNot(
            observed["rrsm_formatter"].call_args.kwargs["amplitudes"],
            rrsm_datasets,
        )
        self.assertEqual(
            observed["manager"].metadata["origin_time"],
            "rrsm-origin",
        )
        self.assertEqual(observed["manager"].metadata["RRSM_status"],
                         "Success")

    def test_esm_keeps_event_separate_from_station_amplitude_dataset(self):
        esm_event = _EventModel("esm")
        station_amplitudes = object()
        esm_datasets = {"station_amplitudes": station_amplitudes}

        observed = self._exercise(
            (503, None, {"peak_motion": None}),
            (200, esm_event, esm_datasets),
            esm_raw=["esm-raw"],
        )

        observed["esm_type"].assert_called_once_with()
        observed["esm_client"].query.assert_called_once_with(
            event_id=self.event_id)
        observed["esm_formatter_type"].assert_called_once_with(
            logger=observed["manager"].logger,
            configuration=observed["manager"].configuration,
        )
        observed["esm_formatter"].assert_called_once_with(
            event_data=esm_event,
            amplitudes=station_amplitudes,
        )
        self.assertIsNot(
            observed["esm_formatter"].call_args.kwargs["amplitudes"],
            esm_datasets,
        )
        self.assertEqual(
            observed["manager"].metadata["origin_time"],
            "esm-origin",
        )
        self.assertEqual(observed["manager"].metadata["ESM_status"],
                         "Success")

    def test_esm_requested_none_dataset_is_not_treated_as_amplitudes(self):
        esm_event = _EventModel("esm")
        observed = self._exercise(
            (404, None, {"peak_motion": None}),
            (200, esm_event, {"station_amplitudes": None}),
        )

        observed["esm_client"].query.assert_called_once_with(
            event_id=self.event_id)
        observed["esm_formatter"].assert_not_called()
        observed["merger_type"].return_value.merge.assert_called_once_with(
            esm_data=[],
            rrsm_data=[],
        )
        self.assertIsNone(observed["result"])

    def test_non_200_codes_retain_partial_event_and_dataset_values(self):
        rrsm_event = _EventModel("rrsm")
        esm_event = _EventModel("esm")
        peak_motion = object()
        station_amplitudes = object()

        observed = self._exercise(
            (503, rrsm_event, {"peak_motion": peak_motion}),
            (502, esm_event,
             {"station_amplitudes": station_amplitudes}),
            rrsm_raw=["rrsm-raw"],
            esm_raw=["esm-raw"],
        )

        self.assertEqual(observed["manager"].metadata["RRSM_status"],
                         "Failed with HTTP 503")
        self.assertEqual(observed["manager"].metadata["ESM_status"],
                         "Failed with HTTP 502")
        observed["rrsm_formatter"].assert_called_once_with(
            event_data=peak_motion,
            amplitudes=peak_motion,
        )
        observed["esm_formatter"].assert_called_once_with(
            event_data=esm_event,
            amplitudes=station_amplitudes,
        )

    def test_missing_required_dataset_keys_fail_visibly(self):
        cases = (
            ((200, _EventModel("rrsm"), {}),
             (200, _EventModel("esm"),
              {"station_amplitudes": object()}),
             "peak_motion"),
            ((404, None, {"peak_motion": None}),
             (200, _EventModel("esm"), {}),
             "station_amplitudes"),
        )

        for rrsm_result, esm_result, missing_key in cases:
            with self.subTest(missing_key=missing_key), \
                    patch.object(
                        findermanager,
                        "RRSMPeakMotionClient",
                    ) as rrsm_type, \
                    patch.object(
                        findermanager,
                        "ESMShakeMapClient",
                    ) as esm_type:
                rrsm_type.return_value.query.return_value = rrsm_result
                esm_type.return_value.query.return_value = esm_result

                with self.assertRaisesRegex(KeyError, missing_key):
                    self._manager().process_event(self.event_id)

                rrsm_type.return_value.query.assert_called_once_with(
                    event_id=self.event_id)
                if missing_key == "peak_motion":
                    esm_type.assert_not_called()
                else:
                    esm_type.return_value.query.assert_called_once_with(
                        event_id=self.event_id)

    def test_non_mapping_datasets_fail_visibly(self):
        with patch.object(
                findermanager, "RRSMPeakMotionClient") as rrsm_type, \
                patch.object(
                    findermanager, "ESMShakeMapClient") as esm_type:
            rrsm_type.return_value.query.return_value = (
                200, _EventModel("rrsm"), None)

            with self.assertRaises(TypeError):
                self._manager().process_event(self.event_id)

            rrsm_type.return_value.query.assert_called_once_with(
                event_id=self.event_id)
            esm_type.assert_not_called()

    def test_transport_and_content_failures_propagate_without_query_retry(self):
        failures = (
            ConnectionError("transport exhausted"),
            ValueError("invalid ParamWS content"),
        )

        for failure in failures:
            with self.subTest(failure=type(failure).__name__), \
                    patch.object(
                        findermanager,
                        "RRSMPeakMotionClient",
                    ) as rrsm_type, \
                    patch.object(
                        findermanager,
                        "ESMShakeMapClient",
                    ) as esm_type:
                rrsm_type.return_value.query.side_effect = failure

                with self.assertRaises(type(failure)) as raised:
                    self._manager().process_event(self.event_id)

                self.assertIs(raised.exception, failure)
                rrsm_type.assert_called_once_with()
                rrsm_type.return_value.query.assert_called_once_with(
                    event_id=self.event_id)
                esm_type.assert_not_called()

    def test_paramws_models_are_the_public_exported_class_objects(self):
        self.assertIs(dataformatter.PeakMotionData, PeakMotionData)
        self.assertIs(dataformatter.ShakeMapEventData, ShakeMapEventData)
        self.assertIs(
            dataformatter.ShakeMapStationAmplitudes,
            ShakeMapStationAmplitudes,
        )
        self.assertIs(finderexec.PeakMotionData, PeakMotionData)
        self.assertIs(
            finderexec.ShakeMapStationAmplitudes,
            ShakeMapStationAmplitudes,
        )

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
