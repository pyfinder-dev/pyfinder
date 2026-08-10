"""Neutral earthquake context shared by acquisition and execution stages."""

from collections.abc import Mapping
from dataclasses import dataclass
import math

from pyfinder.utils.timeutils import get_epoch_time


_PUBLIC_MODEL_ACCESS_ERRORS = (
    AttributeError,
    IndexError,
    KeyError,
    TypeError,
    ValueError,
)


class ProviderModelAccessError(Exception):
    """Report a failure while reading a dependency-owned public model."""


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

        return cls._from_values(
            event_id=alert.get("unid"),
            expected_event_id=scheduled_event_id,
            latitude=alert.get("lat"),
            longitude=alert.get("lon"),
            magnitude=alert.get("mag"),
            depth=alert.get("depth"),
            origin_time=alert.get("time"),
            magnitude_type=alert.get("magtype"),
            source_label="alert",
        )

    @classmethod
    def from_provider_model(cls, event_model, *, requested_event_id):
        """Copy and validate one provider's public event model."""
        if event_model is None:
            raise EventContextError("the provider event candidate is missing")

        return cls._from_values(
            event_id=cls._provider_value(
                event_model,
                ("get_event_id", "get_event_unid"),
                "event identifier",
            ),
            expected_event_id=requested_event_id,
            latitude=cls._provider_value(
                event_model,
                ("get_latitude",),
                "latitude",
            ),
            longitude=cls._provider_value(
                event_model,
                ("get_longitude",),
                "longitude",
            ),
            magnitude=cls._provider_value(
                event_model,
                ("get_magnitude",),
                "magnitude",
            ),
            depth=cls._provider_value(
                event_model,
                ("get_depth",),
                "depth",
            ),
            origin_time=cls._provider_value(
                event_model,
                ("get_origin_time", "get_event_time"),
                "origin time",
            ),
            magnitude_type=cls._provider_value(
                event_model,
                ("get_magnitude_type",),
                "magnitude type",
                required=False,
            ),
            source_label="provider",
        )

    @classmethod
    def _from_values(
        cls,
        *,
        event_id,
        expected_event_id,
        latitude,
        longitude,
        magnitude,
        depth,
        origin_time,
        magnitude_type,
        source_label,
    ):
        """Validate copied values shared by alert and provider boundaries."""
        event_id = cls._event_identifier(
            event_id,
            f"{source_label} event ID",
        )
        scheduled_id = cls._event_identifier(
            expected_event_id,
            "requested event ID",
        )
        if event_id != scheduled_id:
            raise EventContextError(
                f"the {source_label} event ID does not match the requested "
                "event ID"
            )

        if not isinstance(origin_time, str) or not origin_time.strip():
            raise EventContextError("origin time is missing or not a string")
        if get_epoch_time(origin_time) is None:
            raise EventContextError(
                "origin time is not accepted by the current timestamp converter"
            )

        if magnitude_type is None:
            magnitude_type = ""
        else:
            magnitude_type = str(magnitude_type)

        return cls(
            _event_id=event_id,
            _latitude=cls._number(
                latitude,
                "latitude",
                minimum=-90,
                maximum=90,
            ),
            _longitude=cls._number(
                longitude,
                "longitude",
                minimum=-180,
                maximum=180,
            ),
            _magnitude=cls._number(magnitude, "magnitude"),
            _depth=cls._number(
                depth,
                "depth",
                minimum=0,
            ),
            _origin_time=origin_time,
            _magnitude_type=magnitude_type,
        )

    @staticmethod
    def _provider_value(event_model, getter_names, label, required=True):
        """Read one public provider value without importing provider models."""
        found_getter = False
        for getter_name in getter_names:
            try:
                getter = getattr(event_model, getter_name, None)
            except _PUBLIC_MODEL_ACCESS_ERRORS as error:
                raise ProviderModelAccessError(
                    f"provider {label} accessor lookup failed"
                ) from error
            if not callable(getter):
                continue
            found_getter = True
            try:
                value = getter()
            except _PUBLIC_MODEL_ACCESS_ERRORS as error:
                raise ProviderModelAccessError(
                    f"provider {label} accessor failed"
                ) from error
            if value is not None:
                return value

        if required and not found_getter:
            raise EventContextError(
                f"provider event candidate has no public {label} getter"
            )
        return None

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
