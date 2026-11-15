"""The simulated reference DUT.

It works only with signal values. The simulation connects it to the bench through the harness,
so the same device runs behind simulated I/O channels or a virtual CAN bus.

Behaviour (all times in seconds, adjustable for tests):

- It boots ``boot_delay`` after start. Until then it ignores its CAN-side inputs and sends
  nothing.
- After boot it reports ``Mode`` every ``heartbeat_period``, starting in ``standby``, and its
  software version, ``SwVersion`` (102, meaning 1.0.2), alongside it.
- ``ModeRequest = run`` (or ``standby``) changes ``Mode`` after ``mode_delay``. Other codes
  are ignored.
- Each ``Setpoint`` it receives is answered, ``echo_delay`` later, with ``Measured``: the
  setpoint in ``run``, and 0.0 in ``standby``.
- ``supply_ok`` follows ``supply_enable`` immediately, booted or not.

Like a well-behaved real device, it ignores inputs it can't use: a Setpoint that isn't a
number, a ModeRequest that isn't a known mode, a supply_enable that isn't true, false, 0 or 1.
If one of its own actions fails anyway, it records the error in ``errors`` and keeps running,
so a simulation bug is reported (see ``Simulation.check``) rather than looking like a dead DUT.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import math
import numbers
import threading
import time
from collections.abc import Callable
from typing import Any, ClassVar

Publish = Callable[[str, Any], None]

log = logging.getLogger(__name__)


class ReferenceDevice:
    SIGNALS: ClassVar[tuple[str, ...]] = (
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
        self._publish = publish
        self.boot_delay = boot_delay
        self.heartbeat_period = heartbeat_period
        self.echo_delay = echo_delay
        self.mode_delay = mode_delay

        self.booted = False
        self.mode = "standby"
        self.supply_ok = False

        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._events: list[tuple[float, int, Callable[[], None]]] = []
        self._order = itertools.count()
        self._running = False
        self._thread: threading.Thread | None = None
        self.errors: list[str] = []

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # --- life cycle -------------------------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            self._running = True
            self._schedule(self.boot_delay, self._boot)
        self._publish("supply_ok", self.supply_ok)
        self._thread = threading.Thread(
            target=self._run, name="benchtare-reference-dut", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        with self._wake:
            self._running = False
            self._wake.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    # --- inputs -----------------------------------------------------------------------------

    def set_input(self, name: str, value: Any) -> None:
        if name == "supply_enable":
            if value in (True, False, 0, 1) and not isinstance(value, float):
                self.supply_ok = bool(value)
                self._publish("supply_ok", self.supply_ok)
            return
        with self._wake:
            if not self.booted:
                return
            if name == "Setpoint" and _is_number(value):
                self._schedule(self.echo_delay, lambda: self._answer(value))
            elif name == "ModeRequest" and isinstance(value, str) and value in self.MODES:
                self._schedule(self.mode_delay, lambda: self._set_mode(value))

    # --- behaviour --------------------------------------------------------------------------

    def _boot(self) -> None:
        self.booted = True
        # Before the first heartbeat, so every Heartbeat frame carries it.
        self._publish("SwVersion", self.SOFTWARE_VERSION)
        self._publish("Measured", 0.0)
        self._heartbeat()

    def _heartbeat(self) -> None:
        self._publish("Mode", self.mode)
        self._schedule(self.heartbeat_period, self._heartbeat)

    def _answer(self, setpoint: Any) -> None:
        self._publish("Measured", float(setpoint) if self.mode == "run" else 0.0)

    def _set_mode(self, mode: str) -> None:
        if mode != self.mode:
            self.mode = mode
            self._publish("Mode", mode)

    # --- scheduling -------------------------------------------------------------------------

    def _schedule(self, delay: float, action: Callable[[], None]) -> None:
        """Run ``action`` on the device thread after ``delay``. Call with the lock held."""
        heapq.heappush(self._events, (time.monotonic() + delay, next(self._order), action))
        self._wake.notify_all()

    def _run(self) -> None:
        with self._wake:
            while self._running:
                if not self._events:
                    self._wake.wait()
                    continue
                due, _, action = self._events[0]
                now = time.monotonic()
                if due > now:
                    self._wake.wait(due - now)
                    continue
                heapq.heappop(self._events)
                try:
                    action()
                except Exception as exc:  # keep the device alive, and report it
                    self.errors.append(f"{type(exc).__name__}: {exc}")
                    log.exception("reference DUT: an action failed: %s", self.errors[-1])


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, numbers.Real)
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )
