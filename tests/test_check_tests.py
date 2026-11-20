"""The test check through pytest: --check-tests walks through each passing test a reading at a
time, proves each reading's check can fail, and stores the verdicts. The corruption modes
themselves are in test_testcheck.py.

Several tests count their runs in a module-level list, to misbehave on one rerun. The order is:
the usual run, the control, then for each reading shift and stale. So with one reading, run 3
is its shift rerun and run 4 its stale rerun; with two, runs 5 and 6 are reading 2's."""

import json
import shutil
import sqlite3

import pytest

from benchtare import reference

FILES = [reference.DUT, reference.DBC, reference.BENCH, reference.HARNESS_IO]
CLI = [f"--dut={reference.DUT}", f"--bench={reference.BENCH}", f"--harness={reference.HARNESS_IO}"]

STAGING = """
import pytest


@pytest.fixture
def running(bench, signals):
    bench.read(signals.Mode, timeout=2.0)
    bench.write(signals.ModeRequest, "run")
    bench.wait_until(signals.Mode, "run", timeout=1.0)


@pytest.fixture
def at_zero(running, bench, signals):
    # A known starting point that differs from what the tests ask for.
    bench.write(signals.Setpoint, 0.0)
    bench.wait_until(signals.Measured, 0.0, timeout=1.0)
"""

GOOD = """
import pytest


def test_tracks(at_zero, bench, signals):
    bench.write(signals.Setpoint, 42.0)
    bench.wait_until(signals.Measured, pytest.approx(42.0, abs=0.5), timeout=1.0)
"""


@pytest.fixture
def project(pytester):
    for name in FILES:
        shutil.copy(reference.path(name), pytester.path / name)
    pytester.makeconftest(STAGING)
    return pytester


def query(project, sql):
    db = sqlite3.connect(project.path / ".benchtare" / "results.sqlite3")
    try:
        return db.execute(sql).fetchall()
    finally:
        db.close()


# --- every step of a test is exercised -------------------------------------------------------------


def test_a_test_with_a_real_check_can_fail(project):
    project.makepyfile(test_bench=GOOD)
    result = project.runpytest(*CLI, "--check-tests")
    assert result.ret == pytest.ExitCode.OK
    result.assert_outcomes(passed=1)  # the reruns aren't counted
    result.stdout.fnmatch_lines(
        [
            "*= BenchTare test check =*",
            "test_bench.py::test_tracks: able to fail at its reading",
            "Test check: 1 able to fail",
        ]
    )


def test_every_step_is_shown_with_where_it_failed(project):
    project.makepyfile(
        test_bench="""
        import pytest


        def test_steps(at_zero, bench, signals):
            bench.write(signals.Setpoint, 42.0)
            bench.wait_until(signals.Measured, pytest.approx(42.0, abs=0.5), timeout=1.0)
            assert bench.read(signals.Mode, timeout=1.0) == "run"
        """
    )
    result = project.runpytest(*CLI, "--check-tests", "-v")
    assert result.ret == pytest.ExitCode.OK
    result.stdout.fnmatch_lines(
        [
            "test_bench.py::test_steps: able to fail at each of its 2 readings",
            "    control: passed, passed again unchanged, taking the same 2 readings",
            "    reading 1, Measured (test_bench.py:6): able to fail (shift, stale)",
            "        shift: failed, failed at test_bench.py:6: *ComparisonFailed: Measured did not *",
            "        stale: failed, failed at test_bench.py:6: *the last 0.0 degC",
            "    reading 2, Mode (test_bench.py:7): able to fail (shift)",
            "        shift: failed, failed at test_bench.py:7: AssertionError: assert 'standby' *",
            "        stale: not applied, it was already 'run' before the write, so stale can't change this step",
        ]
    )


