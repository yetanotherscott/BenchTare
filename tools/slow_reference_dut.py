"""Slow the simulated reference DUT down, for the timing check (docs/testing.md).

Changes ``src/benchtare/reference/device.py`` in place: the DUT reports its mode every 0.3 s
instead of every 0.05 s, and answers each setpoint after 0.6 s instead of 0.02 s. CI runs the
whole suite with this change, so a test whose outcome depends on how quickly a value arrives
fails there every time, rather than now and then on a busy machine.

Run it from the repository root, then run ``pytest``. It changes a tracked file, so undo it
afterwards with ``git checkout src/benchtare/reference/device.py``.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

DEVICE = Path("src/benchtare/reference/device.py")
SLOWER = {
    "heartbeat_period: float = 0.05,": "heartbeat_period: float = 0.3,",
    "echo_delay: float = 0.02,": "echo_delay: float = 0.6,",
}
EXPECTED = {"heartbeat_period": 0.3, "echo_delay": 0.6}


def main() -> int:
    text = DEVICE.read_text(encoding="utf-8")
    for old, new in SLOWER.items():
        found = text.count(old)
        if found != 1:
            print(
                f"{DEVICE}: expected '{old}' once, found it {found} times. If the reference DUT's "
                f"defaults changed, change SLOWER in {Path(__file__).as_posix()} to match.",
                file=sys.stderr,
            )
            return 1
        text = text.replace(old, new)
    DEVICE.write_text(text, encoding="utf-8")

    # The change must reach what the tests import: this checkout's source, not another copy.
    from benchtare.reference import device

    imported = Path(device.__file__).resolve()
    if imported != DEVICE.resolve():
        print(f"the tests would import {imported}, not the slowed {DEVICE}", file=sys.stderr)
        return 1
    parameters = inspect.signature(device.ReferenceDevice.__init__).parameters
    actual = {name: parameters[name].default for name in EXPECTED}
    if actual != EXPECTED:
        print(f"the reference DUT's defaults are {actual}, not {EXPECTED}", file=sys.stderr)
        return 1
    shown = ", ".join(f"{name} {value:g} s" for name, value in actual.items())
    print(f"Slowed the reference DUT: {shown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
