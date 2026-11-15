"""CAN plumbing shared by the bench and simulated sides: encoding and decoding DUT signals in
frames, opening virtual buses, and receiving frames on a thread."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import can
import cantools

from benchtare.dut.description import CanSignalRef
from benchtare.signals.model import SignalType

log = logging.getLogger(__name__)


class CarryError(ValueError):
    """A value can't be carried by its signal's bits in the frame."""

    def __init__(self, reason: str, limits: str | None = None) -> None:
        self.reason = reason
        self.limits = limits
        super().__init__(reason)


class FrameError(ValueError):
    """A received frame doesn't match its message in the DBC, so it can't be decoded."""


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
        """Decode a received frame's data. Raise ``FrameError`` if it doesn't match the DBC."""
        try:
            return message.decode(data, decode_choices=True, allow_truncated=True)
        except Exception as exc:
            # cantools raises many kinds; all mean "this frame isn't what the DBC says".
            raise FrameError(f"message {message.name}: {exc}") from exc


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


def describe_frame(received: can.Message) -> str:
    """A frame as an engineer would read it off a bus trace: ``0x101 [8] 07 01 00 ...``."""
    width = 8 if received.is_extended_id else 3
    kind = " (extended)" if received.is_extended_id else ""
    data = bytes(received.data).hex(" ")
    return f"0x{received.arbitration_id:0{width}x}{kind} [{len(received.data)}] {data}".rstrip()


class FrameReceiver:
    """Receives frames from a bus on a thread of its own and hands each one to ``handle``.

    python-can's Notifier stops receiving for good if a handler raises, which would leave every
    later read timing out with nothing said. Here:

    - A frame that doesn't match the DBC (``FrameError``) is logged as a warning and skipped.
    - Any other error while handling a frame is logged with its traceback, and skipped.
    - If the bus itself fails, reception stops, the failure is logged, and ``failure`` says
      what happened, so the backend's ``check()`` can report it.

    Each kind of bad frame is logged at most once per ``quiet_period`` seconds, with a count of
    the ones in between, so a DUT sending a bad frame every 10 ms doesn't flood the log.
    """

    quiet_period = 1.0

    def __init__(
        self,
        bus: can.BusABC,
        handle: Callable[[can.Message], None],
        *,
        who: str,
        poll: float = 0.05,
    ) -> None:
        self.who = who
        self.failure: str | None = None
        self._bus = bus
        self._handle = handle
        self._poll = poll
        self._stop = threading.Event()
        self._last_logged: dict[tuple[int, bool], float] = {}
        self._unlogged: dict[tuple[int, bool], int] = {}
        self._thread = threading.Thread(target=self._run, name=f"benchtare-rx-{who}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                received = self._bus.recv(self._poll)
            except Exception as exc:  # noqa: BLE001 - any bus error ends reception
                if not self._stop.is_set():  # an error while closing isn't a failure
                    self.failure = f"stopped receiving: {type(exc).__name__}: {exc}"
                    log.error("%s %s", self.who, self.failure)
                return
            if received is None:
                continue
            try:
                self._handle(received)
            except FrameError as exc:
                self._log_bad_frame(
                    received,
                    logging.WARNING,
                    f"{self.who}: ignored a frame that doesn't match the DBC: "
                    f"{describe_frame(received)}: {exc}\n"
                    "  Check: the DBC files describe what the DUT sends",
                )
            except Exception as exc:  # noqa: BLE001 - one bad frame mustn't stop reception
                self._log_bad_frame(
                    received,
                    logging.ERROR,
                    f"{self.who}: failed to handle a frame: {describe_frame(received)}: "
                    f"{type(exc).__name__}: {exc}",
                    exc_info=True,
                )

    def _log_bad_frame(
        self, received: can.Message, level: int, message: str, *, exc_info: bool = False
    ) -> None:
        key = frame_key(received)
        now = time.monotonic()
        last = self._last_logged.get(key)
        if last is not None and now - last < self.quiet_period:
            self._unlogged[key] = self._unlogged.get(key, 0) + 1
            return
        self._last_logged[key] = now
        unlogged = self._unlogged.pop(key, 0)
        if unlogged:
            message += f"\n  ({unlogged} more like it since the last report)"
        log.log(level, "%s", message, exc_info=exc_info)


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
