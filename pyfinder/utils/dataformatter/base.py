# -*- coding: utf-8 -*-
"""Shared behavior for service-specific data formatters."""

import logging

from ...eventcontext import ProviderModelAccessError

class BaseDataFormatter(object):
    def __init__(self, logger=None):
        self.logger = logger or logging.getLogger("pyfinder")

    def set_logger(self, logger=None):
        """Set a logger for the BaseDataFormatter."""
        if logger is None:
            self.logger = logging.getLogger("pyfinder")
        else:
            self.logger = logger
        self.logger.info("Logger set for BaseDataFormatter.")

    def get_logger(self):
        """Get the logger for the BaseDataFormatter."""
        if self.logger is None:
            self.logger = logging.getLogger("pyfinder")
        return self.logger
    
    def log(self, message, level="info"):
        """ Log a message. """
        if self.logger:
            if level == "info":
                self.logger.info(message)
            elif level == "warning":
                self.logger.warning(message)
            elif level == "error":
                self.logger.error(message)
            elif level == "debug":
                self.logger.debug(message)
            else:
                self.logger.info(message)

    @staticmethod
    def _provider_value(provider_model, getter_name, *args):
        """Read one dependency-owned accessor through an explicit boundary."""
        try:
            getter = getattr(provider_model, getter_name)
            return getter(*args)
        except (AttributeError, IndexError, KeyError, TypeError,
                ValueError) as error:
            raise ProviderModelAccessError(
                f"public model accessor {getter_name} failed"
            ) from error

    @classmethod
    def _provider_collection(cls, provider_model, getter_name):
        """Read one list-valued dependency accessor and validate its shape."""
        collection = cls._provider_value(provider_model, getter_name)
        if not isinstance(collection, list):
            raise ProviderModelAccessError(
                f"public model accessor {getter_name} did not return a list"
            )
        return collection
    
    @staticmethod
    def extract_raw_stations(event_data, amplitudes):
        """ Method to be used when merging the station data from different services. """
        pass
