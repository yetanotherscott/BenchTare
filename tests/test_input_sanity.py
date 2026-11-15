"""Inputs beyond the three YAML files: values tests write, timeouts, DBC content, python-can's
own configuration, and the simulated DUT's handling of what it is sent.

Each test shows that a bad input is refused or survived, not silently mishandled.
"""

import math
import textwrap
import time
import uuid

import pytest

from benchtare import reference
from benchtare.backends._can_codec import CanCodec, open_virtual_bus
from benchtare.backends.base import BenchError, ReadTimeout, WriteError
from benchtare.bench.runtime import SIMULATION, _kind_problem, open_bench
from benchtare.dut import load_interface_description
from benchtare.errors import ConfigError
from benchtare.reference.device import ReferenceDevice
from benchtare.signals import Direction, Signal, SignalType


def open_reference(harness=reference.HARNESS_CAN, **device_options):
    return open_bench(
        reference.path(reference.DUT),
        reference.path(reference.BENCH),
        reference.path(harness),
        device_options=device_options or None,
    )


@pytest.fixture(params=[reference.HARNESS_CAN, reference.HARNESS_IO], ids=["can", "io"])
def bench(request):
    with open_reference(request.param) as opened:
        yield opened


# --- 1. the simulated DUT survives bad inputs ---------------------------------------------------


def test_reference_device_ignores_inputs_it_cannot_use():
    published = []
    device = ReferenceDevice(lambda name, value: published.append((name, value)), boot_delay=0)
    device.start()
    try:
        time.sleep(0.05)
        for name, value in [
            ("Setpoint", "abc"),
            ("Setpoint", None),
            ("Setpoint", float("nan")),
            ("Setpoint", True),
            ("ModeRequest", 1.5),
            ("ModeRequest", ["run"]),
            ("supply_enable", "maybe"),
            ("supply_enable", 0.5),
            ("NoSuchSignal", 1),
        ]:
            device.set_input(name, value)
        time.sleep(0.15)
        assert device.running
        assert device.errors == []
        assert ("supply_ok", True) not in published
        assert published.count(("Mode", "standby")) >= 2  # still heartbeating
    finally:
        device.stop()


def test_failing_device_action_is_reported_and_the_device_keeps_running():
    with open_reference() as bench:
        device = bench.simulation.device

        def broken():
            raise RuntimeError("simulated firmware bug")

        with device._wake:
            device._schedule(0, broken)
        time.sleep(0.1)
        assert device.running
        assert bench.check()[SIMULATION] == [
            "the simulated DUT reference: RuntimeError: simulated firmware bug"
        ]
        bench.wait_until(bench.signals.Mode, "standby", timeout=2.0)  # heartbeat continues


def test_simulation_problems_are_named_in_read_timeouts():
    with open_reference() as bench:
        device = bench.simulation.device
        device.stop()  # as if its thread had died
        assert "has stopped running" in bench.check()[SIMULATION][0]
        bench.write(bench.signals.supply_enable, True)
        with pytest.raises(ReadTimeout, match="Check: the simulated DUT reference has stopped"):
            bench.read(bench.signals.Mode, timeout=0.2)


def test_bad_stimulus_never_reaches_the_simulated_dut(bench):
    with pytest.raises(WriteError):
        bench.write(bench.signals.Setpoint, "abc")
    assert bench.simulation.device.errors == []
    assert bench.simulation.device.running


# --- 2. multiplexed CAN messages ---------------------------------------------------------------

MUX_DBC = """\
VERSION ""

NS_ :

BS_:

BU_: DUT BENCH

BO_ 2 MuxIn: 8 BENCH
 SG_ Sel M : 0|8@1+ (1,0) [0|255] "x" DUT
 SG_ A m0 : 8|8@1+ (1,0) [0|255] "x" DUT
 SG_ B m1 : 8|8@1+ (1,0) [0|255] "x" DUT

BO_ 3 MuxOut: 8 DUT
 SG_ OutSel M : 0|8@1+ (1,0) [0|255] "x" BENCH
 SG_ C m0 : 8|8@1+ (1,0) [0|255] "x" BENCH
"""

BENCH_CAN_IO = """\
bench: b
equipment:
  can0: {backend: sim_can}
  io: {backend: sim, channels: {out1: {drive: true}, in1: {read: true}}}
"""


@pytest.fixture
def files(tmp_path):
    def _write(**named):
        paths = {}
        for name, text in named.items():
            path = tmp_path / name.replace("_", ".")
            path.write_text(textwrap.dedent(text))
            paths[name] = path
        return paths

    return _write


def load_harness(files, dbc, connections, bench=BENCH_CAN_IO):
    from benchtare.bench import load_bench_file
    from benchtare.harness import load_harness_file

    paths = files(
        x_dbc=dbc,
        dut_yaml="dut: x\ncan: {node: DUT, dbc: x.dbc}\n",
        bench_yaml=bench,
        harness_yaml="harness: h\nconnections:\n"
        + "".join(f"  {name}: {target}\n" for name, target in connections.items()),
    )
    description = load_interface_description(paths["dut_yaml"])
    return paths, load_harness_file(
        paths["harness_yaml"], description, load_bench_file(paths["bench_yaml"])
    )


