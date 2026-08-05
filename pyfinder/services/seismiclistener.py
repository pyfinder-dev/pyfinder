# -*- coding: utf-8 -*-
#!/usr/bin/env python
"""Listen for EMSC alerts and hand eligible messages to the workflow."""

from __future__ import unicode_literals

from collections.abc import Collection, Mapping
import json
import logging
import math

from pyfinder.utils.timeutils import parse_normalized_iso8601


_DEFAULT_MIN_MAGNITUDE = 3.0
_WORLDWIDE_REGION_VALUES = frozenset(("", "all", "world"))
_REQUIRED_PROPERTIES = (
    "unid",
    "mag",
    "time",
    "lastupdate",
    "flynn_region",
)
_SUPPORTED_ACTIONS = frozenset(("create", "update"))


def normalize_target_regions(configured_regions=None):
    """Validate and normalize one startup region-filter configuration."""
    if configured_regions is None:
        return ()
    if isinstance(configured_regions, str):
        region_values = (configured_regions,)
    elif isinstance(configured_regions, Collection) and not isinstance(
        configured_regions, (bytes, bytearray, Mapping)
    ):
        region_values = configured_regions
    else:
        raise ValueError(
            "Target regions must be a string or a collection of strings"
        )

    normalized_regions = []
    for region in region_values:
        if not isinstance(region, str):
            raise ValueError("Every target region must be a string")
        normalized_region = region.strip().casefold()
        if normalized_region in _WORLDWIDE_REGION_VALUES:
            return ()
        if normalized_region not in normalized_regions:
            normalized_regions.append(normalized_region)
    return tuple(normalized_regions)


def normalize_min_magnitude(configured_magnitude=None):
    """Validate and normalize the minimum magnitude once during startup."""
    if configured_magnitude is None:
        return _DEFAULT_MIN_MAGNITUDE
    if isinstance(configured_magnitude, bool):
        raise ValueError("Minimum magnitude must be a finite number")

    if isinstance(configured_magnitude, (int, float, str)):
        try:
            magnitude = float(configured_magnitude)
        except (TypeError, ValueError) as error:
            raise ValueError("Minimum magnitude must be a finite number") from error
    else:
        raise ValueError("Minimum magnitude must be a finite number")

    if not math.isfinite(magnitude):
        raise ValueError("Minimum magnitude must be a finite number")
    return magnitude


