"""Safe, shared descriptions of exact ShakeMap attempts and recovery decisions.

This module formats retained facts. It does not query services, read evidence,
log messages itself, or choose scientific profiles from an earthquake location.
"""

import re


REGIONAL_MATERIALIZATION_FAILURE = "configuration_materialization_failed"
NATIVE_CONFIGURATION_FAILURE = "native_configuration_failed"
NATIVE_CONFIGURATION_ORIGINS = frozenset({
    "native_config_validation", "native_model_reference", "native_configured_module",
})


def regional_fallback_policy():
    """Expose policy for diagnostics without predicting a native outcome.

    The runtime decision below still requires exact accepted sequence and
    provenance evidence. Listing a missing profile never authorizes skipping
    its first submission or guarantees that a later global calculation works.
    """
    return {
        "requested_configuration_first": True,
        "requires_confirmed_regional_failure": True,
        "eligible_failure_codes": [
            REGIONAL_MATERIALIZATION_FAILURE, NATIVE_CONFIGURATION_FAILURE,
        ],
        "requires_matching_accepted_sequence_and_configuration": True,
        "global_success": "unverified",
        "explanation": (
            "The requested region is submitted first. Only its confirmed eligible "
            "configuration failure permits one explicit global submission. "
            "Transport errors, uncertain acceptance, observation errors and "
            "unrelated native failures do not permit fallback."
        ),
    }


def safe_text(value, limit=600):
    """Bound external text and remove credentials, controls and private paths."""
    if not isinstance(value, str):
        return ""
    value = "".join(character if character.isprintable() else " " for character in value)
    value = re.sub(r"https?://\S+", "<service-url>", value)
    value = re.sub(r"(?i)(password|passwd|token|secret|authorization)\s*[:=]\s*\S+", r"\1=<redacted>", value)
    value = re.sub(r"(?<!\w)/(?:[^\s:;,]+/?)+", "<path>", value)
    return " ".join(value.split())[:limit]


def error_diagnostic(error, *, operation):
    """Keep useful public HTTP reasons without persisting arbitrary response bodies."""
    kind = type(error).__name__
    status = getattr(error, "status_code", None)
    pieces = [kind]
    if type(status) is int:
        pieces.append(f"HTTP {status}")
    body = getattr(error, "body", None)
    if isinstance(body, dict) and body.get("error") in {
        "request_rejected", "service_unavailable", "service_failure",
    }:
        message = safe_text(body.get("message"))
        if message:
            pieces.append(message)
    if kind in {"ShakeMapSubmissionUncertain", "ShakeMapRecordingError"}:
        action = "Acceptance requires reconciliation; retain this attempt and do not resubmit"
    elif operation == "observation":
        action = "Observe the same accepted sequence again; remote outcome is not changed"
    elif status == 503:
        action = "Inspect ShakeMap health and deployment readiness"
    elif type(status) is int and status < 500:
        action = "Correct the rejected request or shared inputs before the scheduled retry"
    else:
        action = "Inspect the retained attempt and service diagnostics"
    return "; ".join(pieces + [action])


def diagnostic(record):
    """Project an immutable-by-value operator/notification snapshot from a record."""
    request = record["request"]
    observation = record.get("observation") or {}
    details = observation.get("details") or {}
    provenance = details.get("provenance") or {}
    configuration = provenance.get("configuration") or {}
    materialization = configuration.get("materialization") or {}
    failure = details.get("failure") or provenance.get("failure") or {}
    selected = (details.get("configuration") or {}).get("selected", configuration.get("selected"))
    reason = safe_text(failure.get("message")) or safe_text(record.get("last_error"))
    state = record["submission_state"]
    status = details.get("status")
    if state in {"SUBMITTING", "UNCERTAIN"}:
        action = "Reconcile this attempt; do not repeat its POST or guess an accepted sequence"
    elif record.get("last_error"):
        action = "Inspect the retained attempt; communication errors are not native failure"
    elif status == "FAILED":
        action = "Inspect this sequence's retained native/service logs and selected configuration"
    elif status == "SUCCESS" and not details.get("products_ready"):
        action = "Native success is retained, but required products are unavailable"
    else:
        action = "Observe this exact sequence" if status not in {"SUCCESS", "FAILED"} else "Review retained calculation evidence"
    return {
        "attempt_id": record["attempt_id"], "event_id": request["event_id"],
        "internal_sequence": record.get("internal_sequence"),
        "requested_configuration": request["configuration"],
        "selected_configuration": selected, "materialized": materialization.get("materialized"),
        "submission_state": state, "status": status,
        "phase": failure.get("phase", details.get("phase")),
        "failure_code": failure.get("code"), "reason": reason, "action": action,
        "products_ready": details.get("products_ready"), "scope": observation.get("scope"),
        "observed_at": record.get("observed_at"),
        "native_outcome": dict(details["native_outcome"]) if details.get("native_outcome") else None,
    }


def format_diagnostic(value):
    """Keep identifiers beside the reason without dumping records or settings."""
    return (
        f"ShakeMap attempt={value['attempt_id']} calculation={value['event_id']} "
        f"sequence={value['internal_sequence']} requested={value['requested_configuration']} "
        f"selected={value['selected_configuration']} state={value['submission_state']} "
        f"native={value['status']} phase={value['phase']}: "
        f"{value['reason'] or value['action']}"
    )


def regional_configuration_failure(record):
    """Require affirmative service evidence, never infer from a generic exit code."""
    if record["submission_state"] != "ACCEPTED" or record.get("last_error"):
        return False
    request = record["request"]
    if request["configuration"] == "global":
        return False
    details = (record.get("observation") or {}).get("details") or {}
    if (details.get("status") != "FAILED"
            or details.get("internal_sequence") != record.get("internal_sequence")):
        return False
    provenance = details.get("provenance") or {}
    if (provenance.get("event_id") != request["event_id"]
            or provenance.get("internal_sequence") != record.get("internal_sequence")):
        return False
    configuration = provenance.get("configuration") or {}
    materialization = configuration.get("materialization") or {}
    if (configuration.get("selected") != request["configuration"]
            or materialization.get("selected_configuration") != request["configuration"]):
        return False
    failure = details.get("failure") or {}
    profile_failure = materialization.get("failure") or {}
    # This existing typed stage specifically means a selected regional source
    # file is missing/unreadable. Copy/space errors and generic native exits do
    # not prove a regional configuration fault and cannot authorize replacement.
    native_configuration_error = failure.get("configuration_error")
    if (failure.get("code") == NATIVE_CONFIGURATION_FAILURE
            and isinstance(native_configuration_error, dict)
            and native_configuration_error.get("origin") in NATIVE_CONFIGURATION_ORIGINS
            and isinstance(native_configuration_error.get("reference"), str)
            and native_configuration_error.get("exception_type")):
        return True
    return (
        failure.get("code") == REGIONAL_MATERIALIZATION_FAILURE
        and materialization.get("materialized") is False
        and profile_failure.get("type") == "NativeProfileError"
        and profile_failure.get("stage") == "regional_sources"
    )


def local_failure_diagnostic(execution_id, event_id, service, reason):
    """Describe a caller failure without manufacturing a native FAILED outcome."""
    return {
        "attempt_id": execution_id, "event_id": event_id, "service": service,
        "internal_sequence": None, "requested_configuration": None,
        "selected_configuration": None, "status": None, "final_outcome": "FAILED",
        "phase": "caller_execution", "reason": safe_text(reason),
        "action": "Inspect retained caller logs and execution input evidence",
    }