def test_a_step_that_isnt_checked_makes_the_test_unable_to_fail(project):
    # Corrupting everything at once would stop at the first check and call this test able to
    # fail. Going a reading at a time finds the step that checks nothing.
    project.makepyfile(
        test_bench="""
        import pytest


        def test_two_steps(at_zero, bench, signals):
            bench.write(signals.Setpoint, 42.0)
            bench.wait_until(signals.Measured, pytest.approx(42.0, abs=0.5), timeout=1.0)
            bench.read(signals.Mode, timeout=1.0)  # read, but never checked
        """
    )
    result = project.runpytest(*CLI, "--check-tests")
    assert result.ret == pytest.ExitCode.TESTS_FAILED  # though the test passed
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(
        [
            "test_bench.py::test_two_steps: UNABLE TO FAIL at 1 of 2 readings",
            "    reading 2, Mode (test_bench.py:7): unable to fail",
            "        shift: passed, reading Mode 'standby' instead of the 'run' that arrived",
            "Test check: 1 unable to fail",
        ]
    )
    result.stdout.no_fnmatch_line("    reading 1*")  # its one good step needs no comment


def test_a_test_that_ignores_what_it_reads_is_unable_to_fail(project):
    project.makepyfile(
        test_bench="""
        def test_ignores_it(at_zero, bench, signals):
            bench.write(signals.Setpoint, 42.0)
            bench.read(signals.Measured, timeout=1.0)
        """
    )
    result = project.runpytest(*CLI, "--check-tests")
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(
        [
            "test_bench.py::test_ignores_it: UNABLE TO FAIL at its reading",
            "    reading 1, Measured (test_bench.py:3): unable to fail",
            "        shift: passed, reading Measured * instead of the * that arrived",
        ]
    )


def test_a_mode_a_reading_passes_is_shown_even_when_it_can_fail(project):
    # A check this loose catches a DUT that ignores the write, but not a wrong value.
    project.makepyfile(
        test_bench="""
        def test_accepts_almost_anything(at_zero, bench, signals):
            bench.write(signals.Setpoint, 42.0)
            bench.wait_until(signals.Measured, lambda value: value > 10, timeout=1.0)
        """
    )
    result = project.runpytest(*CLI, "--check-tests")
    assert result.ret == pytest.ExitCode.OK
    result.stdout.fnmatch_lines(
        [
            "test_bench.py::test_accepts_almost_anything: able to fail at its reading",
            "    reading 1, Measured (test_bench.py:3): able to fail (stale)",
            "        shift: passed, reading Measured 84.0 degC instead of the 42.0 degC that arrived",
        ]
    )


def test_a_reading_marked_unchecked_is_left_out(project):
    project.makepyfile(
        test_bench=GOOD
        + """

def test_only_records(at_zero, bench, signals):
    bench.write(signals.Setpoint, 42.0)
    bench.wait_until(signals.Measured, pytest.approx(42.0, abs=0.5), timeout=1.0)
    bench.read(signals.Mode, timeout=1.0, check=False)  # kept for trends, on purpose


def test_checks_nothing_on_purpose(at_zero, bench, signals):
    bench.read(signals.Mode, timeout=1.0, check=False)
"""
    )
    result = project.runpytest(*CLI, "--check-tests")
    assert result.ret == pytest.ExitCode.OK
    result.stdout.fnmatch_lines(
        [
            "test_bench.py::test_only_records: able to fail at its reading",
            (
                "test_bench.py::test_checks_nothing_on_purpose: not checked, because it takes "
                "no checked reading in its body"
            ),
        ]
    )


def test_stale_says_when_the_dut_was_already_where_the_test_expects(project):
    # No known starting point: on the rerun, Measured is already 42 from the run before, so a
    # DUT that ignored the write would pass too. Shift still shows the check works.
    project.makepyfile(
        test_bench="""
        import pytest


        def test_tracks(running, bench, signals):
            bench.write(signals.Setpoint, 42.0)
            bench.wait_until(signals.Measured, pytest.approx(42.0, abs=0.5), timeout=1.0)
        """
    )
    result = project.runpytest(*CLI, "--check-tests", "-v")
    assert result.ret == pytest.ExitCode.OK
    result.stdout.fnmatch_lines(
        [
            "test_bench.py::test_tracks: able to fail at its reading",
            "    reading 1, Measured (test_bench.py:6): able to fail (shift)",
            (
                "        stale: not applied, it was already 42.0 degC before the write, so stale "
                "can't change this step"
            ),
        ]
    )


