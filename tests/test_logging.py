"""BenchTare's own log, for debugging BenchTare: what it logs, and the failures in background
threads it would otherwise lose (a bus that stops receiving, a sampler that dies). What the bench
did during a test goes into the test record instead; see test_record.py."""

import logging
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

CODEC_LOG = "benchtare.backends._can_codec"


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


def broken_recv(timeout=None):
    raise can.CanOperationError("bus off")


# --- what the bench logs -------------------------------------------------------------------------


def test_opening_and_closing_are_logged(logs):
    with open_reference(reference.HARNESS_CAN) as bench:
        bench.read(bench.signals.Mode, timeout=2.0)
    info = logs(logging.INFO)
    assert (
        "bench laptop-sim open: equipment can0 (sim_can), io (sim); harness reference-can; "
        "DUT reference (simulated: reference)"
    ) in info
    assert info[-1] == "bench laptop-sim closed"
    assert any(m.startswith("can0: opened virtual CAN bus benchtare-can0-") for m in logs())


def test_bench_calls_go_to_the_test_record_not_the_log(logs):
    with open_reference(reference.HARNESS_CAN) as bench:
        bench.read(bench.signals.Mode, timeout=2.0)
        bench.write(bench.signals.ModeRequest, "run")
        bench.wait_until(bench.signals.Mode, "run", timeout=2.0)
    calls = [m for m in logs() if m.startswith(("read ", "write ", "wait_until "))]
    assert calls == []
    # The backend still logs the frames it sends, for debugging BenchTare.
    assert "can0: sent Command 0x100 [8] 00 00 01 00 00 00 00 00" in logs(logging.DEBUG)


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
        "reference.dut.yaml: imported 5 signals from reference.dbc for node REFDEV",
        (
            "loaded interface description reference.dut.yaml: DUT reference, 7 signals "
            "(5 from DBC files, 2 listed inline)"
        ),
        "loaded bench file bench-sim.yaml: bench laptop-sim, equipment can0 (sim_can), io (sim)",
        (
            "loaded harness file harness-can.yaml: harness reference-can, 7 connections, "
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


def test_a_bus_that_stops_receiving_is_reported(logs, eventually):
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


def test_a_simulated_dut_port_that_stops_receiving_is_reported(logs, eventually):
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


def codec_messages(logs):
    return [r.getMessage() for r in logs.records() if r.name == CODEC_LOG]


def test_a_bad_frame_is_in_the_bench_log_at_debug(mux_bench, logs):
    # The test record keeps every bad frame (test_record.py); BenchTare's own log notes it only
    # for debugging.
    bench, send = mux_bench
    send([7, 1, 0, 0, 0, 0, 0, 0])  # multiplexer 7: the DBC only defines 0
    send([0, 5, 0, 0, 0, 0, 0, 0])
    assert bench.wait_until(bench.signals.C, 5, timeout=1.0) == 5
    (record,) = [r for r in logs.records() if r.name == CODEC_LOG]
    assert record.levelno == logging.DEBUG
    assert record.getMessage() == (
        "can0: ignored a frame that doesn't match the DBC: 0x003 [8] 07 01 00 00 00 00 00 00: "
        "message MuxOut: expected multiplexer id 0, but got 7\n"
        "  Check: the DBC files describe what the DUT sends"
    )


def test_repeated_bad_frames_are_counted_not_logged_each_time(
    mux_bench, logs, monkeypatch, eventually
):
    monkeypatch.setattr(FrameReceiver, "quiet_period", 0.3)
    bench, send = mux_bench
    for _ in range(3):
        send([7, 0, 0, 0, 0, 0, 0, 0])
    send([0, 1, 0, 0, 0, 0, 0, 0])
    bench.wait_until(bench.signals.C, 1, timeout=1.0)  # all four frames have been received
    assert len(codec_messages(logs)) == 1
    time.sleep(0.35)
    send([7, 0, 0, 0, 0, 0, 0, 0])
    eventually(lambda: len(codec_messages(logs)) == 2)
    assert codec_messages(logs)[1].endswith("(2 more like it since the last report)")


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


def test_decoding_a_frame_that_does_not_match_raises_frame_error(mux_message):
    with pytest.raises(FrameError, match="message MuxOut: expected multiplexer id 0, but got 9"):
        CanCodec().decode(mux_message, bytes([9, 0, 0, 0, 0, 0, 0, 0]))


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


def test_a_sampler_that_stops_is_reported(logs, monkeypatch, eventually):
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


def test_a_failing_reference_dut_action_is_logged(logs, monkeypatch, eventually):
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
