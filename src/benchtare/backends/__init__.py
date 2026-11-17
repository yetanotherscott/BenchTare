"""Backends: code that drives one kind of bench equipment.

BenchTare ships two simulated backends. Hardware backends, and third-party backends as plugins,
come later behind the same protocol (``benchtare.backends.base.Backend``). Every backend, these
two included, is found through the ``benchtare.backends`` entry points: see
``benchtare.discovery``.
"""

from __future__ import annotations

from benchtare.backends.base import (
    Backend,
    BackendType,
    BenchError,
    ComparisonFailed,
    ReadTimeout,
    WriteError,
)
from benchtare.backends.sim import SimIO
from benchtare.backends.sim_can import SimCan

__all__ = [
    "Backend",
    "BackendType",
    "BenchError",
    "ComparisonFailed",
    "ReadTimeout",
    "SimCan",
    "SimIO",
    "WriteError",
]
