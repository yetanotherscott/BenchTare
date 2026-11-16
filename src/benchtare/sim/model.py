"""DUT models: how a DUT reacts to its inputs, so it can be simulated.

A model lets the tests, the self-test, the test check and the hygiene check run without
hardware. The reference DUT is one; a team writes its own by subclassing ``DutModel`` and naming
it in the harness file::

    # models/thermostat.py
    from benchtare import DutModel


    class Thermostat(DutModel):
        signals = ("heater_enable", "temperature")

        def started(self):
            self.temperature = 20.0
            self.every(0.05, self.report)

        def on_input(self, name, value):
            if name == "heater_enable":
                self.heating = bool(value)

        def report(self):
            self.publish("temperature", self.temperature)

    # harness file
    simulate: models/thermostat.py:Thermostat

A model works with signal names and values only: the simulation wires it to the bench through
the harness, so the same model runs behind simulated I/O channels or a virtual CAN bus.

All of a model's code runs on one thread, in order: ``started``, each ``on_input``, and each
action scheduled with ``after`` or ``every``. So a model needs no locks. If any of it raises, the
error is recorded in ``errors`` and the model keeps running; the self-test's bench check then
reports it, so a bug in a model never passes for a DUT that is merely slow.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import threading
import time
from collections.abc import Callable
from typing import Any, ClassVar

Publish = Callable[[str, Any], None]

log = logging.getLogger(__name__)


class DutModel:
    """A model of a DUT. Subclass it, and override ``started`` and ``on_input``.

    ``signals`` lists the signals the model uses; the harness file is checked against the
    interface description for them. A subclass that takes options (from the harness file's
    ``simulate: {model: ..., options: {...}}``) defines its own ``__init__`` with keyword
    arguments, and passes ``publish`` on to this one.
    """

    signals: ClassVar[tuple[str, ...]] = ()

    def __init__(self, publish: Publish, **options: Any) -> None:
        if options:
            raise TypeError(
                f"{type(self).__name__} takes no options, but was given "
                f"{', '.join(sorted(options))}"
            )
        self._publish = publish
        self.errors: list[str] = []
        self._wake = threading.Condition(threading.Lock())
        self._events: list[tuple[float, int, Callable[[], None]]] = []
        self._order = itertools.count()
        self._running = False
        self._thread: threading.Thread | None = None

    # --- what a model writes ------------------------------------------------------------------

    def started(self) -> None:
        """The model has started: the DUT is powered. Set up its state and its cyclic outputs."""

    def on_input(self, name: str, value: Any) -> None:
        """An input arrived: ``name`` is the signal, ``value`` what the bench drove. A model
        should ignore what a real DUT would ignore, rather than raise."""

    # --- what a model calls ---------------------------------------------------------------------

    def publish(self, name: str, value: Any) -> None:
        """Send a value of one of the DUT's outputs."""
        self._publish(name, value)

    def after(self, delay: float, action: Callable[..., Any], *args: Any) -> None:
        """Run ``action(*args)`` on the model's thread, ``delay`` seconds from now."""
        with self._wake:
            due = time.monotonic() + max(0.0, delay)
            heapq.heappush(self._events, (due, next(self._order), lambda: action(*args)))
            self._wake.notify_all()

    def every(self, period: float, action: Callable[..., Any], *args: Any) -> None:
        """Run ``action(*args)`` now, and then every ``period`` seconds."""

        def repeat() -> None:
            try:
                action(*args)
            finally:
                self.after(period, repeat)

        self.after(0.0, repeat)

    # --- what the simulation calls --------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._wake:
            self._running = True
        self.after(0.0, self.started)
        self._thread = threading.Thread(
            target=self._run, name=f"benchtare-model-{type(self).__name__}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        with self._wake:
            self._running = False
            self._wake.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def set_input(self, name: str, value: Any) -> None:
        """An input from the bench: handled by ``on_input`` on the model's thread."""
        self.after(0.0, self.on_input, name, value)

    def _run(self) -> None:
        while True:
            with self._wake:
                while self._running:
                    if not self._events:
                        self._wake.wait()
                        continue
                    due = self._events[0][0]
                    now = time.monotonic()
                    if due <= now:
                        break
                    self._wake.wait(due - now)
                if not self._running:
                    return
                _, _, action = heapq.heappop(self._events)
            try:
                action()
            except Exception as exc:  # keep the model alive, and report it
                self.errors.append(f"{type(exc).__name__}: {exc}")
                log.exception(
                    "DUT model %s: an action failed: %s", type(self).__name__, self.errors[-1]
                )
