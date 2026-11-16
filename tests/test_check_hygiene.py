"""The hygiene check through pytest: --check-hygiene runs each passing test again straight after
itself and judges three rules: it repeats, it leaves the DUT clean, it starts from a meaningful
state. The logic itself is in test_hygiene.py, and loading clean_state: in test_clean_state.py.
"""

import json
import shutil
import sqlite3
import textwrap

import pytest

from benchtare import reference

FILES = [reference.DUT, reference.DBC, reference.BENCH, reference.HARNESS_IO]
CLI = [f"--dut={reference.DUT}", f"--bench={reference.BENCH}", f"--harness={reference.HARNESS_IO}"]

CLEAN_STATE = """
clean_state:
  Mode: standby
  supply_enable: false
  Setpoint: 0.0
"""

STAGING = """
import pytest


@pytest.fixture
def clean(bench, signals):
    # Every test starts from a known state: booted, standby, supply off, setpoint 0.
    bench.read(signals.Mode, timeout=2.0)
    bench.write(signals.supply_enable, False)
    bench.write(signals.Setpoint, 0.0)
    bench.write(signals.ModeRequest, "standby")
    bench.wait_until(signals.Mode, "standby", timeout=1.0)


@pytest.fixture
def running(clean, bench, signals):
    bench.write(signals.ModeRequest, "run")
    bench.wait_until(signals.Mode, "run", timeout=1.0)
    yield
    bench.write(signals.Setpoint, 0.0)
    bench.write(signals.ModeRequest, "standby")
    bench.wait_until(signals.Mode, "standby", timeout=1.0)


@pytest.fixture
def mode_only(bench, signals):
    # Puts the DUT in run and back, and touches nothing else.
    bench.read(signals.Mode, timeout=2.0)
    bench.write(signals.ModeRequest, "run")
    bench.wait_until(signals.Mode, "run", timeout=1.0)
    yield
    bench.write(signals.ModeRequest, "standby")
    bench.wait_until(signals.Mode, "standby", timeout=1.0)
"""

CLEAN_TEST = """
import pytest


def test_tracks(running, bench, signals):
    bench.write(signals.Setpoint, 42.0)
    bench.wait_until(signals.Measured, pytest.approx(42.0, abs=0.5), timeout=1.0)
"""


def describe(project, clean_state=CLEAN_STATE):
    path = project.path / reference.DUT
    path.write_text(reference.path(reference.DUT).read_text() + textwrap.dedent(clean_state))


@pytest.fixture
def project(pytester):
    for name in FILES:
        shutil.copy(reference.path(name), pytester.path / name)
    describe(pytester)
    pytester.makeconftest(STAGING)
    return pytester


def query(project, sql):
    db = sqlite3.connect(project.path / ".benchtare" / "results.sqlite3")
    try:
        return db.execute(sql).fetchall()
    finally:
        db.close()


# --- a clean test ---------------------------------------------------------------------------------


def test_a_clean_test_is_clean(project):
    project.makepyfile(test_bench=CLEAN_TEST)
    result = project.runpytest(*CLI, "--check-hygiene")
    assert result.ret == pytest.ExitCode.OK
    result.assert_outcomes(passed=1)  # the repeat isn't counted
    result.stdout.fnmatch_lines(
        [
            "*= BenchTare hygiene check =*",
            "test_bench.py::test_tracks: clean",
            "Hygiene check: 1 clean",
        ]
    )


def test_verbose_shows_every_rule_and_every_item(project):
    project.makepyfile(test_bench=CLEAN_TEST)
    result = project.runpytest(*CLI, "--check-hygiene", "-v")
    result.stdout.fnmatch_lines(
        [
            "test_bench.py::test_tracks: clean",
            "    it repeats: passed, it passed again straight after itself, taking the same 1 reading",
            (
                "    it leaves the DUT clean: passed, Mode, Setpoint, supply_enable clean after "
                "teardown"
            ),
            (
                "    it starts from a meaningful state: passed, none of its 1 wait was already "
                "satisfied before the write it followed"
            ),
            "        run 1, Mode: same, Mode was 'standby' after teardown, its clean value",
            "        run 1, supply_enable: same, *",
            "        run 1, Setpoint: same, Setpoint was 0.0 degC after teardown, its clean value",
            "        run 2, Mode: same, *",
        ]
    )


# --- it leaves the DUT clean ------------------------------------------------------------------------


