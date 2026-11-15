"""The DUT's identity for a run: its hardware revision and software versions.

The interface description lists the items each run is expected to collect, by name only, never
their values (``IdentityItem``). Values (``IdentityValue``) come from a DUT signal read before
the first test, from the team's code through ``bench.identify``, or from the command line. Any
other name can be added during the run. See "DUT identity" in docs/results-schema.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_WITHIN = 2.0  # seconds to wait for an identity signal before the first test

SIGNAL = "signal"
CODE = "code"
COMMAND_LINE = "command-line"
MISSING = "missing"

_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*")


@dataclass(frozen=True)
class IdentityItem:
    """An item the interface description expects each run to collect."""

    name: str
    signal: str | None = None  # a DUT output to read it from, before the first test
    within: float = DEFAULT_WITHIN


@dataclass(frozen=True)
class IdentityValue:
    """A value of an identity item, as collected during a run; or a missing expected item."""

    name: str
    value: str | None  # None only for a missing item
    source: str  # SIGNAL, CODE, COMMAND_LINE or MISSING
    expected: bool  # the interface description lists the item
    at: float | None  # time.time() when it was collected; None when missing


def name_problem(name: object) -> str | None:
    """Why ``name`` can't name an identity item, or None if it can.

    Names are letters, digits, ``_``, ``.`` and ``-``, such as ``bootloader`` or
    ``motor.firmware``; dots group related items.
    """
    if not isinstance(name, str) or not name:
        return "an identity item needs a name"
    if not _NAME.fullmatch(name):
        return (
            f"'{name}' can't name an identity item: use letters, digits, '_', '.' and '-', "
            "such as bootloader or motor.firmware"
        )
    return None
