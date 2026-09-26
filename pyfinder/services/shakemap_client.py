# -*- coding: utf-8 -*-
"""Small synchronous adapter for the separate ShakeMap service.

The caller owns submission and polling cadence. This module never retries a
submission: a lost acknowledgement can mean that the service accepted work.
Discovery describes operational availability, not scientific suitability.
"""

from __future__ import annotations

from dataclasses import dataclass
from http.client import HTTPException
import json
import math
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4


class ShakeMapClientError(RuntimeError):
    """Base error for the service adapter."""


class ShakeMapHTTPError(ShakeMapClientError):
    """An HTTP error response, retaining its body without logging it."""

    def __init__(self, status_code, body):
        """Keep the service response available for caller-side diagnostics."""
        super().__init__(f"ShakeMap returned HTTP {status_code}")
        self.status_code = status_code
        self.body = body


class ShakeMapProtocolError(ShakeMapClientError):
    """The response cannot safely be interpreted as the requested operation."""


class ShakeMapTransportError(ShakeMapClientError):
    """A read operation could not obtain a complete HTTP response."""


class ShakeMapSubmissionUncertain(ShakeMapClientError):
    """Work may have been accepted; callers must not automatically resubmit."""

    def __init__(
        self,
        message,
        *,
        event_id,
        status_code=None,
        body=None,
    ):
        """Retain the submitted identity and any response for later recovery."""
        super().__init__(message)
        self.event_id = event_id
        self.status_code = status_code
        self.body = body


class ShakeMapJobUnavailable(ShakeMapClientError):
    """The exact job is no longer visible in the requested service view."""


def _basename(value, label):
    """Check a service basename without changing caller-supplied identity."""
    # Preserve identifiers verbatim. In particular, punctuation and Unicode
    # are caller-owned identity, not input for a generated slug.
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or any(unicodedata.category(c) in {"Cc", "Cs"} for c in value)
    ):
        raise ValueError(f"{label} must be a safe, nonempty basename")

    if len(value.encode("utf-8")) > 255:
        raise ValueError(f"{label} exceeds 255 UTF-8 bytes")
    return value


def _event_id(value):
    """Apply the service's additional limits for public event identities."""
    value = _basename(value, "event_id")
    if value.startswith("-") or len(value.encode("utf-8")) > 231:
        raise ValueError("event_id must not start with '-' or exceed 231 UTF-8 bytes")
    return value


def _sequence(value):
    """Require a service sequence number, including when restoring saved jobs."""
    if type(value) is not int or value < 1:
        raise ValueError("internal_sequence must be a positive integer")
    return value


@dataclass(frozen=True)
class AcceptedJob:
    """Persist these two fields to resume polling the exact accepted request."""

    event_id: str
    internal_sequence: int

    def __post_init__(self):
        """Check both identity fields before this handle can be used for polling."""
        _event_id(self.event_id)
        _sequence(self.internal_sequence)


@dataclass(frozen=True)
class JobStatus:
    """One matching service row; ``scope`` is either ``jobs`` or ``archives``.

    Archive SUCCESS records can have unavailable products when only part of
    the archive remains. Keep that distinction visible to the caller.
    """

    job: AcceptedJob
    scope: str
    details: dict

    @property
    def completed(self):
        """Report a terminal outcome, whether successful or failed."""
        return self.details["status"] in {"SUCCESS", "FAILED"}

    @property
    def products_ready(self):
        """Expose the service flag separately from the calculation outcome."""
        return self.details["products_ready"]


class _NoRedirects(HTTPRedirectHandler):
    """Leave redirects visible to the client as ordinary HTTP responses."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Decline urllib's automatic follow-up request."""
        # urllib can turn a redirected POST into a GET. Neither following a
        # redirect nor resending its payload is safe for this non-idempotent API.
        return None


