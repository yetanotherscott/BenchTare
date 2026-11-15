"""BenchTare's pytest plugin: the ``bench`` and ``signals`` fixtures. See docs/model.md.

It is configured with three files, on the command line:

    pytest --dut reference.dut.yaml --bench bench-sim.yaml --harness harness-can.yaml

or in the ini file (``pytest.ini``, ``tox.ini``, ``setup.cfg`` or ``pyproject.toml``):

    [tool.pytest.ini_options]
    benchtare_dut = "reference.dut.yaml"
    benchtare_bench = "bench-sim.yaml"
    benchtare_harness = "harness-can.yaml"

Command-line paths are relative to where pytest is run, and ini paths to the ini file.

- **Nothing set.** The plugin does nothing, so installing BenchTare never changes another
  project's test run.
- **All three set.** The files are loaded and checked when the test session starts. A wrong
  file stops the run with the file's problems before any test runs.
- **The self-test.** Once tests are collected, if any of them uses ``bench``, the bench is
  opened and the self-test runs (``benchtare.selftest``). If it fails, the run stops before
  the first test, with what failed and what to check. If no test uses the bench, neither
  happens, so tests that don't need the equipment can run without it.
- **Self-test only.** ``--selftest-only`` (what ``benchtare selftest`` runs) runs the self-test
  and no tests, and exits 0 if it passed and 1 if it failed.

Two records, each with its own verbosity:

- **The test record** (``benchtare.record``): what happened at the bench during each test. It is
  set with ``--bench-record`` or ``benchtare_record`` (``results``, ``stimuli`` or ``debug``),
  kept on each report as ``report.benchtare_record``, and shown as a "BenchTare record" section
  when a test fails. Anything unexpected the bench saw, such as a frame that doesn't match the
  DBC, is always recorded and also raised as a pytest warning for that test.
- **BenchTare's own log**, for debugging BenchTare: Python ``logging`` under ``benchtare``, set
  with pytest's usual options (``--log-level``, ``--log-cli-level``, ``log_file_level``). The
  files are loaded at session start rather than in ``pytest_configure`` so that pytest's live
  log and log file capture the loading.

Results: every run is stored in the results database (``benchtare.results``), at
``.benchtare/results.sqlite3`` under pytest's rootdir unless ``--results`` or
``benchtare_results`` says otherwise, and not at all with ``--no-results``. A run is written as
it goes: when it starts, before the first test (manifest, equipment, self-test, DUT identity),
after each test, and when it ends. A write that fails stops the run with exit code 2 and a message
saying which file and what to check. ``--identify NAME=VALUE`` adds to the DUT's identity.
"""

from __future__ import annotations

import difflib
import os
import threading
import time
import uuid
import warnings
from collections import Counter
from collections.abc import Callable, Generator, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from _pytest.runner import runtestprotocol  # what plugins that rerun a test use

from benchtare import __version__, testcheck
from benchtare._yaml import count
from benchtare.backends.base import ReadTimeout
from benchtare.bench.config import BenchConfig, load_bench_file
from benchtare.bench.runtime import Bench
from benchtare.dut.description import InterfaceDescription, load_interface_description
from benchtare.dut.identity import COMMAND_LINE, MISSING, IdentityValue, name_problem
from benchtare.errors import (
    BenchTareError,
    ConfigError,
    DutIdentityWarning,
    EquipmentWarning,
    FrameMismatchWarning,
)
from benchtare.harness.config import HarnessConfig, load_harness_file
from benchtare.record import (
    ASSERT,
    DEFAULT_LEVEL,
    EQUIPMENT_FAILURE,
    FRAME_MISMATCH,
    IDENTITY_CHANGE,
    LEVEL_NAMES,
    RESULT,
    RecordEntry,
    RecordLevel,
    render,
)
from benchtare.results.manifest import (
    BENCH,
    CONFTEST,
    DBC,
    HARNESS,
    INI,
    INTERFACE,
    TEST,
    environment,
    git_state,
    manifest_files,
    packages,
    shown_path,
)
from benchtare.results.schema import ResultsError
from benchtare.results.store import (
    FINISHED,
    STOPPED,
    TEST_CHECK,
    RunInfo,
    SQLiteStore,
    TestResult,
)
from benchtare.selftest import SelfTestResult, could_not_open, run_selftest
from benchtare.signals.model import SignalSet

SETTINGS = {
    "dut": "interface description",
    "bench": "bench file",
    "harness": "harness file",
}

SESSION_PLUGIN = "benchtare-session"

PHASES = ("setup", "call", "teardown")

_RECORD = pytest.StashKey[dict[str, list[RecordEntry]]]()
_REPORTS = pytest.StashKey[dict[str, pytest.TestReport]]()
_STARTED = pytest.StashKey[float]()
_PATHS = pytest.StashKey[dict[str, Path]]()
_LEVEL = pytest.StashKey[RecordLevel]()
_RESULTS = pytest.StashKey["Path | None"]()
_IDENTIFY = pytest.StashKey[list[tuple[str, str]]]()
_SEQ = pytest.StashKey[int]()  # the test's place in the run's results
_READINGS = pytest.StashKey[list[testcheck.Reading]]()  # the readings its body took

DEFAULT_RESULTS = Path(".benchtare") / "results.sqlite3"

# How pytest's outcomes are counted in a run's totals.
_COUNTED_AS = {
    "passed": "passed",
    "xpassed": "passed",
    "failed": "failed",
    "error": "errors",
    "skipped": "skipped",
    "xfailed": "skipped",
}

NOT_CONFIGURED = (
    "this test uses BenchTare's '{fixture}' fixture, but BenchTare isn't configured\n"
    "  Check: give --dut, --bench and --harness on the command line, or set benchtare_dut, "
    "benchtare_bench and benchtare_harness in the ini file (pyproject.toml "
    "[tool.pytest.ini_options], pytest.ini, tox.ini or setup.cfg)"
)


