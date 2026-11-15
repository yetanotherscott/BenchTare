"""The results database: its schema, and opening, creating and migrating it.

See docs/results-schema.md for what each table holds and why. In short:

- A new file is created at ``SCHEMA_VERSION``.
- An older file is copied to ``<name>.v<N>.bak`` and migrated step by step in one transaction,
  so a failed migration leaves it as it was.
- A newer file, or a file that isn't a BenchTare results database, is refused and left as it
  was.
- Two runs opening the file at once decide whether to create or upgrade it while holding the
  write lock, so only one does it.

The version is kept twice: in SQLite's ``PRAGMA user_version``, which can be read without any
table, and in the ``meta`` table, which is the copy a server database would keep.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from benchtare import __version__
from benchtare.errors import BenchTareError

SCHEMA_VERSION = 2

# Version 1. Each later version adds a migration below instead of changing these.
TABLES_V1 = (
    """CREATE TABLE meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""",
    """CREATE TABLE runs (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        status TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        exit_code INTEGER,
        schema_version INTEGER NOT NULL,
        record_level TEXT NOT NULL,
        dut TEXT NOT NULL,
        bench TEXT NOT NULL,
        harness TEXT NOT NULL,
        simulated_dut TEXT,
        benchtare_version TEXT NOT NULL,
        pytest_version TEXT NOT NULL,
        args TEXT NOT NULL,
        git_commit TEXT,
        git_dirty INTEGER,
        tests_passed INTEGER,
        tests_failed INTEGER,
        tests_errors INTEGER,
        tests_skipped INTEGER
    )""",
    "CREATE INDEX runs_started_at ON runs (started_at)",
    """CREATE TABLE run_environment (
        run_id TEXT PRIMARY KEY REFERENCES runs (id),
        host TEXT,
        os TEXT NOT NULL,
        os_release TEXT,
        os_version TEXT,
        architecture TEXT,
        processor TEXT,
        cpu_count INTEGER,
        python_implementation TEXT NOT NULL,
        python_version TEXT NOT NULL,
        python_build TEXT,
        encoding TEXT,
        timezone TEXT,
        ci TEXT
    )""",
    """CREATE TABLE run_packages (
        run_id TEXT NOT NULL REFERENCES runs (id),
        name TEXT NOT NULL,
        version TEXT NOT NULL,
        PRIMARY KEY (run_id, name)
    )""",
    """CREATE TABLE run_equipment (
        run_id TEXT NOT NULL REFERENCES runs (id),
        equipment TEXT NOT NULL,
        backend TEXT NOT NULL,
        kind TEXT NOT NULL,
        simulated INTEGER NOT NULL,
        vendor TEXT,
        model TEXT,
        serial_number TEXT,
        firmware_version TEXT,
        driver TEXT,
        driver_version TEXT,
        details TEXT,
        PRIMARY KEY (run_id, equipment)
    )""",
    """CREATE TABLE run_dut_identity (
        run_id TEXT NOT NULL REFERENCES runs (id),
        name TEXT NOT NULL,
        seq INTEGER NOT NULL,
        expected INTEGER NOT NULL,
        value TEXT,
        source TEXT NOT NULL,
        at TEXT,
        PRIMARY KEY (run_id, name, seq)
    )""",
    """CREATE TABLE manifest_files (
        run_id TEXT NOT NULL REFERENCES runs (id),
        path TEXT NOT NULL,
        role TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        size INTEGER NOT NULL,
        PRIMARY KEY (run_id, path)
    )""",
    """CREATE TABLE selftests (
        run_id TEXT PRIMARY KEY REFERENCES runs (id),
        passed INTEGER NOT NULL,
        started_at TEXT NOT NULL,
        duration REAL NOT NULL
    )""",
    """CREATE TABLE selftest_checks (
        run_id TEXT NOT NULL REFERENCES runs (id),
        seq INTEGER NOT NULL,
        kind TEXT NOT NULL,
        name TEXT NOT NULL,
        outcome TEXT NOT NULL,
        detail TEXT NOT NULL,
        hint TEXT,
        PRIMARY KEY (run_id, seq)
    )""",
    """CREATE TABLE test_results (
        run_id TEXT NOT NULL REFERENCES runs (id),
        seq INTEGER NOT NULL,
        nodeid TEXT NOT NULL,
        file TEXT NOT NULL,
        outcome TEXT NOT NULL,
        message TEXT,
        details TEXT,
        started_at TEXT NOT NULL,
        setup_duration REAL,
        call_duration REAL,
        teardown_duration REAL,
        properties TEXT,
        PRIMARY KEY (run_id, seq),
        UNIQUE (run_id, nodeid)
    )""",
    "CREATE INDEX test_results_nodeid ON test_results (nodeid)",
    """CREATE TABLE record_entries (
        run_id TEXT NOT NULL REFERENCES runs (id),
        seq INTEGER NOT NULL,
        test_seq INTEGER,
        phase TEXT NOT NULL,
        at TEXT NOT NULL,
        kind TEXT NOT NULL,
        level TEXT NOT NULL,
        text TEXT NOT NULL,
        signal TEXT,
        endpoint TEXT,
        unit TEXT,
        value TEXT,
        value_num REAL,
        data TEXT,
        PRIMARY KEY (run_id, seq)
    )""",
    "CREATE INDEX record_entries_test ON record_entries (run_id, test_seq)",
    "CREATE INDEX record_entries_signal ON record_entries (signal, at)",
)


def _v1_to_v2(db: sqlite3.Connection) -> None:
    """Version 2 (M6), the test check: each test's result under each corruption mode, its
    verdict, and for each test file in a manifest when it was last checked."""
    db.execute(
        """CREATE TABLE test_checks (
            run_id TEXT NOT NULL REFERENCES runs (id),
            test_seq INTEGER NOT NULL,
            mode TEXT NOT NULL,
            outcome TEXT NOT NULL,
            detail TEXT NOT NULL,
            PRIMARY KEY (run_id, test_seq, mode)
        )"""
    )
    db.execute("ALTER TABLE test_results ADD COLUMN check_verdict TEXT")
    db.execute("ALTER TABLE manifest_files ADD COLUMN last_checked_at TEXT")
    db.execute("ALTER TABLE manifest_files ADD COLUMN unchanged_since_check INTEGER")


# MIGRATIONS[n] upgrades a database from version n to version n + 1. A new database is created
# at version 1 and brought up through the same steps, so every database ends up the same.
Migration = Callable[[sqlite3.Connection], None]
MIGRATIONS: dict[int, Migration] = {1: _v1_to_v2}

BUSY_TIMEOUT_MS = 5000  # how long to wait for another run's write before giving up
_RETRY_PAUSE = 0.02  # seconds between tries when SQLite answers "busy" without waiting

_SQLITE_HEADER = b"SQLite format 3\x00"
_CHECK = "point --results at another file, or run with --no-results"


class ResultsError(BenchTareError):
    """The results database can't be used."""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        self.message = message
        self.hint = hint
        super().__init__(str(self))

    def __str__(self) -> str:
        return self.message + (f"\n  Check: {self.hint}" if self.hint else "")


