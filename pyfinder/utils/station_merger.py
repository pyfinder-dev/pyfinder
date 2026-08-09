# -*- coding: utf-8 -*-
""" 
Utility classes used when merging station/amplitude data from different web services. 
"""
from typing import List, NotRequired, TypedDict

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
    # Provider-native provenance is populated by ESM now. It remains optional
    # in the shared record until RRSM completes its own scientific migration.
    provider_value: NotRequired[float]
    provider_unit: NotRequired[str]


class StationMerger:
    def merge(self, esm_data: List[RawStationMeasurement], 
              rrsm_data: List[RawStationMeasurement]) -> List[RawStationMeasurement]:
        """Merge two station lists, giving priority to ESM on conflicts."""
        merged = {}
        
        # Index RRSM data first
        if rrsm_data:
            for sta in rrsm_data:
                key = self._make_key(sta)
                merged[key] = sta

        # Overwrite with ESM (priority)
        if esm_data:
            for sta in esm_data:
                key = self._make_key(sta)
                merged[key] = sta

        # Sort merged stations by descending PGA
        return sorted(merged.values(), key=lambda x: x["pga"], reverse=True)

    def _make_key(self, sta: RawStationMeasurement) -> str:
        """Use SNCL or coordinates as key."""
        # Prefer SNCL if all fields exist
        if all(sta.get(k) for k in ["network", "station", "location", "channel"]):
            return f"{sta['network']}.{sta['station']}.{sta['location']}.{sta['channel']}"
        else:
            # Fall back to rounded lat/lon (avoid float drift)
            return f"{round(sta['latitude'], 4)}_{round(sta['longitude'], 4)}"