# --- options --------------------------------------------------------------------------------------


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("benchtare", "BenchTare: test a DUT on a bench")
    for name, kind in SETTINGS.items():
        group.addoption(
            f"--{name}",
            dest=f"benchtare_{name}",
            metavar="FILE",
            default=None,
            help=f"the {kind} (YAML), relative to where pytest is run",
        )
        parser.addini(f"benchtare_{name}", f"BenchTare: the {kind}, relative to this ini file")
    group.addoption(
        "--bench-record",
        dest="benchtare_record",
        metavar="LEVEL",
        default=None,
        help="what each test's record keeps: results (readings, comparisons, outcomes), "
        f"stimuli (also writes) or debug (also frames). Default: {DEFAULT_LEVEL.label}",
    )
    parser.addini(
        "benchtare_record",
        f"BenchTare: test record level ({', '.join(LEVEL_NAMES)}); default {DEFAULT_LEVEL.label}",
    )
    group.addoption(
        "--results",
        dest="benchtare_results",
        metavar="PATH",
        default=None,
        help=f"the results database, relative to where pytest is run. Default: {DEFAULT_RESULTS} "
        "under pytest's rootdir",
    )
    parser.addini("benchtare_results", "BenchTare: the results database, relative to this ini file")
    group.addoption(
        "--no-results",
        dest="benchtare_no_results",
        action="store_true",
        default=False,
        help="don't store this run in the results database",
    )
    group.addoption(
        "--identify",
        dest="benchtare_identify",
        metavar="NAME=VALUE",
        action="append",
        default=[],
        help="part of the DUT's identity for this run, such as --identify hardware_revision=C; "
        "repeat it for more",
    )
    group.addoption(
        "--selftest-only",
        dest="benchtare_selftest_only",
        action="store_true",
        default=False,
        help="run BenchTare's self-test of the bench and harness, then stop without running "
        "any test (this is what 'benchtare selftest' does)",
    )
    group.addoption(
        "--check-tests",
        dest="benchtare_check_tests",
        action="store_true",
        default=False,
        help="the test check: run the tests, then rerun each one that passed with what it "
        "reads corrupted (shift, then stale), and report any that still pass, because they "
        "can't fail (this is what 'benchtare check-tests' does)",
    )


def configured_paths(config: pytest.Config) -> dict[str, Path] | None:
    """The three files from the command line or the ini file; None if none of them is set."""
    found: dict[str, Path] = {}
    for name in SETTINGS:
        cli = config.getoption(f"benchtare_{name}")
        if cli:
            found[name] = Path(config.invocation_params.dir) / Path(cli).expanduser()
            continue
        ini = config.getini(f"benchtare_{name}")
        if ini:
            base = config.inipath.parent if config.inipath else config.rootpath
            found[name] = base / Path(str(ini)).expanduser()
    if not found:
        return None
    missing = [name for name in SETTINGS if name not in found]
    if missing:
        given = ", ".join(f"--{name}" for name in SETTINGS if name in found)
        needed = ", ".join(f"--{name} (or benchtare_{name})" for name in missing)
        raise pytest.UsageError(
            f"BenchTare needs all three files, but only {given} is set. Also set {needed}: the "
            "interface description, the bench file and the harness file go together."
        )
    return found


def configured_level(config: pytest.Config) -> RecordLevel:
    """The test record level from the command line or the ini file."""
    value = config.getoption("benchtare_record") or config.getini("benchtare_record")
    if not value:
        return DEFAULT_LEVEL
    try:
        return RecordLevel.named(str(value))
    except ValueError as exc:
        raise pytest.UsageError(
            f"BenchTare: {exc}. Check --bench-record, or benchtare_record in the ini file."
        ) from None


def configured_results(config: pytest.Config) -> Path | None:
    """Where this run's results go, or None if they aren't stored."""
    if config.getoption("benchtare_no_results") or config.option.collectonly:
        return None
    cli = config.getoption("benchtare_results")
    if cli:
        return Path(config.invocation_params.dir) / Path(cli).expanduser()
    ini = config.getini("benchtare_results")
    if ini:
        base = config.inipath.parent if config.inipath else config.rootpath
        return base / Path(str(ini)).expanduser()
    return config.rootpath / DEFAULT_RESULTS


def configured_identity(config: pytest.Config) -> list[tuple[str, str]]:
    """The --identify NAME=VALUE options, checked."""
    values = []
    for given in config.getoption("benchtare_identify"):
        name, equals, value = str(given).partition("=")
        problem = name_problem(name.strip()) if equals else "it needs NAME=VALUE"
        if not problem and not value:
            problem = f"'{name.strip()}' needs a value after the ="
        if problem:
            raise pytest.UsageError(
                f"BenchTare: --identify {given}: {problem}. Check: for example "
                "--identify hardware_revision=C"
            )
        values.append((name.strip(), value))
    return values


def pytest_configure(config: pytest.Config) -> None:
    paths = configured_paths(config)
    checking = config.getoption("benchtare_check_tests")
    if checking and config.getoption("benchtare_selftest_only"):
        raise pytest.UsageError(
            "BenchTare: --check-tests and --selftest-only can't be used together: the self-test "
            "runs before every test check anyway."
        )
    if paths is None:
        if config.getoption("benchtare_selftest_only") or checking:
            what = "check" if checking else "self-test"
            raise pytest.UsageError(
                f"There's nothing to {what}: BenchTare isn't configured.\n"
                + NOT_CONFIGURED.split("\n", 1)[1]
            )
        return
    config.stash[_PATHS] = paths
    config.stash[_LEVEL] = configured_level(config)
    config.stash[_RESULTS] = configured_results(config)
    config.stash[_IDENTIFY] = configured_identity(config)


@pytest.hookimpl(tryfirst=True)
def pytest_sessionstart(session: pytest.Session) -> None:
    paths = session.config.stash.get(_PATHS, None)
    if paths is None:
        return
    try:
        description = load_interface_description(paths["dut"])
        bench_config = load_bench_file(paths["bench"])
        harness = load_harness_file(paths["harness"], description, bench_config)
    except ConfigError as exc:
        raise pytest.UsageError(f"BenchTare can't start.\n{exc}") from None
    config = session.config
    bench_session = BenchSession(
        description,
        bench_config,
        harness,
        config.stash[_LEVEL],
        selftest_only=bool(config.getoption("benchtare_selftest_only")),
        check_tests=bool(config.getoption("benchtare_check_tests")),
        results=config.stash[_RESULTS],
        identify=config.stash[_IDENTIFY],
    )
    bench_session.start_run(config)
    config.pluginmanager.register(bench_session, SESSION_PLUGIN)
    if bench_session.check_tests:
        config.pluginmanager.register(_CheckRunner(bench_session), "benchtare-check-tests")


