"""Encoding and decoding DUT signals in CAN frames, shared by the bench and simulated sides."""

from __future__ import annotations

from pathlib import Path
from typing import Any

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
        self._state: dict[int, dict[str, Any]] = {}

    def message(self, ref: CanSignalRef) -> Any:
        database = self._databases.get(ref.dbc)
        if database is None:
            database = cantools.database.load_file(str(ref.dbc), database_format="dbc")
            self._databases[ref.dbc] = database
        return database.get_message_by_frame_id(ref.frame_id)

    def encode(self, ref: CanSignalRef, value: Any) -> tuple[Any, bytes]:
        """Set one signal and return (message, frame data). Raise ``CarryError``."""
        message = self.message(ref)
        state = self._state.setdefault(ref.frame_id, _initial_values(message))
        dbc_signal = message.get_signal_by_name(ref.signal)
        candidate = dict(state)
        candidate[ref.signal] = _to_dbc(value)
        try:
            data = message.encode(candidate, strict=False)
        except KeyError:
            choices = [str(v) for v in (dbc_signal.choices or {}).values()]
            raise CarryError(
                f"{value!r} is not one of its values ({', '.join(choices)})",
                "a value name, or a raw number to send an undefined code",
            ) from None
        except (OverflowError, ValueError, TypeError) as exc:
            raise CarryError(str(exc), _limits(dbc_signal)) from None
        state[ref.signal] = candidate[ref.signal]
        return message, data

    def decode(self, message: Any, data: bytes) -> dict[str, Any]:
        return message.decode(data, decode_choices=True, allow_truncated=True)


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
