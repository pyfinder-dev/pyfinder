# -*- coding: utf-8 -*-
""" Classes for handling and formatting data from the web services 
for the FinDer executable. """

import numpy as np
import logging
import math
import numbers
from typing import List, Tuple
import fnmatch
from typing import Union
from .calculator import Calculator
from .timeutils import get_epoch_time
from pyfinder.eventcontext import ProviderModelAccessError
from pyfinder.pyfinderconfig import pyfinderconfig
from paramws.clients import (FeltReportEventData, FeltReportIntensityData,
                             PeakMotionData, ShakeMapEventData,
                             ShakeMapStationAmplitudes)
from pyfinder.finderutils import FinderChannelList
from pyfinder.utils.station_merger import RawStationMeasurement

# Thresholds for the RRSM peak motion data that are used to filter out
# the stations with PGA/PGV values that are not in the range.
RRSM_PEAKMOTION_PGA_MIN = 0.00001
RRSM_PEAKMOTION_PGA_MAX = 10*980.6 # cm/s/s
RRSM_PEAKMOTION_PGV_MIN = 0.000001
RRSM_PEAKMOTION_PGV_MAX = 1.0 # m/s
RRSM_PEAKMOTION_PGV_BROADBAND_MIN = 0.000001
RRSM_PEAKMOTION_PGV_BROADBAND_MAX = 0.013 # m/s


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

class FinDerFormatterFromRawList:

    @staticmethod
    def format(event_lat: float,
               event_lon: float,
               event_depth_km: float,
               event_mag: float,
               event_time_epoch: float,
               station_list: List[RawStationMeasurement]
              ) -> Tuple[bytes, FinderChannelList]:
        """
        Final FinDer-compatible formatter from merged station data.
        """
        is_live_mode = pyfinderconfig["finder-executable"]["finder-live-mode"]
        data_lines = [f"# {int(event_time_epoch)} 0"]
        finder_channels = FinderChannelList()

        # Select best PGA per unique SNCL
        seen_keys = set()
        valid_stations = []

        for sta in station_list:
            key = f"{sta['network']}.{sta['station']}.{sta['location']}.{sta['channel']}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            valid_stations.append(sta)

        # Convert and optionally log10 the PGA
        pgas = []
        for sta in valid_stations:
            pga = sta["pga"]
            if is_live_mode is False:
                pga = np.log10(pga)
            pgas.append(pga)

            # Build the SNCL code
            sncl = ".".join([
                (sta.get("network") or "").strip("."),
                (sta.get("station") or "").strip("."),
                (sta.get("location") or "").strip("."),
                (sta.get("channel") or "").strip(".")
            ])

            line = (f"{sta['latitude']} {sta['longitude']} {sncl} {int(sta['timestamp'])} {round(pga, 3)}"
                    if is_live_mode else
                    f"{sta['latitude']} {sta['longitude']} {round(pga, 3)}")
            data_lines.append(line)

            finder_channels.add_finder_channel(
                latitude=sta["latitude"],
                longitude=sta["longitude"],
                pga=pga,
                sncl=sncl,
                is_artificial=False
            )

        # Add artificial max PGA at the epicenter
        max_obs_pga = np.max(pgas)
        fake_pga = np.max([
            Calculator.predict_PGA_from_magnitude(
                event_mag, event_depth_km, log_scale=(not is_live_mode)),
            max_obs_pga * 1.2
        ])
        sncl = "XX.NONE.00.HNZ"
        line = (f"{event_lat} {event_lon} {sncl} {int(event_time_epoch)} {round(fake_pga, 3)}"
                if is_live_mode else
                f"{event_lat} {event_lon} {round(fake_pga, 3)}")
        data_lines.insert(1, line)

        finder_channels.add_finder_channel(
            latitude=event_lat,
            longitude=event_lon,
            pga=fake_pga,
            sncl=sncl,
            is_artificial=True
        )

        return "\n".join(data_lines).encode("ascii"), finder_channels


