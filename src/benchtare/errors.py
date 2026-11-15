"""Errors BenchTare reports to the person at the bench.

Every message says what failed, in which file and for which signal, and what to check.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


class BenchTareError(Exception):
    """Base class for errors that are reported to the user rather than as tracebacks."""


@dataclass(frozen=True)
class Problem:
    """One thing wrong with a file, and what to check."""

    message: str
    signal: str | None = None
    where: str | None = None
    hint: str | None = None

    def __str__(self) -> str:
        subject = ""
        if self.signal is not None:
            subject = f"signal '{self.signal}'"
            if self.where:
                subject += f" ({self.where})"
            subject += ": "
        elif self.where:
            # "signals:" is already followed by its colon; don't print "signals::".
            subject = self.where + (" " if self.where.endswith(":") else ": ")
        text = subject + self.message
        if self.hint:
            text += f"\n      Check: {self.hint}"
        return text


class ConfigError(BenchTareError):
    """A file a team writes (interface description, bench file, harness file) is invalid.

    All problems found in the file are reported together, so they can be fixed in one pass.
    """

    def __init__(self, path: Path | str, problems: Sequence[Problem], *, kind: str) -> None:
        if not problems:
            raise ValueError("ConfigError needs at least one problem")
        self.path = Path(path)
        self.problems = tuple(problems)
        self.kind = kind
        super().__init__(str(self))

    def __str__(self) -> str:
        count = len(self.problems)
        noun = "problem" if count == 1 else "problems"
        lines = [f"{self.path}: {count} {noun} in the {self.kind}"]
        lines += [f"  - {problem}" for problem in self.problems]
        return "\n".join(lines)
