"""Backends: code that drives one kind of bench equipment.

The first slice ships two simulated backends. Hardware backends, and third-party backends as
plugins, come later behind the same protocol (``benchtare.backends.base.Backend``).
"""

from __future__ import annotations

from benchtare.backends.base import Backend, BackendType, BenchError, ReadTimeout, WriteError
from benchtare.backends.sim import SimIO
from benchtare.backends.sim_can import SimCan

BACKENDS: dict[str, BackendType] = {SimIO.name: SimIO, SimCan.name: SimCan}

__all__ = [
    "BACKENDS",
    "Backend",
    "BackendType",
    "BenchError",
    "ReadTimeout",
    "SimCan",
    "SimIO",
    "WriteError",
]
