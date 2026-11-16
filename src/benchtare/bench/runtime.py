"""The running bench: what tests drive through the ``bench`` fixture.

It routes each signal through the harness to its equipment and backend.

- ``write(signal, value)`` drives a DUT input. Any value the channel can carry is sent.
- ``read(signal, timeout)`` returns the first value of a DUT output received after the bench's
  most recent write. That suits request/response signals and states that have already settled.
- ``wait_until(signal, expected, timeout)`` waits for a value that equals ``expected`` (or
  satisfies it, if it is a predicate). Use it to check how the DUT reacts: a DUT that reports
  cyclically, or a sampled channel, can still deliver the old value just after a write. Values
  that arrive but never match fail the test like an ``assert`` (``ComparisonFailed``): the DUT
  answered wrongly, or later than the timeout, which is the test's timing requirement. Only
  when nothing arrives at all is it a ``ReadTimeout``.

Each reading a test takes is a verification step. ``readings`` logs them, with the test's line
that took each; ``check=False`` marks one the test deliberately doesn't check, such as a value
kept only for trends. While the test check runs (``benchtare.testcheck``), ``corruption`` names
the mode and ``corrupt_at`` the step it corrupts; the record shows both what arrived and what
the test was given.

Each call goes into the test record (``benchtare.record``) through ``recorder``, which the
pytest plugin sets; so does anything unexpected a backend sees. With no recorder, unexpected
events are logged as warnings instead, so they are never lost.

See docs/model.md.
"""

from __future__ import annotations

import inspect
import logging
import math
import numbers
import os
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from benchtare.backends import BACKENDS
from benchtare.backends.base import (
    Backend,
    BenchError,
    Binding,
    ComparisonFailed,
    EquipmentIdentity,
    ReadTimeout,
    ReceiveBuffer,
    Received,
    WriteError,
)
from benchtare.bench.config import BenchConfig, load_bench_file
from benchtare.dut.description import InterfaceDescription, load_interface_description
from benchtare.dut.identity import CODE, SIGNAL, IdentityValue, name_problem
from benchtare.harness.config import HarnessConfig, load_harness_file
from benchtare.record import (
    DEFAULT_LEVEL,
    IDENTIFY,
    IDENTITY_CHANGE,
    READ,
    WAIT,
    WRITE,
    RecordEntry,
    RecordLevel,
)
from benchtare.signals.model import Direction, Signal, SignalSet, SignalType
from benchtare.sim.simulation import Simulation
from benchtare.testcheck import SHIFT, STALE, Reading, shifted

if TYPE_CHECKING:
    from typing_extensions import Self  # typing.Self needs Python 3.11

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 1.0

_IDENTITY_FIELDS = (
    "vendor",
    "model",
    "serial_number",
    "firmware_version",
    "driver",
    "driver_version",
)

# The key under which Bench.check() lists the simulation's own problems. It has a space, so it
# can never be an equipment name.
SIMULATION = "simulated DUT"

# BenchTare's own code, which a reading's location skips to find the test's line.
_PACKAGE = os.path.normcase(str(Path(__file__).resolve().parents[1])) + os.sep


def open_bench(
    dut: str | os.PathLike[str],
    bench: str | os.PathLike[str],
    harness: str | os.PathLike[str],
    *,
    device_options: Mapping[str, Any] | None = None,
) -> Bench:
    """Load the interface description, bench file and harness file, and open the bench."""
    description = load_interface_description(dut)
    bench_config = load_bench_file(bench)
    harness_config = load_harness_file(harness, description, bench_config)
    return Bench(description, bench_config, harness_config, device_options=device_options).open()


