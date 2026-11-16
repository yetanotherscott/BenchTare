"""Test hygiene: proving that each test stands on its own.

The hygiene check (``pytest --check-hygiene``, or ``benchtare check-hygiene``) runs the tests as
usual, then checks each test that passed against three rules, each with its own verdict:

- **It repeats.** Run again straight after itself, it passes and takes the same readings in
  the same order.
- **It leaves the DUT clean.** After each run's teardown, every item the interface
  description's ``clean_state:`` lists is clean: at its clean value, where the team gives one,
  or otherwise back at its value from before that run's setup. Only those items are compared:
  the team says what shows the DUT is clean. Only the test that left an item dirty is blamed:
  one that finds an item dirty and leaves it so isn't, and one that puts it back is thanked.
- **It starts from a meaningful state.** No checked ``wait_until`` in its body was already
  satisfied by the value from before the test's most recent write, so the test can tell
  whether the DUT responded.

A ``clean_state:`` item is a DUT output (its value before setup is the latest one received),
a DUT input (the value the bench last wrote), or a state probe: state no signal shows, which
the team's ``pytest_benchtare_state`` hook returns. A ``tolerance:`` says how far apart two
numbers may be and still be the same, for a noisy reading; it is the team's definition of
"the same", not a pass criterion. A ``settle:`` gives an output or a probe time to come back
after teardown.

This verifies test hygiene only: the bench and harness are the self-test's, and test logic is
the test check's. See "What BenchTare verifies" in docs/model.md.
"""

from __future__ import annotations

import math
import numbers
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

DEFAULT_SETTLE = 1.0  # seconds an item has to come back after teardown

# What a clean_state: item is.
OUTPUT = "output"
INPUT = "input"
PROBE = "probe"

# The rules, by the short name results are stored under, and as the report says them.
REPEATS = "repeats"
CLEAN = "clean"
MEANINGFUL = "meaningful start"
RULES = (REPEATS, CLEAN, MEANINGFUL)
SAID = {
    REPEATS: "it repeats",
    CLEAN: "it leaves the DUT clean",
    MEANINGFUL: "it starts from a meaningful state",
}

# A rule's outcome.
PASSED = "passed"
FAILED = "failed"
NOT_CHECKED = "not checked"

# A clean_state: item's outcome in one run.
SAME = "same"  # clean after teardown
CHANGED = "changed"  # this run left it dirty
INHERITED = "left by an earlier test"  # dirty before setup, and this run didn't change it
NOT_COMPARED = "not compared"

# The verdict on a test.
IS_CLEAN = "clean"
NOT_CLEAN = "not clean"


class _Missing:
    """No value: an output that never arrived, an input never written, a probe that didn't
    return the name."""

    def __repr__(self) -> str:
        return "nothing"


MISSING: Any = _Missing()


@dataclass(frozen=True)
class CleanStateItem:
    """One ``clean_state:`` item of an interface description."""

    name: str
    kind: str  # OUTPUT, INPUT or PROBE
    tolerance: float = 0.0
    settle: float = DEFAULT_SETTLE
    clean: Any = MISSING  # its clean value, where the team knows it

    @property
    def has_clean(self) -> bool:
        return self.clean is not MISSING


@dataclass(frozen=True)
class ItemResult:
    """One ``clean_state:`` item, compared before setup and after teardown of one run."""

    run: int  # 1 for the usual run, 2 for the repeat
    name: str
    kind: str
    before: Any
    after: Any
    outcome: str  # SAME, CHANGED or NOT_COMPARED
    detail: str


@dataclass(frozen=True)
class RuleResult:
    """One rule's outcome for one test."""

    rule: str  # REPEATS, CLEAN or MEANINGFUL
    outcome: str  # PASSED, FAILED or NOT_CHECKED
    detail: str


