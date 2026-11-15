"""``sim_can``: a simulated CAN interface on a python-can virtual bus.

The bus is a single channel. Each signal's place in the frames comes from the DBC files in the
interface description. Every ``open()`` uses its own virtual bus name, so two benches in one
process never hear each other, unless the bench file fixes the name with ``bus:``.

Like a real interface, the bus carries CAN FD frames only when it is opened in FD mode.

Frames are received on a thread of their own (see ``FrameReceiver``). A frame that doesn't
match the DBC is logged and skipped; if the bus fails, ``check()`` reports it.

Bench file:

    can0:
      backend: sim_can
      bus: my-bus          # optional virtual bus name
      fd: true             # optional; needed for CAN FD messages
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

import can

from benchtare._yaml import kind_of
from benchtare.backends._can_codec import (
    CanCodec,
    CarryError,
    FrameReceiver,
    describe_frame,
    frame,
    frame_key,
    open_virtual_bus,
    to_python,
)
from benchtare.backends.base import Binding, ChannelCaps, EquipmentKind, Receiver, WriteError
from benchtare.errors import Problem

log = logging.getLogger(__name__)

_OPTIONS = ("bus", "fd")


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
                        hint="a CAN bus has no channels; its options are 'bus' "
                        "(the virtual bus name) and 'fd' (CAN FD mode)",
                    )
                )
        bus = options.get("bus")
        if bus is not None and (not isinstance(bus, str) or not bus):
            problems.append(Problem("'bus:' must be a name", where=where))
        fd = options.get("fd", False)
        if not isinstance(fd, bool):
            problems.append(Problem(f"'fd:' must be true or false, not {kind_of(fd)}", where=where))
        return {}

    @staticmethod
    def carries_fd(options: Mapping[str, Any]) -> bool:
        return options.get("fd") is True

    def __init__(
        self, equipment: str, options: Mapping[str, Any], channels: Mapping[str, ChannelCaps]
    ) -> None:
        self.equipment = equipment
        self.fd = self.carries_fd(options)
        self._fixed_bus_name: str | None = options.get("bus")
        self.bus_name: str | None = None
        self._bus: can.BusABC | None = None
        self._frames: FrameReceiver | None = None
        self._codec = CanCodec()
        self._receiver: Receiver | None = None
        self._rx: dict[tuple[int, bool], list[tuple[Any, Binding]]] = {}
        self._failure: str | None = None

    def open(self, bindings: Sequence[Binding], receiver: Receiver) -> None:
        self._receiver = receiver
        for binding in bindings:
            if binding.drive or binding.can is None:
                continue
            message = self._codec.message(binding.can)
            self._rx.setdefault(binding.can.frame_key, []).append((message, binding))
        self.bus_name = self._fixed_bus_name or f"benchtare-{self.equipment}-{uuid.uuid4().hex}"
        self._bus = open_virtual_bus(self.bus_name, fd=self.fd)
        self._frames = FrameReceiver(self._bus, self._on_frame, who=self.equipment)
        self._frames.start()
        log.debug(
            "%s: opened virtual CAN bus %s%s",
            self.equipment,
            self.bus_name,
            " (FD)" if self.fd else "",
        )

    def close(self) -> None:
        if self._frames is not None:
            self._frames.stop()
            self._frames = None
        if self._bus is not None:
            self._bus.shutdown()
            self._bus = None
            log.debug("%s: closed virtual CAN bus %s", self.equipment, self.bus_name)

    def check(self) -> list[str]:
        if self._failure is not None:
            return [self._failure]
        if self._bus is None or self._frames is None:
            return ["not open"]
        if self._frames.failure is not None:
            return [self._frames.failure]
        return []

    def write(self, binding: Binding, value: Any) -> None:
        if self._failure is not None:
            raise WriteError(
                f"can't write {binding.key} on {self.equipment}: {self._failure}",
                hint=f"equipment '{self.equipment}'",
            )
        ref = binding.can
        assert ref is not None and self._bus is not None
        if ref.fd and not self.fd:
            raise WriteError(
                f"can't write {binding.key} on {self.equipment}: message {ref.message} is a "
                "CAN FD frame, and the bus isn't in FD mode",
                hint=f"set fd: true for {self.equipment} in the bench file",
            )
        try:
            _, data = self._codec.encode(ref, value)
        except CarryError as exc:
            raise WriteError(
                f"can't write {binding.key} = {value!r} on {self.equipment}: it doesn't fit "
                f"in message {ref.message} ({exc.reason})",
                hint=exc.limits,
            ) from None
        sent = frame(ref, data)
        self._bus.send(sent)
        log.debug("%s: sent %s %s", self.equipment, ref.message, describe_frame(sent))

    def fail(self, reason: str = "simulated equipment failure") -> None:
        """Simulate a failed CAN interface: check() reports it, and nothing passes."""
        self._failure = reason
        log.info("%s: simulated failure: %s", self.equipment, reason)

    def _on_frame(self, received: can.Message) -> None:
        listening = self._rx.get(frame_key(received))
        receiver = self._receiver
        if not listening or receiver is None or self._failure is not None:
            return
        decoded_by_message: dict[int, dict[str, Any]] = {}
        for message, binding in listening:
            assert binding.can is not None
            decoded = decoded_by_message.get(id(message))
            if decoded is None:
                decoded = decoded_by_message[id(message)] = self._codec.decode(
                    message, bytes(received.data)
                )
            if binding.can.signal in decoded:
                signal_type = binding.signal.type if binding.signal else None
                receiver.deliver(binding.key, to_python(signal_type, decoded[binding.can.signal]))
