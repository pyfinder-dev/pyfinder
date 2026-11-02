"""Normalize USGS ShakeMap station PGA using each channel's declared units."""

import math

from ...pyfinderconfig import USGS_SHAKEMAP_SERVICE, pyfinderconfig
from ..calculator import Calculator
from ..station_merger import RawStationMeasurement
from ..timeutils import get_epoch_time
from .base import BaseDataFormatter


class USGSShakeMapDataFormatter(BaseDataFormatter):
    """Return instrumental observations without treating modeled MMI as PGA."""

    def __init__(self, logger=None, configuration=None):
        super().__init__(logger=logger)
        self.configuration = (
            pyfinderconfig if configuration is None else configuration)

    def _number(self, value, identity, label, minimum=None, maximum=None):
        """Validate a numerical provider field without repairing its sign."""
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            number = math.nan

        if (isinstance(value, bool) or not math.isfinite(number)
                or (minimum is not None and number < minimum)
                or (maximum is not None and number > maximum)):
            self.logger.warning(
                "USGS ShakeMap %s rejected: invalid %s %r",
                identity, label, value,
            )
            return None
        return number

    def _acceleration(self, component, identity):
        """Convert only explicitly declared acceleration units to cm/s²."""
        # Provider flags and uncertainty are not new rejection thresholds.
        # As with the existing instrumental adapters, this validates the
        # supplied numerical PGA; it does not claim provider quality approval.
        provider_value = self._provider_value(component, "get_acceleration")
        value = self._number(provider_value, identity, "PGA", minimum=0)
        if value is None:
            return None
        if value == 0:
            self.logger.warning("USGS ShakeMap %s rejected: zero PGA", identity)
            return None

        unit = self._provider_value(component, "get_acceleration_units")

        # A percent of gravity is 0.01 * 980.665 cm/s². Unlike ESM, USGS
        # supplies the unit on each amplitude, so an absent unit cannot be
        # assumed to mean percent-g. Maximum selection happens after conversion.
        if unit == "%g":
            converted = Calculator.percent_g_to_cm_s2(value)
        elif unit == "g":
            converted = Calculator.convert_pga_to_cm_s2(value)
        elif unit in ("cm/s/s", "cm/s^2", "cm/s²", "gal"):
            converted = value
        elif unit in ("m/s/s", "m/s^2", "m/s²"):
            converted = value * 100
        else:
            self.logger.warning(
                "USGS ShakeMap %s rejected: missing or unsupported PGA unit %r",
                identity, unit,
            )
            return None

        if not math.isfinite(converted) or converted <= 0:
            self.logger.warning(
                "USGS ShakeMap %s rejected: invalid converted PGA %r",
                identity, converted,
            )
            return None
        return converted, provider_value, unit

    def extract_raw_stations(self, event_data, amplitudes):
        """Keep one valid maximum component per station in provider order."""
        selection = self.configuration["general"]["component-selection"]
        if selection not in ("maximum-all", "maximum-horizontal"):
            self.logger.critical(
                "USGS ShakeMap component selection %r is unsupported; "
                "continuing with maximum-all", selection,
            )
            selection = "maximum-all"

        timestamp = get_epoch_time(event_data.get_origin_time())
        records = []
        for station in self._provider_collection(amplitudes, "get_stations"):
            identity = self._provider_value(station, "get_station_id")

            # Macroseismic stations can contain derived ground motions. They
            # are not instrument readings; DYFI has its own explicit adapter.
            if self._provider_value(station, "get_station_type") == "macroseismic":
                self.logger.info(
                    "USGS ShakeMap station %r omitted: macroseismic record",
                    identity,
                )
                continue
            if self._provider_value(station, "get", "predicted") is True:
                self.logger.warning(
                    "USGS ShakeMap station %r omitted: predicted observation",
                    identity,
                )
                continue

            latitude = self._number(
                self._provider_value(station, "get_latitude"), identity,
                "latitude", -90, 90,
            )
            longitude = self._number(
                self._provider_value(station, "get_longitude"), identity,
                "longitude", -180, 180,
            )
            if latitude is None or longitude is None:
                continue

            selected = None
            for component in self._provider_collection(station, "get_components"):
                name = self._provider_value(component, "get_component_name")
                if not isinstance(name, str) or not name:
                    self.logger.warning(
                        "USGS ShakeMap station %r component rejected: "
                        "missing component name", identity,
                    )
                    continue
                if selection == "maximum-horizontal" and name.endswith("Z"):
                    continue

                acceleration = self._acceleration(
                    component, f"station {identity!r} component {name!r}")
                if acceleration is not None:
                    if selected is None or acceleration[0] > selected[0]:
                        selected = (*acceleration, name)

            if selected is None:
                self.logger.warning(
                    "USGS ShakeMap station %r rejected: no eligible valid PGA",
                    identity,
                )
                continue

            pga, provider_value, provider_unit, channel = selected
            channel = channel.lstrip(".")
            location = ""
            if "." in channel:
                location, channel = channel.split(".", 1)

            network = self._provider_value(station, "get_network_code") or ""
            station_code = self._provider_value(station, "get_station_code") or ""
            records.append(RawStationMeasurement(
                latitude=latitude,
                longitude=longitude,
                network=network.lstrip("."),
                station=station_code.lstrip("."),
                location=location,
                channel=channel,
                pga=pga,
                timestamp=timestamp,
                source=USGS_SHAKEMAP_SERVICE,
                provider_value=provider_value,
                provider_unit=provider_unit,
            ))

        if not records:
            self.logger.error(
                "USGS ShakeMap normalization produced zero usable records")
        return records