def test_multiplexed_dut_input_on_a_bus_is_rejected(files):
    with pytest.raises(ConfigError) as info:
        load_harness(files, MUX_DBC, {"A": "can0"})
    text = str(info.value)
    assert "signal 'A'" in text
    assert "is in multiplexed message MuxIn, and BenchTare can't write multiplexed signals" in text


def test_multiplexed_dut_output_on_a_bus_is_allowed(files):
    _, harness = load_harness(files, MUX_DBC, {"C": "can0", "OutSel": "can0"})
    assert set(harness.connections) == {"C", "OutSel"}


def test_multiplexed_dut_input_on_a_plain_channel_is_allowed(files):
    _, harness = load_harness(files, MUX_DBC, {"A": "io.out1"})
    assert str(harness.connections["A"]) == "io.out1"


# --- 3. values of the wrong kind --------------------------------------------------------------


@pytest.mark.parametrize(
    "signal, value, message",
    [
        ("Setpoint", None, "Setpoint is a float signal"),
        ("Setpoint", [1], "Setpoint is a float signal"),
        ("Setpoint", "abc", "Setpoint is a float signal"),
        ("Setpoint", True, "Setpoint is a float signal"),
        ("ModeRequest", 1.5, "ModeRequest is an enum signal"),
        ("ModeRequest", True, "ModeRequest is an enum signal"),
        ("ModeRequest", None, "ModeRequest is an enum signal"),
        ("supply_enable", "maybe", "supply_enable is a bool signal"),
        ("supply_enable", 2, "supply_enable is a bool signal"),
        ("supply_enable", 0.5, "supply_enable is a bool signal"),
    ],
)
def test_value_of_the_wrong_kind_is_refused_on_every_harness(bench, signal, value, message):
    with pytest.raises(WriteError) as info:
        bench.write(signal, value)
    assert message in str(info.value)
    assert "Check: write" in str(info.value)


@pytest.mark.parametrize(
    "signal, value",
    [("Setpoint", 42), ("Setpoint", 42.5), ("ModeRequest", 3), ("supply_enable", 1)],
)
def test_value_of_the_right_kind_is_sent(bench, signal, value):
    bench.write(signal, value)


def int_signal():
    return Signal("Count", SignalType.INT, Direction.INPUT, unit="1")


@pytest.mark.parametrize("value", [2, 2.0, -7])
def test_int_signal_takes_whole_numbers(value):
    assert _kind_problem(int_signal(), value) is None


@pytest.mark.parametrize("value", [2.5, math.nan, math.inf, "2", False])
def test_int_signal_refuses_fractions_and_non_numbers(value):
    problem = _kind_problem(int_signal(), value)
    assert problem == ("Count is an int signal", "write a whole number, for example 42")


def test_nan_is_left_to_the_channel_for_float_signals():
    # An IEEE float signal can carry NaN; an integer-coded CAN signal can't, and says so.
    signal = Signal("F", SignalType.FLOAT, Direction.INPUT, unit="V")
    assert _kind_problem(signal, math.nan) is None
    with open_reference() as bench, pytest.raises(WriteError, match="cannot convert float NaN"):
        bench.write(bench.signals.Setpoint, math.nan)


# --- 4. timeouts ------------------------------------------------------------------------------


@pytest.mark.parametrize("timeout", [None, "1", -1, math.nan, math.inf, True])
@pytest.mark.parametrize("call", ["read", "wait_until"])
def test_bad_timeout_is_refused(timeout, call):
    with open_reference() as bench:
        signals = bench.signals
        with pytest.raises(BenchError, match="timeout must be a number of seconds, 0 or more"):
            if call == "read":
                bench.read(signals.Mode, timeout)
            else:
                bench.wait_until(signals.Mode, "standby", timeout)


def test_zero_timeout_only_looks_at_what_has_arrived():
    with open_reference() as bench:
        bench.wait_until(bench.signals.Mode, "standby", timeout=2.0)
        assert bench.read(bench.signals.Mode, timeout=0) == "standby"


# --- 5. python-can's own configuration --------------------------------------------------------


def test_python_can_configuration_does_not_leak_into_the_virtual_bus(monkeypatch):
    monkeypatch.setenv("CAN_CONFIG", '{"receive_own_messages": true}')
    monkeypatch.setenv("CAN_INTERFACE", "socketcan")
    with open_reference() as bench:
        bus = bench._backends["can0"]._bus
        assert bus.receive_own_messages is False
        assert type(bus).__name__ == "VirtualBus"
        port_bus = bench.simulation._ports["can0"]._bus
        assert port_bus.receive_own_messages is False
        bench.wait_until(bench.signals.Mode, "standby", timeout=2.0)


# --- 6. repeated enum names -------------------------------------------------------------------

RESERVED_DBC = """\
VERSION ""

NS_ :

BS_:

BU_: DUT BENCH

BO_ 1 Cmd: 1 BENCH
 SG_ State : 0|2@1+ (1,0) [0|3] "" DUT

VAL_ 1 State 0 "off" 1 "on" 2 "Reserved" 3 "Reserved" ;
"""


