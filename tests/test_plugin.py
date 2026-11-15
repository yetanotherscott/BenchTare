"""The pytest plugin, tested with pytester against the reference DUT."""

import re
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
        "harness reference-io (harness-io.yaml); test record: stimuli"
    )
    result.stdout.fnmatch_lines([header])


def test_options_are_listed_in_help(pytester):
    result = pytester.runpytest("--help")
    result.stdout.fnmatch_lines(
        [
            "*BenchTare: test a DUT on a bench*",
            "*--dut=FILE*",
            "*--bench=FILE*",
            "*--harness=FILE*",
            "*--bench-record=LEVEL*",
        ]
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


# --- the test record -----------------------------------------------------------------------------

FAILING_TEST = """
import pytest

def test_setpoint_is_tracked(running, bench, signals):
    bench.write(signals.Setpoint, 42.0)
    bench.wait_until(signals.Measured, pytest.approx(99.0, abs=0.5), timeout=0.3)
"""


def reports_of(recorder, name):
    return {
        r.when: r
        for r in recorder.getreports("pytest_runtest_logreport")
        if r.nodeid.endswith(name)
    }


def test_each_report_carries_the_record_of_its_phase(project):
    project.makepyfile(
        test_setpoint="""
        import pytest

        def test_setpoint_is_tracked(running, bench, signals, record_property):
            bench.write(signals.Setpoint, 42.0)
            bench.wait_until(signals.Measured, pytest.approx(42.0, abs=0.5), timeout=1.0)
            record_property("operator_note", "first run")
        """
    )
    reports = reports_of(project.inline_run(*cli()), "test_setpoint_is_tracked")
    setup = reports["setup"].benchtare_record
    assert [e["kind"] for e in setup] == ["read", "write", "wait_until", "result"]
    assert setup[-1]["text"] == "setup passed"
    call = reports["call"].benchtare_record
    assert [(e["kind"], e.get("signal"), e.get("value")) for e in call] == [
        ("write", "Setpoint", 42.0),
        ("wait_until", "Measured", 42.0),
        ("result", None, None),
    ]
    assert call[1]["unit"] == "degC" and isinstance(call[1]["at"], float)
    assert call[-1] | {"at": 0} == {
        "kind": "result",
        "level": "results",
        "at": 0,
        "text": "call passed",
        "phase": "call",
        "outcome": "passed",
    }
    assert ("operator_note", "first run") in reports["call"].user_properties
    assert [e["text"] for e in reports["teardown"].benchtare_record] == ["teardown passed"]


def test_a_test_without_the_bench_records_only_its_results(project):
    project.makepyfile(test_setpoint="def test_plain():\n    pass\n")
    reports = reports_of(project.inline_run(*cli()), "test_plain")
    assert [e["text"] for e in reports["call"].benchtare_record] == ["call passed"]


def test_a_failed_test_shows_its_record(project):
    project.makepyfile(test_setpoint=FAILING_TEST)
    result = project.runpytest(*cli())
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "*- BenchTare record setup -*",
            "??:??:??.??? read Mode = * on io.in2 after *",
            "??:??:??.??? write ModeRequest = 'run' on io.out2",
            "??:??:??.??? wait_until Mode, expecting 'run': got 'run' on io.in2 after *",
            "??:??:??.??? setup passed",
            "*- BenchTare record call -*",
            "??:??:??.??? write Setpoint = 42.0 degC on io.out1",
            (
                "??:??:??.??? wait_until Measured, expecting 99.0 ± *: not reached within 0.3 s on "
                "io.in1; last value 42.0 degC"
            ),
            "??:??:??.??? call failed: *Measured did not reach 99.0*",
        ]
    )
    # BenchTare's own log is separate, and isn't turned up for the test record.
    assert "Captured log" not in result.stdout.str()


def test_passing_tests_do_not_show_their_record(project):
    result = project.runpytest(*cli())
    result.assert_outcomes(passed=2)
    assert "BenchTare record" not in result.stdout.str()


def test_record_level_results_leaves_out_writes(project):
    project.makepyfile(test_setpoint=FAILING_TEST)
    result = project.runpytest(*cli(), "--bench-record=results")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        ["*test record: results*", "*BenchTare record call*", "*wait_until Measured, expecting*"]
    )
    assert "write Setpoint" not in result.stdout.str()


def test_record_level_debug_adds_frames(project):
    project.makepyfile(test_setpoint=FAILING_TEST)
    result = project.runpytest_subprocess(*cli(reference.HARNESS_CAN), "--bench-record=debug")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "*BenchTare record call*",
            "??:??:??.??? write Setpoint = 42.0 degC on can0",
            "??:??:??.??? can0: sent Command 0x100 [[]8] a4 01 01 00 00 00 00 00",
            "??:??:??.??? can0: received Status 0x101 [[]8] a4 01 00 00 00 00 00 00",
        ]
    )


def test_record_level_can_be_set_in_the_ini_file(project):
    project.makeini("[pytest]\nbenchtare_record = results\n")
    result = project.runpytest(*cli())
    result.stdout.fnmatch_lines(["*test record: results"])


def test_unknown_record_level_is_a_usage_error(project):
    result = project.runpytest(*cli(), "--bench-record=verbose")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(
        [
            (
                "*unknown test record level 'verbose'; the levels are results, stimuli, debug. "
                "Check --bench-record, or benchtare_record in the ini file.*"
            )
        ]
    )