def open_database(path: Path) -> sqlite3.Connection:
    """Open the results database at ``path``: create it, migrate it, or refuse it.

    Raise ``ResultsError`` with what is wrong and what to check.
    """
    path = Path(path)
    if path.is_dir():
        raise ResultsError(f"results database {path} is a folder, not a file", hint=_CHECK)
    _check_is_sqlite(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path, isolation_level=None)  # transactions are explicit
    except (OSError, sqlite3.Error) as exc:
        raise ResultsError(f"can't open results database {path}: {exc}", hint=_CHECK) from None
    deadline = time.monotonic() + BUSY_TIMEOUT_MS / 1000
    try:
        db.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")  # another run writing: wait
        db.execute("PRAGMA foreign_keys = ON")
        _patiently(lambda: _bring_up_to_date(db, path), deadline)
        # Only once the file is known to be ours: switching to WAL rewrites the file's header.
        _patiently(lambda: db.execute("PRAGMA journal_mode = WAL"), deadline)
    except sqlite3.OperationalError as exc:
        db.close()
        if _is_busy(exc):
            raise _busy(path) from None
        raise _unreadable(path, exc) from None
    except sqlite3.DatabaseError as exc:
        db.close()
        raise _unreadable(path, exc) from None
    except ResultsError:
        db.close()
        raise
    return db


def write_failed(path: Path, exc: sqlite3.Error) -> ResultsError:
    """What to tell the engineer when a write to the database at ``path`` failed."""
    if isinstance(exc, sqlite3.OperationalError) and _is_busy(exc):
        return _busy(path)
    return ResultsError(
        f"results database {path} couldn't be written: {exc}",
        hint=f"that the disk isn't full and the file and its folder can be written; or {_CHECK}",
    )


