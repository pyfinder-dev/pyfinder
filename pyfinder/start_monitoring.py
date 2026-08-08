# !/usr/bin/env python
# -*- coding: utf-8 -*-
""" 
Start services for the pyfinder module. 

This module is the main entry point for the pyfinder module. It starts the 
listeners to manage the whole workflow from a new event detection to running 
the FinDer with the parametric datasets.
"""
import sys
import threading

from pyfinder.finderconfigs import (
    GlobalFinderConfigError,
    build_default_selector,
)
from pyfinder.services import seismiclistener
from pyfinder.services.querypolicy import build_service_policies
from pyfinder.services.scheduler import FollowUpScheduler
from pyfinder.utils.customlogger import file_logger
import signal
import atexit
import time
_scheduler = None
_listener_thread = None


def _graceful_shutdown(signum=None, frame=None):
    try:
        logger = file_logger("monitoring.log", module_name="ServiceLauncher")
        logger.info(f"Received signal {signum}; shutting down...")
    except Exception:
        pass
    global _scheduler, _listener_thread
    # Stop scheduler if present
    try:
        if _scheduler is not None:
            _scheduler.shutdown()
    except Exception:
        pass
    # Give threads a moment to finish
    try:
        if _listener_thread is not None and _listener_thread.is_alive():
            _listener_thread.join(timeout=5.0)
    except Exception:
        pass
    # Small delay to allow any subprocess cleanup by children
    try:
        time.sleep(0.1)
    except Exception:
        pass
    # Exit cleanly
    try:
        sys.exit(0)
    except SystemExit:
        raise


def start_services():
    logger = file_logger("monitoring.log", module_name="ServiceLauncher")

    try:
        service_policies = build_service_policies()
        rrsm_policy = service_policies["RRSM"]
    except Exception:
        logger.exception(
            "Policy validation failed; aborting PyFinder startup"
        )
        raise

    try:
        finder_config_selector = build_default_selector(logger=logger)
    except GlobalFinderConfigError:
        logger.critical(
            "Global FinDer configuration validation failed; "
            "aborting PyFinder startup",
            exc_info=True,
        )
        raise

    def start_listener():
        logger.info("Starting seismic listener...")
        seismiclistener.start_emsc_listener(policy=rrsm_policy)

    global _listener_thread, _scheduler
    _listener_thread = threading.Thread(target=start_listener, daemon=False)
    _listener_thread.start()

    logger.info("Starting FollowUpScheduler...")
    _scheduler = FollowUpScheduler(
        service_policies=service_policies,
        finder_config_selector=finder_config_selector,
    )
    try:
        _scheduler.run_forever()
    except KeyboardInterrupt:
        _graceful_shutdown()

""" The main execution module for the pyfinder module. """
if __name__ == "__main__":
    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGTERM, _graceful_shutdown)
    signal.signal(signal.SIGINT, _graceful_shutdown)
    atexit.register(_graceful_shutdown)
    # Start the services
    start_services()
