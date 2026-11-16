"""The harness file: what connects the bench to the DUT. See docs/model.md.

    harness: reference-on-laptop
    connections:              # signal: equipment[.channel]
      Setpoint: can0
      supply_enable: io.out1
    loopbacks:                # a bench output wired straight back to a bench input
      - {drive: io.out4, read: io.in4}
    presence:                 # how to tell the DUT is connected and alive
      - {bus: can0, within: 1.0}
      - {signal: Mode, within: 2.0}
    simulate: reference       # only for simulated setups: the DUT model to run

``simulate:`` names a DUT model (``benchtare.sim.model.DutModel``): one that ships with
BenchTare, such as ``reference``; or a team's own, as ``models/my_dut.py:MyDut``, a Python
file relative to the harness file and the class in it; or, with options for the model::

    simulate:
      model: models/my_dut.py:MyDut
      options: {boot_delay: 0.5}

The harness is checked against the interface description and the bench file when it loads.
"""

from __future__ import annotations

import difflib
import hashlib
import importlib.util
import logging
import os
import sys
import traceback
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from benchtare._yaml import YAML_VALUE_WORDS, count, kind_of, read_mapping
from benchtare.backends import BACKENDS
from benchtare.bench.config import BenchConfig, EquipmentConfig
from benchtare.dut.description import InterfaceDescription
from benchtare.errors import ConfigError, Problem
from benchtare.signals.model import Direction, Signal
from benchtare.sim.model import DutModel
from benchtare.sim.registry import SIMULATED_DUTS

log = logging.getLogger(__name__)

KIND = "harness file"

_TOP_KEYS = ("harness", "connections", "loopbacks", "presence", "simulate")


@dataclass(frozen=True)
class Endpoint:
    """A place on the bench: an equipment item, and a channel unless it is a bus."""

    equipment: str
    channel: str | None = None

    def __str__(self) -> str:
        return self.equipment if self.channel is None else f"{self.equipment}.{self.channel}"


@dataclass(frozen=True)
class Loopback:
    drive: Endpoint
    read: Endpoint


@dataclass(frozen=True)
class Presence:
    """Seen within ``within`` seconds: any DUT traffic on ``bus``, or a value of ``signal``."""

    within: float
    bus: str | None = None
    signal: str | None = None


@dataclass(frozen=True)
class HarnessConfig:
    """A loaded harness file."""

    name: str
    path: Path
    connections: Mapping[str, Endpoint]
    loopbacks: tuple[Loopback, ...] = ()
    presence: tuple[Presence, ...] = ()
    simulate: str | None = None  # the DUT model, as the harness file names it
    model: type[DutModel] | None = None
    model_options: Mapping[str, Any] = field(default_factory=dict)


def load_harness_file(
    path: str | os.PathLike[str], description: InterfaceDescription, bench: BenchConfig
) -> HarnessConfig:
    """Load a harness file and check it against the DUT and the bench. Raise ``ConfigError``."""
    path = Path(path)
    data = read_mapping(path, kind=KIND)
    problems: list[Problem] = []
    checker = _Checker(description, bench, problems, path)

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

    name = data.get("harness")
    if not isinstance(name, str) or not name.strip():
        problems.append(
            Problem("'harness:' must name the harness", hint="for example  harness: rig-1-loom-b")
        )
        name = ""

    connections = checker.connections(data.get("connections"))
    loopbacks = checker.loopbacks(data.get("loopbacks"))
    presence = checker.presence(data.get("presence"), connections)
    simulate, model, model_options = checker.simulate(data.get("simulate"))

    if problems:
        raise ConfigError(path, problems, kind=KIND)
    log.info(
        "loaded harness file %s: harness %s, %s, %s, %s%s",
        path.name,
        name,
        count(len(connections), "connection"),
        count(len(loopbacks), "loopback"),
        count(len(presence), "presence check"),
        f", simulating DUT {simulate}" if simulate else "",
    )
    return HarnessConfig(
        name=name,
        path=path,
        connections=MappingProxyType(connections),
        loopbacks=tuple(loopbacks),
        presence=tuple(presence),
        simulate=simulate,
        model=model,
        model_options=MappingProxyType(model_options),
    )


