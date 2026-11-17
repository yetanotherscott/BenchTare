"""The running bench against the simulated reference DUT, through both reference harnesses."""

import re
import textwrap

import pytest

from benchtare import reference
from benchtare.backends.base import BenchError, ComparisonFailed, ReadTimeout, WriteError
from benchtare.bench.runtime import SIMULATION, open_bench

HARNESSES = [reference.HARNESS_CAN, reference.HARNESS_IO]


def open_reference(harness=reference.HARNESS_CAN, **device_options):
    return open_bench(
        reference.path(reference.DUT),
        reference.path(reference.BENCH),
        reference.path(harness) if isinstance(harness, str) else harness,
        device_options=device_options or None,
    )


def stage_run(bench):
    """What a staging fixture does: wait for boot, then put the DUT in run mode."""
    signals = bench.signals
    bench.wait_until(signals.Mode, "standby", timeout=2.0)
    bench.write(signals.ModeRequest, "run")
    bench.wait_until(signals.Mode, "run", timeout=2.0)


@pytest.fixture(params=HARNESSES, ids=["can", "io"])
def bench(request):
    with open_reference(request.param) as opened:
        yield opened


@pytest.fixture
def can_bench():
    with open_reference(reference.HARNESS_CAN) as opened:
        yield opened


# --- the round trip, on both harnesses ----------------------------------------------------------


def test_setpoint_is_tracked(bench):
    signals = bench.signals
    stage_run(bench)
    bench.write(signals.Setpoint, 42.0)
    assert bench.wait_until(signals.Measured, pytest.approx(42.0, abs=0.5), timeout=2.0) == 42.0


def test_without_staging_the_dut_answers_zero(bench):
    signals = bench.signals
    bench.wait_until(signals.Mode, "standby", timeout=2.0)
    bench.write(signals.Setpoint, 42.0)
    # A value after the write, however long a slow runner takes to deliver it: on CAN the DUT's
    # answer, on I/O the sampled channel.
    assert bench.wait_until(signals.Measured, 0.0, timeout=2.0) == 0.0
    with pytest.raises(ComparisonFailed):  # and nothing since has been 42
        bench.wait_until(signals.Measured, 42.0, timeout=0.2)


def test_supply_ok_follows_supply_enable(bench):
    signals = bench.signals
    bench.write(signals.supply_enable, True)
    assert bench.wait_until(signals.supply_ok, True, timeout=2.0) is True
    bench.write(signals.supply_enable, False)
    assert bench.wait_until(signals.supply_ok, False, timeout=2.0) is False


def test_equipment_reports_healthy(bench):
    assert bench.check() == {"can0": [], "io": [], SIMULATION: []}


def test_values_have_python_types(can_bench):
    signals = can_bench.signals
    assert can_bench.wait_until(signals.Mode, "standby", timeout=2.0) == "standby"
    stage_run(can_bench)
    can_bench.write(signals.Setpoint, 21)
    value = can_bench.wait_until(signals.Measured, pytest.approx(21.0, abs=0.05), timeout=2.0)
    assert isinstance(value, float)


# --- the read rule ------------------------------------------------------------------------------


def test_read_never_returns_a_value_from_before_the_write(can_bench):
    signals = can_bench.signals
    stage_run(can_bench)
    can_bench.write(signals.Setpoint, 10.0)
    can_bench.wait_until(signals.Measured, pytest.approx(10.0, abs=0.05), timeout=2.0)
    can_bench.write(signals.Setpoint, 20.0)
    assert can_bench.read(signals.Measured, timeout=2.0) == pytest.approx(20.0, abs=0.05)


def test_read_does_not_miss_an_immediate_reply():
    # echo_delay=0: the DUT answers as fast as the virtual bus allows, possibly before read().
    with open_reference(reference.HARNESS_CAN, echo_delay=0.0) as bench:
        signals = bench.signals
        stage_run(bench)
        bench.write(signals.Setpoint, 10.0)
        bench.wait_until(signals.Measured, pytest.approx(10.0, abs=0.05), timeout=2.0)
        bench.write(signals.Setpoint, 30.0)
        assert bench.read(signals.Measured, timeout=2.0) == pytest.approx(30.0, abs=0.05)


def test_read_on_a_sampled_channel_returns_the_settled_level():
    with open_reference(reference.HARNESS_IO) as bench:
        signals = bench.signals
        stage_run(bench)
        # The first sample after the ModeRequest write may still be 'standby', so staging uses
        # wait_until. Once the state has settled, any later write is followed by fresh samples.
        bench.write(signals.supply_enable, True)
        assert bench.read(signals.Mode, timeout=2.0) == "run"


