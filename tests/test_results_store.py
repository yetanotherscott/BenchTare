"""The results database: creating, reopening, refusing and migrating it, and how rows are
written. The plugin's side is in test_results_plugin.py."""

import json
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from benchtare.dut.identity import IdentityValue
from benchtare.hygiene import ItemResult, RuleResult
from benchtare.record import RecordEntry, RecordLevel
from benchtare.results import schema
from benchtare.results.manifest import ManifestFile
from benchtare.results.schema import SCHEMA_VERSION, ResultsError, open_database
from benchtare.results.store import RunInfo, SQLiteStore, TestResult, utc
from benchtare.selftest import CheckResult, SelfTestResult
from benchtare.testcheck import ModeResult

TABLES = {
    "meta",
    "runs",
    "run_environment",
    "run_packages",
    "run_equipment",
    "run_dut_identity",
    "manifest_files",
    "selftests",
    "selftest_checks",
    "test_results",
    "record_entries",
    "test_checks",  # version 2
    "hygiene_checks",  # version 3
    "hygiene_items",
}
NEXT = SCHEMA_VERSION + 1  # a stand-in for the next version, to test upgrading in general


def run_info(run_id="run-1", **changes):
    fields = {
        "id": run_id,
        "kind": "tests",
        "started_at": time.time(),
        "record_level": "stimuli",
        "dut": "reference",
        "bench": "laptop-sim",
        "harness": "reference-can",
        "simulated_dut": "reference",
        "benchtare_version": "0.0.1",
        "pytest_version": "9.0",
        "args": ["-v"],
    }
    fields.update(changes)
    return RunInfo(**fields)


@pytest.fixture
def path(tmp_path):
    return tmp_path / ".benchtare" / "results.sqlite3"


@pytest.fixture
def store(path):
    opened = SQLiteStore(path)
    yield opened
    opened.close()  # Windows keeps an open database locked


def tables(db):
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


# --- creating and reopening ----------------------------------------------------------------------


def test_a_new_database_is_created_at_the_current_version(path):
    db = open_database(path)
    assert tables(db) == TABLES
    assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 3
    meta = dict(db.execute("SELECT key, value FROM meta"))
    assert meta["schema_version"] == "3"
    assert meta["created_by"].startswith("benchtare ")
    db.close()


def test_reopening_keeps_what_was_stored(path):
    store = SQLiteStore(path)
    store.start_run(run_info())
    store.close()
    store = SQLiteStore(path)
    assert store.db.execute("SELECT id, status FROM runs").fetchall() == [("run-1", "running")]
    store.close()


@pytest.mark.parametrize(
    "parts",
    [
        ("several", "folders", "that", "don't", "exist", "results.sqlite3"),
        ("bench results ü", "résultats 1.sqlite3"),
    ],
    ids=["new-folders", "spaces-and-accents"],
)
def test_a_database_can_be_in_any_folder_and_its_folders_are_made(tmp_path, parts):
    path = tmp_path.joinpath(*parts)
    store = SQLiteStore(path)
    store.start_run(run_info())
    store.close()
    store = SQLiteStore(path)
    assert store.db.execute("SELECT id FROM runs").fetchall() == [("run-1",)]
    store.close()


def test_a_folder_that_cant_be_made_is_refused_with_its_path(tmp_path):
    blocker = tmp_path / "results"
    blocker.write_text("a file where a folder should be")
    path = blocker / "results.sqlite3"
    with pytest.raises(ResultsError) as info:
        open_database(path)
    assert str(path) in str(info.value)
    assert "Check: point --results at another file, or run with --no-results" in str(info.value)
    assert blocker.read_text() == "a file where a folder should be"


