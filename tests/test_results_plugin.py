"""Runs stored by the pytest plugin, read back from the results database."""

import shutil
import sqlite3
import textwrap

import pytest

from benchtare import reference
from benchtare.results import schema
from benchtare.results.schema import write_failed
from benchtare.results.store import SQLiteStore

FILES = [reference.DUT, reference.DBC, reference.BENCH, reference.HARNESS_CAN, reference.HARNESS_IO]
CLI = [f"--dut={reference.DUT}", f"--bench={reference.BENCH}", f"--harness={reference.HARNESS_IO}"]

STAGING = """
import pytest


@pytest.fixture
def running(bench, signals):
    bench.read(signals.Mode, timeout=2.0)
    bench.write(signals.ModeRequest, "run")
    bench.wait_until(signals.Mode, "run", timeout=1.0)
"""

TESTS = """
import pytest


def test_tracks(running, bench, signals):
    bench.write(signals.Setpoint, 42.0)
    bench.wait_until(signals.Measured, pytest.approx(42.0, abs=0.5), timeout=1.0)


def test_misses(running, bench, signals):
    bench.write(signals.Setpoint, 42.0)
    bench.wait_until(signals.Measured, pytest.approx(99.0, abs=0.5), timeout=0.3)
"""


@pytest.fixture
def project(pytester):
    for name in FILES:
        shutil.copy(reference.path(name), pytester.path / name)
    pytester.makeconftest(STAGING)
    pytester.makepyfile(test_bench=TESTS)
    return pytester


def query(project, sql, path=".benchtare/results.sqlite3"):
    db = sqlite3.connect(project.path / path)
    try:
        return db.execute(sql).fetchall()
    finally:
        db.close()


# --- a run is stored --------------------------------------------------------------------------------


def test_a_run_is_stored_and_named_in_the_header(project):
    result = project.runpytest(*CLI)
    result.assert_outcomes(passed=1, failed=1)
    ((run_id, kind, status, exit_code, counts),) = query(
        project,
        "SELECT id, kind, status, exit_code, "
        "tests_passed || '/' || tests_failed || '/' || tests_errors || '/' || tests_skipped "
        "FROM runs",
    )
    assert (kind, status, exit_code, counts) == ("tests", "finished", 1, "1/1/0/0")
    result.stdout.fnmatch_lines([f"benchtare results: .benchtare/results.sqlite3, run {run_id}"])
    ((dut, bench, harness, simulated, level),) = query(
        project, "SELECT dut, bench, harness, simulated_dut, record_level FROM runs"
    )
    assert (dut, bench, harness, simulated, level) == (
        "reference",
        "laptop-sim",
        "reference-io",
        "reference",
        "stimuli",
    )


def test_each_test_is_stored_with_its_outcome(project):
    project.runpytest(*CLI)
    rows = query(
        project,
        "SELECT seq, nodeid, file, outcome, message IS NOT NULL, details IS NOT NULL, "
        "call_duration > 0 FROM test_results ORDER BY seq",
    )
    assert rows == [
        (1, "test_bench.py::test_tracks", "test_bench.py", "passed", 0, 0, 1),
        (2, "test_bench.py::test_misses", "test_bench.py", "failed", 1, 1, 1),
    ]
    ((message,),) = query(project, "SELECT message FROM test_results WHERE outcome = 'failed'")
    assert message.startswith("benchtare.backends.base.ReadTimeout: Measured did not reach 99.0")


