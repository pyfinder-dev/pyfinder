# -*- coding: utf-8 -*-
"""PyFinder's isolated file-logger configuration."""

import logging
import logging.handlers
import os
import threading
import types


# Define a new log level (OK) that is higher than INFO but lower than WARNING.
OK_LOG_LEVEL = logging.INFO + 5

# FinDer output keeps its existing custom level until its caller is refactored.
FINDER_LOG_LEVEL = logging.INFO + 6

logging.addLevelName(OK_LOG_LEVEL, "OK")
logging.addLevelName(FINDER_LOG_LEVEL, "FinDer")


class FileLoggingFormatter(logging.Formatter):
    """Formatter for human-readable file logging without console colours."""

    log_time = "%(asctime)-s "
    log_level = "%(levelname)-8s "
    log_message = "%(message)s (%(filename)s:%(lineno)d)"

    FORMATS = {
        logging.DEBUG: log_time + log_level + log_message,
        logging.INFO: log_time + log_level + log_message,
        logging.WARNING: log_time + log_level + log_message,
        logging.ERROR: log_time + log_level + log_message,
        logging.CRITICAL: log_time + log_level + log_message,
        OK_LOG_LEVEL: log_time + log_level + log_message,
        FINDER_LOG_LEVEL: log_time + log_level + log_message,
    }

    def format(self, record):
        log_fmt = self.FORMATS.get(record.levelno)
        formatter = logging.Formatter(log_fmt, "%Y-%m-%d %H:%M:%S")
        return formatter.format(record)


_DEFAULT_LOGICAL_NAME = "pyfinder.default"
_ROTATION_MAX_BYTES = 1000000
_ROTATION_BACKUP_COUNT = 7

# A configuration stays active while its owned handler remains active. Keeping
# both maps here prevents the standard logging registry from coupling two
# destinations through a shared logger name.
_CONFIGURATIONS = {}
_DESTINATION_OWNERS = {}
_CONFIGURATION_LOCK = threading.RLock()


def _ok(self, message, *args, **kwargs):
    """Log at the PyFinder OK level while preserving caller attribution."""
    if self.isEnabledFor(OK_LOG_LEVEL):
        stacklevel = kwargs.pop("stacklevel", 1)
        self._log(
            OK_LOG_LEVEL,
            message,
            args,
            stacklevel=stacklevel + 1,
            **kwargs,
        )


def _finder(self, message, *args, **kwargs):
    """Log at the retained FinDer level while preserving the real caller."""
    if self.isEnabledFor(FINDER_LOG_LEVEL):
        stacklevel = kwargs.pop("stacklevel", 1)
        self._log(
            FINDER_LOG_LEVEL,
            message,
            args,
            stacklevel=stacklevel + 1,
            **kwargs,
        )


def _attach_custom_methods(logger):
    """Attach PyFinder conveniences to one logger, never to logging globally."""
    logger.ok = types.MethodType(_ok, logger)
    logger.OK = types.MethodType(_ok, logger)
    logger.finder = types.MethodType(_finder, logger)
    logger.FINDER = types.MethodType(_finder, logger)


def _new_handler(destination, overwrite, rotate):
    """Open the requested handler and preserve initial overwrite semantics."""
    if rotate:
        # RotatingFileHandler forces append mode whenever maxBytes is non-zero.
        # Open the bounded handler first, then truncate its existing stream for
        # the initial overwrite request. This avoids silently changing handler
        # type and also avoids modifying the file if construction itself fails.
        handler = logging.handlers.RotatingFileHandler(
            destination,
            mode="a",
            maxBytes=_ROTATION_MAX_BYTES,
            backupCount=_ROTATION_BACKUP_COUNT,
            encoding="utf-8",
        )
        if overwrite:
            try:
                handler.stream.seek(0)
                handler.stream.truncate(0)
                handler.stream.seek(0, os.SEEK_END)
            except Exception:
                handler.close()
                raise
        return handler

    mode = "w" if overwrite else "a"
    return logging.FileHandler(destination, mode=mode, encoding="utf-8")


def file_logger(
    log_file,
    module_name=None,
    overwrite=False,
    rotate=False,
    level=logging.DEBUG,
):
    """Return an isolated PyFinder logger for one logical file destination."""
    destination = os.path.abspath(os.fspath(log_file))
    logical_name = (
        _DEFAULT_LOGICAL_NAME if module_name is None else module_name
    )
    configuration_key = (logical_name, destination)

    with _CONFIGURATION_LOCK:
        existing = _CONFIGURATIONS.get(configuration_key)
        if existing is not None:
            logger, configured_rotate = existing
            if configured_rotate != rotate:
                raise ValueError(
                    "The active logger uses a different rotation setting"
                )
            logger.setLevel(level)
            return logger

        owner = _DESTINATION_OWNERS.get(destination)
        if owner is not None and owner != logical_name:
            raise ValueError(
                "A different logical logger already owns this destination"
            )

        # Construct the handler only after conflict checks. This ordering is
        # important: repeated setup must not open, truncate, or leak a file.
        handler = _new_handler(destination, overwrite, rotate)
        try:
            handler.setLevel(logging.NOTSET)
            handler.setFormatter(FileLoggingFormatter())

            logger = logging.Logger(
                "pyfinder.file.{0}@{1}".format(logical_name, destination)
            )
            logger.setLevel(level)
            logger.propagate = False
            _attach_custom_methods(logger)
            logger.addHandler(handler)
        except Exception:
            handler.close()
            raise

        _CONFIGURATIONS[configuration_key] = (logger, rotate)
        _DESTINATION_OWNERS[destination] = logical_name
        return logger