def store_a_whole_run(store, run_id, *, finish=True):
    """A run that writes to every table, as a pytest session does."""
    now = time.time()
    store.start_run(run_info(run_id, git_commit="0123abc", git_dirty=False))
    store.add_environment(
        run_id,
        {
            "host": "bench-1",
            "os": "Linux",
            "python_implementation": "CPython",
            "python_version": "3.12.1",
        },
        [("cantools", "40.0"), ("pytest", "9.0")],
    )
    store.add_manifest_files(run_id, [ManifestFile("test_x.py", "test", "0" * 64, 10)])
    store.add_equipment(
        run_id,
        [
            {
                "equipment": "io",
                "backend": "sim",
                "kind": "channels",
                "simulated": True,
                "driver": "benchtare sim",
                "details": {"channels": 5},
            }
        ],
    )
    check = CheckResult("loopback", "loopback io.out4 -> io.in4", "passed", "read back True")
    store.add_selftest(run_id, SelfTestResult((check,), now, 0.2))
    write = {"signal": "io.out4", "value": True}
    store.add_record(
        run_id, [RecordEntry("write", RecordLevel.STIMULI, "w", write)], phase="pretest"
    )
    store.add_test(run_id, TestResult(1, "t.py::a", "t.py", "failed", now, message="assert 1 == 2"))
    read = {"signal": "Measured", "value": 42.0, "unit": "degC"}
    store.add_record(
        run_id, [RecordEntry("read", RecordLevel.RESULTS, "r", read)], phase="call", test_seq=1
    )
    store.add_identity(run_id, [IdentityValue("software", "102", "signal", True, now)])
    shifted = {**read, "value": 84.0, "corruption": "shift", "received": 42.0}
    store.add_checks(
        run_id,
        1,
        f"verdict of {run_id}",  # different in each run, so a write to another run shows
        [
            ModeResult(0, "", "", "control", "passed", "passed again unchanged"),
            ModeResult(1, "Measured", "t.py:5", "shift", "failed", "failed at t.py:5"),
        ],
        {"reading 1 shift": {"call": [RecordEntry("read", RecordLevel.RESULTS, "r", shifted)]}},
    )
    store.add_hygiene(
        run_id,
        1,
        f"hygiene of {run_id}",  # different in each run, so a write to another run shows
        [RuleResult("clean", "failed", "Mode was still 'run' after teardown")],
        [ItemResult(1, "Mode", "output", "standby", "run", "changed", "Mode was still 'run'")],
        {"repeat": {"call": [RecordEntry("read", RecordLevel.RESULTS, "r", read)]}},
    )
    if finish:
        store.finish_run(run_id, status="finished", exit_code=1, counts={"failed": 1})


def test_later_runs_add_rows_and_leave_every_earlier_row_as_it_was(path, database_rows):
    store = SQLiteStore(path)
    store_a_whole_run(store, "run-1")
    store.close()
    before = database_rows(path)
    assert all(before.values()), "every table should hold a row of run-1"
    for run_id, finish in (("run-2", True), ("run-3", False)):
        store = SQLiteStore(path)  # each run opens the file afresh, as each session does
        store_a_whole_run(store, run_id, finish=finish)
        store.close()
    after = database_rows(path)
    for table, rows in before.items():
        assert not rows - after[table], f"rows of {table} were changed or removed"
    for table, rows in after.items():
        added = rows - before[table]
        if table == "meta":
            assert not added
        else:  # the run's ID is the first column of every other table
            assert {row[0] for row in added} == {"run-2", "run-3"}, table


def test_a_run_id_already_stored_is_refused_not_overwritten(store):
    store.start_run(run_info("run-1", dut="first"))
    with pytest.raises(ResultsError, match="UNIQUE constraint failed"):
        store.start_run(run_info("run-1", dut="second"))
    assert store.db.execute("SELECT dut FROM runs").fetchall() == [("first",)]


# --- the test check -----------------------------------------------------------------------------


