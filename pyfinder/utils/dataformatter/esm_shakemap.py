# -*- coding: utf-8 -*-
"""Normalization of ESM ShakeMap observations."""

import math
from typing import List

from ...pyfinderconfig import pyfinderconfig
from ..calculator import Calculator
from ..station_merger import RawStationMeasurement
from ..timeutils import get_epoch_time
from .base import BaseDataFormatter

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

