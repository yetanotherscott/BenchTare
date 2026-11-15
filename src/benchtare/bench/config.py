"""The bench file: the equipment on the bench, with each item's backend and channels.

A bench file names no DUT signals, so one bench can be reused for any DUT. See docs/model.md.

    bench: laptop-sim
    equipment:
      can0:
        backend: sim_can
      io:
        backend: sim
        channels:
          out1: {drive: true}
          in1: {read: true}
"""

from __future__ import annotations

import difflib
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from benchtare._yaml import read_mapping
from benchtare.backends import BACKENDS
from benchtare.backends.base import ChannelCaps, EquipmentKind
from benchtare.errors import ConfigError, Problem

KIND = "bench file"

_TOP_KEYS = ("bench", "equipment")


@dataclass(frozen=True)
class EquipmentConfig:
    """One item of equipment as the bench file describes it."""

    id: str
    backend: str
    kind: EquipmentKind
    simulated: bool
    options: Mapping[str, Any]
    channels: Mapping[str, ChannelCaps]


@dataclass(frozen=True)
class BenchConfig:
    """A loaded bench file."""

    name: str
    path: Path
    equipment: Mapping[str, EquipmentConfig]


def load_bench_file(path: str | os.PathLike[str]) -> BenchConfig:
    """Load and check a bench file. Raise ``ConfigError`` listing every problem."""
    path = Path(path)
    data = read_mapping(path, kind=KIND)
    problems: list[Problem] = []

    for key in data:
        if key not in _TOP_KEYS:
            close = difflib.get_close_matches(str(key), _TOP_KEYS, n=1)
            problems.append(
                Problem(
                    f"unknown key '{key}' at the top level",
                    hint=f"did you mean '{close[0]}'?"
                    if close
                    else f"allowed keys: {', '.join(_TOP_KEYS)}",
                )
            )

    name = data.get("bench")
    if not isinstance(name, str) or not name.strip():
        problems.append(Problem("'bench:' must name the bench", hint="for example  bench: lab-1"))
        name = ""

    raw_equipment = data.get("equipment")
    equipment: dict[str, EquipmentConfig] = {}
    if not isinstance(raw_equipment, dict) or not raw_equipment:
        problems.append(
            Problem(
                "'equipment:' must list the bench's equipment",
                hint="for example  equipment: {can0: {backend: sim_can}}",
            )
        )
    else:
        for item_id, spec in raw_equipment.items():
            item = _load_equipment(str(item_id), spec, problems)
            if item is not None:
                equipment[item.id] = item

    if problems:
        raise ConfigError(path, problems, kind=KIND)
    return BenchConfig(name=name, path=path, equipment=MappingProxyType(equipment))


def _load_equipment(item_id: str, spec: Any, problems: list[Problem]) -> EquipmentConfig | None:
    where = f"equipment '{item_id}'"
    if not item_id or "." in item_id or not all(c.isalnum() or c in "_-" for c in item_id):
        problems.append(
            Problem(
                f"equipment name '{item_id}' can't be used",
                hint="use letters, digits, '_' and '-', with no dots; the harness file "
                "writes equipment.channel",
            )
        )
        return None
    if not isinstance(spec, dict):
        problems.append(Problem("must have properties, for example {backend: sim}", where=where))
        return None
    backend_name = spec.get("backend")
    backend = BACKENDS.get(backend_name) if isinstance(backend_name, str) else None
    if backend is None:
        known = ", ".join(sorted(BACKENDS))
        if backend_name is None:
            problems.append(Problem("has no 'backend:'", where=where, hint=f"backends: {known}"))
        else:
            close = difflib.get_close_matches(str(backend_name), list(BACKENDS), n=1)
            problems.append(
                Problem(
                    f"unknown backend '{backend_name}'",
                    where=where,
                    hint=f"did you mean '{close[0]}'?" if close else f"backends: {known}",
                )
            )
        return None
    options = {k: v for k, v in spec.items() if k != "backend"}
    before = len(problems)
    channels = backend.describe(item_id, options, problems)
    if len(problems) > before:
        return None
    return EquipmentConfig(
        id=item_id,
        backend=backend.name,
        kind=backend.kind,
        simulated=backend.simulated,
        options=MappingProxyType(options),
        channels=MappingProxyType(channels),
    )
