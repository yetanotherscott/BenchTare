"""DUT models: a team's own simulated DUT, written as a DutModel and named in the harness file.
The reference DUT is one too, so the rest of the suite runs on the same base class."""

import sys
import threading
import time

import pytest

from benchtare import DutModel
from benchtare.backends.base import BenchError
from benchtare.bench.config import load_bench_file
from benchtare.bench.runtime import Bench
from benchtare.dut.description import load_interface_description
from benchtare.errors import ConfigError
from benchtare.harness.config import load_harness_file

# --- the base class --------------------------------------------------------------------------------


class Recorder(DutModel):
    """A model that notes what happens to it, and on which thread."""

    def __init__(self, publish, **options):
        super().__init__(publish, **options)
        self.seen = []
        self.threads = set()

    def started(self):
        self.threads.add(threading.get_ident())
        self.seen.append("started")

    def on_input(self, name, value):
        self.threads.add(threading.get_ident())
        self.seen.append((name, value))
        if name == "ping":
            self.after(0.05, self.publish, "pong", value)
        if name == "explode":
            raise RuntimeError("a bug in the model")


@pytest.fixture
def model():
    published = []
    recorder = Recorder(lambda name, value: published.append((name, value)))
    recorder.start()
    yield recorder, published
    recorder.stop()


def wait_for(condition, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.005)


def test_a_model_starts_then_handles_its_inputs_in_order_on_one_thread(model):
    recorder, _ = model
    for value in range(5):
        recorder.set_input("count", value)
    wait_for(lambda: len(recorder.seen) == 6)
    assert recorder.seen == ["started", *[("count", value) for value in range(5)]]
    assert len(recorder.threads) == 1
    assert threading.get_ident() not in recorder.threads  # not the caller's thread


def test_after_runs_an_action_later(model):
    recorder, published = model
    started = time.monotonic()
    recorder.set_input("ping", 7)
    wait_for(lambda: published)
    assert published == [("pong", 7)]
    assert time.monotonic() - started >= 0.045


def test_every_repeats_an_action(model):
    recorder, _ = model
    ticks = []
    recorder.every(0.01, ticks.append, "tick")
    wait_for(lambda: len(ticks) >= 5)


def test_an_error_in_a_model_is_recorded_and_it_keeps_running(model):
    recorder, published = model
    recorder.set_input("explode", True)
    recorder.set_input("ping", 1)
    wait_for(lambda: published)
    assert recorder.errors == ["RuntimeError: a bug in the model"]
    assert recorder.running


def test_a_model_without_options_refuses_them():
    with pytest.raises(TypeError, match="Recorder takes no options, but was given colour"):
        Recorder(lambda name, value: None, colour="red")


def test_stopping_a_model_stops_its_thread(model):
    recorder, _ = model
    assert recorder.running
    thread = recorder._thread
    recorder.stop()
    assert not recorder.running
    assert not thread.is_alive()


# --- a team's project -----------------------------------------------------------------------------

INTERFACE = """
dut: thermostat
signals:
  heater_enable: {type: bool, direction: input}
  target: {type: float, unit: degC, direction: input}
  temperature: {type: float, unit: degC, direction: output}
  heating: {type: bool, direction: output}
clean_state:
  heater_enable: false
  target: 20.0
  heating: false
"""

BENCH = """
bench: laptop
equipment:
  io:
    backend: sim
    channels:
      out1: {drive: true}
      out2: {drive: true}
      out3: {drive: true}
      in1: {read: true}
      in2: {read: true}
      in3: {read: true}
"""

HARNESS = """
harness: thermostat-on-laptop
connections:
  heater_enable: io.out1
  target: io.out2
  temperature: io.in1
  heating: io.in2
loopbacks:
  - {{drive: io.out3, read: io.in3}}
presence:
  - {{signal: temperature, within: 2.0}}
simulate: {simulate}
"""

MODEL = '''
from benchtare import DutModel


class Thermostat(DutModel):
    """Heats towards its target while enabled, and cools back to 20 degC when not."""

    signals = ("heater_enable", "target", "temperature", "heating")

    def __init__(self, publish, *, step=1.0, period=0.05):
        super().__init__(publish)
        self.step = step
        self.period = period

    def started(self):
        self.temperature = 20.0
        self.enabled = False
        self.target = 20.0
        self.every(self.period, self.tick)

    def on_input(self, name, value):
        if name == "heater_enable":
            self.enabled = bool(value)
        elif name == "target":
            self.target = float(value)

    def tick(self):
        goal = self.target if self.enabled else 20.0
        if self.temperature < goal:
            self.temperature = min(goal, self.temperature + self.step)
        elif self.temperature > goal:
            self.temperature = max(goal, self.temperature - self.step)
        self.publish("temperature", self.temperature)
        self.publish("heating", self.enabled and self.temperature < self.target)
'''