def urllib_transport(method, url, headers, body, timeout):
    """Return ``(HTTP status, response bytes)`` with no redirects or retries.

    An injected transport must use this signature and the same no-retry rule.
    Network failures should raise OSError, URLError, or HTTPException.
    """
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with build_opener(_NoRedirects()).open(request, timeout=timeout) as response:
            return response.status, response.read()
    except HTTPError as error:
        # HTTP rejection still has a usable status and body. Interpret it in
        # the client, where submission and read operations can be distinguished.
        with error:
            return error.code, error.read()


def _state(row, *, archive=False, product_summary=False):
    """Check the status flags provided by the relevant service response view."""
    if not isinstance(row, dict):
        raise ShakeMapProtocolError("Job information is not an object")

    status = row.get("status")
    if not isinstance(status, str) or status not in {
        "QUEUED",
        "RUNNING",
        "SUCCESS",
        "FAILED",
    }:
        raise ShakeMapProtocolError("Job has an unknown status")

    # A retained archive can remember SUCCESS after its products disappear.
    # The current calculation must still have ready products to report SUCCESS.
    ready = row.get("products_ready")
    if type(ready) is not bool or (ready and status != "SUCCESS"):
        raise ShakeMapProtocolError("Job status disagrees with products_ready")
    if not archive and ready != (status == "SUCCESS"):
        raise ShakeMapProtocolError(
            "Current SUCCESS job does not report products_ready"
        )

    # Full current-job rows carry a completion flag. Archive and product-summary
    # views omit it, so only require agreement where the field is part of the view.
    if not archive and not product_summary:
        completed = row.get("job_completed")
        if (
            type(completed) is not bool
            or completed != (status in {"SUCCESS", "FAILED"})
        ):
            raise ShakeMapProtocolError("Job status disagrees with job_completed")


