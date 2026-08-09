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
EMSC_FEELT_REPORT_SERVICE = "EMSC_FeltReport"


pyfinderconfig = {
    "general": {
        # Web services ordered by priority
        "services": [ESM_SHAKEMAP_SERVICE, RRSM_PEAK_MOTION_SERVICE, EMSC_FEELT_REPORT_SERVICE],

        # Choose the greatest valid acceleration from either every component
        # or only non-vertical components. ESM applies this setting now; RRSM
        # will adopt the same application-wide setting in its normalization
        # refactor.
        "component-selection": "maximum-all",
        
        # The default test event id for the FinDer executable
        "test-event-id": NORCIA_ITALY_EVENT_ID,

        # Blacklist of stations, channels etc. The format is NET.STA.LOC.CHA
        "channel-blacklist": "",

    },

    "shakemap": {
        # Use amplitude from FinDer output, or use the original amplitudes
        # from the web services. Original amplitudes are merged if multiple
        # web services are used.
        "use-amplitude-from-finder-output": False,

        # Region-specific ShakeMap configuration. The configuration files
        # are automatically downloaded and extracted to the extern/shakemap-conf-eu directory.
        # No need to change this path or do anything else. The regional configuration
        # will be ignored if the use-region-specific-shakemap-config is False.
        "use-region-specific-shakemap-config": False,
        "region-specific-shakemap-config": {          
            "al": "extern/shakemap-conf-eu/config/albania",
            "hr": "extern/shakemap-conf-eu/config/croatia",
            "gr": "extern/shakemap-conf-eu/config/greece",
            "ro": "extern/shakemap-conf-eu/config/romania",
            "ch": "extern/shakemap-conf-eu/config/switzerland",
            "be": "extern/shakemap-conf-eu/config/belgium",
            "fr": "extern/shakemap-conf-eu/config/france",
            "it": "extern/shakemap-conf-eu/config/italy",
            "si": "extern/shakemap-conf-eu/config/slovenia"
        },

        # Shapefile for country borders. Pre-downloaded from Natural Earth:
        # https://www.naturalearthdata.com/http//www.naturalearthdata.com/download/110m/cultural/ne_110m_admin_0_countries.zip
        "country-borders-shapefile": "extern/ne_110m_admin_0_countries/ne_110m_admin_0_countries.shp"
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
