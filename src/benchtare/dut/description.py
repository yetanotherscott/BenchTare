"""The interface description: one YAML file per DUT listing every signal it exposes.

Signals can be listed directly, imported from DBC files, or both. See docs/model.md.

    dut: reference
    can:
      node: REFDEV            # the DUT's node name in the DBC files
      dbc: [reference.dbc]
    signals:
      supply_enable: {type: bool, direction: input}
      boot_count: {type: int, unit: none, direction: output}
    overrides:
      Setpoint: {unit: degC}  # adjust an imported signal
    standards: []             # reserved; no standard is available yet

Numeric signals must declare a unit. ``unit: none`` says a signal deliberately has none.
"""

from __future__ import annotations

import difflib
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

import cantools

from benchtare._yaml import YAML_VALUE_WORDS, count, kind_of, read_mapping
from benchtare.errors import ConfigError, Problem
from benchtare.signals.model import (
    NUMERIC_TYPES,
    Direction,
    Signal,
    SignalError,
    SignalSet,
    SignalType,
)

log = logging.getLogger(__name__)

KIND = "interface description"

# The unit to write for a numeric signal that has no unit.
NO_UNIT = "none"

_TOP_KEYS = ("dut", "can", "signals", "overrides", "standards")
_CAN_KEYS = ("node", "dbc")
_SIGNAL_KEYS = ("type", "direction", "unit", "values")
_OVERRIDE_KEYS = ("name", "unit", "direction")


@dataclass(frozen=True)
class CanSignalRef:
    """Where a DUT signal sits in a DBC file. Used by CAN backends to encode and decode it."""

    dbc: Path
    message: str  # unique within its DBC file; frame IDs need not be (standard vs extended)
    frame_id: int
    signal: str  # the name in the DBC; the DUT signal's name differs if an override renamed it
    extended: bool = False  # a 29-bit identifier
    fd: bool = False  # a CAN FD frame: marked FD in the DBC, or longer than 8 bytes
    multiplexed: bool = False  # the message is multiplexed

    @property
    def frame_key(self) -> tuple[int, bool]:
        """How a received frame is matched to its message: the ID and whether it is extended."""
        return (self.frame_id, self.extended)


@dataclass(frozen=True)
class InterfaceDescription:
    """A loaded interface description."""

    name: str
    path: Path
    signals: SignalSet
    can_node: str | None = None
    dbc_files: tuple[Path, ...] = ()
    can_signals: Mapping[str, CanSignalRef] = field(default_factory=dict)

    @property
    def files(self) -> tuple[Path, ...]:
        """Every file this description was built from, for the run manifest."""
        return (self.path, *self.dbc_files)


@dataclass
class _Candidate:
    """A signal before validation, so overrides can change it and all problems are collected."""

    name: str
    type: SignalType | None
    direction: Direction | None
    unit: str | None
    values: tuple[str, ...]
    where: str
    can_ref: CanSignalRef | None = None
    unit_declared: bool = False  # a unit was given, possibly 'none'

    @property
    def qualified(self) -> str | None:
        return f"{self.can_ref.message}.{self.can_ref.signal}" if self.can_ref else None


def load_interface_description(path: str | os.PathLike[str]) -> InterfaceDescription:
    """Load and check an interface description. Raise ``ConfigError`` listing every problem."""
    path = Path(path)
    data = read_mapping(path, kind=KIND)
    problems: list[Problem] = []

    _check_keys(data, _TOP_KEYS, "at the top level", problems)

    name = data.get("dut")
    if not isinstance(name, str) or not name.strip():
        problems.append(
            Problem("'dut:' must name the DUT", hint="add a line such as  dut: my-controller")
        )
        name = ""

    _check_standards(data.get("standards", []), problems)

    candidates: list[_Candidate] = []
    skipped: dict[str, str] = {}  # DBC signals not used by the DUT node, for override messages
    can_node, dbc_files = _load_can(data.get("can"), path, candidates, skipped, problems)
    _load_inline(data.get("signals", {}), path, candidates, problems)
    _apply_overrides(data.get("overrides", {}), candidates, skipped, can_node, problems)

    signals = _build_signals(candidates, problems)
    if not problems and not signals:
        problems.append(
            Problem(
                "the DUT has no signals",
                hint="list signals under 'signals:', or import them with 'can: dbc:'",
            )
        )
    if problems:
        raise ConfigError(path, problems, kind=KIND)

    can_signals = {c.name: c.can_ref for c in candidates if c.can_ref is not None}
    log.info(
        "loaded interface description %s: DUT %s, %s (%d from DBC files, %d listed inline)",
        path.name,
        name,
        count(len(signals), "signal"),
        len(can_signals),
        len(signals) - len(can_signals),
    )
    return InterfaceDescription(
        name=name,
        path=path,
        signals=SignalSet(signals),
        can_node=can_node,
        dbc_files=dbc_files,
        can_signals=MappingProxyType(can_signals),
    )


