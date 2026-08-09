"""Offline tests for FinDerManager's normalized observation handoff."""

import atexit
from contextlib import ExitStack
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch


# ParamWS configures its package logger during import. Keep that dependency
# side effect outside the repository when this focused module runs alone.
_PARAMWS_LOG_DIRECTORY = tempfile.TemporaryDirectory(
    prefix="pyfinder-manager-handoff-unit-")
atexit.register(_PARAMWS_LOG_DIRECTORY.cleanup)
_original_paramws_log_file = os.environ.get("PARAMWS_LOG_FILE")
os.environ["PARAMWS_LOG_FILE"] = str(
    Path(_PARAMWS_LOG_DIRECTORY.name) / "paramws.log")
try:
    from paramws.clients import PeakMotionData, ShakeMapStationAmplitudes
    from pyfinder import finderexec, findermanager
finally:
    if _original_paramws_log_file is None:
        os.environ.pop("PARAMWS_LOG_FILE", None)
    else:
        os.environ["PARAMWS_LOG_FILE"] = _original_paramws_log_file


class _ExecutableBoundaryReached(Exception):
    """Stop a test immediately after the executable receives its inputs."""


class _EventModel:
    """Controlled provider-event double for manager metadata collection."""

    def __init__(self, name):
        self.name = name

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