def test_passing_asserts_are_recorded_when_pytest_reports_them(project):
    project.makeini("[pytest]\nenable_assertion_pass_hook = true\n")
    project.makepyfile(
        test_setpoint="""
        def test_setpoint_is_tracked(running, bench, signals):
            bench.write(signals.Setpoint, 42.0)
            value = bench.wait_until(signals.Measured, lambda v: v > 40, timeout=1.0)
            assert value == 42.0
        """
    )
    reports = reports_of(project.inline_run(*cli()), "test_setpoint_is_tracked")
    (passed,) = [e for e in reports["call"].benchtare_record if e["kind"] == "assert"]
    assert passed["text"] == "assert value == 42.0: passed (line 4)"
    assert passed["assertion"] == "value == 42.0" and passed["line"] == 4


# --- unexpected events: recorded, and warned about ------------------------------------------------

MUX_CONFTEST = """
import time

import can
import pytest

CONFIG = {}


def pytest_configure(config):
    CONFIG["config"] = config


def send_as_dut(data):
    bench = CONFIG["config"].pluginmanager.get_plugin("benchtare-session").bench
    bus = can.Bus(interface="virtual", channel=bench._backends["can0"].bus_name, ignore_config=True)
    try:
        bus.send(can.Message(arbitration_id=3, data=bytes(data), is_extended_id=False))
    finally:
        bus.shutdown()


@pytest.fixture
def dut_sends(bench):
    return send_as_dut


def pytest_runtest_logfinish(nodeid):
    # A bad frame between tests, or after the last one.
    if nodeid.endswith(("test_first", "test_last")):
        send_as_dut([7, 0, 0, 0, 0, 0, 0, 0])
        time.sleep(0.2)
"""

MISBEHAVING_DUT = """
def test_dut_misbehaves(bench, signals, dut_sends):
    for _ in range(3):
        dut_sends([7, 1, 0, 0, 0, 0, 0, 0])  # multiplexer 7: the DBC only defines 0
    dut_sends([0, 5, 0, 0, 0, 0, 0, 0])
    assert bench.wait_until(signals.C, 5, timeout=1.0) == 5
"""

MUX_CLI = ["--dut=x.dut.yaml", "--bench=bench.yaml", "--harness=harness.yaml"]


@pytest.fixture
def mux_project(pytester):
    from conftest import MUX_FILES

    for name, text in MUX_FILES.items():
        (pytester.path / name).write_text(text)
    pytester.makeconftest(MUX_CONFTEST)
    return pytester


def test_bad_frames_are_recorded_and_warned_about_but_the_test_passes(mux_project):
    mux_project.makepyfile(test_dut=MISBEHAVING_DUT)
    result = mux_project.runpytest_subprocess(*MUX_CLI, "-rP")
    result.assert_outcomes(passed=1, warnings=1)
    result.stdout.fnmatch_lines(
        [
            "*warnings summary*",
            "test_dut.py::test_dut_misbehaves",
            (
                "*FrameMismatchWarning: the DUT sent 3 CAN frames that don't match the DBC during "
                "call. The first: can0: the DUT sent a frame that doesn't match the DBC: "
                "0x003 [[]8] 07 01 00 00 00 00 00 00: message MuxOut: expected multiplexer id 0, "
                "but got 7. Every one is in the BenchTare record."
            ),
            "*- BenchTare record call -*",
        ]
    )
    recorded = re.findall(
        r"^\d\d:\d\d:\d\d\.\d{3} can0: the DUT sent a frame that doesn't match the DBC",
        result.stdout.str(),
        flags=re.MULTILINE,
    )
    assert len(recorded) == 3  # every frame, in the record


def test_bad_frames_can_be_made_to_fail_the_test(mux_project):
    mux_project.makepyfile(test_dut=MISBEHAVING_DUT)
    result = mux_project.runpytest_subprocess(
        *MUX_CLI, "-W", "error::benchtare.FrameMismatchWarning"
    )
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        ["*FrameMismatchWarning: the DUT sent 3 CAN frames that don't match the DBC*"]
    )


def test_bad_frames_between_tests_count_against_the_next_test(mux_project):
    mux_project.makepyfile(
        test_dut="""
        def test_first(bench):
            pass

        def test_second(bench):
            pass
        """
    )
    result = mux_project.runpytest_subprocess(*MUX_CLI)
    result.assert_outcomes(passed=2, warnings=1)
    result.stdout.fnmatch_lines(
        [
            "*warnings summary*",
            "test_dut.py::test_second",
            (
                "*FrameMismatchWarning: the DUT sent 1 CAN frame that doesn't match the DBC during "
                "setup*"
            ),
        ]
    )


def test_bad_frames_after_the_last_test_are_reported(mux_project):
    mux_project.makepyfile(test_dut="def test_last(bench):\n    pass\n")
    result = mux_project.runpytest_subprocess(*MUX_CLI)
    result.assert_outcomes(passed=1, warnings=1)
    result.stdout.fnmatch_lines(
        [
            (
                "*x.dut.yaml:1: FrameMismatchWarning: the DUT sent 1 CAN frame that doesn't match "
                "the DBC after the last test*"
            ),
            "*- BenchTare record after the last test -*",
            "??:??:??.??? can0: the DUT sent a frame that doesn't match the DBC: 0x003 [[]8] 07*",
        ]
    )


def test_equipment_that_fails_during_a_test_is_warned_about(project):
    project.makepyfile(
        test_setpoint="""
        import time

        import can

        def test_bus_fails(bench):
            def broken(timeout=None):
                raise can.CanOperationError("bus off")

            bench._backends["can0"]._bus.recv = broken
            time.sleep(0.3)
        """
    )
    result = project.runpytest_subprocess(*cli(reference.HARNESS_CAN))
    result.assert_outcomes(passed=1, warnings=1)
    result.stdout.fnmatch_lines(
        [
            "test_setpoint.py::test_bus_fails",
            "*EquipmentWarning: during call: can0 stopped receiving: CanOperationError: bus off",
        ]
    )


# --- BenchTare's own log --------------------------------------------------------------------------


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