def test_a_tests_check_is_stored_with_its_verdict_steps_and_reruns(store):
    store.start_run(run_info(kind="check-tests"))
    store.add_test("run-1", TestResult(1, "t.py::a", "t.py", "passed", time.time()))
    store.add_record("run-1", [RecordEntry("result", RecordLevel.RESULTS, "ok", {})], phase="call")
    shifted = {"signal": "Measured", "value": 84.0, "corruption": "shift", "received": 42.0}
    store.add_checks(
        "run-1",
        1,
        "unable to fail",
        [
            ModeResult(0, "", "", "control", "passed", "passed again unchanged"),
            ModeResult(1, "Measured", "t.py:5", "shift", "passed", "reading Measured 84.0"),
            ModeResult(1, "Measured", "t.py:5", "stale", "not applied", "it was already 42.0"),
        ],
        {"reading 1 shift": {"call": [RecordEntry("read", RecordLevel.RESULTS, "r", shifted)]}},
    )
    db = store.db
    assert db.execute("SELECT check_verdict FROM test_results").fetchall() == [("unable to fail",)]
    assert db.execute(
        "SELECT reading, signal, location, mode, outcome FROM test_checks ORDER BY reading, mode"
    ).fetchall() == [
        (0, None, None, "control", "passed"),
        (1, "Measured", "t.py:5", "shift", "passed"),
        (1, "Measured", "t.py:5", "stale", "not applied"),
    ]
    rows = db.execute("SELECT seq, phase, value_num FROM record_entries ORDER BY seq")
    assert rows.fetchall() == [(1, "call", None), (2, "reading 1 shift call", 84.0)]


def checked_file(store, run_id, kind, sha, *, dut="reference", finish=True):
    store.start_run(run_info(run_id, kind=kind, dut=dut))
    store.add_manifest_files(run_id, [ManifestFile("t.py", "test", sha, 1)])
    if finish:
        store.finish_run(run_id, status="finished", exit_code=0, counts={})


def test_each_test_file_says_when_it_was_last_checked(store):
    checked_file(store, "plain", "tests", "aa")
    assert store.check_status("plain") == [("t.py", None, None)]  # never checked
    checked_file(store, "check", "check-tests", "aa")
    checked_file(store, "same", "tests", "aa")
    checked_file(store, "changed", "tests", "bb")
    checked_file(store, "other-dut", "tests", "aa", dut="another")
    ((checked_at,),) = store.db.execute("SELECT started_at FROM runs WHERE id = 'check'")
    assert store.check_status("same") == [("t.py", checked_at, True)]
    assert store.check_status("changed") == [("t.py", checked_at, False)]
    assert store.check_status("other-dut") == [("t.py", None, None)]


def test_each_check_keeps_its_own_last_check(store):
    checked_file(store, "check", "check-tests", "aa")
    checked_file(store, "hygiene", "check-hygiene", "aa")
    checked_file(store, "plain", "tests", "aa")
    times = dict(store.db.execute("SELECT id, started_at FROM runs"))
    assert store.check_status("plain") == [("t.py", times["check"], True)]
    assert store.check_status("plain", "check-hygiene") == [("t.py", times["hygiene"], True)]


def test_a_tests_hygiene_is_stored_with_its_rules_items_and_repeat(store):
    from benchtare.hygiene import MISSING

    store.start_run(run_info(kind="check-hygiene"))
    store.add_test("run-1", TestResult(1, "t.py::a", "t.py", "passed", time.time()))
    store.add_hygiene(
        "run-1",
        1,
        "not clean",
        [
            RuleResult("repeats", "passed", "it passed again"),
            RuleResult("clean", "failed", "Mode was still 'run'"),
            RuleResult("meaningful start", "not checked", "it has no wait_until"),
        ],
        [
            ItemResult(1, "Mode", "output", "standby", "run", "changed", "Mode was still 'run'"),
            ItemResult(1, "Setpoint", "input", MISSING, 0.0, "not compared", "never written"),
        ],
        {"repeat": {"call": [RecordEntry("read", RecordLevel.RESULTS, "r", {"value": 1})]}},
    )
    db = store.db
    assert db.execute("SELECT hygiene_verdict FROM test_results").fetchall() == [("not clean",)]
    assert db.execute("SELECT rule, outcome FROM hygiene_checks ORDER BY rule").fetchall() == [
        ("clean", "failed"),
        ("meaningful start", "not checked"),
        ("repeats", "passed"),
    ]
    assert db.execute(
        "SELECT name, kind, before, after, outcome FROM hygiene_items ORDER BY name"
    ).fetchall() == [
        ("Mode", "output", '"standby"', '"run"', "changed"),
        ("Setpoint", "input", None, "0.0", "not compared"),  # nothing known before: null
    ]
    assert db.execute("SELECT phase FROM record_entries").fetchall() == [("repeat call",)]