class _Checker:
    def __init__(
        self,
        description: InterfaceDescription,
        bench: BenchConfig,
        problems: list[Problem],
        path: Path,
    ) -> None:
        self.description = description
        self.bench = bench
        self.problems = problems
        self.path = path
        self.used_channels: dict[str, str] = {}  # "eq.ch" -> what uses it

    # --- endpoints --------------------------------------------------------------------------

    def endpoint(self, raw: Any, *, signal: str | None, where: str) -> Endpoint | None:
        if not isinstance(raw, str) or not raw.strip():
            self.problems.append(
                Problem(
                    "must name an equipment item, or equipment.channel",
                    signal=signal,
                    where=where,
                    hint="for example  can0  or  io.out1",
                )
            )
            return None
        equipment_id, _, channel = raw.strip().partition(".")
        equipment = self.bench.equipment.get(equipment_id)
        if equipment is None:
            close = difflib.get_close_matches(equipment_id, list(self.bench.equipment), n=1)
            self.problems.append(
                Problem(
                    f"no equipment '{equipment_id}' in bench file {self.bench.path.name}",
                    signal=signal,
                    where=where,
                    hint=f"did you mean '{close[0]}'?"
                    if close
                    else f"equipment: {', '.join(self.bench.equipment)}",
                )
            )
            return None
        if equipment.kind == "bus":
            if channel:
                self.problems.append(
                    Problem(
                        f"'{equipment_id}' is a bus, which has no channels",
                        signal=signal,
                        where=where,
                        hint=f"write just  {equipment_id}",
                    )
                )
                return None
            return Endpoint(equipment_id)
        if not channel:
            self.problems.append(
                Problem(
                    f"'{equipment_id}' has channels; say which one",
                    signal=signal,
                    where=where,
                    hint=f"channels: {', '.join(equipment.channels)}",
                )
            )
            return None
        if channel not in equipment.channels:
            close = difflib.get_close_matches(channel, list(equipment.channels), n=1)
            self.problems.append(
                Problem(
                    f"no channel '{channel}' on '{equipment_id}'",
                    signal=signal,
                    where=where,
                    hint=f"did you mean '{equipment_id}.{close[0]}'?"
                    if close
                    else f"channels: {', '.join(equipment.channels)}",
                )
            )
            return None
        return Endpoint(equipment_id, channel)

    def claim(self, endpoint: Endpoint, user: str, *, signal: str | None, where: str) -> bool:
        """A channel carries one thing. Buses carry many."""
        if endpoint.channel is None:
            return True
        key = str(endpoint)
        if key in self.used_channels:
            self.problems.append(
                Problem(
                    f"channel {key} is already used by {self.used_channels[key]}",
                    signal=signal,
                    where=where,
                    hint="each channel carries one signal or one loopback end",
                )
            )
            return False
        self.used_channels[key] = user
        return True

    def caps_allow(
        self, endpoint: Endpoint, *, drive: bool, signal: str | None, where: str, why: str
    ) -> bool:
        if endpoint.channel is None:
            return True  # a bus both drives and reads
        caps = self.bench.equipment[endpoint.equipment].channels[endpoint.channel]
        if drive and not caps.drive:
            self.problems.append(
                Problem(
                    f"{why}, but the bench can't drive {endpoint}",
                    signal=signal,
                    where=where,
                    hint=f"use a channel with drive: true in {self.bench.path.name}",
                )
            )
            return False
        if not drive and not caps.read:
            self.problems.append(
                Problem(
                    f"{why}, but the bench can't read {endpoint}",
                    signal=signal,
                    where=where,
                    hint=f"use a channel with read: true in {self.bench.path.name}",
                )
            )
            return False
        return True

    def bus_can_carry(self, name: str, signal: Signal, equipment: EquipmentConfig) -> bool:
        """CAN features the bus or BenchTare can't handle yet, caught before anything runs."""
        ref = self.description.can_signals[name]
        where = "connections:"
        if ref.multiplexed and signal.direction is Direction.INPUT:
            # A write would have to choose the multiplexer value too, or the frame would
            # silently leave the signal out. Reading multiplexed DUT outputs works.
            self.problems.append(
                Problem(
                    f"is in multiplexed message {ref.message}, and BenchTare can't write "
                    "multiplexed signals on a CAN bus yet",
                    signal=name,
                    where=where,
                    hint="reading multiplexed DUT outputs works; for this input, connect it to "
                    "a plain channel or leave it out of the harness",
                )
            )
            return False
        if ref.fd and not _carries_fd(equipment):
            self.problems.append(
                Problem(
                    f"is in CAN FD message {ref.message}, but bus {equipment.id} isn't in FD mode",
                    signal=name,
                    where=where,
                    hint=f"set fd: true for {equipment.id} in bench file {self.bench.path.name}",
                )
            )
            return False
        return True

    # --- sections ---------------------------------------------------------------------------

    def connections(self, raw: Any) -> dict[str, Endpoint]:
        if raw is None:
            self.problems.append(
                Problem(
                    "'connections:' is missing",
                    hint="map each signal to a bench channel, for example  Setpoint: can0",
                )
            )
            return {}
        if not isinstance(raw, dict):
            self.problems.append(Problem("'connections:' must map signal names to channels"))
            return {}
        where = "connections:"
        result: dict[str, Endpoint] = {}
        signals = self.description.signals
        for name, target in raw.items():
            if not isinstance(name, str):
                self.problems.append(
                    Problem(
                        f"a signal name was read as {kind_of(name)}, not as a name",
                        where=where,
                        hint=f"quote it, for example 'on': io.out1; {YAML_VALUE_WORDS}",
                    )
                )
                continue
            signal = signals.get(name)
            if signal is None:
                close = difflib.get_close_matches(name, list(signals), n=3)
                self.problems.append(
                    Problem(
                        f"the DUT has no such signal in {self.description.path.name}",
                        signal=name,
                        where=where,
                        hint=f"did you mean {', '.join(close)}?" if close else "the spelling",
                    )
                )
                continue
            endpoint = self.endpoint(target, signal=name, where=where)
            if endpoint is None:
                continue
            equipment = self.bench.equipment[endpoint.equipment]
            if equipment.kind == "bus" and name not in self.description.can_signals:
                self.problems.append(
                    Problem(
                        f"is connected to the CAN bus {endpoint}, but it doesn't come from a DBC "
                        "file, so there is no frame to carry it",
                        signal=name,
                        where=where,
                        hint="import it from a DBC in the interface description, or connect "
                        "it to a channel",
                    )
                )
                continue
            if equipment.kind == "bus" and not self.bus_can_carry(name, signal, equipment):
                continue
            is_input = signal.direction is Direction.INPUT
            why = "it is a DUT input" if is_input else "it is a DUT output"
            if not self.caps_allow(endpoint, drive=is_input, signal=name, where=where, why=why):
                continue
            if not self.claim(endpoint, f"signal '{name}'", signal=name, where=where):
                continue
            result[name] = endpoint
        return result

    def loopbacks(self, raw: Any) -> list[Loopback]:
        if raw is None:
            return []
        if not isinstance(raw, list):
            self.problems.append(
                Problem(
                    "'loopbacks:' must be a list",
                    hint="for example  loopbacks: [{drive: io.out4, read: io.in4}]",
                )
            )
            return []
        result: list[Loopback] = []
        for index, item in enumerate(raw, start=1):
            where = f"loopbacks: item {index}"
            if not isinstance(item, dict) or set(item) != {"drive", "read"}:
                self.problems.append(
                    Problem(
                        "must be {drive: equipment.channel, read: equipment.channel}", where=where
                    )
                )
                continue
            drive = self.endpoint(item["drive"], signal=None, where=where)
            read = self.endpoint(item["read"], signal=None, where=where)
            if drive is None or read is None:
                continue
            if drive.channel is None or read.channel is None:
                self.problems.append(Problem("loopbacks connect channels, not buses", where=where))
                continue
            ok = self.caps_allow(drive, drive=True, signal=None, where=where, why="it drives")
            ok &= self.caps_allow(read, drive=False, signal=None, where=where, why="it reads")
            ok &= self.claim(drive, f"loopback {index}", signal=None, where=where)
            ok &= self.claim(read, f"loopback {index}", signal=None, where=where)
            if ok:
                result.append(Loopback(drive, read))
        return result

    def presence(self, raw: Any, connections: Mapping[str, Endpoint]) -> list[Presence]:
        if raw is None:
            return []
        if not isinstance(raw, list):
            self.problems.append(
                Problem(
                    "'presence:' must be a list",
                    hint="for example  presence: [{bus: can0, within: 1.0}]",
                )
            )
            return []
        result: list[Presence] = []
        for index, item in enumerate(raw, start=1):
            where = f"presence: item {index}"
            if (
                not isinstance(item, dict)
                or len(set(item) & {"bus", "signal"}) != 1
                or (set(item) - {"bus", "signal", "within"})
            ):
                self.problems.append(
                    Problem(
                        "must be {bus: equipment, within: seconds} or "
                        "{signal: name, within: seconds}",
                        where=where,
                    )
                )
                continue
            within = item.get("within")
            if isinstance(within, bool) or not isinstance(within, (int, float)) or within <= 0:
                self.problems.append(
                    Problem("'within:' must be a number of seconds above 0", where=where)
                )
                continue
            key = "bus" if "bus" in item else "signal"
            if not isinstance(item[key], str):
                self.problems.append(
                    Problem(f"'{key}:' must be a name, not {kind_of(item[key])}", where=where)
                )
                continue
            if "bus" in item:
                bus = item["bus"]
                equipment = self.bench.equipment.get(bus)
                if equipment is None or equipment.kind != "bus":
                    buses = [e.id for e in self.bench.equipment.values() if e.kind == "bus"]
                    self.problems.append(
                        Problem(
                            f"'{bus}' is not a bus in bench file {self.bench.path.name}",
                            where=where,
                            hint=f"buses: {', '.join(buses) or 'none'}",
                        )
                    )
                    continue
                result.append(Presence(within=float(within), bus=bus))
            else:
                name = item["signal"]
                signal = self.description.signals.get(name)
                if name not in connections or signal is None:
                    self.problems.append(
                        Problem(
                            "isn't connected in this harness, so it can't show the DUT is present",
                            signal=name,
                            where=where,
                        )
                    )
                    continue
                if signal.direction is not Direction.OUTPUT:
                    self.problems.append(
                        Problem(
                            "is a DUT input; presence is shown by a value the DUT sends",
                            signal=name,
                            where=where,
                            hint="use a DUT output",
                        )
                    )
                    continue
                result.append(Presence(within=float(within), signal=name))
        return result

    def simulate(self, raw: Any) -> tuple[str | None, type[DutModel] | None, dict[str, Any]]:
        """The DUT model to run, as the harness names it, and its options."""
        if raw is None:
            return None, None, {}
        options: Any = {}
        if isinstance(raw, dict):
            for key in raw:
                if key not in ("model", "options"):
                    self.problems.append(
                        Problem(f"unknown key '{key}'", where="simulate", hint=_MODEL_EXAMPLE)
                    )
            options = raw.get("options", {})
            if not isinstance(options, dict) or not all(isinstance(k, str) for k in options):
                self.problems.append(
                    Problem(
                        f"'options:' must map option names to values, not {kind_of(options)}",
                        where="simulate",
                        hint=_MODEL_EXAMPLE,
                    )
                )
                options = {}
            raw = raw.get("model")
        if not isinstance(raw, str) or not raw.strip():
            self.problems.append(
                Problem(
                    f"must name a DUT model, not {kind_of(raw)}",
                    where="simulate",
                    hint=f"one that ships with BenchTare ({', '.join(sorted(SIMULATED_DUTS))}), "
                    f"or a team's own; {_MODEL_EXAMPLE}",
                )
            )
            return None, None, {}
        name = raw.strip()
        model = self._model(name)
        if model is None:
            return None, None, {}
        real = [e.id for e in self.bench.equipment.values() if not e.simulated]
        if real:
            self.problems.append(
                Problem(
                    f"a simulated DUT needs simulated equipment, but {', '.join(real)} is real",
                    where="simulate",
                    hint="remove 'simulate:' to test a real DUT",
                )
            )
        missing = [n for n in model.signals if n not in self.description.signals]
        if missing:
            self.problems.append(
                Problem(
                    f"the DUT model '{name}' uses signals that "
                    f"{self.description.path.name} doesn't have: {', '.join(missing)}",
                    where="simulate",
                    hint="use the interface description the model was written for, or change "
                    "the model's signals",
                )
            )
        return name, model, dict(options)

    def _model(self, name: str) -> type[DutModel] | None:
        """A model that ships with BenchTare, by name, or a team's, as ``file.py:Class``."""
        file, colon, class_name = name.rpartition(":")
        if not colon:
            if name in SIMULATED_DUTS:
                return SIMULATED_DUTS[name]
            self.problems.append(
                Problem(
                    f"unknown DUT model '{name}'",
                    where="simulate",
                    hint=f"models that ship with BenchTare: {', '.join(sorted(SIMULATED_DUTS))}; "
                    f"or a team's own, {_MODEL_EXAMPLE}",
                )
            )
            return None
        where = "simulate"
        source = (self.path.parent / file).resolve()
        if not file.endswith(".py") or not class_name.isidentifier():
            self.problems.append(
                Problem(
                    f"'{name}' must be a Python file and a class in it, such as "
                    "models/my_dut.py:MyDut",
                    where=where,
                )
            )
            return None
        if not source.is_file():
            self.problems.append(
                Problem(
                    f"there is no file {file} for the DUT model",
                    where=where,
                    hint=f"the path is relative to the harness file's folder, {self.path.parent}",
                )
            )
            return None
        try:
            module = _import_file(source)
        except Exception as exc:  # noqa: BLE001 - the team's code: report it, with where
            self.problems.append(
                Problem(
                    f"the DUT model file {file} couldn't be loaded: {type(exc).__name__}: {exc}"
                    f"{_where_in(exc, source)}",
                    where=where,
                )
            )
            return None
        found = getattr(module, class_name, None)
        if not (isinstance(found, type) and issubclass(found, DutModel)):
            models = sorted(
                n
                for n, v in vars(module).items()
                if isinstance(v, type) and issubclass(v, DutModel) and v is not DutModel
            )
            what = "isn't a DutModel" if found is not None else "isn't in it"
            self.problems.append(
                Problem(
                    f"{class_name} {what}: a DUT model is a class that subclasses "
                    "benchtare.DutModel",
                    where=where,
                    hint=f"DUT models in {file}: {', '.join(models)}"
                    if models
                    else f"define  class {class_name}(DutModel)  in {file}",
                )
            )
            return None
        return found


_MODEL_EXAMPLE = "for example  simulate: models/my_dut.py:MyDut"


def _import_file(source: Path) -> Any:
    """Import a team's Python file as a module of its own, without it being on sys.path."""
    digest = hashlib.sha256(str(source).encode()).hexdigest()[:12]
    name = f"benchtare_dut_model_{source.stem}_{digest}"
    spec = importlib.util.spec_from_file_location(name, source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[name]
        raise
    return module


def _where_in(exc: BaseException, source: Path) -> str:
    """', at line 12' when the error happened in ``source``."""
    if isinstance(exc, SyntaxError) and exc.lineno:
        return f", at line {exc.lineno}"
    lines = [
        frame.lineno
        for frame in traceback.extract_tb(exc.__traceback__)
        if Path(frame.filename) == source
    ]
    return f", at line {lines[-1]}" if lines else ""


def _carries_fd(equipment: EquipmentConfig) -> bool:
    """Whether a bus is set up for CAN FD frames. Backends without the notion carry none."""
    carries_fd = getattr(BACKENDS[equipment.backend], "carries_fd", None)
    return bool(carries_fd(equipment.options)) if carries_fd is not None else False
