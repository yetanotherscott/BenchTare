"""Storing results: the ``ResultStore`` protocol and its SQLite implementation.

A run is written as it goes: ``start_run`` when the session starts, each test when it finishes,
``finish_run`` at the end. A run that crashed stays ``running``, with every test that finished.
A write that fails raises ``ResultsError``, saying which file and what to check.
Rows are written from pytest's main thread only.

See docs/results-schema.md for the tables.
"""

from __future__ import annotations

import json
import math
import numbers
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from benchtare.dut.identity import IdentityValue
from benchtare.record import RecordEntry
from benchtare.results.manifest import ManifestFile
from benchtare.results.schema import SCHEMA_VERSION, open_database, transaction, write_failed
from benchtare.selftest import SelfTestResult
from benchtare.testcheck import ModeResult

RUNNING = "running"
FINISHED = "finished"
STOPPED = "stopped"

TEST_CHECK = "check-tests"  # the kind of a run of the test check

# Facts of a record entry that get columns of their own; the rest go in ``data``.
_ENTRY_COLUMNS = ("signal", "endpoint", "unit", "value")


@dataclass(frozen=True)
class RunInfo:
    """What is known about a run when it starts."""

    id: str
    kind: str  # "tests", "selftest" or "check-tests"
    started_at: float  # time.time()
    record_level: str
    dut: str
    bench: str
    harness: str
    simulated_dut: str | None
    benchtare_version: str
    pytest_version: str
    args: Sequence[str]
    git_commit: str | None = None
    git_dirty: bool | None = None


@dataclass(frozen=True)
class TestResult:
    """One test, once its last phase has finished."""

    __test__ = False  # not a test class, despite its name

    seq: int
    nodeid: str
    file: str
    outcome: str  # passed, failed, error, skipped, xfailed or xpassed
    started_at: float
    message: str | None = None
    details: str | None = None
    durations: Mapping[str, float] = field(default_factory=dict)  # by phase
    properties: Sequence[tuple[str, Any]] = ()


class ResultStore(Protocol):
    """Where runs are stored. SQLite is the only implementation in the first slice."""

    def start_run(self, run: RunInfo) -> None: ...

    def add_environment(
        self, run_id: str, environment: Mapping[str, Any], packages: Sequence[tuple[str, str]]
    ) -> None: ...

    def add_manifest_files(self, run_id: str, files: Sequence[ManifestFile]) -> None: ...

    def add_equipment(self, run_id: str, equipment: Sequence[Mapping[str, Any]]) -> None: ...

    def add_selftest(self, run_id: str, result: SelfTestResult) -> None: ...

    def add_test(self, run_id: str, result: TestResult) -> None: ...

    def add_record(
        self,
        run_id: str,
        entries: Sequence[RecordEntry],
        *,
        phase: str,
        test_seq: int | None = None,
    ) -> None: ...

    def add_identity(self, run_id: str, values: Sequence[IdentityValue]) -> None: ...

    def add_checks(
        self,
        run_id: str,
        test_seq: int,
        verdict: str,
        results: Sequence[ModeResult],
        records: Mapping[str, Mapping[str, Sequence[RecordEntry]]],
    ) -> None:
        """A test's verdict from the test check, its result under each mode, and the record of
        each rerun (``records[mode][phase]``)."""
        ...

    def check_status(self, run_id: str) -> list[tuple[str, str | None, bool | None]]:
        """Each test file in the run's manifest: its path, when it was last checked, and
        whether it is unchanged since (None when never checked)."""
        ...

    def finish_run(
        self, run_id: str, *, status: str, exit_code: int, counts: Mapping[str, int]
    ) -> None: ...

    def close(self) -> None: ...


