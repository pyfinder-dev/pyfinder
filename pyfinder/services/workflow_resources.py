"""Shared construction of the existing remote workflow and delivery ledger."""


def build_shakemap_boundary(configuration, database_path):
    """Build external resources only for explicitly enabled workflow operation."""
    settings = configuration.get("shakemap", {})
    enabled = settings.get("service-enabled", False)
    if type(enabled) is not bool:
        raise ValueError("shakemap.service-enabled must be a boolean")
    if not enabled:
        return None, None

    from pyfinder.services.shakemap_client import ShakeMapClient
    from pyfinder.services.shakemap_inputs import PreparedShakeMapInputs
    from pyfinder.services.shakemap_workflow import ShakeMapWorkflow

    client = ShakeMapClient(
        settings.get("service-url"),
        timeout=settings.get("request-timeout-seconds", 30.0),
    )

    # Validate settings before the listener is started or a scheduled item is
    # assigned. Configuration is a service name, never a legacy local path or
    # a FinDer profile inferred to be suitable for ShakeMap.
    client.validate_submission(
        "configuration-check", {},
        configuration=settings.get("configuration", "global"),
        overwrite=settings.get("overwrite", True),
    )

    inputs = PreparedShakeMapInputs(settings.get("input-directory"))
    workflow = ShakeMapWorkflow(database_path, client)
    return workflow, inputs


def build_notifier(runtime_context, database_path, inputs, alert_settings, logger):
    """Keep evidence beside the owning database, separate from native products."""
    from pathlib import Path
    from pyfinder.services.alert_delivery import AlertService
    from pyfinder.services.alert_evidence import EvidenceStore

    if inputs is not None:
        service_root = inputs.root.parent.parent
    else:
        service_root = runtime_context.service_root.parent / "shakemap"
    return AlertService(
        database_path,
        EvidenceStore(Path(database_path).parent / "alert-evidence", service_root),
        alert_settings,
        logger=logger,
    )