class Bench:
    """A bench connected to a DUT through a harness. Use it as a context manager."""

    def __init__(
        self,
        description: InterfaceDescription,
        bench: BenchConfig,
        harness: HarnessConfig,
        *,
        device_options: Mapping[str, Any] | None = None,
    ) -> None:
        self.description = description
        self.config = bench
        self.harness = harness
        self._device_options = device_options
        self._buffer = ReceiveBuffer()
        self._bindings = _bindings(description, harness)
        self._backends: dict[str, Backend] = {
            item.id: BACKENDS[item.backend](item.id, item.options, item.channels)
            for item in bench.equipment.values()
        }
        self.simulation: Simulation | None = None
        self._open = False
        # The test record. The pytest plugin sets the recorder and keeps the entries per test.
        self.recorder: Callable[[RecordEntry], None] | None = None
        self._record_level = DEFAULT_LEVEL
        self._buffer.note_level = DEFAULT_LEVEL
        self._buffer.on_note = self._note
        # The DUT's identity for the run: latest value by name, every value in order, and why
        # an expected item couldn't be read.
        self._identity_lock = threading.Lock()
        self._identity: dict[str, str] = {}
        self._identity_values: list[IdentityValue] = []
        self._identity_notes: dict[str, str] = {}
        # The readings taken (the pytest plugin clears them for each test's body) and, for the
        # test check, the mode corrupting them (None when off) and which checked reading it
        # corrupts, numbered from 1 (None for every one).
        self.readings: list[Reading] = []
        self.corruption: str | None = None
        self.corrupt_at: int | None = None

    @property
    def record_level(self) -> RecordLevel:
        """Which entries go to the recorder. Unexpected events always do."""
        return self._record_level

    @record_level.setter
    def record_level(self, level: RecordLevel) -> None:
        self._record_level = level
        self._buffer.note_level = level

    @property
    def signals(self) -> SignalSet:
        return self.description.signals

    # --- life cycle -------------------------------------------------------------------------

    def open(self) -> Bench:
        by_equipment: dict[str, list[Binding]] = {name: [] for name in self._backends}
        for binding in self._bindings.values():
            by_equipment[binding.equipment].append(binding)
        opened: list[Backend] = []
        try:
            for name, backend in self._backends.items():
                backend.open(by_equipment[name], self._buffer)
                opened.append(backend)
            if self.harness.simulate is not None:
                self.simulation = Simulation(
                    self.description, self.harness, self._backends, self._device_options
                )
                self.simulation.start()
        except BaseException:
            for backend in opened:
                backend.close()
            raise
        self._open = True
        log.info(
            "bench %s open: equipment %s; harness %s; DUT %s%s",
            self.config.name,
            ", ".join(f"{item.id} ({item.backend})" for item in self.config.equipment.values()),
            self.harness.name,
            self.description.name,
            f" (simulated: {self.harness.simulate})" if self.harness.simulate else "",
        )
        return self

    def close(self) -> None:
        was_open = self._open
        if self.simulation is not None:
            self.simulation.stop()
            self.simulation = None
        for backend in self._backends.values():
            backend.close()
        self._open = False
        if was_open:
            log.info("bench %s closed", self.config.name)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def check(self) -> dict[str, list[str]]:
        """The problems each equipment item reports; empty lists when all is healthy.

        With a simulated DUT, the simulation's own problems are listed under ``SIMULATION``.
        """
        problems = {name: backend.check() for name, backend in self._backends.items()}
        if self.simulation is not None:
            problems[SIMULATION] = self.simulation.check()
        return problems

    # --- what tests call --------------------------------------------------------------------

    def write(self, signal: Signal | str, value: Any) -> None:
        """Drive a DUT input. Raise ``WriteError`` if its channel can't carry ``value``.

        The value must be of a kind the signal has (see ``_kind_problem``); within that, any
        value the channel can carry is sent, however implausible.
        """
        __tracebackhide__ = True
        binding = self._binding(signal)
        if not binding.drive:
            raise WriteError(
                f"can't write {binding.key}: it is a DUT output, which the bench reads",
                hint="tests write DUT inputs and read DUT outputs; see the direction in "
                f"{self.description.path.name}",
            )
        assert binding.signal is not None
        problem = _kind_problem(binding.signal, value)
        if problem is not None:
            message, hint = problem
            raise WriteError(f"can't write {binding.key} = {value!r}: {message}", hint=hint)
        backend = self._backends[binding.equipment]
        self._note(
            RecordEntry(
                WRITE,
                RecordLevel.STIMULI,
                f"write {binding.key} = {_shown(binding, value)} on {binding.endpoint}",
                _facts(binding, value),
            )
        )
        with self._buffer.writing():
            backend.write(binding, value)

    def read(
        self, signal: Signal | str, timeout: float = DEFAULT_TIMEOUT, *, check: bool = True
    ) -> Any:
        """Return the first value of a DUT output received after the most recent write.

        ``check=False`` says the test doesn't check this value on purpose, such as a
        measurement kept only for trends; the test check then leaves it alone.
        """
        __tracebackhide__ = True
        binding = self._readable(signal)
        _check_timeout(timeout)
        reading = self._start_reading(binding, check)
        started = time.monotonic()
        item = self._buffer.first_after_write(binding.key, timeout)
        waited = time.monotonic() - started
        if item is None:
            self._note(
                RecordEntry(
                    READ,
                    RecordLevel.RESULTS,
                    f"read {binding.key}: nothing within {timeout:g} s on {binding.endpoint}",
                    {
                        **_facts(binding, None),
                        "outcome": "timeout",
                        "timeout": timeout,
                        **_step_facts(reading),
                    },
                )
            )
            raise ReadTimeout(
                f"no value of {binding.key} arrived within {timeout:g} s after the last write "
                f"(on {binding.endpoint}){self._last_seen(binding.key)}",
                hint=self._what_to_check(binding),
            )
        applied, value = self._as_read(binding, item.value, reading)
        self._settle(binding, reading, item.value, applied)
        self._note(
            RecordEntry(
                READ,
                RecordLevel.RESULTS,
                f"read {binding.key} = {_shown(binding, value)} on {binding.endpoint} "
                f"after {waited:.3f} s{self._corrupted_note(binding, item.value, applied)}",
                {
                    **_facts(binding, value),
                    "outcome": "value",
                    "waited": waited,
                    **_step_facts(reading),
                    **self._corrupted_facts(item.value, applied),
                },
            )
        )
        return value

    def wait_until(
        self,
        signal: Signal | str,
        expected: Any,
        timeout: float = DEFAULT_TIMEOUT,
        *,
        check: bool = True,
    ) -> Any:
        """Wait for a value of a DUT output, received after the most recent write, that equals
        ``expected``, or for which ``expected`` returns true if it is a function. Return it.

        ``check=False`` says this wait isn't one of the test's checks, such as waiting for the
        DUT to settle before the real stimulus; the test check then leaves it alone.
        """
        __tracebackhide__ = True
        binding = self._readable(signal)
        _check_timeout(timeout)
        reading = self._start_reading(binding, check)
        matches: Callable[[Any], bool] = (
            expected if _is_predicate(expected) else (lambda value: value == expected)
        )
        want = getattr(expected, "__name__", None) if _is_predicate(expected) else None
        expecting = want or repr(expected)
        started = time.monotonic()
        item = self._buffer.wait_for_match(
            binding.key, lambda value: matches(self._as_read(binding, value, reading)[1]), timeout
        )
        waited = time.monotonic() - started
        if item is None:
            arrived = self._buffer.since_write(binding.key)
            if arrived:
                self._not_reached(binding, expecting, timeout, arrived, reading)
            last = self._buffer.last(binding.key)
            seen = f"; last value {_shown(binding, last.value)}, from earlier" if last else ""
            self._note(
                RecordEntry(
                    WAIT,
                    RecordLevel.RESULTS,
                    f"wait_until {binding.key}, expecting {expecting}: nothing arrived within "
                    f"{timeout:g} s on {binding.endpoint}{seen}",
                    {
                        **_facts(binding, last.value if last else None),
                        "expected": expecting,
                        "outcome": "timeout",
                        "timeout": timeout,
                        **_step_facts(reading),
                    },
                )
            )
            raise ReadTimeout(
                f"no value of {binding.key} arrived within {timeout:g} s after the last write "
                f"(on {binding.endpoint}), so it couldn't reach {expecting}"
                f"{self._last_seen(binding.key)}",
                hint=self._what_to_check(binding),
            )
        applied, value = self._as_read(binding, item.value, reading)
        self._settle(binding, reading, item.value, applied)
        self._note(
            RecordEntry(
                WAIT,
                RecordLevel.RESULTS,
                f"wait_until {binding.key}, expecting {expecting}: got "
                f"{_shown(binding, value)} on {binding.endpoint} after {waited:.3f} s"
                f"{self._corrupted_note(binding, item.value, applied)}",
                {
                    **_facts(binding, value),
                    "expected": expecting,
                    "outcome": "reached",
                    "waited": waited,
                    **_step_facts(reading),
                    **self._corrupted_facts(item.value, applied),
                },
            )
        )
        return value

    def _not_reached(
        self,
        binding: Binding,
        expecting: str,
        timeout: float,
        arrived: list[Received],
        reading: Reading,
    ) -> None:
        """Values arrived but none matched: record it, and fail the test like an ``assert``."""
        __tracebackhide__ = True
        applied, value = self._as_read(binding, arrived[-1].value, reading)
        self._settle(binding, reading, arrived[-1].value, applied)
        count = f"{len(arrived)} value{'' if len(arrived) == 1 else 's'}"
        self._note(
            RecordEntry(
                WAIT,
                RecordLevel.RESULTS,
                f"wait_until {binding.key}, expecting {expecting}: not reached within "
                f"{timeout:g} s on {binding.endpoint}; {count} arrived, the last "
                f"{_shown(binding, value)}{self._corrupted_note(binding, arrived[-1].value, applied)}",
                {
                    **_facts(binding, value),
                    "expected": expecting,
                    "outcome": "not reached",
                    "timeout": timeout,
                    "arrived": len(arrived),
                    **_step_facts(reading),
                    **self._corrupted_facts(arrived[-1].value, applied),
                },
            )
        )
        since = "after the last write" if self._buffer.written else "since the bench opened"
        raise ComparisonFailed(
            f"{binding.key} did not reach {expecting} within {timeout:g} s (on "
            f"{binding.endpoint}): {count} arrived {since}, the last {_shown(binding, value)}"
        )

    def _start_reading(self, binding: Binding, check: bool) -> Reading:
        """Log a reading the test is taking: its number among the checked ones, and where."""
        index = sum(1 for taken in self.readings if taken.checked) + 1 if check else None
        reading = Reading(index, binding.key, _caller(), check)
        self.readings.append(reading)
        return reading

    def _as_read(self, binding: Binding, value: Any, reading: Reading) -> tuple[bool, Any]:
        """What the test is given for a value that arrived: the value itself or, while the test
        check corrupts this reading, the corrupted value; and whether it was corrupted."""
        mode = self.corruption
        if mode is None or not reading.checked:
            return False, value
        if self.corrupt_at is not None and reading.index != self.corrupt_at:
            return False, value
        if mode == SHIFT:
            return shifted(binding.signal, value)
        if mode == STALE:
            before = self._buffer.before_write(binding.key)
            if before is None:
                return False, value
            return before.value != value, before.value
        return False, value

    def _settle(self, binding: Binding, reading: Reading, received: Any, applied: bool) -> None:
        """Note whether the test check changed this reading, and if it was meant to but
        couldn't, why."""
        reading.corrupted = applied
        if applied or self.corruption is None or not reading.checked:
            return
        if self.corrupt_at is not None and reading.index != self.corrupt_at:
            return
        if self.corruption == STALE:
            before = self._buffer.before_write(binding.key)
            reading.note = (
                "no value of it arrived before a write"
                if before is None
                else f"it was already {_shown(binding, before.value)} before the write, so stale "
                "can't change this step"
            )
        else:
            reading.note = f"its value {_shown(binding, received)} can't be shifted"

    def _corrupted_note(self, binding: Binding, received: Any, applied: bool) -> str:
        if not applied:
            return ""
        return f" (test check, {self.corruption}: {_shown(binding, received)} arrived)"

    def _corrupted_facts(self, received: Any, applied: bool) -> dict[str, Any]:
        return {"corruption": self.corruption, "received": received} if applied else {}

    # --- the DUT's identity -----------------------------------------------------------------

    def identify(self, item: str | Mapping[str, Any], value: Any = None) -> None:
        """Record part of the DUT's identity for this run, such as a software version:
        ``bench.identify("bootloader", "1.4.2")``, or several at once with a mapping.

        Any name can be given, such as ``motor.firmware`` for a subsystem; the interface
        description's ``identity:`` lists the ones every run should have. The value is stored
        as text, and belongs to the whole run. Giving the same value again changes nothing; a
        different value is kept as well, and recorded as an unexpected change.
        """
        __tracebackhide__ = True
        values = dict(item) if isinstance(item, Mapping) else {item: value}
        for name, val in values.items():
            self.collect_identity(name, val, CODE)

    @property
    def identity(self) -> dict[str, str]:
        """The DUT's identity collected so far: each item's latest value, by name."""
        with self._identity_lock:
            return dict(self._identity)

    def identity_values(self) -> list[IdentityValue]:
        """Every identity value collected so far, in order, with where it came from."""
        with self._identity_lock:
            return list(self._identity_values)

    def missing_identity(self) -> dict[str, str]:
        """The listed identity items with no value yet, each with why, if that is known."""
        with self._identity_lock:
            return {
                name: self._identity_notes.get(name, "")
                for name in self.description.identity
                if name not in self._identity
            }

    def read_identity(self) -> None:
        """Read each listed identity item that names a DUT output. Run before the first test."""
        for item in self.description.identity.values():
            if item.signal is None or item.name in self.identity:
                continue
            if item.signal not in self._bindings:
                note = f"its signal {item.signal} isn't connected in harness file "
                with self._identity_lock:
                    self._identity_notes[item.name] = note + self.harness.path.name
                continue
            received = self.seen(item.signal, item.within)
            if received is None:
                with self._identity_lock:
                    self._identity_notes[item.name] = (
                        f"no value of {item.signal} arrived within {item.within:g} s"
                    )
                continue
            self.collect_identity(item.name, received.value, SIGNAL, signal=item.signal)

    def collect_identity(
        self, name: str, value: Any, source: str, *, signal: str | None = None
    ) -> None:
        """Record one identity value from ``source`` (see ``benchtare.dut.identity``)."""
        __tracebackhide__ = True
        problem = name_problem(name)
        if problem:
            raise BenchError(problem, hint="for example  bench.identify('bootloader', '1.4.2')")
        if value is None:
            raise BenchError(
                f"identity item '{name}' needs a value",
                hint=f"for example  bench.identify('{name}', '1.4.2')",
            )
        text = str(value)
        with self._identity_lock:
            previous = self._identity.get(name)
            if previous == text:
                return
            self._identity[name] = text
            self._identity_values.append(
                IdentityValue(name, text, source, name in self.description.identity, time.time())
            )
        origin = {"signal": f"signal {signal}", "code": "the team's code"}.get(
            source, "the command line"
        )
        facts = {"item": name, "value": text, "source": source}
        if signal is not None:
            facts["signal"] = signal
        self._note(
            RecordEntry(
                IDENTIFY, RecordLevel.RESULTS, f"identify {name} = {text!r} (from {origin})", facts
            )
        )
        if previous is not None:
            self._note(
                RecordEntry(
                    IDENTITY_CHANGE,
                    RecordLevel.RESULTS,
                    f"the DUT's {name} changed during the run: it was {previous!r}, and is now "
                    f"{text!r} (from {origin})",
                    {"item": name, "was": previous, "value": text, "source": source},
                )
            )

    # --- for the self-test ------------------------------------------------------------------

    def backend(self, equipment: str) -> Backend:
        """The backend that drives an equipment item."""
        return self._backends[equipment]

    def equipment(self) -> list[dict[str, Any]]:
        """Each equipment item as its backend describes it, for the run's results."""
        items = []
        for item in self.config.equipment.values():
            try:
                identity = self._backends[item.id].identity()
            except Exception as exc:  # noqa: BLE001 - a backend that can't describe itself is noted, not fatal
                identity = EquipmentIdentity(
                    details={"identity_error": f"{type(exc).__name__}: {exc}"}
                )
            items.append(
                {
                    "equipment": item.id,
                    "backend": item.backend,
                    "kind": item.kind,
                    "simulated": item.simulated,
                    **{key: getattr(identity, key) for key in _IDENTITY_FIELDS},
                    "details": dict(identity.details),
                }
            )
        return items

    def drive_channel(self, endpoint: str, value: Any) -> None:
        """Drive a bench channel that carries no signal, such as a loopback's drive end."""
        binding = self._channel(endpoint, drive=True)
        self._note(
            RecordEntry(
                WRITE,
                RecordLevel.STIMULI,
                f"write {endpoint} = {value!r}",
                _facts(binding, value),
            )
        )
        with self._buffer.writing():
            self._backends[binding.equipment].write(binding, value)

    def wait_channel(self, endpoint: str, expected: Any, timeout: float) -> Received | None:
        """Wait for ``expected`` on a bench channel that carries no signal, such as a loopback's
        read end, received after the most recent write. None if it doesn't come in time."""
        binding = self._channel(endpoint, drive=False)
        _check_timeout(timeout)
        return self._buffer.wait_for_match(binding.key, lambda value: value == expected, timeout)

    def seen(self, signal: Signal | str, timeout: float) -> Received | None:
        """The latest value of a DUT output received since the bench opened, waiting up to
        ``timeout`` for the first. None if none comes."""
        binding = self._readable(signal)
        _check_timeout(timeout)
        return self._buffer.wait_seen(binding.key, timeout)

    def last_received(self, key: str) -> Received | None:
        """The latest value received on a signal or channel since the bench opened."""
        return self._buffer.last(key)

    # --- helpers ----------------------------------------------------------------------------

    def _channel(self, endpoint: str, *, drive: bool) -> Binding:
        binding = self._bindings.get(endpoint)
        if binding is None or binding.signal is not None or binding.drive is not drive:
            raise BenchError(
                f"{endpoint} isn't a {'driven' if drive else 'read'} loopback end in harness file "
                f"{self.harness.path.name}",
                hint="the loopbacks: list in the harness file",
            )
        return binding

    def _binding(self, signal: Signal | str) -> Binding:
        __tracebackhide__ = True
        if not self._open:
            raise BenchError("the bench is not open", hint="open it with open_bench() first")
        name = signal.name if isinstance(signal, Signal) else str(signal)
        try:
            self.description.signals[name]
        except KeyError as exc:
            # SignalSet's message suggests close names.
            raise BenchError(
                exc.args[0], hint=f"the signal names in {self.description.path.name}"
            ) from None
        binding = self._bindings.get(name)
        if binding is None:
            raise BenchError(
                f"signal {name} isn't connected in harness file {self.harness.path.name}",
                hint=f"add it under 'connections:', for example  {name}: equipment.channel",
            )
        return binding

    def _readable(self, signal: Signal | str) -> Binding:
        __tracebackhide__ = True
        binding = self._binding(signal)
        if binding.drive:
            raise BenchError(
                f"can't read {binding.key}: it is a DUT input, which the bench drives",
                hint=f"tests read DUT outputs; see the direction in {self.description.path.name}",
            )
        return binding

    def _note(self, entry: RecordEntry) -> None:
        """Pass an entry to the recorder if the record level keeps it. Any thread may call it."""
        recorder = self.recorder
        if recorder is None:
            if entry.unexpected:
                log.warning("%s", entry.text)  # nothing is recording it, so it must not vanish
            return
        if entry.unexpected or entry.level <= self._record_level:
            recorder(entry)

    def _last_seen(self, key: str) -> str:
        last = self._buffer.last(key)
        if last is None:
            return "; no value has arrived since the bench opened"
        ago = time.monotonic() - last.at
        return f"; last value {last.value!r}, {ago:.2f} s ago"

    def _what_to_check(self, binding: Binding) -> str:
        problems = self._backends[binding.equipment].check()
        if problems:
            return f"equipment '{binding.equipment}' reports: {'; '.join(problems)}"
        if self.simulation is not None and (sim_problems := self.simulation.check()):
            return "; ".join(sim_problems)
        return (
            f"the DUT is powered and connected at {binding.endpoint}, and sends {binding.key}; "
            f"see harness file {self.harness.path.name}"
        )