def test_a_test_that_leaves_an_input_dirty_is_not_clean(project):
    project.makepyfile(
        test_bench="""
        def test_leaves_the_supply_on(running, bench, signals):
            bench.write(signals.supply_enable, True)
            bench.wait_until(signals.supply_ok, True, timeout=1.0)
        """
    )
    result = project.runpytest(*CLI, "--check-hygiene")
    assert result.ret == pytest.ExitCode.TESTS_FAILED  # though the test passed
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(
        [
            "test_bench.py::test_leaves_the_supply_on: NOT CLEAN",
            (
                "    it leaves the DUT clean: failed, supply_enable was still True after teardown, "
                "not its clean value False"
            ),
            "Hygiene check: 1 not clean",
        ]
    )


def test_a_test_that_leaves_an_output_dirty_is_not_clean(project):
    project.makepyfile(
        test_bench="""
        def test_leaves_run_mode(clean, bench, signals):
            bench.write(signals.ModeRequest, "run")
            bench.wait_until(signals.Mode, "run", timeout=1.0)
        """
    )
    result = project.runpytest(*CLI, "--check-hygiene")
    result.stdout.fnmatch_lines(
        [
            "test_bench.py::test_leaves_run_mode: NOT CLEAN",
            (
                "    it leaves the DUT clean: failed, Mode was still 'run' 1 s after teardown, not "
                "its clean value 'standby' (it was 'standby' before setup)"
            ),
        ]
    )


def test_only_the_test_that_left_a_mess_is_blamed(project):
    project.makepyfile(
        test_bench="""
        def test_a_leaves_the_supply_on(running, bench, signals):
            bench.write(signals.supply_enable, True)
            bench.wait_until(signals.supply_ok, True, timeout=1.0)


        def test_b_finds_it_on_and_leaves_it(mode_only, bench, signals):
            bench.wait_until(signals.Mode, "run", timeout=1.0)


        def test_c_cleans_it_up(running, bench, signals):
            bench.wait_until(signals.Mode, "run", timeout=1.0)
        """
    )
    result = project.runpytest(*CLI, "--check-hygiene", "-v")
    result.stdout.fnmatch_lines(
        [
            "test_bench.py::test_a_leaves_the_supply_on: NOT CLEAN",
            "test_bench.py::test_b_finds_it_on_and_leaves_it: clean",
            (
                "    it leaves the DUT clean: passed, Mode, Setpoint clean after teardown; not "
                "counted: supply_enable was already True before setup, not its clean value False: "
                "an earlier test left it so, and this test didn't change it"
            ),
            "test_bench.py::test_c_cleans_it_up: clean",
            (
                "        run 1, supply_enable: same, supply_enable was False after teardown, its "
                "clean value, though an earlier test had left it at True"
            ),
            "Hygiene check: 2 clean, 1 not clean",
        ]
    )


def test_an_input_never_written_before_is_still_judged_by_its_clean_value(project):
    project.makepyfile(
        test_bench="""
        import pytest


        def test_sets_without_staging(mode_only, bench, signals):
            bench.write(signals.Setpoint, 42.0)
            bench.wait_until(signals.Measured, pytest.approx(42.0, abs=0.5), timeout=1.0)
        """
    )
    result = project.runpytest(*CLI, "--check-hygiene")
    result.stdout.fnmatch_lines(
        [
            "test_bench.py::test_sets_without_staging: NOT CLEAN",
            (
                "    it leaves the DUT clean: failed, Setpoint was still 42.0 degC after teardown, "
                "not its clean value 0.0 degC"
            ),
        ]
    )


def test_without_a_clean_value_an_item_is_compared_before_and_after(project):
    describe(project, "clean_state: [Mode, supply_enable]\n")
    project.makepyfile(
        test_bench="""
        def test_a_stages_the_supply_off(running, bench, signals):
            bench.wait_until(signals.Mode, "run", timeout=1.0)


        def test_b_leaves_the_supply_on(running, bench, signals):
            bench.write(signals.supply_enable, True)
            bench.wait_until(signals.supply_ok, True, timeout=1.0)


        def test_c_puts_it_back(running, bench, signals):
            bench.wait_until(signals.Mode, "run", timeout=1.0)
        """
    )
    result = project.runpytest(*CLI, "--check-hygiene", "-v")
    result.stdout.fnmatch_lines(
        [
            "test_bench.py::test_a_stages_the_supply_off: clean",
            "test_bench.py::test_b_leaves_the_supply_on: NOT CLEAN",
            (
                "    it leaves the DUT clean: failed, supply_enable was False before setup, and "
                "still True after teardown"
            ),
            "test_bench.py::test_c_puts_it_back: clean",
            (
                "        run 1, supply_enable: same, supply_enable was False after teardown, as "
                "the session first found it; an earlier test had left it at True"
            ),
        ]
    )


