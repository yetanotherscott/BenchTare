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
from collections.abc import Callable, Generator, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from _pytest.runner import runtestprotocol  # what plugins that rerun a test use

from benchtare import __version__, hookspecs, hygiene, testcheck
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
    HYGIENE_CHECK,
    STOPPED,
    TEST_CHECK,
    RunInfo,
    SQLiteStore,
    TestResult,
)
from benchtare.selftest import SelfTestResult, could_not_open, run_selftest
from benchtare.selftest import checks as selftest_checks
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
        "--check-hygiene",
        dest="benchtare_check_hygiene",
        action="store_true",
        default=False,
        help="the hygiene check: run the tests, then check that each one that passed repeats, "
        "leaves the DUT's clean_state as it found it, and starts from a meaningful state "
        "(this is what 'benchtare check-hygiene' does)",
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


def pytest_addhooks(pluginmanager: pytest.PytestPluginManager) -> None:
    pluginmanager.add_hookspecs(hookspecs)


def pytest_configure(config: pytest.Config) -> None:
    paths = configured_paths(config)
    checking = config.getoption("benchtare_check_tests") or config.getoption(
        "benchtare_check_hygiene"
    )
    chosen = [
        option
        for option, dest in (
            ("--check-tests", "benchtare_check_tests"),
            ("--check-hygiene", "benchtare_check_hygiene"),
            ("--selftest-only", "benchtare_selftest_only"),
        )
        if config.getoption(dest)
    ]
    if len(chosen) > 1:
        raise pytest.UsageError(
            f"BenchTare: {' and '.join(chosen)} can't be used together: each check runs on its "
            "own, so its verdict stands on its own (and the self-test runs before every check "
            "anyway)."
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
        check_hygiene=bool(config.getoption("benchtare_check_hygiene")),
        results=config.stash[_RESULTS],
        identify=config.stash[_IDENTIFY],
    )
    bench_session.start_run(config)
    config.pluginmanager.register(bench_session, SESSION_PLUGIN)
    if bench_session.check_tests:
        config.pluginmanager.register(_CheckRunner(bench_session), "benchtare-check-tests")
    if bench_session.check_hygiene:
        config.pluginmanager.register(_HygieneRunner(bench_session), "benchtare-check-hygiene")


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
        check_hygiene: bool = False,
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
        # The hygiene check (benchtare.hygiene): each test's verdict so far.
        self.check_hygiene = check_hygiene
        self.hygiene_checked: list[HygieneTest] = []
        # Each clean_state: item's value as the session first found it, before any test's setup.
        self._clean_reference: dict[str, Any] = {}
        self._hygiene_status: list[tuple[str, str | None, bool | None]] = []
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
            kind=(
                "selftest"
                if self.selftest_only
                else TEST_CHECK
                if self.check_tests
                else HYGIENE_CHECK
                if self.check_hygiene
                else "tests"
            ),
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
            self._hygiene_status = store.check_status(run, HYGIENE_CHECK)

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
            plugins = [plugin.record() for plugin in self.bench_config.plugins()]
            self._store(
                "the bench equipment", lambda store, run: store.add_equipment(run, equipment)
            )
            self._store(
                "the backends the bench uses", lambda store, run: store.add_plugins(run, plugins)
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
            reruns = [(step, mode) for step in steps for mode in testcheck.MODES]
            for step, mode in reruns:
                if results[-1].outcome == testcheck.NOT_REPEATABLE:
                    break  # test hygiene: nothing further would prove anything
                assert step.index is not None
                rerun = self._rerun(item, nextitem, mode, step.index)
                records[f"reading {step.index} {mode}"] = rerun.records
                results.append(_step_result(step, mode, rerun))
            unrepeatable = [r for r in results if r.outcome == testcheck.NOT_REPEATABLE]
            if unrepeatable:
                why = f"of test hygiene: {unrepeatable[0].detail}"
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
                reporter.write_line(f"{test.nodeid}: not checked{reason}", yellow=test.hygiene)
                if test.hygiene and not verbose:
                    continue  # the reason says it all; -v shows each rerun
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
        parts = [f"{counts[v]} {v}" for v in order if counts[v]]
        hygiene = sum(test.hygiene for test in self.checked)
        if hygiene:
            parts[-1] += f" ({hygiene} because of test hygiene)"
        reporter.write_line(f"Test check: {', '.join(parts) or 'no tests'}")

    def _show_check_status(self, reporter: Any) -> None:
        """For a usual run: which test files each on-demand check hasn't covered as they are
        now, one line per area."""
        for area, command, status in (
            ("test check", "check-tests", self._check_status),
            ("hygiene check", "check-hygiene", self._hygiene_status),
        ):
            never = [path for path, at, _ in status if at is None]
            changed = [path for path, at, same in status if at is not None and not same]
            if not status:
                continue
            if not never and not changed:
                reporter.write_line(
                    f"benchtare {area}: every test file was checked, and none has changed since"
                )
                continue
            parts = []
            if changed:
                parts.append(f"changed since their last check: {', '.join(changed)}")
            if never:
                parts.append(f"never checked: {', '.join(never)}")
            reporter.write_line(
                f"benchtare {area}: {'; '.join(parts)}. Run benchtare {command} to check them."
            )

    # --- the hygiene check --------------------------------------------------------------------

    def hygiene_protocol(self, item: pytest.Item, nextitem: pytest.Item | None) -> bool:
        """Run a test as usual and, if it passed, check its hygiene: compare the clean-state
        items around that run and around a repeat run straight after it, and look at its waits.
        Only the usual run is reported and counted."""
        ihook = item.ihook
        ihook.pytest_runtest_logstart(nodeid=item.nodeid, location=item.location)
        uses_bench = "bench" in getattr(item, "fixturenames", ()) and self.bench is not None
        before = self._clean_values() if uses_bench else {}
        runtestprotocol(item, nextitem=nextitem, log=True)
        if self.results_failure is not None:
            self._stop_run()
        normal, _ = _outcome(item.stash.get(_REPORTS, {}))
        rules: list[hygiene.RuleResult] = []
        items: list[hygiene.ItemResult] = []
        records: dict[str, dict[str, list[RecordEntry]]] = {}
        why = ""
        if normal != "passed":
            why = f"it {normal}"
        elif not uses_bench:
            why = "it doesn't use the bench"
        else:
            readings = list(item.stash.get(_READINGS, []))
            items += self._clean_after(1, before)  # straight after its teardown
            before = self._clean_values()
            repeat = self._rerun(item, nextitem, None, None)
            records["repeat"] = repeat.records
            items += self._clean_after(2, before)
            rules = [
                _repeats(readings, repeat),
                _left_clean(self.description.clean_state, items),
                self._meaningful(readings, repeat.readings),
            ]
        verdict = hygiene.verdict(normal, rules)
        self.hygiene_checked.append(
            HygieneTest(item.nodeid, normal, verdict, tuple(rules), tuple(items), why)
        )
        seq = item.stash.get(_SEQ, None)
        if seq is not None:
            self._store(
                f"the hygiene check of {item.nodeid}",
                lambda store, run: store.add_hygiene(run, seq, verdict, rules, items, records),
            )
        ihook.pytest_runtest_logfinish(nodeid=item.nodeid, location=item.location)
        return True

    def _clean_values(self) -> dict[str, Any]:
        """Each clean_state: item's value now."""
        probes, _ = self._probe()
        return {
            item.name: self._current(item, probes) for item in self.description.clean_state.values()
        }

    def _current(self, item: hygiene.CleanStateItem, probes: Mapping[str, Any]) -> Any:
        assert self.bench is not None
        if item.kind == hygiene.OUTPUT:
            last = self.bench.last_received(item.name)
            return hygiene.MISSING if last is None else last.value
        if item.kind == hygiene.INPUT:
            return self.bench.written.get(item.name, hygiene.MISSING)
        return probes.get(item.name, hygiene.MISSING)

    def _probe(self) -> tuple[dict[str, Any], str]:
        """What the team's pytest_benchtare_state hooks return, merged; and, if one failed,
        why."""
        wanted = [i for i in self.description.clean_state.values() if i.kind == hygiene.PROBE]
        if not wanted or self._config is None:
            return {}, ""
        try:
            results = self._config.hook.pytest_benchtare_state(
                bench=self.bench, config=self._config
            )
        except Exception as exc:  # noqa: BLE001 - the team's hook: report it, don't stop the run
            return {}, f"the state probe failed: {type(exc).__name__}: {exc}"
        merged: dict[str, Any] = {}
        for result in results:
            merged.update(result or {})
        return merged, ""

    def _clean_after(self, run: int, before: Mapping[str, Any]) -> list[hygiene.ItemResult]:
        """Judge each clean_state: item after teardown: at its clean value, or back where it
        was, given its settle time. Only a run that left it dirty is blamed."""
        for name, value in before.items():
            if value is not hygiene.MISSING:
                self._clean_reference.setdefault(name, value)  # as the session first found it
        results = []
        for item in self.description.clean_state.values():

            def read(item: hygiene.CleanStateItem = item) -> Any:
                probes = self._probe()[0] if item.kind == hygiene.PROBE else {}
                return self._current(item, probes)

            outcome, after, detail = hygiene.judge(
                item if item.kind != hygiene.INPUT else replace(item, settle=0.0),
                before.get(item.name, hygiene.MISSING),
                read,
                reference=self._clean_reference.get(item.name, hygiene.MISSING),
                unknown=self._why_not_known(item),
                shown=lambda value, name=item.name: self._shown(name, value),
            )
            results.append(
                hygiene.ItemResult(
                    run,
                    item.name,
                    item.kind,
                    before.get(item.name, hygiene.MISSING),
                    after,
                    outcome,
                    detail,
                )
            )
        return results

    def _why_not_known(self, item: hygiene.CleanStateItem) -> str:
        if item.kind == hygiene.OUTPUT:
            return "no value of it had arrived, so it couldn't be compared"
        if item.kind == hygiene.INPUT:
            return (
                "it hadn't been written, so its value isn't known: write it in staging, so "
                "every test starts from a known value"
            )
        _, failed = self._probe()
        return failed or "no pytest_benchtare_state hook returned it"

    def _shown(self, name: str, value: Any) -> str:
        signal = self.description.signals.get(name)
        unit = signal.unit if signal is not None else None
        return f"{value!r} {unit}" if unit and value is not hygiene.MISSING else repr(value)

    def _meaningful(
        self, first: Sequence[testcheck.Reading], repeat: Sequence[testcheck.Reading]
    ) -> hygiene.RuleResult:
        """No checked wait was already satisfied before the write it followed."""
        waits = [(run, r) for run, taken in ((1, first), (2, repeat)) for r in taken]
        waits = [(run, r) for run, r in waits if r.checked and r.waits]
        if not waits:
            return hygiene.RuleResult(
                hygiene.MEANINGFUL, hygiene.NOT_CHECKED, "it has no wait_until in its body"
            )
        already = [(run, r) for run, r in waits if r.already]
        if not already:
            count = len([r for run, r in waits if run == 1])
            return hygiene.RuleResult(
                hygiene.MEANINGFUL,
                hygiene.PASSED,
                f"none of its {count} wait{'' if count == 1 else 's'} was already satisfied "
                "before the write it followed",
            )
        run, reading = already[0]
        when = "" if run == 1 else "run again straight after itself, "
        return hygiene.RuleResult(
            hygiene.MEANINGFUL,
            hygiene.FAILED,
            f"{when}its wait for {reading.signal} at {reading.where} was already satisfied "
            f"before the write of {reading.after_write}: {reading.signal} was already "
            f"{self._shown(reading.signal, reading.before)}, so the test can't tell whether the "
            "DUT responded",
        )

    def _show_hygiene(self, reporter: Any) -> None:
        verbose = reporter.config.getoption("verbose") > 0
        reporter.write_sep("=", "BenchTare hygiene check")
        for test in self.hygiene_checked:
            if test.verdict == hygiene.IS_CLEAN:
                reporter.write_line(f"{test.nodeid}: clean")
            elif test.verdict == hygiene.NOT_CLEAN:
                reporter.write_line(f"{test.nodeid}: NOT CLEAN", red=True, bold=True)
            else:
                reason = f", because {test.why}" if test.why else ""
                reporter.write_line(f"{test.nodeid}: not checked{reason}")
            for rule in test.rules:
                if verbose or rule.outcome == hygiene.FAILED:
                    reporter.write_line(
                        f"    {hygiene.SAID[rule.rule]}: {rule.outcome}, {rule.detail}",
                        red=rule.outcome == hygiene.FAILED,
                    )
            if verbose:
                for result in test.items:
                    reporter.write_line(
                        f"        run {result.run}, {result.name}: {result.outcome}, "
                        f"{result.detail}"
                    )
        counts = Counter(test.verdict for test in self.hygiene_checked)
        order = (hygiene.IS_CLEAN, hygiene.NOT_CLEAN, hygiene.NOT_CHECKED)
        summary = ", ".join(f"{counts[v]} {v}" for v in order if counts[v])
        reporter.write_line(f"Hygiene check: {summary or 'no tests'}")

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
        result = self.pretest(config)
        if not result.passed:
            pytest.exit(_selftest_failed(result), returncode=pytest.ExitCode.INTERRUPTED)
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
        unclean = any(test.verdict == hygiene.NOT_CLEAN for test in self.hygiene_checked)
        if (unable or unclean) and session.exitstatus == pytest.ExitCode.OK:
            # A test that can't fail fails the test check; one that isn't clean, the hygiene
            # check.
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
        elif self.check_hygiene:
            self._show_hygiene(terminalreporter)
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

    @property
    def hygiene(self) -> bool:
        """It wasn't checked because it doesn't repeat: a test hygiene problem."""
        return any(r.outcome == testcheck.NOT_REPEATABLE for r in self.results)


@dataclass(frozen=True)
class HygieneTest:
    """One test's part in the hygiene check."""

    nodeid: str
    outcome: str  # its usual outcome, as pytest's summary counts it
    verdict: str  # hygiene.IS_CLEAN, NOT_CLEAN or NOT_CHECKED
    rules: tuple[hygiene.RuleResult, ...]
    items: tuple[hygiene.ItemResult, ...]
    why: str = ""  # why it wasn't checked


class _HygieneRunner:
    """Runs each test through the hygiene check. Registered only with --check-hygiene."""

    def __init__(self, session: BenchSession) -> None:
        self.session = session

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_protocol(self, item: pytest.Item, nextitem: pytest.Item | None) -> bool:
        return self.session.hygiene_protocol(item, nextitem)


def _repeats(readings: Sequence[testcheck.Reading], repeat: _Rerun) -> hygiene.RuleResult:
    """Run again straight after itself, the test passes and takes the same readings."""

    def result(outcome: str, detail: str) -> hygiene.RuleResult:
        return hygiene.RuleResult(hygiene.REPEATS, outcome, detail)

    setup, call, teardown = (repeat.reports.get(phase) for phase in PHASES)
    if setup is None or not setup.passed or call is None:
        why = _short_message(setup) if setup is not None else None
        return result(
            hygiene.FAILED,
            f"its staging didn't pass when it ran again straight after itself: "
            f"{why or 'no report'}",
        )
    if not call.passed:
        return result(
            hygiene.FAILED,
            f"it {call.outcome} when it ran again straight after itself, at "
            f"{_failed_at(call, repeat)}: {_short_message(call)}",
        )
    if teardown is not None and teardown.failed:
        return result(
            hygiene.FAILED,
            f"its teardown failed when it ran again straight after itself: "
            f"{_short_message(teardown)}",
        )
    first = [r.signal for r in readings if r.checked]
    again = [r.signal for r in repeat.taken]
    if again != first:
        return result(
            hygiene.FAILED,
            f"run again straight after itself, it read {', '.join(again) or 'nothing'} instead "
            f"of {', '.join(first) or 'nothing'}",
        )
    count = len(first)
    return result(
        hygiene.PASSED,
        f"it passed again straight after itself, taking the same {count} "
        f"reading{'' if count == 1 else 's'}",
    )


def _left_clean(
    clean_state: Mapping[str, hygiene.CleanStateItem], items: Sequence[hygiene.ItemResult]
) -> hygiene.RuleResult:
    """After each run's teardown, every clean_state: item is clean, or no dirtier than this test
    found it."""

    def result(outcome: str, detail: str) -> hygiene.RuleResult:
        return hygiene.RuleResult(hygiene.CLEAN, outcome, detail)

    if not clean_state:
        return result(
            hygiene.NOT_CHECKED,
            "the interface description doesn't list what shows the DUT is clean (clean_state:)",
        )

    # Per item: a change in either run counts against the test. Being clean needs evidence: for
    # an item with a clean value, either run; without one, only the first, since the repeat run
    # starts from this test's own end state, so "back where it was" says nothing there.
    changed: list[hygiene.ItemResult] = []
    clean: list[str] = []
    notes: list[str] = []
    for name, spec in clean_state.items():
        mine = [item for item in items if item.name == name]
        dirty = [item for item in mine if item.outcome == hygiene.CHANGED]
        if dirty:
            changed.append(dirty[0])
            continue
        evidence = mine if spec.has_clean else [item for item in mine if item.run == 1]
        if any(item.outcome == hygiene.SAME for item in evidence):
            clean.append(name)
            continue
        unjudged = [i for i in mine if i.outcome in (hygiene.INHERITED, hygiene.NOT_COMPARED)]
        if unjudged:
            notes.append(unjudged[0].detail)
    if changed:
        return result(hygiene.FAILED, "; ".join(item.detail for item in changed))
    note = "; ".join(notes)
    if not clean:
        return result(hygiene.NOT_CHECKED, f"nothing it left could be judged: {note}")
    detail = f"{', '.join(sorted(clean))} clean after teardown"
    return result(hygiene.PASSED, f"{detail}; not counted: {note}" if note else detail)


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
    """The control: the test, run again unchanged, must pass and take the same readings. If
    it doesn't, it doesn't repeat: a test hygiene problem, and its logic can't be checked."""

    def result(outcome: str, detail: str) -> testcheck.ModeResult:
        return testcheck.ModeResult(0, "", "", testcheck.CONTROL, outcome, detail)

    setup, call = rerun.reports.get("setup"), rerun.reports.get("call")
    if setup is None or not setup.passed or call is None:
        why = _short_message(setup) if setup is not None else None
        return result(
            testcheck.NOT_REPEATABLE,
            f"its staging didn't pass when it ran again unchanged: {why or 'no report'}",
        )
    if not call.passed:
        return result(
            testcheck.NOT_REPEATABLE,
            f"it {call.outcome} when it ran again unchanged, at {_failed_at(call, rerun)}: "
            f"{_short_message(call)}",
        )
    expected = [step.signal for step in steps]
    taken = [reading.signal for reading in rerun.taken]
    if taken != expected:
        return result(
            testcheck.NOT_REPEATABLE,
            f"run again unchanged, it read {', '.join(taken) or 'nothing'} instead of "
            f"{', '.join(expected)}",
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
            testcheck.NOT_REPEATABLE,
            f"its staging didn't pass on the rerun for reading {step.index}: {why or 'no report'}",
        )
    taken = rerun.taken
    target = taken[step.index - 1] if len(taken) >= step.index else None
    if target is not None and target.signal != step.signal:
        return result(
            testcheck.NOT_REPEATABLE,
            f"on the rerun for reading {step.index}, that reading was {target.signal}, not "
            f"{step.signal}",
        )
    message = _short_message(call) or ""
    if call.skipped:
        return result(testcheck.INCONCLUSIVE, f"skipped: {message}")
    if call.failed:
        at = _failed_at(call, rerun)
        if target is None:
            return result(
                testcheck.NOT_REPEATABLE,
                f"on the rerun for reading {step.index}, it failed at {at} before reaching "
                f"that reading, where it passed unchanged: {message}",
            )
        if getattr(call, "benchtare_nothing_arrived", False):
            return result(testcheck.INCONCLUSIVE, f"nothing arrived, at {at}: {message}")
        if not target.corrupted:
            return result(
                testcheck.NOT_REPEATABLE,
                f"on the rerun for reading {step.index}, it failed at {at} with nothing "
                f"corrupted: {message}",
            )
        if len(taken) == step.index:
            return result(testcheck.FAILED, f"failed at {at}: {message}")
        return result(
            testcheck.FAILED, f"failed later, at {at}, after reading {len(taken)}: {message}"
        )
    if target is None:
        return result(
            testcheck.NOT_REPEATABLE,
            f"on the rerun for reading {step.index}, it never took that reading",
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
    """The bench check and the harness check, each with its own verdict."""
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:
        return
    full = (
        not result.passed
        or config.getoption("verbose") > 0
        or config.getoption("benchtare_selftest_only")
    )
    for area in selftest_checks.AREAS:
        outcome = result.outcome_of(area)
        skipped = outcome == selftest_checks.SKIPPED
        word = outcome
        if skipped:  # because the bench check failed
            ran = any(check.outcome == selftest_checks.PASSED for check in result.checks_of(area))
            word = "not complete" if ran else "not run"
        colour = {
            "green": outcome == selftest_checks.PASSED,
            "red": outcome == selftest_checks.FAILED,
            "yellow": skipped,
        }
        if not full:
            reporter.write_line(f"benchtare {area} check {word}: {result.summary(area)}", **colour)
            continue
        reporter.write_sep("=", f"BenchTare {area} check {word}", **colour)
        checks = result.checks_of(area)
        if not checks and area == selftest_checks.HARNESS:
            reporter.write_line("not checked, because the bench check failed")
        for check in checks:
            for line in check.lines():
                reporter.write_line(line, red=line.startswith(("FAILED", "        Check:")))
    if not result.passed and record:
        reporter.write_sep("-", "BenchTare record self-test")
        reporter.write_line(render(record))


def _selftest_failed(result: SelfTestResult) -> str:
    """Why no tests ran: which of the self-test's checks failed."""
    failed = " and ".join(f"{area} check" for area in result.failed_areas()) or "self-test"
    return f"BenchTare's {failed} failed, so no tests ran. What failed is listed above."


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