def _bindings(description: InterfaceDescription, harness: HarnessConfig) -> dict[str, Binding]:
    bindings: dict[str, Binding] = {}
    for name, endpoint in harness.connections.items():
        signal = description.signals[name]
        bindings[name] = Binding(
            key=name,
            equipment=endpoint.equipment,
            channel=endpoint.channel,
            drive=signal.direction is Direction.INPUT,
            signal=signal,
            can=description.can_signals.get(name),
        )
    for loopback in harness.loopbacks:
        for endpoint, drive in ((loopback.drive, True), (loopback.read, False)):
            key = str(endpoint)
            bindings[key] = Binding(
                key=key, equipment=endpoint.equipment, channel=endpoint.channel, drive=drive
            )
    return bindings


def _step_facts(reading: Reading) -> dict[str, Any]:
    """Where the test took a reading, and whether it is one of the test's checks."""
    facts: dict[str, Any] = {"where": reading.where} if reading.where else {}
    if not reading.checked:
        facts["checked"] = False
    return facts


def _caller() -> str:
    """The file and line outside BenchTare that called into it: the test's step."""
    frame = inspect.currentframe()
    while frame is not None:
        filename = os.path.normcase(os.path.abspath(frame.f_code.co_filename))
        if not filename.startswith(_PACKAGE):
            return f"{os.path.basename(filename)}:{frame.f_lineno}"
        frame = frame.f_back
    return ""