# --- fixtures ------------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def signals(request: pytest.FixtureRequest) -> SignalSet:
    """The DUT's signals, by name: ``signals.Setpoint``. Opening the bench isn't needed."""
    return _session(request, "signals").description.signals


@pytest.fixture(scope="session")
def bench(request: pytest.FixtureRequest) -> Bench:
    """The open bench: ``bench.write``, ``bench.wait_until`` and ``bench.read``."""
    return _session(request, "bench").open_bench()


def _session(request: pytest.FixtureRequest, fixture: str) -> BenchSession:
    session = request.config.pluginmanager.get_plugin(SESSION_PLUGIN)
    if session is None:
        pytest.fail(NOT_CONFIGURED.format(fixture=fixture), pytrace=False)
    return session


# --- the configured session ------------------------------------------------------------------------


class BenchSession:
    """Registered as a plugin only when BenchTare is configured. Owns the bench for the run,
    keeps each test's record, and stores the run in the results database."""

    def __init__(
        self,
        description: InterfaceDescription,
        bench: BenchConfig,
        harness: HarnessConfig,
        level: RecordLevel = DEFAULT_LEVEL,
        *,
        selftest_only: bool = False,
        check_tests: bool = False,
        results: Path | None = None,
        identify: Sequence[tuple[str, str]] = (),
    ) -> None:
        self.description = description
        self.bench_config = bench
        self.harness = harness
        self.level = level
        self.selftest_only = selftest_only
        # The test check (benchtare.testcheck): the mode of the rerun in progress, if any, and
        # each test's verdict so far.
        self.check_tests = check_tests
        # The rerun in progress: (mode, the reading it corrupts); (None, None) for the control.
        self._rerun_of: tuple[str | None, int | None] | None = None
        self.checked: list[CheckedTest] = []
        self._check_status: list[tuple[str, str | None, bool | None]] = []
        self.bench: Bench | None = None
        self.selftest: SelfTestResult | None = None
        self.pretest_record: list[RecordEntry] = []
        self._open_error: str | None = None
        self._identify = list(identify)  # from the command line
        self._identity_read = False  # listed identity signals have been read
        # Entries arrive from receive threads as well as from the test, so all of this is
        # guarded by the lock.
        self._lock = threading.Lock()
        self._item: pytest.Item | None = None
        self._phase: str | None = None
        self._pending: list[RecordEntry] = []  # arrived between phases: they go to the next one
        self._in_pretest = False  # entries then go to the record before the first test
        self._after_last_test: list[RecordEntry] = []
        # The results database: None when this run isn't stored.
        self.results_path = results
        self.store: SQLiteStore | None = None
        self.run_id: str | None = None
        self._started = time.time()
        self._test_seq = 0
        self._counts: Counter[str] = Counter()
        self._identity_stored = 0  # how many of the bench's identity values are stored
        self._manifest_stored = False
        self._tests_stored = 0
        self._config: pytest.Config | None = None
        # Set when a write to the results database failed: (what was being stored, why).
        self.results_failure: tuple[str, ResultsError] | None = None
        self._results_failure_shown = False

    # --- the run in the results database --------------------------------------------------

    def start_run(self, config: pytest.Config) -> None:
        """Open the results database and store the run's start. Called at session start."""
        self._config = config
        if self.results_path is None:
            return
        try:
            self.store = SQLiteStore(self.results_path)
        except ResultsError as exc:
            raise pytest.UsageError(f"BenchTare can't store results.\n{exc}") from None
        run_id = str(uuid.uuid4())
        commit, dirty = git_state(config.rootpath)
        run = RunInfo(
            id=run_id,
            kind="selftest" if self.selftest_only else TEST_CHECK if self.check_tests else "tests",
            started_at=self._started,
            record_level=self.level.label,
            dut=self.description.name,
            bench=self.bench_config.name,
            harness=self.harness.name,
            simulated_dut=self.harness.simulate,
            benchtare_version=__version__,
            pytest_version=pytest.__version__,
            args=[str(arg) for arg in config.invocation_params.args],
            git_commit=commit,
            git_dirty=dirty,
        )
        try:
            self.store.start_run(run)
            self.store.add_environment(run_id, environment(), packages())
        except ResultsError as exc:
            self.store.close()
            self.store = None
            raise pytest.UsageError(f"BenchTare can't store results.\n{exc}") from None
        self.run_id = run_id

    def _store(
        self, what: str, write: Callable[[SQLiteStore, str], None], *, stop: bool = True
    ) -> bool:
        """Write ``what`` to the results database; False if this run isn't stored.

        If the write fails, nothing more is stored, and with ``stop`` the run stops here: a run
        whose results can't be kept shouldn't go on. What was stored before stays, and the run
        stays marked running, like a run that crashed. Without ``stop``, the caller stops the
        run later (``pytest_runtest_logfinish``), or, at the end of the run when every test has
        run, the failure is shown in the summary and the exit code is 2.
        """
        if self.store is None or self.run_id is None:
            return False
        try:
            write(self.store, self.run_id)
        except ResultsError as exc:
            self.results_failure = (what, exc)
            self.store.close()
            self.store = None
            if stop:
                self._stop_run()
            return False
        return True

    def _stop_run(self) -> None:
        """Stop the run, because its results can't be stored."""
        self._show_results_failure(stopped=True)
        pytest.exit(
            "BenchTare couldn't store results, so the run stopped. What failed is listed above.",
            returncode=pytest.ExitCode.INTERRUPTED,
        )

    def _show_results_failure(self, *, stopped: bool) -> None:
        if self.results_failure is None or self._config is None:
            return
        reporter = self._config.pluginmanager.get_plugin("terminalreporter")
        if reporter is None:
            return
        self._results_failure_shown = True
        what, exc = self.results_failure
        title = "BenchTare couldn't store results" + (", so the run stopped" if stopped else "")
        reporter.write_sep("=", title, red=True)
        reporter.write_line(f"Storing {what} failed: {exc.message}", red=True)
        if exc.hint:
            reporter.write_line(f"  Check: {exc.hint}")
        tests = f"{self._tests_stored} test{'' if self._tests_stored == 1 else 's'}"
        reporter.write_line(
            f"Run {self.run_id}: {tests} stored before this. It stays marked running."
        )

    def _store_manifest(self, session: pytest.Session) -> None:
        if self.store is None or self._manifest_stored:
            return
        self._manifest_stored = True
        config = session.config
        files: list[tuple[Path, str]] = [(self.description.path, INTERFACE)]
        files += [(dbc, DBC) for dbc in self.description.dbc_files]
        files += [(self.bench_config.path, BENCH), (self.harness.path, HARNESS)]
        if config.inipath is not None:
            files.append((Path(config.inipath), INI))
        files += [(path, CONFTEST) for path in _conftest_files(config)]
        files += [(Path(item.path), TEST) for item in session.items]
        manifest = manifest_files(config.rootpath, files)

        def write(store: SQLiteStore, run: str) -> None:
            store.add_manifest_files(run, manifest)
            self._check_status = store.check_status(run)

        self._store("the manifest", write)

    def _store_identity(self, *, stop: bool = True) -> None:
        """Store the identity values collected since last time."""
        if self.bench is None:
            return
        values = self.bench.identity_values()
        new = values[self._identity_stored :]
        if self._store(
            "the DUT identity", lambda store, run: store.add_identity(run, new), stop=stop
        ):
            self._identity_stored = len(values)

    def _store_test(self, item: pytest.Item, *, stop: bool = True) -> None:
        reports = item.stash.get(_REPORTS, {})
        outcome, main = _outcome(reports)
        self._counts[_COUNTED_AS[outcome]] += 1
        if self.store is None:
            return
        self._test_seq += 1
        seq = item.stash[_SEQ] = self._test_seq
        details = main.longreprtext if main is not None and not main.passed else ""
        teardown = reports.get("teardown")
        if teardown is not None and teardown.failed and teardown is not main:
            details += f"\n\nTeardown also failed:\n{teardown.longreprtext}"
        last = teardown or main
        result = TestResult(
            seq=seq,
            nodeid=item.nodeid,
            file=shown_path(Path(item.path), item.config.rootpath),
            outcome=outcome,
            started_at=item.stash.get(_STARTED, time.time()),
            message=_short_message(main) if main is not None else None,
            details=details.strip() or None,
            durations={when: report.duration for when, report in reports.items()},
            properties=list(last.user_properties) if last is not None else [],
        )
        with self._lock:
            records = {
                phase: list(entries) for phase, entries in item.stash.get(_RECORD, {}).items()
            }

        def write(store: SQLiteStore, run: str) -> None:
            store.add_test(run, result)
            for phase in PHASES:
                store.add_record(run, records.get(phase, []), phase=phase, test_seq=seq)

        if self._store(item.nodeid, write, stop=stop):
            self._tests_stored += 1
        self._store_identity(stop=stop)

    # --- the bench --------------------------------------------------------------------------

    def open_bench(self) -> Bench:
        """The open bench, for the ``bench`` fixture. Fails the test if it can't be opened."""
        if self._open_error is not None:
            pytest.fail(self._open_error, pytrace=False)
        try:
            bench = self._open()
        except BenchTareError as exc:
            self._open_error = f"BenchTare couldn't open the bench.\n{exc}"
            pytest.fail(self._open_error, pytrace=False)
        if not self._identity_read:  # opened without the self-test: read the identity now
            self._identity_read = True
            bench.read_identity()
        return bench

    def _open(self) -> Bench:
        if self.bench is None:
            bench = Bench(self.description, self.bench_config, self.harness)
            bench.record_level = self.level
            bench.recorder = self._record
            bench.open()
            self.bench = bench
            equipment = bench.equipment() if self.store is not None else []
            self._store(
                "the bench equipment", lambda store, run: store.add_equipment(run, equipment)
            )
            for name, value in self._identify:
                bench.collect_identity(name, value, COMMAND_LINE)
        return self.bench

    def _record(self, entry: RecordEntry) -> None:
        with self._lock:
            if self._in_pretest:
                self.pretest_record.append(entry)
            elif self._item is not None and self._phase is not None:
                _phase_record(self._item, self._phase).append(entry)
            else:
                self._pending.append(entry)

    def pretest(self, config: pytest.Config) -> SelfTestResult:
        """What happens before the first test: open the bench, run the self-test, and if it
        passed, read the DUT's identity. Show the result, and store it."""
        with self._lock:
            self._in_pretest = True
        try:
            try:
                bench = self._open()
            except BenchTareError as exc:
                result = could_not_open(exc)
            else:
                result = run_selftest(bench)
                if result.passed:
                    self._identity_read = True
                    bench.read_identity()
        finally:
            with self._lock:
                self._in_pretest = False
        self.selftest = result
        _show_selftest(config, result, self.pretest_record)
        if result.passed and self.bench is not None and self.bench.identity:
            shown = ", ".join(f"{name} {value}" for name, value in self.bench.identity.items())
            _write(config, f"benchtare DUT identity: {shown}")
        unexpected = [entry for entry in self.pretest_record if entry.unexpected]
        _warn_unexpected(self._warn_at_description, "before the first test", unexpected)

        def write(store: SQLiteStore, run: str) -> None:
            store.add_selftest(run, result)
            store.add_record(run, self.pretest_record, phase="pretest")

        self._store("the self-test", write)
        self._store_identity()
        return result

    def _warn_at_description(self, warning: Warning) -> None:
        # No test to point at: point at the interface description the DUT is described by.
        warnings.warn_explicit(
            warning, type(warning), filename=str(self.description.path), lineno=1
        )

    def _finish_identity(self, stopped: bool) -> None:
        """At the end of the run: warn about listed identity items that were never collected,
        and store them as missing."""
        listed = self.description.identity
        bench = self.bench
        if bench is None:
            # The bench never opened, so only the command line gave anything.
            given = [
                IdentityValue(name, value, COMMAND_LINE, name in listed, self._started)
                for name, value in self._identify
            ]
            names = {name for name, _ in self._identify}
            missing = {name: "" for name in listed if name not in names}
        else:
            given = []
            missing = bench.missing_identity()
            if self._identity_read and not stopped:
                for name, note in missing.items():
                    warning = DutIdentityWarning(_missing_text(name, note, bench.identity))
                    self._warn_at_description(warning)
        self._store_identity(stop=False)
        gone = [IdentityValue(name, None, MISSING, True, None) for name in missing]
        self._store(
            "the DUT identity",
            lambda store, run: store.add_identity(run, [*given, *gone]),
            stop=False,
        )

    # --- the test check ---------------------------------------------------------------------

    def check_protocol(self, item: pytest.Item, nextitem: pytest.Item | None) -> bool:
        """Run a test as usual and, if it passed, walk through it a reading at a time: a
        control rerun with nothing corrupted, then a rerun per checked reading and mode with
        only that reading corrupted. Only the usual run is reported and counted."""
        ihook = item.ihook
        ihook.pytest_runtest_logstart(nodeid=item.nodeid, location=item.location)
        runtestprotocol(item, nextitem=nextitem, log=True)
        if self.results_failure is not None:
            self._stop_run()
        normal, _ = _outcome(item.stash.get(_REPORTS, {}))
        steps = [reading for reading in item.stash.get(_READINGS, []) if reading.checked]
        results: list[testcheck.ModeResult] = []
        records: dict[str, dict[str, list[RecordEntry]]] = {}
        why = ""
        if normal != "passed":
            why = f"it {normal}"
        elif "bench" not in getattr(item, "fixturenames", ()) or self.bench is None:
            why = "it doesn't use the bench"
        elif not steps:
            why = "it takes no checked reading in its body"
        else:
            control = self._rerun(item, nextitem, None, None)
            records[testcheck.CONTROL] = control.records
            results.append(_control_result(control, steps))
            if results[0].outcome == testcheck.PASSED:
                for step in steps:
                    assert step.index is not None
                    for mode in testcheck.MODES:
                        rerun = self._rerun(item, nextitem, mode, step.index)
                        records[f"reading {step.index} {mode}"] = rerun.records
                        results.append(_step_result(step, mode, rerun))
        verdict = testcheck.verdict(normal, results)
        self.checked.append(CheckedTest(item.nodeid, normal, verdict, tuple(results), why))
        seq = item.stash.get(_SEQ, None)
        if seq is not None:
            self._store(
                f"the test check of {item.nodeid}",
                lambda store, run: store.add_checks(run, seq, verdict, results, records),
            )
        ihook.pytest_runtest_logfinish(nodeid=item.nodeid, location=item.location)
        return True

    def _rerun(
        self,
        item: pytest.Item,
        nextitem: pytest.Item | None,
        mode: str | None,
        reading: int | None,
    ) -> _Rerun:
        """Run the test again, corrupting ``reading`` under ``mode`` (nothing for the control),
        unreported. Its fixtures are set up afresh."""
        item.stash[_REPORTS] = {}
        item.stash[_RECORD] = {}
        item.stash[_READINGS] = []
        self._rerun_of = (mode, reading)
        try:
            runtestprotocol(item, nextitem=nextitem, log=False)
        finally:
            self._rerun_of = None
        return _Rerun(
            dict(item.stash.get(_REPORTS, {})),
            {phase: list(entries) for phase, entries in item.stash.get(_RECORD, {}).items()},
            list(item.stash.get(_READINGS, [])),
        )

    def _show_checks(self, reporter: Any) -> None:
        verbose = reporter.config.getoption("verbose") > 0
        reporter.write_sep("=", "BenchTare test check")
        for test in self.checked:
            groups = testcheck.by_reading(test.results)
            if test.verdict == testcheck.ABLE:
                count = len(groups)
                each = "its reading" if count == 1 else f"each of its {count} readings"
                reporter.write_line(f"{test.nodeid}: able to fail at {each}")
            elif test.verdict == testcheck.UNABLE:
                unable = sum(
                    testcheck.reading_verdict(group) == testcheck.UNABLE
                    for group in groups.values()
                )
                where = "its reading" if len(groups) == 1 else f"{unable} of {len(groups)} readings"
                reporter.write_line(
                    f"{test.nodeid}: UNABLE TO FAIL at {where}", red=True, bold=True
                )
            elif test.verdict == testcheck.UNKNOWN:
                reporter.write_line(f"{test.nodeid}: inconclusive", yellow=True)
            else:
                reason = f", because {test.why}" if test.why else ""
                reporter.write_line(f"{test.nodeid}: not checked{reason}")
            for result in test.results:
                if result.mode == testcheck.CONTROL and (
                    verbose or result.outcome != testcheck.PASSED
                ):
                    reporter.write_line(f"    control: {result.outcome}, {result.detail}")
            for number, group in groups.items():
                reading_verdict = testcheck.reading_verdict(group)
                failed = [r.mode for r in group if r.outcome == testcheck.FAILED]
                missed = [
                    r for r in group if r.outcome in (testcheck.PASSED, testcheck.INCONCLUSIVE)
                ]
                if not verbose and not missed:
                    continue
                caught = f" ({', '.join(failed)})" if failed else ""
                reporter.write_line(
                    f"    reading {number}, {group[0].signal} ({group[0].where}): "
                    f"{reading_verdict}{caught}",
                    red=reading_verdict == testcheck.UNABLE,
                )
                for result in group if verbose else missed:
                    reporter.write_line(f"        {result.mode}: {result.outcome}, {result.detail}")
        counts = Counter(test.verdict for test in self.checked)
        order = (testcheck.ABLE, testcheck.UNABLE, testcheck.UNKNOWN, testcheck.NOT_CHECKED)
        summary = ", ".join(f"{counts[v]} {v}" for v in order if counts[v])
        reporter.write_line(f"Test check: {summary or 'no tests'}")

    def _show_check_status(self, reporter: Any) -> None:
        """For a usual run: which test files the test check hasn't covered as they are now."""
        never = [path for path, at, _ in self._check_status if at is None]
        changed = [path for path, at, same in self._check_status if at is not None and not same]
        if not self._check_status:
            return
        if not never and not changed:
            reporter.write_line(
                "benchtare test check: every test file was checked, and none has changed since"
            )
            return
        parts = []
        if changed:
            parts.append(f"changed since their last check: {', '.join(changed)}")
        if never:
            parts.append(f"never checked: {', '.join(never)}")
        reporter.write_line(
            f"benchtare test check: {'; '.join(parts)}. Run benchtare check-tests to check them."
        )

    # --- hooks ----------------------------------------------------------------------------

    def pytest_report_header(self, config: pytest.Config) -> list[str]:
        def shown(path: Path) -> str:
            try:
                return str(path.resolve().relative_to(config.rootpath.resolve()))
            except ValueError:
                return str(path)

        d, b, h = self.description, self.bench_config, self.harness
        lines = [
            (
                f"benchtare: DUT {d.name} ({shown(d.path)}), bench {b.name} ({shown(b.path)}), "
                f"harness {h.name} ({shown(h.path)}); test record: {self.level.label}"
            )
        ]
        if self.results_path is not None and self.run_id is not None:
            lines.append(f"benchtare results: {shown(self.results_path)}, run {self.run_id}")
        else:
            lines.append("benchtare results: not stored")
        return lines

    @pytest.hookimpl(tryfirst=True)
    def pytest_collection(self, session: pytest.Session) -> bool | None:
        if self.selftest_only:
            session.items = []
            return True  # collect nothing: only the self-test runs
        return None

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtestloop(self, session: pytest.Session) -> bool | None:
        config = session.config
        self._store_manifest(session)
        if self.selftest_only:
            self.pretest(config)
            return True
        if config.option.collectonly or session.testsfailed or not session.items:
            return None  # pytest reports these itself
        if not any("bench" in getattr(item, "fixturenames", ()) for item in session.items):
            _write(config, "benchtare self-test: not run, because no test uses the bench")
            return None
        if not self.pretest(config).passed:
            pytest.exit(
                "BenchTare's self-test failed, so no tests ran. What failed is listed above.",
                returncode=pytest.ExitCode.INTERRUPTED,
            )
        return None

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(self, item: pytest.Item) -> Generator[None, Any, None]:
        item.stash[_STARTED] = time.time()
        with self._lock:
            self._item = item
        try:
            yield
        finally:
            with self._lock:
                self._item = None

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_setup(self, item: pytest.Item) -> Generator[None, Any, None]:
        yield from self._in_phase(item, "setup")

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_call(self, item: pytest.Item) -> Generator[None, Any, None]:
        yield from self._in_phase(item, "call")

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_teardown(self, item: pytest.Item) -> Generator[None, Any, None]:
        yield from self._in_phase(item, "teardown")

    def _in_phase(self, item: pytest.Item, phase: str) -> Generator[None, Any, None]:
        with self._lock:
            self._phase = phase
            entries = _phase_record(item, phase)
            entries.extend(self._pending)
            self._pending.clear()
        # The test's body: log the readings it takes, its verification steps. The test check
        # corrupts one of them; staging and teardown read the real values, so the DUT is put
        # into the same state for every rerun.
        body = phase == "call" and self.bench is not None
        if body:
            assert self.bench is not None
            self.bench.readings = []
            if self._rerun_of is not None:
                self.bench.corruption, self.bench.corrupt_at = self._rerun_of
        try:
            yield
        finally:
            if body:
                assert self.bench is not None
                item.stash[_READINGS] = list(self.bench.readings)
                self.bench.corruption = self.bench.corrupt_at = None
            with self._lock:
                self._phase = None
                unexpected = [entry for entry in entries if entry.unexpected]
            # A warning for this test, so the run's summary lists it.
            _warn_unexpected(item.warn, f"during {phase}", unexpected)

    def pytest_assertion_pass(self, item: pytest.Item, lineno: int, orig: str, expl: str) -> None:
        """Passing asserts, when the ini file sets ``enable_assertion_pass_hook = true``."""
        self._record(
            RecordEntry(
                ASSERT,
                RecordLevel.RESULTS,
                f"assert {orig}: passed (line {lineno})",
                {"assertion": orig, "explanation": expl, "line": lineno},
            )
        )

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(
        self, item: pytest.Item, call: pytest.CallInfo[None]
    ) -> Generator[None, Any, None]:
        outcome = yield
        report = outcome.get_result()
        result = RecordEntry(
            RESULT,
            RecordLevel.RESULTS,
            _result_text(report),
            {"phase": report.when, "outcome": report.outcome},
        )
        with self._lock:
            records = item.stash.setdefault(_RECORD, {})
            records.setdefault(call.when, []).append(result)
            snapshot = {phase: list(entries) for phase, entries in records.items()}
        item.stash.setdefault(_REPORTS, {})[call.when] = report
        # Plain dicts, so the report can be serialised (for example by pytest-xdist).
        report.benchtare_record = [entry.as_dict() for entry in snapshot[call.when]]
        # Like pytest's captured output, a phase's report shows the record of the phases so far.
        for phase in PHASES[: PHASES.index(call.when) + 1]:
            entries = snapshot.get(phase, [])
            if any(entry.kind != RESULT for entry in entries):
                report.sections.append((f"BenchTare record {phase}", render(entries)))
        if self._rerun_of is not None:
            # A rerun of the test check: judged by check_protocol, not stored as a test.
            report.benchtare_nothing_arrived = call.excinfo is not None and (
                call.excinfo.errisinstance(ReadTimeout)
            )
            return
        if call.when == "teardown":
            # A hook wrapper shouldn't raise, so a test that couldn't be stored stops the run in
            # pytest_runtest_logfinish, as soon as pytest has reported the test.
            self._store_test(item, stop=False)

    def pytest_runtest_logfinish(self) -> None:
        if self.results_failure is not None:
            self._stop_run()

    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        if self.bench is not None:
            self.bench.close()
        with self._lock:
            self._after_last_test = [entry for entry in self._pending if entry.unexpected]
            self._pending.clear()
        _warn_unexpected(self._warn_at_description, "after the last test", self._after_last_test)
        if self.selftest_only and self.selftest is not None:
            # No tests were collected, which pytest would report as exit code 5.
            ok = self.selftest.passed
            session.exitstatus = pytest.ExitCode.OK if ok else pytest.ExitCode.TESTS_FAILED
        unable = any(test.verdict == testcheck.UNABLE for test in self.checked)
        if unable and session.exitstatus == pytest.ExitCode.OK:
            # A test that can't fail fails the test check.
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
        stopped = session.exitstatus == pytest.ExitCode.INTERRUPTED
        self._finish_identity(stopped)
        status = STOPPED if stopped else FINISHED

        def write(store: SQLiteStore, run: str) -> None:
            store.add_record(run, self._after_last_test, phase="after-last-test")
            store.finish_run(
                run, status=status, exit_code=int(session.exitstatus), counts=self._counts
            )

        self._store("the end of the run", write, stop=False)
        if self.store is not None:
            self.store.close()
            self.store = None
        if self.results_failure is not None:
            # Results that couldn't be stored fail the run, even when every test passed.
            session.exitstatus = pytest.ExitCode.INTERRUPTED
        self.bench = None

    def pytest_terminal_summary(self, terminalreporter: Any, exitstatus: int) -> None:
        if self._after_last_test:
            terminalreporter.write_sep("-", "BenchTare record after the last test")
            terminalreporter.write_line(render(self._after_last_test))
        if self.check_tests:
            self._show_checks(terminalreporter)
        elif self._counts and exitstatus != pytest.ExitCode.INTERRUPTED:
            self._show_check_status(terminalreporter)  # only after tests ran to the end
        if self.results_failure is not None and not self._results_failure_shown:
            self._show_results_failure(stopped=False)


