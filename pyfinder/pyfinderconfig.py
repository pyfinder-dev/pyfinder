# -*- coding: utf-8 -*-
""" Configuration file for the pyfinder module. """
import os

def get_path_of_configuration():
    return os.path.dirname(os.path.abspath(__file__))

# Kahramanmaras, Turkey earthquake, 2023, Mw 7.8
KAHRAMANMARAS_TURKEY_EVENT_ID = "20230206_0000008"

# Norcia, Italy earthquake, 2016-10-30 06:40:18 UTC, Mw 6.5
NORCIA_ITALY_EVENT_ID = "20161030_0000029"

ESM_SHAKEMAP_SERVICE = "ESM_ShakeMap"
RRSM_PEAK_MOTION_SERVICE = "RRSM_PeakMotion"
RRSM_SHAKEMAP_SERVICE = "RRSM_ShakeMap"
EMSC_FELT_REPORT_SERVICE = "EMSC_FeltReport"
USGS_SHAKEMAP_SERVICE = "USGS_ShakeMap"
USGS_DYFI_SERVICE = "USGS_DYFI"

SHIPPED_SERVICE_PRIORITY = (
    ESM_SHAKEMAP_SERVICE,
    RRSM_PEAK_MOTION_SERVICE,
    EMSC_FELT_REPORT_SERVICE,
    USGS_SHAKEMAP_SERVICE,
    USGS_DYFI_SERVICE,
)


pyfinderconfig = {
    "general": {
        # Enabled services are a membership list. Their order does not express
        # scientific preference; that is owned separately by services-priority.
        # USGS_SHAKEMAP_SERVICE and USGS_DYFI_SERVICE are opt-in. Their
        # shipped priority follows the existing sources; enable them here
        # and adjust services-priority only when a different rank is intended.
        "services-enabled": [
            ESM_SHAKEMAP_SERVICE,
            RRSM_PEAK_MOTION_SERVICE,
        ],

        # This order is shared by event-context selection and cross-source
        # observation merging. A service can have priority without being enabled.
        "services-priority": list(SHIPPED_SERVICE_PRIORITY),

        # Choose the greatest valid acceleration from either every component
        # or only non-vertical components. The same application-wide setting
        # applies to both ESM and RRSM normalization.
        "component-selection": "maximum-all",
        
        # The default test event id for the FinDer executable
        "test-event-id": NORCIA_ITALY_EVENT_ID,

        # Blacklist of stations, channels etc. The format is NET.STA.LOC.CHA
        "channel-blacklist": "",

    },

    "shakemap": {
        # Continuous and playback use this separate ShakeMap service. Set the
        # real service URL and caller-owned data/inputs mount before enabling
        # it. Playback requires it because every execution runs the full chain.
        "service-enabled": False,
        "service-url": None,
        "request-timeout-seconds": 30.0,
        "input-directory": None,
        "overwrite": True,

        # Use amplitude from FinDer output, or use the original amplitudes
        # from the web services. Original amplitudes are merged if multiple
        # web services are used.
        "use-amplitude-from-finder-output": False,

        # Each key is a human region label, not a country code or FinDer ID.
        # The WKT file defines its actual coverage in WGS84 lon/lat coordinates;
        # labels do not impose national boundaries. A region may cross borders,
        # be one of several subregions, or contain disjoint MULTIPOLYGON areas.
        #
        # configuration is the exact ShakeMap service profile for that area.
        # Several areas may share it, independently of FinDer's configurations.
        # The WKT basename resolves under extern/shakemap_regional_wkt; adding or
        # editing these packaged resources requires a caller rebuild/redeploy.
        # Keep the catalog nonempty. Uncovered or conflicting coverage uses
        # global; missing/unusable optional WKT logs a warning and uses global.
        "regional-configurations": {
            "albania": {
                "wkt": "albania.wkt",
                "configuration": "albania",
            },
            "belgium": {
                "wkt": "belgium.wkt",
                "configuration": "belgium",
            },
            "croatia": {
                "wkt": "croatia.wkt",
                "configuration": "croatia",
            },
            "france": {
                "wkt": "france.wkt",
                "configuration": "france",
            },
            "greece": {
                "wkt": "greece.wkt",
                "configuration": "greece",
            },
            "italy": {
                "wkt": "italy.wkt",
                "configuration": "italy",
            },
            "romania": {
                "wkt": "romania.wkt",
                "configuration": "romania",
            },
            "slovenia": {
                "wkt": "slovenia.wkt",
                "configuration": "slovenia",
            },
            "switzerland": {
                "wkt": "switzerland.wkt",
                "configuration": "switzerland",
            },
        },
    },

    # Configuration for the Seismic Portal WebSocket
    "seismic-portal-listener": {
        # WebSocket URI for Seismic Portal
        "echo-uri": 'wss://www.seismicportal.eu/standing_order/websocket',

        # Interval to ping the server to keep the connection alive
        "ping-interval": 15,

        # Filter for region
        "target-regions": ["Switzerland", "Italy", "World"],

        # Filter for magnitude
        "min-magnitude": 3.0,
    },

    # Logging configuration
    "logging": {
        # The default log level for the console/file logging
        "log-level": "DEBUG",

        # Overwrite the log file if it exists from previous runs. If False, 
        # the log file will be appended. If True, the previous logs will be lost.
        "overwrite-log-file": False,

        # Rotate the log file after reaching a certain size
        "rotate-log-file": True,
    },

    # Configuration for the FinDer executable
    "finder-executable": {
        # Path to the FinDer executable, including the executable name
        "path": "/usr/local/src/FinDer/finder_run",

        # The root path for outputs of all FinDer runs. A subfolder for
        # each run will be created under this path to store all the output.
        "output-root-folder": os.path.join(get_path_of_configuration(), "output"),

        # Paths for FinDer outputs (temp_data and temp), in case they are updated in the future.
        # {FINDER_RUN_DIR} will be replaced with the run directory of the FinDer executable.
        "finder-temp-data-dir": "{FINDER_RUN_DIR}/temp_data",
        "finder-temp-dir": "{FINDER_RUN_DIR}/temp",

        # Path for logging the output of the FinDer executable
        "log-file-name": "pyfinder.log",

        # Path for all finder resources (templates, etc.)
        "path-for-finder-resources": "/usr/local/src/FinDer/config",

        # Path for GMT resources
        "path-for-gmt-resources": "/usr/local/src/FinDer/config/gmt_input",

        # The mode of finder run. If live mode is False, the data_ file will
        # contain three columns: station lat/lon and log10(PGA). If True, the
        # data_ file will contain the lat/lon, station code, a time stamp, 
        # and the PGA (not log10).
        "finder-live-mode": False,

        # HNZ is the known FinDer-recognized component used when felt reports
        # receive a synthetic SNCL for input materialization.
        "felt-report-component-code": "HNZ",

        # This percentage is applied while observed amplitudes remain in the
        # linear cm/s² domain when the artificial point is calculated.
        "artificial-point-margin-percent": 1.0,
    }
}


# Finder resources (templates, etc.) are stored in the following path
finder_resources = pyfinderconfig["finder-executable"]["path-for-finder-resources"]

# GMT resources are stored in the following path
gmt_resources = pyfinderconfig["finder-executable"]["path-for-gmt-resources"]

# If the GMT resources already includes gmt_input folder, remove it 
# and use the parent folder. It is added later in the configuration below.
if gmt_resources.endswith("gmt_input"):
    gmt_resources = os.path.dirname(gmt_resources)
