"""Simulated DUTs a harness file can plug in with ``simulate:``."""

from __future__ import annotations

from benchtare.reference.device import ReferenceDevice
from benchtare.sim.model import DutModel

# The models that ship with BenchTare, by the name a harness file's simulate: uses.
SIMULATED_DUTS: dict[str, type[DutModel]] = {"reference": ReferenceDevice}
