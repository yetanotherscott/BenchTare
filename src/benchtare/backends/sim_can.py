"""``sim_can``: a simulated CAN interface on a python-can virtual bus.

The bus is a single channel. Each signal's place in the frames comes from the DBC files in the
interface description. Every ``open()`` uses its own virtual bus name, so two benches in one
process never hear each other, unless the bench file fixes the name with ``bus:``.

Bench file:

    can0:
      backend: sim_can
      bus: my-bus          # optional virtual bus name
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

import can

from benchtare.backends._can_codec import CanCodec, CarryError, to_python
from benchtare.backends.base import Binding, ChannelCaps, EquipmentKind, Receiver, WriteError
from benchtare.errors import Problem

_OPTIONS = ("bus",)


class SimCan:
    name: ClassVar[str] = "sim_can"
    kind: ClassVar[EquipmentKind] = "bus"
    simulated: ClassVar[bool] = True

    @classmethod
    def describe(
        cls, equipment: str, options: Mapping[str, Any], problems: list[Problem]
    ) -> dict[str, ChannelCaps]:
        where = f"equipment '{equipment}'"
        for key in options:
            if key not in _OPTIONS:
                problems.append(
                    Problem(
                        f"unknown option '{key}' for a sim_can backend",
                        where=where,
                        hint="a CAN bus has no channels; its only option is 'bus' "
                        "(the virtual bus name)",
                    )
                )
        bus = options.get("bus")
        if bus is not None and (not isinstance(bus, str) or not bus):
            problems.append(Problem("'bus:' must be a name", where=where))
        return {}

    def __init__(
        self, equipment: str, options: Mapping[str, Any], channels: Mapping[str, ChannelCaps]
    ) -> None:
        self.equipment = equipment
        self._fixed_bus_name: str | None = options.get("bus")
        self.bus_name: str | None = None
        self._bus: can.BusABC | None = None
        self._notifier: can.Notifier | None = None
        self._codec = CanCodec()
        self._receiver: Receiver | None = None
        self._rx: dict[int, tuple[Any, list[Binding]]] = {}
        self._failure: str | None = None

    def open(self, bindings: Sequence[Binding], receiver: Receiver) -> None:
        self._receiver = receiver
        for binding in bindings:
            if binding.drive or binding.can is None:
                continue
            message = self._codec.message(binding.can)
            _, listening = self._rx.setdefault(binding.can.frame_id, (message, []))
            listening.append(binding)
        self.bus_name = self._fixed_bus_name or f"benchtare-{self.equipment}-{uuid.uuid4().hex}"
        self._bus = can.Bus(interface="virtual", channel=self.bus_name)
        self._notifier = can.Notifier(self._bus, [self._on_frame], timeout=0.05)

    def close(self) -> None:
        if self._notifier is not None:
            self._notifier.stop(timeout=1.0)
            self._notifier = None
        if self._bus is not None:
            self._bus.shutdown()
            self._bus = None

    def check(self) -> list[str]:
        if self._failure is not None:
            return [self._failure]
        if self._bus is None:
            return ["not open"]
        return []

    def write(self, binding: Binding, value: Any) -> None:
        if self._failure is not None:
            raise WriteError(
                f"can't write {binding.key} on {self.equipment}: {self._failure}",
                hint=f"equipment '{self.equipment}'",
            )
        assert binding.can is not None and self._bus is not None
        try:
            message, data = self._codec.encode(binding.can, value)
        except CarryError as exc:
            raise WriteError(
                f"can't write {binding.key} = {value!r} on {self.equipment}: it doesn't fit "
                f"in message {binding.can.message} ({exc.reason})",
                hint=exc.limits,
            ) from None
        self._bus.send(
            can.Message(
                arbitration_id=message.frame_id,
                data=data,
                is_extended_id=message.is_extended_frame,
            )
        )

    def fail(self, reason: str = "simulated equipment failure") -> None:
        """Simulate a failed CAN interface: check() reports it, and nothing passes."""
        self._failure = reason

    def _on_frame(self, frame: can.Message) -> None:
        entry = self._rx.get(frame.arbitration_id)
        receiver = self._receiver
        if entry is None or receiver is None or self._failure is not None:
            return
        message, listening = entry
        decoded = self._codec.decode(message, bytes(frame.data))
        for binding in listening:
            assert binding.can is not None
            if binding.can.signal in decoded:
                signal_type = binding.signal.type if binding.signal else None
                receiver.deliver(binding.key, to_python(signal_type, decoded[binding.can.signal]))