def test_staging_reads_the_real_values(project):
    # This staging's first reading must match. Were staging corrupted as well as the test's
    # body, the reruns would fail in setup and the verdict would be inconclusive.
    project.makeconftest(
        STAGING
        + """

@pytest.fixture
def strict(bench, signals):
    bench.write(signals.ModeRequest, "run")
    bench.wait_until(signals.Mode, "run", timeout=1.0)
    bench.write(signals.Setpoint, 0.0)
    bench.wait_until(signals.Measured, 0.0, timeout=1.0)
"""
    )
    project.makepyfile(test_bench=GOOD.replace("at_zero", "strict"))
    result = project.runpytest(*CLI, "--check-tests", "-v")
    result.stdout.no_fnmatch_line("*staging didn't pass*")
    result.stdout.fnmatch_lines(["*: able to fail at its reading"])


# --- every failure counted is the corrupted reading's doing ----------------------------------------


def test_a_failure_caught_at_a_later_step_counts_and_says_where(project):
    project.makepyfile(
        test_bench="""
        import pytest


        def test_checks_at_the_end(at_zero, bench, signals):
            bench.write(signals.Setpoint, 42.0)
            measured = bench.read(signals.Measured, timeout=1.0)
            bench.wait_until(signals.Mode, "run", timeout=1.0)
            assert measured in (0.0, 42.0)
        """
    )
    result = project.runpytest(*CLI, "--check-tests", "-v")
    result.stdout.fnmatch_lines(
        [
            "    reading 1, Measured (test_bench.py:6): able to fail (shift*",
            (
                "        shift: failed, failed later, at test_bench.py:8, after reading 2: "
                "assert * in (0.0, 42.0)"
            ),
        ]
    )


# --- a test that doesn't repeat is a test hygiene problem, not a logic verdict -------------------


def test_a_failure_before_the_corrupted_step_means_the_test_doesnt_repeat(project):
    project.makepyfile(
        test_bench="""
        from pathlib import Path

        import pytest


        def test_glitches_once(at_zero, bench, signals):
            with Path("runs.txt").open("a") as runs:
                runs.write("run\\n")
            bench.write(signals.Setpoint, 42.0)
            bench.wait_until(signals.Measured, pytest.approx(42.0, abs=0.5), timeout=1.0)
            runs = len(Path("runs.txt").read_text().splitlines())
            assert runs != 5, "a glitch on the shift rerun for reading 2, before reaching it"
            assert bench.read(signals.Mode, timeout=1.0) == "run"
        """
    )
    result = project.runpytest(*CLI, "--check-tests")
    assert result.ret == pytest.ExitCode.OK  # hygiene doesn't fail the test check
    result.stdout.fnmatch_lines(
        [
            (
                "test_bench.py::test_glitches_once: not checked, because of test hygiene: on the "
                "rerun for reading 2, it failed at test_bench.py:12 before reaching that "
                "reading, where it passed unchanged: AssertionError: a glitch*"
            ),
            "Test check: 1 not checked (1 because of test hygiene)",
        ]
    )
    # The walk stopped there: no stale rerun of reading 2 after the test failed to repeat.
    assert len((project.path / "runs.txt").read_text().splitlines()) == 5


def test_a_failure_with_nothing_corrupted_means_the_test_doesnt_repeat(project):
    project.makepyfile(
        test_bench="""
        RUNS = []


        def test_flaky(at_zero, bench, signals):
            RUNS.append(1)
            bench.write(signals.Setpoint, 42.0)
            bench.read(signals.Mode, timeout=1.0)
            # Run 4 is the stale rerun, which can't change Mode: it was 'run' all along.
            assert len(RUNS) != 4, "fails on its own"
        """
    )
    result = project.runpytest(*CLI, "--check-tests")
    result.stdout.fnmatch_lines(
        [
            (
                "test_bench.py::test_flaky: not checked, because of test hygiene: on the rerun for "
                "reading 1, it failed at test_bench.py:9 with nothing corrupted: AssertionError: "
                "fails on its own*"
            ),
        ]
    )