class ShakeMapClient:
    """Expose the existing unversioned service API without workflow machinery."""

    def __init__(self, base_url, *, timeout=30.0, transport=None):
        """Configure the endpoint and one-request timeout without opening a connection."""
        if not isinstance(base_url, str) or any(c.isspace() for c in base_url):
            raise ValueError("base_url must be an absolute HTTP(S) URL")

        try:
            parsed = urlsplit(base_url)
            port = parsed.port
        except ValueError as error:
            raise ValueError("base_url is malformed") from error

        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or port == 0
            or "\\" in base_url
            or any(ord(c) < 32 or ord(c) == 127 for c in base_url)
        ):
            raise ValueError(
                "base_url requires HTTP(S), a host, and no credentials/query/fragment"
            )

        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("timeout must be a positive finite number of seconds")

        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self._transport = transport if transport is not None else urllib_transport

    def _request(
        self,
        method,
        path,
        *,
        body=None,
        content_type=None,
        event_id=None,
    ):
        """Make one request and distinguish rejection from uncertain acceptance."""
        headers = {"Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type

        # A failed read may be polled again by the caller. A failed submission
        # may already be queued remotely, so it must never be retried here.
        try:
            status, raw = self._transport(
                method,
                self.base_url + path,
                headers,
                body,
                self.timeout,
            )
        except (OSError, URLError, HTTPException) as error:
            if method == "POST":
                raise ShakeMapSubmissionUncertain(
                    "ShakeMap submission acknowledgement was lost; acceptance is unknown",
                    event_id=event_id,
                ) from error
            raise ShakeMapTransportError(
                "ShakeMap response could not be received"
            ) from error

        # Preserve non-JSON error bodies as bytes for diagnostics; do not log
        # response contents here or replace them with a JSON-decoding failure.
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError, UnicodeError):
            payload = raw

        if type(status) is not int or not 100 <= status <= 599:
            if method == "POST":
                raise ShakeMapSubmissionUncertain(
                    "ShakeMap returned an invalid HTTP status; acceptance is unknown",
                    event_id=event_id,
                    body=payload,
                )
            raise ShakeMapProtocolError("Transport returned an invalid HTTP status")

        if status >= 400:
            # A server failure can happen after durable acceptance but before
            # acknowledgement. A 503 is the API's explicit admission rejection.
            if method == "POST" and status >= 500 and status != 503:
                raise ShakeMapSubmissionUncertain(
                    "ShakeMap failed before acknowledgement; acceptance is unknown",
                    event_id=event_id,
                    status_code=status,
                    body=payload,
                )
            raise ShakeMapHTTPError(status, payload)

        expected = 202 if method == "POST" else 200
        if status != expected or not isinstance(payload, dict):
            if method == "POST":
                raise ShakeMapSubmissionUncertain(
                    "ShakeMap returned an unusable acknowledgement; acceptance is unknown",
                    event_id=event_id,
                    status_code=status,
                    body=payload,
                )
            raise ShakeMapProtocolError(
                "ShakeMap response is not the expected HTTP 200 JSON object"
            )
        return payload

    def health(self):
        """Return recorded operational readiness, without scientific validation."""
        result = self._request("GET", "/healthz")
        if type(result.get("ready")) is not bool:
            raise ShakeMapProtocolError("Health response has no boolean ready field")
        return result

    def configuration(self):
        """Return effective deployment settings and installed service identity."""
        return self._request("GET", "/config")

    def configurations(self):
        """List configured names; listing does not prove native usability."""
        result = self._request("GET", "/configurations")
        names = result.get("configurations")
        if (
            result.get("default") != "global"
            or not isinstance(names, list)
            or not all(isinstance(name, str) and name for name in names)
            or "global" not in names
        ):
            raise ShakeMapProtocolError("Malformed configuration-name response")
        return result

    def events(self):
        """Return the service's event listing without interpreting workflow state."""
        return self._request("GET", "/events")

    def queue(self):
        """Return the current service queue and capacity information."""
        return self._request("GET", "/queue")

    def submit(
        self,
        event_id,
        files,
        *,
        configuration="global",
        overwrite=True,
    ):
        """Return ``(AcceptedJob, acknowledgement)`` after a usable HTTP 202.

        Files map native basenames to bytes, normally from ShakeMapExporter.
        Empty mappings retain the service's existing canonical-input behavior.
        No local/shared paths are opened and no scientific input is rewritten.
        """
        # Check the complete request before crossing the service boundary.
        # Once POST begins, an unusable response cannot prove rejection.
        event_id = _event_id(event_id)
        configuration = _basename(configuration, "configuration")
        if type(overwrite) is not bool:
            raise ValueError("overwrite must be a boolean")
        if not isinstance(files, dict):
            raise ValueError("files must map native basenames to bytes")

        for name, content in files.items():
            _basename(name, "filename")
            # Quoted multipart filename headers must never accept escaping or
            # header syntax supplied by the caller. Native exporter names need none.
            if '"' in name or "\\" in name or not isinstance(content, bytes):
                raise ValueError(
                    "files require bytes and basenames without quotes/backslashes"
                )

        # Encode the caller's selections as ordinary form fields. Native files
        # remain bytes throughout multipart assembly, including empty files.
        fields = {
            "event_id": event_id.encode(),
            "configuration": configuration.encode(),
            "overwrite": b"true" if overwrite else b"false",
        }

        # The separator must not occur inside any field or file payload.
        boundary = "pyfinder-" + uuid4().hex
        while any(
            boundary.encode() in value
            for value in [*fields.values(), *files.values()]
        ):
            boundary = "pyfinder-" + uuid4().hex

        parts = []
        for name, value in fields.items():
            parts.append(
                (
                    f'--{boundary}\r\n'
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                ).encode()
                + value
                + b"\r\n"
            )

        for name, value in files.items():
            parts.append(
                (
                    f'--{boundary}\r\n'
                    f'Content-Disposition: form-data; name="files"; filename="{name}"\r\n'
                    'Content-Type: application/octet-stream\r\n\r\n'
                ).encode()
                + value
                + b"\r\n"
            )

        body = b"".join(parts) + f"--{boundary}--\r\n".encode()

        ack = self._request(
            "POST",
            "/events",
            body=body,
            content_type=f"multipart/form-data; boundary={boundary}",
            event_id=event_id,
        )

        # HTTP 202 alone is not enough to resume monitoring safely. Confirm the
        # accepted identity and caller selections before returning a job handle.
        try:
            job = AcceptedJob(ack.get("event_id"), ack.get("internal_sequence"))
            _state(ack)
            if (
                job.event_id != event_id
                or ack.get("status") != "QUEUED"
                or ack.get("requested_configuration") != configuration
                or ack.get("overwrite") is not overwrite
            ):
                raise ValueError(
                    "Acknowledged request differs from the submitted request"
                )
        except (ValueError, ShakeMapProtocolError) as error:
            raise ShakeMapSubmissionUncertain(
                "ShakeMap acknowledgement cannot identify the accepted request safely",
                event_id=event_id,
                status_code=202,
                body=ack,
            ) from error

        return job, ack

    def poll(self, job):
        """Read once and select only this accepted sequence, including archives."""
        detail = self._request("GET", "/events/" + quote(job.event_id, safe=""))
        if detail.get("event_id") != job.event_id:
            raise ShakeMapProtocolError("Event detail returned a different event_id")

        # Reusing an event_id creates another calculation. Match the saved
        # sequence across both views; a newer successful job is not this job.
        matches = []
        for scope in ("jobs", "archives"):
            rows = detail.get(scope)
            if not isinstance(rows, list) or not all(
                isinstance(row, dict) for row in rows
            ):
                raise ShakeMapProtocolError(
                    "Event detail has malformed job/archive rows"
                )
            for row in rows:
                sequence = row.get("internal_sequence")
                if type(sequence) is int and sequence == job.internal_sequence:
                    matches.append((scope, row))

        if not matches:
            raise ShakeMapJobUnavailable(
                "Accepted sequence is absent from current and retained records"
            )
        if len(matches) != 1:
            raise ShakeMapProtocolError("Accepted sequence appears more than once")

        scope, row = matches[0]
        _state(row, archive=scope == "archives")
        return JobStatus(job, scope, row)

    def current_products(self, job):
        """Return current manifest information only if it belongs to this job.

        The service has no archived-product inventory or download endpoint.
        Use poll() for archived shared-path references. A replacement can happen
        after any read; these references are not a reservation of product bytes.
        """
        result = self._request(
            "GET",
            "/events/" + quote(job.event_id, safe="") + "/products",
        )
        if result.get("event_id") != job.event_id or "current" not in result:
            raise ShakeMapProtocolError(
                "Product response has the wrong event identity or shape"
            )

        # This endpoint describes the current calculation only. Do not return
        # a replacement calculation's products under an older accepted handle.
        current = result["current"]
        if current is None:
            raise ShakeMapJobUnavailable("No current materialized calculation")
        if (
            not isinstance(current, dict)
            or type(current.get("internal_sequence")) is not int
        ):
            raise ShakeMapProtocolError(
                "Product response has no valid current sequence"
            )
        if current["internal_sequence"] != job.internal_sequence:
            raise ShakeMapJobUnavailable(
                "Current products belong to a different accepted sequence"
            )

        _state(current, product_summary=True)
        return result

    def log_information(self, job):
        """Return scoped log references from status; the API does not serve bytes."""
        status = self.poll(job)
        paths = status.details.get("shared_paths")
        if not isinstance(paths, dict):
            raise ShakeMapProtocolError("Job has no shared_paths object")

        return {
            "event_id": job.event_id,
            "internal_sequence": job.internal_sequence,
            "scope": status.scope,
            "service_log": paths.get("service_log"),
            "shake_log": paths.get("shake_log"),
        }
