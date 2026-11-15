"""The test check through pytest: --check-tests reruns each passing test with its readings
corrupted, judges whether it can fail, and stores the verdicts. The corruption modes themselves
are in test_testcheck.py."""

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


def test_a_test_with_a_real_check_can_fail(project):
    project.makepyfile(test_bench=GOOD)
    result = project.runpytest(*CLI, "--check-tests")
    assert result.ret == pytest.ExitCode.OK
    result.assert_outcomes(passed=1)  # the reruns aren't counted
    result.stdout.fnmatch_lines(
        [
            "*= BenchTare test check =*",
            "test_bench.py::test_tracks: able to fail (shift, stale)",
            "Test check: 1 able to fail",
        ]
    )


def test_a_test_that_ignores_what_it_reads_is_unable_to_fail(project):
    project.makepyfile(
        test_bench="""
        def test_ignores_it(at_zero, bench, signals):
            bench.write(signals.Setpoint, 42.0)
            bench.read(signals.Measured, timeout=1.0)
        """
    )
    result = project.runpytest(*CLI, "--check-tests")
    assert result.ret == pytest.ExitCode.TESTS_FAILED  # though the test passed
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(
        [
            "test_bench.py::test_ignores_it: UNABLE TO FAIL",
            "    shift: passed, reading Measured * instead of the * that arrived",
            "    stale: passed, reading Measured 0.0 degC from before the write*",
            "Test check: 1 unable to fail",
        ]
    )


def test_a_mode_a_test_passes_is_shown_even_when_it_can_fail(project):
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
            "test_bench.py::test_accepts_almost_anything: able to fail (stale)",
            "    shift: passed, reading Measured 84.0 degC instead of the 42.0 degC that arrived",
            "Test check: 1 able to fail",
        ]
    )


def test_stale_shows_a_test_that_cant_tell_whether_the_dut_responded(project):
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
    result = project.runpytest(*CLI, "--check-tests")
    assert result.ret == pytest.ExitCode.OK
    result.stdout.fnmatch_lines(
        [
            "test_bench.py::test_tracks: able to fail (shift)",
            (
                "    stale: passed, reading Measured 42.0 degC from before the write, the same "
                "as what arrived after it, so it can't tell whether the DUT responded"
            ),
        ]
    )


def test_staging_reads_the_real_values(project):
    # The staging fixture waits for Mode 'run'. Were it corrupted, every rerun would fail in
    # setup and the verdict would be inconclusive.
    project.makepyfile(test_bench=GOOD)
    result = project.runpytest(*CLI, "--check-tests")
    result.stdout.no_fnmatch_line("*staging didn't pass*")
    result.stdout.fnmatch_lines(["*: able to fail (shift, stale)"])


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


def test_a_rerun_where_nothing_arrives_proves_nothing(project):
    # The test only fails on its reruns, and only because no value arrives: a timeout fails any
    # test, good or bad, so it doesn't count as able to fail.
    project.makepyfile(
        test_bench="""
        RUNS = []


        def test_times_out_on_reruns(running, bench, signals):
            RUNS.append(1)
            if len(RUNS) > 1:
                bench.simulation.cut("io.in1")  # Measured stops arriving
            bench.write(signals.Setpoint, 42.0)
            bench.read(signals.Measured, timeout=0.3)
        """
    )
    result = project.runpytest(*CLI, "--check-tests")
    assert result.ret == pytest.ExitCode.OK  # inconclusive is reported, but fails nothing
    result.stdout.fnmatch_lines(
        [
            "test_bench.py::test_times_out_on_reruns: inconclusive",
            "    shift: inconclusive, nothing arrived to corrupt: *no value of Measured arrived*",
            "    stale: inconclusive, nothing arrived to corrupt: *",
            "Test check: 1 inconclusive",
        ]
    )


def test_a_rerun_that_fails_with_nothing_corrupted_may_be_flaky(project):
    project.makepyfile(
        test_bench="""
        RUNS = []


        def test_flaky(at_zero, bench, signals):
            RUNS.append(1)
            bench.write(signals.Setpoint, 42.0)
            assert len(RUNS) == 1, "only passes the first time"
        """
    )
    result = project.runpytest(*CLI, "--check-tests")
    result.stdout.fnmatch_lines(
        [
            "test_bench.py::test_flaky: inconclusive",
            (
                "    shift: inconclusive, failed with nothing corrupted, so the test may be "
                "flaky: AssertionError: only passes the first time*"
            ),
        ]
    )


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
            "test_bench.py::test_only_writes: not checked",
            "    shift: not applied, nothing it read could be shifted",
            "    stale: not applied, nothing it read had a value from before a write",
            "test_bench.py::test_plain: not checked, because it doesn't use the bench",
            "Test check: 2 not checked",
        ]
    )


def test_the_check_is_stored_with_each_tests_verdict_and_reruns(project):
    project.makepyfile(
        test_bench=GOOD
        + """

def test_ignores_it(at_zero, bench, signals):
    bench.write(signals.Setpoint, 42.0)
    bench.read(signals.Measured, timeout=1.0)
"""
    )
    project.runpytest(*CLI, "--check-tests")
    assert query(project, "SELECT kind, exit_code FROM runs") == [("check-tests", 1)]
    assert query(project, "SELECT nodeid, outcome, check_verdict FROM test_results") == [
        ("test_bench.py::test_tracks", "passed", "able to fail"),
        ("test_bench.py::test_ignores_it", "passed", "unable to fail"),
    ]
    checks = query(
        project, "SELECT test_seq, mode, outcome FROM test_checks ORDER BY test_seq, mode"
    )
    assert checks == [
        (1, "shift", "failed"),
        (1, "stale", "failed"),
        (2, "shift", "passed"),
        (2, "stale", "passed"),
    ]
    phases = query(
        project,
        "SELECT DISTINCT phase FROM record_entries WHERE test_seq = 2 ORDER BY phase",
    )
    assert phases == [
        ("call",),
        ("setup",),
        ("shift call",),
        ("shift setup",),
        ("shift teardown",),
        ("stale call",),
        ("stale setup",),
        ("stale teardown",),
        ("teardown",),
    ]
    ((data,),) = query(
        project,
        "SELECT data FROM record_entries WHERE test_seq = 2 AND phase = 'shift call' "
        "AND kind = 'read'",
    )
    assert json.loads(data)["check"] == "shift"


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
