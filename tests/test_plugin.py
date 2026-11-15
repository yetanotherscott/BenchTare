"""The pytest plugin, tested with pytester against the reference DUT."""

import logging
import shutil
import textwrap
import threading

import pytest

from benchtare import reference

STAGING = '''
import pytest


@pytest.fixture
def running(bench, signals):
    """The DUT has booted and is in run mode."""
    bench.read(signals.Mode, timeout=2.0)  # it has booted once it reports a mode
    bench.write(signals.ModeRequest, "run")
    bench.wait_until(signals.Mode, "run", timeout=1.0)
'''

# The example from docs/model.md, twice: the second test starts with the DUT already running.
TESTS = """
import pytest


def test_setpoint_is_tracked(running, bench, signals):
    bench.write(signals.Setpoint, 42.0)
    bench.wait_until(signals.Measured, pytest.approx(42.0, abs=0.5), timeout=1.0)


def test_another_setpoint(running, bench, signals):
    bench.write(signals.Setpoint, -7.5)
    bench.wait_until(signals.Measured, pytest.approx(-7.5, abs=0.5), timeout=1.0)
"""

FILES = [reference.DUT, reference.DBC, reference.BENCH, reference.HARNESS_CAN, reference.HARNESS_IO]


def copy_reference(directory):
    directory.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        shutil.copy(reference.path(name), directory / name)


def cli(harness=reference.HARNESS_IO, folder="."):
    return [
        f"--dut={folder}/{reference.DUT}",
        f"--bench={folder}/{reference.BENCH}",
        f"--harness={folder}/{harness}",
    ]


@pytest.fixture
def project(pytester):
    """A project with the reference files, a staging fixture and the example tests."""
    copy_reference(pytester.path)
    pytester.makeconftest(STAGING)
    pytester.makepyfile(test_setpoint=TESTS)
    return pytester


def no_reference_threads():
    return not any(t.name == "benchtare-reference-dut" for t in threading.enumerate())


# --- the example runs, with no BenchTare imports in the tests -------------------------------------


def test_example_passes_on_the_io_harness(project):
    result = project.runpytest(*cli(reference.HARNESS_IO))
    result.assert_outcomes(passed=2)


def test_example_passes_on_the_can_harness(project):
    result = project.runpytest_subprocess(*cli(reference.HARNESS_CAN))
    result.assert_outcomes(passed=2)


def test_report_header_names_the_dut_bench_and_harness(project):
    result = project.runpytest(*cli())
    header = (
        "benchtare: DUT reference (reference.dut.yaml), bench laptop-sim (bench-sim.yaml), "
        "harness reference-io (harness-io.yaml)"
    )
    result.stdout.fnmatch_lines([header])


def test_options_are_listed_in_help(pytester):
    result = pytester.runpytest("--help")
    result.stdout.fnmatch_lines(
        ["*BenchTare: test a DUT on a bench*", "*--dut=FILE*", "*--bench=FILE*", "*--harness=FILE*"]
    )


# --- where the three files are found ---------------------------------------------------------------


def test_ini_paths_are_relative_to_the_ini_file(pytester, monkeypatch):
    copy_reference(pytester.path / "setup")
    pytester.makepyprojecttoml(
        textwrap.dedent(
            f"""
            [tool.pytest.ini_options]
            benchtare_dut = "setup/{reference.DUT}"
            benchtare_bench = "setup/{reference.BENCH}"
            benchtare_harness = "setup/{reference.HARNESS_IO}"
            """
        )
    )
    tests = pytester.mkdir("tests")
    (tests / "conftest.py").write_text(STAGING)
    (tests / "test_setpoint.py").write_text(TESTS)
    monkeypatch.chdir(tests)  # run from a subfolder: paths still resolve from the ini file
    result = pytester.runpytest()
    result.assert_outcomes(passed=2)


def test_command_line_paths_are_relative_to_where_pytest_runs(project, monkeypatch):
    elsewhere = project.mkdir("elsewhere")
    monkeypatch.chdir(elsewhere)
    result = project.runpytest(*cli(folder=".."), str(project.path))
    result.assert_outcomes(passed=2)