def test_outcomes_follow_pytests_summary(project):
    project.makepyfile(
        test_bench="""
        import pytest

        @pytest.fixture
        def broken():
            raise RuntimeError("staging failed")

        @pytest.fixture
        def bad_teardown():
            yield
            raise RuntimeError("teardown failed")

        def test_error(broken):
            pass

        def test_skipped():
            pytest.skip("not on this bench")

        @pytest.mark.xfail(reason="known")
        def test_xfailed():
            assert False

        @pytest.mark.xfail(reason="known")
        def test_xpassed():
            pass

        def test_teardown_error(bad_teardown):
            pass
        """
    )
    project.runpytest(*CLI)
    rows = dict(query(project, "SELECT nodeid, outcome FROM test_results"))
    assert {nodeid.split("::")[1]: outcome for nodeid, outcome in rows.items()} == {
        "test_error": "error",
        "test_skipped": "skipped",
        "test_xfailed": "xfailed",
        "test_xpassed": "xpassed",
        "test_teardown_error": "error",
    }
    assert query(project, "SELECT tests_passed, tests_errors, tests_skipped FROM runs") == [
        (1, 2, 2)
    ]
    ((message,),) = query(
        project, "SELECT message FROM test_results WHERE nodeid LIKE '%test_skipped'"
    )
    assert message == "Skipped: not on this bench"


def test_record_properties_are_stored(project):
    project.makepyfile(
        test_bench="""
        def test_noted(record_property):
            record_property("operator", "first shift")
        """
    )
    project.runpytest(*CLI)
    assert query(project, "SELECT properties FROM test_results") == [
        ('{"operator": "first shift"}',)
    ]


# --- the manifest, environment and equipment ---------------------------------------------------


def test_the_manifest_lists_every_file_with_its_role(project):
    project.makeini("[pytest]\n")
    project.runpytest(*CLI)
    rows = dict(query(project, "SELECT path, role FROM manifest_files"))
    assert rows == {
        reference.DUT: "interface",
        reference.DBC: "dbc",
        reference.BENCH: "bench",
        reference.HARNESS_IO: "harness",
        "tox.ini": "ini",
        "conftest.py": "conftest",
        "test_bench.py": "test",
    }


def test_two_runs_have_their_own_manifests_and_a_changed_test_shows(project):
    project.runpytest(*CLI)
    project.makepyfile(test_bench=TESTS + "\n# changed\n")
    project.runpytest(*CLI)
    runs = query(project, "SELECT id FROM runs ORDER BY started_at")
    assert len(runs) == 2
    hashes = query(
        project,
        "SELECT r.started_at, m.sha256 FROM manifest_files m JOIN runs r ON r.id = m.run_id "
        "WHERE m.path = 'test_bench.py' ORDER BY r.started_at",
    )
    assert len(hashes) == 2 and hashes[0][1] != hashes[1][1]
    unchanged = query(
        project,
        "SELECT count(DISTINCT sha256) FROM manifest_files WHERE path = 'conftest.py'",
    )
    assert unchanged == [(1,)]


def test_the_environment_and_packages_are_stored(project):
    project.runpytest(*CLI)
    ((os_name, python, host),) = query(
        project, "SELECT os, python_version, host FROM run_environment"
    )
    assert os_name and python and host
    assert query(project, "SELECT count(*) > 0 FROM run_packages WHERE name = 'pytest'") == [(1,)]


def test_the_equipment_is_stored(project):
    project.runpytest(*CLI)
    rows = query(project, "SELECT equipment, backend, kind, simulated, driver FROM run_equipment")
    assert sorted(rows) == [
        ("can0", "sim_can", "bus", 1, "python-can virtual bus"),
        ("io", "sim", "channels", 1, "benchtare sim"),
    ]


# --- the self-test and the test record --------------------------------------------------------------


def test_the_self_test_and_whatever_ran_before_the_first_test_are_stored(project):
    project.runpytest(*CLI)
    assert query(project, "SELECT passed FROM selftests") == [(1,)]
    checks = query(project, "SELECT seq, name, outcome FROM selftest_checks ORDER BY seq")
    assert checks[3] == (4, "loopback io.out4 -> io.in4", "passed")
    pretest = query(
        project, "SELECT kind, signal FROM record_entries WHERE phase = 'pretest' ORDER BY seq"
    )
    assert pretest == [("write", "io.out4"), ("write", "io.out4"), ("identify", "SwVersion")]