def test_without_a_clean_value_the_first_test_to_write_an_input_cant_be_judged_on_it(project):
    # Nothing was known before its setup. That is a limit of comparing before and after, and why
    # an input is best given a clean value: the bench always knows what it wrote.
    describe(project, "clean_state: [Mode, supply_enable]\n")
    project.makepyfile(
        test_bench="""
        def test_leaves_the_supply_on(running, bench, signals):
            bench.write(signals.supply_enable, True)
        """
    )
    result = project.runpytest(*CLI, "--check-hygiene", "-v")
    result.stdout.fnmatch_lines(
        [
            "test_bench.py::test_leaves_the_supply_on: clean",
            (
                "    it leaves the DUT clean: passed, Mode clean after teardown; not counted: "
                "supply_enable: it hadn't been written, so its value isn't known*"
            ),
        ]
    )


def test_an_input_with_no_known_value_isnt_compared_and_says_why(project):
    describe(project, "clean_state: [Mode, Setpoint]\n")
    project.makepyfile(
        test_bench="""
        def test_mode(mode_only, bench, signals):
            bench.wait_until(signals.Mode, "run", timeout=1.0)
        """
    )
    result = project.runpytest(*CLI, "--check-hygiene", "-v")
    result.stdout.fnmatch_lines(
        [
            (
                "    it leaves the DUT clean: passed, Mode clean after teardown; not counted: "
                "Setpoint: it hadn't been written, so its value isn't known: write it in staging, "
                "so every test starts from a known value"
            ),
        ]
    )


def test_with_no_clean_state_that_rule_isnt_checked_but_the_others_are(project):
    shutil.copy(reference.path(reference.DUT), project.path / reference.DUT)
    project.makepyfile(test_bench=CLEAN_TEST)
    result = project.runpytest(*CLI, "--check-hygiene", "-v")
    assert result.ret == pytest.ExitCode.OK
    result.stdout.fnmatch_lines(
        [
            "test_bench.py::test_tracks: clean",
            "    it repeats: passed, *",
            (
                "    it leaves the DUT clean: not checked, the interface description doesn't list "
                "what shows the DUT is clean (clean_state:)"
            ),
            "    it starts from a meaningful state: passed, *",
        ]
    )


# --- state probes -----------------------------------------------------------------------------------

PROBE = """

FAULTS = []


def pytest_benchtare_state(bench, config):
    return {"stored_faults": len(FAULTS)}


@pytest.fixture
def faults():
    return FAULTS


@pytest.fixture
def cleared_faults():
    yield FAULTS
    FAULTS.clear()
"""


def test_a_state_probe_is_compared_like_a_signal(project):
    describe(project, CLEAN_STATE + "  stored_faults: {probe: true, clean: 0, settle: 0}\n")
    project.makeconftest(STAGING + PROBE)
    project.makepyfile(
        test_bench="""
        def test_leaves_a_fault(running, faults, bench, signals):
            faults.append("overtemperature")
            bench.wait_until(signals.Mode, "run", timeout=1.0)


        def test_clears_its_fault(running, cleared_faults, bench, signals):
            cleared_faults.append("overtemperature")
            bench.wait_until(signals.Mode, "run", timeout=1.0)
        """
    )
    result = project.runpytest(*CLI, "--check-hygiene", "-p", "no:randomly")
    result.stdout.fnmatch_lines(
        [
            "test_bench.py::test_leaves_a_fault: NOT CLEAN",
            (
                "    it leaves the DUT clean: failed, stored_faults was still 1 after teardown, not "
                "its clean value 0 (it was 0 before setup)"
            ),
            "test_bench.py::test_clears_its_fault: clean",
        ]
    )


def test_a_state_probe_that_fails_or_is_missing_is_reported_not_compared(project):
    describe(project, CLEAN_STATE + "  stored_faults: {probe: true}\n  fault_log: {probe: true}\n")
    project.makeconftest(
        STAGING
        + """

def pytest_benchtare_state(bench, config):
    raise RuntimeError("the target didn't answer over SSH")
"""
    )
    project.makepyfile(test_bench=CLEAN_TEST)
    result = project.runpytest(*CLI, "--check-hygiene", "-v")
    assert result.ret == pytest.ExitCode.OK  # what could be compared was clean
    result.stdout.fnmatch_lines(
        [
            (
                "    it leaves the DUT clean: passed, Mode, Setpoint, supply_enable clean after "
                "teardown; not counted: stored_faults: the state probe failed: RuntimeError: the "
                "target didn't answer over SSH; fault_log: the state probe failed: *"
            ),
        ]
    )