###### Service-specific data formatters ######
# Base class for data formatters
class BaseDataFormatter(object):
    def __init__(self, logger=None):
        self.logger = logger or logging.getLogger("pyfinder")

    def set_logger(self, logger=None):
        """Set a logger for the BaseDataFormatter."""
        if logger is None:
            self.logger = logging.getLogger("pyfinder")
        else:
            self.logger = logger
        self.logger.info("Logger set for BaseDataFormatter.")

    def get_logger(self):
        """Get the logger for the BaseDataFormatter."""
        if self.logger is None:
            self.logger = logging.getLogger("pyfinder")
        return self.logger
    
    def log(self, message, level="info"):
        """ Log a message. """
        if self.logger:
            if level == "info":
                self.logger.info(message)
            elif level == "warning":
                self.logger.warning(message)
            elif level == "error":
                self.logger.error(message)
            elif level == "debug":
                self.logger.debug(message)
            else:
                self.logger.info(message)

    @staticmethod
    def _provider_value(provider_model, getter_name, *args):
        """Read one dependency-owned accessor through an explicit boundary."""
        try:
            getter = getattr(provider_model, getter_name)
            return getter(*args)
        except (AttributeError, IndexError, KeyError, TypeError,
                ValueError) as error:
            raise ProviderModelAccessError(
                f"public model accessor {getter_name} failed"
            ) from error

    @classmethod
    def _provider_collection(cls, provider_model, getter_name):
        """Read one list-valued dependency accessor and validate its shape."""
        collection = cls._provider_value(provider_model, getter_name)
        if not isinstance(collection, list):
            raise ProviderModelAccessError(
                f"public model accessor {getter_name} did not return a list"
            )
        return collection
    
    def safe_sort(self, paired_list, key_index=0, reverse=False):
        """
        Sorts a list of tuples/lists safely by key_index.

        Args:
            paired_list (list): List of tuples or lists (e.g., [(key, obj), ...]).
            key_index (int): Index of the element in the tuple to sort by.
            reverse (bool): Whether to sort in descending order.

        Returns:
            list: The values from each tuple/list in the original second position.
        """
        return [item for _, item in sorted(paired_list, key=lambda x: x[key_index], reverse=reverse)]

    def format_data(self, event_data, amplitudes) -> Union[str, FinderChannelList]:
        """ Format the data for the FinDer executable. """
        pass

    @staticmethod
    def extract_raw_stations(event_data, amplitudes):
        """ Method to be used when merging the station data from different services. """
        pass


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


