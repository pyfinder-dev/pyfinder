# !/usr/bin/env python
# -*- coding: utf-8 -*-
"""Start continuous PyFinder listener and scheduler services."""

import logging
import signal
import sys
import threading

from pyfinder.finderconfigs import (
    GlobalFinderConfigError,
    build_default_selector,
)
from pyfinder.pyfinderconfig import pyfinderconfig
from pyfinder.services import seismiclistener
from pyfinder.services.querypolicy import build_service_policies
from pyfinder.services.scheduler import FollowUpScheduler
from pyfinder.utils.customlogger import file_logger


_scheduler = None
_listener = None
_listener_thread = None
_shutdown_event = None
_launcher_logger = None


def _request_shutdown(signum=None, frame=None):
    """Request an orderly stop without exiting the reusable command helper."""
    del frame
    logger = _launcher_logger or logging.getLogger(__name__)
    logger.info("Received signal %s; stopping continuous services.", signum)
    if _shutdown_event is not None:
        _shutdown_event.set()
    if _listener is not None:
        _listener.stop()


def _install_signal_handlers():
    """Install process signal handlers and return the previous handlers."""
    previous_handlers = {}
    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, _request_shutdown)
    except BaseException:
        for installed_signum, previous_handler in previous_handlers.items():
            signal.signal(installed_signum, previous_handler)
        raise
    return previous_handlers


def _cleanup_continuous_services(
    *,
    listener,
    scheduler,
    listener_thread,
    listener_started,
    previous_signal_handlers,
    logger,
):
    """Stop owned resources in order and return every cleanup failure."""
    failures = []

    def attempt(description, operation):
        try:
            operation()
        except BaseException as error:
            logger.error(
                "%s failed: %s",
                description,
                error,
                exc_info=(type(error), error, error.__traceback__),
            )
            failures.append(error)

    if listener is not None:
        attempt("Stopping the seismic listener", listener.stop)
    if scheduler is not None:
        attempt("Shutting down the scheduler", scheduler.shutdown)
    if listener_thread is not None and listener_started:
        attempt("Joining the seismic listener thread", listener_thread.join)
    if listener is not None:
        attempt("Closing seismic listener persistence", listener.close)
    for signum, previous_handler in previous_signal_handlers.items():
        attempt(
            "Restoring signal handler {0}".format(signum),
            lambda signum=signum, previous_handler=previous_handler: (
                signal.signal(signum, previous_handler)
            ),
        )
    return failures


def start_services(*, runtime_context):
    """Start continuous services from one validated runtime context."""
    global _launcher_logger, _listener, _listener_thread, _scheduler
    global _shutdown_event

    logger = file_logger(
        runtime_context.process_log_path,
        module_name="ServiceLauncher",
        rotate=True,
        overwrite=False,
    )
    listener_logger = file_logger(
        runtime_context.listener_log_path,
        module_name="SeismicListener",
        rotate=True,
        overwrite=False,
        level=logging.DEBUG,
    )
    scheduler_logger = file_logger(
        runtime_context.scheduler_log_path,
        module_name="FollowUpScheduler",
        rotate=True,
        overwrite=False,
    )
    _launcher_logger = logger
    application_configuration = runtime_context.isolated_configuration(
        pyfinderconfig
    )

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

    shutdown_event = threading.Event()
    listener = None
    scheduler = None
    listener_thread = None
    listener_started = False
    previous_signal_handlers = {}
    listener_failures = []
    primary_error = None
    primary_traceback = None

    try:
        listener = seismiclistener.build_emsc_listener(
            policy=rrsm_policy,
            db_path=runtime_context.operational_database_path,
            logger=listener_logger,
            configuration=application_configuration,
        )
        scheduler = FollowUpScheduler(
            service_policies=service_policies,
            finder_config_selector=finder_config_selector,
            db_path=runtime_context.operational_database_path,
            logger=scheduler_logger,
            configuration=application_configuration,
        )

        _listener = listener
        _scheduler = scheduler
        _shutdown_event = shutdown_event
        previous_signal_handlers = _install_signal_handlers()

        def run_listener():
            try:
                listener.run()
            except BaseException as error:
                listener_failures.append(error)
            finally:
                shutdown_event.set()

        listener_thread = threading.Thread(
            target=run_listener,
            daemon=False,
        )
        _listener_thread = listener_thread
        listener_thread.start()
        listener_started = True
        logger.info("Continuous listener and scheduler started.")

        scheduler.run_forever(shutdown_event=shutdown_event)
        if listener_failures:
            raise listener_failures[0]
    except BaseException as error:
        primary_error = error
        primary_traceback = error.__traceback__
    finally:
        shutdown_event.set()
        cleanup_failures = _cleanup_continuous_services(
            listener=listener,
            scheduler=scheduler,
            listener_thread=listener_thread,
            listener_started=listener_started,
            previous_signal_handlers=previous_signal_handlers,
            logger=logger,
        )
        _shutdown_event = None
        _listener_thread = None
        _scheduler = None
        _listener = None

    if primary_error is not None:
        for cleanup_error in cleanup_failures:
            primary_error.add_note(
                "Cleanup also failed: {0}: {1}".format(
                    type(cleanup_error).__name__,
                    cleanup_error,
                )
            )
        raise primary_error.with_traceback(primary_traceback)
    if cleanup_failures:
        first_cleanup_error = cleanup_failures[0]
        for additional_error in cleanup_failures[1:]:
            first_cleanup_error.add_note(
                "Additional cleanup failure: {0}: {1}".format(
                    type(additional_error).__name__,
                    additional_error,
                )
            )
        raise first_cleanup_error
    return 0


if __name__ == "__main__":
    from pyfinder.cli import main

    sys.exit(main(["continuous", *sys.argv[1:]]))
