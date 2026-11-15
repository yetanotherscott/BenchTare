"""Wiring a simulated DUT to simulated equipment, following the harness file.

With real hardware the harness is a physical thing. In simulation, this module builds it: each
connection in the harness file becomes a simulated wire between a bench channel and the DUT, or
a DUT port on a virtual CAN bus, and each loopback becomes a wire from one bench channel to
another.

For BenchTare's own tests, ``cut`` and ``fail`` break the simulated setup on purpose, which
proves the self-test catches real problems.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import partial
from typing import Any

import can

from benchtare.backends._can_codec import CanCodec, CarryError, to_python
from benchtare.backends.base import Backend
from benchtare.backends.sim import SimIO
from benchtare.backends.sim_can import SimCan
from benchtare.dut.description import CanSignalRef, InterfaceDescription
from benchtare.harness.config import Endpoint, HarnessConfig
from benchtare.signals.model import Direction, SignalType
from benchtare.sim.registry import SIMULATED_DUTS


class DutCanPort:
    """The simulated DUT's connection to one virtual CAN bus."""

    def __init__(self, bus_name: str, device: Any) -> None:
        self._device = device
        self._codec = CanCodec()
        self._inputs: dict[int, tuple[Any, list[tuple[str, str, SignalType]]]] = {}
        self._outputs: dict[str, CanSignalRef] = {}
        self._bus = can.Bus(interface="virtual", channel=bus_name)
        self._notifier: can.Notifier | None = None
        self.cut = False

    def add_input(self, name: str, ref: CanSignalRef, signal_type: SignalType) -> None:
        message = self._codec.message(ref)
        _, listening = self._inputs.setdefault(ref.frame_id, (message, []))
        listening.append((name, ref.signal, signal_type))

    def add_output(self, name: str, ref: CanSignalRef) -> None:
        self._outputs[name] = ref

    def start(self) -> None:
        self._notifier = can.Notifier(self._bus, [self._on_frame], timeout=0.05)

    def stop(self) -> None:
        if self._notifier is not None:
            self._notifier.stop(timeout=1.0)
            self._notifier = None
        self._bus.shutdown()

    def send(self, name: str, value: Any) -> None:
        ref = self._outputs.get(name)
        if ref is None or self.cut:
            return
        try:
            message, data = self._codec.encode(ref, value)
        except CarryError:
            return  # the DUT can't put this value on the bus; a real one would clip or refuse
        self._bus.send(
            can.Message(
                arbitration_id=message.frame_id,
                data=data,
                is_extended_id=message.is_extended_frame,
            )
        )

    def _on_frame(self, frame: can.Message) -> None:
        entry = self._inputs.get(frame.arbitration_id)
        if entry is None or self.cut:
            return
        message, listening = entry
        decoded = self._codec.decode(message, bytes(frame.data))
        for name, dbc_name, signal_type in listening:
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
        assert harness.simulate is not None
        self._description = description
        self._harness = harness
        self._backends = backends
        self.device = SIMULATED_DUTS[harness.simulate](self._route, **(device_options or {}))
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

    def stop(self) -> None:
        self.device.stop()
        for port in self._ports.values():
            port.stop()

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

    def fail(self, equipment: str, reason: str = "simulated equipment failure") -> None:
        """Make simulated equipment fail: it reports the failure and stops passing values."""
        backend = self._backends[equipment]
        assert isinstance(backend, (SimIO, SimCan))
        backend.fail(reason)

    def _port(self, equipment: str, backend: SimCan) -> DutCanPort:
        port = self._ports.get(equipment)
        if port is None:
            assert backend.bus_name is not None, "open the backend before starting the simulation"
            port = self._ports[equipment] = DutCanPort(backend.bus_name, self.device)
        return port

    def _route(self, name: str, value: Any) -> None:
        """Called by the device for each output it produces."""
        target = self._outputs.get(name)
        if target is None:
            return  # not connected in this harness
        sink, endpoint = target
        if isinstance(sink, DutCanPort):
            sink.send(name, value)
        elif isinstance(sink, SimIO):
            assert endpoint.channel is not None
            sink.set_level(endpoint.channel, value)
