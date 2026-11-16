"""The simulated reference DUT: a ``DutModel``, like any team's own.

It works only with signal values. The simulation connects it to the bench through the harness,
so the same device runs behind simulated I/O channels or a virtual CAN bus.

Behaviour (all times in seconds, adjustable for tests):

- It boots ``boot_delay`` after start. Until then it ignores its inputs, except
  ``supply_enable``, and sends nothing but ``supply_ok``.
- After boot it reports ``Mode`` every ``heartbeat_period``, starting in ``standby``, and its
  software version, ``SwVersion`` (102, meaning 1.0.2), alongside it.
- ``ModeRequest = run`` (or ``standby``) changes ``Mode`` after ``mode_delay``. Other codes
  are ignored.
- Each ``Setpoint`` it receives is answered, ``echo_delay`` later, with ``Measured``: the
  setpoint in ``run``, and 0.0 in ``standby``.
- ``supply_ok`` follows ``supply_enable`` at once, booted or not.

Like a well-behaved real device, it ignores inputs it can't use: a Setpoint that isn't a
number, a ModeRequest that isn't a known mode, a supply_enable that isn't true, false, 0 or 1.
If one of its own actions fails anyway, ``DutModel`` records the error in ``errors`` and keeps
it running, so a simulation bug is reported (see ``Simulation.check``) rather than looking like
a dead DUT.
"""

from __future__ import annotations

import math
import numbers
from typing import Any, ClassVar

from benchtare.sim.model import DutModel, Publish


class ReferenceDevice(DutModel):
    signals: ClassVar[tuple[str, ...]] = (
        "Setpoint",
        "ModeRequest",
        "Measured",
        "Mode",
        "SwVersion",
        "supply_enable",
        "supply_ok",
    )
    SOFTWARE_VERSION: ClassVar[int] = 102  # 1.0.2
    MODES: ClassVar[tuple[str, ...]] = ("standby", "run")

    def __init__(
        self,
        publish: Publish,
        *,
        boot_delay: float = 0.1,
        heartbeat_period: float = 0.05,
        echo_delay: float = 0.02,
        mode_delay: float = 0.01,
    ) -> None:
        super().__init__(publish)
        self.boot_delay = boot_delay
        self.heartbeat_period = heartbeat_period
        self.echo_delay = echo_delay
        self.mode_delay = mode_delay
        self.booted = False
        self.mode = "standby"
        self.supply_ok = False

    def started(self) -> None:
        self.publish("supply_ok", self.supply_ok)
        self.after(self.boot_delay, self._boot)

    def on_input(self, name: str, value: Any) -> None:
        if name == "supply_enable":
            if value in (True, False, 0, 1) and not isinstance(value, float):
                self.supply_ok = bool(value)
                self.publish("supply_ok", self.supply_ok)
            return
        if not self.booted:
            return
        if name == "Setpoint" and _is_number(value):
            self.after(self.echo_delay, self._answer, value)
        elif name == "ModeRequest" and isinstance(value, str) and value in self.MODES:
            self.after(self.mode_delay, self._set_mode, value)

    def _boot(self) -> None:
        self.booted = True
        # Before the first heartbeat, so every Heartbeat frame carries it.
        self.publish("SwVersion", self.SOFTWARE_VERSION)
        self.publish("Measured", 0.0)
        self.every(self.heartbeat_period, self._heartbeat)

    def _heartbeat(self) -> None:
        self.publish("Mode", self.mode)

    def _answer(self, setpoint: Any) -> None:
        self.publish("Measured", float(setpoint) if self.mode == "run" else 0.0)

    def _set_mode(self, mode: str) -> None:
        if mode != self.mode:
            self.mode = mode
            self.publish("Mode", mode)


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, numbers.Real)
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )
