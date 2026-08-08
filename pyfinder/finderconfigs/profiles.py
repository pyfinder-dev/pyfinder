"""Static catalog of named FinDer computational configurations."""

from dataclasses import dataclass
from typing import Mapping, Optional

from pyfinder.finderconfigs.globalconfig import GLOBAL_CONFIG
from pyfinder.finderconfigs.italy import ITALY_CONFIG
from pyfinder.finderconfigs.switzerland import (
    SWITZERLAND_ALPINE_CONFIG,
    SWITZERLAND_FORELAND_CONFIG,
)


@dataclass(frozen=True)
class FinderConfigProfile:
    """Connect one runtime name to a complete configuration and optional WKT."""

    name: str
    configuration: Mapping[str, object]
    wkt_filename: Optional[str] = None


GLOBAL_PROFILE = FinderConfigProfile(
    name="global",
    configuration=GLOBAL_CONFIG,
)


# Registry order is explicit because a later selector will use the first
# covering geometry when registered computational regions overlap.
REGIONAL_PROFILES = (
    FinderConfigProfile(
        name="switzerland-alpine",
        configuration=SWITZERLAND_ALPINE_CONFIG,
        wkt_filename="swiss_alpine.wkt",
    ),
    FinderConfigProfile(
        name="switzerland-foreland",
        configuration=SWITZERLAND_FORELAND_CONFIG,
        wkt_filename="swiss_foreland.wkt",
    ),
    FinderConfigProfile(
        name="italy",
        configuration=ITALY_CONFIG,
        wkt_filename="italy.wkt",
    ),
)
