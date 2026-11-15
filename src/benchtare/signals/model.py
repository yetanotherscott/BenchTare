"""Signals: named values on the DUT.

A signal has a name, a type, a unit and a direction. It has no valid range: what counts as a
correct value is decided by a test's assertions. A signal knows nothing about buses or channels.
See docs/model.md.
"""

from __future__ import annotations

import difflib
import keyword
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from enum import Enum


class SignalError(ValueError):
    """A signal definition is invalid. The loader adds the file it came from.

    ``field`` names the property at fault (``"name"``, ``"unit"``, ``"values"``, ...), so a
    loader can give a hint that fits where the signal came from.
    """

    def __init__(
        self,
        message: str,
        *,
        signal: str | None = None,
        field: str | None = None,
        hint: str | None = None,
    ) -> None:
        self.signal = signal
        self.field = field
        self.hint = hint
        super().__init__(message)


class SignalType(str, Enum):
    FLOAT = "float"
    INT = "int"
    BOOL = "bool"
    ENUM = "enum"


class Direction(str, Enum):
    """Direction from the DUT's point of view."""

    INPUT = "input"  # the bench drives it
    OUTPUT = "output"  # the bench reads it


NUMERIC_TYPES = frozenset({SignalType.FLOAT, SignalType.INT})


@dataclass(frozen=True)
class Signal:
    """A named value on the DUT.

    ``unit`` is None for a signal with no unit. The interface description makes every numeric
    signal declare its unit, or ``none``, so that a forgotten unit is caught there.

    ``source`` says where the signal was defined, for error messages, for example
    ``"reference.dbc, message Status"``.
    """

    name: str
    type: SignalType
    direction: Direction
    unit: str | None = None
    values: tuple[str, ...] = ()
    source: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        _check_name(self.name)
        if not isinstance(self.type, SignalType):
            raise SignalError(
                f"type must be a SignalType, got {self.type!r}", signal=self.name, field="type"
            )
        if not isinstance(self.direction, Direction):
            raise SignalError(
                f"direction must be a Direction, got {self.direction!r}",
                signal=self.name,
                field="direction",
            )
        if self.unit is not None and (not isinstance(self.unit, str) or not self.unit.strip()):
            raise SignalError(
                f"unit must be text or None, got {self.unit!r}", signal=self.name, field="unit"
            )
        if self.type is SignalType.ENUM:
            if not self.values:
                raise SignalError(
                    "enum signal has no values",
                    signal=self.name,
                    field="values",
                    hint="list its values, for example values: [standby, run]",
                )
            duplicates = sorted({v for v in self.values if self.values.count(v) > 1})
            if duplicates:
                raise SignalError(
                    f"enum value listed more than once: {', '.join(duplicates)}",
                    signal=self.name,
                    field="values",
                )
        elif self.values:
            raise SignalError(
                f"only enum signals have values, but this is a {self.type.value} signal",
                signal=self.name,
                field="values",
            )


def _check_name(name: str) -> None:
    if not isinstance(name, str) or not name.isidentifier() or keyword.iskeyword(name):
        raise SignalError(
            f"name {name!r} can't be used as a signal name",
            signal=str(name),
            field="name",
            hint="tests write signals.<name>, so names must be valid Python identifiers "
            "and not keywords; rename it with an override",
        )
    if name.startswith("_") or name in RESERVED_NAMES:
        raise SignalError(
            f"name {name!r} is reserved",
            signal=name,
            field="name",
            hint="names starting with '_' and the names "
            f"{', '.join(sorted(RESERVED_NAMES))} are reserved; rename it with an override",
        )


class SignalSet(Mapping[str, Signal]):
    """The signals of one DUT, by name. Tests use attribute access: ``signals.Setpoint``."""

    def __init__(self, signals: Iterable[Signal] = ()) -> None:
        by_name: dict[str, Signal] = {}
        for signal in signals:
            existing = by_name.get(signal.name)
            if existing is not None:
                raise SignalError(
                    f"defined twice: in {existing.source or '?'} and in {signal.source or '?'}",
                    signal=signal.name,
                    hint="remove one, or rename one with an override",
                )
            by_name[signal.name] = signal
        self._by_name = by_name

    def __getitem__(self, name: str) -> Signal:
        try:
            return self._by_name[name]
        except KeyError:
            raise KeyError(self._unknown_message(name)) from None

    def __getattr__(self, name: str) -> Signal:
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self._by_name[name]
        except KeyError:
            raise AttributeError(self._unknown_message(name)) from None

    def __iter__(self) -> Iterator[str]:
        return iter(self._by_name)

    def __len__(self) -> int:
        return len(self._by_name)

    def __dir__(self) -> list[str]:
        return sorted(set(super().__dir__()) | set(self._by_name))

    def __repr__(self) -> str:
        return f"SignalSet({', '.join(self._by_name)})"

    def _unknown_message(self, name: str) -> str:
        message = f"the DUT has no signal named '{name}'"
        close = difflib.get_close_matches(name, list(self._by_name), n=3)
        if close:
            message += f"; did you mean {', '.join(repr(c) for c in close)}?"
        return message


# Attribute names a signal can't use, because SignalSet already has them.
RESERVED_NAMES = frozenset(n for n in dir(SignalSet) if not n.startswith("_"))
