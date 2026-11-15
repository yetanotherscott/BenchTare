"""The simulated reference DUT.

It works only with signal values. The simulation connects it to the bench through the harness,
so the same device runs behind simulated I/O channels or a virtual CAN bus.

Behaviour (all times in seconds, adjustable for tests):

- It boots ``boot_delay`` after start. Until then it ignores its CAN-side inputs and sends
  nothing.
- After boot it reports ``Mode`` every ``heartbeat_period``, starting in ``standby``.
- ``ModeRequest = run`` (or ``standby``) changes ``Mode`` after ``mode_delay``. Other codes
  are ignored.
- Each ``Setpoint`` it receives is answered, ``echo_delay`` later, with ``Measured``: the
  setpoint in ``run``, and 0.0 in ``standby``.
- ``supply_ok`` follows ``supply_enable`` immediately, booted or not.
"""

from __future__ import annotations

import heapq
import itertools
import threading
import time
from collections.abc import Callable
from typing import Any, ClassVar

Publish = Callable[[str, Any], None]


class ReferenceDevice:
    SIGNALS: ClassVar[tuple[str, ...]] = (
        "Setpoint",
        "ModeRequest",
        "Measured",
        "Mode",
        "supply_enable",
        "supply_ok",
    )
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
            self.supply_ok = bool(value)
            self._publish("supply_ok", self.supply_ok)
            return
        with self._wake:
            if not self.booted:
                return
            if name == "Setpoint":
                self._schedule(self.echo_delay, lambda: self._answer(value))
            elif name == "ModeRequest" and value in self.MODES:
                self._schedule(self.mode_delay, lambda: self._set_mode(value))

    # --- behaviour --------------------------------------------------------------------------

    def _boot(self) -> None:
        self.booted = True
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
                action()
