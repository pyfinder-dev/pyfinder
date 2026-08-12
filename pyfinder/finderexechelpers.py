# -*- coding: utf-8 -*-
"""Implementation helpers for selected :mod:`pyfinder.finderexec` methods."""

from collections.abc import Mapping
import math
from numbers import Real

from pyfinder.finderutils import FinderChannel, FinderChannelList
from pyfinder.utils.calculator import Calculator
from pyfinder.utils.station_merger import RawStationMeasurement


def resolve_live_mode(value):
    """Return the configured FinDer mode without accepting truthy values."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized_value = value.lower()
        if normalized_value == "yes":
            return True
        if normalized_value == "no":
            return False
    raise ValueError(
        "finder-executable.finder-live-mode must be a Boolean or the "
        "case-insensitive string 'yes' or 'no'"
    )


def resolve_artificial_point_margin_percent(value):
    """Return a finite nonnegative percentage as a float."""
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(
            "finder-executable.artificial-point-margin-percent must be a "
            "real number, not a Boolean"
        )
    try:
        normalized_value = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError(
            "finder-executable.artificial-point-margin-percent must be a "
            "finite real number"
        ) from exc
    if not math.isfinite(normalized_value) or normalized_value < 0:
        raise ValueError(
            "finder-executable.artificial-point-margin-percent must be "
            "finite and greater than or equal to zero"
        )
    return normalized_value


def render_amplitude_companion(
    self,
    finder_channels: FinderChannelList,
    event_latitude,
    event_longitude,
) -> bytes:
    """Render operator-facing linear PGA and epicentral-distance rows."""
    # Sorting a copy keeps the completed channel list and data_0 order
    # unchanged. Python's stable sort retains supplied order for PGA ties.
    ordered_channels = sorted(
        finder_channels,
        key=lambda channel: float(channel.pga),
        reverse=True,
    )
    lines = ["# SNCL PGA_CM_S2 EPI_DISTANCE_KM"]
    for index, channel in enumerate(ordered_channels):
        channel_identity = channel.get_sncl()
        try:
            distance_result = Calculator.haversine(
                event_latitude,
                event_longitude,
                channel.latitude,
                channel.longitude,
            )
        except Exception as error:
            raise ValueError(
                "FinDer amplitude companion distance calculation failed "
                f"for channel {index} ({channel_identity})"
            ) from error

        if isinstance(distance_result, bool) or not isinstance(
            distance_result,
            Real,
        ):
            raise ValueError(
                "FinDer amplitude companion distance must be numerical "
                f"for channel {index} ({channel_identity})"
            )
        try:
            distance_km = float(distance_result)
        except (OverflowError, TypeError, ValueError) as error:
            raise ValueError(
                "FinDer amplitude companion distance must be a finite "
                f"number for channel {index} ({channel_identity})"
            ) from error
        if not math.isfinite(distance_km) or distance_km < 0:
            raise ValueError(
                "FinDer amplitude companion distance must be finite and "
                f"nonnegative for channel {index} ({channel_identity})"
            )

        # Distance is derived only for operator information; it is not
        # retained in FinderChannel or used to order companion rows.
        lines.append(
            f"{channel_identity} {repr(float(channel.pga))} "
            f"{distance_km:.1f}"
        )

    return "\n".join(lines).encode("ascii")


def build_real_finder_channels(
    observations: list[RawStationMeasurement],
) -> FinderChannelList:
    """Copy the merger-selected observations into linear channels."""
    if not isinstance(observations, list):
        raise TypeError(
            "FinDerExecutable requires merged normalized observations "
            "as a list"
        )
    if not observations:
        raise ValueError(
            "FinDerExecutable requires at least one merged normalized "
            "observation"
        )

    finder_channels = FinderChannelList()
    for index, observation in enumerate(observations):
        if not isinstance(observation, Mapping):
            raise TypeError(
                f"Merged normalized observation {index} must be a mapping"
            )
        try:
            finder_channels.append(FinderChannel(
                latitude=observation["latitude"],
                longitude=observation["longitude"],
                network_code=observation["network"],
                station_code=observation["station"],
                location_code=observation["location"],
                channel_code=observation["channel"],
                pga=observation["pga"],
                is_artificial=False,
            ))
        except KeyError as error:
            raise ValueError(
                f"Merged normalized observation {index} is missing "
                f"required field {error.args[0]!r}"
            ) from error

    return finder_channels


def calculate_artificial_linear_pga(
    self,
    finder_channels: FinderChannelList,
    event_magnitude: float,
    event_depth: float,
) -> float:
    """Select the stabilizing PGA from linear event and observed values."""
    predicted_event_linear_pga = Calculator.predict_PGA_from_magnitude(
        event_magnitude,
        event_depth,
        log_scale=False,
    )
    if (
        isinstance(predicted_event_linear_pga, bool)
        or not isinstance(predicted_event_linear_pga, Real)
    ):
        raise ValueError("predicted artificial PGA must be numerical")
    try:
        predicted_event_linear_pga = float(predicted_event_linear_pga)
    except (OverflowError, TypeError, ValueError) as error:
        raise ValueError(
            "predicted artificial PGA must be finite"
        ) from error
    if (
        not math.isfinite(predicted_event_linear_pga)
        or predicted_event_linear_pga <= 0
    ):
        raise ValueError(
            "predicted artificial PGA must be finite and greater than zero"
        )

    maximum_real_linear_pga = max(
        float(channel.pga) for channel in finder_channels
    )
    observed_margin_pga = maximum_real_linear_pga * (
        1 + self.artificial_point_margin_percent / 100
    )
    if not math.isfinite(observed_margin_pga):
        raise ValueError("observed-margin artificial PGA must be finite")

    artificial_linear_pga = max(
        predicted_event_linear_pga,
        observed_margin_pga,
    )
    if (
        not math.isfinite(artificial_linear_pga)
        or artificial_linear_pga <= 0
    ):
        raise ValueError(
            "selected artificial PGA must be finite and greater than zero"
        )

    self.logger.info("Adding artificial PGA:")
    self.logger.info(
        "Maximum observed linear PGA: "
        f"{maximum_real_linear_pga:.5f} cm/s^2"
    )
    self.logger.info(
        "Event-predicted linear PGA: "
        f"{predicted_event_linear_pga:.5f} cm/s^2"
    )
    self.logger.info(
        "Configured artificial-point margin: "
        f"{self.artificial_point_margin_percent}%"
    )
    self.logger.info(
        "Observed PGA with artificial margin: "
        f"{observed_margin_pga:.5f} cm/s^2"
    )
    self.logger.info(
        f"Selected artificial PGA: {artificial_linear_pga:.5f} cm/s^2"
    )

    return artificial_linear_pga