def test_each_tests_record_is_stored_with_it(project):
    project.runpytest(*CLI)
    rows = query(
        project,
        "SELECT t.nodeid, e.phase, e.kind, e.signal FROM record_entries e "
        "JOIN test_results t ON t.run_id = e.run_id AND t.seq = e.test_seq "
        "WHERE t.nodeid LIKE '%test_tracks' ORDER BY e.seq",
    )
    assert [row[1:] for row in rows] == [
        ("setup", "read", "Mode"),
        ("setup", "write", "ModeRequest"),
        ("setup", "wait_until", "Mode"),
        ("setup", "result", None),
        ("call", "write", "Setpoint"),
        ("call", "wait_until", "Measured"),
        ("call", "result", None),
        ("teardown", "result", None),
    ]


def test_readings_can_be_queried_across_runs(project):
    project.runpytest(*CLI)
    project.runpytest(*CLI)
    readings = query(
        project,
        "SELECT value_num, unit FROM record_entries "
        "WHERE signal = 'Measured' AND kind = 'wait_until' ORDER BY at",
    )
    assert len(readings) == 4  # two tests, two runs
    assert (42.0, "degC") in readings


def test_a_failed_self_test_stores_a_stopped_run_with_no_tests(project):
    project.makeconftest(
        STAGING
        + textwrap.dedent(
            """
            from benchtare.sim.simulation import Simulation

            _start = Simulation.start

            def start(self):
                _start(self)
                self.cut("io.out4")

            Simulation.start = start
            """
        )
    )
    result = project.runpytest_subprocess(*CLI)
    assert result.ret == pytest.ExitCode.INTERRUPTED
    assert query(project, "SELECT status, exit_code FROM runs") == [("stopped", 2)]
    assert query(project, "SELECT passed FROM selftests") == [(0,)]
    assert query(project, "SELECT count(*) FROM test_results") == [(0,)]


def test_a_run_that_dies_stays_running_with_the_tests_that_finished(project):
    project.makepyfile(
        test_bench="""
        import os

        def test_first(bench):
            pass

        def test_dies():
            os._exit(3)
        """
    )
    project.runpytest_subprocess(*CLI)
    assert query(project, "SELECT status, finished_at FROM runs") == [("running", None)]
    assert query(project, "SELECT nodeid, outcome FROM test_results") == [
        ("test_bench.py::test_first", "passed")
    ]


def test_a_selftest_only_run_is_stored_as_such(project):
    result = project.runpytest(*CLI, "--selftest-only")
    assert result.ret == 0
    assert query(project, "SELECT kind, status, exit_code FROM runs") == [
        ("selftest", "finished", 0)
    ]
    assert query(project, "SELECT passed FROM selftests") == [(1,)]


# --- where results go ------------------------------------------------------------------------------


def test_no_results_stores_nothing(project):
    result = project.runpytest(*CLI, "--no-results")
    result.assert_outcomes(passed=1, failed=1)
    result.stdout.fnmatch_lines(["benchtare results: not stored"])
    assert not (project.path / ".benchtare").exists()


def test_collecting_only_stores_nothing(project):
    project.runpytest(*CLI, "--collect-only")
    assert not (project.path / ".benchtare").exists()


def test_results_path_on_the_command_line_and_in_the_ini_file(project):
    project.runpytest(*CLI, "--results=elsewhere/runs.sqlite3")
    assert query(project, "SELECT count(*) FROM runs", "elsewhere/runs.sqlite3") == [(1,)]
    project.makeini("[pytest]\nbenchtare_results = ini-results.sqlite3\n")
    project.runpytest(*CLI)
    assert query(project, "SELECT count(*) FROM runs", "ini-results.sqlite3") == [(1,)]


