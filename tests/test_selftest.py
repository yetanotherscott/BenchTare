"""The self-test's checks, run on the reference bench. Each kind of fault it exists to catch is
broken on purpose here, to prove it is caught: failed equipment, a cut or stuck loopback wire,
a missing DUT, and a simulation that stopped."""

import pytest

from benchtare import reference
from benchtare.backends.base import BenchError
from benchtare.bench.runtime import open_bench
from benchtare.selftest import could_not_open, run_selftest
from benchtare.selftest.checks import BENCH, FAILED, HARNESS, PASSED, SKIPPED

HEALTHY_CAN = [
    ("equipment can0", "passed", "sim_can, healthy"),
    ("equipment io", "passed", "sim, healthy"),
    ("simulated DUT reference", "passed", "running"),
    ("loopback io.out4 -> io.in4", "passed", "read back True and False"),
    ("DUT traffic on can0", "passed", "frames received"),
    ("DUT sends Mode", "passed", "Mode = 'standby' on can0"),
]


def open_reference(harness=reference.HARNESS_CAN):
    return open_bench(
        reference.path(reference.DUT), reference.path(reference.BENCH), reference.path(harness)
    )


def open_quick(tmp_path, harness=reference.HARNESS_CAN, **device_options):
    """The reference, with presence checks shortened to 0.2 s so failures are quick to see."""
    text = reference.path(harness).read_text()
    text = text.replace("within: 1.0", "within: 0.2").replace("within: 2.0", "within: 0.2")
    quick = tmp_path / harness
    quick.write_text(text)
    return open_bench(
        reference.path(reference.DUT),
        reference.path(reference.BENCH),
        quick,
        device_options=device_options or None,
    )


def outcomes(result):
    return [(check.name, check.outcome, check.detail) for check in result.checks]


# --- a healthy setup passes ------------------------------------------------------------------------


def test_the_healthy_reference_passes_on_the_can_harness():
    with open_reference(reference.HARNESS_CAN) as bench:
        result = run_selftest(bench)
    assert result.passed
    assert outcomes(result) == HEALTHY_CAN
    assert result.summary(BENCH) == "2 equipment items, the simulated DUT"
    assert result.summary(HARNESS) == "1 loopback, 2 presence checks"
    assert (result.outcome_of(BENCH), result.outcome_of(HARNESS)) == (PASSED, PASSED)


def test_the_healthy_reference_passes_on_the_io_harness():
    with open_reference(reference.HARNESS_IO) as bench:
        result = run_selftest(bench)
    assert result.passed
    assert outcomes(result)[-1] == ("DUT sends Mode", "passed", "Mode = 'standby' on io.in2")
    assert result.summary(BENCH) == "2 equipment items, the simulated DUT"
    assert result.summary(HARNESS) == "1 loopback, 1 presence check"


def test_the_self_test_leaves_tests_a_clean_start():
    # It writes to the loopback; a test's first read is still the DUT's next value.
    with open_reference(reference.HARNESS_IO) as bench:
        assert run_selftest(bench).passed
        assert bench.read(bench.signals.Mode, timeout=1.0) == "standby"


# --- each fault is caught --------------------------------------------------------------------------


def test_failed_equipment_is_caught_and_its_checks_are_skipped():
    with open_reference(reference.HARNESS_IO) as bench:
        bench.simulation.fail("io", "DAQ unplugged")
        result = run_selftest(bench)
    assert not result.passed
    assert [(c.name, c.outcome) for c in result.problems] == [
        ("equipment io", "failed"),
        ("loopback io.out4 -> io.in4", "skipped"),
        ("DUT sends Mode", "skipped"),
    ]
    (failed, *_) = result.problems
    assert failed.detail == "DAQ unplugged"
    assert failed.hint == (
        "that equipment 'io' (sim, in bench file bench-sim.yaml) is connected and powered"
    )
    assert result.problems[1].detail == "not checked, because equipment io failed"


def test_a_failed_bus_is_caught():
    with open_reference(reference.HARNESS_CAN) as bench:
        bench.simulation.fail("can0", "CAN interface not responding")
        result = run_selftest(bench)
    assert [(c.name, c.outcome) for c in result.problems] == [
        ("equipment can0", "failed"),
        ("DUT traffic on can0", "skipped"),
        ("DUT sends Mode", "skipped"),
    ]


def test_a_cut_loopback_wire_is_caught():
    with open_reference() as bench:
        bench.simulation.cut("io.out4")
        result = run_selftest(bench)
    (problem,) = result.problems
    assert problem.name == "loopback io.out4 -> io.in4"
    assert problem.detail == (
        "drove True on io.out4, but io.in4 didn't read it back within 1 s; it read nothing"
    )
    assert (
        problem.hint == "the loopback wire from io.out4 to io.in4 (harness file harness-can.yaml)"
    )


