"""Neutral earthquake context shared by acquisition and execution stages."""

from collections.abc import Mapping
from dataclasses import dataclass
import math

from pyfinder.utils.timeutils import get_epoch_time


class EventContextError(ValueError):
    """Report that authoritative event metadata cannot form a usable context."""


@dataclass(frozen=True)
class EventContext:
    """Copy the authoritative earthquake values needed by current consumers."""

    _event_id: str
    _latitude: float
    _longitude: float
    _magnitude: float
    _depth: float
    _origin_time: str
    _magnitude_type: str = ""

    @classmethod
    def from_alert_mapping(cls, alert, *, scheduled_event_id):
        """Build a validated context from one persisted EMSC alert snapshot."""
        if not isinstance(alert, Mapping):
            raise EventContextError("the persisted EMSC alert is not a mapping")

        event_id = cls._event_identifier(alert.get("unid"), "alert event ID")
        scheduled_id = cls._event_identifier(
            scheduled_event_id,
            "scheduled event ID",
        )
        if event_id != scheduled_id:
            raise EventContextError(
                "the alert event ID does not match the scheduled event ID"
            )

        origin_time = alert.get("time")
        if not isinstance(origin_time, str) or not origin_time.strip():
            raise EventContextError("origin time is missing or not a string")
        if get_epoch_time(origin_time) is None:
            raise EventContextError(
                "origin time is not accepted by the current timestamp converter"
            )

        magnitude_type = alert.get("magtype")
        if magnitude_type is None:
            magnitude_type = ""
        else:
            magnitude_type = str(magnitude_type)

        return cls(
            _event_id=event_id,
            _latitude=cls._number(
                alert.get("lat"),
                "latitude",
                minimum=-90,
                maximum=90,
            ),
            _longitude=cls._number(
                alert.get("lon"),
                "longitude",
                minimum=-180,
                maximum=180,
            ),
            _magnitude=cls._number(alert.get("mag"), "magnitude"),
            _depth=cls._number(
                alert.get("depth"),
                "depth",
                minimum=0,
            ),
            _origin_time=origin_time,
            _magnitude_type=magnitude_type,
        )

    @staticmethod
    def _event_identifier(value, label):
        if not isinstance(value, str) or not value.strip():
            raise EventContextError(f"{label} is missing or empty")
        return value.strip()

    @staticmethod
    def _number(value, label, minimum=None, maximum=None):
        if value is None or isinstance(value, bool):
            raise EventContextError(f"{label} is missing or boolean")
        try:
            normalized = float(value)
        except (TypeError, ValueError, OverflowError) as error:
            raise EventContextError(f"{label} is not numeric") from error
        if not math.isfinite(normalized):
            raise EventContextError(f"{label} is not finite")
        if minimum is not None and normalized < minimum:
            raise EventContextError(f"{label} is below {minimum}")
        if maximum is not None and normalized > maximum:
            raise EventContextError(f"{label} is above {maximum}")
        return normalized

    def get_event_id(self):
        return self._event_id

    def get_event_unid(self):
        return self._event_id

    def get_latitude(self):
        return self._latitude

    def get_longitude(self):
        return self._longitude

    def get_magnitude(self):
        return self._magnitude

    def get_depth(self):
        return self._depth

    def get_origin_time(self):
        return self._origin_time

    def get_event_time(self):
        return self._origin_time

    def get_magnitude_type(self):
        return self._magnitude_type
