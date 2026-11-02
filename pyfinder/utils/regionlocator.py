"""Locate countries for ShakeMap without coupling them to FinDer regions."""

from functools import lru_cache
from importlib import resources
import logging
import math
import re


@lru_cache(maxsize=1)
def _country_records():
    """Read the packaged shapefile family once and retain only in-memory data."""
    import geopandas

    directory = resources.files("pyfinder").joinpath(
        "extern", "ne_110m_admin_0_countries"
    )
    try:
        # Keep the whole directory materialized while GDAL reads its companion
        # files. No cached path may outlive a temporary package extraction.
        with resources.as_file(directory) as path:
            countries = geopandas.read_file(path / "ne_110m_admin_0_countries.shp")

        if countries.crs is None or countries.crs.to_epsg() != 4326:
            raise ValueError("country data must use EPSG:4326")

        if not {"ISO_A2", "ISO_A2_EH", "geometry"}.issubset(countries.columns):
            raise ValueError("country identifier or geometry fields are missing")

        records = []
        for row in countries.itertuples(index=False):
            geometry = row.geometry
            if (
                geometry is None
                or geometry.is_empty
                or not geometry.is_valid
                or geometry.geom_type not in {"Polygon", "MultiPolygon"}
            ):
                raise ValueError("country data contains an unusable polygon")

            code = next(
                (
                    value.lower()
                    for value in (row.ISO_A2, row.ISO_A2_EH)
                    if isinstance(value, str)
                    and re.fullmatch(r"[A-Za-z]{2}", value)
                ),
                None,
            )
            # Some disputed territories have no two-letter identifier in either
            # field. Keep their polygons, so those locations honestly use global.
            records.append((code, geometry))

        if not records:
            raise ValueError("country data contains no polygons")

        return tuple(records)
    except Exception as error:
        raise ValueError(
            "Cannot load packaged ShakeMap country boundaries: "
            f"{error}; verify the installed country shapefile and its companion files"
        ) from error


class RegionLocator:
    """Use unambiguous country coverage; offshore and shared borders use global."""

    def __init__(self, *, logger=None):
        self.logger = logger or logging.getLogger(__name__)

    def get_region(self, lat, lon):
        from shapely.geometry import Point

        # Reject unusable coordinates before loading the packaged data. A bad
        # solution must not be reported as an ordinary lack of regional coverage.
        coordinates = []
        for value, label, limit in ((lat, "latitude", 90), (lon, "longitude", 180)):
            try:
                if isinstance(value, bool):
                    raise ValueError()

                number = float(value)
                if not math.isfinite(number) or not -limit <= number <= limit:
                    raise ValueError()
            except (TypeError, ValueError, OverflowError):
                raise ValueError(
                    f"ShakeMap selection requires finite {label} "
                    f"within [-{limit}, {limit}]"
                ) from None

            coordinates.append(number)

        point = Point(coordinates[1], coordinates[0])

        # Cached records are never modified by concurrent workers. Repeated
        # features for one country do not create a false border conflict.
        matches = {
            code
            for code, geometry in _country_records()
            if geometry.covers(point)
        }
        if len(matches) == 1 and None not in matches:
            return matches.pop()

        reason = (
            "no country coverage"
            if not matches else
            "ambiguous or unidentified country coverage"
        )
        self.logger.info(
            "ShakeMap country lookup uses global: %s at latitude=%s longitude=%s",
            reason,
            coordinates[0],
            coordinates[1],
        )
        return None
