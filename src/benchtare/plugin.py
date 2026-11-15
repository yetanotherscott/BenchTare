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
- **Opening the bench.** That happens the first time a test asks for ``bench``, so tests that
  don't need it can run without the equipment.

Logging: BenchTare logs under the ``benchtare`` logger, and pytest captures it like any other.
While BenchTare is configured, that logger is set to DEBUG (unless the project has set its level
already), so the report of a failed test shows what the bench wrote and read. Loading the files
happens at session start rather than in ``pytest_configure`` so that pytest's live log and log
file capture it.
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from benchtare.bench.config import BenchConfig, load_bench_file
from benchtare.bench.runtime import Bench, Measurement
from benchtare.dut.description import InterfaceDescription, load_interface_description
from benchtare.errors import BenchTareError, ConfigError
from benchtare.harness.config import HarnessConfig, load_harness_file
from benchtare.signals.model import SignalSet

SETTINGS = {
    "dut": "interface description",
    "bench": "bench file",
    "harness": "harness file",
}

SESSION_PLUGIN = "benchtare-session"

_MEASUREMENTS = pytest.StashKey[dict[str, list[Measurement]]]()
_PATHS = pytest.StashKey[dict[str, Path]]()
_LOG_LEVEL_SET = pytest.StashKey[bool]()

LOGGER = logging.getLogger("benchtare")

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


def pytest_configure(config: pytest.Config) -> None:
    paths = configured_paths(config)
    if paths is None:
        return
    config.stash[_PATHS] = paths
    # Bench activity is logged at DEBUG. Let pytest's handlers see it, so a failed test's report
    # shows it, unless the project has already chosen a level for BenchTare's logger.
    if LOGGER.level == logging.NOTSET:
        LOGGER.setLevel(logging.DEBUG)
        config.stash[_LOG_LEVEL_SET] = True


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
    session.config.pluginmanager.register(
        BenchSession(description, bench_config, harness), SESSION_PLUGIN
    )


def pytest_unconfigure(config: pytest.Config) -> None:
    if config.stash.get(_LOG_LEVEL_SET, False):
        LOGGER.setLevel(logging.NOTSET)


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
    """Registered as a plugin only when BenchTare is configured. Owns the bench for the run."""

    def __init__(
        self, description: InterfaceDescription, bench: BenchConfig, harness: HarnessConfig
    ) -> None:
        self.description = description
        self.bench_config = bench
        self.harness = harness
        self.bench: Bench | None = None
        self._open_error: str | None = None
        self._item: pytest.Item | None = None
        self._phase: str | None = None

    def open_bench(self) -> Bench:
        if self._open_error is not None:
            pytest.fail(self._open_error, pytrace=False)
        if self.bench is None:
            bench = Bench(self.description, self.bench_config, self.harness)
            try:
                bench.open()
            except BenchTareError as exc:
                self._open_error = f"BenchTare couldn't open the bench.\n{exc}"
                pytest.fail(self._open_error, pytrace=False)
            bench.recorder = self._record
            self.bench = bench
        return self.bench

    def _record(self, measurement: Measurement) -> None:
        if self._item is None or self._phase is None:
            return
        by_phase = self._item.stash.setdefault(_MEASUREMENTS, {})
        by_phase.setdefault(self._phase, []).append(measurement)

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
            f"harness {h.name} ({shown(h.path)})"
        )
        return [line]

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(self, item: pytest.Item) -> Generator[None, Any, None]:
        self._item = item
        try:
            yield
        finally:
            self._item = None

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_setup(self) -> Generator[None, Any, None]:
        yield from self._in_phase("setup")

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_call(self) -> Generator[None, Any, None]:
        yield from self._in_phase("call")

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_teardown(self) -> Generator[None, Any, None]:
        yield from self._in_phase("teardown")

    def _in_phase(self, phase: str) -> Generator[None, Any, None]:
        self._phase = phase
        try:
            yield
        finally:
            self._phase = None

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(
        self, item: pytest.Item, call: pytest.CallInfo[None]
    ) -> Generator[None, Any, None]:
        outcome = yield
        report = outcome.get_result()
        measurements = item.stash.get(_MEASUREMENTS, {}).get(call.when, [])
        # Plain dicts, so the report can be serialised (for example by pytest-xdist).
        report.benchtare_measurements = [m.as_dict() for m in measurements]

    def pytest_sessionfinish(self) -> None:
        if self.bench is not None:
            self.bench.close()
            self.bench = None
