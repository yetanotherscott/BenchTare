"""``sim``: simulated I/O equipment, in-process, with named channels.

Driven channels pass each written value to whatever the simulation wired to them. Read channels
hold a level, set by the simulation, and are sampled like a DAQ: the current level is delivered
every ``sample_period`` seconds. A channel with no level (nothing connected, or a cut wire)
delivers nothing. If sampling fails, the error is logged, put in the test record, and
``check()`` reports it.

Bench file:

    io:
      backend: sim
      sample_period: 0.01     # optional, seconds
      channels:
        out1: {drive: true}
        in1: {read: true}
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any, ClassVar

from benchtare import __version__
from benchtare._yaml import YAML_VALUE_WORDS, kind_of
from benchtare.backends.base import (
    Binding,
    ChannelCaps,
    EquipmentIdentity,
    EquipmentKind,
    Receiver,
    WriteError,
)
from benchtare.errors import Problem
from benchtare.record import EQUIPMENT_FAILURE, RecordEntry, RecordLevel

log = logging.getLogger(__name__)

_OPTIONS = ("channels", "sample_period")
_NO_LEVEL = object()


class SimIO:
    name: ClassVar[str] = "sim"
    uuid: ClassVar[str] = "12feeba6-1032-4c47-b17d-81ec67f3cb40"
    api_version: ClassVar[int] = 1
    kind: ClassVar[EquipmentKind] = "channels"
    simulated: ClassVar[bool] = True

    DEFAULT_SAMPLE_PERIOD = 0.01

    @classmethod
    def describe(
        cls, equipment: str, options: Mapping[str, Any], problems: list[Problem]
    ) -> dict[str, ChannelCaps]:
        where = f"equipment '{equipment}'"
        for key in options:
            if key not in _OPTIONS:
                problems.append(
                    Problem(
                        f"unknown option '{key}' for a sim backend",
                        where=where,
                        hint=f"sim options: {', '.join(_OPTIONS)}",
                    )
                )
        period = options.get("sample_period", cls.DEFAULT_SAMPLE_PERIOD)
        if isinstance(period, bool) or not isinstance(period, (int, float)) or period <= 0:
            problems.append(
                Problem("sample_period must be a number of seconds above 0", where=where)
            )
        raw = options.get("channels")
        if not isinstance(raw, dict) or not raw:
            problems.append(
                Problem(
                    "a sim backend needs 'channels:'",
                    where=where,
                    hint="for example  channels: {out1: {drive: true}, in1: {read: true}}",
                )
            )
            return {}
        channels: dict[str, ChannelCaps] = {}
        for channel, caps in raw.items():
            if channel is None or isinstance(channel, bool):
                problems.append(
                    Problem(
                        f"a channel name was read as {kind_of(channel)}, not as a name",
                        where=where,
                        hint=f"quote it, for example 'on': {{read: true}}; {YAML_VALUE_WORDS}",
                    )
                )
                continue
            channel = str(channel)  # numbered channels such as 0: are fine
            if not _valid_name(channel):
                problems.append(
                    Problem(
                        f"channel name '{channel}' can't be used",
                        where=where,
                        hint="use letters, digits, '_' and '-', with no dots",
                    )
                )
                continue
            if not isinstance(caps, dict) or set(caps) - {"drive", "read"}:
                problems.append(
                    Problem(
                        f"channel '{channel}' must say {{drive: true}}, {{read: true}} or both",
                        where=where,
                    )
                )
                continue
            drive, read = caps.get("drive", False), caps.get("read", False)
            if not isinstance(drive, bool) or not isinstance(read, bool) or not (drive or read):
                problems.append(
                    Problem(
                        f"channel '{channel}' must be drivable, readable or both",
                        where=where,
                        hint="set drive: true and/or read: true",
                    )
                )
                continue
            channels[channel] = ChannelCaps(drive=drive, read=read)
        return channels

    def __init__(
        self, equipment: str, options: Mapping[str, Any], channels: Mapping[str, ChannelCaps]
    ) -> None:
        self.equipment = equipment
        self.channels = dict(channels)
        self.sample_period = float(options.get("sample_period", self.DEFAULT_SAMPLE_PERIOD))
        self._far: dict[str, Callable[[Any], None]] = {}
        self._levels: dict[str, Any] = {}
        self._cut: set[str] = set()
        self._failure: str | None = None
        self._read_bindings: list[Binding] = []
        self._receiver: Receiver | None = None
        self._stop = threading.Event()
        self._sampler: threading.Thread | None = None
        self._sampler_failure: str | None = None

    # --- Backend protocol -------------------------------------------------------------------

    def open(self, bindings: Sequence[Binding], receiver: Receiver) -> None:
        self._receiver = receiver
        self._read_bindings = [b for b in bindings if not b.drive]
        self._stop.clear()
        self._sampler_failure = None
        self._sampler = threading.Thread(
            target=self._sample_loop, name=f"benchtare-sim-{self.equipment}", daemon=True
        )
        self._sampler.start()

    def close(self) -> None:
        self._stop.set()
        if self._sampler is not None:
            self._sampler.join(timeout=1.0)
            self._sampler = None

    def identity(self) -> EquipmentIdentity:
        def caps(c: ChannelCaps) -> str:
            return ", ".join(word for word, on in (("drive", c.drive), ("read", c.read)) if on)

        return EquipmentIdentity(
            driver="benchtare sim",
            driver_version=__version__,
            details={
                "sample_period": self.sample_period,
                "channels": {name: caps(c) for name, c in sorted(self.channels.items())},
            },
        )

    def check(self) -> list[str]:
        if self._failure is not None:
            return [self._failure]
        if self._sampler_failure is not None:
            return [self._sampler_failure]
        if self._sampler is None or not self._sampler.is_alive():
            return ["not open"]
        return []

    def write(self, binding: Binding, value: Any) -> None:
        if self._failure is not None:
            raise WriteError(
                f"can't write {binding.key} on {binding.endpoint}: {self._failure}",
                hint=f"equipment '{self.equipment}'",
            )
        assert binding.channel is not None
        far = self._far.get(binding.channel)
        if far is not None and binding.channel not in self._cut:
            far(value)

    # --- The simulation's side of the channels ----------------------------------------------

    def connect_far(self, channel: str, receive: Callable[[Any], None]) -> None:
        """Wire a driven channel to the simulation: ``receive`` gets every value written."""
        self._far[channel] = receive

    def set_level(self, channel: str, value: Any) -> None:
        """Set the level a read channel sees. Ignored while the channel's wire is cut."""
        if channel not in self._cut:
            self._levels[channel] = value

    def cut(self, channel: str) -> None:
        """Simulate a cut wire: nothing passes through the channel any more."""
        self._cut.add(channel)
        self._levels.pop(channel, None)

    def fail(self, reason: str = "simulated equipment failure") -> None:
        """Simulate failed equipment: check() reports it, and nothing is read or written."""
        self._failure = reason
        log.info("%s: simulated failure: %s", self.equipment, reason)

    # --- Sampling ---------------------------------------------------------------------------

    def _sample_loop(self) -> None:
        try:
            while not self._stop.wait(self.sample_period):
                self._sample()
        except Exception as exc:  # logged here, recorded, and check() reports it
            failure = self._sampler_failure = f"stopped sampling: {type(exc).__name__}: {exc}"
            log.exception("%s %s", self.equipment, failure)
            if self._receiver is not None:
                self._receiver.note(
                    RecordEntry(
                        EQUIPMENT_FAILURE,
                        RecordLevel.RESULTS,
                        f"{self.equipment} {failure}",
                        {"equipment": self.equipment, "problem": failure},
                    )
                )

    def _sample(self) -> None:
        receiver = self._receiver
        if receiver is None or self._failure is not None:
            return
        # Sample and deliver under the bench's lock, so a sample can't straddle a write.
        with receiver.lock:
            for binding in self._read_bindings:
                assert binding.channel is not None
                level = self._levels.get(binding.channel, _NO_LEVEL)
                if level is not _NO_LEVEL:
                    receiver.deliver(binding.key, level)


def _valid_name(name: str) -> bool:
    return bool(name) and "." not in name and all(c.isalnum() or c in "_-" for c in name)
