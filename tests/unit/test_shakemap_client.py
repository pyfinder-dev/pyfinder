"""Offline contract tests for the small ShakeMap HTTP adapter."""

from email import policy
from email.parser import BytesParser
from io import BytesIO
from http.client import IncompleteRead, BadStatusLine
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from pyfinder.services.shakemap_client import (
    AcceptedJob,
    ShakeMapClient,
    ShakeMapHTTPError,
    ShakeMapJobUnavailable,
    ShakeMapProtocolError,
    ShakeMapSubmissionUncertain,
    ShakeMapTransportError,
    _NoRedirects,
    urllib_transport,
)


class FakeTransport:
    """Record each request and return the next prepared response without network I/O."""

    def __init__(self, *responses):
        """Keep response order so tests can detect unintended repeat requests."""
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        """Return HTTP bytes or raise the supplied transport failure."""
        self.calls.append((method, url, headers, body, timeout))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result

        status, value = result
        return status, value if isinstance(value, bytes) else json.dumps(value).encode()


def acknowledgement(**changes):
    """Build a valid admission response, then apply a test's specific changes."""
    result = dict(
        event_id="quake-1",
        internal_sequence=42,
        status="QUEUED",
        job_completed=False,
        products_ready=False,
        requested_configuration="global",
        overwrite=True,
        warnings=["Caller-facing event.xml was replaced"],
    )
    result.update(changes)
    return result


def row(sequence=42, status="RUNNING", **changes):
    """Build a current-job row whose flags agree unless the test overrides them."""
    result = dict(
        internal_sequence=sequence,
        status=status,
        job_completed=status in ("SUCCESS", "FAILED"),
        products_ready=status == "SUCCESS",
        shared_paths={
            "service_log": "/shared/.service/events/quake-1/logs/service.log",
            "shake_log": None,
        },
        failure=None,
    )
    result.update(changes)
    return result


def detail(*jobs, archives=None, event_id="quake-1"):
    """Place job rows in the current and retained views of one public event."""
    return dict(event_id=event_id, jobs=list(jobs), archives=archives or [])