def test_a_check_that_never_finished_doesnt_count(store):
    checked_file(store, "check", "check-tests", "aa", finish=False)
    checked_file(store, "after", "tests", "aa")
    assert store.check_status("after") == [("t.py", None, None)]


# --- refusing what can't be used -------------------------------------------------------------------


def text_file(path):
    path.write_text("not a database")


def sqlite_file_of_another_program(path):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE invoices (id INTEGER)")
    db.commit()
    db.close()


def newer_results_database(path):
    open_database(path).close()
    db = sqlite3.connect(path)
    db.execute("PRAGMA user_version = 99")
    db.close()


@pytest.mark.parametrize(
    "make", [text_file, sqlite_file_of_another_program, newer_results_database]
)
def test_a_refused_file_is_left_exactly_as_it_was(path, make):
    path.parent.mkdir()
    make(path)
    before = path.read_bytes()
    with pytest.raises(ResultsError):
        open_database(path)
    assert path.read_bytes() == before
    assert [p.name for p in path.parent.iterdir()] == [path.name]  # no journal or WAL left


def test_a_newer_database_is_refused_and_left_alone(path):
    open_database(path).close()
    db = sqlite3.connect(path)
    db.execute("PRAGMA user_version = 99")
    db.close()
    before = path.read_bytes()
    with pytest.raises(ResultsError) as info:
        SQLiteStore(path)
    message = str(info.value)
    assert "is schema version 99, written by a newer BenchTare" in message
    assert f"reads up to version {SCHEMA_VERSION}, so it won't write to it" in message
    assert "Check: upgrade BenchTare, or point --results at another file" in message
    assert path.read_bytes() == before


def test_a_file_that_is_not_sqlite_is_refused(path):
    path.parent.mkdir()
    path.write_text("not a database")
    with pytest.raises(
        ResultsError, match="isn't a BenchTare results database: it isn't an SQLite"
    ):
        open_database(path)


def test_an_sqlite_file_with_other_tables_is_refused(path):
    path.parent.mkdir()
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE invoices (id INTEGER)")
    db.close()
    with pytest.raises(ResultsError, match=r"it is an SQLite file with other tables \(invoices\)"):
        open_database(path)


def test_a_folder_is_refused(path):
    path.mkdir(parents=True)
    with pytest.raises(ResultsError, match="is a folder, not a file"):
        open_database(path)


# --- migrating -------------------------------------------------------------------------------------


def add_notes_table(db):
    db.execute("CREATE TABLE notes (run_id TEXT, note TEXT)")


def upgrade_to_next(monkeypatch, step):
    """Make this BenchTare one version newer, with ``step`` as its upgrade."""
    monkeypatch.setattr(schema, "SCHEMA_VERSION", NEXT)
    monkeypatch.setattr(schema, "MIGRATIONS", {**schema.MIGRATIONS, SCHEMA_VERSION: step})


def test_an_older_database_is_backed_up_and_migrated(path, monkeypatch):
    store = SQLiteStore(path)
    store.start_run(run_info())
    store.close()
    upgrade_to_next(monkeypatch, add_notes_table)
    db = schema.open_database(path)
    assert "notes" in tables(db)
    assert db.execute("PRAGMA user_version").fetchone()[0] == NEXT
    assert dict(db.execute("SELECT key, value FROM meta"))["schema_version"] == str(NEXT)
    assert db.execute("SELECT id FROM runs").fetchall() == [("run-1",)]  # data kept
    db.close()
    backup = sqlite3.connect(path.with_name(f"results.sqlite3.v{SCHEMA_VERSION}.bak"))
    assert backup.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert backup.execute("SELECT id FROM runs").fetchall() == [("run-1",)]
    backup.close()