@dataclass(frozen=True)
class CheckedTest:
    """One test's part in the test check."""

    nodeid: str
    outcome: str  # its usual outcome, as pytest's summary counts it
    verdict: str  # testcheck.ABLE, UNABLE, UNKNOWN or NOT_CHECKED
    results: tuple[testcheck.ModeResult, ...]
    why: str = ""  # why it wasn't checked


class _CheckRunner:
    """Runs each test through the test check. Registered only with --check-tests."""

    def __init__(self, session: BenchSession) -> None:
        self.session = session

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_protocol(self, item: pytest.Item, nextitem: pytest.Item | None) -> bool:
        return self.session.check_protocol(item, nextitem)


@dataclass(frozen=True)
class _Rerun:
    """What one rerun of the test check left: its reports, record and readings."""

    reports: dict[str, pytest.TestReport]
    records: dict[str, list[RecordEntry]]
    readings: list[testcheck.Reading]

    @property
    def taken(self) -> list[testcheck.Reading]:
        """The checked readings the body took, in order."""
        return [reading for reading in self.readings if reading.checked]


def _control_result(rerun: _Rerun, steps: Sequence[testcheck.Reading]) -> testcheck.ModeResult:
    """The control: the test, rerun unchanged, must pass and take the same readings."""

    def result(outcome: str, detail: str) -> testcheck.ModeResult:
        return testcheck.ModeResult(0, "", "", testcheck.CONTROL, outcome, detail)

    setup, call = rerun.reports.get("setup"), rerun.reports.get("call")
    if setup is None or not setup.passed or call is None:
        why = _short_message(setup) if setup is not None else None
        return result(
            testcheck.INCONCLUSIVE,
            f"staging didn't pass when the test was rerun unchanged: {why or 'no report'}",
        )
    if not call.passed:
        return result(
            testcheck.INCONCLUSIVE,
            f"the test {call.outcome} when rerun unchanged, at {_failed_at(call, rerun)}: "
            f"{_short_message(call)}. It isn't repeatable, so nothing the check finds could be "
            "trusted",
        )
    expected = [step.signal for step in steps]
    taken = [reading.signal for reading in rerun.taken]
    if taken != expected:
        return result(
            testcheck.INCONCLUSIVE,
            f"rerun unchanged, it read {', '.join(taken) or 'nothing'} instead of "
            f"{', '.join(expected)}: it doesn't take the same readings each time",
        )
    count = len(steps)
    return result(
        testcheck.PASSED,
        f"passed again unchanged, taking the same {count} reading{'' if count == 1 else 's'}",
    )


