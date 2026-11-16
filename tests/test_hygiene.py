"""The hygiene check's logic, with synthetic values: comparing, settling, judging each
clean_state: item, and the verdict. The reference DUT is exact and immediate, so tolerance and
settle are proven here. How the plugin runs it is in test_check_hygiene.py."""

import pytest

from benchtare import reference
from benchtare.bench.runtime import open_bench
from benchtare.hygiene import (
    CHANGED,
    CLEAN,
    FAILED,
    INHERITED,
    INPUT,
    IS_CLEAN,
    MEANINGFUL,
    MISSING,
    NOT_CHECKED,
    NOT_CLEAN,
    NOT_COMPARED,
    OUTPUT,
    PASSED,
    REPEATS,
    SAME,
    CleanStateItem,
    RuleResult,
    come_back,
    judge,
    same,
    verdict,
)


@pytest.mark.parametrize(
    ("before", "after", "tolerance", "expected"),
    [
        (42.0, 42.0, 0.0, True),
        (42.0, 42.05, 0.0, False),  # exact by default
        (42.0, 42.05, 0.1, True),
        (42.0, 42.2, 0.1, False),
        (1, 1.0, 0.0, True),
        ("run", "run", 0.0, True),
        ("run", "standby", 5.0, False),  # a tolerance never stretches text
        (True, False, 1.0, False),  # nor bools, though Python counts them as numbers
        (float("nan"), float("nan"), 0.0, True),
        (MISSING, MISSING, 0.0, True),
        (MISSING, 0.0, 0.0, False),
    ],
)
def test_same(before, after, tolerance, expected):
    assert same(before, after, tolerance) is expected


