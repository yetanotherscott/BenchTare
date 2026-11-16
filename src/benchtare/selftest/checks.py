"""The self-test: the bench check and the harness check, run before any test.

- **Bench check.** Each equipment item reports itself healthy (``check()``), and so does the
  simulated DUT, if there is one.
- **Harness check.** Each loopback reads back what was driven, first True and then False, so a
  cut wire and a line stuck at either level both fail. Each presence check passes: any frame
  received on the bus, or any value of the signal, within its ``within:`` seconds.

They verify two different things and give two verdicts. A harness check on equipment that
failed the bench check is not checked rather than failed: a broken item says nothing about the
wiring. See "What BenchTare verifies" and "The self-test" in docs/model.md.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from benchtare._yaml import count
from benchtare.backends.base import BusBackend
from benchtare.bench.runtime import Bench
from benchtare.errors import BenchTareError
from benchtare.harness.config import Loopback, Presence

PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"

# The two areas the self-test verifies, and which kinds of check belong to each.
BENCH = "bench"
HARNESS = "harness"
AREAS = (BENCH, HARNESS)
_AREA_OF = {
    "equipment": BENCH,
    "simulation": BENCH,
    "bench": BENCH,
    "loopback": HARNESS,
    "presence": HARNESS,
}

LOOPBACK_VALUES = (True, False)
LOOPBACK_TIMEOUT = 1.0  # seconds for each value to read back


@dataclass(frozen=True)
class CheckResult:
    """One check: what was checked, its outcome, what was seen, and what to check if it failed."""

    kind: str  # "equipment", "simulation", "loopback", "presence" or "bench"
    name: str
    outcome: str
    detail: str
    hint: str | None = None

    @property
    def passed(self) -> bool:
        return self.outcome == PASSED

    def lines(self) -> list[str]:
        lines = [f"{self.outcome.upper():7} {self.name}: {self.detail}"]
        if self.hint and not self.passed:
            lines.append(f"        Check: {self.hint}")
        return lines

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "outcome": self.outcome,
            "detail": self.detail,
            "hint": self.hint,
        }


@dataclass(frozen=True)
class SelfTestResult:
    checks: tuple[CheckResult, ...]
    started: float  # time.time()
    duration: float  # seconds

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)

    @property
    def problems(self) -> list[CheckResult]:
        return [check for check in self.checks if not check.passed]

    def checks_of(self, area: str) -> list[CheckResult]:
        """The checks of one area: ``BENCH`` or ``HARNESS``."""
        return [check for check in self.checks if _AREA_OF[check.kind] == area]

    def outcome_of(self, area: str) -> str:
        """An area's verdict: failed if any of its checks failed, skipped if any couldn't be
        run because the bench check failed, otherwise passed."""
        checks = self.checks_of(area)
        if any(check.outcome == FAILED for check in checks):
            return FAILED
        if any(check.outcome == SKIPPED for check in checks):
            return SKIPPED
        if area == HARNESS and not checks and self.outcome_of(BENCH) == FAILED:
            return SKIPPED  # the bench couldn't even be opened
        return PASSED

    def summary(self, area: str) -> str:
        """'2 equipment items, the simulated DUT' or '1 loopback, 2 presence checks'."""
        kinds = [check.kind for check in self.checks_of(area)]
        if area == BENCH:
            parts = [count(kinds.count("equipment"), "equipment item")]
            if "simulation" in kinds:
                parts.append("the simulated DUT")
        else:
            parts = [
                count(kinds.count("loopback"), "loopback"),
                count(kinds.count("presence"), "presence check"),
            ]
        return ", ".join(parts)

    def failed_areas(self) -> list[str]:
        return [area for area in AREAS if self.outcome_of(area) == FAILED]

    def lines(self) -> list[str]:
        return [line for check in self.checks for line in check.lines()]

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "started": self.started,
            "duration": self.duration,
            "checks": [check.as_dict() for check in self.checks],
        }


def run_selftest(bench: Bench) -> SelfTestResult:
    """Run the bench check, then the harness check, on an open bench."""
    started, clock = time.time(), time.monotonic()
    checks: list[CheckResult] = []
    failed: set[str] = set()  # equipment that failed the bench check

    bench_file = bench.config.path.name
    for item in bench.config.equipment.values():
        problems = bench.backend(item.id).check()
        if problems:
            failed.add(item.id)
            checks.append(
                CheckResult(
                    "equipment",
                    f"equipment {item.id}",
                    FAILED,
                    "; ".join(problems),
                    hint=f"that equipment '{item.id}' ({item.backend}, in bench file "
                    f"{bench_file}) is connected and powered",
                )
            )
        else:
            checks.append(
                CheckResult("equipment", f"equipment {item.id}", PASSED, f"{item.backend}, healthy")
            )

    if bench.simulation is not None:
        name = f"simulated DUT {bench.harness.simulate}"
        problems = bench.simulation.check()
        if problems:
            checks.append(
                CheckResult(
                    "simulation",
                    name,
                    FAILED,
                    "; ".join(problems),
                    hint="this is a fault in the simulation, not in the bench",
                )
            )
        else:
            checks.append(CheckResult("simulation", name, PASSED, "running"))

    for loopback in bench.harness.loopbacks:
        checks.append(_check_loopback(bench, loopback, failed))
    for presence in bench.harness.presence:
        checks.append(_check_presence(bench, presence, failed))

    return SelfTestResult(tuple(checks), started, time.monotonic() - clock)


def could_not_open(error: BenchTareError) -> SelfTestResult:
    """The result when the bench couldn't even be opened."""
    message, _, hint = str(error).partition("\n  Check: ")
    check = CheckResult("bench", "opening the bench", FAILED, message, hint=hint or None)
    return SelfTestResult((check,), time.time(), 0.0)


