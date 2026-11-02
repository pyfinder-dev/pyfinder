"""Normalize USGS DYFI community intensity while preserving grid-cell geometry."""

from copy import deepcopy
import math
import numbers

import numpy as np
from shapely.geometry import Polygon

from ...pyfinderconfig import USGS_DYFI_SERVICE, pyfinderconfig
from ..calculator import Calculator
from ..station_merger import RawStationMeasurement
from ..timeutils import get_epoch_time
from .base import BaseDataFormatter
from .emsc_felt_report import _felt_report_station_code


def _finite_number(value):
    """Accept finite real provider numbers, never booleans or textual numbers."""
    return (
        isinstance(value, numbers.Real)
        and not isinstance(value, (bool, np.bool_))
        and math.isfinite(value)
    )


def _position(value):
    """Read GeoJSON longitude first; do not repair invalid coordinates."""
    if (not isinstance(value, (list, tuple)) or len(value) < 2
            or not all(_finite_number(item) for item in value[:2])):
        raise ValueError("position must contain finite numerical longitude/latitude")
    longitude, latitude = value[:2]
    if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
        raise ValueError("position lies outside longitude/latitude bounds")
    return float(longitude), float(latitude)


def _representative_position(geometry):
    """Return a supplied point or the planar area centroid of a small DYFI cell.

    The 1 km product describes areas, not individual respondent locations.
    For a Polygon, Shapely's area centroid in longitude/latitude is the chosen
    representative of that small cell. It is not a geodesic-area calculation.
    Longitudes are unwrapped around the first vertex before taking the centroid
    so a cell crossing the antimeridian is not moved to the Greenwich meridian.
    """
    if not isinstance(geometry, dict):
        raise ValueError("missing GeoJSON geometry")
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if geometry_type == "Point":
        longitude, latitude = _position(coordinates)
        return longitude, latitude, "provider_point"
    if geometry_type != "Polygon":
        raise ValueError(f"unsupported DYFI geometry {geometry_type!r}")
    if not isinstance(coordinates, (list, tuple)) or not coordinates:
        raise ValueError("Polygon has no rings")

    rings = []
    anchor = None
    for ring in coordinates:
        if not isinstance(ring, (list, tuple)) or len(ring) < 4:
            raise ValueError("Polygon ring needs at least four positions")
        positions = [_position(value) for value in ring]
        if positions[0] != positions[-1]:
            raise ValueError("Polygon ring is not explicitly closed")
        if anchor is None:
            anchor = positions[0][0]
        rings.append([
            (anchor + (longitude - anchor + 180) % 360 - 180, latitude)
            for longitude, latitude in positions
        ])

    longitudes = [longitude for ring in rings for longitude, _ in ring]
    if max(longitudes) - min(longitudes) >= 180:
        raise ValueError("Polygon longitude extent is ambiguous for a DYFI cell")
    polygon = Polygon(rings[0], holes=rings[1:])
    if not polygon.is_valid or polygon.is_empty or polygon.area <= 0:
        raise ValueError("Polygon is empty, degenerate, or topologically invalid")

    centroid = polygon.centroid
    longitude = (centroid.x + 180) % 360 - 180
    return longitude, centroid.y, "cell_centroid"


class USGSDYFIDataFormatter(BaseDataFormatter):
    """Apply the existing felt-intensity policy to USGS CDI/MMI observations."""

    def __init__(self, logger=None, configuration=None):
        super().__init__(logger=logger)
        self.configuration = (
            pyfinderconfig if configuration is None else configuration)

    def extract_raw_stations(self, event_data, felt_reports):
        """Keep rows independently, including their original area provenance."""
        component = self.configuration["finder-executable"][
            "felt-report-component-code"]
        if (not isinstance(component, str) or not component
                or not component.isascii() or not component.isalnum()
                or component != component.upper()):
            raise ValueError(
                "finder-executable.felt-report-component-code must be a "
                "nonempty ASCII uppercase alphanumeric SNCL component")

        # Use the common authoritative event context, not USGS's independently
        # revised event location or the distance supplied in a DYFI row.
        event_latitude = event_data.get_latitude()
        event_longitude = event_data.get_longitude()
        magnitude = event_data.get_magnitude()
        depth = event_data.get_depth()
        if (not all(_finite_number(value) for value in (
                event_latitude, event_longitude, magnitude, depth))
                or not -90 <= event_latitude <= 90
                or not -180 <= event_longitude <= 180 or depth < 0):
            self.logger.error("USGS DYFI normalization has invalid event context")
            return []

        timestamp = get_epoch_time(event_data.get_origin_time())
        records = []
        for row_index, row in enumerate(
                self._provider_collection(felt_reports, "get_intensities")):
            intensity = self._provider_value(row, "get", "cdi")
            if (not _finite_number(intensity) or intensity < 1
                    or np.round(intensity) > 10):
                self.logger.warning(
                    "USGS DYFI row %s rejected: invalid CDI/MMI %r",
                    row_index, intensity,
                )
                continue

            geometry = self._provider_value(row, "get", "geometry")
            try:
                longitude, latitude, method = _representative_position(geometry)
            except ValueError as error:
                self.logger.warning(
                    "USGS DYFI row %s rejected: %s", row_index, error)
                continue

            distance = Calculator.haversine(
                event_latitude, event_longitude, latitude, longitude)
            predicted = Calculator.I_Allen2012_Rhypo(magnitude, depth, distance)
            if not predicted > 3 or abs(intensity - predicted) > 3:
                self.logger.warning(
                    "USGS DYFI row %s rejected by existing Allen felt policy: "
                    "CDI/MMI %r, predicted intensity %r",
                    row_index, intensity, predicted,
                )
                continue

            # Worden returns log10(PGA in cm/s²). Exponentiate exactly once;
            # CDI/MMI is an intensity, so percent-g conversion is inappropriate.
            log_pga = float(Calculator.I_to_PGA_Wordon2012(intensity))
            try:
                pga = 10 ** log_pga
            except OverflowError:
                pga = math.inf
            if not math.isfinite(pga) or pga <= 0:
                self.logger.warning(
                    "USGS DYFI row %s rejected: invalid converted PGA %r",
                    row_index, pga,
                )
                continue

            records.append(RawStationMeasurement(
                latitude=latitude,
                longitude=longitude,
                network="DY",
                station=_felt_report_station_code(len(records)),
                location="00",
                channel=component,
                pga=pga,
                timestamp=timestamp,
                source=USGS_DYFI_SERVICE,
                provider_value=intensity,
                provider_unit="MMI",
                provider_geometry=deepcopy(geometry),
                coordinate_method=method,
                provider_observation_id=self._provider_value(row, "get", "id"),
            ))

        if not records:
            self.logger.error("USGS DYFI normalization produced zero usable records")
        return records
