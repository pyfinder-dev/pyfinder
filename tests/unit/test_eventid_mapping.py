"""Offline EventID checks for association selection and observable failures."""

from io import BytesIO
from http.client import IncompleteRead
import json
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from pyfinder.services.eventid_mapping import EventIDMapper, EventIDMappingError


class TestEventIDMapping(unittest.TestCase):
    """Exercise lookup outcomes without contacting EventID or any product service."""

    def response(self, payload, *, status=200):
        """Provide a context-managed HTTP response with controlled bytes."""
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = status
        response.read.return_value = (
            payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        )
        return response

    def association(self, **changes):
        """Use a non-us prefix to keep agency IDs independent of local naming rules."""
        row = {"catalog": "USGS", "eventid": "ci38457511", "misfit": 0.07}
        row.update(changes)
        return row

    def test_preferred_mapping_preserves_identity_and_service_evidence(self):
        row = self.association(
            url="https://unused.invalid/event", m2=3, delta_time=1.7,
            delta_loc=8.4, delta_mag=0.0, unrelated="not retained",
        )
        with patch(
            "pyfinder.services.eventid_mapping.urlopen",
            return_value=self.response([row]),
        ) as open_request:
            result = EventIDMapper(timeout=12).resolve("20230206_0000008")

        self.assertEqual(result.source_id, "20230206_0000008")
        self.assertEqual(result.usgs_id, "ci38457511")
        self.assertEqual(result.misfit, 0.07)
        self.assertEqual(result.association, {
            "catalog": "USGS", "eventid": "ci38457511", "misfit": 0.07,
            "m2": 3, "delta_time": 1.7, "delta_loc": 8.4, "delta_mag": 0.0,
        })

        # Exactly one request proves the response URL was not fetched. The
        # public query selects the right catalog without altering thresholds.
        open_request.assert_called_once()
        request = open_request.call_args.args[0]
        url = urlsplit(request.full_url)
        self.assertEqual(url.scheme, "https")
        self.assertEqual(url.netloc, "www.seismicportal.eu")
        self.assertEqual(url.path, "/eventid/api/convert")
        self.assertEqual(parse_qs(url.query), {
            "source_id": ["20230206_0000008"], "source_catalog": ["UNID"],
            "out_catalog": ["USGS"], "preferred_only": ["true"],
            "include_info": ["true"],
        })
        self.assertEqual(open_request.call_args.kwargs["timeout"], 12.0)

    def test_empty_and_not_found_are_no_match(self):
        responses = [
            self.response([]),
            HTTPError("https://example.invalid", 404, "Not Found", {}, BytesIO()),
        ]
        for response in responses:
            with self.subTest(response=response):
                with patch("pyfinder.services.eventid_mapping.urlopen") as request:
                    if isinstance(response, Exception):
                        request.side_effect = response
                    else:
                        request.return_value = response
                    with self.assertRaises(EventIDMappingError) as raised:
                        EventIDMapper().resolve("event-1")
                self.assertEqual(raised.exception.reason, "no_match")
                request.assert_called_once()

    def test_invalid_response_is_not_treated_as_no_match(self):
        payloads = [
            b"not JSON", b"\xff", {}, [None],
            [self.association(catalog="EMSC")],
            [self.association(eventid="")],
            [self.association(eventid="ci bad")],
            [self.association(eventid=123)],
            [self.association(misfit=None)],
            [self.association(misfit=True)],
            [self.association(misfit=float("nan"))],
            [self.association(misfit=-1)],
            [self.association(), self.association()],
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                with patch(
                    "pyfinder.services.eventid_mapping.urlopen",
                    return_value=self.response(payload),
                ):
                    with self.assertRaises(EventIDMappingError) as raised:
                        EventIDMapper().resolve("event-1")
                self.assertEqual(raised.exception.reason, "invalid_response")

    def test_multiple_preferred_ids_are_not_ranked_locally(self):
        payload = [self.association(), self.association(eventid="us-other", misfit=0.01)]
        with patch(
            "pyfinder.services.eventid_mapping.urlopen",
            return_value=self.response(payload),
        ):
            with self.assertRaises(EventIDMappingError) as raised:
                EventIDMapper().resolve("event-1")
        self.assertEqual(raised.exception.reason, "ambiguous_response")

    def test_http_failures_retain_status_without_retry(self):
        for status in (422, 500, 503):
            error = HTTPError("https://example.invalid", status, "Failed", {}, BytesIO())
            with self.subTest(status=status):
                with patch(
                    "pyfinder.services.eventid_mapping.urlopen", side_effect=error,
                ) as request:
                    with self.assertRaises(EventIDMappingError) as raised:
                        EventIDMapper().resolve("event-1")
                self.assertEqual(raised.exception.reason, "service_failure")
                self.assertEqual(raised.exception.status_code, status)
                request.assert_called_once()

    def test_timeout_and_transport_failures_remain_distinct(self):
        errors = [
            (TimeoutError(), "timeout"),
            (URLError(TimeoutError()), "timeout"),
            (URLError("connection unavailable"), "transport_failure"),
            (OSError("connection lost"), "transport_failure"),
            (IncompleteRead(b"partial"), "transport_failure"),
        ]
        for error, reason in errors:
            with self.subTest(error=error):
                with patch(
                    "pyfinder.services.eventid_mapping.urlopen", side_effect=error,
                ) as request:
                    with self.assertRaises(EventIDMappingError) as raised:
                        EventIDMapper().resolve("event-1")
                self.assertEqual(raised.exception.reason, reason)
                request.assert_called_once()

    def test_later_attempt_can_obtain_previously_absent_association(self):
        with patch(
            "pyfinder.services.eventid_mapping.urlopen",
            side_effect=[self.response([]), self.response([self.association()])],
        ) as request:
            mapper = EventIDMapper()
            with self.assertRaises(EventIDMappingError):
                mapper.resolve("event-1")
            result = mapper.resolve("event-1")

        self.assertEqual(result.usgs_id, "ci38457511")
        self.assertEqual(request.call_count, 2)

    def test_invalid_timeout_and_source_are_rejected_without_io(self):
        with patch("pyfinder.services.eventid_mapping.urlopen") as request:
            for timeout in (0, -1, True, None, float("inf"), float("nan")):
                with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                    EventIDMapper(timeout=timeout)
            for source_id in (None, "", "  "):
                with self.subTest(source_id=source_id), self.assertRaises(ValueError):
                    EventIDMapper().resolve(source_id)
            request.assert_not_called()