def _check_keys(
    section: Mapping[str, Any], allowed: tuple[str, ...], where: str, problems: list[Problem]
) -> None:
    for key in section:
        if key not in allowed:
            close = difflib.get_close_matches(str(key), allowed, n=1)
            hint = f"did you mean '{close[0]}'?" if close else f"allowed keys: {', '.join(allowed)}"
            problems.append(Problem(f"unknown key '{key}' {where}", hint=hint))


def _check_standards(standards: Any, problems: list[Problem]) -> None:
    if standards is None:
        return
    if not isinstance(standards, list):
        problems.append(Problem("'standards:' must be a list, for example  standards: []"))
        return
    for standard in standards:
        if not isinstance(standard, str):
            problems.append(
                Problem(
                    f"each entry must name a standard, not {kind_of(standard)}",
                    where="standards",
                )
            )
            continue
        problems.append(
            Problem(
                f"standard '{standard}' is not available in this version of BenchTare",
                where="standards",
                hint="remove it, and list the signals you need under 'signals:' "
                "or import them from a DBC file",
            )
        )


# --- CAN imports -------------------------------------------------------------------------------


def _load_can(
    section: Any,
    description_path: Path,
    candidates: list[_Candidate],
    skipped: dict[str, str],
    problems: list[Problem],
) -> tuple[str | None, tuple[Path, ...]]:
    if section is None:
        return None, ()
    if not isinstance(section, dict):
        problems.append(Problem("'can:' must contain 'node:' and 'dbc:' entries"))
        return None, ()
    _check_keys(section, _CAN_KEYS, "in 'can:'", problems)

    node = section.get("node")
    node_given = node is not None
    if node_given and (not isinstance(node, str) or not node.strip()):
        problems.append(
            Problem(
                f"must be the DUT's node name in the DBC, not {kind_of(node)}",
                where="can: node",
            )
        )
        node = None
    raw_files = section.get("dbc")
    if raw_files is None:
        problems.append(
            Problem(
                "'can:' has no 'dbc:' entry", hint="list the DBC files, for example dbc: [x.dbc]"
            )
        )
        return node if isinstance(node, str) else None, ()
    if isinstance(raw_files, str):
        raw_files = [raw_files]
    if not isinstance(raw_files, list) or not all(isinstance(f, str) for f in raw_files):
        problems.append(Problem("'can: dbc:' must be a file name or a list of file names"))
        return None, ()
    if not raw_files:
        problems.append(
            Problem("'can: dbc:' is an empty list", hint="list the DBC files, or remove 'can:'")
        )
        return None, ()

    databases: list[tuple[Path, Any]] = []
    listed: dict[Path, str] = {}
    for raw in raw_files:
        dbc_path = (description_path.parent / raw) if not Path(raw).is_absolute() else Path(raw)
        resolved = dbc_path.resolve()
        if resolved in listed:
            problems.append(
                Problem(
                    f"DBC file '{raw}' is listed more than once"
                    + (f" (also as '{listed[resolved]}')" if listed[resolved] != raw else ""),
                    where="can: dbc",
                    hint="list each DBC file once",
                )
            )
            continue
        listed[resolved] = raw
        database = _load_dbc(dbc_path, raw, problems)
        if database is not None:
            databases.append((dbc_path, database))

    if not isinstance(node, str) or not node:
        if node_given:
            return None, tuple(p for p, _ in databases)  # already reported
        nodes = sorted({n.name for _, db in databases for n in db.nodes})
        problems.append(
            Problem(
                "'can:' has no 'node:' entry",
                hint="set node to the DUT's node name in the DBC"
                + (f" (nodes found: {', '.join(nodes)})" if nodes else ""),
            )
        )
        return None, tuple(p for p, _ in databases)

    for dbc_path, database in databases:
        node_names = [n.name for n in database.nodes]
        if node not in node_names:
            problems.append(
                Problem(
                    f"node '{node}' is not in {dbc_path.name}",
                    where="can: node",
                    hint=f"nodes in {dbc_path.name}: {', '.join(node_names) or 'none'}",
                )
            )
            continue
        before = len(candidates)
        unused = _import_dbc(dbc_path, database, node, candidates, skipped)
        imported = len(candidates) - before
        if imported:
            log.info(
                "%s: imported %s from %s for node %s%s",
                description_path.name,
                count(imported, "signal"),
                dbc_path.name,
                node,
                f"; skipped {count(len(unused), 'signal')} it neither sends nor receives"
                if unused
                else "",
            )
            if unused:
                log.debug(
                    "%s: skipped from %s: %s",
                    description_path.name,
                    dbc_path.name,
                    ", ".join(unused),
                )
        else:
            problems.append(
                Problem(
                    f"imports no signals: node '{node}' neither sends nor receives any message "
                    f"in {dbc_path.name}",
                    where="can: dbc",
                    hint="the node name, and the senders and receivers in the DBC",
                )
            )

    return node, tuple(p for p, _ in databases)


