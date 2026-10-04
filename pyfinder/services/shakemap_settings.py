"""Apply the small continuous-process ShakeMap environment interface.

These are deployment settings for the existing REST adapter in continuous and
playback operation. They do not select scientific profiles by geography.
"""

from copy import deepcopy
import os

from pyfinder.runtime import RuntimeBootstrapError
from pyfinder.services.shakemap_client import ShakeMapClient
from pyfinder.services.shakemap_inputs import PreparedShakeMapInputs


# Keep the whitelist explicit. Unrelated process settings, including secrets,
# must never become application configuration through a generic prefix loader.
ENVIRONMENT_SETTINGS = {
    "PYFINDER_SHAKEMAP_ENABLED": "service-enabled",
    "PYFINDER_SHAKEMAP_URL": "service-url",
    "PYFINDER_SHAKEMAP_INPUT_DIRECTORY": "input-directory",
    "PYFINDER_SHAKEMAP_CONFIGURATION": "configuration",
    "PYFINDER_SHAKEMAP_REQUEST_TIMEOUT_SECONDS": "request-timeout-seconds",
    "PYFINDER_SHAKEMAP_OVERWRITE": "overwrite",
}


def continuous_shakemap_configuration(configuration, *, environment=None):
    """Copy, override and validate settings without persistence or HTTP calls.

    Absent variables preserve packaged defaults. Explicit mistakes are rejected
    even while disabled; missing endpoint and input root are required only when
    enabled. Never include supplied values in startup errors, since a malformed
    URL may contain credentials that the adapter will refuse.
    """
    if environment is None:
        environment = os.environ

    configured = deepcopy(configuration)
    settings = configured.setdefault("shakemap", {})
    for variable, key in ENVIRONMENT_SETTINGS.items():
        if variable not in environment:
            continue

        value = environment[variable]
        if key in {"service-enabled", "overwrite"}:
            if value not in {"true", "false"}:
                raise RuntimeBootstrapError(
                    f"{variable} must be exactly true or false"
                )
            value = value == "true"
        elif key == "request-timeout-seconds":
            try:
                value = float(value)
            except (TypeError, ValueError):
                raise RuntimeBootstrapError(
                    f"{variable} must be a positive finite number of seconds"
                ) from None
        settings[key] = value

    def validate(variable, operation, requirement):
        try:
            operation()
        except (TypeError, ValueError, OSError):
            raise RuntimeBootstrapError(f"{variable}: {requirement}") from None

    enabled = settings.get("service-enabled", False)
    if type(enabled) is not bool:
        raise RuntimeBootstrapError("PYFINDER_SHAKEMAP_ENABLED must be a boolean")

    # Reuse the adapter's operational validation instead of maintaining a second
    # URL, timeout or configuration-name grammar here. Construction sends no HTTP.
    validate(
        "PYFINDER_SHAKEMAP_REQUEST_TIMEOUT_SECONDS",
        lambda: ShakeMapClient(
            "http://validation.invalid",
            timeout=settings.get("request-timeout-seconds", 30.0),
        ),
        "supply a positive finite number of seconds",
    )
    validate(
        "PYFINDER_SHAKEMAP_CONFIGURATION / PYFINDER_SHAKEMAP_OVERWRITE",
        lambda: ShakeMapClient.validate_submission(
            "configuration-check", {},
            configuration=settings.get("configuration", "global"),
            overwrite=settings.get("overwrite", True),
        ),
        "supply a safe configuration name and boolean overwrite setting",
    )
    if enabled or "PYFINDER_SHAKEMAP_URL" in environment:
        validate(
            "PYFINDER_SHAKEMAP_URL",
            lambda: ShakeMapClient(settings.get("service-url")),
            "supply an absolute HTTP(S) URL without credentials, query or fragment",
        )
    if enabled or "PYFINDER_SHAKEMAP_INPUT_DIRECTORY" in environment:
        validate(
            "PYFINDER_SHAKEMAP_INPUT_DIRECTORY",
            lambda: PreparedShakeMapInputs(settings.get("input-directory")),
            "supply an existing absolute resolved directory shared with the service",
        )

    return configured


def check_configuration(configuration, *, environment=None, input_directory=None):
    """Describe caller configuration without starting its operational runtime.

    Deployment can supply the host path corresponding to its known shared
    input mount. This substitutes only the location inspected by the existing
    validator; it does not change submitted configuration or application state.
    No service, provider, database, logger or SMTP connection is constructed.
    """
    from pyfinder.services.shakemap_diagnostics import regional_fallback_policy

    checked_environment = dict(os.environ if environment is None else environment)
    if input_directory is not None:
        checked_environment["PYFINDER_SHAKEMAP_INPUT_DIRECTORY"] = str(input_directory)

    report = {
        "status": "blocked",
        "scope": "caller configuration; no installed-image or native proof",
        "checks": [],
        "settings": {},
        "fallback": regional_fallback_policy(),
    }
    try:
        configured = continuous_shakemap_configuration(
            configuration, environment=checked_environment,
        )
    except RuntimeBootstrapError as error:
        # Validation errors identify documented setting names and requirements,
        # never supplied URLs, credentials or arbitrary configuration contents.
        report["checks"].append({
            "name": "caller_settings",
            "status": "blocked",
            "reason": str(error),
        })
        return report

    settings = configured["shakemap"]
    report["settings"] = {
        "enabled": settings.get("service-enabled", False),
        "selected_configuration": settings.get("configuration", "global"),
        "overwrite": settings.get("overwrite", True),
        "input_directory": settings.get("input-directory"),
        "request_timeout_seconds": settings.get("request-timeout-seconds", 30.0),
    }
    enabled = report["settings"]["enabled"]
    report["status"] = "ready" if enabled else "blocked"
    report["checks"].append({
        "name": "caller_settings",
        "status": report["status"],
        "reason": (
            "Caller settings pass static validation; native execution is unverified"
            if enabled else
            "ShakeMap integration is disabled; the required full chain is unavailable"
        ),
    })
    return report


def main(argv=None):
    """Print read-only caller facts for deployment aggregation or direct use."""
    import argparse
    import json

    parser = argparse.ArgumentParser(
        description=(
            "Inspect caller settings without bootstrap, Docker, network, "
            "database writes, native calculations or email."
        ),
    )
    parser.add_argument("--check", action="store_true", required=True)
    parser.add_argument(
        "--input-directory",
        help="Host path for the deployment's known shared input mount",
    )
    arguments = parser.parse_args(argv)

    from pyfinder.pyfinderconfig import pyfinderconfig

    report = check_configuration(
        pyfinderconfig, input_directory=arguments.input_directory,
    )
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
