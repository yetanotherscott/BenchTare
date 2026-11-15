"""Logging: what BenchTare logs, and the failures in background threads that it would otherwise
lose (a bus that stops receiving, a frame that doesn't match the DBC, a sampler that dies)."""

import logging
import textwrap
import threading
import time

import can
import pytest

from benchtare import reference
from benchtare.backends._can_codec import CanCodec, FrameError, FrameReceiver, describe_frame
from benchtare.backends.base import ReadTimeout
from benchtare.bench import load_bench_file
from benchtare.bench.runtime import SIMULATION, open_bench
from benchtare.dut.description import load_interface_description
from benchtare.harness import load_harness_file


def open_reference(harness=reference.HARNESS_CAN):
    return open_bench(
        reference.path(reference.DUT), reference.path(reference.BENCH), reference.path(harness)
    )


@pytest.fixture
def logs(caplog):
    """BenchTare's log records, at every level."""
    caplog.set_level(logging.DEBUG, logger="benchtare")

    def messages(level=None):
        return [
            r.getMessage()
            for r in caplog.records
            if r.name.startswith("benchtare") and (level is None or r.levelno == level)
        ]

    messages.records = lambda: [r for r in caplog.records if r.name.startswith("benchtare")]
    return messages


def eventually(condition, timeout=2.0):
    """Wait for a background thread to make ``condition()`` true."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.01)


def broken_recv(timeout=None):
    raise can.CanOperationError("bus off")


def index_of(messages, start):
    return next(i for i, m in enumerate(messages) if m.startswith(start))


# --- what the bench logs -------------------------------------------------------------------------


def test_bench_activity_is_logged_in_order(logs):
    with open_reference(reference.HARNESS_CAN) as bench:
        signals = bench.signals
        bench.read(signals.Mode, timeout=2.0)
        bench.write(signals.ModeRequest, "run")
        bench.wait_until(signals.Mode, "run", timeout=2.0)

    info = logs(logging.INFO)
    assert (
        "bench laptop-sim open: equipment can0 (sim_can), io (sim); harness reference-can; "
        "DUT reference (simulated: reference)"
    ) in info
    assert info[-1] == "bench laptop-sim closed"

    debug = logs(logging.DEBUG)
    assert any(m.startswith("read Mode = 'standby' on can0 (after ") for m in debug)
    write = index_of(debug, "write ModeRequest = 'run' on can0")
    sent = index_of(debug, "can0: sent Command 0x100 [8] ")
    waited = index_of(debug, "wait_until Mode = 'run' on can0 (after ")
    assert write < sent < waited


def test_values_are_logged_with_their_units(logs):
    with open_reference(reference.HARNESS_IO) as bench:
        bench.write(bench.signals.Setpoint, 42.0)
        bench.write(bench.signals.supply_enable, True)
    assert "write Setpoint = 42.0 degC on io.out1" in logs(logging.DEBUG)
    assert "write supply_enable = True on io.out3" in logs(logging.DEBUG)


def test_timeouts_are_logged(logs):
    with open_reference(reference.HARNESS_CAN) as bench:
        bench.write(bench.signals.Setpoint, 1.0)  # the DUT hasn't booted: no Measured comes
        with pytest.raises(ReadTimeout):
            bench.read(bench.signals.Measured, timeout=0.05)
        with pytest.raises(ReadTimeout):
            bench.wait_until(bench.signals.Mode, "run", timeout=0.05)
    debug = logs(logging.DEBUG)
    assert "read Measured on can0: nothing within 0.05 s" in debug
    assert "wait_until Mode on can0: 'run' not reached within 0.05 s" in debug


def test_a_healthy_run_logs_no_warnings_and_leaves_no_threads(logs):
    with open_reference(reference.HARNESS_CAN) as bench:
        bench.wait_until(bench.signals.Mode, "standby", timeout=2.0)
        bench.write(bench.signals.ModeRequest, "run")
        bench.wait_until(bench.signals.Mode, "run", timeout=2.0)
    # Closing shuts the bus under the receive threads; that must not look like a failure.
    assert [r for r in logs.records() if r.levelno >= logging.WARNING] == []
    assert not [t.name for t in threading.enumerate() if t.name.startswith("benchtare-")]


# --- what the loaders log ------------------------------------------------------------------------


def test_loading_the_reference_files_is_logged(logs):
    description = load_interface_description(reference.path(reference.DUT))
    bench = load_bench_file(reference.path(reference.BENCH))
    load_harness_file(reference.path(reference.HARNESS_CAN), description, bench)
    assert logs(logging.INFO) == [
        "reference.dut.yaml: imported 4 signals from reference.dbc for node REFDEV",
        (
            "loaded interface description reference.dut.yaml: DUT reference, 6 signals "
            "(4 from DBC files, 2 listed inline)"
        ),
        "loaded bench file bench-sim.yaml: bench laptop-sim, equipment can0 (sim_can), io (sim)",
        (
            "loaded harness file harness-can.yaml: harness reference-can, 6 connections, "
            "1 loopback, 2 presence checks, simulating DUT reference"
        ),
    ]


SKIPPING_DBC = """\
VERSION ""