def _load_dbc(dbc_path: Path, raw: str, problems: list[Problem]) -> Any:
    try:
        return cantools.database.load_file(str(dbc_path), database_format="dbc")
    except FileNotFoundError:
        problems.append(
            Problem(
                f"DBC file '{raw}' not found",
                where="can: dbc",
                hint=f"paths are relative to the interface description; it resolved to "
                f"{dbc_path.resolve()}",
            )
        )
    except OSError as exc:
        problems.append(
            Problem(
                f"can't read DBC file '{raw}': {exc.strerror or exc}",
                where="can: dbc",
                hint=f"it resolved to {dbc_path.resolve()}",
            )
        )
    except (cantools.database.Error, ValueError) as exc:
        problems.append(Problem(f"can't read DBC file '{raw}': {exc}", where="can: dbc"))
    return None


def _import_dbc(
    dbc_path: Path,
    database: Any,
    node: str,
    candidates: list[_Candidate],
    skipped: dict[str, str],
) -> list[str]:
    """Add the signals the DUT node sends or receives. Return the ones it doesn't use."""
    unused: list[str] = []
    for message in database.messages:
        sent_by_dut = node in message.senders
        for dbc_signal in message.signals:
            qualified = f"{message.name}.{dbc_signal.name}"
            if sent_by_dut:
                direction = Direction.OUTPUT
            elif node in dbc_signal.receivers:
                direction = Direction.INPUT
            else:
                skipped[qualified] = f"{dbc_path.name}, message {message.name}"
                unused.append(qualified)
                continue
            signal_type, values = _dbc_type(dbc_signal)
            dbc_unit = (dbc_signal.unit or "").strip()
            candidates.append(
                _Candidate(
                    name=dbc_signal.name,
                    type=signal_type,
                    direction=direction,
                    unit=None if dbc_unit.lower() in ("", NO_UNIT) else dbc_unit,
                    unit_declared=bool(dbc_unit),
                    values=values,
                    where=f"{dbc_path.name}, message {message.name}",
                    can_ref=CanSignalRef(
                        dbc=dbc_path,
                        message=message.name,
                        frame_id=message.frame_id,
                        signal=dbc_signal.name,
                        extended=bool(message.is_extended_frame),
                        fd=bool(message.is_fd) or message.length > 8,
                        multiplexed=bool(message.is_multiplexed()),
                    ),
                )
            )
    return unused


