"""Select ShakeMap service configurations from independent registered regions."""

from functools import lru_cache
from importlib import resources
import logging
import math


@lru_cache(maxsize=None)
def _region_geometry(filename):
    """Cache valid packaged WKT geometry; failed reads remain retryable."""
    from shapely import from_wkt

    resource = resources.files("pyfinder").joinpath(
        "extern", "shakemap_regional_wkt", filename,
    )
    try:
        geometry = from_wkt(resource.read_text(encoding="utf-8"))
        if (
            geometry is None
            or geometry.is_empty
            or not geometry.is_valid
            or geometry.geom_type not in {"Polygon", "MultiPolygon"}
        ):
            raise ValueError("expected a nonempty, valid POLYGON or MULTIPOLYGON")

        west, south, east, north = geometry.bounds
        if not (
            -180 <= west <= east <= 180
            and -90 <= south <= north <= 90
        ):
            raise ValueError("WKT coordinates must be WGS84 longitude/latitude")

        return geometry
    except Exception as error:
        raise ValueError(f"{resource}: {error}") from error


class RegionLocator:
    """Resolve coverage to a profile without depending on country or FinDer IDs."""

    def __init__(self, catalog, *, logger=None):
        self.logger = logger or logging.getLogger(__name__)
        self.regions = tuple(
            (label, region["wkt"], region["configuration"])
            for label, region in catalog.items()
        )

    def get_configuration(self, lat, lon):
        from shapely.geometry import Point

        # A malformed solution is not ordinary lack of coverage. Validate it
        # before reading optional regional resources or choosing global.
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
        profiles = set()
        matched_labels = []
        incomplete_coverage = False

        for label, filename, profile in self.regions:
            try:
                geometry = _region_geometry(filename)
            except ValueError as error:
                # A missing polygon could cover this point. Continuing with
                # another region would pretend the partial catalog is complete.
                # Log the exact resource, use global for this attempt and leave
                # services running so corrected files can be used after redeploy.
                self.logger.warning(
                    "ShakeMap region %s has unusable WKT %s: %s. "
                    "Restore/correct the registered file and rebuild the caller; "
                    "this calculation uses global.",
                    label,
                    filename,
                    error,
                )
                incomplete_coverage = True
                continue

            if geometry.covers(point):
                # Regions are coverage areas, not priority slots. Adjacent or
                # overlapping areas may deliberately share one service profile.
                profiles.add(profile)
                matched_labels.append(label)

        if incomplete_coverage:
            return "global"

        if len(profiles) == 1:
            selected = profiles.pop()
            self.logger.info(
                "ShakeMap matching regions=%s configuration=%s",
                matched_labels,
                selected,
            )
            return selected

        if profiles:
            self.logger.warning(
                "ShakeMap regions %s select conflicting configurations %s at "
                "latitude=%s longitude=%s; this calculation uses global.",
                matched_labels,
                sorted(profiles),
                coordinates[0],
                coordinates[1],
            )
        else:
            self.logger.info(
                "No registered ShakeMap region covers latitude=%s longitude=%s; "
                "this calculation uses global.",
                coordinates[0],
                coordinates[1],
            )

        return "global"