def test_relative_paths_follow_where_pytest_runs_and_where_the_ini_file_is(project, monkeypatch):
    project.makeini("[pytest]\n")
    here = project.mkdir("sub")
    monkeypatch.chdir(here)  # pytest is run from a folder below the project's rootdir
    absolute = [
        f"--{option}={project.path / name}"
        for option, name in zip(
            ("dut", "bench", "harness"), (reference.DUT, reference.BENCH, reference.HARNESS_IO)
        )
    ]
    tests = str(project.path / "test_bench.py")
    project.runpytest(*absolute, tests)
    assert (project.path / ".benchtare" / "results.sqlite3").is_file()  # under the rootdir
    project.runpytest(*absolute, "--results=runs.sqlite3", tests)
    assert (here / "runs.sqlite3").is_file()  # where pytest was run
    project.makeini("[pytest]\nbenchtare_results = stored/runs.sqlite3\n")
    project.runpytest(*absolute, tests)
    assert (project.path / "stored" / "runs.sqlite3").is_file()  # beside the ini file
    assert sorted(p.name for p in here.iterdir() if not p.name.startswith(".")) == ["runs.sqlite3"]
    assert not (here / ".benchtare").exists()


def test_results_can_go_to_an_absolute_path_or_the_home_folder(
    project, tmp_path_factory, monkeypatch
):
    elsewhere = tmp_path_factory.mktemp("shared drive") / "bench 1" / "results.sqlite3"
    project.runpytest(*CLI, f"--results={elsewhere}")
    assert query(project, "SELECT count(*) FROM runs", elsewhere) == [(1,)]
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))  # where ~ is on Linux and macOS
    monkeypatch.setenv("USERPROFILE", str(home))  # and on Windows
    project.runpytest(*CLI, "--results=~/bench-results/runs.sqlite3")
    stored = home / "bench-results" / "runs.sqlite3"
    assert query(project, "SELECT count(*) FROM runs", stored) == [(1,)]
    assert not (project.path / ".benchtare").exists()


def test_later_runs_leave_every_row_of_earlier_runs_as_it_was(project, database_rows):
    project.runpytest(*CLI)
    path = project.path / ".benchtare" / "results.sqlite3"
    before = database_rows(path)
    (first_run,) = {row[0] for row in before["runs"]}
    project.makepyfile(test_bench=TESTS + "\n# changed\n")
    project.runpytest(*CLI, "--identify=hardware_revision=C")
    project.runpytest(*CLI, "--selftest-only")
    after = database_rows(path)
    for table, rows in before.items():
        assert not rows - after[table], f"rows of {table} were changed or removed"
    assert len(after["runs"]) == 3
    added = {table: rows - before[table] for table, rows in after.items() if table != "meta"}
    assert all(first_run not in {row[0] for row in rows} for rows in added.values())
    assert not after["meta"] - before["meta"]


def test_a_newer_results_database_stops_the_run_before_it_starts(project):
    project.runpytest(*CLI)
    db = sqlite3.connect(project.path / ".benchtare" / "results.sqlite3")
    db.execute("PRAGMA user_version = 99")
    db.close()
    result = project.runpytest(*CLI)
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(
        [
            "*BenchTare can't store results.*",
            "*is schema version 99, written by a newer BenchTare*",
            (
                "*Check: upgrade BenchTare, or point --results at another file, or run with "
                "--no-results*"
            ),
        ]
    )


# --- results that can't be stored -------------------------------------------------------------------


def disk_full(store, *args, **kwargs):
    """A store write that fails as it would on a full disk."""
    raise write_failed(store.path, sqlite3.OperationalError("database or disk is full"))


