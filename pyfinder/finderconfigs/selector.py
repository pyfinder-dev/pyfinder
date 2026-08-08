"""Resolve one complete FinDer configuration from an epicenter location."""

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from importlib import resources
import logging
import math


class GlobalFinderConfigError(RuntimeError):
    """Report that the required global fallback cannot support selection."""


@dataclass(frozen=True)
class ResolvedFinderConfig:
    """Hold one selected configuration name and an execution-owned dictionary."""

    configuration_name: str
    configuration: dict


class FinderConfigSelector:
    """Cache registered geometry and resolve an isolated FinDer configuration."""

    COORDINATE_REFERENCE_SYSTEM = "EPSG:4326"

    def __init__(
        self,
        global_profile,
        regional_profiles,
        *,
        resource_root=None,
        resource_resolver=None,
        logger=None,
    ):
        self._logger = logger if logger is not None else logging.getLogger(__name__)
        self._global_configuration = self._validate_global_profile(
            global_profile
        )
        self._global_keys = frozenset(self._global_configuration)
        self._regional_profiles = tuple(regional_profiles)
        self._resource_root = (
            resource_root
            if resource_root is not None
            else resources.files("pyfinder").joinpath(
                "extern",
                "finder_regional_wkt",
            )
        )
        self._resource_resolver = (
            resource_resolver
            if resource_resolver is not None
            else self._join_resource
        )
        self._regional_geometries = ()
        self._catalog_failures = ()
        self._point_type = None
        self._load_registered_geometries()

    @staticmethod
    def _join_resource(resource_root, filename):
        return resource_root.joinpath(filename)

    @staticmethod
    def _native_line_error(configuration):
        """Return why a mapping entry cannot form one native config line."""
        for key, value in configuration.items():
            if not isinstance(key, str) or not key:
                return "native field names must be non-empty strings"
            if any(character.isspace() for character in key):
                return "native field names must not contain whitespace"

            try:
                rendered_value = str(value)
            except Exception as exc:
                return "native field value conversion failed: {0}: {1}".format(
                    type(exc).__name__,
                    exc,
                )
            if "\r" in rendered_value or "\n" in rendered_value:
                return "native field values must not contain line breaks"
        return None

    @staticmethod
    def _validate_global_profile(global_profile):
        name = getattr(global_profile, "name", None)
        wkt_filename = getattr(global_profile, "wkt_filename", None)
        configuration = getattr(global_profile, "configuration", None)

        if name != "global":
            raise GlobalFinderConfigError(
                "The required global FinDer profile must be named 'global'."
            )
        if wkt_filename is not None:
            raise GlobalFinderConfigError(
                "The global FinDer profile must not register a WKT resource."
            )
        if not isinstance(configuration, Mapping) or not configuration:
            raise GlobalFinderConfigError(
                "The global FinDer profile must contain a non-empty mapping."
            )
        if "DATA_FOLDER" not in configuration:
            raise GlobalFinderConfigError(
                "The global FinDer configuration must define DATA_FOLDER."
            )
        native_line_error = FinderConfigSelector._native_line_error(
            configuration
        )
        if native_line_error is not None:
            raise GlobalFinderConfigError(
                "The global FinDer configuration cannot be written as native "
                "key/value lines: {0}.".format(native_line_error)
            )

        try:
            return deepcopy(dict(configuration))
        except Exception as exc:
            raise GlobalFinderConfigError(
                "The global FinDer configuration cannot be isolated safely."
            ) from exc

    def _load_registered_geometries(self):
        if not self._regional_profiles:
            return

        # Geometry dependencies stay inside explicit selector construction so
        # importing pyfinder.finderconfigs remains free of WKT and native-
        # geometry initialization work.
        try:
            import geopandas
            from shapely.geometry import MultiPolygon, Point, Polygon
        except Exception as exc:
            self._catalog_failures = (
                "Geometry dependencies could not be imported: {0}: {1}".format(
                    type(exc).__name__,
                    exc,
                ),
            )
            return

        self._point_type = Point
        supported_geometry_types = (Polygon, MultiPolygon)
        loaded_geometries = []
        catalog_failures = []

        for profile in self._regional_profiles:
            filename = getattr(profile, "wkt_filename", None)
            try:
                resource = self._resource_resolver(
                    self._resource_root,
                    filename,
                )
                wkt_text = resource.read_text(encoding="utf-8")
                if not isinstance(wkt_text, str) or not wkt_text.strip():
                    raise ValueError("registered WKT content is empty")

                geometry_series = geopandas.GeoSeries.from_wkt(
                    [wkt_text],
                    crs=self.COORDINATE_REFERENCE_SYSTEM,
                    on_invalid="raise",
                )
                geometry = geometry_series.iloc[0]
                if not isinstance(geometry, supported_geometry_types):
                    geometry_type = (
                        geometry.geom_type if geometry is not None else "None"
                    )
                    raise ValueError(
                        "registered WKT geometry type is unsupported: {0}".format(
                            geometry_type
                        )
                    )
                if geometry.is_empty:
                    raise ValueError("registered WKT geometry is empty")
                if not geometry.is_valid:
                    raise ValueError("registered WKT geometry is invalid")

                bounds = tuple(float(value) for value in geometry.bounds)
                if len(bounds) != 4 or not all(
                    math.isfinite(value) for value in bounds
                ):
                    raise ValueError("registered WKT bounds are not finite")
                min_longitude, min_latitude, max_longitude, max_latitude = bounds
                if min_longitude < -180 or max_longitude > 180:
                    raise ValueError(
                        "registered WKT longitude bounds exceed [-180, 180]"
                    )
                if min_latitude < -90 or max_latitude > 90:
                    raise ValueError(
                        "registered WKT latitude bounds exceed [-90, 90]"
                    )

                loaded_geometries.append(
                    (profile, geometry, geometry_series.crs)
                )
            except Exception as exc:
                catalog_failures.append(
                    "{0}: {1}: {2}".format(
                        filename if filename is not None else "<missing filename>",
                        type(exc).__name__,
                        exc,
                    )
                )

        # Successful entries remain cached for diagnostics, but one failed
        # registered resource poisons regional selection as a whole. This
        # prevents a damaged catalog from silently changing its selection order.
        self._regional_geometries = tuple(loaded_geometries)
        self._catalog_failures = tuple(catalog_failures)

    def resolve(self, latitude, longitude):
        """Resolve one profile using explicit latitude and longitude values."""

        try:
            normalized_latitude = self._normalize_coordinate(
                latitude,
                "latitude",
                -90,
                90,
            )
            normalized_longitude = self._normalize_coordinate(
                longitude,
                "longitude",
                -180,
                180,
            )
        except ValueError as exc:
            return self._global_fallback(
                "Invalid computational-profile coordinates: {0}".format(exc)
            )

        if self._catalog_failures:
            return self._global_fallback(
                "Registered computational-region WKT failure: {0}".format(
                    "; ".join(self._catalog_failures)
                )
            )

        if not self._regional_geometries:
            return self._global_fallback(
                "No computational regions are registered for selection."
            )

        point = self._point_type(
            normalized_longitude,
            normalized_latitude,
        )
        for profile, geometry, _crs in self._regional_geometries:
            if geometry.covers(point):
                return self._resolved_regional_profile(profile)

        return self._global_fallback(
            "No registered computational region covers latitude {0} and "
            "longitude {1}.".format(
                normalized_latitude,
                normalized_longitude,
            )
        )

    @staticmethod
    def _normalize_coordinate(value, label, minimum, maximum):
        if value is None or isinstance(value, bool):
            raise ValueError("{0} is missing or boolean".format(label))

        try:
            normalized = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("{0} is not numeric".format(label)) from exc

        if not math.isfinite(normalized):
            raise ValueError("{0} is not finite".format(label))
        if normalized < minimum or normalized > maximum:
            raise ValueError(
                "{0} is outside [{1}, {2}]".format(
                    label,
                    minimum,
                    maximum,
                )
            )
        return normalized

    def _resolved_regional_profile(self, profile):
        profile_name = getattr(profile, "name", None)
        configuration = getattr(profile, "configuration", None)
        native_line_error = (
            self._native_line_error(configuration)
            if isinstance(configuration, Mapping) and configuration
            else None
        )
        if (
            not isinstance(profile_name, str)
            or not profile_name.strip()
            or not isinstance(configuration, Mapping)
            or not configuration
            or frozenset(configuration) != self._global_keys
            or native_line_error is not None
        ):
            return self._global_fallback(
                "Selected regional FinDer configuration {0!r} is missing, "
                "invalid, or incomplete.".format(profile_name)
            )

        try:
            isolated_configuration = deepcopy(dict(configuration))
        except Exception as exc:
            return self._global_fallback(
                "Selected regional FinDer configuration {0!r} cannot be "
                "isolated safely: {1}: {2}".format(
                    profile_name,
                    type(exc).__name__,
                    exc,
                )
            )

        return ResolvedFinderConfig(
            configuration_name=profile_name,
            configuration=isolated_configuration,
        )

    def _global_fallback(self, diagnostic):
        self._logger.critical(diagnostic)
        return ResolvedFinderConfig(
            configuration_name="global",
            configuration=deepcopy(self._global_configuration),
        )


def build_default_selector(
    *,
    resource_root=None,
    resource_resolver=None,
    logger=None,
):
    """Construct the accepted selector from the explicit static profile catalog."""

    from pyfinder.finderconfigs.profiles import (
        GLOBAL_PROFILE,
        REGIONAL_PROFILES,
    )

    return FinderConfigSelector(
        GLOBAL_PROFILE,
        REGIONAL_PROFILES,
        resource_root=resource_root,
        resource_resolver=resource_resolver,
        logger=logger,
    )
