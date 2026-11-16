"""The test check: proving that each test can fail at every verification step.

A test that passes whatever the DUT does proves nothing, and neither does a test that fails at
its first check but would pass whatever its later checks read. The test check (``pytest
--check-tests``, or ``benchtare check-tests``) walks through each test that passed, one reading
at a time. Each ``read`` and ``wait_until`` in the test's body is a verification step;
``check=False`` marks one the test deliberately doesn't check.

1. **The control.** The test runs again with nothing corrupted. It must pass and take the same
   readings in the same order; otherwise it isn't repeatable, and nothing the check finds about
   it could be trusted.
2. **Each reading, under each mode.** The test runs again with only that reading corrupted, so
   every earlier step reads real values and the test gets as far as the corrupted one:

   - ``shift``: the DUT reports a wrong value. A number moves by its own size (at least 1), a
     boolean is inverted, and an enum takes the next of its states.
   - ``stale``: the DUT ignored the command. The reading gives the value from before the
     bench's most recent write, which a DUT that never responded would still be reporting.
     When that is the same as what arrived, stale changes nothing at that step, and says so.

A failure counts only if it is the corrupted reading's doing: it happened at that step or after
it, never before, and something was corrupted. A reading can fail if some mode makes the test
fail; a test can fail only if every checked reading can. Staging (setup) and teardown always
read the real values. Nothing here judges whether a value is valid, so no ranges are needed.

The test check verifies test logic only. A test that shows it doesn't repeat (the control fails
or takes other readings, staging fails on a rerun, or a rerun fails before its corrupted step
or with nothing corrupted) has a test hygiene problem: its logic is "not checked", the walk
stops, and none of its reruns count, since a failure might not be real. A rerun that ends
because nothing arrived is inconclusive. See "What BenchTare verifies" in docs/model.md.

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
CONTROL = "control"  # the rerun with nothing corrupted

# What happened in one rerun.
FAILED = "failed"  # the test failed because of the corrupted reading: it can catch this fault
PASSED = "passed"  # it still passed (for the control: it repeats)
INCONCLUSIVE = "inconclusive"  # the rerun proves nothing about the reading
NOT_APPLIED = "not applied"  # this mode couldn't corrupt the reading
NOT_REPEATABLE = "not repeatable"  # the test didn't repeat: a test hygiene problem

# The verdict on a reading, or on a test.
ABLE = "able to fail"
UNABLE = "unable to fail"
UNKNOWN = "inconclusive"
NOT_CHECKED = "not checked"  # it didn't pass to begin with, or took no reading to corrupt


@dataclass
class Reading:
    """A reading the test took in its body: one of its verification steps."""

    index: int | None  # its number among the test's checked readings, from 1; None if unchecked
    signal: str
    where: str  # the test's file and line that took it, such as "test_x.py:12"
    checked: bool = True  # False when taken with check=False
    corrupted: bool = False  # the test check gave the test a different value from what arrived
    note: str = ""  # why the test check's mode couldn't change it, when it couldn't
    # For test hygiene: a wait already satisfied by the value from before the most recent write
    # (that value, and which write).
    waits: bool = False  # a wait_until, which says the test expects the DUT to react
    already: bool = False
    before: Any = None
    after_write: str = ""


@dataclass(frozen=True)
class ModeResult:
    """One rerun of a test: the control (``reading`` 0), or one reading under one mode."""

    reading: int  # the checked reading corrupted, from 1; 0 for the control
    signal: str  # its signal; empty for the control
    where: str  # the test's line that took it; empty for the control
    mode: str  # CONTROL, SHIFT or STALE
    outcome: str  # FAILED, PASSED, INCONCLUSIVE, NOT_APPLIED or NOT_REPEATABLE
    detail: str  # where and why it failed, or what it read when it passed


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


def by_reading(results: Sequence[ModeResult]) -> dict[int, list[ModeResult]]:
    """Each corrupted reading's results, in the test's order; the control is left out."""
    grouped: dict[int, list[ModeResult]] = {}
    for result in sorted(results, key=lambda r: r.reading):
        if result.mode != CONTROL:
            grouped.setdefault(result.reading, []).append(result)
    return grouped


def reading_verdict(results: Sequence[ModeResult]) -> str:
    """The verdict on one reading: able to fail if some mode made the test fail."""
    outcomes = {result.outcome for result in results}
    if FAILED in outcomes:
        return ABLE
    if INCONCLUSIVE in outcomes:
        return UNKNOWN
    if PASSED in outcomes:
        return UNABLE
    return NOT_CHECKED  # no mode could corrupt it


def verdict(normal: str, results: Sequence[ModeResult]) -> str:
    """The verdict on a test: able to fail only if every checked reading can."""
    if normal != "passed":
        return NOT_CHECKED
    if any(r.outcome == NOT_REPEATABLE for r in results):
        return NOT_CHECKED  # test hygiene: no rerun of it proves anything about its logic
    verdicts = {reading_verdict(group) for group in by_reading(results).values()}
    for decisive in (UNABLE, UNKNOWN, ABLE):
        if decisive in verdicts:
            return decisive
    return NOT_CHECKED  # no reading, or none that could be corrupted
