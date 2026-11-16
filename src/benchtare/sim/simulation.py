"""Wiring a simulated DUT to simulated equipment, following the harness file.

With real hardware the harness is a physical thing. In simulation, this module builds it: each
connection in the harness file becomes a simulated wire between a bench channel and the DUT, or
a DUT port on a virtual CAN bus, and each loopback becomes a wire from one bench channel to
another.

For BenchTare's own tests, ``cut`` and ``fail`` break the simulated setup on purpose, which
proves the self-test catches real problems.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from functools import partial
from typing import Any

import can

from benchtare.backends._can_codec import (
    CanCodec,
    CarryError,
    FrameReceiver,
    frame,
    frame_key,
    open_virtual_bus,
    to_python,
)
from benchtare.backends.base import Backend, BenchError
from benchtare.backends.sim import SimIO
from benchtare.backends.sim_can import SimCan
from benchtare.dut.description import CanSignalRef, InterfaceDescription
from benchtare.harness.config import Endpoint, HarnessConfig
from benchtare.signals.model import Direction, SignalType

log = logging.getLogger(__name__)


class DutCanPort:
    """The simulated DUT's connection to one virtual CAN bus."""

    def __init__(self, equipment: str, bus_name: str, device: Any, *, fd: bool) -> None:
        self.equipment = equipment
        self._device = device
        self._codec = CanCodec()
        self._inputs: dict[tuple[int, bool], list[tuple[Any, str, str, SignalType]]] = {}
        self._outputs: dict[str, CanSignalRef] = {}
        self._bus = open_virtual_bus(bus_name, fd=fd)
        self._frames = FrameReceiver(self._bus, self._on_frame, who=f"simulated DUT on {equipment}")
        self.cut = False

    def add_input(self, name: str, ref: CanSignalRef, signal_type: SignalType) -> None:
        message = self._codec.message(ref)
        self._inputs.setdefault(ref.frame_key, []).append((message, name, ref.signal, signal_type))

    def add_output(self, name: str, ref: CanSignalRef) -> None:
        self._outputs[name] = ref

    @property
    def failure(self) -> str | None:
        return self._frames.failure

    def start(self) -> None:
        self._frames.start()

    def stop(self) -> None:
        self._frames.stop()
        self._bus.shutdown()

    def send(self, name: str, value: Any) -> None:
        ref = self._outputs.get(name)
        if ref is None or self.cut:
            return
        try:
            _, data = self._codec.encode(ref, value)
        except CarryError:
            return  # the DUT can't put this value on the bus; a real one would clip or refuse
        self._bus.send(frame(ref, data))

    def _on_frame(self, received: can.Message) -> None:
        listening = self._inputs.get(frame_key(received))
        if not listening or self.cut:
            return
        decoded_by_message: dict[int, dict[str, Any]] = {}
        for message, name, dbc_name, signal_type in listening:
            decoded = decoded_by_message.get(id(message))
            if decoded is None:
                decoded = decoded_by_message[id(message)] = self._codec.decode(
                    message, bytes(received.data)
                )
            if dbc_name in decoded:
                self._device.set_input(name, to_python(signal_type, decoded[dbc_name]))