def test_dbc_with_repeated_value_names_imports(files):
    paths = files(x_dbc=RESERVED_DBC, dut_yaml="dut: x\ncan: {node: DUT, dbc: x.dbc}\n")
    state = load_interface_description(paths["dut_yaml"]).signals.State
    assert state.values == ("off", "on", "Reserved")


def test_ambiguous_value_name_asks_for_the_raw_code(files):
    paths, _ = load_harness(files, RESERVED_DBC, {"State": "can0"})
    with open_bench(paths["dut_yaml"], paths["bench_yaml"], paths["harness_yaml"]) as bench:
        with pytest.raises(WriteError) as info:
            bench.write(bench.signals.State, "Reserved")
        text = str(info.value)
        assert "'Reserved' is the name of codes 2, 3" in text
        assert "write the raw code you mean" in text
        bench.write(bench.signals.State, 2)  # the raw code works
        bench.write(bench.signals.State, "on")  # an unambiguous name works


def test_repeated_name_decodes_as_the_name(files):
    paths = files(x_dbc=RESERVED_DBC, dut_yaml="dut: x\ncan: {node: DUT, dbc: x.dbc}\n")
    ref = load_interface_description(paths["dut_yaml"]).can_signals["State"]
    codec = CanCodec()
    message, data = codec.encode(ref, 3)
    assert str(codec.decode(message, data)["State"]) == "Reserved"


# --- 7 and 8. CAN FD, and standard and extended frames with the same ID number ---------------

FRAMES_DBC = """\
VERSION ""

NS_ :

BS_:

BU_: DUT BENCH

BO_ 257 StdIn: 8 BENCH
 SG_ S1 : 0|8@1+ (1,0) [0|255] "x" DUT

BO_ 2147483905 ExtIn: 8 BENCH
 SG_ E1 : 8|8@1+ (1,0) [0|255] "x" DUT

BO_ 258 StdOut: 8 DUT
 SG_ S2 : 0|8@1+ (1,0) [0|255] "x" BENCH

BO_ 2147483906 ExtOut: 8 DUT
 SG_ E2 : 0|8@1+ (1,0) [0|255] "x" BENCH

BO_ 3 Wide: 64 BENCH
 SG_ W : 500|8@1+ (1,0) [0|255] "x" DUT
"""


def fixed_bus_bench(fd):
    return (
        "bench: b\nequipment:\n"
        f"  can0: {{backend: sim_can, bus: test-{uuid.uuid4().hex}, fd: {str(fd).lower()}}}\n"
    )


def test_fd_message_on_a_classic_bus_is_rejected(files):
    with pytest.raises(ConfigError) as info:
        load_harness(files, FRAMES_DBC, {"W": "can0"}, bench=fixed_bus_bench(fd=False))
    text = str(info.value)
    assert "is in CAN FD message Wide, but bus can0 isn't in FD mode" in text
    assert "set fd: true for can0" in text


def test_fd_message_is_sent_as_an_fd_frame(files):
    bench_text = fixed_bus_bench(fd=True)
    paths, _ = load_harness(files, FRAMES_DBC, {"W": "can0"}, bench=bench_text)
    bus_name = bench_text.split("bus: ")[1].split(",")[0]
    with (
        open_bench(paths["dut_yaml"], paths["bench_yaml"], paths["harness_yaml"]) as bench,
        open_virtual_bus(bus_name, fd=True) as listener,
    ):
        bench.write(bench.signals.W, 9)
        received = listener.recv(2.0)
        assert received is not None
        assert received.is_fd is True
        assert len(received.data) == 64


def test_standard_and_extended_frames_with_the_same_id_stay_apart(files):
    bench_text = fixed_bus_bench(fd=False)
    connections = {"S1": "can0", "E1": "can0", "S2": "can0", "E2": "can0"}
    paths, _ = load_harness(files, FRAMES_DBC, connections, bench=bench_text)
    bus_name = bench_text.split("bus: ")[1].split(",")[0]
    import can

    with (
        open_bench(paths["dut_yaml"], paths["bench_yaml"], paths["harness_yaml"]) as bench,
        open_virtual_bus(bus_name, fd=False) as dut_side,
    ):
        # Writing: each signal goes out in its own frame type, with its own layout.
        bench.write(bench.signals.S1, 11)
        bench.write(bench.signals.E1, 22)
        standard, extended = dut_side.recv(2.0), dut_side.recv(2.0)
        assert (standard.arbitration_id, standard.is_extended_id) == (0x101, False)
        assert (extended.arbitration_id, extended.is_extended_id) == (0x101, True)
        assert standard.data[0] == 11
        assert extended.data[1] == 22  # E1 sits in byte 1 of ExtIn, not byte 0 of StdIn

        # Reading: frames with the same ID number are told apart by their type.
        dut_side.send(can.Message(arbitration_id=0x102, data=bytes([33]), is_extended_id=True))
        assert bench.wait_until(bench.signals.E2, 33, timeout=2.0) == 33
        with pytest.raises(ReadTimeout):
            bench.wait_until(bench.signals.S2, 33, timeout=0.2)
