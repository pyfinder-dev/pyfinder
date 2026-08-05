# -*- coding: utf-8 -*-
"""
Query policy classes defining persisted follow-up schedules and retry rules.
"""
from abc import ABC, abstractmethod
import math
from numbers import Real


class AbstractPolicy(ABC):
    def __init__(self):
        """Validate each concrete policy before it can be used."""
        self._validate_configuration()

    @abstractmethod
    def _validate_configuration(self):
        """Validate the concrete policy's service identity and schedule."""
        pass

    def _validate_service_name(self):
        """Require a usable service identity without selecting its value."""
        if not isinstance(self.service_name, str) or not self.service_name.strip():
            raise ValueError("Policy service name must be a non-empty string")

    @abstractmethod
    def should_retry_on_failure(self, event_meta: dict) -> bool:
        """Return True if the service should retry after failure."""
        pass


########
# RRSM
########
class RRSMQueryPolicy(AbstractPolicy):
    """
    Create persisted RRSM work at these registration-relative delays:
    0, 5, 15, 60, 180, 360, 1440, 2880 minutes
    """
    # RRSM query schedule in minutes
    QUERY_SCHEDULE_MINUTES = [0, 5, 15, 60, 180, 360, 1440, 2880]

    # The service name
    service_name = "RRSM"

    def _validate_configuration(self):
        self._validate_service_name()

        schedule = self.QUERY_SCHEDULE_MINUTES
        if not schedule:
            raise ValueError("RRSM policy schedule must not be empty")
        for delay in schedule:
            if isinstance(delay, bool) or not isinstance(delay, Real):
                raise ValueError("RRSM policy delays must be real numbers")
            if not math.isfinite(delay) or delay < 0:
                raise ValueError(
                    "RRSM policy delays must be finite and non-negative"
                )
        if len(set(schedule)) != len(schedule):
            raise ValueError("RRSM policy delays must be unique")
        if any(
            current >= following
            for current, following in zip(schedule, schedule[1:])
        ):
            raise ValueError("RRSM policy delays must be strictly increasing")

    def should_retry_on_failure(self, event_meta):
        return event_meta.get("retry_count", 0) < 3


########
# ESM: Dummy Policy for ESM. Coded for possible future use.
########
class ESMQueryPolicy(AbstractPolicy):
    QUERY_SCHEDULE_MINUTES = []
    service_name = "ESM"

    def _validate_configuration(self):
        self._validate_service_name()
        if self.QUERY_SCHEDULE_MINUTES:
            raise ValueError("ESM policy must remain an inactive placeholder")

    def should_retry_on_failure(self, event_meta):
        pass


########
# EMSC: Dummy Policy for now. Coded for possible future use
########
class EMSCQueryPolicy(AbstractPolicy):
    QUERY_SCHEDULE_MINUTES = []
    service_name = "EMSC"

    def _validate_configuration(self):
        self._validate_service_name()
        if self.QUERY_SCHEDULE_MINUTES:
            raise ValueError("EMSC policy must remain an inactive placeholder")

    def should_retry_on_failure(self, event_meta):
        pass


########
# Explicit registry construction
########
def build_service_policies():
    """Construct and validate a fresh registry at an explicit startup boundary."""
    return {
        "RRSM": RRSMQueryPolicy(),
        "ESM": ESMQueryPolicy(),
        "EMSC": EMSCQueryPolicy(),
    }