class Simulation:
    """A simulated DUT, wired to simulated equipment as the harness file says."""

    def __init__(
        self,
        description: InterfaceDescription,
        harness: HarnessConfig,
        backends: Mapping[str, Backend],
        device_options: Mapping[str, Any] | None = None,
    ) -> None:
        assert harness.model is not None
        self._description = description
        self._harness = harness
        self._backends = backends
        options = {**harness.model_options, **(device_options or {})}
        try:
            self.device = harness.model(self._route, **options)
        except Exception as exc:  # noqa: BLE001 - the team's model: say which, and what to check
            raise BenchError(
                f"the DUT model {harness.simulate} couldn't be created: {type(exc).__name__}: "
                f"{exc}",
                hint=f"its options under 'simulate:' in harness file {harness.path.name}, and its "
                "__init__",
            ) from None
        self._ports: dict[str, DutCanPort] = {}
        self._outputs: dict[str, tuple[Backend | DutCanPort, Endpoint]] = {}

    def start(self) -> None:
        signals = self._description.signals
        for name, endpoint in self._harness.connections.items():
            signal = signals[name]
            backend = self._backends[endpoint.equipment]
            if isinstance(backend, SimCan):
                port = self._port(endpoint.equipment, backend)
                ref = self._description.can_signals[name]
                if signal.direction is Direction.INPUT:
                    port.add_input(name, ref, signal.type)
                else:
                    port.add_output(name, ref)
                    self._outputs[name] = (port, endpoint)
            elif isinstance(backend, SimIO):
                assert endpoint.channel is not None
                if signal.direction is Direction.INPUT:
                    backend.connect_far(endpoint.channel, partial(self.device.set_input, name))
                else:
                    self._outputs[name] = (backend, endpoint)

        for loopback in self._harness.loopbacks:
            source = self._backends[loopback.drive.equipment]
            sink = self._backends[loopback.read.equipment]
            assert isinstance(source, SimIO) and isinstance(sink, SimIO)
            assert loopback.drive.channel is not None and loopback.read.channel is not None
            source.connect_far(
                loopback.drive.channel, partial(sink.set_level, loopback.read.channel)
            )

        for port in self._ports.values():
            port.start()
        self.device.start()
        log.info("simulated DUT %s started", self._harness.simulate)

    def stop(self) -> None:
        self.device.stop()
        for port in self._ports.values():
            port.stop()
        log.info("simulated DUT %s stopped", self._harness.simulate)

    def cut(self, endpoint: str) -> None:
        """Cut the simulated wire at ``equipment.channel``, or unplug the DUT from bus ``equipment``."""
        equipment, _, channel = endpoint.partition(".")
        backend = self._backends.get(equipment)
        if isinstance(backend, SimIO) and channel:
            backend.cut(channel)
        elif isinstance(backend, SimCan) and not channel:
            if equipment in self._ports:
                self._ports[equipment].cut = True
        else:
            raise ValueError(f"nothing to cut at {endpoint!r}")
        log.info("simulated wire cut at %s", endpoint)

    def fail(self, equipment: str, reason: str = "simulated equipment failure") -> None:
        """Make simulated equipment fail: it reports the failure and stops passing values."""
        backend = self._backends[equipment]
        assert isinstance(backend, (SimIO, SimCan))
        backend.fail(reason)

    def _port(self, equipment: str, backend: SimCan) -> DutCanPort:
        port = self._ports.get(equipment)
        if port is None:
            assert backend.bus_name is not None, "open the backend before starting the simulation"
            port = self._ports[equipment] = DutCanPort(
                equipment, backend.bus_name, self.device, fd=backend.fd
            )
        return port

    def check(self) -> list[str]:
        """Problems with the simulated DUT itself. A healthy simulation reports none.

        The reference DUT ignores inputs it can't use and keeps running if one of its own
        actions fails, but it reports what happened here, so a bug in a simulation never
        passes for a DUT that is merely slow.
        """
        dut = f"the simulated DUT {self._harness.simulate}"
        problems = [f"{dut}: {e}" for e in self.device.errors]
        if not self.device.running:
            problems.append(f"{dut} has stopped running")
        for port in self._ports.values():
            if port.failure is not None:
                problems.append(f"{dut}'s port on {port.equipment} {port.failure}")
        return problems

    def _route(self, name: str, value: Any) -> None:
        """Called by the device for each output it produces."""
        target = self._outputs.get(name)
        if target is None:
            signal = self._description.signals.get(name)
            if signal is None or signal.direction is not Direction.OUTPUT:
                # Recorded in the model's errors, so the self-test reports it.
                raise ValueError(
                    f"it published {name!r}, which isn't one of the DUT's outputs in "
                    f"{self._description.path.name}"
                )
            return  # an output this harness doesn't connect
        sink, endpoint = target
        if isinstance(sink, DutCanPort):
            sink.send(name, value)
        elif isinstance(sink, SimIO):
            assert endpoint.channel is not None
            sink.set_level(endpoint.channel, value)