class FinDerManagerNormalizedHandoffTests(unittest.TestCase):

    event_id = "handoff-event"

    @staticmethod
    def _manager():
        # Bypass construction because logger/output setup and configuration
        # selection are independent of the observation-handoff boundary.
        manager = object.__new__(findermanager.FinDerManager)
        manager.options = {"use_library": False}
        manager.configuration = {}
        manager.finder_configuration_name = "offline-test"
        manager.finder_configuration = {"DATA_FOLDER": "offline-test"}
        manager.metadata = {}
        manager.logger = Mock()
        return manager

    @staticmethod
    def _provider_models():
        """Build truthy objects from the public ParamWS model namespace."""
        return (
            PeakMotionData(data_dict={}),
            ShakeMapStationAmplitudes(data_dict={"stations": []}),
        )

    def _exercise(self, *, rrsm_event, esm_event, rrsm_provider,
                  esm_provider, rrsm_records, esm_records, merged_records,
                  expect_execution=True):
        manager = self._manager()

        with ExitStack() as stack:
            rrsm_type = stack.enter_context(patch.object(
                findermanager, "RRSMPeakMotionClient"))
            esm_type = stack.enter_context(patch.object(
                findermanager, "ESMShakeMapClient"))
            rrsm_extract = stack.enter_context(patch.object(
                findermanager.RRSMPeakMotionDataFormatter,
                "extract_raw_stations",
                return_value=rrsm_records,
            ))
            esm_extract = stack.enter_context(patch.object(
                findermanager.ESMShakeMapDataFormatter,
                "extract_raw_stations",
                return_value=esm_records,
            ))
            rrsm_direct_format = stack.enter_context(patch.object(
                findermanager.RRSMPeakMotionDataFormatter,
                "format_data",
            ))
            esm_direct_format = stack.enter_context(patch.object(
                findermanager.ESMShakeMapDataFormatter,
                "format_data",
            ))
            artificial_point_format = stack.enter_context(patch.object(
                findermanager.FinDerFormatterFromRawList,
                "format",
            ))
            merger_type = stack.enter_context(patch.object(
                findermanager, "StationMerger"))
            executable_type = stack.enter_context(patch.object(
                finderexec, "FinDerExecutable"))

            rrsm_type.return_value.query.return_value = (
                200,
                rrsm_event,
                {"peak_motion": rrsm_provider},
            )
            esm_type.return_value.query.return_value = (
                200,
                esm_event,
                {"station_amplitudes": esm_provider},
            )
            merger_type.return_value.merge.return_value = merged_records
            executable = executable_type.return_value
            executable.execute.side_effect = _ExecutableBoundaryReached(
                "normal manager handoff reached the executable")

            if expect_execution:
                with self.assertRaises(_ExecutableBoundaryReached):
                    manager.process_event(self.event_id)
                result = None
            else:
                result = manager.process_event(self.event_id)

        return {
            "manager": manager,
            "result": result,
            "rrsm_extract": rrsm_extract,
            "esm_extract": esm_extract,
            "rrsm_direct_format": rrsm_direct_format,
            "esm_direct_format": esm_direct_format,
            "artificial_point_format": artificial_point_format,
            "merger_type": merger_type,
            "executable_type": executable_type,
            "executable": executable,
        }

    @staticmethod
    def _messages(log_method):
        return [str(call.args[0]) for call in log_method.call_args_list]

    def test_both_normalized_lists_reach_merger_and_only_merge_reaches_finder(self):
        rrsm_event = _EventModel("rrsm")
        esm_event = _EventModel("esm")
        rrsm_provider, esm_provider = self._provider_models()
        rrsm_records = [{"source": "RRSM"}]
        esm_records = [{"source": "ESM"}]
        merged_records = [{"source": "ESM"}, {"source": "RRSM"}]

        observed = self._exercise(
            rrsm_event=rrsm_event,
            esm_event=esm_event,
            rrsm_provider=rrsm_provider,
            esm_provider=esm_provider,
            rrsm_records=rrsm_records,
            esm_records=esm_records,
            merged_records=merged_records,
        )

        observed["merger_type"].return_value.merge.assert_called_once_with(
            esm_data=esm_records,
            rrsm_data=rrsm_records,
        )
        observed["executable"].execute.assert_called_once_with(
            event_data=esm_event,
            amplitudes=merged_records,
        )
        executable_amplitudes = (
            observed["executable"].execute.call_args.kwargs["amplitudes"])
        self.assertIsInstance(executable_amplitudes, list)
        self.assertIsNot(executable_amplitudes, esm_provider)
        self.assertIsNot(executable_amplitudes, rrsm_provider)

    def test_zero_esm_logs_error_and_usable_rrsm_still_reaches_finder(self):
        rrsm_event = _EventModel("rrsm")
        esm_event = _EventModel("esm")
        rrsm_provider, esm_provider = self._provider_models()
        rrsm_records = [{"source": "RRSM"}]
        merged_records = list(rrsm_records)

        observed = self._exercise(
            rrsm_event=rrsm_event,
            esm_event=esm_event,
            rrsm_provider=rrsm_provider,
            esm_provider=esm_provider,
            rrsm_records=rrsm_records,
            esm_records=[],
            merged_records=merged_records,
        )

        errors = self._messages(observed["manager"].logger.error)
        self.assertTrue(any(
            "ESM" in message
            and "zero usable normalized observations" in message
            for message in errors
        ), errors)
        observed["merger_type"].return_value.merge.assert_called_once_with(
            esm_data=[],
            rrsm_data=rrsm_records,
        )
        # Event preference remains ESM-over-RRSM even when ESM contributes no
        # usable normalized observation.
        observed["executable"].execute.assert_called_once_with(
            event_data=esm_event,
            amplitudes=merged_records,
        )
        self.assertIsNot(
            observed["executable"].execute.call_args.kwargs["amplitudes"],
            esm_provider,
        )

    def test_usable_esm_and_zero_rrsm_continues_without_esm_error(self):
        rrsm_event = _EventModel("rrsm")
        esm_event = _EventModel("esm")
        rrsm_provider, esm_provider = self._provider_models()
        esm_records = [{"source": "ESM"}]
        merged_records = list(esm_records)

        observed = self._exercise(
            rrsm_event=rrsm_event,
            esm_event=esm_event,
            rrsm_provider=rrsm_provider,
            esm_provider=esm_provider,
            rrsm_records=[],
            esm_records=esm_records,
            merged_records=merged_records,
        )

        observed["merger_type"].return_value.merge.assert_called_once_with(
            esm_data=esm_records,
            rrsm_data=[],
        )
        observed["executable"].execute.assert_called_once_with(
            event_data=esm_event,
            amplitudes=merged_records,
        )
        errors = self._messages(observed["manager"].logger.error)
        self.assertFalse(any(
            "ESM" in message
            and "zero usable normalized observations" in message
            for message in errors
        ), errors)

    def test_truthy_provider_models_cannot_replace_empty_normalized_lists(self):
        rrsm_event = _EventModel("rrsm")
        esm_event = _EventModel("esm")
        rrsm_provider, esm_provider = self._provider_models()

        self.assertTrue(rrsm_provider)
        self.assertTrue(esm_provider)

        observed = self._exercise(
            rrsm_event=rrsm_event,
            esm_event=esm_event,
            rrsm_provider=rrsm_provider,
            esm_provider=esm_provider,
            rrsm_records=[],
            esm_records=[],
            merged_records=[],
            expect_execution=False,
        )

        self.assertIsNone(observed["result"])
        observed["merger_type"].return_value.merge.assert_called_once_with(
            esm_data=[],
            rrsm_data=[],
        )
        observed["executable_type"].assert_not_called()
        observed["executable"].execute.assert_not_called()
        observed["esm_direct_format"].assert_not_called()
        observed["rrsm_direct_format"].assert_not_called()
        observed["artificial_point_format"].assert_not_called()

        errors = self._messages(observed["manager"].logger.error)
        self.assertTrue(any(
            "ESM" in message
            and "zero usable normalized observations" in message
            for message in errors
        ), errors)
        self.assertTrue(any(
            "All normalized observation sources" in message
            and "zero usable records" in message
            for message in errors
        ), errors)

    def test_absent_esm_result_is_an_empty_list_and_logs_zero_result(self):
        rrsm_event = _EventModel("rrsm")
        rrsm_provider = object()
        rrsm_records = [{"source": "RRSM"}]
        merged_records = list(rrsm_records)

        observed = self._exercise(
            rrsm_event=rrsm_event,
            esm_event=None,
            rrsm_provider=rrsm_provider,
            esm_provider=None,
            rrsm_records=rrsm_records,
            esm_records=[],
            merged_records=merged_records,
        )

        observed["esm_extract"].assert_not_called()
        observed["merger_type"].return_value.merge.assert_called_once_with(
            esm_data=[],
            rrsm_data=rrsm_records,
        )
        observed["executable"].execute.assert_called_once_with(
            event_data=rrsm_event,
            amplitudes=merged_records,
        )
        errors = self._messages(observed["manager"].logger.error)
        self.assertTrue(any(
            "ESM" in message
            and "zero usable normalized observations" in message
            for message in errors
        ), errors)


if __name__ == "__main__":
    unittest.main()
