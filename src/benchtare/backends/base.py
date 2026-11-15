"""The Backend protocol, and the receive buffer that implements the read rule.

A backend drives one kind of bench equipment. It is told which signals (and loopback ends) are
bound to its channels, writes values to them, and delivers every value it receives to the
bench's receive buffer. It never interprets values: pass criteria live in the tests. It also
notes, for the test record, anything unexpected it sees (a frame that doesn't match the DBC,
equipment that stopped working) and, for the debug level, the frames it sends and receives.

The read rule: ``Bench.read`` returns the first value received after the bench's most recent
write. Writes and deliveries share one lock, so a value delivered while a write is in progress
always counts as after the write, and a reply that arrives before ``read`` is called is never
missed. See docs/model.md.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal, Protocol, runtime_checkable

from benchtare.dut.description import CanSignalRef
from benchtare.errors import BenchTareError, Problem
from benchtare.record import RecordEntry, RecordLevel
from benchtare.signals.model import Signal

EquipmentKind = Literal["channels", "bus"]


class BenchError(BenchTareError):
    """Something went wrong while driving or reading the bench."""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        self.message = message
        self.hint = hint
        super().__init__(str(self))

    def __str__(self) -> str:
        return self.message + (f"\n  Check: {self.hint}" if self.hint else "")


class WriteError(BenchError):
    """A value could not be written: the channel can't carry it, or the equipment has failed."""


class ReadTimeout(BenchError):
    """No value (or no matching value) arrived in time."""


@dataclass(frozen=True)
class ChannelCaps:
    """What the bench can do with one channel."""

    drive: bool = False
    read: bool = False


@dataclass(frozen=True)
class Binding:
    """Something bound to a backend: a DUT signal, or one end of a loopback.

    ``key`` is the signal name, or ``"equipment.channel"`` for a loopback end. ``channel`` is
    None on a bus, where the signal's encoding (``can``) says where it sits in the frames.
    """

    key: str
    equipment: str
    channel: str | None
    drive: bool
    signal: Signal | None = None
    can: CanSignalRef | None = None

    @property
    def endpoint(self) -> str:
        return self.equipment if self.channel is None else f"{self.equipment}.{self.channel}"


class Receiver(Protocol):
    """Where a backend delivers received values. ``lock`` is held by every write."""

    lock: threading.RLock

    def deliver(self, key: str, value: Any) -> None: ...

    def note(self, entry: RecordEntry) -> None:
        """Add an entry to the test record. Callable from any thread."""
        ...

    def wants(self, level: RecordLevel) -> bool:
        """Whether the test record keeps entries of ``level``; skip building them if not."""
        ...


@dataclass(frozen=True)
class EquipmentIdentity:
    """What a backend can tell about its equipment, stored with each run's results. Anything it
    can't tell stays None. A simulated backend leaves the hardware fields empty."""

    vendor: str | None = None
    model: str | None = None
    serial_number: str | None = None
    firmware_version: str | None = None
    driver: str | None = None
    driver_version: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class Backend(Protocol):
    """Drives one item of bench equipment. See the module docstring."""

    equipment: str

    def open(self, bindings: Sequence[Binding], receiver: Receiver) -> None: ...

    def close(self) -> None: ...

    def check(self) -> list[str]:
        """Return the problems found with the equipment; empty when it is healthy."""
        ...

    def write(self, binding: Binding, value: Any) -> None:
        """Drive a value. Raise ``WriteError`` if the channel can't carry it."""
        ...

    def identity(self) -> EquipmentIdentity:
        """The equipment's vendor, model, serial number, firmware and driver, as far as it can
        report them. Called once the backend is open."""
        ...


@runtime_checkable
class BusBackend(Backend, Protocol):
    """A backend for a bus (``kind = "bus"``), which can also tell whether anything is on it."""

    def wait_for_traffic(self, timeout: float) -> bool:
        """Whether any frame has been received since the bus opened, waiting up to ``timeout``."""
        ...


class BackendType(Protocol):
    """The class of a backend, as named by ``backend:`` in a bench file."""

    name: ClassVar[str]
    kind: ClassVar[EquipmentKind]
    simulated: ClassVar[bool]

    @classmethod
    def describe(
        cls, equipment: str, options: Mapping[str, Any], problems: list[Problem]
    ) -> dict[str, ChannelCaps]:
        """Check the bench-file options for one equipment item and return its channels.

        Append to ``problems`` instead of raising. A bus returns no channels.
        """
        ...

    def __call__(
        self, equipment: str, options: Mapping[str, Any], channels: Mapping[str, ChannelCaps]
    ) -> Backend: ...


@dataclass(frozen=True)
class Received:
    """A value as it arrived, with its place in the bench's sequence of events."""

    seq: int
    value: Any
    at: float  # time.monotonic()


class ReceiveBuffer:
    """Holds received values and implements the read rule. Shared by all backends of a bench."""

    MAX_PER_KEY = 1000

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self._changed = threading.Condition(self.lock)
        self._seq = 0
        self._marker = 0
        self._since_write: dict[str, deque[Received]] = {}
        self._first: dict[str, Received] = {}
        self._last: dict[str, Received] = {}
        # The bench's handler for test-record entries that backends note, and the level it keeps.
        self.on_note: Callable[[RecordEntry], None] | None = None
        self.note_level = RecordLevel.RESULTS

    def note(self, entry: RecordEntry) -> None:
        handler = self.on_note
        if handler is not None:
            handler(entry)

    def wants(self, level: RecordLevel) -> bool:
        return self.on_note is not None and level <= self.note_level

    @contextmanager
    def writing(self) -> Iterator[None]:
        """Hold the lock while a write is sent. Values delivered from now on count as after it."""
        with self.lock:
            self._seq += 1
            self._marker = self._seq
            self._since_write.clear()
            self._first.clear()
            yield

    def deliver(self, key: str, value: Any) -> None:
        with self._changed:
            self._seq += 1
            item = Received(self._seq, value, time.monotonic())
            self._last[key] = item
            self._first.setdefault(key, item)
            queue = self._since_write.get(key)
            if queue is None:
                queue = self._since_write[key] = deque(maxlen=self.MAX_PER_KEY)
            queue.append(item)
            self._changed.notify_all()

    def first_after_write(self, key: str, timeout: float) -> Received | None:
        """The first value of ``key`` received after the most recent write, or None on timeout."""
        with self._changed:
            self._changed.wait_for(lambda: key in self._first, timeout)
            return self._first.get(key)

    def wait_for_match(
        self, key: str, matches: Callable[[Any], bool], timeout: float
    ) -> Received | None:
        """The first value of ``key`` after the most recent write for which ``matches`` is true."""
        deadline = time.monotonic() + timeout
        checked = 0
        with self._changed:
            while True:
                for item in self._since_write.get(key, ()):
                    if item.seq <= checked:
                        continue
                    checked = item.seq
                    if matches(item.value):
                        return item
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._changed.wait(remaining)

    def wait_seen(self, key: str, timeout: float) -> Received | None:
        """The most recent value of ``key`` received since the bench opened, waiting up to
        ``timeout`` for the first one. The self-test's presence check uses it."""
        with self._changed:
            self._changed.wait_for(lambda: key in self._last, timeout)
            return self._last.get(key)

    def last(self, key: str) -> Received | None:
        """The most recent value of ``key`` ever received, for error messages."""
        with self.lock:
            return self._last.get(key)