class ShakeMapClientTests(unittest.TestCase):
    def client(self, *responses):
        """Create a client with an inspectable transport for this test scenario."""
        self.transport = FakeTransport(*responses)
        return ShakeMapClient(
            "http://localhost:9010/",
            timeout=3.5,
            transport=self.transport,
        )

    def test_discovery_calls_actual_unversioned_routes(self):
        results = [
            dict(ready=False, reason="not ready"),
            {"readiness": {"ready": True}},
            dict(default="global", configurations=["global", "turkiye"]),
            {"jobs": []},
            {"capacity": {}, "jobs": []},
        ]
        client = self.client(*[(200, item) for item in results])

        self.assertEqual(
            [
                client.health(),
                client.configuration(),
                client.configurations(),
                client.events(),
                client.queue(),
            ],
            results,
        )
        self.assertEqual(
            [call[1] for call in self.transport.calls],
            [
                "http://localhost:9010" + path
                for path in ("/healthz", "/config", "/configurations", "/events", "/queue")
            ],
        )
        self.assertTrue(all(call[4] == 3.5 for call in self.transport.calls))

    def test_rejects_invalid_url_and_timeout_before_io(self):
        for value in (
            None,
            "",
            "localhost:9010",
            "ftp://host",
            "http://",
            "http://host:0",
            "http://user:secret@host",
            "http://host?x=1",
            "http://host#x",
            "http://host:bad",
            "http://host:99999",
            "http://host/\n",
        ):
            with self.subTest(url=value), self.assertRaises(ValueError):
                ShakeMapClient(value)

        for value in (None, True, "5", 0, -1, float("inf"), float("nan")):
            with self.subTest(timeout=value), self.assertRaises(ValueError):
                ShakeMapClient("https://host", timeout=value)

    def test_multipart_preserves_each_native_file_byte(self):
        client = self.client((202, acknowledgement()))
        # Include non-text bytes and an empty file: multipart assembly must not
        # reinterpret native file contents as strings or omit empty payloads.
        files = {
            "event.xml": b"<event>\x00\xff\r\n</event>",
            "rupture.json": b'{"x": 1}\n',
            "stationlist.xml": b"",
        }

        job, ack = client.submit("quake-1", files)

        self.assertEqual(job, AcceptedJob("quake-1", 42))
        self.assertEqual(ack["warnings"], ["Caller-facing event.xml was replaced"])

        method, url, headers, body, _ = self.transport.calls[0]

        self.assertEqual((method, url), ("POST", "http://localhost:9010/events"))
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: "
            + headers["Content-Type"].encode()
            + b"\r\nMIME-Version: 1.0\r\n\r\n"
            + body
        )
        parts = list(message.iter_parts())

        self.assertEqual(
            [p.get_param("name", header="content-disposition") for p in parts],
            ["event_id", "configuration", "overwrite", "files", "files", "files"],
        )
        self.assertEqual(
            [p.get_payload(decode=True) for p in parts[:3]],
            [b"quake-1", b"global", b"true"],
        )
        self.assertEqual(
            {p.get_filename(): p.get_payload(decode=True) for p in parts[3:]},
            files,
        )

    def test_caller_configuration_and_overwrite_are_sent_verbatim(self):
        client = self.client(
            (202, acknowledgement(requested_configuration="turkiye", overwrite=False))
        )
        job, ack = client.submit(
            "quake-1", {}, configuration="turkiye", overwrite=False
        )

        self.assertIn(b"\r\n\r\nturkiye\r\n", self.transport.calls[0][3])
        self.assertIn(b"\r\n\r\nfalse\r\n", self.transport.calls[0][3])
        self.assertFalse(ack["overwrite"])
        self.assertEqual(job.internal_sequence, 42)

    def test_invalid_submission_never_uses_transport(self):
        client = self.client()
        requests = [
            dict(event_id=value, files={})
            for value in ("", ".", "..", "a/b", "a\nb", "-option", "a" * 232, True)
        ]
        requests += [
            dict(event_id="quake-1", files={name: b"x"})
            for name in ("a/b", "..", "bad\r\nX: y", 'bad"name', "bad\\name")
        ]
        requests += [
            dict(event_id="quake-1", files={"event.xml": "text"}),
            dict(event_id="quake-1", files={}, overwrite="false"),
            dict(event_id="quake-1", files={}, configuration="../global"),
        ]

        for request in requests:
            with self.subTest(request=request), self.assertRaises(ValueError):
                client.submit(**request)

        self.assertEqual(self.transport.calls, [])

    def test_transport_failure_on_submit_is_uncertain_and_never_retried(self):
        for error in (TimeoutError("timed out"), URLError("offline"), OSError("reset")):
            client = self.client(error)
            with self.assertRaises(ShakeMapSubmissionUncertain) as caught:
                client.submit("quake-1", {})

            self.assertEqual(caught.exception.event_id, "quake-1")
            self.assertEqual(len(self.transport.calls), 1)

    def test_interrupted_response_is_uncertain_only_for_submission(self):
        for error in (IncompleteRead(b'{"event_id":'), BadStatusLine("broken")):
            client = self.client(error)
            with self.assertRaises(ShakeMapSubmissionUncertain):
                client.submit("quake-1", {})

            self.assertEqual(len(self.transport.calls), 1)

            # The same interrupted response has different meaning for a read:
            # no submission occurred, so acceptance uncertainty does not apply.
            client = self.client(error)
            with self.assertRaises(ShakeMapTransportError):
                client.health()

    def test_unusable_http_status_is_uncertain_for_submission(self):
        for status in (True, "202", None, 999):
            client = self.client((status, acknowledgement()))
            with self.assertRaises(ShakeMapSubmissionUncertain):
                client.submit("quake-1", {})

            self.assertEqual(len(self.transport.calls), 1)

    def test_default_transport_partial_acknowledgement_is_uncertain(self):
        with patch("pyfinder.services.shakemap_client.build_opener") as opener:
            response = opener.return_value.open.return_value.__enter__.return_value
            response.status = 202
            response.read.side_effect = IncompleteRead(b'{"event_id":')
            client = ShakeMapClient("http://host")
            with self.assertRaises(ShakeMapSubmissionUncertain):
                client.submit("quake-1", {})
            opener.return_value.open.assert_called_once()

    def test_read_transport_failure_is_not_submission_uncertainty(self):
        client = self.client(TimeoutError())
        with self.assertRaises(ShakeMapTransportError):
            client.health()

        self.assertEqual(len(self.transport.calls), 1)

    def test_http_rejection_keeps_status_and_json_body(self):
        body = dict(
            error="request_rejected", message="Bad input", details=["event.xml"]
        )
        for status in (400, 404, 503):
            client = self.client((status, body))
            with self.assertRaises(ShakeMapHTTPError) as caught:
                client.submit("quake-1", {})

            self.assertEqual(
                (caught.exception.status_code, caught.exception.body),
                (status, body),
            )
            self.assertEqual(len(self.transport.calls), 1)

    def test_non_json_http_error_keeps_raw_bytes(self):
        client = self.client((502, b"bad gateway"))
        with self.assertRaises(ShakeMapHTTPError) as caught:
            client.health()

        self.assertEqual(caught.exception.body, b"bad gateway")

    def test_server_failure_and_redirect_acknowledgements_are_uncertain(self):
        for status, payload in (
            (500, {"error": "service_failure"}),
            (502, b"gateway"),
            (200, acknowledgement()),
            (302, b"redirect"),
            (202, b"not JSON"),
            (202, []),
        ):
            client = self.client((status, payload))
            with (
                self.subTest(status=status),
                self.assertRaises(ShakeMapSubmissionUncertain) as caught,
            ):
                client.submit("quake-1", {})

            self.assertEqual(caught.exception.status_code, status)
            self.assertEqual(len(self.transport.calls), 1)

    def test_invalid_identity_or_acknowledgement_is_uncertain(self):
        changes = [
            dict(internal_sequence=value)
            for value in (None, True, 0, -1, 42.0, "42")
        ]
        changes += [
            dict(event_id="other"),
            dict(status=[]),
            dict(status={}),
            dict(status="RUNNING"),
            dict(job_completed=True),
            dict(products_ready=True),
            dict(overwrite=1),
            dict(requested_configuration="other"),
        ]

        for change in changes:
            client = self.client((202, acknowledgement(**change)))
            with (
                self.subTest(change=change),
                self.assertRaises(ShakeMapSubmissionUncertain),
            ):
                client.submit("quake-1", {})

            self.assertEqual(len(self.transport.calls), 1)

    def test_poll_selects_exact_sequence_without_event_id_in_row(self):
        target = row(42, "QUEUED")
        client = self.client(
            (200, detail(row(41, "SUCCESS"), target, row(43, "QUEUED")))
        )
        status = client.poll(AcceptedJob("quake-1", 42))

        self.assertEqual(status.scope, "jobs")
        self.assertEqual(status.details, target)
        self.assertFalse(status.completed)
        self.assertFalse(status.products_ready)
        self.assertEqual(len(self.transport.calls), 1)

    def test_success_and_failure_require_authoritative_flags(self):
        for state in ("SUCCESS", "FAILED"):
            client = self.client((200, detail(row(status=state))))
            status = client.poll(AcceptedJob("quake-1", 42))

            self.assertTrue(status.completed)
            self.assertEqual(status.products_ready, state == "SUCCESS")

        # Status labels cannot override contradictory service flags.
        for changes in (
            dict(status="SUCCESS", products_ready=False),
            dict(status="SUCCESS", job_completed=False),
            dict(status="FAILED", products_ready=True),
            dict(status="RUNNING", job_completed=True),
            dict(status="PENDING"),
            dict(status=[]),
            dict(status={}),
            dict(products_ready=0),
        ):
            client = self.client((200, detail(row(**changes))))
            with (
                self.subTest(changes=changes),
                self.assertRaises(ShakeMapProtocolError),
            ):
                client.poll(AcceptedJob("quake-1", 42))

    def test_archive_preserves_success_but_unavailable_products(self):
        # Archive retention can lose product files without changing the
        # historical outcome. Keep those facts separate in the client view.
        archive = row(status="SUCCESS", products_ready=False)
        del archive["job_completed"]
        archive["shared_paths"] = {
            "service_log": "/shared/archive/service.log",
            "products": None,
        }
        client = self.client((200, detail(row(43), archives=[archive])))
        status = client.poll(AcceptedJob("quake-1", 42))

        self.assertEqual(status.scope, "archives")
        self.assertTrue(status.completed)
        self.assertFalse(status.products_ready)
        self.assertEqual(status.details["status"], "SUCCESS")

    def test_discarded_job_is_unavailable_not_newer_job_success(self):
        client = self.client((200, detail(row(43, "SUCCESS"))))
        with self.assertRaises(ShakeMapJobUnavailable):
            client.poll(AcceptedJob("quake-1", 42))

    def test_poll_rejects_wrong_identity_duplicate_and_malformed_rows(self):
        for result in (
            detail(row(), event_id="other"),
            detail(row(), row()),
            {"event_id": "quake-1", "jobs": {}, "archives": []},
        ):
            client = self.client((200, result))
            with self.assertRaises(ShakeMapProtocolError):
                client.poll(AcceptedJob("quake-1", 42))

    def test_encoded_id_remains_one_path_segment(self):
        event_id = "event ?#%2F ü\\x"
        client = self.client((200, detail(row(), event_id=event_id)))
        client.poll(AcceptedJob(event_id, 42))

        self.assertEqual(
            self.transport.calls[0][1],
            "http://localhost:9010/events/event%20%3F%23%252F%20%C3%BC%5Cx",
        )

    def test_current_products_refuse_a_newer_sequence(self):
        client = self.client(
            (200, {"event_id": "quake-1", "current": row(43, "SUCCESS")})
        )
        with self.assertRaises(ShakeMapJobUnavailable):
            client.current_products(AcceptedJob("quake-1", 42))

    def test_current_products_do_not_turn_failed_files_into_success(self):
        current = row(
            status="FAILED",
            products=[{"path": "shake_result.hdf", "size_bytes": 12}],
        )
        del current["job_completed"]
        result = {"event_id": "quake-1", "current": current}
        client = self.client((200, result))

        self.assertEqual(client.current_products(AcceptedJob("quake-1", 42)), result)
        self.assertFalse(result["current"]["products_ready"])

    def test_current_products_reject_missing_and_inconsistent_data(self):
        for current, error in (
            (None, ShakeMapJobUnavailable),
            (row(status="SUCCESS", products_ready=False), ShakeMapProtocolError),
            ({"internal_sequence": True}, ShakeMapProtocolError),
        ):
            client = self.client((200, {"event_id": "quake-1", "current": current}))
            with self.assertRaises(error):
                client.current_products(AcceptedJob("quake-1", 42))

    def test_log_information_uses_event_detail_and_explicit_archive_scope(self):
        archive = row(status="FAILED")
        del archive["job_completed"]
        client = self.client((200, detail(archives=[archive])))
        result = client.log_information(AcceptedJob("quake-1", 42))

        self.assertEqual(result["scope"], "archives")
        self.assertEqual(result["service_log"], archive["shared_paths"]["service_log"])
        self.assertEqual(
            self.transport.calls[0][1], "http://localhost:9010/events/quake-1"
        )

    def test_discovery_rejects_malformed_json_shapes(self):
        for payload in (b"<html>error</html>", [], None):
            client = self.client((200, payload))
            with self.assertRaises(ShakeMapProtocolError):
                client.configuration()

        client = self.client((200, {"ready": "yes"}))
        with self.assertRaises(ShakeMapProtocolError):
            client.health()

        client = self.client(
            (200, {"default": "regional", "configurations": ["regional"]})
        )
        with self.assertRaises(ShakeMapProtocolError):
            client.configurations()

    def test_persisted_identity_requires_actual_positive_integer(self):
        for value in (True, 0, -1, 2.0, "2"):
            with self.assertRaises(ValueError):
                AcceptedJob("quake-1", value)

    def test_urllib_transport_does_not_follow_redirects(self):
        handler = _NoRedirects()
        for status in (301, 302, 303, 307, 308):
            self.assertIsNone(
                handler.redirect_request(None, None, status, "", {}, "https://other")
            )

        response = HTTPError(
            "http://host/events", 302, "redirect", {}, BytesIO(b"redirect")
        )
        with patch("pyfinder.services.shakemap_client.build_opener") as opener:
            opener.return_value.open.side_effect = response
            result = urllib_transport(
                "POST", "http://host/events", {}, b"payload", 1.25
            )

            self.assertEqual(result, (302, b"redirect"))
            opener.return_value.open.assert_called_once()

            self.assertEqual(opener.return_value.open.call_args.kwargs["timeout"], 1.25)


if __name__ == "__main__":
    unittest.main()