class SQLiteStore:
    """Results in a local SQLite file. Opening it creates, migrates or refuses the file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.db = open_database(self.path)
        self._next_entry: dict[str, int] = {}
        self._identity_seq: dict[tuple[str, str], int] = {}

    def start_run(self, run: RunInfo) -> None:
        with self._write():
            self.db.execute(
                """INSERT INTO runs (id, kind, status, started_at, schema_version, record_level,
                    dut, bench, harness, simulated_dut, benchtare_version, pytest_version, args,
                    git_commit, git_dirty)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run.id,
                    run.kind,
                    RUNNING,
                    utc(run.started_at),
                    SCHEMA_VERSION,
                    run.record_level,
                    run.dut,
                    run.bench,
                    run.harness,
                    run.simulated_dut,
                    run.benchtare_version,
                    run.pytest_version,
                    _json(list(run.args)),
                    run.git_commit,
                    None if run.git_dirty is None else int(run.git_dirty),
                ),
            )
        self._next_entry[run.id] = 1

    def add_environment(
        self, run_id: str, environment: Mapping[str, Any], packages: Sequence[tuple[str, str]]
    ) -> None:
        columns = (
            "host",
            "os",
            "os_release",
            "os_version",
            "architecture",
            "processor",
            "cpu_count",
            "python_implementation",
            "python_version",
            "python_build",
            "encoding",
            "timezone",
            "ci",
        )
        with self._write():
            self.db.execute(
                f"INSERT INTO run_environment (run_id, {', '.join(columns)}) "
                f"VALUES (?{', ?' * len(columns)})",
                (run_id, *(environment.get(column) for column in columns)),
            )
            self.db.executemany(
                "INSERT OR IGNORE INTO run_packages (run_id, name, version) VALUES (?, ?, ?)",
                [(run_id, name, version) for name, version in packages],
            )

    def add_manifest_files(self, run_id: str, files: Sequence[ManifestFile]) -> None:
        """Store the manifest. For each test file, also when the test check last ran on it
        (the latest finished check run of the same DUT, before this run) and whether the file
        is unchanged since."""
        rows = []
        for f in files:
            checked_at, unchanged = None, None
            if f.role == "test":
                last = self.db.execute(
                    """SELECT r.started_at, m.sha256 FROM manifest_files m
                    JOIN runs r ON r.id = m.run_id
                    WHERE r.kind = ? AND r.status = ? AND m.path = ? AND r.id != ?
                        AND r.dut = (SELECT dut FROM runs WHERE id = ?)
                    ORDER BY r.started_at DESC LIMIT 1""",
                    (TEST_CHECK, FINISHED, f.path, run_id, run_id),
                ).fetchone()
                if last is not None:
                    checked_at, unchanged = last[0], int(last[1] == f.sha256)
            rows.append((run_id, f.path, f.role, f.sha256, f.size, checked_at, unchanged))
        with self._write():
            self.db.executemany(
                "INSERT OR IGNORE INTO manifest_files (run_id, path, role, sha256, size, "
                "last_checked_at, unchanged_since_check) VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    def check_status(self, run_id: str) -> list[tuple[str, str | None, bool | None]]:
        rows = self.db.execute(
            "SELECT path, last_checked_at, unchanged_since_check FROM manifest_files "
            "WHERE run_id = ? AND role = 'test' ORDER BY path",
            (run_id,),
        ).fetchall()
        return [(path, at, None if same is None else bool(same)) for path, at, same in rows]

    def add_equipment(self, run_id: str, equipment: Sequence[Mapping[str, Any]]) -> None:
        with self._write():
            self.db.executemany(
                """INSERT OR REPLACE INTO run_equipment (run_id, equipment, backend, kind,
                    simulated, vendor, model, serial_number, firmware_version, driver,
                    driver_version, details)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        run_id,
                        item["equipment"],
                        item["backend"],
                        item["kind"],
                        int(bool(item["simulated"])),
                        item.get("vendor"),
                        item.get("model"),
                        item.get("serial_number"),
                        item.get("firmware_version"),
                        item.get("driver"),
                        item.get("driver_version"),
                        _json(item["details"]) if item.get("details") else None,
                    )
                    for item in equipment
                ],
            )

    def add_selftest(self, run_id: str, result: SelfTestResult) -> None:
        with self._write():
            self.db.execute(
                "INSERT INTO selftests (run_id, passed, started_at, duration) VALUES (?, ?, ?, ?)",
                (run_id, int(result.passed), utc(result.started), result.duration),
            )
            self.db.executemany(
                """INSERT INTO selftest_checks (run_id, seq, kind, name, outcome, detail, hint)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [
                    (run_id, seq, c.kind, c.name, c.outcome, c.detail, c.hint)
                    for seq, c in enumerate(result.checks, start=1)
                ],
            )

    def add_test(self, run_id: str, result: TestResult) -> None:
        with self._write():
            self.db.execute(
                """INSERT INTO test_results (run_id, seq, nodeid, file, outcome, message, details,
                    started_at, setup_duration, call_duration, teardown_duration, properties)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    result.seq,
                    result.nodeid,
                    result.file,
                    result.outcome,
                    result.message,
                    result.details,
                    utc(result.started_at),
                    result.durations.get("setup"),
                    result.durations.get("call"),
                    result.durations.get("teardown"),
                    _json(dict(result.properties)) if result.properties else None,
                ),
            )

    def add_record(
        self,
        run_id: str,
        entries: Sequence[RecordEntry],
        *,
        phase: str,
        test_seq: int | None = None,
    ) -> None:
        if not entries:
            return
        first = self._next_entry.get(run_id, 1)
        rows = [_entry_row(run_id, first + i, test_seq, phase, e) for i, e in enumerate(entries)]
        with self._write():
            self.db.executemany(
                """INSERT INTO record_entries (run_id, seq, test_seq, phase, at, kind, level,
                    text, signal, endpoint, unit, value, value_num, data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
        self._next_entry[run_id] = first + len(entries)

    def add_identity(self, run_id: str, values: Sequence[IdentityValue]) -> None:
        rows = []
        for value in values:
            seq = self._identity_seq.get((run_id, value.name), 0) + 1
            self._identity_seq[(run_id, value.name)] = seq
            rows.append(
                (
                    run_id,
                    value.name,
                    seq,
                    int(value.expected),
                    value.value,
                    value.source,
                    None if value.at is None else utc(value.at),
                )
            )
        if not rows:
            return
        with self._write():
            self.db.executemany(
                """INSERT INTO run_dut_identity (run_id, name, seq, expected, value, source, at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )

    def add_checks(
        self,
        run_id: str,
        test_seq: int,
        verdict: str,
        results: Sequence[ModeResult],
        records: Mapping[str, Mapping[str, Sequence[RecordEntry]]],
    ) -> None:
        first = self._next_entry.get(run_id, 1)
        entries = [
            (mode, phase, entry)
            for mode, phases in records.items()
            for phase, phase_entries in phases.items()
            for entry in phase_entries
        ]
        rows = [
            _entry_row(run_id, first + i, test_seq, f"{mode} {phase}", entry)
            for i, (mode, phase, entry) in enumerate(entries)
        ]
        with self._write():
            self.db.execute(
                "UPDATE test_results SET check_verdict = ? WHERE run_id = ? AND seq = ?",
                (verdict, run_id, test_seq),
            )
            self.db.executemany(
                "INSERT INTO test_checks (run_id, test_seq, mode, outcome, detail) "
                "VALUES (?, ?, ?, ?, ?)",
                [(run_id, test_seq, r.mode, r.outcome, r.detail) for r in results],
            )
            self.db.executemany(
                """INSERT INTO record_entries (run_id, seq, test_seq, phase, at, kind, level,
                    text, signal, endpoint, unit, value, value_num, data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
        self._next_entry[run_id] = first + len(rows)

    def finish_run(
        self, run_id: str, *, status: str, exit_code: int, counts: Mapping[str, int]
    ) -> None:
        with self._write():
            self.db.execute(
                """UPDATE runs SET status = ?, finished_at = ?, exit_code = ?, tests_passed = ?,
                    tests_failed = ?, tests_errors = ?, tests_skipped = ?
                WHERE id = ?""",
                (
                    status,
                    utc(None),
                    int(exit_code),
                    counts.get("passed", 0),
                    counts.get("failed", 0),
                    counts.get("errors", 0),
                    counts.get("skipped", 0),
                    run_id,
                ),
            )

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def _write(self) -> Iterator[None]:
        """One write, all or nothing. Raise ``ResultsError`` saying which file and what to
        check if it can't be written: busy for too long, a full disk, a read-only file."""
        try:
            with transaction(self.db):
                yield
        except sqlite3.Error as exc:
            raise write_failed(self.path, exc) from None


def utc(at: float | None) -> str:
    """``2026-11-15T18:52:09.123456Z``: a time.time() value (or now) as UTC, to the µs."""
    moment = datetime.now(timezone.utc) if at is None else datetime.fromtimestamp(at, timezone.utc)
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _entry_row(
    run_id: str, seq: int, test_seq: int | None, phase: str, entry: RecordEntry
) -> tuple[Any, ...]:
    data = dict(entry.data)
    columns = {key: data.pop(key, None) for key in _ENTRY_COLUMNS}
    value = columns["value"]
    has_value = "value" in entry.data
    return (
        run_id,
        seq,
        test_seq,
        phase,
        utc(entry.at),
        entry.kind,
        entry.level.label,
        entry.text,
        None if columns["signal"] is None else str(columns["signal"]),
        None if columns["endpoint"] is None else str(columns["endpoint"]),
        columns["unit"],
        _json(value) if has_value and value is not None else None,
        _number(value),
        _json(data) if data else None,
    )


def _number(value: Any) -> float | None:
    """A value as a number for trends: finite numbers as they are, bools as 0 and 1 (Python's
    bool is a number)."""
    if isinstance(value, numbers.Real):
        number = float(value)
        return number if math.isfinite(number) else None
    return None


def _json(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)
