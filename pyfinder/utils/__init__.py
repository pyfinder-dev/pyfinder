# -*- coding: utf-8 -*-
from . import customlogger

__all__ = ["Calculator", "customlogger"]


def __getattr__(name):
    """Load optional utility dependencies only when their public name is used."""
    if name == "Calculator":
        # Importing the package-level logger should not require NumPy. Keep the
        # established Calculator exposure while deferring its dependency until
        # callers actually request the class.
        from .calculator import Calculator

        globals()[name] = Calculator
        return Calculator
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
