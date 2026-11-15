"""The reference: a simulated DUT, bench and harness that ship with BenchTare.

Used by the quickstart and by BenchTare's own tests, so nothing needs hardware.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

DUT = "reference.dut.yaml"
DBC = "reference.dbc"
BENCH = "bench-sim.yaml"
HARNESS_CAN = "harness-can.yaml"
HARNESS_IO = "harness-io.yaml"


def path(name: str) -> Path:
    """The path of a reference file shipped in the package."""
    return Path(str(files(__name__) / name))