def _step_result(step: testcheck.Reading, mode: str, rerun: _Rerun) -> testcheck.ModeResult:
    """One reading under one mode. A failure counts only if it is that reading's doing: at the
    corrupted step or after it, with something corrupted, and not for want of a value."""
    assert step.index is not None

    def result(outcome: str, detail: str) -> testcheck.ModeResult:
        return testcheck.ModeResult(step.index or 0, step.signal, step.where, mode, outcome, detail)

    setup, call = rerun.reports.get("setup"), rerun.reports.get("call")
    if setup is None or not setup.passed or call is None:
        why = _short_message(setup) if setup is not None else None
        return result(
            testcheck.INCONCLUSIVE, f"staging didn't pass on the rerun: {why or 'no report'}"
        )
    taken = rerun.taken
    target = taken[step.index - 1] if len(taken) >= step.index else None
    if target is not None and target.signal != step.signal:
        return result(
            testcheck.INCONCLUSIVE,
            f"its reading {step.index} was {target.signal}, not {step.signal}: the test took "
            "different readings",
        )
    message = _short_message(call) or ""
    if call.skipped:
        return result(testcheck.INCONCLUSIVE, f"skipped: {message}")
    if call.failed:
        at = _failed_at(call, rerun)
        if target is None:
            return result(
                testcheck.INCONCLUSIVE,
                f"failed at {at} before reaching this reading, which the unchanged rerun "
                f"passed, so the failure isn't this reading's doing: {message}",
            )
        if getattr(call, "benchtare_nothing_arrived", False):
            return result(testcheck.INCONCLUSIVE, f"nothing arrived, at {at}: {message}")
        if not target.corrupted:
            return result(
                testcheck.INCONCLUSIVE,
                f"failed at {at} with nothing corrupted, so the test may be flaky: {message}",
            )
        if len(taken) == step.index:
            return result(testcheck.FAILED, f"failed at {at}: {message}")
        return result(
            testcheck.FAILED, f"failed later, at {at}, after reading {len(taken)}: {message}"
        )
    if target is None:
        return result(
            testcheck.INCONCLUSIVE, "the rerun never took this reading: it took different ones"
        )
    if not target.corrupted:
        return result(testcheck.NOT_APPLIED, target.note or "nothing could be changed")
    given = [entry for entry in rerun.records.get("call", []) if "corruption" in entry.data]
    shown = "; ".join(_corrupted_reading(entry) for entry in given)
    return result(testcheck.PASSED, f"reading {shown}")


