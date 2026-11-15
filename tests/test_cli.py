"""The ``benchtare`` command, run as a separate process."""

import shutil
import sys
import textwrap
from importlib.metadata import entry_points

import pytest

from benchtare import __version__, reference

FILES = [reference.DUT, reference.DBC, reference.BENCH, reference.HARNESS_CAN, reference.HARNESS_IO]
CLI = [f"--dut={reference.DUT}", f"--bench={reference.BENCH}", f"--harness={reference.HARNESS_IO}"]

CUT_LOOPBACK = """
from benchtare.sim.simulation import Simulation

_start = Simulation.start


def start(self):
    _start(self)
    self.cut("io.out4")


Simulation.start = start
"""


@pytest.fixture
def project(pytester):
    for name in FILES:
        shutil.copy(reference.path(name), pytester.path / name)
    pytester.makepyfile(test_never_run="def test_would_fail(bench):\n    assert False\n")
    return pytester


def benchtare(pytester, *args):
    return pytester.run(sys.executable, "-m", "benchtare", *args)


def test_selftest_passes_on_the_healthy_reference(project):
    result = benchtare(project, "selftest", *CLI)
    assert result.ret == 0
    result.stdout.fnmatch_lines(
        [
            "*= BenchTare self-test passed in * s =*",
            "PASSED  loopback io.out4 -> io.in4: read back True and False",
            "PASSED  DUT sends Mode: Mode = 'standby' on io.in2",
        ]
    )
    assert "test_would_fail" not in result.stdout.str()  # tests aren't run
    assert "warning" not in result.stdout.str().lower()
    assert not (project.path / ".pytest_cache").exists()


def test_selftest_finds_the_files_in_the_ini_file(project):
    project.makepyprojecttoml(
        textwrap.dedent(
            f"""
            [tool.pytest.ini_options]
            benchtare_dut = "{reference.DUT}"
            benchtare_bench = "{reference.BENCH}"
            benchtare_harness = "{reference.HARNESS_CAN}"
            """
        )
    )
    result = benchtare(project, "selftest")
    assert result.ret == 0
    result.stdout.fnmatch_lines(["PASSED  DUT traffic on can0: frames received"])


def test_a_failed_selftest_exits_1(project):
    project.makeconftest(CUT_LOOPBACK)
    result = benchtare(project, "selftest", *CLI)
    assert result.ret == 1
    result.stdout.fnmatch_lines(
        ["*= BenchTare self-test failed in * s =*", "FAILED  loopback io.out4 -> io.in4: *"]
    )


def test_selftest_without_configuration_exits_4(project):
    result = benchtare(project, "selftest")
    assert result.ret == 4
    result.stderr.fnmatch_lines(["*There's nothing to self-test: BenchTare isn't configured.*"])


def test_selftest_with_a_wrong_file_exits_4(project):
    (project.path / "bad.yaml").write_text("harness: bad\nconnections:\n  Setpoint: io.in1\n")
    result = benchtare(project, "selftest", *CLI[:2], "--harness=bad.yaml")
    assert result.ret == 4
    result.stderr.fnmatch_lines(
        ["*BenchTare can't start.*", "*bad.yaml: 1 problem in the harness file*"]
    )


def test_selftest_help_explains_the_command(project):
    result = benchtare(project, "selftest", "--help")
    assert result.ret == 0
    result.stdout.fnmatch_lines(
        [
            "Run BenchTare's self-test: check the bench and the harness, without running*",
            "Exit codes: 0 passed, 1 failed, 2 results couldn't be stored, 4 couldn't*",
        ]
    )


GOOD_TEST = """
import pytest


@pytest.fixture
def at_zero(bench, signals):
    bench.read(signals.Mode, timeout=2.0)
    bench.write(signals.ModeRequest, "run")
    bench.wait_until(signals.Mode, "run", timeout=1.0)
    bench.write(signals.Setpoint, 0.0)
    bench.wait_until(signals.Measured, 0.0, timeout=1.0)


def test_tracks(at_zero, bench, signals):
    bench.write(signals.Setpoint, 42.0)
    bench.wait_until(signals.Measured, pytest.approx(42.0, abs=0.5), timeout=1.0)
"""


def test_check_tests_exits_0_when_every_test_can_fail(project):
    (project.path / "test_never_run.py").unlink()
    project.makepyfile(test_good=GOOD_TEST)
    result = benchtare(project, "check-tests", *CLI)
    assert result.ret == 0
    result.stdout.fnmatch_lines(
        ["test_good.py::test_tracks: able to fail at its reading", "Test check: 1 able to fail"]
    )


def test_check_tests_exits_1_when_a_test_cant_fail(project):
    (project.path / "test_never_run.py").unlink()
    project.makepyfile(
        test_weak="""
        def test_reads_but_never_checks(bench, signals):
            bench.read(signals.Mode, timeout=2.0)
        """
    )
    result = benchtare(project, "check-tests", *CLI)
    assert result.ret == 1
    result.stdout.fnmatch_lines(["test_weak.py::test_reads_but_never_checks: UNABLE TO FAIL*"])


def test_check_tests_help_explains_the_command(project):
    result = benchtare(project, "check-tests", "--help")
    assert result.ret == 0
    result.stdout.fnmatch_lines(
        [
            "Run the test check: prove each test can fail, by rerunning it with what it*",
            "Exit codes: 0 every test passed and can fail, 1 a test failed or can't fail*",
        ]
    )


def test_a_command_is_required(project):
    result = benchtare(project)
    assert result.ret == 2
    result.stderr.fnmatch_lines(["usage: benchtare *", "*required: COMMAND*"])


def test_version(project):
    result = benchtare(project, "--version")
    assert result.ret == 0
    assert result.stdout.str().strip() == f"benchtare {__version__}"


def test_the_benchtare_command_is_installed():
    (script,) = entry_points(group="console_scripts", name="benchtare")
    assert script.value == "benchtare.cli:main"
