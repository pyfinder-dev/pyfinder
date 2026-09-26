"""Build native ShakeMap inputs from the caller's selected FinDer solution."""

from copy import copy
from datetime import datetime, timezone
import json
import math
from numbers import Real
from xml.etree.ElementTree import Element, SubElement, tostring

from ..finderutils import FinderChannel, FinderEvent, FinderRupture, FinderSolution


class ShakeMapExporter:
    """Prepare native input bytes without writing files or running ShakeMap.

    The caller supplies the already-selected raw or processed FinDer solution.
    This class translates that solution into ShakeMap's file formats; it does
    not choose observations, select regional models, or manage calculations.

    ``origin_time`` is the physical earthquake time as an aware datetime.
    FinDer's internally assigned timestamp has a different purpose and is
    deliberately not used as a fallback.
    """

    def __init__(
        self,
        solution: FinderSolution,
        event_id: str,
        origin_time: datetime,
    ):
        """Keep the caller's solution and normalize its physical time to UTC."""
        if not isinstance(solution, FinderSolution):
            raise TypeError("solution must be a FinderSolution")

        self.event_id = _text(event_id, "event_id")

        # An explicit timezone avoids depending on the machine's local clock
        # settings when the same event is exported on different hosts.
        if not isinstance(origin_time, datetime):
            raise TypeError("origin_time must be a timezone-aware datetime")

        if origin_time.tzinfo is None or origin_time.utcoffset() is None:
            raise ValueError("origin_time must include its timezone")

        self.origin_time = origin_time.astimezone(timezone.utc)
        self.solution = solution

    def export_all(self) -> dict[str, bytes]:
        """Return the complete native input bundle for the supplied solution.

        The result always contains event.xml and event_dat.xml. A supplied
        rupture adds rupture.json. Nothing is written until the caller decides
        what to do with these bytes, so a serialization error leaves no partial
        files behind. Native ShakeMap remains responsible for accepting the
        scientific geometry.
        """
        files = {
            "event.xml": self._event_xml(),
            "event_dat.xml": self._event_dat_xml(),
        }

        if self.solution.get_rupture() is not None:
            files["rupture.json"] = self._rupture_json()

        return files

    def _event_xml(self) -> bytes:
        """Combine FinDer's spatial result with the supplied physical origin."""
        event = self.solution.get_event()

        if not isinstance(event, FinderEvent):
            raise ValueError("event.xml requires a FinderEvent")

        # FinderEvent's getters can normalize stored strings in place. Read a
        # copy so exporting cannot alter the caller's original FinDer result.
        event = copy(event)

        # Both XML identity fields describe this caller-owned calculation.
        # Magnitude, location, and depth come from the selected FinDer result;
        # only time comes from the separately supplied earthquake origin.
        root = Element(
            "earthquake",
            {
                "id": self.event_id,
                "event_id": self.event_id,
                "netid": "FinDer",
                "network": "FinDer",
                "mag": str(_number(event.get_magnitude(), "event magnitude")),
                "lat": str(_number(event.get_latitude(), "event latitude")),
                "lon": str(_number(event.get_longitude(), "event longitude")),
                "depth": str(_number(event.get_depth(), "event depth")),
                "time": self.origin_time.isoformat().replace("+00:00", "Z"),
                "locstring": "FinDer Origin",
                "event_type": "ACTUAL",
            },
        )

        return tostring(root, encoding="utf-8", xml_declaration=True)

    def _event_dat_xml(self) -> bytes:
        """Serialize selected channel amplitudes in their existing order."""
        channels = self.solution.get_channels()

        if not channels:
            raise ValueError("event_dat.xml requires selected channels")

        root = Element(
            "stationlist",
            {"xmlns": "ch.ethz.sed.shakemap.usgs.xml"},
        )

        for index, channel in enumerate(channels):
            label = f"channel {index}"

            if not isinstance(channel, FinderChannel):
                raise ValueError(f"{label} must be a FinderChannel")

            # Retain the supplied identities. In particular, location is not
            # replaced with a default and artificial channels are not removed.
            network = _text(channel.get_network_code(), f"{label} network")
            station = _text(channel.get_station_code(), f"{label} station")
            component = _text(channel.get_channel_code(), f"{label} component")
            location = _text(
                channel.get_location_code(),
                f"{label} location",
                empty=True,
            )

            pga = _number(channel.get_pga(), f"{label} PGA")

            if pga <= 0:
                raise ValueError(f"{label} PGA must be positive linear cm/s²")

            # Preserve every selected channel, including location variants,
            # in input order. The native reader groups stations by network/code,
            # not loc; repeated components and station coordinates can therefore
            # be replaced during parsing. This accepted library limitation must
            # not become an exporter rejection or an invented station identity.
            node = SubElement(
                root,
                "station",
                {
                    "code": station,
                    "insttype": component,
                    "lat": str(
                        _number(channel.get_latitude(), f"{label} latitude")
                    ),
                    "lon": str(
                        _number(channel.get_longitude(), f"{label} longitude")
                    ),
                    "source": network,
                    "commtype": "DIG",
                    "netid": network,
                    "loc": location,
                },
            )

            # Omit the optional station name: native ShakeMap can infer a
            # horizontal orientation when it happens to equal a component name.
            # Preserve the actual component, including Z or an unknown numeric
            # code. Adding an N suffix would change its scientific meaning.
            component_node = SubElement(node, "comp", {"name": component})

            # One percent of standard gravity is 9.80665 cm/s². Explicit units
            # avoid relying on the reader's default. Keep numerical precision
            # so small positive amplitudes are not rounded to zero in the XML.
            SubElement(
                component_node,
                "acc",
                {
                    "value": str(pga / 9.80665),
                    "units": "%g",
                    "flag": "0",
                },
            )

        return tostring(root, encoding="utf-8", xml_declaration=True)

    def _rupture_json(self) -> bytes:
        """Express the supplied ordered rupture points as native GeoJSON."""
        rupture = self.solution.get_rupture()

        if not isinstance(rupture, FinderRupture):
            raise ValueError("rupture.json requires a FinderRupture")

        # get_points() uses zip. Check the parallel lists before reading them
        # so an incomplete coordinate list cannot silently discard a point.
        if not (len(rupture.lats) == len(rupture.lons) == len(rupture.depths)):
            raise ValueError(
                "rupture.json has incomplete latitude/longitude/depth points"
            )

        # FinDer stores latitude first; GeoJSON requires longitude first.
        # This changes the representation only, not point order or depth.
        coordinates = [
            [
                _number(lon, "rupture longitude"),
                _number(lat, "rupture latitude"),
                _number(depth, "rupture depth"),
            ]
            for lat, lon, depth in rupture.get_points()
        ]

        if not coordinates:
            raise ValueError("rupture.json requires rupture points")

        # Close a copy for the native file format, leaving FinderRupture intact.
        # No points are reordered and no depth or thickness is manufactured to
        # make an unsupported geometry pass the native reader.
        if coordinates[0] != coordinates[-1]:
            coordinates.append(coordinates[0].copy())

        payload = {
            "type": "FeatureCollection",
            "metadata": {
                "reference": "Generated by FinDer during pyfinder runtime",
            },
            "features": [
                {
                    "type": "Feature",
                    "properties": {"rupture type": "rupture extent"},
                    "geometry": {
                        "type": "MultiPolygon",
                        "coordinates": [[coordinates]],
                    },
                },
            ],
        }

        return json.dumps(payload, allow_nan=False).encode("utf-8")


def _number(value, label):
    """Require a finite number without substituting missing scientific data."""
    if (
        isinstance(value, bool)
        or not isinstance(value, Real)
        or not math.isfinite(value)
    ):
        raise ValueError(f"{label} must be a finite number")

    return float(value)


def _text(value, label, *, empty=False):
    """Preserve supplied text while checking that XML 1.0 can represent it."""
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ValueError(
            f"{label} must be {'a' if empty else 'a nonempty'} string"
        )

    # ElementTree escapes markup characters, but it can still emit characters
    # forbidden by XML 1.0. Check those ranges without rewriting the identity.
    if any(
        not (
            character in "\t\n\r"
            or "\u0020" <= character <= "\ud7ff"
            or "\ue000" <= character <= "\ufffd"
            or "\U00010000" <= character <= "\U0010ffff"
        )
        for character in value
    ):
        raise ValueError(f"{label} contains characters unsupported by XML")

    return value
