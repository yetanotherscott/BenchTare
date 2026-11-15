"""The results database: creating, reopening, refusing and migrating it, and how rows are
written. The plugin's side is in test_results_plugin.py."""

import json
import sqlite3
import time

import pytest

from benchtare.dut.identity import IdentityValue
from benchtare.record import RecordEntry, RecordLevel
from benchtare.results import schema
from benchtare.results.schema import SCHEMA_VERSION, ResultsError, open_database
from benchtare.results.store import RunInfo, SQLiteStore, TestResult, utc

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
}


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
    assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 1
    meta = dict(db.execute("SELECT key, value FROM meta"))
    assert meta["schema_version"] == "1"
    assert meta["created_by"].startswith("benchtare ")
    db.close()


def test_reopening_keeps_what_was_stored(path):
    store = SQLiteStore(path)
    store.start_run(run_info())
    store.close()
    store = SQLiteStore(path)
    assert store.db.execute("SELECT id, status FROM runs").fetchall() == [("run-1", "running")]
    store.close()


# --- refusing what can't be used -------------------------------------------------------------------


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


def test_an_older_database_is_backed_up_and_migrated(path, monkeypatch):
    store = SQLiteStore(path)
    store.start_run(run_info())
    store.close()
    monkeypatch.setattr(schema, "SCHEMA_VERSION", 2)
    monkeypatch.setattr(schema, "MIGRATIONS", {1: add_notes_table})
    db = schema.open_database(path)
    assert "notes" in tables(db)
    assert db.execute("PRAGMA user_version").fetchone()[0] == 2
    assert dict(db.execute("SELECT key, value FROM meta"))["schema_version"] == "2"
    assert db.execute("SELECT id FROM runs").fetchall() == [("run-1",)]  # data kept
    db.close()
    backup = sqlite3.connect(path.with_name("results.sqlite3.v1.bak"))
    assert backup.execute("PRAGMA user_version").fetchone()[0] == 1
    assert backup.execute("SELECT id FROM runs").fetchall() == [("run-1",)]
    backup.close()


def test_a_failed_migration_leaves_the_database_unchanged(path, monkeypatch):
    open_database(path).close()

    def broken(db):
        db.execute("CREATE TABLE notes (note TEXT)")
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(schema, "SCHEMA_VERSION", 2)
    monkeypatch.setattr(schema, "MIGRATIONS", {1: broken})
    with pytest.raises(ResultsError) as info:
        schema.open_database(path)
    assert "couldn't upgrade results database" in str(info.value)
    assert "RuntimeError: disk on fire. It is unchanged, and a copy is in" in str(info.value)
    db = sqlite3.connect(path)
    assert db.execute("PRAGMA user_version").fetchone()[0] == 1
    assert "notes" not in tables(db)
    db.close()


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
