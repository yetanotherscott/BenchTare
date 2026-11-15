"""Encoding and decoding DUT signals in CAN frames, shared by the bench and simulated sides."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import can
import cantools

from benchtare.dut.description import CanSignalRef
from benchtare.signals.model import SignalType


class CarryError(ValueError):
    """A value can't be carried by its signal's bits in the frame."""

    def __init__(self, reason: str, limits: str | None = None) -> None:
        self.reason = reason
        self.limits = limits
        super().__init__(reason)


class CanCodec:
    """Loads DBC files once, and keeps the latest value of every signal in each sent message.

    A frame always carries every signal in its message, so writing one signal resends the
    others at their latest values (their DBC initial value until first written).
    """

    def __init__(self) -> None:
        self._databases: dict[Path, Any] = {}
        self._state: dict[tuple[Path, str], dict[str, Any]] = {}

    def message(self, ref: CanSignalRef) -> Any:
        database = self._databases.get(ref.dbc)
        if database is None:
            database = cantools.database.load_file(str(ref.dbc), database_format="dbc")
            self._databases[ref.dbc] = database
        # By name: a standard and an extended frame may share an ID number.
        return database.get_message_by_name(ref.message)

    def encode(self, ref: CanSignalRef, value: Any) -> tuple[Any, bytes]:
        """Set one signal and return (message, frame data). Raise ``CarryError``."""
        message = self.message(ref)
        state = self._state.setdefault((ref.dbc, ref.message), _initial_values(message))
        dbc_signal = message.get_signal_by_name(ref.signal)
        if isinstance(value, str) and dbc_signal.choices:
            codes = [code for code, name in dbc_signal.choices.items() if str(name) == value]
            if not codes:
                names = ", ".join(dict.fromkeys(str(v) for v in dbc_signal.choices.values()))
                raise CarryError(
                    f"{value!r} is not one of its values ({names})",
                    "a value name, or a raw number to send an undefined code",
                )
            if len(codes) > 1:
                raise CarryError(
                    f"{value!r} is the name of codes {', '.join(str(c) for c in codes)}",
                    f"write the raw code you mean, for example bench.write(signal, {codes[0]})",
                )
        candidate = dict(state)
        candidate[ref.signal] = _to_dbc(value)
        try:
            data = message.encode(candidate, strict=False)
        except Exception as exc:  # noqa: BLE001 - cantools raises many kinds; all mean "can't encode"
            raise CarryError(str(exc) or type(exc).__name__, _limits(dbc_signal)) from None
        state[ref.signal] = candidate[ref.signal]
        return message, data

    def decode(self, message: Any, data: bytes) -> dict[str, Any]:
        return message.decode(data, decode_choices=True, allow_truncated=True)


def open_virtual_bus(name: str, *, fd: bool) -> can.BusABC:
    """A python-can virtual bus that ignores python-can's own configuration.

    Without ``ignore_config``, a ``~/.canrc``, a ``can.ini`` or the ``CAN_*`` environment
    variables on the machine would quietly change how the simulated bus behaves.
    """
    return can.Bus(interface="virtual", channel=name, fd=fd, ignore_config=True)


def frame(ref: CanSignalRef, data: bytes) -> can.Message:
    """The CAN frame that carries a message, with its ID type and FD flag from the DBC."""
    return can.Message(
        arbitration_id=ref.frame_id, data=data, is_extended_id=ref.extended, is_fd=ref.fd
    )


def frame_key(received: can.Message) -> tuple[int, bool]:
    return (received.arbitration_id, bool(received.is_extended_id))


def to_python(signal_type: SignalType | None, value: Any) -> Any:
    """Convert a decoded DBC value to the Python value tests see."""
    if signal_type is SignalType.ENUM:
        return value if isinstance(value, (int, float)) else str(value)  # raw for unknown codes
    if signal_type is SignalType.BOOL:
        return bool(value)
    if signal_type is SignalType.INT:
        return int(value)
    if signal_type is SignalType.FLOAT:
        return float(value)
    return value


def _to_dbc(value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    return value


def _initial_values(message: Any) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for dbc_signal in message.signals:
        if dbc_signal.initial is not None:
            values[dbc_signal.name] = dbc_signal.initial
        else:
            values[dbc_signal.name] = dbc_signal.offset  # raw 0
    return values


def _limits(dbc_signal: Any) -> str:
    bits = dbc_signal.length
    if dbc_signal.is_float:
        return f"it is a {bits}-bit float"
    if dbc_signal.is_signed:
        low_raw, high_raw = -(2 ** (bits - 1)), 2 ** (bits - 1) - 1
    else:
        low_raw, high_raw = 0, 2**bits - 1
    scale, offset = dbc_signal.scale, dbc_signal.offset
    low, high = sorted((low_raw * scale + offset, high_raw * scale + offset))
    return f"its {bits} bits (scale {scale:g}, offset {offset:g}) carry {low:g} to {high:g}"
