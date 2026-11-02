"""Resolve an EMSC UNID to the preferred USGS association for one attempt.

EventID estimates associations from earthquake parameters; it is not an
identity authority. Keep its returned evidence alongside the query ID, and
leave the workflow's original identity unchanged. The caller owns activation,
provider failure handling, and any later scheduled attempt.
"""

from dataclasses import dataclass
from http.client import HTTPException
import json
import math
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


EVENTID_ENDPOINT = "https://www.seismicportal.eu/eventid/api/convert"


class EventIDMappingError(RuntimeError):
    """A lookup failed, with a stable reason for provider outcome diagnostics."""

    def __init__(self, reason, message, *, status_code=None):
        super().__init__(message)
        self.reason = reason
        self.status_code = status_code


@dataclass(frozen=True)
class EventIDAssociation:
    """Keep the agency query ID separate from the original workflow ID."""

    source_id: str
    usgs_id: str
    misfit: float
    association: dict


def _preferred_association(source_id, payload):
    """Validate the preferred result without inventing local matching rules."""
    if not isinstance(payload, list):
        raise EventIDMappingError(
            "invalid_response", "EventID response must be a list of associations"
        )

    if not payload:
        raise EventIDMappingError("no_match", "EventID found no USGS association")

    # The request selects only USGS. Accepting a different catalog, a blank ID,
    # or incomplete matching evidence would disguise a service/schema problem.
    # Identifiers are otherwise opaque: ComCat also uses prefixes such as ci.
    for row in payload:
        if not isinstance(row, dict) or row.get("catalog") != "USGS":
            raise EventIDMappingError(
                "invalid_response", "EventID returned an unexpected association catalog"
            )

        event_id = row.get("eventid")
        misfit = row.get("misfit")
        if (
            not isinstance(event_id, str)
            or not event_id
            or any(character.isspace() or ord(character) < 32 for character in event_id)
            or isinstance(misfit, bool)
            or not isinstance(misfit, (int, float))
            or not math.isfinite(misfit)
            or misfit < 0
        ):
            raise EventIDMappingError(
                "invalid_response", "EventID returned an invalid event ID or misfit"
            )

    # preferred_only asks the service to choose. Multiple distinct IDs cannot
    # be resolved by response order or by recreating its scientific ranking.
    if len({row["eventid"] for row in payload}) != 1:
        raise EventIDMappingError(
            "ambiguous_response", "EventID returned multiple preferred USGS IDs"
        )

    if len(payload) != 1:
        raise EventIDMappingError(
            "invalid_response", "EventID returned duplicate preferred associations"
        )

    row = payload[0]
    association = {
        "catalog": "USGS",
        "eventid": row["eventid"],
        "misfit": float(row["misfit"]),
    }

    # Optional diagnostics explain the association without retaining unrelated
    # response content. They are evidence, not additional acceptance cutoffs.
    for key in ("m2", "delta_time", "delta_loc", "delta_mag"):
        value = row.get(key)
        if (
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value)
        ):
            association[key] = value

    return EventIDAssociation(
        source_id=source_id,
        usgs_id=row["eventid"],
        misfit=float(row["misfit"]),
        association=association,
    )


class EventIDMapper:
    """Perform one bounded lookup, without retries or a persistent cache."""

    def __init__(self, *, timeout=30.0):
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("EventID timeout must be a positive finite number of seconds")

        self.timeout = float(timeout)

    def resolve(self, source_id):
        """Return the selected USGS association or a classified lookup failure."""
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError("EventID source_id must be a nonempty string")

        # Continuous and playback workflows use the UNID catalog, which differs
        # from EMSC's numeric catalog IDs. Leave all matching thresholds at the
        # service defaults; include_info retains its association evidence.
        query = urlencode({
            "source_id": source_id,
            "source_catalog": "UNID",
            "out_catalog": "USGS",
            "preferred_only": "true",
            "include_info": "true",
        })
        request = Request(
            f"{EVENTID_ENDPOINT}?{query}",
            headers={"Accept": "application/json"},
            method="GET",
        )

        try:
            with urlopen(request, timeout=self.timeout) as response:
                status = response.status
                body = response.read()
        except HTTPError as error:
            # A documented 404 means no association. Other HTTP failures do
            # not prove absence and must remain visible as service failures.
            with error:
                status = error.code
            body = b""
        except TimeoutError as error:
            raise EventIDMappingError("timeout", "EventID request timed out") from error
        except URLError as error:
            reason = (
                "timeout" if isinstance(error.reason, TimeoutError)
                else "transport_failure"
            )
            raise EventIDMappingError(reason, "EventID request could not complete") from error
        except (OSError, HTTPException) as error:
            raise EventIDMappingError(
                "transport_failure", "EventID response could not be received"
            ) from error

        if status == 404:
            raise EventIDMappingError(
                "no_match", "EventID found no USGS association", status_code=status
            )

        if status != 200:
            raise EventIDMappingError(
                "service_failure", f"EventID returned HTTP {status}", status_code=status
            )

        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError) as error:
            raise EventIDMappingError(
                "invalid_response", "EventID returned invalid JSON"
            ) from error

        # The URL in a returned row is provenance only. It is never followed;
        # the existing ComCat client receives only the validated USGS ID.
        return _preferred_association(source_id, payload)
