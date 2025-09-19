# !/usr/bin/env python
# -*- coding: utf-8 -*-
""" 
Start services for the pyfinder module. 

This module is the main entry point for the pyfinder module. It starts the 
listeners to manage the whole workflow from a new event detection to running 
the FinDer with the parametric datasets.
"""
import os
import sys
# Add the parent directory to the system path
if not os.path.abspath("../") in sys.path:
    sys.path.insert(0, os.path.abspath("../"))

import threading
from services import seismiclistener
from services.scheduler import FollowUpScheduler
from utils.customlogger import file_logger
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

    def start_listener():
        logger.info("Starting seismic listener...")
        seismiclistener.start_emsc_listener()

    global _listener_thread, _scheduler
    _listener_thread = threading.Thread(target=start_listener, daemon=False)
    _listener_thread.start()

    logger.info("Starting FollowUpScheduler...")
    _scheduler = FollowUpScheduler()
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