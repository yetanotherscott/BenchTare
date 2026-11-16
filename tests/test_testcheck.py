"""The test check's corruption modes, on their own and in the running bench. How the plugin
reruns tests with them is in test_check_tests.py."""

import inspect
from pathlib import Path

import pytest

from benchtare import reference
from benchtare.backends.base import ComparisonFailed
from benchtare.bench.runtime import open_bench
from benchtare.record import RecordLevel
from benchtare.signals.model import Direction, Signal, SignalType
from benchtare.testcheck import (
    ABLE,
    CONTROL,
    FAILED,
    INCONCLUSIVE,
    NOT_APPLIED,
    NOT_CHECKED,
    NOT_REPEATABLE,
    PASSED,
    SHIFT,
    STALE,
    UNABLE,
    UNKNOWN,
    ModeResult,
    by_reading,
    reading_verdict,
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


def control(outcome=PASSED):
    return ModeResult(0, "", "", CONTROL, outcome, "")


def step(reading, mode, outcome):
    return ModeResult(reading, "Measured", f"test_x.py:{10 + reading}", mode, outcome, "")


@pytest.mark.parametrize(
    ("outcomes", "expected"),
    [
        ((FAILED, FAILED), ABLE),
        ((PASSED, FAILED), ABLE),  # one mode is enough for a reading
        ((PASSED, PASSED), UNABLE),
        ((PASSED, NOT_APPLIED), UNABLE),
        ((INCONCLUSIVE, PASSED), UNKNOWN),  # it might have failed
        ((NOT_APPLIED, NOT_APPLIED), NOT_CHECKED),  # nothing could change it
    ],
)
def test_the_verdict_on_one_reading(outcomes, expected):
    assert reading_verdict([step(1, SHIFT, outcomes[0]), step(1, STALE, outcomes[1])]) == expected


def test_a_test_can_fail_only_if_every_reading_can():
    able = [step(1, SHIFT, FAILED), step(1, STALE, FAILED)]
    unable = [step(2, SHIFT, PASSED), step(2, STALE, PASSED)]
    unknown = [step(3, SHIFT, INCONCLUSIVE), step(3, STALE, PASSED)]
    uncorruptible = [step(4, SHIFT, NOT_APPLIED), step(4, STALE, NOT_APPLIED)]
    assert verdict("passed", [control(), *able]) == ABLE
    assert verdict("passed", [control(), *able, *unable]) == UNABLE  # the first step isn't enough
    assert verdict("passed", [control(), *able, *unknown]) == UNKNOWN
    assert verdict("passed", [control(), *unable, *unknown]) == UNABLE
    assert verdict("passed", [control(), *able, *uncorruptible]) == ABLE
    assert verdict("passed", [control(), *uncorruptible]) == NOT_CHECKED


def test_a_test_that_doesnt_repeat_isnt_judged_on_its_logic():
    # Test hygiene, not test logic: and once a test is seen not to repeat, even its failures
    # can't be trusted to be real.
    assert verdict("passed", [control(NOT_REPEATABLE)]) == NOT_CHECKED
    flaky = [control(), step(1, SHIFT, FAILED), step(1, STALE, NOT_REPEATABLE)]
    assert verdict("passed", flaky) == NOT_CHECKED


@pytest.mark.parametrize("normal", ["failed", "skipped", "error"])
def test_a_test_that_didnt_pass_isnt_checked(normal):
    assert verdict(normal, []) == NOT_CHECKED


def test_results_are_grouped_by_reading_in_order_without_the_control():
    results = [step(2, SHIFT, FAILED), control(), step(1, STALE, PASSED), step(1, SHIFT, FAILED)]
    grouped = by_reading(results)
    assert list(grouped) == [1, 2]
    assert [r.mode for r in grouped[1]] == [STALE, SHIFT]


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
        opened.readings = []
        yield opened, entries


def here(offset=0):
    """This file and the line ``offset`` lines below the call."""
    frame = inspect.currentframe().f_back
    return f"{Path(__file__).name}:{frame.f_lineno + offset}"


def test_each_reading_is_logged_as_a_step_with_the_line_that_took_it(bench):
    bench, entries = bench
    line = here(1)
    bench.read(bench.signals.Mode, timeout=1.0)
    bench.wait_until(bench.signals.Mode, "run", timeout=1.0)
    bench.read(bench.signals.Mode, timeout=1.0, check=False)
    taken = [(r.index, r.signal, r.where, r.checked) for r in bench.readings]
    name, number = line.split(":")
    assert taken == [
        (1, "Mode", f"{name}:{number}", True),
        (2, "Mode", f"{name}:{int(number) + 1}", True),
        (None, "Mode", f"{name}:{int(number) + 2}", False),  # not one of the test's checks
    ]
    assert entries[0].data["where"] == line
    assert entries[2].data["checked"] is False


def test_shift_corrupts_what_read_and_wait_until_give_the_test(bench):
    bench, entries = bench
    signals = bench.signals
    bench.write(signals.Setpoint, 42.0)
    bench.wait_until(signals.Measured, pytest.approx(42.0, abs=0.05), timeout=2.0)
    bench.corruption = SHIFT  # every checked reading
    with pytest.raises(ComparisonFailed, match="the last 84.0 degC"):
        bench.wait_until(signals.Measured, pytest.approx(42.0, abs=0.05), timeout=0.3)
    assert bench.wait_until(signals.Measured, 84.0, timeout=1.0) == 84.0
    assert bench.read(signals.Mode, timeout=1.0) == "standby"  # its other state
    assert [r.corrupted for r in bench.readings] == [False, True, True, True]
    reached = entries[-2]
    assert "got 84.0 degC on can0 after" in reached.text
    assert reached.text.endswith("(test check, shift: 42.0 degC arrived)")
    assert reached.data["value"] == 84.0
    assert reached.data["received"] == 42.0
    assert reached.data["corruption"] == SHIFT


def test_a_wait_counts_as_corrupted_if_any_value_it_saw_was():
    # Stale gives Mode 'standby' from before the second write. The DUT goes to run and back to
    # standby, so the last value to arrive equals the stale one, but the run in between was
    # changed. A slow mode change keeps the DUT in standby between the two writes.
    with open_bench(
        reference.path(reference.DUT),
        reference.path(reference.BENCH),
        reference.path(reference.HARNESS_CAN),
        device_options={"mode_delay": 0.3},
    ) as bench:
        bench.wait_until(bench.signals.Mode, "standby", timeout=2.0)
        bench.readings = []
        bench.corruption = STALE
        bench.write(bench.signals.ModeRequest, "run")
        bench.write(bench.signals.ModeRequest, "standby")
        with pytest.raises(ComparisonFailed):
            bench.wait_until(bench.signals.Mode, "run", timeout=1.0)
        (reading,) = bench.readings
        assert reading.corrupted


def test_only_the_targeted_reading_is_corrupted(bench):
    bench, _ = bench
    bench.corruption, bench.corrupt_at = SHIFT, 2
    assert bench.read(bench.signals.Mode, timeout=1.0) == "run"  # reading 1: real
    assert bench.read(bench.signals.Mode, timeout=1.0, check=False) == "run"  # not a step
    assert bench.read(bench.signals.Mode, timeout=1.0) == "standby"  # reading 2: corrupted
    assert bench.read(bench.signals.Mode, timeout=1.0) == "run"  # reading 3: real
    assert [r.corrupted for r in bench.readings] == [False, False, True, False]


def test_a_reading_marked_unchecked_is_never_corrupted(bench):
    bench, _ = bench
    bench.corruption = SHIFT
    assert bench.read(bench.signals.Mode, timeout=1.0, check=False) == "run"
    assert bench.wait_until(bench.signals.Mode, "run", timeout=1.0, check=False) == "run"
    assert not any(r.corrupted for r in bench.readings)


def test_stale_gives_the_test_the_value_from_before_the_write(bench):
    bench, entries = bench
    signals = bench.signals
    bench.wait_until(signals.Measured, 0.0, timeout=2.0)
    bench.write(signals.Setpoint, 42.0)
    bench.readings = []
    bench.corruption = STALE
    # The DUT moves to 42, but the test sees what it reported before the write.
    with pytest.raises(ComparisonFailed, match="the last 0.0 degC"):
        bench.wait_until(signals.Measured, pytest.approx(42.0, abs=0.05), timeout=0.5)
    assert bench.read(signals.Measured, timeout=1.0) == 0.0
    assert [r.corrupted for r in bench.readings] == [True, True]
    assert entries[-1].data["corruption"] == STALE
    assert entries[-1].data["received"] != 0.0


def test_stale_changes_nothing_when_the_value_was_already_there_and_says_why(bench):
    # Mode was already 'run', so a DUT that ignored the request would look the same.
    bench, _ = bench
    bench.write(bench.signals.ModeRequest, "run")
    bench.corruption = STALE
    assert bench.wait_until(bench.signals.Mode, "run", timeout=1.0) == "run"
    (reading,) = bench.readings
    assert not reading.corrupted
    assert reading.note == "it was already 'run' before the write, so stale can't change this step"


def test_nothing_is_corrupted_when_the_mode_is_off_or_cant_apply():
    with open_bench(
        reference.path(reference.DUT),
        reference.path(reference.BENCH),
        reference.path(reference.HARNESS_CAN),
    ) as bench:
        bench.corruption = STALE  # no write yet, so there is no value from before one
        assert bench.wait_until(bench.signals.Mode, "standby", timeout=2.0) == "standby"
        assert bench.readings[0].note == "no value of it arrived before a write"
        bench.corruption = None
        assert bench.read(bench.signals.Mode, timeout=1.0) == "standby"
        assert not any(r.corrupted for r in bench.readings)
