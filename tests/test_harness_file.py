"""Loading harness files, checked against the interface description and the bench file."""

import textwrap

import pytest

from benchtare import reference
from benchtare.bench import load_bench_file
from benchtare.dut import load_interface_description
from benchtare.errors import ConfigError
from benchtare.harness import Endpoint, Loopback, Presence, load_harness_file


@pytest.fixture(scope="module")
def dut():
    return load_interface_description(reference.path(reference.DUT))


@pytest.fixture(scope="module")
def bench():
    return load_bench_file(reference.path(reference.BENCH))


@pytest.fixture
def harness(tmp_path, dut, bench):
    def _load(text):
        path = tmp_path / "harness.yaml"
        path.write_text("harness: h\n" + textwrap.dedent(text))
        return load_harness_file(path, dut, bench)

    return _load


def problems(excinfo):
    return "\n".join(str(p) for p in excinfo.value.problems)


@pytest.mark.parametrize("name", [reference.HARNESS_CAN, reference.HARNESS_IO])
def test_reference_harnesses_load(dut, bench, name):
    harness = load_harness_file(reference.path(name), dut, bench)
    assert harness.simulate == "reference"
    assert set(harness.connections) == set(dut.signals)
    assert harness.loopbacks == (Loopback(Endpoint("io", "out4"), Endpoint("io", "in4")),)
    assert Presence(within=2.0, signal="Mode") in harness.presence


def test_can_harness_puts_can_signals_on_the_bus(dut, bench):
    harness = load_harness_file(reference.path(reference.HARNESS_CAN), dut, bench)
    assert harness.connections["Setpoint"] == Endpoint("can0")
    assert harness.connections["supply_ok"] == Endpoint("io", "in1")


def test_dbc_signal_may_be_wired_to_a_plain_channel(harness):
    # The DBC only says how a signal is encoded on CAN; the harness decides where it goes.
    loaded = harness("connections:\n  Setpoint: io.out1\n")
    assert loaded.connections["Setpoint"] == Endpoint("io", "out1")


def test_dut_input_on_read_only_channel_is_rejected(harness):
    with pytest.raises(ConfigError) as info:
        harness("connections:\n  Setpoint: io.in1\n")
    text = problems(info)
    assert "signal 'Setpoint'" in text
    assert "it is a DUT input, but the bench can't drive io.in1" in text


def test_dut_output_on_drive_only_channel_is_rejected(harness):
    with pytest.raises(ConfigError, match="it is a DUT output, but the bench can't read io.out1"):
        harness("connections:\n  Measured: io.out1\n")


def test_unknown_signal_suggests_close_names(harness):
    with pytest.raises(ConfigError, match="(?s)signal 'Setpiont'.*did you mean Setpoint"):
        harness("connections:\n  Setpiont: can0\n")


def test_unknown_equipment(harness):
    with pytest.raises(ConfigError, match="(?s)no equipment 'can1'.*did you mean 'can0'"):
        harness("connections:\n  Setpoint: can1\n")


def test_unknown_channel(harness):
    with pytest.raises(ConfigError, match="no channel 'out9' on 'io'"):
        harness("connections:\n  Setpoint: io.out9\n")


def test_channel_on_a_bus_is_rejected(harness):
    with pytest.raises(ConfigError, match="'can0' is a bus, which has no channels"):
        harness("connections:\n  Setpoint: can0.x\n")


def test_channel_equipment_needs_a_channel(harness):
    with pytest.raises(ConfigError, match="'io' has channels; say which one"):
        harness("connections:\n  supply_enable: io\n")


def test_signal_without_dbc_cannot_go_on_a_can_bus(harness):
    with pytest.raises(ConfigError, match="doesn't come from a DBC file"):
        harness("connections:\n  supply_enable: can0\n")


def test_channel_used_twice_is_rejected(harness):
    with pytest.raises(ConfigError, match="channel io.out1 is already used by signal 'Setpoint'"):
        harness("connections:\n  Setpoint: io.out1\n  ModeRequest: io.out1\n")


def test_many_signals_may_share_a_bus(harness):
    loaded = harness("connections:\n  Setpoint: can0\n  Measured: can0\n")
    assert len(loaded.connections) == 2


def test_loopback_must_drive_a_drivable_channel(harness):
    with pytest.raises(ConfigError, match="it drives, but the bench can't drive io.in3"):
        harness("connections: {}\nloopbacks:\n  - {drive: io.in3, read: io.in4}\n")


def test_loopback_cannot_reuse_a_signal_channel(harness):
    with pytest.raises(ConfigError, match="already used by signal 'Setpoint'"):
        harness(
            "connections:\n  Setpoint: io.out1\nloopbacks:\n  - {drive: io.out1, read: io.in4}\n"
        )


def test_loopback_cannot_use_a_bus(harness):
    with pytest.raises(ConfigError, match="loopbacks connect channels, not buses"):
        harness("connections: {}\nloopbacks:\n  - {drive: can0, read: io.in4}\n")


def test_presence_signal_must_be_a_connected_output(harness):
    with pytest.raises(ConfigError) as info:
        harness(
            """\
            connections:
              Setpoint: can0
            presence:
              - {signal: Setpoint, within: 1}
              - {signal: Mode, within: 1}
            """
        )
    text = problems(info)
    assert "is a DUT input; presence is shown by a value the DUT sends" in text
    assert "signal 'Mode'" in text and "isn't connected in this harness" in text


def test_presence_bus_must_be_a_bus(harness):
    with pytest.raises(ConfigError, match="(?s)'io' is not a bus.*buses: can0"):
        harness("connections: {}\npresence:\n  - {bus: io, within: 1}\n")


def test_presence_needs_a_positive_time(harness):
    with pytest.raises(ConfigError, match="'within:' must be a number of seconds above 0"):
        harness("connections: {}\npresence:\n  - {bus: can0, within: 0}\n")


def test_unknown_simulated_dut(harness):
    with pytest.raises(ConfigError, match="(?s)unknown DUT model 'refrence'.*reference"):
        harness("connections: {}\nsimulate: refrence\n")


def test_simulated_dut_needs_its_signals(tmp_path, bench):
    dut_path = tmp_path / "other.dut.yaml"
    dut_path.write_text("dut: other\nsignals:\n  x: {type: bool, direction: input}\n")
    other = load_interface_description(dut_path)
    harness_path = tmp_path / "h.yaml"
    harness_path.write_text("harness: h\nconnections: {}\nsimulate: reference\n")
    with pytest.raises(ConfigError, match="uses signals that other.dut.yaml doesn't have"):
        load_harness_file(harness_path, other, bench)


def test_missing_connections_section(harness):
    with pytest.raises(ConfigError, match="'connections:' is missing"):
        harness("simulate: reference\n")


def test_error_names_the_harness_file(harness, tmp_path):
    with pytest.raises(ConfigError) as info:
        harness("connections:\n  Setpoint: io.in1\n")
    assert str(info.value).startswith(f"{tmp_path / 'harness.yaml'}: 1 problem in the harness file")