STAGING = """
import pytest


@pytest.fixture
def cold(bench, signals):
    bench.write(signals.heater_enable, False)
    bench.write(signals.target, 20.0)
    bench.wait_until(signals.temperature, 20.0, timeout=5.0)
    yield
    bench.write(signals.heater_enable, False)
    bench.write(signals.target, 20.0)
    bench.wait_until(signals.temperature, 20.0, timeout=5.0)
    bench.wait_until(signals.heating, False, timeout=1.0)
"""

# heating is true only while the temperature climbs: 9 ticks of 50 ms, from 20 to 30 degC. The
# sim equipment samples its read channels like a DAQ, so a shorter pulse can fall between samples
# on a slow machine (a 40 ms pulse did, on a macOS CI runner) and the test fails for no reason.
TESTS = """
import pytest


def test_heats_to_its_target(cold, bench, signals):
    bench.write(signals.target, 30.0)
    bench.write(signals.heater_enable, True)
    bench.wait_until(signals.heating, True, timeout=1.0)
    bench.wait_until(signals.temperature, pytest.approx(30.0), timeout=2.0)
"""

CLI = ["--dut=thermostat.dut.yaml", "--bench=bench.yaml", "--harness=harness.yaml"]


def write_project(root, simulate="models/thermostat.py:Thermostat", model=MODEL):
    (root / "thermostat.dut.yaml").write_text(INTERFACE)
    (root / "bench.yaml").write_text(BENCH)
    (root / "harness.yaml").write_text(HARNESS.format(simulate=simulate))
    (root / "models").mkdir(exist_ok=True)
    (root / "models" / "thermostat.py").write_text(model)


@pytest.fixture
def project(pytester):
    write_project(pytester.path)
    pytester.makeconftest(STAGING)
    pytester.makepyfile(test_thermostat=TESTS)
    return pytester


def test_the_tests_run_on_a_teams_model(project):
    result = project.runpytest(*CLI)
    assert result.ret == pytest.ExitCode.OK
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(
        [
            "benchtare bench check passed: 1 equipment item, the simulated DUT",
            "benchtare harness check passed: 1 loopback, 1 presence check",
        ]
    )


def test_the_self_test_checks_a_teams_model(project):
    result = project.runpytest(*CLI, "--selftest-only")
    assert result.ret == pytest.ExitCode.OK
    result.stdout.fnmatch_lines(
        [
            "PASSED  simulated DUT models/thermostat.py:Thermostat: running",
            "PASSED  DUT sends temperature: temperature = 20.0 on io.in1",
        ]
    )


def test_the_test_check_runs_on_a_teams_model(project):
    result = project.runpytest(*CLI, "--check-tests")
    assert result.ret == pytest.ExitCode.OK
    result.stdout.fnmatch_lines(
        ["test_thermostat.py::test_heats_to_its_target: able to fail at each of its 2 readings"]
    )


def test_the_hygiene_check_runs_on_a_teams_model(project):
    result = project.runpytest(*CLI, "--check-hygiene")
    assert result.ret == pytest.ExitCode.OK
    result.stdout.fnmatch_lines(["test_thermostat.py::test_heats_to_its_target: clean"])


def test_options_from_the_harness_reach_the_model(project):
    (project.path / "harness.yaml").write_text(
        HARNESS.format(
            simulate="{model: models/thermostat.py:Thermostat, options: {step: 5.0, period: 0.02}}"
        )
    )
    project.makepyfile(
        test_thermostat="""
        def test_the_model_has_its_options(bench):
            model = bench.simulation.device
            assert (model.step, model.period) == (5.0, 0.02)
        """
    )
    project.runpytest(*CLI).assert_outcomes(passed=1)


def test_a_model_given_an_option_it_doesnt_take_stops_at_the_self_test(project):
    (project.path / "harness.yaml").write_text(
        HARNESS.format(simulate="{model: models/thermostat.py:Thermostat, options: {colour: red}}")
    )
    result = project.runpytest(*CLI)
    assert result.ret == pytest.ExitCode.INTERRUPTED
    result.stdout.fnmatch_lines(
        [
            (
                "FAILED  opening the bench: the DUT model models/thermostat.py:Thermostat couldn't "
                "be created: TypeError: *colour*"
            ),
            (
                "        Check: its options under 'simulate:' in harness file harness.yaml, and its "
                "__init__"
            ),
        ]
    )


def test_a_model_that_publishes_something_that_isnt_an_output_fails_the_bench_check(project):
    (project.path / "models" / "thermostat.py").write_text(
        MODEL.replace(
            "        self.every(self.period, self.tick)",
            "        self.every(self.period, self.tick)\n        self.publish('humidity', 40)",
        )
    )
    result = project.runpytest(*CLI)
    assert result.ret == pytest.ExitCode.INTERRUPTED
    result.stdout.fnmatch_lines(
        [
            "*= BenchTare bench check failed =*",
            (
                "FAILED  simulated DUT models/thermostat.py:Thermostat: the simulated DUT "
                "models/thermostat.py:Thermostat: ValueError: it published 'humidity', which "
                "isn't one of the DUT's outputs in thermostat.dut.yaml"
            ),
        ]
    )


