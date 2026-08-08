"""Complete Italian FinDer configuration for the deployed FinDer version."""

import os

from pyfinder.pyfinderconfig import finder_resources, gmt_resources


# This is an independent complete source dictionary. Its initial values match
# the other named configurations until verified regional parameters are known.
ITALY_CONFIG = {
    "THRESHOLDS": "10 0.1 2.0 4.6 10.5 23.2 48.6 90.7 148.8 221.3 304.5",
    "TEMPLATE_DIRECTORY": os.path.join(
        finder_resources,
        "Templates_PGA_20161020_CH2009_resolution_5",
    ),
    "TEMPLATE_ID_FILE": os.path.join(finder_resources, "template.config"),
    "D_DEG": "5.0",
    "MIN_DEG": "0.0",
    "MAX_DEG": "175.0",
    "MIN_LENGTH": "0.0",
    "MAX_LENGTH": "300.0",
    "DEFAULT_DEPTH": "10.0",
    "DEFAULT_DEPTH_UNCER": "5.0",
    "MAG_OPTION": "1",
    "RUN_SPEED": "fast",
    "REGIONAL_MASK": "calculate",
    "MASK_STATION_DISTANCE": "75.0",
    "MIN_TRIGGER_STATIONS": "2",
    "TRIGGER_RADIUS": "50.0",
    "USE_FIXED_TRIGRAD": "yes",
    "MAX_STATION_TRIGRAD": "150.0",
    "SECONDARY_NETWORKS": "2 CE CSN",
    "BORDER_DEGREES": "1.0",
    "IMAGE_PIXELS": "10",
    "MAX_IMAGE_PIXELS": "50",
    "MIN_LIKELIHOOD_ESTIMATE_FOR_MESSAGE": "0.65",
    "SIGMA_LENGTH": "1.0",
    "SIGMA_AZIMUTH": "1.0",
    "SIGMA_LATLON": "1.0",
    "MAX_RUPTURES": "30",
    "GMT_API_OPTION": "yes",
    "GMT_PREFIX": "gmt",
    "GMT_PLOT": "yes",
    "COLOR_SCALE": os.path.join(
        gmt_resources,
        "gmt_input",
        "log_pga_wald.cpt",
    ),
    "FAULT_DEFINITIONS": os.path.join(
        gmt_resources,
        "gmt_input",
        "jennings.xy",
    ),
    "STATION_CONFIG": "---",
    "GMT_FOLDER": os.path.join(gmt_resources, "gmt_input"),
    "DATA_FOLDER": "<PATH>",
    "EPI_FAULT_DIST_THRESH": "100.",
    "MAG_REGRESSION_THRESH": 5.5,
    "STOP_LENGTH_DECREASE_PC": "0.2",
    "RESTART_LENGTH_INCREASE_PC": "0.0001",
    "UNCERTAINTY_METHOD": "0",
}