def version_1_database(path, monkeypatch):
    """A results database as schema version 1 left it, with a run in it."""
    with monkeypatch.context() as old:
        old.setattr(schema, "SCHEMA_VERSION", 1)
        old.setattr(schema, "MIGRATIONS", {})
        db = schema.open_database(path)
        db.execute(
            "INSERT INTO runs (id, kind, status, started_at, schema_version, record_level, dut, "
            "bench, harness, benchtare_version, pytest_version, args) VALUES ('old', 'tests', "
            "'finished', '2026-11-01T00:00:00.000000Z', 1, 'stimuli', 'reference', 'b', 'h', "
            "'0.0.1', '9.2', '[]')"
        )
        db.execute(
            "INSERT INTO test_results (run_id, seq, nodeid, file, outcome, started_at) VALUES "
            "('old', 1, 't.py::a', 't.py', 'passed', '2026-11-01T00:00:00.000000Z')"
        )
        db.execute(
            "INSERT INTO manifest_files (run_id, path, role, sha256, size) VALUES "
            "('old', 't.py', 'test', 'aa', 2)"
        )
        db.close()


def test_a_version_1_database_is_upgraded_to_the_current_version_keeping_its_runs(
    path, monkeypatch
):
    version_1_database(path, monkeypatch)
    before = sqlite3.connect(path)
    assert schema.schema_version(before) == 1
    assert "test_checks" not in tables(before)
    before.close()
    store = SQLiteStore(path)
    assert schema.schema_version(store.db) == 3
    assert {"test_checks", "hygiene_checks", "hygiene_items"} <= tables(store.db)
    assert store.db.execute(
        "SELECT nodeid, outcome, check_verdict, hygiene_verdict FROM test_results"
    ).fetchall() == [("t.py::a", "passed", None, None)]
    assert store.db.execute(
        "SELECT path, last_checked_at, unchanged_since_check, last_hygiene_at, "
        "unchanged_since_hygiene FROM manifest_files"
    ).fetchall() == [("t.py", None, None, None, None)]
    store.start_run(run_info("new"))  # and a new run can be stored in it
    store.close()
    backup = sqlite3.connect(path.with_name("results.sqlite3.v1.bak"))
    assert schema.schema_version(backup) == 1
    backup.close()


def test_a_failed_migration_leaves_the_database_unchanged(path, monkeypatch):
    open_database(path).close()

    def broken(db):
        db.execute("CREATE TABLE notes (note TEXT)")
        raise RuntimeError("disk on fire")

    upgrade_to_next(monkeypatch, broken)
    with pytest.raises(ResultsError) as info:
        schema.open_database(path)
    assert "couldn't upgrade results database" in str(info.value)
    assert "RuntimeError: disk on fire. It is unchanged, and a copy is in" in str(info.value)
    db = sqlite3.connect(path)
    assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert "notes" not in tables(db)
    db.close()


def ours_before_switching_to_wal(path):
    """A results database just created by another run, before that run switched it to WAL."""
    open_database(path).close()
    db = sqlite3.connect(path, isolation_level=None)
    db.execute("PRAGMA journal_mode = DELETE")
    db.close()


def test_opening_waits_while_another_run_writes_during_the_switch_to_wal(path):
    # With another run holding the write lock, SQLite answers "busy" at once to the switch to
    # WAL, without waiting: opening must try again until the other run is done.
    ours_before_switching_to_wal(path)
    other = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    other.execute("BEGIN IMMEDIATE")
    let_go = threading.Timer(0.3, lambda: other.execute("ROLLBACK"))
    let_go.start()
    started = time.monotonic()
    try:
        db = open_database(path)
    finally:
        let_go.join()
        other.close()
    assert time.monotonic() - started >= 0.25  # it waited for the other run
    assert db.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    db.close()


