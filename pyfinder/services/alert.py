"""Terminal email rendering and finite SMTP delivery, separate from science.

Only this boundary sees SMTP credentials. Callers pass a safe diagnostic
projection and immutable attachments; neither scientific configuration nor
application logs should contain the settings object.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import make_msgid
import html
import json
import math
import os
from pathlib import Path
import smtplib
import ssl


@dataclass(frozen=True)
class AlertSettings:
    host: str
    port: int
    sender: str
    lists: list[dict] = field(repr=False)
    subject: str | None = None
    security: str = "starttls"
    timeout: float = 30.0
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)


def load_alert_settings(path=None):
    """An absent explicit path disables delivery; invalid files fail startup.

    Never include supplied values or parser excerpts in an error. The JSON is
    operator-owned and deliberately separate from the FinDer configuration.
    """
    if path is None:
        return None

    try:
        supplied = Path(path)
        if not supplied.is_absolute():
            raise ValueError()
        data = json.loads(supplied.read_text(encoding="utf-8"))
        # Keep the established operator file and recipient fields. Optional
        # named lists extend that file; operators need not migrate secrets to
        # a second configuration format merely to enable terminal delivery.
        legacy_keys = {"smtp_server", "smtp_port", "from", "to", "password", "address",
                       "subject", "alert_lists", "security", "timeout"}
        if isinstance(data, dict) and "smtp_server" in data:
            if set(data) - legacy_keys:
                raise ValueError()
            recipients = data.get("to")
            if isinstance(recipients, str):
                recipients = [recipients]
            data = {
                "host": data.get("smtp_server"), "port": data.get("smtp_port"),
                "sender": data.get("from"), "subject": data.get("subject"),
                "username": data.get("from"), "password": data.get("password"),
                "security": data.get("security", "starttls"),
                "timeout": data.get("timeout", 30),
                "lists": data.get("alert_lists", [{
                    "name": "default", "recipients": recipients,
                    "outcomes": ["SUCCESS", "FAILED"], "required_attachments": [],
                }]),
            }
        allowed = {"host", "port", "sender", "lists", "security", "timeout", "username", "password", "subject"}
        if not isinstance(data, dict) or set(data) - allowed:
            raise ValueError()
        settings = AlertSettings(**data)
        if not isinstance(settings.host, str) or not settings.host or any(c.isspace() or c in "@/\\" for c in settings.host):
            raise ValueError()
        if type(settings.port) is not int or not 1 <= settings.port <= 65535:
            raise ValueError()
        if settings.security not in {"starttls", "tls"}:
            raise ValueError()
        if isinstance(settings.timeout, bool) or not math.isfinite(settings.timeout) or not 0 < settings.timeout <= 300:
            raise ValueError()
        if settings.subject is not None and (
            not isinstance(settings.subject, str) or any(c in settings.subject for c in "\r\n")
        ):
            raise ValueError()
        if bool(settings.username) != bool(settings.password) or any(
            value is not None and not isinstance(value, str)
            for value in (settings.username, settings.password)
        ):
            raise ValueError()
        if not isinstance(settings.lists, list) or not settings.lists:
            raise ValueError()
        names = set()
        addresses = [settings.sender]
        for audience in settings.lists:
            if set(audience) != {"name", "recipients", "outcomes", "required_attachments"}:
                raise ValueError()
            name = audience["name"]
            if not isinstance(name, str) or not name or name in names:
                raise ValueError()
            names.add(name)
            if not audience["recipients"] or not isinstance(audience["recipients"], list):
                raise ValueError()
            addresses.extend(audience["recipients"])
            if not isinstance(audience["outcomes"], list) or not audience["outcomes"] or not set(audience["outcomes"]) <= {"SUCCESS", "FAILED", "INTERRUPTED", "UNKNOWN"}:
                raise ValueError()
            if not isinstance(audience["required_attachments"], list) or not all(
                isinstance(label, str) and label for label in audience["required_attachments"]
            ):
                raise ValueError()
        for address in addresses:
            if not isinstance(address, str) or address.count("@") != 1 or any(
                c.isspace() or c in "<>,;" for c in address
            ):
                raise ValueError()
        return settings
    except (OSError, ValueError, TypeError, KeyError):
        raise ValueError("Alert configuration is invalid; check the documented JSON settings") from None


def render_message(settings, diagnostic, attachments, *, message_id=None):
    """Build readable text and HTML from the shared diagnostic projection.

    Workflow outcome and native outcome are distinct. Observation timestamps
    describe monitoring, never the earthquake origin. Unknown fields remain in
    the attached machine report instead of leaking into the human-facing body.
    """
    event_id = str(diagnostic.get("event_id") or "Unavailable")
    outcome = str(diagnostic.get("final_outcome") or diagnostic.get("status") or "UNKNOWN")
    if any(character in event_id + outcome for character in "\r\n"):
        raise ValueError("Alert header identity contains a newline")

    def known(value):
        return "Unavailable" if value is None or value == "" else str(value)

    def utc_time(value):
        if value is None:
            return "Unavailable"
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return "Unavailable (source timezone unspecified)"
            return parsed.astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError):
            return "Unavailable (invalid source timestamp)"

    def native_execution(value):
        if not isinstance(value, dict):
            return "No native execution evidence"
        signal = "None reported" if "signal" in value and value["signal"] is None else known(value.get("signal"))
        return (f"Started: {known(value.get('started'))}; "
                f"exit code: {known(value.get('exit_code'))}; signal: {signal}")

    requested = diagnostic.get("initial_configuration") or diagnostic.get("requested_configuration")
    overview = [
        ("Calculation", event_id),
        ("Final workflow outcome", outcome),
        ("ShakeMap job outcome", known(diagnostic.get("status"))),
        ("Native execution", native_execution(diagnostic.get("native_outcome"))),
        ("Requested configuration", known(requested)),
        ("Service-selected configuration", known(diagnostic.get("selected_configuration"))),
        ("Configuration materialized", known(diagnostic.get("materialized"))),
        ("Native sequence", known(diagnostic.get("internal_sequence"))),
        ("Observed at (UTC)", utc_time(diagnostic.get("observed_at"))),
    ]
    if diagnostic.get("fallback_reason"):
        overview.append(("Reason for global fallback", diagnostic["fallback_reason"]))
    for key, label in (("phase", "Stage"), ("reason", "Reason"), ("action", "Operator action"),
                       ("local_error", "Caller diagnostic")):
        if diagnostic.get(key):
            overview.append((label, str(diagnostic[key])))

    sections = [("Calculation outcome", overview)]
    context = diagnostic.get("event_context") or {}
    earthquake = []
    if "origin_time_epoch" in diagnostic:
        earthquake.append(("Earthquake origin (UTC)", datetime.fromtimestamp(
            diagnostic["origin_time_epoch"], timezone.utc,
        ).isoformat()))
    elif context.get("origin_time") is not None:
        earthquake.append(("Earthquake origin (UTC)", utc_time(context["origin_time"])))
    for key, label in (("latitude", "Latitude"), ("longitude", "Longitude"),
                       ("depth", "Depth (km)"), ("magnitude", "Magnitude"),
                       ("magnitude_type", "Magnitude type"), ("region", "Region"),
                       ("current_delay", "Scheduled delay (minutes)"),
                       ("ESM_status", "ESM acquisition"), ("RRSM_status", "RRSM acquisition")):
        if context.get(key) is not None:
            earthquake.append((label, str(context[key])))
    sections.append(("Authoritative earthquake and scheduled update",
                     earthquake or [("Summary", "Unavailable for this attempt")]))

    selected = diagnostic.get("finder_solution")
    if selected is None:
        sections.append(("Selected FinDer solution", [("Summary", "Unavailable for this attempt")]))
    else:
        sections.append(("Selected FinDer solution", [
            ("Source", "Selected FinDer solution returned by the manager"),
            ("Physical origin (authoritative earthquake, UTC)", utc_time(selected.get("physical_origin_time"))),
            ("Latitude", known(selected.get("latitude"))),
            ("Longitude", known(selected.get("longitude"))),
            ("Depth (km)", known(selected.get("depth"))),
            ("Magnitude", known(selected.get("magnitude"))),
        ]))

    attempts = []
    for index, attempt in enumerate(diagnostic.get("attempts") or []):
        description = (
            f"requested {known(attempt.get('requested_configuration'))}; "
            f"selected {known(attempt.get('selected_configuration'))}; "
            f"materialized {known(attempt.get('materialized'))}; "
            f"job {known(attempt.get('status'))}; sequence {known(attempt.get('internal_sequence'))}; "
            + native_execution(attempt.get("native_outcome"))
        )
        if attempt.get("reason"):
            description += "; " + str(attempt["reason"])
        attempts.append((f"Attempt {index + 1}", description))
    if attempts:
        sections.append(("Calculation attempts", attempts))

    evidence = [("Attached", ", ".join(attachments) or "No native/input attachments available")]
    for index, bundle in enumerate(diagnostic.get("evidence") or []):
        if bundle.get("unavailable"):
            evidence.append((f"Evidence bundle {index + 1} unavailable", ", ".join(bundle["unavailable"])))
    sections.append(("Evidence", evidence))

    plain_sections = ["PyFinder terminal calculation report"]
    html_sections = ["<html><body><h1>PyFinder terminal calculation report</h1>"]
    for title, entries in sections:
        plain_sections.append(title + "\n" + "\n".join(f"{label}: {value}" for label, value in entries))
        html_sections.append("<h2>" + html.escape(title) + "</h2><dl>")
        for label, value in entries:
            html_sections.append("<dt>" + html.escape(label) + "</dt><dd>" + html.escape(str(value)) + "</dd>")
        html_sections.append("</dl>")
    footer = "Do not reply to this email. Contact your EEW support group."
    plain_sections.append(footer)
    html_sections.append("<p>" + footer + "</p></body></html>")

    message = EmailMessage()
    message["From"] = settings.sender
    message["To"] = settings.sender
    message["Subject"] = settings.subject or f"PyFinder {event_id}: {outcome}"
    message["Message-ID"] = message_id or make_msgid()
    message.set_content("\n\n".join(plain_sections))
    message.add_alternative("".join(html_sections), subtype="html")
    for name, contents in attachments.items():
        message.add_attachment(contents, maintype="application", subtype="octet-stream", filename=name)
    message.add_attachment(json.dumps(diagnostic, indent=2).encode(), maintype="application",
                           subtype="json", filename="calculation-report.json")
    return message


def deliver_message(settings, recipients, message, *, smtp_factory=None):
    """Classify definite rejection separately from unknown completion.

    Once DATA may have reached the server, disconnect/timeout does not prove
    non-delivery. The durable caller retains UNKNOWN and never auto-replays it.
    Recipient addresses and server response text never enter returned diagnostics.
    """
    client = None
    sending = False
    try:
        factory = smtp_factory or (smtplib.SMTP_SSL if settings.security == "tls" else smtplib.SMTP)
        options = {"timeout": settings.timeout}
        if settings.security == "tls":
            options["context"] = ssl.create_default_context()
        client = factory(settings.host, settings.port, **options)
        if settings.security == "starttls":
            client.starttls(context=ssl.create_default_context())
        if settings.username:
            client.login(settings.username, settings.password)

        sending = True
        refused = client.send_message(message, from_addr=settings.sender, to_addrs=list(recipients))
        return {"state": "PARTIAL" if refused else "SENT", "refused_count": len(refused)}
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError):
        return {"state": "FAILED", "reason": "SMTP explicitly rejected delivery"}
    except (OSError, smtplib.SMTPException):
        return {"state": "UNKNOWN" if sending else "FAILED", "reason": "SMTP completion unknown" if sending else "SMTP connection or authentication failed"}
    finally:
        if client is not None:
            # QUIT failure after an acknowledged send cannot undo acceptance.
            try:
                client.close()
            except (OSError, smtplib.SMTPException):
                pass


def configured_alert_settings(*, environment=None):
    """Resolve the existing private config locations, with an optional override.

    An empty override explicitly disables mail, including when a legacy config
    is installed. This is useful for confined verification. No discovery occurs
    at module import, and no file contents are included in diagnostics.
    """
    environment = os.environ if environment is None else environment
    if "PYFINDER_ALERT_CONFIG" in environment:
        explicit = environment["PYFINDER_ALERT_CONFIG"]
        return load_alert_settings(explicit) if explicit else None
    module = Path(__file__).resolve().parent
    for directory in (module, module.parent):
        candidate = directory / ".pyfinder_alert_config.json"
        if candidate.exists():
            return load_alert_settings(candidate)
    return None