def test_a_rerun_where_nothing_arrives_proves_nothing(project):
    # Only the reruns that corrupt a reading lose the value: a timeout fails any test, good or
    # bad, so it doesn't count as able to fail.
    project.makepyfile(
        test_bench="""
        RUNS = []


        def test_times_out_on_reruns(running, bench, signals):
            RUNS.append(1)
            if len(RUNS) > 2:
                bench.simulation.cut("io.in1")  # Measured stops arriving
            bench.write(signals.Setpoint, 42.0)
            bench.read(signals.Measured, timeout=1.0)
        """
    )
    result = project.runpytest(*CLI, "--check-tests")
    assert result.ret == pytest.ExitCode.OK  # inconclusive is reported, but fails nothing
    result.stdout.fnmatch_lines(
        [
            "test_bench.py::test_times_out_on_reruns: inconclusive",
            "    reading 1, Measured (test_bench.py:9): inconclusive",
            (
                "        shift: inconclusive, nothing arrived, at test_bench.py:9: *no value of "
                "Measured arrived*"
            ),
            "Test check: 1 inconclusive",
        ]
    )


def test_a_test_that_fails_when_run_again_is_not_checked_for_test_hygiene(project):
    project.makepyfile(
        test_bench="""
        import pytest

        RUNS = []


        def test_only_passes_once(at_zero, bench, signals):
            RUNS.append(1)
            bench.write(signals.Setpoint, 42.0)
            bench.wait_until(signals.Measured, pytest.approx(42.0, abs=0.5), timeout=1.0)
            assert len(RUNS) == 1, "only passes the first time"
        """
    )
    result = project.runpytest(*CLI, "--check-tests")
    assert result.ret == pytest.ExitCode.OK
    result.stdout.fnmatch_lines(
        [
            (
                "test_bench.py::test_only_passes_once: not checked, because of test hygiene: it "
                "failed when it ran again unchanged, at test_bench.py:10: AssertionError: only "
                "passes the first time*"
            ),
        ]
    )
    result.stdout.no_fnmatch_line("    *")  # no step is judged, and no rerun is listed
    # The walk stopped at the control: no reading was rerun.
    assert query(project, "SELECT mode, outcome FROM test_checks") == [
        ("control", "not repeatable")
    ]


def test_a_test_that_takes_different_readings_is_not_checked_for_test_hygiene(project):
    project.makepyfile(
        test_bench="""
        import pytest

        RUNS = []


        def test_wanders(at_zero, bench, signals):
            RUNS.append(1)
            bench.write(signals.Setpoint, 42.0)
            if len(RUNS) == 2:
                assert bench.read(signals.Mode, timeout=1.0) == "run"
            bench.wait_until(signals.Measured, pytest.approx(42.0, abs=0.5), timeout=1.0)
        """
    )
    result = project.runpytest(*CLI, "--check-tests")
    result.stdout.fnmatch_lines(
        [
            (
                "test_bench.py::test_wanders: not checked, because of test hygiene: run again "
                "unchanged, it read Mode, Measured instead of Measured"
            ),
        ]
    )


# --- what isn't checked ----------------------------------------------------------------------------


def test_a_test_that_fails_is_not_rerun(project):
    project.makepyfile(
        test_bench="""
        from pathlib import Path


        def test_misses(at_zero, bench, signals):
            with Path("runs.txt").open("a") as runs:
                runs.write("run\\n")
            bench.write(signals.Setpoint, 42.0)
            bench.wait_until(signals.Measured, 99.0, timeout=0.2)
        """
    )
    result = project.runpytest(*CLI, "--check-tests")
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["test_bench.py::test_misses: not checked, because it failed"])
    assert (project.path / "runs.txt").read_text().splitlines() == ["run"]


def test_a_test_that_reads_nothing_is_not_checked(project):
    project.makepyfile(
        test_bench="""
        def test_only_writes(at_zero, bench, signals):
            bench.write(signals.Setpoint, 42.0)


        def test_plain():
            assert 1 + 1 == 2
        """
    )
    result = project.runpytest(*CLI, "--check-tests")
    assert result.ret == pytest.ExitCode.OK
    result.stdout.fnmatch_lines(
        [
            (
                "test_bench.py::test_only_writes: not checked, because it takes no checked "
                "reading in its body"
            ),
            "test_bench.py::test_plain: not checked, because it doesn't use the bench",
            "Test check: 2 not checked",
        ]
    )


