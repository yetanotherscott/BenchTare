"""The test check's corruption modes, on their own and in the running bench. How the plugin
reruns tests with them is in test_check_tests.py."""

import pytest

from benchtare import reference
from benchtare.backends.base import ComparisonFailed
from benchtare.bench.runtime import open_bench
from benchtare.record import RecordLevel
from benchtare.signals.model import Direction, Signal, SignalType
from benchtare.testcheck import (
    ABLE,
    FAILED,
    INCONCLUSIVE,
    NOT_APPLIED,
    NOT_CHECKED,
    PASSED,
    SHIFT,
    STALE,
    UNABLE,
    UNKNOWN,
    ModeResult,
    shifted,
    verdict,
)

MODE = Signal("Mode", SignalType.ENUM, Direction.OUTPUT, values=("standby", "run", "fault"))
ALONE = Signal("Alone", SignalType.ENUM, Direction.OUTPUT, values=("only",))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (42.0, 84.0),
        (0.25, 1.25),  # at least 1, so a small reading moves clearly
        (0, 1),
        (-5, 0),
        (7, 14),
        (True, False),
        (False, True),
    ],
)
def test_shift_gives_a_clearly_different_value_of_the_same_kind(value, expected):
    applied, moved = shifted(None, value)
    assert applied
    assert moved == expected
    assert type(moved) is type(expected)


def test_shift_moves_an_enum_to_its_next_state():
    assert shifted(MODE, "standby") == (True, "run")
    assert shifted(MODE, "fault") == (True, "standby")  # round to the first


@pytest.mark.parametrize(
    ("signal", "value"),
    [(ALONE, "only"), (MODE, 3), (None, float("nan")), (None, float("inf")), (None, "text")],
    ids=["one-state-enum", "undefined-enum-code", "nan", "inf", "text"],
)
def test_shift_leaves_alone_what_it_cant_move(signal, value):
    applied, same = shifted(signal, value)
    assert not applied
    assert same is value


def results(*outcomes):
    return [ModeResult(mode, outcome, "") for mode, outcome in zip((SHIFT, STALE), outcomes)]


@pytest.mark.parametrize(
    ("normal", "outcomes", "expected"),
    [
        ("passed", (FAILED, FAILED), ABLE),
        ("passed", (PASSED, FAILED), ABLE),  # one mode is enough
        ("passed", (PASSED, PASSED), UNABLE),
        ("passed", (PASSED, NOT_APPLIED), UNABLE),
        ("passed", (INCONCLUSIVE, PASSED), UNKNOWN),  # it might have failed
        ("passed", (NOT_APPLIED, NOT_APPLIED), NOT_CHECKED),  # it read nothing
        ("failed", (), NOT_CHECKED),
        ("skipped", (), NOT_CHECKED),
    ],
)
def test_the_verdict(normal, outcomes, expected):
    assert verdict(normal, results(*outcomes)) == expected


# --- in the running bench ------------------------------------------------------------------------


@pytest.fixture
def bench():
    with open_bench(
        reference.path(reference.DUT),
        reference.path(reference.BENCH),
        reference.path(reference.HARNESS_CAN),
    ) as opened:
        entries = []
        opened.record_level = RecordLevel.RESULTS
        opened.recorder = entries.append
        opened.wait_until(opened.signals.Mode, "standby", timeout=2.0)
        opened.write(opened.signals.ModeRequest, "run")
        opened.wait_until(opened.signals.Mode, "run", timeout=2.0)
        entries.clear()
        yield opened, entries


def test_shift_corrupts_what_read_and_wait_until_give_the_test(bench):
    bench, entries = bench
    signals = bench.signals
    bench.write(signals.Setpoint, 42.0)
    bench.wait_until(signals.Measured, pytest.approx(42.0, abs=0.05), timeout=2.0)
    bench.corruption = SHIFT
    with pytest.raises(ComparisonFailed, match="the last 84.0 degC"):
        bench.wait_until(signals.Measured, pytest.approx(42.0, abs=0.05), timeout=0.3)
    assert bench.wait_until(signals.Measured, 84.0, timeout=1.0) == 84.0
    assert bench.read(signals.Mode, timeout=1.0) == "standby"  # its other state
    assert bench.corrupted == 3
    reached = entries[-2]
    assert "got 84.0 degC on can0 after" in reached.text
    assert reached.text.endswith("(test check, shift: 42.0 degC arrived)")
    assert reached.data["value"] == 84.0
    assert reached.data["received"] == 42.0
    assert reached.data["check"] == SHIFT


def test_stale_gives_the_test_the_value_from_before_the_write(bench):
    bench, entries = bench
    signals = bench.signals
    bench.wait_until(signals.Measured, 0.0, timeout=2.0)
    bench.write(signals.Setpoint, 42.0)
    bench.corruption = STALE
    # The DUT moves to 42, but the test sees what it reported before the write.
    with pytest.raises(ComparisonFailed, match="the last 0.0 degC"):
        bench.wait_until(signals.Measured, pytest.approx(42.0, abs=0.05), timeout=0.5)
    assert bench.read(signals.Measured, timeout=1.0) == 0.0
    assert bench.corrupted == 2
    assert entries[-1].data["check"] == STALE
    assert entries[-1].data["received"] != 0.0


def test_stale_passes_a_test_whose_dut_was_already_in_the_state_it_expects(bench):
    # The classic false pass: Mode was already 'run', so a DUT that ignored the request
    # would still satisfy the test.
    bench, _ = bench
    bench.write(bench.signals.ModeRequest, "run")
    bench.corruption = STALE
    assert bench.wait_until(bench.signals.Mode, "run", timeout=1.0) == "run"
    assert bench.corrupted == 1


def test_nothing_is_corrupted_when_the_mode_is_off_or_cant_apply():
    with open_bench(
        reference.path(reference.DUT),
        reference.path(reference.BENCH),
        reference.path(reference.HARNESS_CAN),
    ) as bench:
        bench.corruption = STALE  # no write yet, so there is no value from before one
        assert bench.wait_until(bench.signals.Mode, "standby", timeout=2.0) == "standby"
        bench.corruption = None
        assert bench.read(bench.signals.Mode, timeout=1.0) == "standby"
        assert bench.corrupted == 0