def test_first_value_after_a_write_can_predate_the_dut_reacting():
    # Why responses are checked with wait_until: on a sampled channel the DUT has not yet
    # reacted when the first sample after the write is taken.
    with open_reference(reference.HARNESS_IO, mode_delay=0.2) as bench:
        signals = bench.signals
        bench.wait_until(signals.Mode, "standby", timeout=2.0)
        bench.write(signals.ModeRequest, "run")
        assert bench.read(signals.Mode, timeout=2.0) == "standby"
        assert bench.wait_until(signals.Mode, "run", timeout=2.0) == "run"


def test_read_timeout_says_what_to_check(can_bench):
    can_bench.simulation.cut("can0")  # unplug the DUT from the bus
    with pytest.raises(ReadTimeout) as info:
        can_bench.read(can_bench.signals.Measured, timeout=0.2)
    text = str(info.value)
    assert "no value of Measured arrived within 0.2 s after the last write (on can0)" in text
    assert "Check: the DUT is powered and connected at can0" in text
    assert "harness-can.yaml" in text


def test_values_that_never_match_fail_like_an_assert_with_the_last_value(can_bench):
    signals = can_bench.signals
    can_bench.wait_until(signals.Mode, "standby", timeout=2.0)
    with pytest.raises(ComparisonFailed) as info:
        can_bench.wait_until(signals.Mode, "run", timeout=0.2)
    assert isinstance(info.value, AssertionError)  # pytest reports it as a failed check
    assert re.fullmatch(
        r"Mode did not reach 'run' within 0\.2 s \(on can0\): \d+ values? arrived since the "
        r"bench opened, the last 'standby'",
        str(info.value),
    )
    can_bench.write(signals.ModeRequest, "standby")
    # A heartbeat after the write has arrived, so the wait below fails on what arrived, never on
    # how quickly: a slow runner can't turn it into a read timeout.
    can_bench.wait_until(signals.Mode, "standby", timeout=2.0)
    with pytest.raises(ComparisonFailed, match="arrived after the last write, the last 'standby'"):
        can_bench.wait_until(signals.Mode, "run", timeout=0.2)


def test_a_wait_that_receives_nothing_is_a_read_timeout(can_bench):
    can_bench.wait_until(can_bench.signals.Mode, "standby", timeout=2.0)
    can_bench.simulation.cut("can0")  # unplug the DUT from the bus
    can_bench.write(can_bench.signals.ModeRequest, "run")
    with pytest.raises(ReadTimeout) as info:
        can_bench.wait_until(can_bench.signals.Mode, "run", timeout=0.2)
    text = str(info.value)
    assert "no value of Mode arrived within 0.2 s after the last write (on can0), so it " in text
    assert "couldn't reach 'run'; last value 'standby'" in text
    assert "Check: the DUT is powered and connected at can0" in text


def test_wait_until_accepts_a_predicate(can_bench):
    def is_standby(value):
        return value == "standby"

    assert can_bench.wait_until(can_bench.signals.Mode, is_standby, timeout=2.0) == "standby"


# --- writing values ------------------------------------------------------------------------------


def test_value_too_wide_for_its_bits_fails_clearly(can_bench):
    with pytest.raises(WriteError) as info:
        can_bench.write(can_bench.signals.Setpoint, 5000.0)
    text = str(info.value)
    assert "can't write Setpoint = 5000.0 on can0: it doesn't fit in message Command" in text
    assert "carry -3276.8 to 3276.7" in text


def test_any_value_the_channel_can_carry_is_sent(can_bench):
    # Range isn't BenchTare's business: an implausible setpoint is a legitimate stimulus.
    signals = can_bench.signals
    stage_run(can_bench)
    can_bench.write(signals.Setpoint, -3000.0)
    assert can_bench.wait_until(
        signals.Measured, pytest.approx(-3000.0, abs=0.05), timeout=2.0
    ) == pytest.approx(-3000.0, abs=0.05)


def test_undefined_enum_code_can_be_sent_as_a_raw_number(can_bench):
    signals = can_bench.signals
    can_bench.wait_until(signals.Mode, "standby", timeout=2.0)
    can_bench.write(signals.ModeRequest, 3)  # not a defined value: the DUT should ignore it
    # Its next five heartbeats all say standby. Counting heartbeats, not seconds, keeps this
    # true on a runner too slow to deliver any within a short wait.
    heartbeats = []

    def fifth(mode):
        heartbeats.append(mode)
        return len(heartbeats) == 5

    can_bench.wait_until(signals.Mode, fifth, timeout=2.0)
    assert heartbeats == ["standby"] * 5


