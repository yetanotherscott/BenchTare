"""The running bench: what tests drive through the ``bench`` fixture.

It routes each signal through the harness to its equipment and backend.

- ``write(signal, value)`` drives a DUT input. Any value the channel can carry is sent.
- ``read(signal, timeout)`` returns the first value of a DUT output received after the bench's
  most recent write. That suits request/response signals and states that have already settled.
- ``wait_until(signal, expected, timeout)`` waits for a value that equals ``expected`` (or
  satisfies it, if it is a predicate). Use it to check how the DUT reacts: a DUT that reports
  cyclically, or a sampled channel, can still deliver the old value just after a write.

See docs/model.md.
"""

from __future__ import annotations

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
from benchtare.signals.model import Direction, Signal, SignalSet
from benchtare.sim.simulation import Simulation

if TYPE_CHECKING:
    from typing_extensions import Self  # typing.Self needs Python 3.11

DEFAULT_TIMEOUT = 1.0


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
        return self

    def close(self) -> None:
        if self.simulation is not None:
            self.simulation.stop()
            self.simulation = None
        for backend in self._backends.values():
            backend.close()
        self._open = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def check(self) -> dict[str, list[str]]:
        """The problems each equipment item reports; empty lists when all is healthy."""
        return {name: backend.check() for name, backend in self._backends.items()}

    # --- what tests call --------------------------------------------------------------------

    def write(self, signal: Signal | str, value: Any) -> None:
        """Drive a DUT input. Raise ``WriteError`` if its channel can't carry ``value``."""
        binding = self._binding(signal)
        if not binding.drive:
            raise WriteError(
                f"can't write {binding.key}: it is a DUT output, which the bench reads",
                hint="tests write DUT inputs and read DUT outputs; see the direction in "
                f"{self.description.path.name}",
            )
        backend = self._backends[binding.equipment]
        with self._buffer.writing():
            backend.write(binding, value)

    def read(self, signal: Signal | str, timeout: float = DEFAULT_TIMEOUT) -> Any:
        """Return the first value of a DUT output received after the most recent write."""
        binding = self._readable(signal)
        item = self._buffer.first_after_write(binding.key, timeout)
        if item is None:
            raise ReadTimeout(
                f"no value of {binding.key} arrived within {timeout:g} s after the last write "
                f"(on {binding.endpoint}){self._last_seen(binding.key)}",
                hint=self._what_to_check(binding),
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
        binding = self._readable(signal)
        matches: Callable[[Any], bool] = (
            expected if _is_predicate(expected) else (lambda value: value == expected)
        )
        item = self._buffer.wait_for_match(binding.key, matches, timeout)
        if item is None:
            want = getattr(expected, "__name__", None) if _is_predicate(expected) else None
            raise ReadTimeout(
                f"{binding.key} did not reach {want or repr(expected)} within {timeout:g} s "
                f"(on {binding.endpoint}){self._last_seen(binding.key)}",
                hint=self._what_to_check(binding),
            )
        return item.value

    # --- helpers ----------------------------------------------------------------------------

    def _binding(self, signal: Signal | str) -> Binding:
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
        binding = self._binding(signal)
        if binding.drive:
            raise BenchError(
                f"can't read {binding.key}: it is a DUT input, which the bench drives",
                hint=f"tests read DUT outputs; see the direction in {self.description.path.name}",
            )
        return binding

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


def _is_predicate(expected: Any) -> bool:
    # pytest.approx and similar comparison helpers are not callable; plain functions are.
    return callable(expected) and not isinstance(expected, type)
