"""Resolve the scientific priority shared by observation-service workflows."""

import logging

from pyfinder.pyfinderconfig import SHIPPED_SERVICE_PRIORITY


def _contains_duplicates(values):
    """Return whether a list repeats an entry without requiring hashable values."""
    return any(
        value in values[:index]
        for index, value in enumerate(values)
    )


def resolve_service_priority(configured_priority=None, *, logger=None):
    """Return a valid configured priority or an isolated shipped-order fallback."""
    if (
        not isinstance(configured_priority, list)
        or not configured_priority
        or _contains_duplicates(configured_priority)
    ):
        active_logger = (
            logger if logger is not None else logging.getLogger(__name__)
        )
        active_logger.critical(
            "The configured services-priority must be a nonempty list without "
            "duplicate entries. Using the shipped service priority: %s.",
            list(SHIPPED_SERVICE_PRIORITY),
        )
        return list(SHIPPED_SERVICE_PRIORITY)

    return list(configured_priority)
