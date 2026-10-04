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
