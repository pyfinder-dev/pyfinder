"""Run identity and filesystem selection for FinDer event workspaces."""

from pathlib import Path, PureWindowsPath


class WorkspaceIdentityError(ValueError):
    """Report an identity that cannot safely name one event workspace."""


def build_augmented_event_id(event_id, delay_minutes):
    """Return the established event-and-delay identity used by PyFinder."""
    appendix = "t00000"
    if delay_minutes is not None:
        appendix = "t{0:05d}".format(int(delay_minutes))
    return "{0}_{1}".format(event_id, appendix)


def select_workspace_path(work_root, augmented_event_id):
    """Resolve one unchanged identity beneath its configured work root.

    The identity is a filesystem name, not a relative path. Existing path
    components are resolved so that a workspace symlink cannot redirect an
    execution outside the configured root.
    """
    if not isinstance(augmented_event_id, str):
        raise WorkspaceIdentityError(
            "augmented workspace identity must be a string"
        )
    if not augmented_event_id:
        raise WorkspaceIdentityError(
            "augmented workspace identity must not be empty"
        )
    if "\x00" in augmented_event_id:
        raise WorkspaceIdentityError(
            "augmented workspace identity contains a null character"
        )
    if augmented_event_id in (".", ".."):
        raise WorkspaceIdentityError(
            "augmented workspace identity must not be a traversal component"
        )
    if "/" in augmented_event_id or "\\" in augmented_event_id:
        raise WorkspaceIdentityError(
            "augmented workspace identity must be exactly one path component"
        )

    identity_path = Path(augmented_event_id)
    windows_identity = PureWindowsPath(augmented_event_id)
    if identity_path.is_absolute() or windows_identity.is_absolute():
        raise WorkspaceIdentityError(
            "augmented workspace identity must not be absolute"
        )
    if windows_identity.drive:
        raise WorkspaceIdentityError(
            "augmented workspace identity must not contain a drive"
        )

    root = Path(work_root)
    if not root.is_absolute():
        raise WorkspaceIdentityError(
            "configured FinDer work root must be absolute: {0}".format(root)
        )

    workspace = root / augmented_event_id
    try:
        resolved_root = root.resolve(strict=False)
        resolved_workspace = workspace.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise WorkspaceIdentityError(
            "augmented workspace path cannot be resolved safely: {0}".format(
                augmented_event_id
            )
        ) from error
    try:
        resolved_workspace.relative_to(resolved_root)
    except ValueError as error:
        raise WorkspaceIdentityError(
            "augmented workspace identity escapes the configured work root: "
            "{0}".format(augmented_event_id)
        ) from error

    return workspace
