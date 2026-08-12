# -*- coding: utf-8 -*-
"""Formatting of completed linear channel lists for FinDer input."""

import math
import numbers

import numpy as np

from ...finderutils import FinderChannel, FinderChannelList

class FinDerInputFormatter:
    """Serialize supplied linear channels without changing membership or order."""

    @staticmethod
    def _finite_number(value, field_name):
        """Return one finite boundary value as a Python float."""
        if isinstance(value, (bool, np.bool_)) or not isinstance(
                value, numbers.Real):
            raise ValueError(f"{field_name} must be numerical, not Boolean")
        try:
            normalized_value = float(value)
        except (OverflowError, TypeError, ValueError) as error:
            raise ValueError(f"{field_name} must be a finite number") from error
        if not math.isfinite(normalized_value):
            raise ValueError(f"{field_name} must be finite")
        return normalized_value

    @staticmethod
    def _sncl(channel):
        """Render one exact four-component ASCII SNCL or fail visibly."""
        components = (
            channel.network,
            channel.station,
            channel.location,
            channel.channel,
        )
        component_names = ("network", "station", "location", "channel")
        for component_name, component in zip(component_names, components):
            if not isinstance(component, str):
                raise ValueError(
                    f"FinderChannel {component_name} must be a string"
                )
            if not component.isascii():
                raise ValueError(
                    f"FinderChannel {component_name} must be ASCII"
                )
            if (
                "." in component
                or "/" in component
                or "\\" in component
                or not component.isprintable()
                or any(character.isspace() for character in component)
            ):
                raise ValueError(
                    f"FinderChannel {component_name} is not a valid SNCL "
                    "component"
                )
        if not components[0] or not components[1] or not components[3]:
            raise ValueError(
                "FinderChannel network, station, and channel components "
                "must be nonempty"
            )
        return ".".join(components)

    @classmethod
    def validate_finder_channels(cls, finder_channels):
        """Validate the caller's exact channel membership without changing it."""
        if not isinstance(finder_channels, FinderChannelList):
            raise TypeError(
                "FinDerInputFormatter requires a FinderChannelList"
            )
        if not finder_channels:
            raise ValueError(
                "FinDerInputFormatter requires at least one FinderChannel"
            )

        seen_sncls = set()
        for index, channel in enumerate(finder_channels):
            if not isinstance(channel, FinderChannel):
                raise TypeError(
                    f"FinDer input channel {index} is not a FinderChannel"
                )
            latitude = cls._finite_number(
                channel.latitude,
                f"FinDer input channel {index} latitude",
            )
            longitude = cls._finite_number(
                channel.longitude,
                f"FinDer input channel {index} longitude",
            )
            pga = cls._finite_number(
                channel.pga,
                f"FinDer input channel {index} PGA",
            )
            if latitude < -90 or latitude > 90:
                raise ValueError(
                    f"FinDer input channel {index} latitude is outside "
                    "[-90, 90]"
                )
            if longitude < -180 or longitude > 180:
                raise ValueError(
                    f"FinDer input channel {index} longitude is outside "
                    "[-180, 180]"
                )
            if pga <= 0:
                raise ValueError(
                    f"FinDer input channel {index} PGA must be greater than "
                    "zero"
                )

            sncl = cls._sncl(channel)
            if sncl in seen_sncls:
                raise ValueError(f"Duplicate completed FinDer SNCL: {sncl}")
            seen_sncls.add(sncl)

    @staticmethod
    def _number_text(value):
        """Render a locale-independent double-precision numeric value."""
        return repr(float(value))

    @classmethod
    def format(
        cls,
        finder_channels: FinderChannelList,
        event_time_epoch: float,
        is_live_mode: bool,
    ) -> bytes:
        """Return ``data_0`` bytes for the supplied linear channel list."""
        if not isinstance(is_live_mode, bool):
            raise ValueError("is_live_mode must be a Boolean")
        event_time = cls._finite_number(
            event_time_epoch,
            "authoritative event-origin timestamp",
        )
        cls.validate_finder_channels(finder_channels)

        whole_event_time = int(event_time)
        data_lines = [f"# {whole_event_time} 0"]
        for channel in finder_channels:
            latitude = cls._number_text(channel.latitude)
            longitude = cls._number_text(channel.longitude)
            linear_pga = float(channel.pga)
            if is_live_mode:
                data_lines.append(
                    f"{latitude} {longitude} {cls._sncl(channel)} "
                    f"{whole_event_time} {cls._number_text(linear_pga)}"
                )
            else:
                data_lines.append(
                    f"{latitude} {longitude} "
                    f"{cls._number_text(math.log10(linear_pga))}"
                )

        # Membership and order belong to the caller. Live output writes the
        # stored linear PGA; log10 conversion belongs only to non-live data_0
        # serialization and never changes the FinderChannel objects.
        return "\n".join(data_lines).encode("ascii")
