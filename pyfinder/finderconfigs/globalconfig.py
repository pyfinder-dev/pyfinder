"""Complete global FinDer configuration for the deployed FinDer version."""

import os

from pyfinder.pyfinderconfig import finder_resources, gmt_resources


# This mapping intentionally remains a complete native FinDer configuration.
# Regional configurations repeat the same full structure rather than inheriting
# individual values from this fallback definition.
GLOBAL_CONFIG = {
    # <size_t> number of thresholds, list of their <double> PGA values
    # "THRESHOLDS": "9 2.0 4.6 10.5 23.2 48.6 90.7 148.8 221.3 304.5",
    "THRESHOLDS": "10 0.1 2.0 4.6 10.5 23.2 48.6 90.7 148.8 221.3 304.5",

    # <string> [local directory] for generic templates
    "TEMPLATE_DIRECTORY": os.path.join(
        finder_resources,
        "Templates_PGA_20161020_CH2009_resolution_5",
    ),

    # <string> [filename] list of generic + fault-specific template IDs
    "TEMPLATE_ID_FILE": os.path.join(finder_resources, "template.config"),

    # <double> delta degrees in strike search
    "D_DEG": "5.0",

    # <double> minimum strike angle to search over
    "MIN_DEG": "0.0",

    # <double> maximum strike angle to search over
    "MAX_DEG": "175.0",

    # <double> minimum rupture length to search over
    "MIN_LENGTH": "0.0",

    # <double> maximum rupture length to search over
    "MAX_LENGTH": "300.0",

    # <double> default depth for the earthquake source; no calculation effect
    "DEFAULT_DEPTH": "10.0",

    # <double> default source-depth uncertainty; no calculation effect
    "DEFAULT_DEPTH_UNCER": "5.0",

    # <int> 1 for Wells and Coppersmith (e.g. CA), 2 for Blaser (e.g. JP)
    "MAG_OPTION": "1",

    # <string> "complete" or "fast"
    "RUN_SPEED": "fast",

    # <string> filename, "calculate" to generate a mask, or "no_mask"
    "REGIONAL_MASK": "calculate",

    # <double> [km] maximum distance between stations when calculating a mask
    "MASK_STATION_DISTANCE": "75.0",

    # <size_t> minimum stations needed to trigger FinDer; minimum of 1
    "MIN_TRIGGER_STATIONS": "2",

    # <double> maximum radius between trigger stations
    "TRIGGER_RADIUS": "50.0",

    # <string> switch for station-specific triggering radius
    "USE_FIXED_TRIGRAD": "yes",

    # <double> maximum station-specific triggering radius in km
    "MAX_STATION_TRIGRAD": "150.0",

    # <size_t> number of networks followed by their network codes
    "SECONDARY_NETWORKS": "2 CE CSN",

    # <double> minimum 1.0; minimum degree border around image
    "BORDER_DEGREES": "1.0",

    # <size_t> pixels that must pass a threshold before it is used
    "IMAGE_PIXELS": "10",

    # <size_t> pixels that must pass before moving up a threshold
    "MAX_IMAGE_PIXELS": "50",

    # <double> minimum likelihood value for an estimate
    "MIN_LIKELIHOOD_ESTIMATE_FOR_MESSAGE": "0.65",

    # <double> value related to misfit calculations
    "SIGMA_LENGTH": "1.0",

    # <double> value related to misfit calculations
    "SIGMA_AZIMUTH": "1.0",

    # <double> value related to misfit calculations
    "SIGMA_LATLON": "1.0",

    # <size_t> maximum generic-template ruptures FinDer outputs; minimum of 1
    "MAX_RUPTURES": "30",

    # <string> "yes" or "no" for using the GMT 5.2.0 API
    "GMT_API_OPTION": "yes",

    # <string> "gmt" for GMT 5.0, or "---" for no command prefix
    "GMT_PREFIX": "gmt",

    # <string> "yes" or "no" for creating GMT plots in offline testing
    "GMT_PLOT": "yes",

    # <string> [filename]
    "COLOR_SCALE": os.path.join(
        gmt_resources,
        "gmt_input",
        "log_pga_wald.cpt",
    ),

    # <string> [filename]
    "FAULT_DEFINITIONS": os.path.join(
        gmt_resources,
        "gmt_input",
        "jennings.xy",
    ),

    # <string> [filename] station-specific thresholds
    "STATION_CONFIG": "---",

    # <string> [foldername]
    "GMT_FOLDER": os.path.join(gmt_resources, "gmt_input"),

    # <string> [foldername] place to put temp and temp_data folders
    "DATA_FOLDER": "<PATH>",

    # <double> epicenter-to-fault distance threshold in km; exceeding it
    # creates a new event ID
    "EPI_FAULT_DIST_THRESH": "100.",

    # <double> regression threshold. Below this value regression may be used;
    # above it the original rupture-based FinDer magnitude is retained.
    "MAG_REGRESSION_THRESH": 5.5,

    "STOP_LENGTH_DECREASE_PC": "0.2",

    "RESTART_LENGTH_INCREASE_PC": "0.0001",

    "UNCERTAINTY_METHOD": "0",
}