# --- naming a model in the harness file -----------------------------------------------------------


def load(tmp_path, simulate, model=MODEL):
    write_project(tmp_path, simulate=simulate, model=model)
    description = load_interface_description(tmp_path / "thermostat.dut.yaml")
    bench = load_bench_file(tmp_path / "bench.yaml")
    return load_harness_file(tmp_path / "harness.yaml", description, bench), description, bench


def test_a_model_file_is_found_relative_to_the_harness_file(tmp_path):
    harness, _, _ = load(tmp_path, "models/thermostat.py:Thermostat")
    assert harness.model.__name__ == "Thermostat"
    assert issubclass(harness.model, DutModel)
    assert harness.simulate == "models/thermostat.py:Thermostat"
    assert dict(harness.model_options) == {}


def test_a_model_can_be_given_options(tmp_path):
    harness, _, _ = load(tmp_path, "{model: models/thermostat.py:Thermostat, options: {step: 2.0}}")
    assert dict(harness.model_options) == {"step": 2.0}


def test_a_models_options_are_checked_when_the_bench_opens(tmp_path):
    harness, description, bench = load(
        tmp_path, "{model: models/thermostat.py:Thermostat, options: {colour: red}}"
    )
    with pytest.raises(BenchError, match="couldn't be created: TypeError: .*colour"):
        Bench(description, bench, harness).open()


@pytest.mark.parametrize(
    ("simulate", "model", "message"),
    [
        (
            "models/nowhere.py:Thermostat",
            MODEL,
            "simulate: there is no file models/nowhere.py for the DUT model",
        ),
        (
            "models/thermostat.py",
            MODEL,
            "unknown DUT model 'models/thermostat.py'",
        ),
        (
            "models/thermostat.txt:Thermostat",
            MODEL,
            "must be a Python file and a class in it",
        ),
        (
            "models/thermostat.py:Heater",
            MODEL,
            "Heater isn't in it: a DUT model is a class that subclasses benchtare.DutModel",
        ),
        (
            "models/thermostat.py:Plain",
            MODEL + "\n\nclass Plain:\n    pass\n",
            "Plain isn't a DutModel",
        ),
        (
            "models/thermostat.py:Thermostat",
            MODEL + "\ndef broken(:\n",
            "couldn't be loaded: SyntaxError: *at line ",
        ),
        (
            "models/thermostat.py:Thermostat",
            MODEL + "\n1 / 0\n",
            "couldn't be loaded: ZeroDivisionError: division by zero, at line ",
        ),
        (
            "models/thermostat.py:Thermostat",
            MODEL.replace('"heating")', '"heating", "humidity")'),
            (
                "the DUT model 'models/thermostat.py:Thermostat' uses signals that "
                "thermostat.dut.yaml doesn't have: humidity"
            ),
        ),
        (
            "{model: models/thermostat.py:Thermostat, options: [1, 2]}",
            MODEL,
            "'options:' must map option names to values, not a list",
        ),
        (
            "{model: models/thermostat.py:Thermostat, colour: red}",
            MODEL,
            "unknown key 'colour'",
        ),
        ("42", MODEL, "must name a DUT model, not a number (42)"),
        ("thermo", MODEL, "unknown DUT model 'thermo'"),
    ],
    ids=[
        "no-file",
        "no-class",
        "not-python",
        "class-missing",
        "not-a-model",
        "syntax-error",
        "error-on-import",
        "unknown-signal",
        "options-not-a-mapping",
        "unknown-key",
        "number",
        "unknown-name",
    ],
)
def test_mistakes_naming_a_model_are_explained(tmp_path, simulate, model, message):
    with pytest.raises(ConfigError) as info:
        load(tmp_path, simulate, model)
    import fnmatch

    assert fnmatch.fnmatch(str(info.value), f"*{message}*"), str(info.value)


def test_a_missing_class_lists_the_models_in_the_file(tmp_path):
    with pytest.raises(ConfigError, match="DUT models in models/thermostat.py: Thermostat"):
        load(tmp_path, "models/thermostat.py:Heater")


def test_a_model_file_needs_no_place_on_the_path(tmp_path):
    # Two projects with model files of the same name don't clash.
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()
    one, _, _ = load(first, "models/thermostat.py:Thermostat")
    two, _, _ = load(second, "models/thermostat.py:Thermostat", MODEL.replace("1.0", "2.0", 1))
    assert one.model.__module__ != two.model.__module__
    # Each stays loaded as itself: the second didn't replace the first.
    assert sys.modules[one.model.__module__].Thermostat is one.model
    assert sys.modules[two.model.__module__].Thermostat is two.model