def test_command_line_overrides_the_ini_file(project):
    project.makeini(
        """
        [pytest]
        benchtare_dut = missing.dut.yaml
        benchtare_bench = missing.yaml
        benchtare_harness = missing.yaml
        """
    )
    result = project.runpytest(*cli())
    result.assert_outcomes(passed=2)


# --- failures land in the right place --------------------------------------------------------------


def test_staging_failure_is_an_error_not_a_test_failure(project):
    project.makeconftest(
        """
        import pytest

        @pytest.fixture
        def running(bench, signals):
            bench.wait_until(signals.Mode, "nonsense", timeout=0.3)
        """
    )
    result = project.runpytest(*cli())
    result.assert_outcomes(errors=2)
    result.stdout.fnmatch_lines(["*ERROR at setup of test_setpoint_is_tracked*"])


def test_failed_check_is_a_failure_with_a_clean_message(project):
    project.makepyfile(
        test_setpoint="""
        import pytest

        def test_setpoint_is_tracked(running, bench, signals):
            bench.write(signals.Setpoint, 42.0)
            bench.wait_until(signals.Measured, pytest.approx(99.0, abs=0.5), timeout=0.3)
        """
    )
    result = project.runpytest(*cli())
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "*bench.wait_until(signals.Measured, pytest.approx(99.0*",
            "*Measured did not reach 99.0 ± * within 0.3 s (on io.in1); last value 42.0*",
            "*Check: the DUT is powered and connected at io.in1*",
        ]
    )
    assert "bench/runtime.py" not in result.stdout.str()  # BenchTare's own frames are hidden


# --- when BenchTare isn't configured, or is configured wrongly ----------------------------------


def test_nothing_configured_changes_nothing(pytester):
    pytester.makepyfile("def test_plain():\n    assert True\n")
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)
    assert "benchtare:" not in result.stdout.str()


@pytest.mark.parametrize("fixture", ["bench", "signals"])
def test_fixture_without_configuration_says_what_to_set(pytester, fixture):
    pytester.makepyfile(f"def test_needs_bench({fixture}):\n    pass\n")
    result = pytester.runpytest()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(
        [
            f"*this test uses BenchTare's '{fixture}' fixture, but BenchTare isn't configured*",
            "*Check: give --dut, --bench and --harness*",
        ]
    )
    assert "Traceback" not in result.stdout.str()


def test_partial_configuration_is_a_usage_error(project):
    result = project.runpytest(f"--dut={reference.DUT}")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(
        ["*BenchTare needs all three files, but only --dut is set*--bench (or benchtare_bench)*"]
    )


def test_wrong_file_stops_the_run_before_any_test(project):
    (project.path / "bad-harness.yaml").write_text(
        "harness: bad\nconnections:\n  Setpoint: io.in1\n"
    )
    result = project.runpytest(*cli(harness="bad-harness.yaml"))
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(
        [
            "*BenchTare can't start.*",
            "*bad-harness.yaml: 1 problem in the harness file*",
            "*it is a DUT input, but the bench can't drive io.in1*",
        ]
    )
    assert "passed" not in result.stdout.str()


def test_missing_file_is_a_usage_error(project):
    result = project.runpytest("--dut=nope.yaml", *cli()[1:])
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(
        ["*nope.yaml: 1 problem in the interface description*", "*file not found*"]
    )


def test_bench_that_fails_to_open_errors_each_test_with_one_message(project):
    project.makeconftest(
        STAGING
        + textwrap.dedent(
            """
            from benchtare.backends.base import BenchError
            from benchtare.backends.sim import SimIO

            def broken_open(self, bindings, receiver):
                raise BenchError("DAQ not found on USB", hint="the USB cable")

            SimIO.open = broken_open
            """
        )
    )
    result = project.runpytest_subprocess(*cli())
    result.assert_outcomes(errors=2)
    result.stdout.fnmatch_lines(
        ["*BenchTare couldn't open the bench.*", "*DAQ not found on USB*", "*Check: the USB cable*"]
    )


# --- the bench's life cycle ------------------------------------------------------------------------


def test_bench_is_not_opened_when_no_test_needs_it(project):
    project.makepyfile(
        test_setpoint="""
        import threading

        def test_without_bench(signals):
            # signals come from the interface description; the equipment isn't opened
            assert "Setpoint" in signals
            assert not any(t.name == "benchtare-reference-dut" for t in threading.enumerate())
        """
    )
    result = project.runpytest_subprocess(*cli())
    result.assert_outcomes(passed=1)