class ESMShakeMapDataFormatter(BaseDataFormatter):
    """ Class for formatting the ESM ShakeMap data for the FinDer executable. """
    SUPPORTED_COMPONENT_SELECTIONS = (
        "maximum-all",
        "maximum-horizontal",
    )

    def __init__(self, logger=None, configuration=None):
        super().__init__(logger=logger)
        self.configuration = (
            pyfinderconfig if configuration is None else configuration)

    def _component_selection(self):
        """Return the configured component policy with its visible fallback."""
        component_selection = self.configuration["general"][
            "component-selection"]
        if component_selection not in self.SUPPORTED_COMPONENT_SELECTIONS:
            self.logger.critical(
                "ESM component selection configuration %r is unsupported; "
                "continuing with maximum-all",
                component_selection,
            )
            return "maximum-all"
        return component_selection

    def _station_identity(self, station):
        """Build the most useful station identity available for diagnostics."""
        network_code = self._provider_value(station, "get_network_code")
        station_code = self._provider_value(station, "get_station_code")
        identity_parts = [
            str(code).lstrip(".")
            for code in (network_code, station_code)
            if code not in (None, "")
        ]
        return ".".join(identity_parts) or "unknown station"

    def _coordinate(self, station, coordinate_name, minimum, maximum,
                    station_identity):
        """Validate one ESM coordinate without changing its meaning."""
        provider_coordinate = self._provider_value(
            station,
            f"get_{coordinate_name}",
        )
        if provider_coordinate is None:
            self.logger.warning(
                f"ESM station {station_identity} rejected: missing "
                f"{coordinate_name}")
            return None

        try:
            coordinate = float(provider_coordinate)
        except (TypeError, ValueError):
            self.logger.warning(
                f"ESM station {station_identity} rejected: nonnumeric "
                f"{coordinate_name} {provider_coordinate!r}")
            return None

        if not math.isfinite(coordinate):
            self.logger.warning(
                f"ESM station {station_identity} rejected: nonfinite "
                f"{coordinate_name} {provider_coordinate!r}")
            return None
        if not minimum <= coordinate <= maximum:
            self.logger.warning(
                f"ESM station {station_identity} rejected: {coordinate_name} "
                f"{coordinate!r} is outside [{minimum}, {maximum}]")
            return None
        return coordinate

    def _valid_acceleration(self, station_identity, component):
        """Return valid provider and normalized ESM accelerations."""
        component_name = self._provider_value(
            component,
            "get_component_name",
        )
        component_identity = (
            str(component_name) if component_name not in (None, "")
            else "unknown component"
        )
        provider_acceleration = self._provider_value(
            component,
            "get_acceleration",
        )
        diagnostic_prefix = (
            f"ESM station {station_identity} component {component_identity} "
            "rejected: "
        )

        if provider_acceleration is None:
            self.logger.warning(diagnostic_prefix + "missing acceleration")
            return None
        try:
            acceleration = float(provider_acceleration)
        except (TypeError, ValueError):
            self.logger.warning(
                diagnostic_prefix
                + f"nonnumeric acceleration {provider_acceleration!r}")
            return None

        if not math.isfinite(acceleration):
            self.logger.warning(
                diagnostic_prefix
                + f"nonfinite acceleration {provider_acceleration!r}")
            return None
        if acceleration == 0:
            self.logger.warning(diagnostic_prefix + "zero acceleration")
            return None
        if acceleration < 0:
            self.logger.warning(
                diagnostic_prefix
                + f"negative acceleration {provider_acceleration!r}")
            return None

        # Convert before station-level maximum selection so an overflow or
        # underflow rejects only this component. A smaller valid sibling must
        # remain available instead of being lost after raw-value selection.
        converted_acceleration = Calculator.percent_g_to_cm_s2(acceleration)
        if not math.isfinite(converted_acceleration):
            self.logger.warning(
                diagnostic_prefix
                + "percent-g conversion produced nonfinite acceleration "
                + f"from provider value {provider_acceleration!r}")
            return None
        if converted_acceleration <= 0:
            self.logger.warning(
                diagnostic_prefix
                + "percent-g conversion produced nonpositive acceleration "
                + f"from provider value {provider_acceleration!r}")
            return None
        return acceleration, converted_acceleration

    def extract_raw_stations(self, event_data,
                             amplitudes) -> List[RawStationMeasurement]:
        """
        Extract and normalize valid ESM station data for merging.

        Component failures are isolated so a bad component cannot discard a
        valid sibling or stop later stations from being processed.
        Used for merging the station data from different services.
        """
        raw_stations = []
        time_epoch = get_epoch_time(event_data.get_origin_time())
        component_selection = self._component_selection()

        stations = self._provider_collection(amplitudes, "get_stations")
        
        for station in stations:
            station_identity = self._station_identity(station)
            latitude = self._coordinate(
                station, "latitude", -90, 90, station_identity)
            longitude = self._coordinate(
                station, "longitude", -180, 180, station_identity)
            if latitude is None or longitude is None:
                continue

            components = self._provider_collection(station, "get_components")
            eligible_components = []
            for component in components:
                component_name = self._provider_value(
                    component,
                    "get_component_name",
                )
                if (component_selection == "maximum-horizontal"
                        and str(component_name).endswith("Z")):
                    continue
                eligible_components.append(component)

            selected_provider_pga = None
            selected_converted_pga = None
            selected_channel = None

            for channel in eligible_components:
                valid_acceleration = self._valid_acceleration(
                    station_identity, channel)
                if valid_acceleration is None:
                    continue

                provider_pga, converted_pga = valid_acceleration
                if (selected_converted_pga is None
                        or converted_pga > selected_converted_pga):
                    selected_provider_pga = provider_pga
                    selected_converted_pga = converted_pga
                    selected_channel = channel

            if selected_channel is None:
                if not components:
                    reason = "component collection is empty"
                elif not eligible_components:
                    reason = (
                        "no component is eligible under "
                        f"{component_selection}")
                else:
                    reason = "all eligible components are invalid"
                self.logger.warning(
                    f"ESM station {station_identity} rejected: no eligible "
                    f"valid component remains ({reason})")
                continue

            # Strip codes
            network_code = (
                self._provider_value(station, "get_network_code") or ""
            ).lstrip(".")
            station_code = (
                self._provider_value(station, "get_station_code") or ""
            ).lstrip(".")
            channel_code = self._provider_value(
                selected_channel,
                "get_component_name",
            ).lstrip(".")

            location_code = ""
            if "." in channel_code:
                location_code, channel_code = channel_code.split(".", 1)

            raw_stations.append(RawStationMeasurement(
                latitude=latitude,
                longitude=longitude,
                network=network_code,
                station=station_code,
                location=location_code,
                channel=channel_code,
                pga=selected_converted_pga,
                timestamp=time_epoch,
                source="ESM",
                provider_value=selected_provider_pga,
                provider_unit="%g",
            ))

        return raw_stations


    def format_data(self, event_data: ShakeMapEventData, 
                    amplitudes: ShakeMapStationAmplitudes) -> Union[str, FinderChannelList]:
        """ Format the data for the FinDer executable. """
        self.log(message=f"Formatting the ESM ShakeMap: {type(amplitudes)}.......")
        is_live_mode = pyfinderconfig["finder-executable"]["finder-live-mode"]

        time_epoch = int(get_epoch_time(event_data.get_origin_time()))

        # Print the event information
        self.log(message=f"Event ID: {event_data.get_event_id()}")
        self.log(message=f"|- Time: {event_data.get_origin_time()}")
        self.log(message=f"|- Latitude: {event_data.get_latitude()}")
        self.log(message=f"|- Longitude: {event_data.get_longitude()}")
        self.log(message=f"|- Depth: {event_data.get_depth()}")
        self.log(message=f"|- Magnitude: {event_data.get_magnitude()}")

        # Collect the station, channel and PGA information
        stations = amplitudes.get_stations()
        self.log(message=f"There are {len(stations)} stations. Looking for the maximum PGA for each.")
        
        selected_channels = []
        pga_strings = []
        
        # Create a FinderChannelList object to store the channel data
        finder_channels = FinderChannelList()

        for station in stations:
            # Find the component with the maximum PGA
            pga = -np.inf
            selected_channel = None

            for channel in station.get_components():
                if channel.get_acceleration() > pga:
                    pga = channel.get_acceleration()
                    selected_channel = channel

            if selected_channel is not None:
                selected_channels.append(selected_channel)

                latitude = station.get_latitude()
                longitude = station.get_longitude()
                network_code = station.get_network_code()
                station_code = station.get_station_code()
                channel_code = selected_channel.get_component_name()

                # Remove any leading dots from all codes
                network_code = network_code.lstrip(".")
                station_code = station_code.lstrip(".")
                channel_code = channel_code.lstrip(".")
                location_code = ""    

                if len(channel_code.split(".")) > 1:
                    location_code, channel_code = channel_code.split(".")

                # Create the SNCL code
                sncl = f"{network_code}.{station_code}.{location_code}.{channel_code}"

                # Convert the percent PGA to cm/s/s
                pga = Calculator.percent_g_to_cm_s2(pga)

                if is_live_mode == False:
                    pga = np.log10(pga)

                if is_live_mode:
                    pga_strings.append(f"{latitude} {longitude} {sncl} {time_epoch} {round(pga, 3)}")

                    self.log(message=f"{sncl} PGA: {round(pga, 3)} m/s/s at " + \
                             f" Latitude: {latitude}, Longitude: {longitude}")
                    
                else:
                    pga_strings.append(f"{latitude} {longitude} {round(pga, 3)}")

                    self.log(message=f"{sncl} logPGA: {round(pga, 3)} m/s/s at " + \
                             f" Latitude: {latitude}, Longitude: {longitude}")
                    
                finder_channels.add_finder_channel(latitude=latitude, longitude=longitude,
                                                   pga=pga, sncl=sncl, is_artificial=False)

        # Create an artificial maximum PGA at the epicenter to make FinDer 
        # stick to the actual location. 
        fake_latitude = event_data.get_latitude()
        fake_longitude = event_data.get_longitude()
        fake_station = f"XX.NONE.00.HNZ"

        # Magnitude-dependent artificial PGA 
        magnitude = event_data.get_magnitude()
        depth = event_data.get_depth() or 10

        # Find the maximum observed PGA
        max_oberserved_pga = np.max([channel.get_acceleration() for channel in selected_channels])
        self.log(message=f"Maximum observed PGA: {round(max_oberserved_pga, 3)} cm/s/s at the stations.")
        fake_max_pga = np.max([Calculator.predict_PGA_from_magnitude(
                magnitude=magnitude, event_depth=depth, log_scale=(is_live_mode == False)), 
                max_oberserved_pga * 1.2]) 
        self.log(message=f"Use log10(PGA) in the FinDer input: {is_live_mode == False}")
        self.log(message=f"Artificial maximum PGA: {round(fake_max_pga, 3)} cm/s/s at the epicenter.")
        
        # List to store merged the coordinates and PGAs 
        data = []
        
        # Origin time epoch goes first as the header. The header is
        # timestamp and time step increment, which is zero in our case.
        data.append(f"# {time_epoch} 0")

        # Append the epicenter
        if is_live_mode:
            data.append(f"{fake_latitude} {fake_longitude} {fake_station} {time_epoch} {round(fake_max_pga, 3)}")
        else:
            data.append(f"{fake_latitude} {fake_longitude} {np.round(fake_max_pga, 3)}")

        finder_channels.add_finder_channel(latitude=fake_latitude, longitude=fake_longitude,
                                           pga=fake_max_pga, sncl=fake_station, is_artificial=True)

        # Append the stations
        for pga_string in pga_strings:
            data.append(pga_string)

        return "\n".join(data).encode("ascii"), finder_channels
    

        