def _check_loopback(bench: Bench, loopback: Loopback, failed: set[str]) -> CheckResult:
    drive, read = str(loopback.drive), str(loopback.read)
    name = f"loopback {drive} -> {read}"
    if skipped := _skipped(
        "loopback", name, {loopback.drive.equipment, loopback.read.equipment}, failed
    ):
        return skipped
    hint = f"the loopback wire from {drive} to {read} (harness file {bench.harness.path.name})"
    try:
        for value in LOOPBACK_VALUES:
            bench.drive_channel(drive, value)
            if bench.wait_channel(read, value, LOOPBACK_TIMEOUT) is None:
                last = bench.last_received(read)
                seen = f"it last read {last.value!r}" if last else "it read nothing"
                return CheckResult(
                    "loopback",
                    name,
                    FAILED,
                    f"drove {value!r} on {drive}, but {read} didn't read it back within "
                    f"{LOOPBACK_TIMEOUT:g} s; {seen}",
                    hint=hint,
                )
    except BenchTareError as exc:
        return CheckResult("loopback", name, FAILED, str(exc).split("\n")[0], hint=hint)
    shown = " and ".join(repr(value) for value in LOOPBACK_VALUES)
    return CheckResult("loopback", name, PASSED, f"read back {shown}")


def _check_presence(bench: Bench, presence: Presence, failed: set[str]) -> CheckResult:
    harness_file = bench.harness.path.name
    if presence.bus is not None:
        bus = presence.bus
        name = f"DUT traffic on {bus}"
        if skipped := _skipped("presence", name, {bus}, failed):
            return skipped
        backend = bench.backend(bus)
        if isinstance(backend, BusBackend) and backend.wait_for_traffic(presence.within):
            return CheckResult("presence", name, PASSED, "frames received")
        return CheckResult(
            "presence",
            name,
            FAILED,
            f"no frames received on {bus} within {presence.within:g} s",
            hint=f"the DUT is powered, has booted and is connected to {bus}",
        )

    assert presence.signal is not None
    signal = presence.signal
    endpoint = bench.harness.connections[signal]
    name = f"DUT sends {signal}"
    if skipped := _skipped("presence", name, {endpoint.equipment}, failed):
        return skipped
    item = bench.seen(signal, presence.within)
    if item is not None:
        return CheckResult("presence", name, PASSED, f"{signal} = {item.value!r} on {endpoint}")
    return CheckResult(
        "presence",
        name,
        FAILED,
        f"no value of {signal} arrived on {endpoint} within {presence.within:g} s",
        hint=f"the DUT is powered, has booted and is connected at {endpoint}; see harness file "
        f"{harness_file}",
    )


def _skipped(kind: str, name: str, uses: Iterable[str], failed: set[str]) -> CheckResult | None:
    broken = sorted(set(uses) & failed)
    if not broken:
        return None
    return CheckResult(
        kind, name, SKIPPED, f"not checked, because equipment {', '.join(broken)} failed"
    )