def _facts(binding: Binding, value: Any) -> dict[str, Any]:
    unit = binding.signal.unit if binding.signal is not None else None
    return {"signal": binding.key, "value": value, "unit": unit, "endpoint": binding.endpoint}


def _shown(binding: Binding, value: Any) -> str:
    """A value as the log shows it: ``42.0 degC``, ``'run'``, ``True``."""
    unit = binding.signal.unit if binding.signal is not None else None
    return f"{value!r} {unit}" if unit else repr(value)


def _is_predicate(expected: Any) -> bool:
    # pytest.approx and similar comparison helpers are not callable; plain functions are.
    return callable(expected) and not isinstance(expected, type)


def _check_timeout(timeout: Any) -> None:
    """A timeout is a finite number of seconds, 0 or more. A bench never waits forever."""
    __tracebackhide__ = True
    ok = (
        isinstance(timeout, numbers.Real)
        and not isinstance(timeout, bool)
        and math.isfinite(float(timeout))
        and timeout >= 0
    )
    if not ok:
        raise BenchError(
            f"timeout must be a number of seconds, 0 or more; got {timeout!r}",
            hint="for example timeout=1.0; there is no 'wait forever', so a missing DUT can't "
            "hang the run",
        )


def _kind_problem(signal: Signal, value: Any) -> tuple[str, str] | None:
    """Whether ``value`` is a kind of value ``signal`` can have. Return (problem, hint) if not.

    This checks the kind only (a number for a float signal, a value name for an enum), so that
    every backend, simulated or real, refuses the same things. Whether a number fits the
    channel is the backend's business, and whether it is a sensible value is the test's.
    """
    kind = signal.type
    is_number = isinstance(value, numbers.Real) and not isinstance(value, bool)
    if kind is SignalType.FLOAT:
        if is_number:
            return None
        return (f"{signal.name} is a float signal", "write a number, for example 42.0")
    if kind is SignalType.INT:
        if is_number and _is_whole(value):
            return None
        return (f"{signal.name} is an int signal", "write a whole number, for example 42")
    if kind is SignalType.BOOL:
        if isinstance(value, bool) or (isinstance(value, numbers.Integral) and value in (0, 1)):
            return None
        return (f"{signal.name} is a bool signal", "write True or False")
    # An enum takes one of its value names, or a raw code to send a code the DUT doesn't define.
    if isinstance(value, str):
        if value in signal.values:
            return None
        return (
            f"'{value}' is not one of the values of {signal.name}",
            f"its values are {', '.join(signal.values)}; a raw number sends an undefined code",
        )
    if isinstance(value, numbers.Integral) and not isinstance(value, bool):
        return None
    return (
        f"{signal.name} is an enum signal",
        f"write one of {', '.join(signal.values)}, or a raw whole number for an undefined code",
    )


def _is_whole(value: numbers.Real) -> bool:
    return isinstance(value, numbers.Integral) or (
        math.isfinite(float(value)) and float(value).is_integer()
    )