def test_unknown_enum_name_lists_the_values(bench):
    # Refused before any backend sees it, so both harnesses say the same thing.
    with pytest.raises(WriteError) as info:
        bench.write(bench.signals.ModeRequest, "go")
    text = str(info.value)
    assert "'go' is not one of the values of ModeRequest" in text
    assert "its values are standby, run; a raw number sends an undefined code" in text


def test_writing_a_dut_output_is_refused(bench):
    with pytest.raises(WriteError, match="can't write Measured: it is a DUT output"):
        bench.write(bench.signals.Measured, 1.0)


def test_reading_a_dut_input_is_refused(bench):
    with pytest.raises(BenchError, match="can't read Setpoint: it is a DUT input"):
        bench.read(bench.signals.Setpoint)


def test_unknown_signal_name_suggests_close_names(bench):
    with pytest.raises(BenchError, match="no signal named 'Setpiont'.*'Setpoint'"):
        bench.write("Setpiont", 1.0)


def test_unconnected_signal_says_how_to_connect_it(tmp_path):
    harness = tmp_path / "partial.yaml"
    harness.write_text(
        textwrap.dedent(
            """\
            harness: partial
            connections:
              Setpoint: can0
            """
        )
    )
    with (
        open_reference(harness) as bench,
        pytest.raises(BenchError, match="Measured isn't connected in harness file partial"),
    ):
        bench.read(bench.signals.Measured)


# --- isolation and simulated faults ---------------------------------------------------------------


def test_two_can_benches_in_one_process_are_isolated():
    with open_reference(reference.HARNESS_CAN) as one, open_reference(reference.HARNESS_CAN) as two:
        assert one._backends["can0"].bus_name != two._backends["can0"].bus_name
        stage_run(one)
        stage_run(two)
        one.write(one.signals.Setpoint, 11.0)
        two.write(two.signals.Setpoint, 22.0)
        eleven, twenty_two = pytest.approx(11.0, abs=0.05), pytest.approx(22.0, abs=0.05)
        assert one.wait_until(one.signals.Measured, eleven, timeout=2.0) == eleven
        assert two.wait_until(two.signals.Measured, twenty_two, timeout=2.0) == twenty_two
        # Neither bench hears the other's DUT.
        with pytest.raises(ComparisonFailed):
            two.wait_until(two.signals.Measured, eleven, timeout=0.2)
        with pytest.raises(ComparisonFailed):
            one.wait_until(one.signals.Measured, twenty_two, timeout=0.2)


def test_failed_equipment_is_reported_and_refuses_writes(can_bench):
    can_bench.simulation.fail("io")
    assert can_bench.check()["io"] == ["simulated equipment failure"]
    with pytest.raises(WriteError, match="simulated equipment failure"):
        can_bench.write(can_bench.signals.supply_enable, True)


def test_failed_equipment_is_named_in_read_timeouts(can_bench):
    can_bench.simulation.fail("can0", "CAN interface not responding")
    with pytest.raises(ReadTimeout, match="equipment 'can0' reports: CAN interface not responding"):
        can_bench.read(can_bench.signals.Mode, timeout=0.2)


def test_cut_wire_stops_values():
    with open_reference(reference.HARNESS_IO) as bench:
        bench.wait_until(bench.signals.Mode, "standby", timeout=2.0)
        bench.simulation.cut("io.in2")
        bench.write(bench.signals.supply_enable, True)  # any write, so read waits for new data
        with pytest.raises(ReadTimeout):
            bench.read(bench.signals.Mode, timeout=0.2)


def test_loopback_reads_back_what_is_driven(can_bench):
    # The self-test (M4) drives loopbacks through the bench; here, through the backend.
    io = can_bench._backends["io"]
    with can_bench._buffer.writing():
        io.write(can_bench._bindings["io.out4"], True)
    assert can_bench._buffer.first_after_write("io.in4", timeout=2.0).value is True


def test_cut_loopback_reads_nothing(can_bench):
    can_bench.simulation.cut("io.out4")
    io = can_bench._backends["io"]
    with can_bench._buffer.writing():
        io.write(can_bench._bindings["io.out4"], True)
    assert can_bench._buffer.first_after_write("io.in4", timeout=0.2) is None


def test_closing_stops_the_simulation():
    bench = open_reference()
    bench.close()
    assert bench.simulation is None
    with pytest.raises(BenchError, match="the bench is not open"):
        bench.write(bench.signals.Setpoint, 1.0)