def test_a_database_held_during_the_run_stops_it_with_a_clear_message(project):
    project.makeconftest(
        STAGING + "\nfrom benchtare.results import schema\n\nschema.BUSY_TIMEOUT_MS = 200\n"
    )
    project.makepyfile(
        test_bench="""
        import sqlite3

        HELD = []


        def test_first(bench):
            pass


        def test_holds_the_database(bench):
            db = sqlite3.connect(".benchtare/results.sqlite3", isolation_level=None)
            db.execute("BEGIN IMMEDIATE")  # a database browser with unsaved changes, say
            HELD.append(db)


        def test_never_runs(bench):
            pass
        """
    )
    result = project.runpytest_subprocess(*CLI)
    assert result.ret == pytest.ExitCode.INTERRUPTED
    result.assert_outcomes(passed=2)
    result.stdout.fnmatch_lines(
        [
            "*= BenchTare couldn't store results, so the run stopped =*",
            (
                "Storing test_bench.py::test_holds_the_database failed: results database "
                "*results.sqlite3 is busy: another program held it for more than 0.2 s"
            ),
            (
                "  Check: close whatever holds it, such as a database browser with unsaved "
                "changes or another BenchTare run upgrading it, and run again; or point "
                "--results at another file, or run with --no-results"
            ),
            "Run *: 1 test stored before this. It stays marked running.",
            "*BenchTare couldn't store results, so the run stopped. What failed is listed above*",
        ]
    )
    result.stdout.no_fnmatch_line("*INTERNALERROR*")
    result.stdout.no_fnmatch_line("*PluggyTeardownRaisedWarning*")
    assert query(project, "SELECT status FROM runs") == [("running",)]
    assert query(project, "SELECT nodeid FROM test_results") == [("test_bench.py::test_first",)]