@contextmanager
def transaction(db: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One write: all of it or none of it."""
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
    except BaseException:
        # SQLite rolls some failures back itself (a full disk, say); don't hide the failure
        # behind "no transaction is active".
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")


@contextmanager
def _reading(db: sqlite3.Connection) -> Iterator[None]:
    """Reads that see one snapshot of the file. Nothing is written."""
    db.execute("BEGIN")  # deferred: the first read starts the read transaction
    try:
        yield
    finally:
        if db.in_transaction:
            db.execute("ROLLBACK")


def _patiently(action: Callable[[], object], deadline: float) -> None:
    """Run ``action``, trying again while the file is busy, until ``deadline``.

    SQLite's busy timeout covers most waits, but where waiting could deadlock it answers "busy"
    at once instead: switching a file to WAL while another run holds the write lock, say. Each
    try is all or nothing, so trying again is safe.
    """
    while True:
        try:
            action()
            return
        except sqlite3.OperationalError as exc:
            if not _is_busy(exc) or time.monotonic() >= deadline:
                raise
        time.sleep(_RETRY_PAUSE)


def schema_version(db: sqlite3.Connection) -> int:
    return int(db.execute("PRAGMA user_version").fetchone()[0])


def _check_is_sqlite(path: Path) -> None:
    try:
        with path.open("rb") as file:
            header = file.read(len(_SQLITE_HEADER))
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ResultsError(f"can't read results database {path}: {exc}", hint=_CHECK) from None
    if header and header != _SQLITE_HEADER:
        raise ResultsError(
            f"{path} isn't a BenchTare results database: it isn't an SQLite file at all",
            hint=_CHECK,
        )


def _bring_up_to_date(db: sqlite3.Connection, path: Path) -> None:
    # A first look without a write lock, so a file that isn't ours is refused untouched. The
    # version and the tables are read as one snapshot: another run creating or upgrading the
    # file between the two reads would otherwise make it look like someone else's file.
    with _reading(db):
        version = _usable_version(db, path)
    if version == SCHEMA_VERSION:
        return
    # Then decide again holding the write lock: two runs starting together on a new or older
    # file must not both create or upgrade it. The second waits, then finds it done.
    with transaction(db):
        version = _usable_version(db, path)
        if version == 0:
            _create(db)
        elif version < SCHEMA_VERSION:
            _migrate(db, path, version)


def _usable_version(db: sqlite3.Connection, path: Path) -> int:
    """The file's schema version: 0 for an empty file. Raise if it isn't ours to write."""
    version = schema_version(db)
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if version == 0:
        if tables:
            raise ResultsError(
                f"{path} isn't a BenchTare results database: it is an SQLite file with other "
                f"tables ({', '.join(sorted(tables))})",
                hint=_CHECK,
            )
        return 0
    if "meta" not in tables:
        raise ResultsError(
            f"{path} isn't a BenchTare results database: it has no meta table", hint=_CHECK
        )
    if version > SCHEMA_VERSION:
        raise ResultsError(
            f"results database {path} is schema version {version}, written by a newer "
            f"BenchTare; this BenchTare ({__version__}) reads up to version {SCHEMA_VERSION}, so "
            "it won't write to it",
            hint=f"upgrade BenchTare, or {_CHECK}",
        )
    return version


def _create(db: sqlite3.Connection) -> None:
    """Create the tables. Called inside the transaction that holds the write lock."""
    for statement in TABLES_V1:
        db.execute(statement)
    for upgrade in range(1, SCHEMA_VERSION):
        MIGRATIONS[upgrade](db)
    _set_version(db, SCHEMA_VERSION)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    db.executemany(
        "INSERT INTO meta (key, value) VALUES (?, ?)",
        [("created_at", now), ("created_by", f"benchtare {__version__}")],
    )


def _migrate(db: sqlite3.Connection, path: Path, version: int) -> None:
    """Back up, then upgrade. Called inside the transaction that holds the write lock, so no
    other run writes between the backup and the upgrade; any failure rolls the upgrade back."""
    backup = path.with_name(f"{path.name}.v{version}.bak")
    try:
        _back_up(path, backup)
    except (OSError, sqlite3.Error) as exc:
        raise ResultsError(
            f"results database {path} needs upgrading from schema version {version} to "
            f"{SCHEMA_VERSION}, but a backup couldn't be written to {backup}: {exc}",
            hint=f"that the folder is writable, or {_CHECK}",
        ) from None
    try:
        for step in range(version, SCHEMA_VERSION):
            MIGRATIONS[step](db)
        _set_version(db, SCHEMA_VERSION)
    except Exception as exc:  # noqa: BLE001 - any failure: the transaction is rolled back
        raise ResultsError(
            f"couldn't upgrade results database {path} from schema version {version}: "
            f"{type(exc).__name__}: {exc}. It is unchanged, and a copy is in {backup}",
            hint=_CHECK,
        ) from None


def _back_up(path: Path, backup: Path) -> None:
    # A second connection reads the file: SQLite can't back up through the connection that
    # holds the write lock. Nothing has been written yet, so it reads the file as it is.
    source = sqlite3.connect(path, isolation_level=None)
    try:
        copy = sqlite3.connect(backup)
        try:
            source.backup(copy)
        finally:
            copy.close()
    finally:
        source.close()


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)  # Python 3.11 and later
    if code is not None:
        return code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    return "locked" in str(exc)  # SQLite's own wording, the same on every OS


def _busy(path: Path) -> ResultsError:
    return ResultsError(
        f"results database {path} is busy: another program held it for more than "
        f"{BUSY_TIMEOUT_MS / 1000:g} s",
        hint=(
            "close whatever holds it, such as a database browser with unsaved changes or "
            f"another BenchTare run upgrading it, and run again; or {_CHECK}"
        ),
    )


def _unreadable(path: Path, exc: sqlite3.Error) -> ResultsError:
    return ResultsError(
        f"results database {path} can't be read: {exc}",
        hint=f"the file may be damaged; {_CHECK}",
    )


def _set_version(db: sqlite3.Connection, version: int) -> None:
    db.execute(f"PRAGMA user_version = {int(version)}")
    db.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (str(version),),
    )