NS_ :

BS_:

BU_: DUT OTHER BENCH

BO_ 1 Ours: 8 DUT
 SG_ Speed : 0|16@1+ (1,0) [0|0] "rpm" BENCH

BO_ 2 Theirs: 8 OTHER
 SG_ Gear : 0|8@1+ (1,0) [0|0] "" BENCH
 SG_ Clutch : 8|8@1+ (1,0) [0|0] "" BENCH
"""


def test_dbc_signals_the_dut_does_not_use_are_logged(tmp_path, logs):
    (tmp_path / "x.dbc").write_text(SKIPPING_DBC)
    (tmp_path / "x.dut.yaml").write_text("dut: x\ncan: {node: DUT, dbc: x.dbc}\n")
    load_interface_description(tmp_path / "x.dut.yaml")
    assert (
        "x.dut.yaml: imported 1 signal from x.dbc for node DUT; skipped 2 signals it neither "
        "sends nor receives" in logs(logging.INFO)
    )
    assert "x.dut.yaml: skipped from x.dbc: Theirs.Gear, Theirs.Clutch" in logs(logging.DEBUG)


# --- a bus that stops receiving -------------------------------------------------------------------


def test_a_bus_that_stops_receiving_is_reported(logs):
    with open_reference(reference.HARNESS_CAN) as bench:
        can0 = bench._backends["can0"]
        bench.wait_until(bench.signals.Mode, "standby", timeout=2.0)
        can0._bus.recv = broken_recv
        eventually(lambda: can0.check() != [])
        assert bench.check()["can0"] == ["stopped receiving: CanOperationError: bus off"]
        bench.write(bench.signals.ModeRequest, "run")
        with pytest.raises(ReadTimeout) as info:
            bench.read(bench.signals.Mode, timeout=0.2)
    assert "equipment 'can0' reports: stopped receiving: CanOperationError: bus off" in str(
        info.value
    )
    assert logs(logging.ERROR) == ["can0 stopped receiving: CanOperationError: bus off"]


def test_a_bus_error_while_closing_is_not_a_failure(logs):
    # Closing joins the receive thread before shutting the bus, but if the join times out (a
    # slow driver), the thread's last recv() fails on a closed bus. That is not a bus failure.
    class ClosingBus:
        def recv(self, timeout):
            receiver._stop.set()
            raise can.CanOperationError("Cannot operate on a closed bus")

    receiver = FrameReceiver(ClosingBus(), lambda frame: None, who="can0")
    receiver._run()
    assert receiver.failure is None
    assert logs() == []


def test_a_simulated_dut_port_that_stops_receiving_is_reported(logs):
    with open_reference(reference.HARNESS_CAN) as bench:
        port = bench.simulation._ports["can0"]
        bench.wait_until(bench.signals.Mode, "standby", timeout=2.0)
        port._bus.recv = broken_recv
        eventually(lambda: bench.check()[SIMULATION] != [])
        assert bench.check()[SIMULATION] == [
            (
                "the simulated DUT reference's port on can0 stopped receiving: "
                "CanOperationError: bus off"
            )
        ]
    assert logs(logging.ERROR) == [
        "simulated DUT on can0 stopped receiving: CanOperationError: bus off"
    ]


# --- frames that don't match the DBC --------------------------------------------------------------

MUX_DBC = """\
VERSION ""

NS_ :

BS_:

BU_: DUT BENCH

BO_ 3 MuxOut: 8 DUT
 SG_ OutSel M : 0|8@1+ (1,0) [0|255] "x" BENCH
 SG_ C m0 : 8|8@1+ (1,0) [0|255] "x" BENCH