def same(before: Any, after: Any, tolerance: float = 0.0) -> bool:
    """Whether two values are the same: numbers within ``tolerance``, anything else equal."""
    if before is MISSING or after is MISSING:
        return before is after
    if _number(before) and _number(after):
        if math.isnan(before) or math.isnan(after):
            return math.isnan(before) and math.isnan(after)
        return abs(after - before) <= tolerance
    return bool(before == after)


def come_back(
    read: Callable[[], Any],
    before: Any,
    item: CleanStateItem,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    poll: float = 0.05,
) -> tuple[bool, Any]:
    """Read an item until it is the same as ``before``, for up to its settle time. Return
    whether it came back, and the last value read."""
    deadline = clock() + item.settle
    while True:
        value = read()
        if same(before, value, item.tolerance):
            return True, value
        remaining = deadline - clock()
        if remaining <= 0:
            return False, value
        sleep(min(poll, remaining))


def judge(
    item: CleanStateItem,
    before: Any,
    read: Callable[[], Any],
    *,
    reference: Any = MISSING,
    unknown: str = "its value isn't known",
    shown: Callable[[Any], str] = repr,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[str, Any, str]:
    """Whether one run left a clean_state: item clean. Return the outcome, the value after
    teardown, and what to say.

    With a clean value, the item must be at it after teardown, given its settle time. Without
    one, it must be back at ``before``, its value before setup, or at ``reference``, the value
    the session first found it at. Either way, a run that found the item dirty and didn't
    change it isn't blamed: an earlier test left it so (``INHERITED``). ``unknown`` says why a
    value that is missing isn't known.
    """
    name, tolerance = item.name, item.tolerance
    late = f" {item.settle:g} s" if item.settle else ""
    if item.has_clean:
        back, after = come_back(read, item.clean, item, clock=clock, sleep=sleep)
        if after is MISSING:
            return NOT_COMPARED, after, f"{name}: {unknown}"
        clean = shown(item.clean)
        dirty_before = before is not MISSING and not same(before, item.clean, tolerance)
        if back:
            note = (
                f", though an earlier test had left it at {shown(before)}" if dirty_before else ""
            )
            return SAME, after, f"{name} was {shown(after)} after teardown, its clean value{note}"
        if dirty_before and same(before, after, tolerance):
            return (
                INHERITED,
                after,
                (
                    f"{name} was already {shown(before)} before setup, not its clean value {clean}: "
                    "an earlier test left it so, and this test didn't change it"
                ),
            )
        apart = f", more than {tolerance:g} from it" if tolerance else ""
        was = f" (it was {shown(before)} before setup)" if before is not MISSING else ""
        return (
            CHANGED,
            after,
            (
                f"{name} was still {shown(after)}{late} after teardown, not its clean value "
                f"{clean}{apart}{was}"
            ),
        )
    if before is MISSING:
        return NOT_COMPARED, MISSING, f"{name}: {unknown}"
    back, after = come_back(read, before, item, clock=clock, sleep=sleep)
    if back:
        return (
            SAME,
            after,
            f"{name} was {shown(before)} before setup, and {shown(after)} after teardown",
        )
    if (
        reference is not MISSING
        and not same(before, reference, tolerance)
        and same(after, reference, tolerance)
    ):
        return (
            SAME,
            after,
            (
                f"{name} was {shown(after)} after teardown, as the session first found it; an earlier "
                f"test had left it at {shown(before)}"
            ),
        )
    apart = f", more than {tolerance:g} apart" if tolerance else ""
    return (
        CHANGED,
        after,
        (
            f"{name} was {shown(before)} before setup, and still {shown(after)}{late} after "
            f"teardown{apart}"
        ),
    )


def verdict(normal: str, rules: Sequence[RuleResult]) -> str:
    """The verdict on a test: clean only if it passed and broke no rule that was checked."""
    if normal != "passed" or all(rule.outcome == NOT_CHECKED for rule in rules):
        return NOT_CHECKED
    if any(rule.outcome == FAILED for rule in rules):
        return NOT_CLEAN
    return IS_CLEAN


def _number(value: Any) -> bool:
    return isinstance(value, numbers.Real) and not isinstance(value, bool)
