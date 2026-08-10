# -*- coding: utf-8 -*-
#!/usr/bin/env python
"""Listen for EMSC alerts and hand eligible messages to the workflow."""

from __future__ import unicode_literals

from collections.abc import Collection, Mapping
import json
import logging
import math
import threading

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


def listen(ws, processor, logger, stop_event=None):
    """Read messages from one open WebSocket until it closes."""
    while stop_event is None or not stop_event.is_set():
        message = yield ws.read_message()
        if message is None:
            logger.info("WebSocket closed")
            break
        if stop_event is not None and stop_event.is_set():
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
    stop_event=None,
):
    """Maintain the existing reconnect loop around the EMSC WebSocket."""
    while stop_event is None or not stop_event.is_set():
        try:
            logger.info("Opening WebSocket connection to %s", echo_uri)
            ws = yield websocket_connect(echo_uri, ping_interval=ping_interval)
            if stop_event is not None and stop_event.is_set():
                ws.close()
                break
            logger.info(
                "WebSocket connection established. Waiting for messages..."
            )
            yield listen_coroutine(
                ws,
                processor=processor,
                logger=logger,
                stop_event=stop_event,
            )
        except Exception:
            if stop_event is not None and stop_event.is_set():
                break
            logger.exception("Connection error")
            logger.info("Retrying connection in 5 seconds...")
            yield sleep(5)


class EMSCListener:
    """Own one listener's persistence and controllable read loop."""

    def __init__(
        self,
        policy,
        *,
        db_path,
        logger,
        configuration,
    ):
        from functools import partial

        from tornado import gen
        from tornado.ioloop import IOLoop
        from tornado.websocket import websocket_connect

        from pyfinder.services.eventtracker import EventTracker

        listener_config = configuration.get("seismic-portal-listener", {})
        self.target_regions = normalize_target_regions(
            listener_config.get("target-regions")
        )
        self.min_magnitude = normalize_min_magnitude(
            listener_config.get("min-magnitude")
        )
        self.echo_uri = listener_config["echo-uri"]
        self.ping_interval = listener_config["ping-interval"]
        self.logger = logger
        self._tracker = EventTracker(str(db_path), logger=logger)
        handoff = _make_eventtracker_handoff(self._tracker, policy, logger)
        self._processor = partial(
            process_emsc_message,
            target_regions=self.target_regions,
            min_magnitude=self.min_magnitude,
            handoff=handoff,
            logger=logger,
        )
        self._gen = gen
        self._ioloop_type = IOLoop
        self._websocket_connect = websocket_connect
        self._stop_event = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._ioloop = None
        self._closed = False

    def run(self):
        """Run the WebSocket loop until it stops or shutdown is requested."""
        listen_coroutine = self._gen.coroutine(listen)
        launch_coroutine = self._gen.coroutine(launch_client)

        self.logger.info(
            " ===== Starting Seismic Portal WebSocket listener ====="
        )
        self.logger.info("Configuration:")
        self.logger.info(
            "|- Target regions: %s", self.target_regions or ("world",)
        )
        self.logger.info("|- Minimum magnitude: %s", self.min_magnitude)
        self.logger.info("|- WebSocket URI: %s", self.echo_uri)

        with self._lifecycle_lock:
            if self._stop_event.is_set():
                return
            self._ioloop = self._ioloop_type()
            ioloop = self._ioloop

        self.logger.info("Launching WebSocket client")
        ioloop.add_callback(
            launch_coroutine,
            echo_uri=self.echo_uri,
            ping_interval=self.ping_interval,
            processor=self._processor,
            logger=self.logger,
            websocket_connect=self._websocket_connect,
            listen_coroutine=listen_coroutine,
            sleep=self._gen.sleep,
            stop_event=self._stop_event,
        )

        try:
            self.logger.info("Starting the IOLoop ...")
            ioloop.start()
        finally:
            with self._lifecycle_lock:
                self._ioloop = None
            ioloop.close(all_fds=True)

    def stop(self):
        """Stop accepting alerts and interrupt the owned read loop."""
        self._stop_event.set()
        with self._lifecycle_lock:
            ioloop = self._ioloop
        if ioloop is not None:
            ioloop.add_callback(ioloop.stop)

    def close(self):
        """Close listener persistence once its read loop has ended."""
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
        self._tracker.close()


def build_emsc_listener(
    policy=None,
    *,
    db_path=None,
    logger=None,
    configuration=None,
):
    """Construct a listener after its policy and paths are validated."""
    logger = logger or logging.getLogger(__name__)

    if policy is None:
        try:
            from pyfinder.services.querypolicy import RRSMQueryPolicy

            policy = RRSMQueryPolicy()
        except Exception:
            logger.exception(
                "RRSM policy validation failed; aborting listener startup"
            )
            raise

    if db_path is None:
        raise ValueError(
            "the EMSC listener requires an explicit operational database path"
        )

    if configuration is None:
        from pyfinder.pyfinderconfig import pyfinderconfig

        configuration = pyfinderconfig
    return EMSCListener(
        policy,
        db_path=db_path,
        logger=logger,
        configuration=configuration,
    )


def start_emsc_listener(
    policy=None,
    *,
    db_path=None,
    logger=None,
    configuration=None,
):
    """Construct and run one listener for direct library callers."""
    listener = build_emsc_listener(
        policy=policy,
        db_path=db_path,
        logger=logger,
        configuration=configuration,
    )
    try:
        listener.run()
    finally:
        listener.stop()
        listener.close()


if __name__ == "__main__":
    start_emsc_listener()
