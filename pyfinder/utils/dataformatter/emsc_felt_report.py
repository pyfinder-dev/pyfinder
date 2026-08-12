# -*- coding: utf-8 -*-
"""Normalization of EMSC felt-intensity observations."""

import math
import numbers
from typing import List

import numpy as np
from paramws.clients import FeltReportEventData, FeltReportIntensityData

from ...pyfinderconfig import pyfinderconfig
from ..calculator import Calculator
from ..station_merger import RawStationMeasurement
from ..timeutils import get_epoch_time
from .base import BaseDataFormatter

_FELT_REPORT_STATION_CODE_PATTERNS = (
    ("LDDD", 26 * 999),
    ("DLDD", 10 * 26 * 99),
    ("DDLD", 10 * 10 * 26 * 10),
    ("DDDL", 10 * 10 * 10 * 26),
    ("LLDD", 26 * 26 * 10 * 10),
    ("LDLD", 26 * 10 * 26 * 10),
    ("LDDL", 26 * 10 * 10 * 26),
    ("DLLD", 10 * 26 * 26 * 10),
    ("DLDL", 10 * 26 * 10 * 26),
    ("DDLL", 10 * 10 * 26 * 26),
    ("LLLD", 26 * 26 * 26 * 10),
    ("LLDL", 26 * 26 * 10 * 26),
    ("LDLL", 26 * 10 * 26 * 26),
    ("DLLL", 10 * 26 * 26 * 26),
)
_FELT_REPORT_STATION_CODE_COUNT = sum(
    pattern_size for _pattern, pattern_size
    in _FELT_REPORT_STATION_CODE_PATTERNS
)


def _felt_report_station_code(index):
    """Return the station code at one zero-based FeltReport sequence index."""
    if isinstance(index, bool) or not isinstance(index, numbers.Integral):
        raise TypeError("FeltReport station-code index must be an integer")
    if index < 0 or index >= _FELT_REPORT_STATION_CODE_COUNT:
        raise ValueError(
            "FeltReport station-code namespace exhausted after "
            f"{_FELT_REPORT_STATION_CODE_COUNT} retained reports"
        )

    remaining = int(index)
    for pattern, pattern_size in _FELT_REPORT_STATION_CODE_PATTERNS:
        if remaining >= pattern_size:
            remaining -= pattern_size
            continue

        # The first two patterns omit an all-zero numerical suffix. Their
        # unusual sequence gives identity to reports that have no provider
        # station code while keeping every generated value four characters.
        if pattern == "LDDD":
            letter_index, numerical_suffix = divmod(remaining, 999)
            return (
                chr(ord("A") + letter_index)
                + f"{numerical_suffix + 1:03d}"
            )
        if pattern == "DLDD":
            prefix_index, numerical_suffix = divmod(remaining, 99)
            digit_index, letter_index = divmod(prefix_index, 26)
            return (
                str(digit_index)
                + chr(ord("A") + letter_index)
                + f"{numerical_suffix + 1:02d}"
            )

        characters = [None] * len(pattern)
        for position in range(len(pattern) - 1, -1, -1):
            radix = 26 if pattern[position] == "L" else 10
            remaining, value = divmod(remaining, radix)
            characters[position] = (
                chr(ord("A") + value)
                if pattern[position] == "L"
                else str(value)
            )
        return "".join(characters)

    raise AssertionError("FeltReport station-code pattern table is incomplete")
