"""A test of the reference DUT: in run mode, it reports the setpoint it is given as Measured."""

import pytest


def test_measured_follows_the_setpoint(running, bench, signals):
    bench.write(signals.Setpoint, 42.0)
    bench.wait_until(signals.Measured, pytest.approx(42.0, abs=0.5), timeout=1.0)
