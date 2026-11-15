"""The test check: proving that each test can fail, by corrupting what it reads.

A test that passes whatever the DUT does proves nothing. The test check (``pytest
--check-tests``, or ``benchtare check-tests``) runs each test as usual, then reruns each test
that passed once per mode, with every reading in the test's body corrupted. Staging (setup) and
teardown read the real values, so the DUT is put into the same state each time.

- ``shift``: the DUT reports a wrong value. A number moves by its own size (at least 1), a
  boolean is inverted, and an enum takes the next of its states.
- ``stale``: the DUT ignored the command. The test reads the value from before the bench's
  most recent write, which a DUT that never responded would still be reporting.

A test that fails under a mode can fail. A test that still passes under every mode that applied
to it is reported as unable to fail. Readings that never arrived (``ReadTimeout``) prove
nothing about the test's checks, so a mode that ends that way is inconclusive. Corrupting
needs no ranges and works on any DUT: nothing here judges whether a value is valid.

See "The test check" in docs/model.md.
"""

from __future__ import annotations

import math
import numbers
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from benchtare.signals.model import Signal, SignalType

SHIFT = "shift"
STALE = "stale"
MODES = (SHIFT, STALE)

# What happened to a test under one mode.
FAILED = "failed"  # the test failed: it can catch this kind of fault
PASSED = "passed"  # it still passed
INCONCLUSIVE = "inconclusive"  # nothing arrived to corrupt, or staging failed
NOT_APPLIED = "not applied"  # nothing it read could be corrupted this way

# The verdict on a test.
ABLE = "able to fail"
UNABLE = "unable to fail"
UNKNOWN = "inconclusive"
NOT_CHECKED = "not checked"  # it didn't pass to begin with, or read nothing to corrupt


@dataclass(frozen=True)
class ModeResult:
    """One rerun of a test under one mode."""

    mode: str
    outcome: str  # FAILED, PASSED, INCONCLUSIVE or NOT_APPLIED
    detail: str  # why it failed, or what it read when it passed


def shifted(signal: Signal | None, value: Any) -> tuple[bool, Any]:
    """The value a ``shift`` reading gives the test, and whether it differs from ``value``."""
    if isinstance(value, bool):
        return True, not value
    if signal is not None and signal.type is SignalType.ENUM:
        states = list(signal.values)
        if value in states and len(states) > 1:
            return True, states[(states.index(value) + 1) % len(states)]
        return False, value
    if isinstance(value, numbers.Real):
        if not math.isfinite(value):
            return False, value
        moved = value + max(1, abs(value))
        return True, int(moved) if isinstance(value, numbers.Integral) else float(moved)
    return False, value


def verdict(normal: str, results: Sequence[ModeResult]) -> str:
    """The verdict on a test from its normal outcome and its results under each mode."""
    if normal != "passed":
        return NOT_CHECKED
    outcomes = {result.outcome for result in results}
    if FAILED in outcomes:
        return ABLE
    if INCONCLUSIVE in outcomes:
        return UNKNOWN
    if PASSED in outcomes:
        return UNABLE
    return NOT_CHECKED  # no mode applied: it read nothing in its body