def _failed_at(report: pytest.TestReport, rerun: _Rerun) -> str:
    """The test's line where a rerun failed: where pytest says it crashed, unless that is
    inside BenchTare, then the step it was taking."""
    crash = getattr(report.longrepr, "reprcrash", None)
    path = getattr(crash, "path", None)
    if path is not None and not _inside_benchtare(str(path)):
        return f"{Path(str(path)).name}:{crash.lineno}"
    return rerun.readings[-1].where if rerun.readings else "an unknown line"


def _inside_benchtare(path: str) -> bool:
    package = os.path.normcase(str(Path(__file__).resolve().parent)) + os.sep
    return os.path.normcase(os.path.abspath(path)).startswith(package)


def _corrupted_reading(entry: RecordEntry) -> str:
    """What a rerun gave the test instead of what arrived."""
    unit = entry.data.get("unit")

    def shown(value: Any) -> str:
        return f"{value!r} {unit}" if unit else repr(value)

    signal, given, arrived = (entry.data.get(key) for key in ("signal", "value", "received"))
    if entry.data.get("corruption") == testcheck.STALE:
        if given == arrived:
            return (
                f"{signal} {shown(given)} from before the write, the same as what arrived "
                "after it, so it can't tell whether the DUT responded"
            )
        return f"{signal} {shown(given)} from before the write, not the {shown(arrived)} after it"
    return f"{signal} {shown(given)} instead of the {shown(arrived)} that arrived"