class Clock:
    """A clock that only moves when the code under test sleeps."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def late(values_by_time):
    """A reading that changes over time: ``[(from_time, value), ...]``."""

    def read(clock):
        value = MISSING
        for at, v in values_by_time:
            if clock.now >= at:
                value = v
        return value

    return read


def item(settle=1.0, tolerance=0.0, clean=MISSING, kind=OUTPUT):
    return CleanStateItem("Measured", kind, tolerance, settle, clean)


def test_a_value_that_comes_back_within_its_settle_time_is_the_same():
    clock = Clock()
    read = late([(0.0, 40.0), (0.6, 42.0)])
    back, value = come_back(
        lambda: read(clock), 42.0, item(settle=1.0), clock=clock, sleep=clock.sleep
    )
    assert (back, value) == (True, 42.0)
    assert clock.now == pytest.approx(0.6, abs=0.05)


def test_a_value_that_comes_back_too_late_is_not():
    clock = Clock()
    read = late([(0.0, 40.0), (1.5, 42.0)])
    back, value = come_back(
        lambda: read(clock), 42.0, item(settle=1.0), clock=clock, sleep=clock.sleep
    )
    assert (back, value) == (False, 40.0)
    assert clock.now == pytest.approx(1.0)


def test_no_settle_time_reads_once():
    clock = Clock()
    back, _ = come_back(lambda: 40.0, 42.0, item(settle=0.0), clock=clock, sleep=clock.sleep)
    assert not back
    assert clock.now == 0.0


def judged(it, before, after, **kwargs):
    clock = Clock()
    read = after if callable(after) else (lambda: after)
    return judge(it, before, read, clock=clock, sleep=clock.sleep, **kwargs)


# --- an item with a clean value --------------------------------------------------------------------


def test_with_a_clean_value_clean_after_teardown_is_clean():
    outcome, after, detail = judged(item(clean=0.0), 0.0, 0.0)
    assert (outcome, after) == (SAME, 0.0)
    assert detail == "Measured was 0.0 after teardown, its clean value"


def test_with_a_clean_value_a_test_that_leaves_it_dirty_is_blamed():
    outcome, _, detail = judged(item(clean=0.0), 0.0, 42.0)
    assert outcome == CHANGED
    assert detail == (
        "Measured was still 42.0 1 s after teardown, not its clean value 0.0 (it was 0.0 before "
        "setup)"
    )


def test_with_a_clean_value_a_test_that_finds_it_dirty_and_leaves_it_isnt_blamed():
    outcome, _, detail = judged(item(clean=0.0), 42.0, 42.0)
    assert outcome == INHERITED
    assert detail == (
        "Measured was already 42.0 before setup, not its clean value 0.0: an earlier test left "
        "it so, and this test didn't change it"
    )


def test_with_a_clean_value_a_test_that_cleans_up_is_clean_and_says_so():
    outcome, _, detail = judged(item(clean=0.0), 42.0, 0.0)
    assert outcome == SAME
    assert detail.endswith("its clean value, though an earlier test had left it at 42.0")


def test_with_a_clean_value_a_test_that_finds_it_dirty_and_makes_it_worse_is_blamed():
    outcome, _, _ = judged(item(clean=0.0), 42.0, 99.0)
    assert outcome == CHANGED


def test_with_a_clean_value_nothing_known_before_setup_still_judges_what_it_left():
    assert judged(item(clean=0.0), MISSING, 42.0)[0] == CHANGED
    assert judged(item(clean=0.0), MISSING, 0.0)[0] == SAME


def test_with_a_clean_value_a_tolerance_and_a_settle_time_apply():
    assert judged(item(clean=0.0, tolerance=0.1), 0.0, 0.05)[0] == SAME
    outcome, _, detail = judged(item(clean=0.0, tolerance=0.1), 0.0, 0.5)
    assert outcome == CHANGED
    assert ", more than 0.1 from it" in detail
    clock = Clock()
    cooling = late([(0.0, 42.0), (0.5, 0.0)])
    assert (
        judge(
            item(clean=0.0, settle=1.0),
            0.0,
            lambda: cooling(clock),
            clock=clock,
            sleep=clock.sleep,
        )[0]
        == SAME
    )


def test_with_a_clean_value_nothing_known_after_teardown_isnt_compared():
    outcome, _, detail = judged(item(clean=0.0), 0.0, MISSING, unknown="no value of it arrived")
    assert (outcome, detail) == (NOT_COMPARED, "Measured: no value of it arrived")


# --- an item without a clean value -----------------------------------------------------------------


def test_without_a_clean_value_back_where_it_was_is_clean():
    outcome, _, detail = judged(item(), 42.0, 42.0)
    assert outcome == SAME
    assert detail == "Measured was 42.0 before setup, and 42.0 after teardown"


def test_without_a_clean_value_a_change_is_blamed():
    outcome, _, detail = judged(item(), 0.0, 42.0)
    assert outcome == CHANGED
    assert detail == "Measured was 0.0 before setup, and still 42.0 1 s after teardown"


def test_without_a_clean_value_putting_back_how_the_session_found_it_is_clean():
    outcome, _, detail = judged(item(), 42.0, 0.0, reference=0.0)
    assert outcome == SAME
    assert detail == (
        "Measured was 0.0 after teardown, as the session first found it; an earlier test had "
        "left it at 42.0"
    )


def test_without_a_clean_value_nothing_known_before_setup_isnt_compared():
    outcome, after, detail = judged(item(kind=INPUT), MISSING, True, unknown="it wasn't written")
    assert (outcome, after, detail) == (NOT_COMPARED, MISSING, "Measured: it wasn't written")


def test_values_are_shown_the_way_the_caller_says():
    _, _, detail = judged(item(clean=0.0), 0.0, 42.0, shown=lambda v: f"{v!r} degC")
    assert detail.startswith("Measured was still 42.0 degC 1 s after teardown")


# --- the verdict ----------------------------------------------------------------------------------


def rules(*outcomes):
    return [
        RuleResult(rule, outcome, "")
        for rule, outcome in zip((REPEATS, CLEAN, MEANINGFUL), outcomes)
    ]


@pytest.mark.parametrize(
    ("normal", "outcomes", "expected"),
    [
        ("passed", (PASSED, PASSED, PASSED), IS_CLEAN),
        ("passed", (PASSED, NOT_CHECKED, PASSED), IS_CLEAN),
        ("passed", (PASSED, FAILED, PASSED), NOT_CLEAN),
        ("passed", (FAILED, PASSED, PASSED), NOT_CLEAN),
        ("passed", (PASSED, PASSED, FAILED), NOT_CLEAN),
        ("passed", (NOT_CHECKED, NOT_CHECKED, NOT_CHECKED), NOT_CHECKED),
        ("failed", (), NOT_CHECKED),
    ],
)
def test_the_verdict(normal, outcomes, expected):
    assert verdict(normal, rules(*outcomes)) == expected


# --- what the bench notes for it ------------------------------------------------------------------


@pytest.fixture
def bench():
    with open_bench(
        reference.path(reference.DUT),
        reference.path(reference.BENCH),
        reference.path(reference.HARNESS_CAN),
    ) as opened:
        opened.wait_until(opened.signals.Mode, "standby", timeout=2.0)
        opened.readings = []
        yield opened


def test_the_bench_remembers_what_it_last_wrote_to_each_input_and_where(bench):
    assert bench.written == {} and bench.last_write is None
    bench.write(bench.signals.supply_enable, True)
    bench.write(bench.signals.ModeRequest, "run")
    assert bench.written == {"supply_enable": True, "ModeRequest": "run"}
    signal, where = bench.last_write
    assert signal == "ModeRequest"
    assert where.startswith("test_hygiene.py:")


def test_a_wait_already_satisfied_before_the_write_is_noted(bench):
    bench.write(bench.signals.ModeRequest, "standby")  # Mode is already standby
    bench.wait_until(bench.signals.Mode, "standby", timeout=1.0)
    bench.write(bench.signals.ModeRequest, "run")
    bench.wait_until(bench.signals.Mode, "run", timeout=1.0)
    bench.write(bench.signals.ModeRequest, "run")  # already run: this wait is satisfied too...
    bench.wait_until(bench.signals.Mode, "run", timeout=1.0, check=False)
    first, second, unchecked = bench.readings
    assert (first.waits, first.already, first.before) == (True, True, "standby")
    assert first.after_write.startswith("ModeRequest at test_hygiene.py:")
    assert (second.waits, second.already) == (True, False)
    assert not unchecked.already  # ...but it isn't one of the test's checks


def test_a_read_is_never_judged_as_a_wait(bench):
    bench.write(bench.signals.ModeRequest, "standby")
    bench.read(bench.signals.Mode, timeout=1.0)
    (reading,) = bench.readings
    assert not reading.waits and not reading.already


def test_a_predicate_that_fails_on_the_old_value_isnt_a_problem(bench):
    bench.write(bench.signals.ModeRequest, "run")
    bench.wait_until(bench.signals.Mode, "run", timeout=1.0)
    bench.readings = []

    calls = []

    def one(value):
        calls.append(value)
        if len(calls) == 1:  # the first call is on the value from before the write
            raise ValueError("can't judge that one")
        return value == 1.0

    bench.write(bench.signals.Setpoint, 1.0)  # Measured was 0.0 before this write
    assert bench.wait_until(bench.signals.Measured, one, timeout=1.0) == 1.0
    (reading,) = bench.readings
    assert not reading.already
    assert calls[0] == 0.0
