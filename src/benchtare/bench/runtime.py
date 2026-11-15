"""The running bench: what tests drive through the ``bench`` fixture.

It routes each signal through the harness to its equipment and backend.

- ``write(signal, value)`` drives a DUT input. Any value the channel can carry is sent.
- ``read(signal, timeout)`` returns the first value of a DUT output received after the bench's
  most recent write. That suits request/response signals and states that have already settled.
- ``wait_until(signal, expected, timeout)`` waits for a value that equals ``expected`` (or
  satisfies it, if it is a predicate). Use it to check how the DUT reacts: a DUT that reports
  cyclically, or a sampled channel, can still deliver the old value just after a write.

Each call goes into the test record (``benchtare.record``) through ``recorder``, which the
pytest plugin sets; so does anything unexpected a backend sees. With no recorder, unexpected
events are logged as warnings instead, so they are never lost.

See docs/model.md.
"""

from __future__ import annotations

import logging
import math
import numbers
import os
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from benchtare.backends import BACKENDS
from benchtare.backends.base import (
    Backend,
    BenchError,
    Binding,
    ReadTimeout,
    ReceiveBuffer,
    WriteError,
)
from benchtare.bench.config import BenchConfig, load_bench_file
from benchtare.dut.description import InterfaceDescription, load_interface_description
from benchtare.harness.config import HarnessConfig, load_harness_file
from benchtare.record import DEFAULT_LEVEL, READ, WAIT, WRITE, RecordEntry, RecordLevel
from benchtare.signals.model import Direction, Signal, SignalSet, SignalType
from benchtare.sim.simulation import Simulation

if TYPE_CHECKING:
    from typing_extensions import Self  # typing.Self needs Python 3.11

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 1.0

# The key under which Bench.check() lists the simulation's own problems. It has a space, so it
# can never be an equipment name.
SIMULATION = "simulated DUT"


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

    def read(self, signal: Signal | str, timeout: float = DEFAULT_TIMEOUT) -> Any:
        """Return the first value of a DUT output received after the most recent write."""
        __tracebackhide__ = True
        binding = self._readable(signal)
        _check_timeout(timeout)
        started = time.monotonic()
        item = self._buffer.first_after_write(binding.key, timeout)
        waited = time.monotonic() - started
        if item is None:
            self._note(
                RecordEntry(
                    READ,
                    RecordLevel.RESULTS,
                    f"read {binding.key}: nothing within {timeout:g} s on {binding.endpoint}",
                    {**_facts(binding, None), "outcome": "timeout", "timeout": timeout},
                )
            )
            raise ReadTimeout(
                f"no value of {binding.key} arrived within {timeout:g} s after the last write "
                f"(on {binding.endpoint}){self._last_seen(binding.key)}",
                hint=self._what_to_check(binding),
            )
        self._note(
            RecordEntry(
                READ,
                RecordLevel.RESULTS,
                f"read {binding.key} = {_shown(binding, item.value)} on {binding.endpoint} "
                f"after {waited:.3f} s",
                {**_facts(binding, item.value), "outcome": "value", "waited": waited},
            )
        )
        return item.value

    def wait_until(
        self,
        signal: Signal | str,
        expected: Any,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> Any:
        """Wait for a value of a DUT output, received after the most recent write, that equals
        ``expected``, or for which ``expected`` returns true if it is a function. Return it."""
        __tracebackhide__ = True
        binding = self._readable(signal)
        _check_timeout(timeout)
        matches: Callable[[Any], bool] = (
            expected if _is_predicate(expected) else (lambda value: value == expected)
        )
        want = getattr(expected, "__name__", None) if _is_predicate(expected) else None
        expecting = want or repr(expected)
        started = time.monotonic()
        item = self._buffer.wait_for_match(binding.key, matches, timeout)
        waited = time.monotonic() - started
        if item is None:
            last = self._buffer.last(binding.key)
            seen = f"last value {_shown(binding, last.value)}" if last else "nothing received"
            self._note(
                RecordEntry(
                    WAIT,
                    RecordLevel.RESULTS,
                    f"wait_until {binding.key}, expecting {expecting}: not reached within "
                    f"{timeout:g} s on {binding.endpoint}; {seen}",
                    {
                        **_facts(binding, last.value if last else None),
                        "expected": expecting,
                        "outcome": "timeout",
                        "timeout": timeout,
                    },
                )
            )
            raise ReadTimeout(
                f"{binding.key} did not reach {expecting} within {timeout:g} s "
                f"(on {binding.endpoint}){self._last_seen(binding.key)}",
                hint=self._what_to_check(binding),
            )
        self._note(
            RecordEntry(
                WAIT,
                RecordLevel.RESULTS,
                f"wait_until {binding.key}, expecting {expecting}: got "
                f"{_shown(binding, item.value)} on {binding.endpoint} after {waited:.3f} s",
                {
                    **_facts(binding, item.value),
                    "expected": expecting,
                    "outcome": "reached",
                    "waited": waited,
                },
            )
        )
        return item.value

    # --- helpers ----------------------------------------------------------------------------

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
