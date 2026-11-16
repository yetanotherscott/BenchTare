"""Staging: fixtures that put the DUT into the state a test needs.

A fixture that fails is reported as an error, not a test failure: a DUT that never got ready
isn't counted against the test.
"""

import pytest


@pytest.fixture
def standby(bench, signals):
    """Every test starts here: booted, in standby, with the setpoint at 0."""
    bench.read(signals.Mode, timeout=2.0)  # it has booted once it reports a mode
    bench.write(signals.Setpoint, 0.0)
    bench.write(signals.ModeRequest, "standby")
    bench.wait_until(signals.Mode, "standby", timeout=1.0)


@pytest.fixture
def running(standby, bench, signals):
    """In run mode for the test, and back to clean afterwards."""
    bench.write(signals.ModeRequest, "run")
    bench.wait_until(signals.Mode, "run", timeout=1.0)
    yield
    bench.write(signals.Setpoint, 0.0)
    bench.write(signals.ModeRequest, "standby")
    bench.wait_until(signals.Mode, "standby", timeout=1.0)
