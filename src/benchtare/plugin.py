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
"""

from __future__ import annotations

import threading
import warnings
from collections.abc import Callable, Generator, Sequence
from pathlib import Path
from typing import Any

import pytest

from benchtare._yaml import count
from benchtare.bench.config import BenchConfig, load_bench_file
from benchtare.bench.runtime import Bench
from benchtare.dut.description import InterfaceDescription, load_interface_description
from benchtare.errors import BenchTareError, ConfigError, EquipmentWarning, FrameMismatchWarning
from benchtare.harness.config import HarnessConfig, load_harness_file
from benchtare.record import (
    ASSERT,
    DEFAULT_LEVEL,
    EQUIPMENT_FAILURE,
    FRAME_MISMATCH,
    LEVEL_NAMES,
    RESULT,
    RecordEntry,
    RecordLevel,
    render,
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
_PATHS = pytest.StashKey[dict[str, Path]]()
_LEVEL = pytest.StashKey[RecordLevel]()

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
        "--selftest-only",
        dest="benchtare_selftest_only",
        action="store_true",
        default=False,
        help="run BenchTare's self-test of the bench and harness, then stop without running "
        "any test (this is what 'benchtare selftest' does)",
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


def pytest_configure(config: pytest.Config) -> None:
    paths = configured_paths(config)
    if paths is None:
        if config.getoption("benchtare_selftest_only"):
            raise pytest.UsageError(
                "There's nothing to self-test: BenchTare isn't configured.\n"
                + NOT_CONFIGURED.split("\n", 1)[1]
            )
        return
    config.stash[_PATHS] = paths
    config.stash[_LEVEL] = configured_level(config)


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
    level = session.config.stash[_LEVEL]
    selftest_only = bool(session.config.getoption("benchtare_selftest_only"))
    session.config.pluginmanager.register(
        BenchSession(description, bench_config, harness, level, selftest_only=selftest_only),
        SESSION_PLUGIN,
    )


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
    """Registered as a plugin only when BenchTare is configured. Owns the bench for the run, and
    keeps each test's record."""

    def __init__(
        self,
        description: InterfaceDescription,
        bench: BenchConfig,
        harness: HarnessConfig,
        level: RecordLevel = DEFAULT_LEVEL,
        *,
        selftest_only: bool = False,
    ) -> None:
        self.description = description
        self.bench_config = bench
        self.harness = harness
        self.level = level
        self.selftest_only = selftest_only
        self.bench: Bench | None = None
        self.selftest: SelfTestResult | None = None  # kept for the results (M5)
        self.selftest_record: list[RecordEntry] = []
        self._open_error: str | None = None
        # Entries arrive from receive threads as well as from the test, so all of this is
        # guarded by the lock.
        self._lock = threading.Lock()
        self._item: pytest.Item | None = None
        self._phase: str | None = None
        self._pending: list[RecordEntry] = []  # arrived between phases: they go to the next one
        self._in_selftest = False  # entries then go to the self-test's record
        self._after_last_test: list[RecordEntry] = []

    def open_bench(self) -> Bench:
        """The open bench, for the ``bench`` fixture. Fails the test if it can't be opened."""
        if self._open_error is not None:
            pytest.fail(self._open_error, pytrace=False)
        try:
            return self._open()
        except BenchTareError as exc:
            self._open_error = f"BenchTare couldn't open the bench.\n{exc}"
            pytest.fail(self._open_error, pytrace=False)

    def _open(self) -> Bench:
        if self.bench is None:
            bench = Bench(self.description, self.bench_config, self.harness)
            bench.record_level = self.level
            bench.recorder = self._record
            bench.open()
            self.bench = bench
        return self.bench

    def _record(self, entry: RecordEntry) -> None:
        with self._lock:
            if self._in_selftest:
                self.selftest_record.append(entry)
            elif self._item is not None and self._phase is not None:
                _phase_record(self._item, self._phase).append(entry)
            else:
                self._pending.append(entry)

    def run_selftest(self, config: pytest.Config) -> SelfTestResult:
        """Open the bench and run the self-test, and show the result."""
        with self._lock:
            self._in_selftest = True
        try:
            try:
                bench = self._open()
            except BenchTareError as exc:
                result = could_not_open(exc)
            else:
                result = run_selftest(bench)
        finally:
            with self._lock:
                self._in_selftest = False
        self.selftest = result
        _show_selftest(config, result, self.selftest_record)
        unexpected = [entry for entry in self.selftest_record if entry.unexpected]
        _warn_unexpected(self._warn_at_description, "during the self-test", unexpected)
        return result

    def _warn_at_description(self, warning: Warning) -> None:
        # No test to point at: point at the interface description the DUT is described by.
        warnings.warn_explicit(
            warning, type(warning), filename=str(self.description.path), lineno=1
        )

    # --- hooks ----------------------------------------------------------------------------

    def pytest_report_header(self, config: pytest.Config) -> list[str]:
        def shown(path: Path) -> str:
            try:
                return str(path.resolve().relative_to(config.rootpath.resolve()))
            except ValueError:
                return str(path)

        d, b, h = self.description, self.bench_config, self.harness
        line = (
            f"benchtare: DUT {d.name} ({shown(d.path)}), bench {b.name} ({shown(b.path)}), "
            f"harness {h.name} ({shown(h.path)}); test record: {self.level.label}"
        )
        return [line]

    @pytest.hookimpl(tryfirst=True)
    def pytest_collection(self, session: pytest.Session) -> bool | None:
        if self.selftest_only:
            session.items = []
            return True  # collect nothing: only the self-test runs
        return None

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtestloop(self, session: pytest.Session) -> bool | None:
        config = session.config
        if self.selftest_only:
            self.run_selftest(config)
            return True
        if config.option.collectonly or session.testsfailed or not session.items:
            return None  # pytest reports these itself
        if not any("bench" in getattr(item, "fixturenames", ()) for item in session.items):
            _write(config, "benchtare self-test: not run, because no test uses the bench")
            return None
        if not self.run_selftest(config).passed:
            pytest.exit(
                "BenchTare's self-test failed, so no tests ran. What failed is listed above.",
                returncode=pytest.ExitCode.INTERRUPTED,
            )
        return None

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(self, item: pytest.Item) -> Generator[None, Any, None]:
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
        try:
            yield
        finally:
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
        # Plain dicts, so the report can be serialised (for example by pytest-xdist).
        report.benchtare_record = [entry.as_dict() for entry in snapshot[call.when]]
        # Like pytest's captured output, a phase's report shows the record of the phases so far.
        for phase in PHASES[: PHASES.index(call.when) + 1]:
            entries = snapshot.get(phase, [])
            if any(entry.kind != RESULT for entry in entries):
                report.sections.append((f"BenchTare record {phase}", render(entries)))

    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        if self.bench is not None:
            self.bench.close()
            self.bench = None
        with self._lock:
            self._after_last_test = [entry for entry in self._pending if entry.unexpected]
            self._pending.clear()
        _warn_unexpected(self._warn_at_description, "after the last test", self._after_last_test)
        if self.selftest_only and self.selftest is not None:
            # No tests were collected, which pytest would report as exit code 5.
            ok = self.selftest.passed
            session.exitstatus = pytest.ExitCode.OK if ok else pytest.ExitCode.TESTS_FAILED

    def pytest_terminal_summary(self, terminalreporter: Any) -> None:
        if self._after_last_test:
            terminalreporter.write_sep("-", "BenchTare record after the last test")
            terminalreporter.write_line(render(self._after_last_test))


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
    """One warning for the frames that don't match the DBC, one for each equipment failure."""
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