def test_a_database_held_when_the_run_starts_stops_it_before_any_test(project, monkeypatch):
    project.runpytest(*CLI)
    monkeypatch.setattr(schema, "BUSY_TIMEOUT_MS", 100)
    other = sqlite3.connect(project.path / ".benchtare" / "results.sqlite3", isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    try:
        result = project.runpytest(*CLI)
    finally:
        other.execute("ROLLBACK")
        other.close()
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(
        [
            "*BenchTare can't store results.*",
            "*results database *results.sqlite3 is busy: another program held it for more than 0.1 s",
        ]
    )
    assert query(project, "SELECT count(*) FROM runs") == [(1,)]


def test_a_failure_while_the_bench_opens_is_about_results_not_the_bench(project, monkeypatch):
    monkeypatch.setattr(SQLiteStore, "add_equipment", disk_full)
    result = project.runpytest(*CLI)
    assert result.ret == pytest.ExitCode.INTERRUPTED
    result.stdout.fnmatch_lines(
        [
            "*= BenchTare couldn't store results, so the run stopped =*",
            (
                "Storing the bench equipment failed: results database *results.sqlite3 couldn't "
                "be written: database or disk is full"
            ),
            (
                "  Check: that the disk isn't full and the file and its folder can be written; "
                "or point --results at another file, or run with --no-results"
            ),
            "Run *: 0 tests stored before this. It stays marked running.",
        ]
    )
    result.stdout.no_fnmatch_line("*couldn't open the bench*")
    result.stdout.no_fnmatch_line("*self-test passed*")  # it stopped there, before the self-test
    assert query(project, "SELECT status FROM runs") == [("running",)]
    assert query(project, "SELECT count(*) FROM test_results") == [(0,)]


def test_results_that_cant_be_stored_at_the_end_fail_the_run(project, monkeypatch):
    monkeypatch.setattr(SQLiteStore, "finish_run", disk_full)
    result = project.runpytest(*CLI)
    assert result.ret == pytest.ExitCode.INTERRUPTED
    result.assert_outcomes(passed=1, failed=1)  # every test ran
    result.stdout.fnmatch_lines(
        [
            "*= BenchTare couldn't store results =*",
            "Storing the end of the run failed: results database *couldn't be written: database*",
            "  Check: that the disk isn't full*",
            "Run *: 2 tests stored before this. It stays marked running.",
        ]
    )
    assert query(project, "SELECT status FROM runs") == [("running",)]
    assert query(project, "SELECT count(*) FROM test_results") == [(2,)]


def test_a_selftest_whose_results_cant_be_stored_does_not_pass(project, monkeypatch):
    monkeypatch.setattr(SQLiteStore, "add_selftest", disk_full)
    result = project.runpytest(*CLI, "--selftest-only")
    assert result.ret == pytest.ExitCode.INTERRUPTED
    result.stdout.fnmatch_lines(["Storing the self-test failed: results database *"])


# --- DUT identity ----------------------------------------------------------------------------------


def identity_rows(project):
    return query(
        project,
        "SELECT name, seq, expected, value, source FROM run_dut_identity ORDER BY name, seq",
    )


def test_identity_from_a_signal_the_command_line_and_code_is_stored(project):
    project.makeconftest(
        STAGING
        + textwrap.dedent(
            """
            @pytest.fixture(scope="session", autouse=True)
            def identify(bench):
                bench.identify({"bootloader": "1.4.2", "motor.firmware": "2.3.1"})
            """
        )
    )
    result = project.runpytest(*CLI, "--identify", "hardware_revision=C")
    result.stdout.fnmatch_lines(["benchtare DUT identity: hardware_revision C, software 102"])
    assert identity_rows(project) == [
        ("bootloader", 1, 0, "1.4.2", "code"),
        ("hardware_revision", 1, 0, "C", "command-line"),
        ("motor.firmware", 1, 0, "2.3.1", "code"),
        ("software", 1, 1, "102", "signal"),
    ]


def test_a_missing_listed_item_is_warned_about_and_stored(project):
    dut = project.path / reference.DUT
    dut.write_text(dut.read_text() + "  bootloader:\n")
    project.makeconftest(
        STAGING
        + textwrap.dedent(
            """
            @pytest.fixture(scope="session", autouse=True)
            def identify(bench):
                bench.identify("bootloadr", "1.4.2")  # a typo
            """
        )
    )
    result = project.runpytest(*CLI)
    result.stdout.fnmatch_lines(
        [
            (
                "*DutIdentityWarning: the DUT's bootloader was never collected during this run; "
                "it is stored as missing. 'bootloadr' was recorded instead: is that a "
                "misspelling of 'bootloader'?"
            ),
        ]
    )
    assert ("bootloader", 1, 1, None, "missing") in identity_rows(project)


def test_a_change_during_the_run_is_warned_about_and_both_values_stored(project):
    project.makepyfile(
        test_bench="""
        def test_reflash(bench):
            bench.identify("software", "103")
        """
    )
    result = project.runpytest(*CLI)
    result.stdout.fnmatch_lines(
        [
            "test_bench.py::test_reflash",
            (
                "*DutIdentityWarning: during call: the DUT's software changed during the run: it "
                "was '102', and is now '103'*"
            ),
        ]
    )
    assert identity_rows(project) == [
        ("software", 1, 1, "102", "signal"),
        ("software", 2, 1, "103", "code"),
    ]


def test_without_the_bench_the_command_line_identity_is_still_stored(project):
    project.makepyfile(test_bench="def test_names(signals):\n    pass\n")
    result = project.runpytest(*CLI, "--identify", "hardware_revision=C")
    result.assert_outcomes(passed=1)
    assert "DutIdentityWarning" not in result.stdout.str()  # nothing could be collected
    assert identity_rows(project) == [
        ("hardware_revision", 1, 0, "C", "command-line"),
        ("software", 1, 1, None, "missing"),
    ]


@pytest.mark.parametrize(
    ("given", "message"),
    [
        ("hardware_revision", "it needs NAME=VALUE"),
        ("=C", "an identity item needs a name"),
        ("bad name=C", "'bad name' can't name an identity item"),
        ("hardware_revision=", "'hardware_revision' needs a value after the ="),
    ],
)
def test_a_wrong_identify_option_is_a_usage_error(project, given, message):
    result = project.runpytest(*CLI, "--identify", given)
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines([f"*BenchTare: --identify {given}: {message}*"])