class RRSMPeakMotionDataFormatter(BaseDataFormatter):
    """ Class for formatting the RRSM peak motion data for the FinDer executable. """
    SUPPORTED_COMPONENT_SELECTIONS = (
        "maximum-all",
        "maximum-horizontal",
    )

    def __init__(self, logger=None, configuration=None):
        super().__init__(logger=logger)
        self.configuration = (
            pyfinderconfig if configuration is None else configuration)

    def _component_selection(self):
        """Return the configured RRSM component policy with a visible fallback."""
        component_selection = self.configuration["general"][
            "component-selection"]
        if component_selection not in self.SUPPORTED_COMPONENT_SELECTIONS:
            self.logger.critical(
                "RRSM component selection configuration %r is unsupported; "
                "continuing with maximum-all",
                component_selection,
            )
            return "maximum-all"
        return component_selection

    @staticmethod
    def _provider_code(provider_code):
        """Safely retain a provider identity code and clean leading separators."""
        if provider_code is None:
            return ""
        return str(provider_code).lstrip(".")

    def _station_identity(self, station):
        """Build the most useful safely converted RRSM station identity."""
        identity_parts = [
            self._provider_code(provider_code)
            for provider_code in (
                self._provider_value(station, "get_network_code"),
                self._provider_value(station, "get_station_code"),
                self._provider_value(station, "get_location_code"),
            )
        ]
        return ".".join(
            part for part in identity_parts if part) or "unknown station"

    def _coordinate(self, station, coordinate_name, minimum, maximum,
                    station_identity):
        """Validate one RRSM coordinate without rewriting its convention."""
        provider_coordinate = self._provider_value(
            station,
            f"get_{coordinate_name}",
        )
        if provider_coordinate is None:
            self.logger.warning(
                f"RRSM station {station_identity} rejected: missing "
                f"{coordinate_name}")
            return None

        try:
            coordinate = float(provider_coordinate)
        except (TypeError, ValueError):
            self.logger.warning(
                f"RRSM station {station_identity} rejected: nonnumeric "
                f"{coordinate_name} {provider_coordinate!r}")
            return None

        if not math.isfinite(coordinate):
            self.logger.warning(
                f"RRSM station {station_identity} rejected: nonfinite "
                f"{coordinate_name} {provider_coordinate!r}")
            return None
        if not minimum <= coordinate <= maximum:
            self.logger.warning(
                f"RRSM station {station_identity} rejected: "
                f"{coordinate_name} {coordinate!r} is outside "
                f"[{minimum}, {maximum}]")
            return None
        return coordinate

    def _valid_pga(self, station_identity, component_identity, channel):
        """Return one exact valid RRSM provider PGA, otherwise warn and omit it."""
        provider_pga = self._provider_value(channel, "get_channel_pga")
        diagnostic_prefix = (
            f"RRSM station {station_identity} component "
            f"{component_identity or 'unknown component'} rejected: "
        )

        if provider_pga is None:
            self.logger.warning(diagnostic_prefix + "missing PGA value None")
            return None
        if (isinstance(provider_pga, bool)
                or not isinstance(provider_pga, (int, float))):
            self.logger.warning(
                diagnostic_prefix + f"nonnumeric PGA {provider_pga!r}")
            return None
        if isinstance(provider_pga, float) and not math.isfinite(provider_pga):
            self.logger.warning(
                diagnostic_prefix + f"nonfinite PGA {provider_pga!r}")
            return None
        if provider_pga == 0:
            self.logger.warning(
                diagnostic_prefix + f"zero PGA {provider_pga!r}")
            return None
        if provider_pga < 0:
            self.logger.warning(
                diagnostic_prefix + f"negative PGA {provider_pga!r}")
            return None
        if provider_pga < RRSM_PEAKMOTION_PGA_MIN:
            self.logger.warning(
                diagnostic_prefix + f"PGA {provider_pga!r} is below "
                f"minimum {RRSM_PEAKMOTION_PGA_MIN} cm/s^2")
            return None
        if provider_pga > RRSM_PEAKMOTION_PGA_MAX:
            self.logger.warning(
                diagnostic_prefix + f"PGA {provider_pga!r} is above "
                f"maximum {RRSM_PEAKMOTION_PGA_MAX} cm/s^2")
            return None
        return provider_pga

    def extract_raw_stations(self, event_data,
                             amplitudes) -> List[RawStationMeasurement]:
        """
        Extract and normalize valid RRSM station data for merging.

        Station and component failures are isolated so malformed provider
        values cannot discard valid siblings or stop later stations.
        Used for merging the station data from different services.
        """
        # PeakMotionData owns the station hierarchy, while event_data remains
        # the authoritative context selected for this complete attempt.
        peak_motions = amplitudes

        raw_stations = []
        component_selection = self._component_selection()

        for station_data in self._provider_collection(
                peak_motions, "get_stations"):
            station_identity = self._station_identity(station_data)
            latitude = self._coordinate(
                station_data, "latitude", -90, 90, station_identity)
            longitude = self._coordinate(
                station_data, "longitude", -180, 180, station_identity)
            if latitude is None or longitude is None:
                continue

            channels = self._provider_collection(
                station_data,
                "get_channels",
            )
            eligible_channels = []
            for channel in channels:
                channel_code = self._provider_code(
                    self._provider_value(channel, "get_channel_code"))
                if (component_selection == "maximum-horizontal"
                        and channel_code.endswith("Z")):
                    continue
                eligible_channels.append((channel, channel_code))

            best_pga = None
            best_channel = None
            best_channel_code = None

            for channel, channel_code in eligible_channels:
                pga_val = self._valid_pga(
                    station_identity, channel_code, channel)
                if pga_val is None:
                    continue
                if best_pga is None or pga_val > best_pga:
                    best_pga = pga_val
                    best_channel = channel
                    best_channel_code = channel_code

            if best_channel is None:
                if not channels:
                    reason = "channel collection is empty"
                elif not eligible_channels:
                    reason = (
                        "no channel is eligible under "
                        f"{component_selection}")
                else:
                    reason = "all eligible channels are invalid"
                self.logger.warning(
                    f"RRSM station {station_identity} rejected: no eligible "
                    f"valid component remains ({reason})")
                continue

            network_code = self._provider_code(
                self._provider_value(station_data, "get_network_code"))
            station_code = self._provider_code(
                self._provider_value(station_data, "get_station_code"))
            location_code = self._provider_code(
                self._provider_value(station_data, "get_location_code"))
            timestamp = get_epoch_time(event_data.get_origin_time())

            raw_stations.append(RawStationMeasurement(
                latitude=latitude,
                longitude=longitude,
                network=network_code,
                station=station_code,
                location=location_code,
                channel=best_channel_code,
                pga=best_pga,  # RRSM Peak Motion is already linear cm/s^2.
                timestamp=timestamp,
                source="RRSM",
                provider_value=best_pga,
                provider_unit="cm/s^2",
            ))

        return raw_stations

    def format_data(self, event_data, amplitudes) -> Union[str, FinderChannelList]:
        """ Format the data for the FinDer executable. """
        self.log(message="Formatting the RRSM PeakMotionData.......")
        
        station_codes = event_data.get_station_codes()

        # Swap variables
        peak_motions = amplitudes
        # RRSM peak motion data contains both event and amplitude data.
        # Check if the peak motion data is passed to the formatter.
        if isinstance(peak_motions, PeakMotionData):
            event_data = peak_motions.get_event_data()
        
        is_live_mode = pyfinderconfig["finder-executable"]["finder-live-mode"]
        
        # Print the event information
        self.log(message=f"Event ID: {event_data.get_event_id()}")
        self.log(message=f"|- Time: {event_data.get_origin_time()}")
        self.log(message=f"|- Latitude: {event_data.get_latitude()}")
        self.log(message=f"|- Longitude: {event_data.get_longitude()}")
        self.log(message=f"|- Depth: {event_data.get_depth()}")
        self.log(message=f"|- Magnitude: {event_data.get_magnitude()}, {event_data.get_magnitude_type()}")

        # Collect the station, channel and PGA information
        self.log(message=f"There are {len(station_codes)} stations. Looking for the maximum PGA for each.")
        all_stations = []
        all_pga = []

        # Create a FinderChannelList object to store the channel data passed to the FinDer
        finder_channels = FinderChannelList()

        for station_code in station_codes:
            station_data = peak_motions.get_station(station_code=station_code)
            all_stations.append(station_data)

            # Find the component with the maximum PGA
            pga = -np.inf
            selected_channel = None

            for channel in station_data.get_channels():
                if channel.get_channel_pga() > pga:
                    pga = channel.get_channel_pga()
                    selected_channel = channel
            all_pga.append(pga)

        # Sort the stations by the maximum PGA just for logging in order
        self.log(message=f"Sorting the stations by the maximum PGA.")
        try:
            # Sort the stations by the maximum PGA, by taking care of the
            # same-value problem. The sort is done by the first element of 
            # the tuple only.
            sorted_stations = self.safe_sort(
                zip(all_pga, all_stations), key_index=0, reverse=True)

        except Exception as e:
            self.log(message=f"Error sorting the stations: {e}", level="error")

            # Continue without sorting
            sorted_stations = all_stations
            
        self.log(message=f"Sorting the stations by the maximum PGA done.")

        # Valid stations have PGAs within the range. Invalid stations are either
        # missing the PGA value or the value is not in the range.
        valid_stations = []
        valid_pgas = []
        valid_channels = []
        invalid_stations = []

        for station_data in sorted_stations:
            latitude = station_data.get_latitude()
            longitude = station_data.get_longitude()
            network_code = station_data.get_network_code()
            station_code = station_data.get_station_code()
            distance = station_data.get_epicentral_distance()

            # Find the component with the maximum PGA
            pga = -np.inf
            selected_channel = None

            for channel in station_data.get_channels():
                # A PGA should be within the range to be considered.
                channel_pga = np.abs(channel.get_channel_pga())
                
                if channel_pga > pga and \
                    channel_pga >= RRSM_PEAKMOTION_PGA_MIN \
                        and channel_pga <= RRSM_PEAKMOTION_PGA_MAX:

                    pga = channel_pga
                    selected_channel = channel

            if selected_channel is None:
                # No valid PGA found for this station. Either PGAs for all componentds are 
                # not in the range, or value is missing.
                invalid_stations.append(station_data)
                self.log(message=f"Discarding station {station_code}. No (valid) PGA found.", level="warning")
                
            elif pga <= RRSM_PEAKMOTION_PGA_MIN or pga >= RRSM_PEAKMOTION_PGA_MAX:
                # The maximum PGA for this station is not in the range.
                invalid_stations.append(station_data)
                self.log(message=f"Discarding station {network_code}.{station_code}. PGA ({pga}) not in the range.",
                         level="warning")
            
            else:
                # A valid PGA found for this station.
                valid_stations.append(station_data)

                # Log10 transform the PGA if NOT in the live mode. Otherwise, keep it in cm/s/s as it is.
                if is_live_mode == False:
                    if pga <= 0:
                        self.log(message=f"Discarding station {network_code}.{station_code}. "
                                     f"PGA ({pga}) is not in the range.", level="warning")
                        continue
                    pga = np.log10(pga)
                valid_pgas.append(pga)

                # Remove any leading dots from all codes
                network_code = network_code.lstrip(".")
                station_code = station_code.lstrip(".")
                channel_code = selected_channel.get_channel_code().lstrip(".")

                # Check if the channel code has a location code
                location_code = ""
                if len(channel_code.split(".")) > 1:
                    location_code, channel_code = channel_code.split(".")
            
                sncl = f"{network_code}.{station_code}.{location_code}.{channel_code}"
                valid_channels.append(sncl)

                finder_channels.add_finder_channel(latitude=latitude, longitude=longitude,
                                                   pga=pga, sncl=sncl, is_artificial=False)

                if is_live_mode:
                    self.log(message=f"{sncl}, PGA: {round(pga, 3)} cm/s/s at {round(distance, 2)} km,"
                             f" Latitude: {latitude}, Longitude: {longitude}")
                else:
                    self.log(message=f"{sncl}, log10(PGA): {round(pga, 3)} cm/s/s at {round(distance, 2)} km,"
                             f" Latitude: {latitude}, Longitude: {longitude}")
            
        # A small summary
        self.log(message=f"Total number of stations: {len(sorted_stations)}")
        self.log(message=f"Number of valid stations: {len(valid_stations)} out of {len(sorted_stations)}")
        self.log(message=f"Number of invalid stations: {len(invalid_stations)} out of {len(sorted_stations)}")

        # We insert a fake maximum PGA at the epicenter to make FinDer 
        # stick to the actual location. This fake PGA is 1% more than the
        # maximum PGA of the stations. 
        fake_latitude = event_data.get_latitude()
        fake_longitude = event_data.get_longitude()
        fake_station = f"XX.NONE.00.HNZ"
        
        # Magnitude-dependent artificial PGA 
        magnitude = event_data.get_magnitude()
        depth = event_data.get_depth() or 10

        # Find the maximum observed PGA
        max_oberserved_pga = np.max(valid_pgas)
        self.log(message=f"Maximum observed PGA: {round(max_oberserved_pga, 3)} cm/s/s at the stations.")
        fake_max_pga = np.max([Calculator.predict_PGA_from_magnitude(
                magnitude=magnitude, event_depth=depth, log_scale=(is_live_mode == False)), 
                max_oberserved_pga * 1.2]) 
        self.log(message=f"Use log10(PGA) in the FinDer input: {is_live_mode == False}")
        self.log(message=f"Artificial maximum PGA: {round(fake_max_pga, 3)} cm/s/s at the epicenter.")
        
        # Merge the coordinates and PGAs into a string
        data = []

        # Origin time epoch goes first as the header. The header is 
        # timestamp and time step increment, which is zero in our case.
        time_epoch = int(get_epoch_time(event_data.get_origin_time()))
        data.append(f"# {time_epoch} 0")

        # Append the epicenter
        if is_live_mode:
            data.append(f"{fake_latitude} {fake_longitude} {fake_station} {time_epoch} {fake_max_pga}")
        else:
            data.append(f"{fake_latitude} {fake_longitude} {np.round(fake_max_pga, 3)}")

        finder_channels.add_finder_channel(latitude=fake_latitude, longitude=fake_longitude,
                                           pga=fake_max_pga, sncl=fake_station, is_artificial=True)

        
        # Sort all arrays using safe_sort
        try:
            valid_stations = self.safe_sort(
                zip(valid_pgas, valid_stations), key_index=0, reverse=True)
            valid_channels = self.safe_sort(
                zip(valid_pgas, valid_channels), key_index=0, reverse=True)
            valid_pgas = sorted(valid_pgas, reverse=True)

        except Exception as e:
            self.log(message=f"Error sorting the stations: {e}", level="error")

            # Continue without sorting
            pass

        # Append the stations
        for station_data, pga, sncl_data in zip(valid_stations, valid_pgas, valid_channels):
            latitude = station_data.get_latitude()
            longitude = station_data.get_longitude()
            
            if is_live_mode:
                data.append(f"{latitude}  {longitude}  {sncl_data}  {time_epoch}  {np.round(pga, 3)}")
            else:
                data.append(f"{latitude} {longitude} {np.round(pga, 3)}")
            
        self.log(message=f"Formatted {len(data)} lines of data for FinDer. Returning to the caller.")
        
        # Return the formatted data
        return "\n".join(data).encode("ascii"), finder_channels