def test_busy_means_the_whole_timeout_went_by(path, monkeypatch):
    monkeypatch.setattr(schema, "BUSY_TIMEOUT_MS", 300)
    ours_before_switching_to_wal(path)
    other = sqlite3.connect(path, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    try:
        with pytest.raises(
            ResultsError, match="is busy: another program held it for more than 0.3 s"
        ):
            schema.open_database(path)
    finally:
        other.execute("ROLLBACK")
        other.close()
    assert time.monotonic() - started >= 0.3


def test_a_database_another_program_holds_says_so(path, monkeypatch):
    open_database(path).close()
    monkeypatch.setattr(schema, "BUSY_TIMEOUT_MS", 100)
    upgrade_to_next(monkeypatch, add_notes_table)  # so opening it needs the write lock
    other = sqlite3.connect(path, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")  # a database browser with unsaved changes, say
    try:
        with pytest.raises(ResultsError) as info:
            schema.open_database(path)
    finally:
        other.execute("ROLLBACK")
        other.close()
    message = str(info.value)
    assert f"results database {path} is busy: another program held it for more than 0.1 s" in (
        message
    )
    assert (
        "Check: close whatever holds it, such as a database browser with unsaved changes or "
        "another BenchTare run upgrading it, and run again; or point --results" in message
    )
    db = sqlite3.connect(path)
    assert schema.schema_version(db) == SCHEMA_VERSION  # not upgraded
    db.close()


# --- writes that fail -------------------------------------------------------------------------------


def test_a_write_that_fails_says_which_database_and_what_to_check(store):
    store.db.execute("PRAGMA query_only = ON")  # as if the file had become read-only
    with pytest.raises(ResultsError) as info:
        store.start_run(run_info())
    assert info.value.message == (
        f"results database {store.path} couldn't be written: attempt to write a readonly database"
    )
    assert info.value.hint == (
        "that the disk isn't full and the file and its folder can be written; or point --results "
        "at another file, or run with --no-results"
    )


def test_a_write_while_another_program_holds_the_database_says_so(path, monkeypatch):
    monkeypatch.setattr(schema, "BUSY_TIMEOUT_MS", 100)
    store = SQLiteStore(path)
    other = sqlite3.connect(path, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(ResultsError, match=r"is busy: another program held it for more than"):
            store.start_run(run_info())
    finally:
        other.execute("ROLLBACK")
        other.close()
    store.start_run(run_info())  # once it's let go, writing works again
    assert store.db.execute("SELECT id FROM runs").fetchall() == [("run-1",)]
    store.close()


def test_a_failure_sqlite_already_rolled_back_is_not_hidden(path):
    db = open_database(path)
    with pytest.raises(ValueError, match="the real failure"), schema.transaction(db):
        db.execute("ROLLBACK")  # SQLite does this itself for a full disk, say
        raise ValueError("the real failure")
    db.close()


# --- runs at the same time ---------------------------------------------------------------------------

TESTS_PER_RUN = 5

# One process storing whole runs, as one pytest session does. Each worker waits until every
# worker is ready, so they open the database at the same moment.
WORKER = """
import sys
import time
from pathlib import Path

from benchtare.record import RecordEntry, RecordLevel
from benchtare.results import schema
from benchtare.results.store import RunInfo, SQLiteStore, TestResult

path, ready, name, runs, workers, mode = sys.argv[1:]
ready = Path(ready)

if mode == "slow-first-look" and name != "w0":
    # A slow machine: the first worker creates the database while the others are part way
    # through their first look at it.
    read_version = schema.schema_version
    looks = []

    def slow_schema_version(db):
        version = read_version(db)
        if not looks:
            looks.append(version)
            time.sleep(0.3)
        return version

    schema.schema_version = slow_schema_version

if mode == "upgrade":  # this BenchTare is a version newer, with an upgrade that takes time

    def add_notes(db):
        time.sleep(0.2)
        db.execute("CREATE TABLE notes (run_id TEXT, note TEXT)")

    schema.MIGRATIONS = {**schema.MIGRATIONS, schema.SCHEMA_VERSION: add_notes}
    schema.SCHEMA_VERSION += 1

(ready / name).touch()
deadline = time.monotonic() + 60
while len(list(ready.iterdir())) < int(workers):
    if time.monotonic() > deadline:
        sys.exit("the other workers never started")
    time.sleep(0.001)

for n in range(int(runs)):
    run_id = f"{name}-{n}"
    store = SQLiteStore(Path(path))
    store.start_run(
        RunInfo(run_id, "tests", time.time(), "stimuli", "d", "b", "h", None, "0", "0", [])
    )
    for seq in range(1, TESTS + 1):
        entry = RecordEntry("read", RecordLevel.RESULTS, "r", {"signal": "X", "value": seq})
        store.add_record(run_id, [entry], phase="call", test_seq=seq)
        store.add_test(run_id, TestResult(seq, f"t.py::test_{seq}", "t.py", "passed", time.time()))
    store.finish_run(run_id, status="finished", exit_code=0, counts={"passed": TESTS})
    store.close()
""".replace("TESTS", str(TESTS_PER_RUN))


def run_together(tmp_path, path, *, workers=4, runs=1, mode="at-full-speed"):
    """Start ``workers`` processes on one database at once; return each one's (exit, stderr).
    ``mode`` is "at-full-speed", "slow-first-look" or "upgrade"."""
    script = tmp_path / "worker.py"
    script.write_text(WORKER, encoding="utf-8")
    ready = tmp_path / "ready"
    ready.mkdir()
    started = [
        subprocess.Popen(
            [
                sys.executable,
                str(script),
                str(path),
                str(ready),
                f"w{i}",
                str(runs),
                str(workers),
                mode,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        for i in range(workers)
    ]
    return [(proc.wait(timeout=120), proc.communicate()[1]) for proc in started]


@pytest.mark.parametrize("mode", ["at-full-speed", "slow-first-look"])
def test_runs_starting_together_on_a_new_database_are_all_stored(tmp_path, path, mode):
    # With a slow first look, another run creates the tables between this run reading the
    # version and listing the tables: both must come from one snapshot of the file.
    assert run_together(tmp_path, path, runs=5, mode=mode) == [(0, "")] * 4
    db = sqlite3.connect(path)
    assert db.execute("SELECT count(*), sum(status = 'finished') FROM runs").fetchone() == (20, 20)
    assert db.execute("SELECT count(*) FROM test_results").fetchone() == (20 * TESTS_PER_RUN,)
    per_run = db.execute(
        "SELECT count(*), min(seq), max(seq) FROM record_entries GROUP BY run_id"
    ).fetchall()
    assert per_run == [(TESTS_PER_RUN, 1, TESTS_PER_RUN)] * 20
    db.close()


def test_runs_upgrading_an_older_database_together_upgrade_it_once(tmp_path, path):
    store = SQLiteStore(path)
    store.start_run(run_info())
    store.close()
    assert run_together(tmp_path, path, mode="upgrade") == [(0, "")] * 4
    db = sqlite3.connect(path)
    assert schema.schema_version(db) == NEXT
    assert "notes" in tables(db)
    assert db.execute("SELECT count(*) FROM runs").fetchone() == (5,)  # run-1 kept, plus 4
    db.close()
    backup = sqlite3.connect(path.with_name(f"results.sqlite3.v{SCHEMA_VERSION}.bak"))
    assert schema.schema_version(backup) == SCHEMA_VERSION  # the file before the upgrade
    assert backup.execute("SELECT id FROM runs").fetchall() == [("run-1",)]
    backup.close()


# --- writing a run ---------------------------------------------------------------------------------


def test_a_run_that_never_finishes_stays_running_with_what_finished(path):
    store = SQLiteStore(path)
    store.start_run(run_info())
    store.add_test("run-1", TestResult(1, "t.py::a", "t.py", "passed", time.time()))
    store.close()  # no finish_run: the process died
    db = sqlite3.connect(path)
    assert db.execute("SELECT status, finished_at FROM runs").fetchall() == [("running", None)]
    assert db.execute("SELECT nodeid, outcome FROM test_results").fetchall() == [
        ("t.py::a", "passed")
    ]
    db.close()


def test_finishing_a_run_stores_its_status_and_counts(store):
    store.start_run(run_info())
    store.finish_run(
        "run-1", status="finished", exit_code=1, counts={"passed": 3, "failed": 1, "errors": 2}
    )
    row = store.db.execute(
        "SELECT status, exit_code, tests_passed, tests_failed, tests_errors, tests_skipped, "
        "finished_at FROM runs"
    ).fetchone()
    assert row[:6] == ("finished", 1, 3, 1, 2, 0)
    assert row[6].endswith("Z")


def test_record_entries_keep_their_types_and_numbers_for_trends(store):
    store.start_run(run_info())
    facts = {"endpoint": "io.in1", "unit": None}
    values = [42, 42.5, True, "run", None, float("nan")]
    entries = [
        RecordEntry(
            "read", RecordLevel.RESULTS, f"read {v!r}", {"signal": "X", "value": v, **facts}
        )
        for v in values
    ]
    entries.append(RecordEntry("result", RecordLevel.RESULTS, "call passed", {"phase": "call"}))
    store.add_record("run-1", entries, phase="call", test_seq=1)
    rows = store.db.execute(
        "SELECT seq, test_seq, phase, signal, value, value_num, data FROM record_entries "
        "ORDER BY seq"
    ).fetchall()
    assert [row[0] for row in rows] == [1, 2, 3, 4, 5, 6, 7]
    assert [json.loads(row[4]) if row[4] else None for row in rows[:5]] == [
        42,
        42.5,
        True,
        "run",
        None,
    ]
    assert [row[5] for row in rows[:6]] == [42.0, 42.5, 1.0, None, None, None]
    assert rows[0][1:4] == (1, "call", "X")
    assert rows[0][6] is None  # nothing left over beyond the columns
    assert json.loads(rows[6][6]) == {"phase": "call"}
    store.add_record("run-1", entries[:1], phase="teardown", test_seq=1)
    assert store.db.execute("SELECT max(seq) FROM record_entries").fetchone()[0] == 8


def test_identity_values_are_numbered_per_item(store):
    store.start_run(run_info())
    now = time.time()
    store.add_identity(
        "run-1",
        [
            IdentityValue("software", "1.0.2", "signal", True, now),
            IdentityValue("motor.firmware", "2.3.1", "code", False, now),
            IdentityValue("software", "1.0.3", "code", True, now),
            IdentityValue("bootloader", None, "missing", True, None),
        ],
    )
    rows = store.db.execute(
        "SELECT name, seq, expected, value, source, at IS NULL FROM run_dut_identity "
        "ORDER BY name, seq"
    ).fetchall()
    assert rows == [
        ("bootloader", 1, 1, None, "missing", 1),
        ("motor.firmware", 1, 0, "2.3.1", "code", 0),
        ("software", 1, 1, "1.0.2", "signal", 0),
        ("software", 2, 1, "1.0.3", "code", 0),
    ]


def test_two_runs_are_kept_apart(store):
    for run_id in ("run-1", "run-2"):
        store.start_run(run_info(run_id))
        store.add_record(
            run_id, [RecordEntry("read", RecordLevel.RESULTS, "r", {"value": 1})], phase="call"
        )
    assert store.db.execute(
        "SELECT run_id, seq FROM record_entries ORDER BY run_id"
    ).fetchall() == [
        ("run-1", 1),
        ("run-2", 1),
    ]


def test_times_are_utc_to_the_microsecond():
    assert utc(0.5) == "1970-01-01T00:00:00.500000Z"
    assert utc(None).endswith("Z")
