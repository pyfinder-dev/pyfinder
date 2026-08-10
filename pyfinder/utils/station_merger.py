# -*- coding: utf-8 -*-
"""Utility classes for merging normalized observations from web services."""
import logging
from typing import List, Mapping, Sequence, TypedDict

from pyfinder.pyfinderconfig import EMSC_FELT_REPORT_SERVICE


InstrumentalIdentity = tuple[str, str, str, str]


class RawStationMeasurement(TypedDict):
    """
    A dictionary to hold the raw station measurement data.
    """
    latitude: float
    longitude: float
    network: str
    station: str
    location: str 
    channel: str
    pga: float  # in cm/s/s
    timestamp: float 
    source: str  # "ESM" or "RRSM" etc.
    provider_value: float
    provider_unit: str


class StationMerger:
    """Merge normalized service lists in configured scientific priority."""

    def __init__(self, service_priority: Sequence[str], logger=None):
        self.service_priority = service_priority
        self.logger = logger or logging.getLogger(__name__)

    def merge(
        self,
        available_results: Mapping[str, List[RawStationMeasurement]],
    ) -> List[RawStationMeasurement]:
        """Return retained records in service-priority and provider order."""
        merged = []
        service_identities = {}
        global_winners = {}

        # Provider acquisition has already decided operational membership.
        # Iterating priority while requiring a present mapping key keeps that
        # membership separate from scientific ordering.
        for service_name in self.service_priority:
            if service_name not in available_results:
                continue

            seen_identities = service_identities.setdefault(
                service_name,
                set(),
            )

            for station in available_results[service_name]:
                if service_name == EMSC_FELT_REPORT_SERVICE:
                    merged.append(station)
                    continue

                identity = self._instrumental_identity(station)
                if identity is None:
                    merged.append(station)
                    continue

                # Same-service tracking is deliberately checked before the
                # global winner. A lower-priority service still has one local
                # representative even when that representative loses globally.
                if identity in seen_identities:
                    self.logger.warning(
                        "Omitting duplicate instrumental observation from "
                        "service %s for identity %r; the first provider-order "
                        "record from that service was used as its "
                        "representative.",
                        service_name,
                        identity,
                    )
                    continue
                seen_identities.add(identity)

                winning_service = global_winners.get(identity)
                if winning_service is not None:
                    self.logger.warning(
                        "Omitting lower-priority instrumental observation from "
                        "service %s for identity %r; service %s supplied the "
                        "higher-priority record.",
                        service_name,
                        identity,
                        winning_service,
                    )
                    continue

                global_winners[identity] = service_name
                merged.append(station)

        return merged

    @staticmethod
    def _instrumental_identity(
        station: RawStationMeasurement,
    ) -> InstrumentalIdentity | None:
        """Return an exact usable SNCL identity without repairing its codes."""
        network = station["network"]
        station_code = station["station"]
        channel = station["channel"]
        if not network or not station_code or not channel:
            return None

        return network, station_code, station["location"], channel