def test_a_probe_no_hook_returns_says_so(project):
    describe(project, CLEAN_STATE + "  stored_faults: {probe: true}\n")
    project.makepyfile(test_bench=CLEAN_TEST)
    result = project.runpytest(*CLI, "--check-hygiene", "-v")
    result.stdout.fnmatch_lines(
        ["*not counted: stored_faults: no pytest_benchtare_state hook returned it"]
    )


# --- it repeats, and starts from a meaningful state ---------------------------------------------


def test_a_test_that_doesnt_repeat_is_not_clean(project):
    project.makepyfile(
        test_bench="""
        import pytest

        RUNS = []


        def test_only_once(running, bench, signals):
            RUNS.append(1)
            bench.write(signals.Setpoint, 42.0)
            bench.wait_until(signals.Measured, pytest.approx(42.0, abs=0.5), timeout=1.0)
            assert len(RUNS) == 1, "only passes the first time"
        """
    )
    result = project.runpytest(*CLI, "--check-hygiene")
    result.stdout.fnmatch_lines(
        [
            "test_bench.py::test_only_once: NOT CLEAN",
            (
                "    it repeats: failed, it failed when it ran again straight after itself, at "
                "test_bench.py:10: AssertionError: only passes the first time*"
            ),
        ]
    )


def test_a_test_that_takes_different_readings_when_repeated_is_not_clean(project):
    project.makepyfile(
        test_bench="""
        import pytest

        RUNS = []


        def test_wanders(running, bench, signals):
            RUNS.append(1)
            bench.write(signals.Setpoint, 42.0)
            if len(RUNS) == 2:
                bench.wait_until(signals.Mode, "run", timeout=1.0)
            bench.wait_until(signals.Measured, pytest.approx(42.0, abs=0.5), timeout=1.0)
        """
    )
    result = project.runpytest(*CLI, "--check-hygiene")
    result.stdout.fnmatch_lines(
        [
            "test_bench.py::test_wanders: NOT CLEAN",
            (
                "    it repeats: failed, run again straight after itself, it read Mode, Measured "
                "instead of Measured"
            ),
        ]
    )


def test_a_wait_already_satisfied_before_its_write_is_not_a_meaningful_start(project):
    project.makepyfile(
        test_bench="""
        def test_already_there(running, bench, signals):
            bench.write(signals.ModeRequest, "run")
            bench.wait_until(signals.Mode, "run", timeout=1.0)
        """
    )
    result = project.runpytest(*CLI, "--check-hygiene")
    result.stdout.fnmatch_lines(
        [
            "test_bench.py::test_already_there: NOT CLEAN",
            (
                "    it starts from a meaningful state: failed, its wait for Mode at "
                "test_bench.py:3 was already satisfied before the write of ModeRequest at "
                "test_bench.py:2: Mode was already 'run', so the test can't tell whether the DUT "
                "responded"
            ),
        ]
    )


def test_a_wait_marked_unchecked_isnt_judged(project):
    project.makepyfile(
        test_bench="""
        def test_settles_first(running, bench, signals):
            bench.write(signals.ModeRequest, "run")
            bench.wait_until(signals.Mode, "run", timeout=1.0, check=False)
        """
    )
    result = project.runpytest(*CLI, "--check-hygiene", "-v")
    result.stdout.fnmatch_lines(
        [
            "test_bench.py::test_settles_first: clean",
            "    it starts from a meaningful state: not checked, it has no wait_until in its body",
        ]
    )


def test_a_staging_without_its_own_starting_point_shows_on_the_repeat(project):
    # The first time, Measured starts at 0. Run again straight after itself, it is already 42,
    # because the staging never sets a starting point of its own.
    project.makepyfile(
        test_bench="""
        import pytest


        def test_tracks(mode_only, bench, signals):
            bench.write(signals.Setpoint, 42.0)
            bench.wait_until(signals.Measured, pytest.approx(42.0, abs=0.5), timeout=1.0)
        """
    )
    result = project.runpytest(*CLI, "--check-hygiene")
    result.stdout.fnmatch_lines(
        [
            "test_bench.py::test_tracks: NOT CLEAN",
            (
                "    it starts from a meaningful state: failed, run again straight after itself, "
                "its wait for Measured at test_bench.py:6 was already satisfied before the write "
                "of Setpoint at test_bench.py:5: Measured was already 42.0 degC, so the test "
                "can't tell whether the DUT responded"
            ),
        ]
    )


