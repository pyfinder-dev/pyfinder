# -*- coding: utf-8 -*-
"""Normalization of RRSM peak-motion observations."""

import math
from typing import List

from ...pyfinderconfig import pyfinderconfig
from ..station_merger import RawStationMeasurement
from ..timeutils import get_epoch_time
from .base import BaseDataFormatter

# Thresholds for the RRSM peak motion data that are used to filter out
# the stations with PGA/PGV values that are not in the range.
RRSM_PEAKMOTION_PGA_MIN = 0.00001
RRSM_PEAKMOTION_PGA_MAX = 10*980.6 # cm/s/s
RRSM_PEAKMOTION_PGV_MIN = 0.000001
RRSM_PEAKMOTION_PGV_MAX = 1.0 # m/s
RRSM_PEAKMOTION_PGV_BROADBAND_MIN = 0.000001
RRSM_PEAKMOTION_PGV_BROADBAND_MAX = 0.013 # m/s


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