# --- stored ---------------------------------------------------------------------------------------


def test_the_check_is_stored_with_each_tests_verdict_and_reruns(project):
    project.makepyfile(
        test_bench=GOOD
        + """

def test_ignores_it(at_zero, bench, signals):
    bench.write(signals.Setpoint, 42.0)
    bench.read(signals.Mode, timeout=1.0)
"""
    )
    project.runpytest(*CLI, "--check-tests")
    assert query(project, "SELECT kind, exit_code FROM runs") == [("check-tests", 1)]
    assert query(project, "SELECT nodeid, outcome, check_verdict FROM test_results") == [
        ("test_bench.py::test_tracks", "passed", "able to fail"),
        ("test_bench.py::test_ignores_it", "passed", "unable to fail"),
    ]
    checks = query(
        project,
        "SELECT test_seq, reading, signal, location, mode, outcome FROM test_checks "
        "ORDER BY test_seq, reading, mode",
    )
    assert checks == [
        (1, 0, None, None, "control", "passed"),
        (1, 1, "Measured", "test_bench.py:6", "shift", "failed"),
        (1, 1, "Measured", "test_bench.py:6", "stale", "failed"),
        (2, 0, None, None, "control", "passed"),
        (2, 1, "Mode", "test_bench.py:11", "shift", "passed"),
        (2, 1, "Mode", "test_bench.py:11", "stale", "not applied"),
    ]
    phases = query(
        project,
        "SELECT DISTINCT phase FROM record_entries WHERE test_seq = 2 ORDER BY phase",
    )
    assert [phase for (phase,) in phases] == [
        "call",
        "control call",
        "control setup",
        "control teardown",
        "reading 1 shift call",
        "reading 1 shift setup",
        "reading 1 shift teardown",
        "reading 1 stale call",
        "reading 1 stale setup",
        "reading 1 stale teardown",
        "setup",
        "teardown",
    ]
    ((data,),) = query(
        project,
        "SELECT data FROM record_entries WHERE test_seq = 2 AND phase = 'reading 1 shift call' "
        "AND kind = 'read'",
    )
    assert json.loads(data)["corruption"] == "shift"
    assert json.loads(data)["where"] == "test_bench.py:11"


def test_a_usual_run_says_which_test_files_need_checking(project):
    project.makepyfile(test_bench=GOOD)
    result = project.runpytest(*CLI)
    result.stdout.fnmatch_lines(
        [
            (
                "benchtare test check: never checked: test_bench.py. Run benchtare check-tests "
                "to check them."
            )
        ]
    )
    project.runpytest(*CLI, "--check-tests")
    result = project.runpytest(*CLI)
    result.stdout.fnmatch_lines(
        ["benchtare test check: every test file was checked, and none has changed since"]
    )
    project.makepyfile(test_bench=GOOD + "\n# changed\n")
    result = project.runpytest(*CLI)
    result.stdout.fnmatch_lines(
        ["benchtare test check: changed since their last check: test_bench.py. Run *"]
    )
    rows = query(
        project,
        "SELECT r.kind, m.last_checked_at IS NOT NULL, m.unchanged_since_check "
        "FROM manifest_files m JOIN runs r ON r.id = m.run_id "
        "WHERE m.role = 'test' ORDER BY r.started_at",
    )
    assert rows == [
        ("tests", 0, None),
        ("check-tests", 0, None),
        ("tests", 1, 1),
        ("tests", 1, 0),
    ]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (
            ["--check-tests", "--selftest-only"],
            "*--check-tests and --selftest-only can't be used together*",
        ),
        (["--check-tests"], "*There's nothing to check: BenchTare isn't configured.*"),
    ],
    ids=["with-selftest-only", "not-configured"],
)
def test_check_tests_needs_a_configured_bench_and_tests(pytester, args, message):
    if "--selftest-only" in args:
        for name in FILES:
            shutil.copy(reference.path(name), pytester.path / name)
        args = [*CLI, *args]
    result = pytester.runpytest(*args)
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines([message])