def _write(config: pytest.Config, line: str, **markup: bool) -> None:
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(line, **markup)


def _show_selftest(
    config: pytest.Config, result: SelfTestResult, record: Sequence[RecordEntry]
) -> None:
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:
        return
    verbose = config.getoption("verbose") > 0
    took = f"in {result.duration:.2f} s"
    if result.passed:
        if verbose or config.getoption("benchtare_selftest_only"):
            reporter.write_sep("=", f"BenchTare self-test passed {took}", green=True)
            for line in result.lines():
                reporter.write_line(line)
        else:
            reporter.write_line(
                f"benchtare self-test passed: {result.summary()} {took}", green=True
            )
        return
    reporter.write_sep("=", f"BenchTare self-test failed {took}", red=True)
    for line in result.lines():
        reporter.write_line(line, red=line.startswith(("FAILED", "        Check:")))
    if record:
        reporter.write_sep("-", "BenchTare record self-test")
        reporter.write_line(render(record))


def _phase_record(item: pytest.Item, phase: str) -> list[RecordEntry]:
    return item.stash.setdefault(_RECORD, {}).setdefault(phase, [])


def _warn_unexpected(
    warn: Callable[[Warning], None], during: str, entries: Sequence[RecordEntry]
) -> None:
    """One warning for the frames that don't match the DBC, and one for each other event."""
    mismatches = [entry for entry in entries if entry.kind == FRAME_MISMATCH]
    if mismatches:
        verb = "doesn't" if len(mismatches) == 1 else "don't"
        warn(
            FrameMismatchWarning(
                f"the DUT sent {count(len(mismatches), 'CAN frame')} that {verb} match the DBC "
                f"{during}. The first: {mismatches[0].text}. Every one is in the BenchTare record."
            )
        )
    for entry in entries:
        if entry.kind == EQUIPMENT_FAILURE:
            warn(EquipmentWarning(f"{during}: {entry.text}"))
        elif entry.kind == IDENTITY_CHANGE:
            warn(DutIdentityWarning(f"{during}: {entry.text}"))