def _dbc_type(dbc_signal: Any) -> tuple[SignalType, tuple[str, ...]]:
    if dbc_signal.choices:
        # Real DBCs often give several codes one name, such as "Reserved". The signal lists each
        # name once; writing an ambiguous name is refused on the bus, and the raw code works.
        names = dict.fromkeys(str(v) for v in dbc_signal.choices.values())
        return SignalType.ENUM, tuple(names)
    scaled = not _integral(dbc_signal.scale) or not _integral(dbc_signal.offset)
    if dbc_signal.is_float or scaled:
        return SignalType.FLOAT, ()
    if dbc_signal.length == 1 and dbc_signal.scale == 1 and dbc_signal.offset == 0:
        return SignalType.BOOL, ()
    return SignalType.INT, ()


def _integral(number: float) -> bool:
    return float(number).is_integer()


# --- Inline signals ----------------------------------------------------------------------------


def _load_inline(
    section: Any, path: Path, candidates: list[_Candidate], problems: list[Problem]
) -> None:
    if section is None:
        return
    if not isinstance(section, dict):
        problems.append(
            Problem(
                "'signals:' must map each signal name to its properties",
                hint="for example  signals: {supply_ok: {type: bool, direction: output}}",
            )
        )
        return
    where = f"{path.name}, signals:"
    for name, spec in section.items():
        if not isinstance(name, str):
            problems.append(_name_read_as_value(name, where))
            continue
        if not isinstance(spec, dict):
            problems.append(
                Problem(
                    "must have properties, for example {type: float, unit: V, direction: input}",
                    signal=str(name),
                    where=where,
                )
            )
            continue
        before = len(problems)
        for key in spec:
            if key not in _SIGNAL_KEYS:
                problems.append(
                    Problem(
                        f"unknown property '{key}'",
                        signal=str(name),
                        where=where,
                        hint=f"allowed properties: {', '.join(_SIGNAL_KEYS)}",
                    )
                )
        signal_type = _parse_type(spec.get("type"), str(name), where, problems)
        direction = _parse_direction(spec.get("direction"), str(name), where, problems)
        unit, unit_declared = _parse_unit(spec.get("unit"), str(name), where, problems)
        values = _parse_values(spec.get("values"), str(name), where, problems)
        if len(problems) == before:
            candidates.append(
                _Candidate(
                    name=name,
                    type=signal_type,
                    direction=direction,
                    unit=unit,
                    unit_declared=unit_declared,
                    values=values,
                    where=where,
                )
            )


def _parse_type(raw: Any, name: str, where: str, problems: list[Problem]) -> SignalType | None:
    allowed = ", ".join(t.value for t in SignalType)
    if raw is None:
        problems.append(
            Problem("has no type", signal=name, where=where, hint=f"set type to one of: {allowed}")
        )
        return None
    if not isinstance(raw, str):
        problems.append(
            Problem(f"type must be one of {allowed}, not {kind_of(raw)}", signal=name, where=where)
        )
        return None
    try:
        return SignalType(raw)
    except ValueError:
        problems.append(
            Problem(
                f"unknown type '{raw}'",
                signal=name,
                where=where,
                hint=_suggest(raw, [t.value for t in SignalType]) or f"use one of: {allowed}",
            )
        )
        return None


def _parse_direction(
    raw: Any, name: str, where: str, problems: list[Problem], *, required: bool = True
) -> Direction | None:
    hint = "input (the bench drives it) or output (the bench reads it), from the DUT's view"
    if raw is None:
        if required:
            problems.append(Problem("has no direction", signal=name, where=where, hint=hint))
        return None
    if not isinstance(raw, str):
        problems.append(
            Problem(
                f"direction must be input or output, not {kind_of(raw)}",
                signal=name,
                where=where,
                hint=hint,
            )
        )
        return None
    try:
        return Direction(raw)
    except ValueError:
        suggestion = _suggest(raw, [d.value for d in Direction])
        problems.append(
            Problem(
                f"unknown direction '{raw}'",
                signal=name,
                where=where,
                hint=f"{suggestion} It is {hint}" if suggestion else hint,
            )
        )
        return None


