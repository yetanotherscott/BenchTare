"""Simulated DUTs a harness file can plug in with ``simulate:``."""

from __future__ import annotations

from benchtare.reference.device import ReferenceDevice

SIMULATED_DUTS: dict[str, type[ReferenceDevice]] = {"reference": ReferenceDevice}