def _result_text(report: pytest.TestReport) -> str:
    text = f"{report.when} {report.outcome}"
    if report.passed:
        return text
    longrepr = report.longrepr
    if isinstance(longrepr, tuple):  # a skip: (path, line, reason)
        message = str(longrepr[2])
    elif getattr(longrepr, "reprcrash", None) is not None:
        message = longrepr.reprcrash.message  # type: ignore[union-attr]
    else:
        message = str(longrepr or "")
    first_line = message.strip().splitlines()[0] if message.strip() else ""
    return f"{text}: {first_line}" if first_line else text


def _conftest_files(config: pytest.Config) -> list[Path]:
    """The conftest.py files pytest loaded."""
    found = []
    for plugin in config.pluginmanager.get_plugins():
        file = getattr(plugin, "__file__", None)
        if file and Path(file).name == "conftest.py":
            found.append(Path(file))
    return sorted(set(found))


def _outcome(reports: dict[str, pytest.TestReport]) -> tuple[str, pytest.TestReport | None]:
    """A test's outcome as pytest's summary counts it, and the report that decided it."""
    setup, call, teardown = (reports.get(when) for when in PHASES)
    if setup is not None and setup.failed:
        return "error", setup
    if setup is not None and setup.skipped:
        return ("xfailed" if hasattr(setup, "wasxfail") else "skipped"), setup
    if call is not None:
        if call.failed:
            return "failed", call
        if call.skipped:
            return ("xfailed" if hasattr(call, "wasxfail") else "skipped"), call
    if teardown is not None and teardown.failed:
        return "error", teardown
    if call is not None and hasattr(call, "wasxfail"):
        return "xpassed", call
    return "passed", call


def _short_message(report: pytest.TestReport) -> str | None:
    """The one-line reason for a failure, error or skip; None for a pass."""
    text = _result_text(report)
    _, _, message = text.partition(": ")
    return message or None


def _missing_text(name: str, note: str, collected: Iterable[str]) -> str:
    text = f"the DUT's {name} was never collected during this run"
    if note:
        text += f" ({note})"
    text += "; it is stored as missing."
    close = difflib.get_close_matches(name, list(collected), n=1, cutoff=0.6)
    if close:
        text += f" '{close[0]}' was recorded instead: is that a misspelling of '{name}'?"
    return text