# --- what isn't checked ----------------------------------------------------------------------------


def test_a_test_that_fails_is_not_checked_or_rerun(project):
    project.makepyfile(
        test_bench="""
        from pathlib import Path


        def test_misses(running, bench, signals):
            with Path("runs.txt").open("a") as runs:
                runs.write("run\\n")
            bench.write(signals.Setpoint, 42.0)
            bench.wait_until(signals.Measured, 99.0, timeout=0.2)


        def test_plain():
            assert True
        """
    )
    result = project.runpytest(*CLI, "--check-hygiene")
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.assert_outcomes(passed=1, failed=1)
    result.stdout.fnmatch_lines(
        [
            "test_bench.py::test_misses: not checked, because it failed",
            "test_bench.py::test_plain: not checked, because it doesn't use the bench",
            "Hygiene check: 2 not checked",
        ]
    )
    assert (project.path / "runs.txt").read_text().splitlines() == ["run"]


# --- stored, and the closing line of a usual run ---------------------------------------------------


def test_the_hygiene_check_is_stored(project):
    project.makepyfile(
        test_bench=CLEAN_TEST
        + """

def test_leaves_the_supply_on(running, bench, signals):
    bench.write(signals.supply_enable, True)
"""
    )
    project.runpytest(*CLI, "--check-hygiene")
    assert query(project, "SELECT kind, exit_code FROM runs") == [("check-hygiene", 1)]
    assert query(project, "SELECT nodeid, hygiene_verdict FROM test_results") == [
        ("test_bench.py::test_tracks", "clean"),
        ("test_bench.py::test_leaves_the_supply_on", "not clean"),
    ]
    assert query(
        project, "SELECT test_seq, rule, outcome FROM hygiene_checks ORDER BY test_seq, rule"
    ) == [
        (1, "clean", "passed"),
        (1, "meaningful start", "passed"),
        (1, "repeats", "passed"),
        (2, "clean", "failed"),
        (2, "meaningful start", "not checked"),
        (2, "repeats", "passed"),
    ]
    items = query(
        project,
        "SELECT run, before, after, outcome FROM hygiene_items "
        "WHERE test_seq = 2 AND name = 'supply_enable' ORDER BY run",
    )
    assert [(run, json.loads(b), json.loads(a), o) for run, b, a, o in items] == [
        (1, False, True, "changed"),
        (2, True, True, "left by an earlier test"),
    ]
    phases = query(
        project, "SELECT DISTINCT phase FROM record_entries WHERE test_seq = 1 ORDER BY phase"
    )
    assert [p for (p,) in phases] == [
        "call",
        "repeat call",
        "repeat setup",
        "repeat teardown",
        "setup",
        "teardown",
    ]


def test_a_usual_run_says_which_test_files_need_a_hygiene_check(project):
    project.makepyfile(test_bench=CLEAN_TEST)
    result = project.runpytest(*CLI)
    result.stdout.fnmatch_lines(
        [
            "benchtare test check: never checked: test_bench.py. Run *",
            (
                "benchtare hygiene check: never checked: test_bench.py. Run benchtare "
                "check-hygiene to check them."
            ),
        ]
    )
    project.runpytest(*CLI, "--check-hygiene")
    result = project.runpytest(*CLI)
    result.stdout.fnmatch_lines(
        ["benchtare hygiene check: every test file was checked, and none has changed since"]
    )


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (
            ["--check-hygiene", "--check-tests"],
            (
                "*--check-tests and --check-hygiene can't be used together: each check runs on its "
                "own*"
            ),
        ),
        (["--check-hygiene"], "*There's nothing to check: BenchTare isn't configured.*"),
    ],
    ids=["with-check-tests", "not-configured"],
)
def test_check_hygiene_runs_on_its_own_on_a_configured_bench(pytester, args, message):
    if "--check-tests" in args:
        for name in FILES:
            shutil.copy(reference.path(name), pytester.path / name)
        args = [*CLI, *args]
    result = pytester.runpytest(*args)
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines([message])
