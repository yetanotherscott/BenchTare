"""The test record: what happened at the bench during each test, kept with the test's result.

The record is evidence about the DUT. It is separate from BenchTare's own log (Python
``logging`` under ``benchtare``), which is for debugging BenchTare itself. Each has its own
verbosity. See "The test record" in docs/model.md.

Levels, each including the ones before it:

- ``results``: every value read, every comparison (``wait_until``, and passing ``assert``
  statements when pytest's ``enable_assertion_pass_hook`` is on), and each phase's outcome.
- ``stimuli``: also every value written. The default.
- ``debug``: also every frame the bench sent and received.

Anything unexpected the bench saw, such as a frame that doesn't match the DBC or equipment that
stopped working, is recorded at every level, so no setting can hide it.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class RecordLevel(IntEnum):
    RESULTS = 1
    STIMULI = 2
    DEBUG = 3

    @classmethod
    def named(cls, name: str) -> RecordLevel:
        """The level called ``name`` (``results``, ``stimuli`` or ``debug``)."""
        try:
            return cls[name.strip().upper()]
        except KeyError:
            raise ValueError(
                f"unknown test record level '{name}'; the levels are {', '.join(LEVEL_NAMES)}"
            ) from None

    @property
    def label(self) -> str:
        return self.name.lower()


LEVEL_NAMES = tuple(level.label for level in RecordLevel)
DEFAULT_LEVEL = RecordLevel.STIMULI

# What an entry records.
READ = "read"
WAIT = "wait_until"
ASSERT = "assert"
RESULT = "result"
WRITE = "write"
FRAME_MISMATCH = "frame_mismatch"
EQUIPMENT_FAILURE = "equipment_failure"
FRAME_SENT = "frame_sent"
FRAME_RECEIVED = "frame_received"

UNEXPECTED = frozenset({FRAME_MISMATCH, EQUIPMENT_FAILURE})


@dataclass(frozen=True)
class RecordEntry:
    """One thing that happened at the bench.

    ``text`` is a line for people to read; ``data`` holds the same facts as plain values, for
    the results database.
    """

    kind: str
    level: RecordLevel
    text: str
    data: Mapping[str, Any] = field(default_factory=dict)
    at: float = field(default_factory=time.time)

    @property
    def unexpected(self) -> bool:
        """Something the bench saw that a test may not have checked for. Never filtered out."""
        return self.kind in UNEXPECTED

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "level": self.level.label,
            "at": self.at,
            "text": self.text,
            **self.data,
        }


def render(entries: Iterable[RecordEntry]) -> str:
    """The record as lines of text, each starting with the wall-clock time."""
    return "\n".join(f"{clock(entry.at)} {entry.text}" for entry in entries)


def clock(at: float) -> str:
    """``14:03:07.215``: local time to the millisecond, to line up with a bus trace."""
    seconds, millis = divmod(round(at * 1000), 1000)
    return time.strftime("%H:%M:%S", time.localtime(seconds)) + f".{millis:03d}"