def _normalize_alert_magnitude(value):
    """Return one finite alert magnitude or raise for malformed input."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("Alert magnitude must be a finite number")
    try:
        magnitude = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError("Alert magnitude must be a finite number") from error
    if not math.isfinite(magnitude):
        raise ValueError("Alert magnitude must be a finite number")
    return magnitude


def _decode_alert(message):
    """Decode and validate one message without mutating its source mapping."""
    if isinstance(message, bytes):
        message = message.decode("utf-8")
    elif not isinstance(message, str):
        raise ValueError("The inbound message must be JSON text or UTF-8 bytes")

    envelope = json.loads(message)
    if not isinstance(envelope, Mapping):
        raise ValueError("The alert root must be a mapping")
    if "action" not in envelope:
        raise ValueError("The alert action is missing")

    data = envelope.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("The alert data member must be a mapping")
    properties = data.get("properties")
    if not isinstance(properties, Mapping):
        raise ValueError("The alert properties member must be a mapping")

    missing = [key for key in _REQUIRED_PROPERTIES if key not in properties]
    if missing:
        raise ValueError(
            "The alert is missing required properties: {0}".format(
                ", ".join(missing)
            )
        )

    # The copied mapping is the handoff value. Normalized fields and the
    # authoritative envelope action must never alter json.loads' mapping.
    information = dict(properties)
    event_id = information["unid"]
    if not isinstance(event_id, str) or not event_id.strip():
        raise ValueError("The alert event identifier must be a non-empty string")
    information["unid"] = event_id.strip()

    if not isinstance(information["flynn_region"], str):
        raise ValueError("The alert Flynn region must be a string")
    information["mag"] = _normalize_alert_magnitude(information["mag"])
    information["action"] = envelope["action"]

    # Timestamp behavior is deliberately retained for this transitional pass.
    # The existing parser and isoformat precision remain unchanged until X3a.
    origin_time = parse_normalized_iso8601(
        information["time"]
    ).isoformat(timespec="microseconds")
    last_update_time = parse_normalized_iso8601(
        information["lastupdate"]
    ).isoformat(timespec="seconds")
    return information, origin_time, last_update_time


def _matches_region(flynn_region, normalized_regions):
    """Apply case-insensitive substring matching to normalized regions."""
    if not normalized_regions:
        return True
    event_region = flynn_region.casefold()
    return any(region in event_region for region in normalized_regions)


def process_emsc_message(
    message,
    target_regions,
    min_magnitude,
    handoff,
    logger,
):
    """Validate, filter, and hand off one EMSC message at most once."""
    try:
        information, origin_time, last_update_time = _decode_alert(message)
    except Exception as error:
        logger.warning("Malformed EMSC message: %s", error)
        return

    action = information["action"]
    if not isinstance(action, str) or action not in _SUPPORTED_ACTIONS:
        logger.info(
            "Unsupported EMSC action for event %s: %r",
            information["unid"],
            action,
        )
        return

    if not _matches_region(information["flynn_region"], target_regions):
        logger.info(
            "EMSC filter rejection for event %s: region %r did not match %r",
            information["unid"],
            information["flynn_region"],
            target_regions,
        )
        return
    if information["mag"] < min_magnitude:
        logger.info(
            "EMSC filter rejection for event %s: magnitude %s is below %s",
            information["unid"],
            information["mag"],
            min_magnitude,
        )
        return

    try:
        handoff(information, origin_time, last_update_time)
    except Exception:
        logger.exception(
            "EMSC handoff failure for event %s with action %s",
            information["unid"],
            action,
        )
        return

    logger.info(
        "Accepted EMSC handoff for event %s with action %s",
        information["unid"],
        action,
    )


def _make_eventtracker_handoff(tracker, policy, logger):
    """Bind accepted listener alerts to the EventTracker-owned operation."""

    def handoff(information, origin_time, last_update_time):
        action = information["action"]
        logger.info(
            "Received accepted EMSC %s alert for event %s at %s, "
            "Magnitude: %s, Region: %s",
            action,
            information["unid"],
            information["time"],
            information["mag"],
            information["flynn_region"],
        )

        try:
            emsc_alert_json = json.dumps(information)
        except Exception as error:
            logger.warning(
                "Could not serialize alert JSON for event %s: %s",
                information["unid"],
                error,
            )
            emsc_alert_json = None

        tracker.apply_emsc_alert(
            event_id=information["unid"],
            policy=policy,
            origin_time=origin_time,
            last_update_time=last_update_time,
            emsc_alert_json=emsc_alert_json,
        )

    return handoff


def listen(ws, processor, logger):
    """Read messages from one open WebSocket until it closes."""
    while True:
        message = yield ws.read_message()
        if message is None:
            logger.info("WebSocket closed")
            break
        processor(message)


def launch_client(
    echo_uri,
    ping_interval,
    processor,
    logger,
    websocket_connect,
    listen_coroutine,
    sleep,
):
    """Maintain the existing reconnect loop around the EMSC WebSocket."""
    while True:
        try:
            logger.info("Opening WebSocket connection to %s", echo_uri)
            ws = yield websocket_connect(echo_uri, ping_interval=ping_interval)
            logger.info(
                "WebSocket connection established. Waiting for messages..."
            )
            yield listen_coroutine(ws, processor=processor, logger=logger)
        except Exception:
            logger.exception("Connection error")
            logger.info("Retrying connection in 5 seconds...")
            yield sleep(5)


def start_emsc_listener(policy=None):
    """Start the Seismic Portal WebSocket listener service."""
    # The listener logger must exist before standalone startup validates its
    # policy. Parent startup may supply the already-validated policy instead.
    from functools import partial

    from pyfinder.utils.customlogger import file_logger

    logger = file_logger(
        module_name="SeismicListener",
        log_file="seismiclistener.log",
        rotate=True,
        overwrite=False,
        level=logging.DEBUG,
    )

    if policy is None:
        try:
            from pyfinder.services.querypolicy import RRSMQueryPolicy

            policy = RRSMQueryPolicy()
        except Exception:
            logger.exception(
                "RRSM policy validation failed; aborting listener startup"
            )
            raise

    # Operational runtime imports and construction occur only after policy
    # validation has succeeded at this startup boundary.
    from tornado import gen
    from tornado.ioloop import IOLoop
    from tornado.websocket import websocket_connect

    from pyfinder.pyfinderconfig import pyfinderconfig
    from pyfinder.services.eventtracker import EventTracker

    listener_config = pyfinderconfig.get("seismic-portal-listener", {})
    target_regions = normalize_target_regions(
        listener_config.get("target-regions")
    )
    min_magnitude = normalize_min_magnitude(
        listener_config.get("min-magnitude")
    )
    echo_uri = listener_config["echo-uri"]
    ping_interval = listener_config["ping-interval"]

    tracker = EventTracker("event_update_follow_up.db", logger=logger)
    handoff = _make_eventtracker_handoff(tracker, policy, logger)
    processor = partial(
        process_emsc_message,
        target_regions=target_regions,
        min_magnitude=min_magnitude,
        handoff=handoff,
        logger=logger,
    )
    listen_coroutine = gen.coroutine(listen)
    launch_coroutine = gen.coroutine(launch_client)

    logger.info(" ===== Starting Seismic Portal WebSocket listener =====")
    logger.info("Configuration:")
    logger.info("|- Target regions: %s", target_regions or ("world",))
    logger.info("|- Minimum magnitude: %s", min_magnitude)
    logger.info("|- WebSocket URI: %s", echo_uri)

    logger.info("Starting Tornado IOLoop")
    ioloop = IOLoop.instance()
    logger.info("Launching WebSocket client")
    ioloop.add_callback(
        launch_coroutine,
        echo_uri=echo_uri,
        ping_interval=ping_interval,
        processor=processor,
        logger=logger,
        websocket_connect=websocket_connect,
        listen_coroutine=listen_coroutine,
        sleep=gen.sleep,
    )

    try:
        logger.info("Starting the IOLoop ...")
        ioloop.start()
    except KeyboardInterrupt:
        logger.info("Closing WebSocket")
        ioloop.stop()


if __name__ == "__main__":
    start_emsc_listener()