def test_a_cut_wire_fails_the_harness_check_not_the_bench_check():
    with open_reference() as bench:
        bench.simulation.cut("io.out4")
        result = run_selftest(bench)
    assert (result.outcome_of(BENCH), result.outcome_of(HARNESS)) == (PASSED, FAILED)
    assert result.failed_areas() == [HARNESS]


def test_failed_equipment_fails_the_bench_check_and_leaves_its_harness_checks_not_checked():
    with open_reference(reference.HARNESS_CAN) as bench:
        bench.simulation.fail("can0", "CAN interface not responding")
        result = run_selftest(bench)
    # The loopback on io still ran and passed, but the presence checks on can0 couldn't run:
    # the harness isn't failed for a broken bench, nor passed.
    assert (result.outcome_of(BENCH), result.outcome_of(HARNESS)) == (FAILED, SKIPPED)
    assert result.failed_areas() == [BENCH]


def test_a_bench_that_could_not_open_leaves_the_harness_check_not_run():
    result = could_not_open(BenchError("no DAQ", hint="the USB cable"))
    assert (result.outcome_of(BENCH), result.outcome_of(HARNESS)) == (FAILED, SKIPPED)
    assert result.checks_of(HARNESS) == []


def test_a_loopback_stuck_at_one_level_is_caught():
    with open_reference() as bench:
        io = bench.backend("io")
        io._far["out4"] = lambda value: io.set_level("in4", True)  # in4 is stuck high
        result = run_selftest(bench)
    (problem,) = result.problems
    assert problem.detail == (
        "drove False on io.out4, but io.in4 didn't read it back within 1 s; it last read True"
    )


def test_a_dut_unplugged_from_the_bus_is_caught(tmp_path):
    with open_quick(tmp_path) as bench:
        bench.simulation.cut("can0")
        result = run_selftest(bench)
    assert [(c.name, c.outcome, c.detail) for c in result.problems] == [
        ("DUT traffic on can0", "failed", "no frames received on can0 within 0.2 s"),
        ("DUT sends Mode", "failed", "no value of Mode arrived on can0 within 0.2 s"),
    ]
    assert result.problems[0].hint == "the DUT is powered, has booted and is connected to can0"


def test_a_dut_that_never_boots_is_caught(tmp_path):
    with open_quick(tmp_path, reference.HARNESS_IO, boot_delay=30) as bench:
        result = run_selftest(bench)
    (problem,) = result.problems
    assert problem.name == "DUT sends Mode"
    assert problem.hint == (
        "the DUT is powered, has booted and is connected at io.in2; see harness file "
        "harness-io.yaml"
    )


def test_a_simulation_that_stopped_is_caught(tmp_path):
    with open_quick(tmp_path) as bench:
        bench.simulation.device.stop()
        result = run_selftest(bench)
    problem = result.problems[0]
    assert (problem.name, problem.outcome) == ("simulated DUT reference", "failed")
    assert problem.detail == "the simulated DUT reference has stopped running"


def test_a_bench_that_could_not_open_is_a_failed_check():
    result = could_not_open(BenchError("DAQ not found on USB", hint="the USB cable"))
    assert not result.passed
    assert result.lines() == [
        "FAILED  opening the bench: DAQ not found on USB",
        "        Check: the USB cable",
    ]


# --- results ---------------------------------------------------------------------------------------


def test_results_list_each_check_and_become_plain_dicts():
    with open_reference() as bench:
        bench.simulation.cut("io.out4")
        result = run_selftest(bench)
    lines = result.lines()
    assert lines[0] == "PASSED  equipment can0: sim_can, healthy"
    assert "        Check: the loopback wire from io.out4 to io.in4 (harness file " in "".join(
        lines
    )
    data = result.as_dict()
    assert data["passed"] is False
    assert data["checks"][3] == {
        "kind": "loopback",
        "name": "loopback io.out4 -> io.in4",
        "outcome": "failed",
        "detail": "drove True on io.out4, but io.in4 didn't read it back within 1 s; it read "
        "nothing",
        "hint": "the loopback wire from io.out4 to io.in4 (harness file harness-can.yaml)",
    }
    assert data["duration"] > 0


def test_an_empty_self_test_does_not_pass():
    from benchtare.selftest import SelfTestResult

    assert not SelfTestResult((), 0.0, 0.0).passed


@pytest.mark.parametrize("endpoint", ["io.in4", "io.out1", "nope"])
def test_only_loopback_ends_can_be_driven_by_channel(endpoint):
    with open_reference() as bench, pytest.raises(BenchError, match="isn't a driven loopback end"):
        bench.drive_channel(endpoint, True)