class EMSCFeltReportDataFormatter(BaseDataFormatter):
    """Normalize one public EMSC felt-intensity event view."""

    def __init__(self, logger=None, configuration=None):
        super().__init__(logger=logger)
        self.configuration = (
            pyfinderconfig if configuration is None else configuration)

    def _felt_report_component_code(self):
        """Return the configured complete-SNCL component or fail visibly."""
        try:
            component_code = self.configuration["finder-executable"][
                "felt-report-component-code"
            ]
        except (KeyError, TypeError) as error:
            raise ValueError(
                "finder-executable.felt-report-component-code is required"
            ) from error

        if (
            isinstance(component_code, str)
            and component_code
            and component_code.isascii()
            and component_code.isalnum()
            and component_code == component_code.upper()
        ):
            return component_code
        raise ValueError(
            "finder-executable.felt-report-component-code must be a nonempty "
            "ASCII uppercase alphanumeric SNCL component without dots, "
            "whitespace, path separators, or control characters"
        )

    @staticmethod
    def _numeric_rejection_reason(value):
        """Describe why one provider value is not a finite real number."""
        if value is None:
            return "missing value"
        if isinstance(value, (bool, np.bool_)):
            return "boolean value is not accepted as numerical"
        if not isinstance(value, numbers.Real):
            return "nonnumeric value"
        if not math.isfinite(value):
            return "nonfinite value"
        return None

    @staticmethod
    def _event_identity(event_data):
        """Return the best public EMSC event identifier available."""
        event_identity = event_data.get_event_id()
        if event_identity is None:
            event_identity = event_data.get_event_unid()
        return event_identity

    def _event_context_is_invalid(self, event_identity, field_name, value,
                                  minimum=None, maximum=None):
        """Validate one required event value and emit the single fatal error."""
        reason = self._numeric_rejection_reason(value)
        if reason is None and minimum is not None and value < minimum:
            reason = f"value is below {minimum}"
        if reason is None and maximum is not None and value > maximum:
            reason = f"value is above {maximum}"
        if reason is None:
            return False

        identity = (
            repr(event_identity)
            if event_identity is not None
            else "unknown event"
        )
        self.logger.error(
            f"EMSC event {identity} has invalid {field_name} {value!r}: "
            f"{reason}; normalization produced no usable felt records")
        return True

    def _valid_intensity(self, row_index, intensity):
        """Return whether one selected EMSC provider intensity is usable."""
        reason = self._numeric_rejection_reason(intensity)
        if reason is None and intensity < 1:
            reason = "value is below the accepted minimum 1"
        if reason is None and np.round(intensity) > 10:
            reason = (
                f"NumPy-rounded value {np.round(intensity)!r} is above 10")
        if reason is None:
            return True

        self.logger.warning(
            f"EMSC felt row {row_index} rejected: invalid selected intensity "
            f"{intensity!r} ({reason})")
        return False

    def _coordinate(self, row, row_index, coordinate_name, minimum, maximum):
        """Validate one row coordinate without coercion or range rewriting."""
        coordinate = self._provider_value(row, "get", coordinate_name)
        coordinate_label = {
            "lat": "latitude",
            "lon": "longitude",
        }.get(coordinate_name, coordinate_name)
        reason = self._numeric_rejection_reason(coordinate)
        if reason is None and coordinate < minimum:
            reason = f"value is below {minimum}"
        if reason is None and coordinate > maximum:
            reason = f"value is above {maximum}"
        if reason is None:
            return coordinate

        self.logger.warning(
            f"EMSC felt row {row_index} rejected: invalid {coordinate_label} "
            f"{coordinate!r} ({reason})")
        return None

    def extract_raw_stations(
            self,
            event_data: FeltReportEventData,
            felt_reports: FeltReportIntensityData,
    ) -> List[RawStationMeasurement]:
        """
        Normalize the requested event's EMSC felt-intensity rows.

        Rows remain independent and in provider order. Dependency model access
        failures cross the explicit public-model boundary for orchestration.
        """
        component_code = self._felt_report_component_code()
        event_identity = self._event_identity(event_data)
        event_latitude = event_data.get_latitude()
        event_longitude = event_data.get_longitude()
        event_magnitude = event_data.get_magnitude()
        event_depth = event_data.get_depth()
        event_time = event_data.get_event_time()

        context_rules = (
            ("latitude", event_latitude, -90, 90),
            ("longitude", event_longitude, -180, 180),
            ("magnitude", event_magnitude, None, None),
            ("depth", event_depth, 0, None),
        )
        for field_name, value, minimum, maximum in context_rules:
            if self._event_context_is_invalid(
                    event_identity, field_name, value, minimum, maximum):
                return []

        # EMSC rows have no report timestamp. Preserve the current project
        # behavior by converting the matching event origin time once and
        # reusing it without introducing new timestamp policy here.
        time_epoch = get_epoch_time(event_time)
        intensity_rows = self._provider_collection(
            felt_reports,
            "get_intensities",
        )
        raw_stations = []
        missing = object()

        for row_index, row in enumerate(intensity_rows):
            corrected_intensity = self._provider_value(
                row,
                "get",
                "corrected",
                missing,
            )
            if corrected_intensity is missing or corrected_intensity is None:
                selected_intensity = self._provider_value(row, "get", "raw")
                corrected_reason = (
                    "missing" if corrected_intensity is missing else "None")
                self.logger.warning(
                    f"EMSC felt row {row_index} using raw intensity fallback "
                    f"{selected_intensity!r}: corrected is "
                    f"{corrected_reason}")
            else:
                selected_intensity = corrected_intensity

            if not self._valid_intensity(row_index, selected_intensity):
                continue

            latitude = self._coordinate(
                row, row_index, "lat", -90, 90)
            longitude = self._coordinate(
                row, row_index, "lon", -180, 180)
            if latitude is None or longitude is None:
                continue

            epicentral_distance = Calculator.haversine(
                event_latitude,
                event_longitude,
                latitude,
                longitude,
            )
            predicted_intensity = Calculator.I_Allen2012_Rhypo(
                event_magnitude,
                event_depth,
                epicentral_distance,
            )
            if not predicted_intensity > 3:
                self.logger.warning(
                    f"EMSC felt row {row_index} rejected by Allen reach: "
                    f"predicted intensity {predicted_intensity!r} is not "
                    f"greater than 3 for selected value "
                    f"{selected_intensity!r}")
                continue

            intensity_residual = abs(
                selected_intensity - predicted_intensity)
            if intensity_residual > 3:
                self.logger.warning(
                    f"EMSC felt row {row_index} rejected by Allen residual: "
                    f"selected intensity {selected_intensity!r}, predicted "
                    f"intensity {predicted_intensity!r}, residual "
                    f"{intensity_residual!r} is above 3")
                continue

            log10_pga_cm_s2 = Calculator.I_to_PGA_Wordon2012(
                selected_intensity)
            try:
                with np.errstate(
                        over="ignore", under="ignore", invalid="ignore"):
                    pga_cm_s2 = 10 ** log10_pga_cm_s2
            except OverflowError:
                self.logger.warning(
                    f"EMSC felt row {row_index} rejected: invalid converted "
                    f"PGA for selected intensity {selected_intensity!r}; "
                    f"exponentiating log10 value {log10_pga_cm_s2!r} "
                    "overflowed")
                continue

            if isinstance(pga_cm_s2, np.ndarray) and pga_cm_s2.ndim == 0:
                pga_cm_s2 = pga_cm_s2.item()
            elif isinstance(pga_cm_s2, np.generic):
                pga_cm_s2 = pga_cm_s2.item()

            conversion_reason = self._numeric_rejection_reason(pga_cm_s2)
            if conversion_reason is None and pga_cm_s2 <= 0:
                conversion_reason = "converted value is not positive"
            if conversion_reason is not None:
                self.logger.warning(
                    f"EMSC felt row {row_index} rejected: invalid converted "
                    f"PGA {pga_cm_s2!r} from selected intensity "
                    f"{selected_intensity!r} ({conversion_reason})")
                continue

            raw_stations.append(RawStationMeasurement(
                latitude=latitude,
                longitude=longitude,
                network="FR",
                station=_felt_report_station_code(len(raw_stations)),
                location="00",
                channel=component_code,
                pga=pga_cm_s2,
                timestamp=time_epoch,
                source="EMSC",
                provider_value=selected_intensity,
                provider_unit="EMS-98",
            ))

        if not raw_stations:
            self.logger.error(
                f"EMSC felt normalization produced zero usable records for "
                f"event {event_identity!r}")
        return raw_stations