def _suggest(raw: str, allowed: list[str]) -> str | None:
    """'did you mean ...?' for a near miss, including a difference only in capitals."""
    for option in allowed:
        if raw.strip().lower() == option:
            return f"did you mean '{option}'? (written in lower case)"
    close = difflib.get_close_matches(raw.strip().lower(), allowed, n=1, cutoff=0.5)
    return f"did you mean '{close[0]}'?" if close else None


def _name_read_as_value(name: Any, where: str) -> Problem:
    """A key that YAML turned into a value: an unquoted on/off/yes/no/null, or a number."""
    return Problem(
        f"a signal name was read as {kind_of(name)}, not as a name",
        where=where,
        hint=f"quote it, for example 'on': {{...}}; {YAML_VALUE_WORDS}",
    )


def _parse_unit(
    raw: Any, name: str, where: str, problems: list[Problem]
) -> tuple[str | None, bool]:
    """Return (unit, declared). ``unit: none`` declares that a signal has no unit."""
    if raw is None:
        return None, False
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)) or not str(raw).strip():
        problems.append(
            Problem(
                f"unit must be text, for example unit: V, or unit: {NO_UNIT} if it has none",
                signal=name,
                where=where,
            )
        )
        return None, True  # reported here, so don't report it again as missing
    text = str(raw).strip()
    if text.lower() == NO_UNIT:
        return None, True
    return text, True


def _parse_values(raw: Any, name: str, where: str, problems: list[Problem]) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        problems.append(
            Problem(
                "values must be a list, for example values: [standby, run]",
                signal=name,
                where=where,
            )
        )
        return ()
    not_text = [v for v in raw if not isinstance(v, str)]
    if not_text:
        problems.append(
            Problem(
                f"enum values must be text, but got {', '.join(repr(v) for v in not_text)}",
                signal=name,
                where=where,
                hint="quote them, for example values: ['off', 'on']; YAML reads unquoted "
                "on, off, yes and no as true and false",
            )
        )
        return ()
    return tuple(raw)


# --- Overrides ---------------------------------------------------------------------------------


def _apply_overrides(
    section: Any,
    candidates: list[_Candidate],
    skipped: dict[str, str],
    can_node: str | None,
    problems: list[Problem],
) -> None:
    if section is None:
        return
    if not isinstance(section, dict):
        problems.append(
            Problem(
                "'overrides:' must map each imported signal to the properties to change",
                hint="for example  overrides: {Setpoint: {unit: degC}}",
            )
        )
        return
    where = "overrides:"
    for key, spec in section.items():
        if not isinstance(key, str):
            problems.append(_name_read_as_value(key, where))
            continue
        if not isinstance(spec, dict):
            problems.append(
                Problem(
                    "must list the properties to change, for example {unit: V}",
                    signal=key,
                    where=where,
                )
            )
            continue
        target = _find_override_target(key, candidates, skipped, can_node, problems)
        if target is None:
            continue
        for prop in spec:
            if prop not in _OVERRIDE_KEYS:
                problems.append(
                    Problem(
                        f"can't override '{prop}'",
                        signal=key,
                        where=where,
                        hint=f"an override can change: {', '.join(_OVERRIDE_KEYS)}",
                    )
                )
        if "name" in spec:
            new_name = spec["name"]
            if not isinstance(new_name, str):
                problems.append(Problem("the new name must be text", signal=key, where=where))
            else:
                target.name = new_name
        if "unit" in spec:
            unit, declared = _parse_unit(spec["unit"], key, where, problems)
            if declared:
                target.unit, target.unit_declared = unit, True
            else:
                problems.append(
                    Problem(
                        f"'unit:' has no value; give a unit, or {NO_UNIT} if it has none",
                        signal=key,
                        where=where,
                    )
                )
        if "direction" in spec:
            direction = _parse_direction(spec["direction"], key, where, problems, required=False)
            if direction is not None:
                target.direction = direction