def test_bench_is_closed_when_the_run_ends(project):
    result = project.runpytest(*cli())
    result.assert_outcomes(passed=2)
    assert no_reference_threads()


# --- measurements ---------------------------------------------------------------------------------


def test_reads_are_recorded_on_each_report(project):
    project.makepyfile(
        test_setpoint="""
        import pytest

        def test_setpoint_is_tracked(running, bench, signals, record_property):
            bench.write(signals.Setpoint, 42.0)
            bench.wait_until(signals.Measured, pytest.approx(42.0, abs=0.5), timeout=1.0)
            record_property("operator_note", "first run")
        """
    )
    recorder = project.inline_run(*cli())
    reports = {
        r.when: r
        for r in recorder.getreports("pytest_runtest_logreport")
        if r.nodeid.endswith("test_setpoint_is_tracked")
    }
    setup = reports["setup"].benchtare_measurements
    call = reports["call"].benchtare_measurements
    assert [m["signal"] for m in setup] == ["Mode", "Mode"]  # staging: booted, then run
    assert setup[-1]["value"] == "run"
    assert [(m["signal"], m["value"], m["unit"]) for m in call] == [("Measured", 42.0, "degC")]
    assert isinstance(call[0]["at"], float)
    assert ("operator_note", "first run") in reports["call"].user_properties
    assert reports["teardown"].benchtare_measurements == []


def test_reports_without_bench_have_no_measurements(project):
    project.makepyfile(test_setpoint="def test_plain():\n    pass\n")
    recorder = project.inline_run(*cli())
    (call,) = [r for r in recorder.getreports("pytest_runtest_logreport") if r.when == "call"]
    assert call.benchtare_measurements == []


# --- logging --------------------------------------------------------------------------------------

FAILING_TEST = """
import pytest

def test_setpoint_is_tracked(running, bench, signals):
    bench.write(signals.Setpoint, 42.0)
    bench.wait_until(signals.Measured, pytest.approx(99.0, abs=0.5), timeout=0.3)
"""


def test_a_failed_test_shows_what_the_bench_did(project):
    project.makepyfile(test_setpoint=FAILING_TEST)
    result = project.runpytest(*cli())
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "*Captured log setup*",
            "*bench laptop-sim open: equipment can0 (sim_can), io (sim)*",
            "*write ModeRequest = 'run' on io.out2*",
            "*Captured log call*",
            "*write Setpoint = 42.0 degC on io.out1*",
            "*wait_until Measured on io.in1: 99.0 ± * not reached within 0.3 s*",
        ]
    )


def test_loading_the_files_shows_in_the_live_log(project):
    result = project.runpytest(*cli(), "--log-cli-level=INFO")
    result.assert_outcomes(passed=2)
    result.stdout.fnmatch_lines(
        [
            "*live log sessionstart*",
            "*loaded interface description reference.dut.yaml: DUT reference, 6 signals*",
            "*loaded harness file harness-io.yaml: harness reference-io*",
        ]
    )


def test_a_project_level_for_benchtare_logs_is_kept(project):
    project.makeconftest(
        STAGING + "\nimport logging\nlogging.getLogger('benchtare').setLevel(logging.INFO)\n"
    )
    project.makepyfile(test_setpoint=FAILING_TEST)
    result = project.runpytest_subprocess(*cli())
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*Captured log setup*", "*bench laptop-sim open*"])
    assert "write Setpoint" not in result.stdout.str()


def test_the_benchtare_log_level_is_restored_after_the_run(project):
    assert logging.getLogger("benchtare").level == logging.NOTSET
    project.runpytest(*cli()).assert_outcomes(passed=2)
    assert logging.getLogger("benchtare").level == logging.NOTSET


def test_unconfigured_runs_leave_the_log_level_alone(pytester):
    pytester.makepyfile(
        "import logging\n\n"
        "def test_level():\n"
        "    assert logging.getLogger('benchtare').level == logging.NOTSET\n"
    )
    pytester.runpytest().assert_outcomes(passed=1)