"""


@pytest.fixture
def mux_bench(tmp_path):
    """A bench listening for a multiplexed DUT output, with no simulated DUT: the test sends."""
    files = {
        "x.dbc": MUX_DBC,
        "x.dut.yaml": "dut: x\ncan: {node: DUT, dbc: x.dbc}\n",
        "bench.yaml": "bench: b\nequipment:\n  can0: {backend: sim_can}\n",
        "harness.yaml": "harness: h\nconnections:\n  C: can0\n",
    }
    for name, text in files.items():
        (tmp_path / name).write_text(textwrap.dedent(text))
    with open_bench(
        tmp_path / "x.dut.yaml", tmp_path / "bench.yaml", tmp_path / "harness.yaml"
    ) as bench:
        bus = can.Bus(
            interface="virtual", channel=bench._backends["can0"].bus_name, ignore_config=True
        )
        try:
            yield (
                bench,
                lambda data: bus.send(
                    can.Message(arbitration_id=3, data=bytes(data), is_extended_id=False)
                ),
            )
        finally:
            bus.shutdown()


def test_a_frame_that_does_not_match_the_dbc_is_logged_and_skipped(mux_bench, logs):
    bench, send = mux_bench
    send([7, 1, 0, 0, 0, 0, 0, 0])  # multiplexer 7: the DBC only defines 0
    send([0, 5, 0, 0, 0, 0, 0, 0])
    assert bench.wait_until(bench.signals.C, 5, timeout=1.0) == 5  # reception went on
    assert bench.check()["can0"] == []
    assert logs(logging.WARNING) == [
        (
            "can0: ignored a frame that doesn't match the DBC: 0x003 [8] 07 01 00 00 00 00 00 00: "
            "message MuxOut: expected multiplexer id 0, but got 7\n"
            "  Check: the DBC files describe what the DUT sends"
        )
    ]


def test_repeated_bad_frames_are_counted_not_logged_each_time(mux_bench, logs, monkeypatch):
    monkeypatch.setattr(FrameReceiver, "quiet_period", 0.3)
    bench, send = mux_bench
    for _ in range(3):
        send([7, 0, 0, 0, 0, 0, 0, 0])
    send([0, 1, 0, 0, 0, 0, 0, 0])
    bench.wait_until(bench.signals.C, 1, timeout=1.0)  # all four frames have been received
    assert len(logs(logging.WARNING)) == 1
    time.sleep(0.35)
    send([7, 0, 0, 0, 0, 0, 0, 0])
    eventually(lambda: len(logs(logging.WARNING)) == 2)
    assert logs(logging.WARNING)[1].endswith("(2 more like it since the last report)")


def test_an_error_handling_a_frame_is_logged_with_its_traceback(mux_bench, logs, monkeypatch):
    bench, send = mux_bench
    deliver = bench._buffer.deliver
    calls = []

    def fails_once(key, value):
        calls.append(value)
        if len(calls) == 1:
            raise RuntimeError("boom")
        deliver(key, value)

    monkeypatch.setattr(bench._buffer, "deliver", fails_once)
    send([0, 1, 0, 0, 0, 0, 0, 0])
    send([0, 2, 0, 0, 0, 0, 0, 0])
    assert bench.wait_until(bench.signals.C, 2, timeout=1.0) == 2
    (error,) = [r for r in logs.records() if r.levelno == logging.ERROR]
    assert error.getMessage() == (
        "can0: failed to handle a frame: 0x003 [8] 00 01 00 00 00 00 00 00: RuntimeError: boom"
    )
    assert error.exc_info is not None
    assert bench.check()["can0"] == []


def test_decoding_a_frame_that_does_not_match_raises_frame_error():
    import cantools

    message = cantools.database.load_string(MUX_DBC, "dbc").get_message_by_name("MuxOut")
    with pytest.raises(FrameError, match="message MuxOut: expected multiplexer id 0, but got 9"):
        CanCodec().decode(message, bytes([9, 0, 0, 0, 0, 0, 0, 0]))


@pytest.mark.parametrize(
    ("frame", "shown"),
    [
        (
            can.Message(arbitration_id=0x101, data=b"\x07\x01", is_extended_id=False),
            "0x101 [2] 07 01",
        ),
        (
            can.Message(arbitration_id=0x18FEF100, data=b"\xff", is_extended_id=True),
            "0x18fef100 (extended) [1] ff",
        ),
        (can.Message(arbitration_id=0x7, data=b"", is_extended_id=False), "0x007 [0]"),
    ],
)
def test_frames_are_shown_as_on_a_bus_trace(frame, shown):
    assert describe_frame(frame) == shown


# --- other background threads ---------------------------------------------------------------------


def test_a_sampler_that_stops_is_reported(logs, monkeypatch):
    with open_reference(reference.HARNESS_IO) as bench:
        bench.wait_until(bench.signals.Mode, "standby", timeout=2.0)

        def boom(key, value):
            raise RuntimeError("boom")

        monkeypatch.setattr(bench._buffer, "deliver", boom)
        eventually(lambda: bench.check()["io"] != [])
        assert bench.check()["io"] == ["stopped sampling: RuntimeError: boom"]
        bench.write(bench.signals.supply_enable, True)
        with pytest.raises(ReadTimeout, match="equipment 'io' reports: stopped sampling"):
            bench.read(bench.signals.supply_ok, timeout=0.1)
    (error,) = [r for r in logs.records() if r.levelno == logging.ERROR]
    assert error.getMessage() == "io stopped sampling: RuntimeError: boom"
    assert error.exc_info is not None


def test_a_failing_reference_dut_action_is_logged(logs, monkeypatch):
    with open_reference(reference.HARNESS_IO) as bench:
        bench.wait_until(bench.signals.Mode, "standby", timeout=2.0)
        device = bench.simulation.device

        def boom(setpoint):
            raise RuntimeError("boom")

        monkeypatch.setattr(device, "_answer", boom)
        bench.write(bench.signals.Setpoint, 1.0)
        eventually(lambda: device.errors)
    (error,) = [r for r in logs.records() if r.levelno == logging.ERROR]
    assert error.getMessage() == "reference DUT: an action failed: RuntimeError: boom"
    assert error.exc_info is not None


def test_simulated_failures_and_cuts_are_logged(logs):
    with open_reference(reference.HARNESS_IO) as bench:
        bench.simulation.cut("io.in2")
        bench.simulation.fail("io", "DAQ unplugged")
    info = logs(logging.INFO)
    assert "simulated wire cut at io.in2" in info
    assert "io: simulated failure: DAQ unplugged" in info