def _find_override_target(
    key: str,
    candidates: list[_Candidate],
    skipped: dict[str, str],
    can_node: str | None,
    problems: list[Problem],
) -> _Candidate | None:
    where = "overrides:"
    if "." in key:
        matches = [c for c in candidates if c.qualified == key]
    else:
        matches = [c for c in candidates if c.can_ref is not None and c.can_ref.signal == key]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        problems.append(
            Problem(
                "matches more than one imported signal: "
                + ", ".join(c.qualified or c.name for c in matches),
                signal=key,
                where=where,
                hint=f"name one with Message.Signal, for example {matches[0].qualified}: {{...}}",
            )
        )
        return None

    inline = [c for c in candidates if c.can_ref is None and c.name == key]
    if inline:
        problems.append(
            Problem(
                "is listed under 'signals:', so change it there instead",
                signal=key,
                where=where,
            )
        )
        return None
    unused = [q for q in skipped if q == key or q.split(".", 1)[1] == key]
    if unused:
        problems.append(
            Problem(
                f"is in {skipped[unused[0]]}, but the DUT node '{can_node}' neither sends nor "
                "receives it, so it isn't imported",
                signal=key,
                where=where,
                hint="the message's sender and the signal's receivers in the DBC",
            )
        )
        return None
    names = [c.can_ref.signal for c in candidates if c.can_ref] + [
        c.qualified for c in candidates if c.qualified
    ]
    close = difflib.get_close_matches(key, names, n=3)
    problems.append(
        Problem(
            "no imported signal has this name",
            signal=key,
            where=where,
            hint=f"did you mean {', '.join(close)}?" if close else "the spelling",
        )
    )
    return None


# --- Building ----------------------------------------------------------------------------------


def _build_signals(candidates: list[_Candidate], problems: list[Problem]) -> list[Signal]:
    signals: list[Signal] = []
    by_name: dict[str, _Candidate] = {}
    for candidate in candidates:
        first = by_name.get(candidate.name)
        if first is not None:
            problems.append(_duplicate_problem(first, candidate))
            continue
        by_name[candidate.name] = candidate
        if candidate.type is None or candidate.direction is None:
            continue  # already reported
        if candidate.type in NUMERIC_TYPES and not candidate.unit_declared:
            problems.append(_missing_unit_problem(candidate))
            continue
        try:
            signals.append(
                Signal(
                    name=candidate.name,
                    type=candidate.type,
                    direction=candidate.direction,
                    unit=candidate.unit,
                    values=candidate.values,
                    source=candidate.where,
                )
            )
        except SignalError as exc:
            problems.append(
                Problem(
                    str(exc),
                    signal=str(candidate.name),
                    where=candidate.where,
                    hint=_hint_for(candidate, exc),
                )
            )
    return signals


def _missing_unit_problem(candidate: _Candidate) -> Problem:
    assert candidate.type is not None
    if candidate.can_ref is None:
        hint = f"add a unit, for example unit: V, or unit: {NO_UNIT} if it has none"
    else:
        hint = (
            "the DBC gives no unit; set one in overrides, for example  "
            f"{candidate.qualified}: {{unit: {NO_UNIT}}}  or  {{unit: V}}"
        )
    return Problem(
        f"{candidate.type.value} signal has no unit",
        signal=str(candidate.name),
        where=candidate.where,
        hint=hint,
    )


def _hint_for(candidate: _Candidate, exc: SignalError) -> str | None:
    if candidate.can_ref is None:
        return exc.hint
    if exc.field == "name":
        return f"rename it in overrides, for example  {candidate.qualified}: {{name: NewName}}"
    return exc.hint


def _duplicate_problem(first: _Candidate, second: _Candidate) -> Problem:
    example = second.qualified or first.qualified
    hint = "remove one, or rename one"
    if example:
        hint += f" with an override, for example  {example}: {{name: NewName}}"
    return Problem(
        f"defined more than once: in {first.where} and in {second.where}",
        signal=str(second.name),
        hint=hint,
    )
