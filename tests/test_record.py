"""The test record at the bench: what each level keeps, and that unexpected events are kept at
every level. The plugin's side (per test, on reports, warnings) is in test_plugin.py."""

import logging
import re
import time

import can
import pytest

from benchtare import reference
from benchtare.backends.base import ReadTimeout
from benchtare.bench.runtime import open_bench
from benchtare.record import (
    EQUIPMENT_FAILURE,
    FRAME_MISMATCH,
    FRAME_RECEIVED,
    FRAME_SENT,
    READ,
    WAIT,
    WRITE,
    RecordEntry,
    RecordLevel,
    render,
)


def broken_recv(timeout=None):
    raise can.CanOperationError("bus off")


def open_recorded(level, harness=reference.HARNESS_CAN, **device_options):
    """The reference bench, with a recorder at ``level``. Returns (bench, entries)."""
    bench = open_bench(
        reference.path(reference.DUT),
        reference.path(reference.BENCH),
        reference.path(harness),
        device_options=device_options or None,
    )
    entries = []
    bench.record_level = level
    bench.recorder = entries.append
    return bench, entries


def stage_run(bench):
    bench.read(bench.signals.Mode, timeout=2.0)
    bench.write(bench.signals.ModeRequest, "run")
    bench.wait_until(bench.signals.Mode, "run", timeout=2.0)


def kinds(entries, *, debug=False):
    shown = [e.kind for e in entries]
    return shown if debug else [k for k in shown if k not in (FRAME_SENT, FRAME_RECEIVED)]


# --- what each level keeps ------------------------------------------------------------------------


def test_results_level_keeps_readings_and_comparisons_but_not_writes():
    bench, entries = open_recorded(RecordLevel.RESULTS)
    with bench:
        stage_run(bench)
    assert kinds(entries, debug=True) == [READ, WAIT]
    read, wait = entries
    assert re.fullmatch(r"read Mode = 'standby' on can0 after \d\.\d{3} s", read.text)
    assert re.fullmatch(
        r"wait_until Mode, expecting 'run': got 'run' on can0 after \d\.\d{3} s", wait.text
    )
    assert wait.data["expected"] == "'run'" and wait.data["outcome"] == "reached"


def test_stimuli_level_adds_writes():
    bench, entries = open_recorded(RecordLevel.STIMULI)
    with bench:
        stage_run(bench)
    assert kinds(entries, debug=True) == [READ, WRITE, WAIT]
    write = entries[1]
    assert write.text == "write ModeRequest = 'run' on can0"
    assert write.data == {"signal": "ModeRequest", "value": "run", "unit": None, "endpoint": "can0"}


def test_debug_level_adds_the_frames_sent_and_received():
    bench, entries = open_recorded(RecordLevel.DEBUG)
    with bench:
        stage_run(bench)
    assert kinds(entries) == [READ, WRITE, WAIT]
    texts = [e.text for e in entries]
    write = texts.index("write ModeRequest = 'run' on can0")
    assert texts[write + 1] == "can0: sent Command 0x100 [8] 00 00 01 00 00 00 00 00"
    heartbeats = [
        e for e in entries if e.kind == FRAME_RECEIVED and e.data["message"] == "Heartbeat"
    ]
    assert heartbeats, "the DUT's Heartbeat frames are recorded as received"
    assert heartbeats[0].text.startswith("can0: received Heartbeat 0x102 [8] ")
    assert heartbeats[0].data["frame_id"] == 0x102 and heartbeats[0].data["equipment"] == "can0"


def test_values_are_recorded_with_their_units():
    bench, entries = open_recorded(RecordLevel.STIMULI, reference.HARNESS_IO)
    with bench:
        stage_run(bench)
        bench.write(bench.signals.Setpoint, 42.0)
        bench.wait_until(bench.signals.Measured, pytest.approx(42.0, abs=0.5), timeout=2.0)
    assert "write Setpoint = 42.0 degC on io.out1" in [e.text for e in entries]
    wait = entries[-1]
    # pytest 7 shows the tolerance as 5.0e-01, later versions as 0.5.
    assert re.fullmatch(
        r"wait_until Measured, expecting 42\.0 ± \S+: got 42\.0 degC on io\.in1 after \d\.\d{3} s",
        wait.text,
    )
    assert wait.data["unit"] == "degC" and wait.data["value"] == 42.0


def test_timeouts_are_recorded():
    bench, entries = open_recorded(RecordLevel.RESULTS, boot_delay=30)  # the DUT never boots
    with bench:
        bench.write(bench.signals.Setpoint, 1.0)
        with pytest.raises(ReadTimeout):
            bench.read(bench.signals.Measured, timeout=0.05)
        with pytest.raises(ReadTimeout):
            bench.wait_until(bench.signals.Mode, "run", timeout=0.05)
    assert [e.text for e in entries] == [
        "read Measured: nothing within 0.05 s on can0",
        "wait_until Mode, expecting 'run': not reached within 0.05 s on can0; nothing received",
    ]
    assert [e.data["outcome"] for e in entries] == ["timeout", "timeout"]


# --- unexpected events are kept at every level ---------------------------------------------------


def test_every_frame_that_does_not_match_the_dbc_is_recorded(mux_bench):
    bench, send = mux_bench
    entries = []
    bench.record_level = RecordLevel.RESULTS
    bench.recorder = entries.append
    for _ in range(3):
        send([7, 1, 0, 0, 0, 0, 0, 0])  # multiplexer 7: the DBC only defines 0
    send([0, 5, 0, 0, 0, 0, 0, 0])
    assert bench.wait_until(bench.signals.C, 5, timeout=1.0) == 5  # reception went on
    mismatches = [e for e in entries if e.kind == FRAME_MISMATCH]
    assert len(mismatches) == 3  # every one, not just the first
    assert mismatches[0].text == (
        "can0: the DUT sent a frame that doesn't match the DBC: 0x003 [8] 07 01 00 00 00 00 00 00: "
        "message MuxOut: expected multiplexer id 0, but got 7"
    )
    assert mismatches[0].data == {
        "equipment": "can0",
        "message": "MuxOut",
        "problem": "message MuxOut: expected multiplexer id 0, but got 7",
        "frame_id": 3,
        "extended": False,
        "data": "07 01 00 00 00 00 00 00",
    }
    assert mismatches[0].unexpected and mismatches[0].level is RecordLevel.RESULTS
    assert bench.check()["can0"] == []  # the DUT misbehaved; the equipment is fine


def test_a_bus_that_stops_receiving_is_recorded(eventually):
    bench, entries = open_recorded(RecordLevel.RESULTS)
    with bench:
        bench.wait_until(bench.signals.Mode, "standby", timeout=2.0)
        bench._backends["can0"]._bus.recv = broken_recv
        eventually(lambda: any(e.kind == EQUIPMENT_FAILURE for e in entries))
    (failure,) = [e for e in entries if e.kind == EQUIPMENT_FAILURE]
    assert failure.text == "can0 stopped receiving: CanOperationError: bus off"
    assert failure.data == {
        "equipment": "can0",
        "problem": "stopped receiving: CanOperationError: bus off",
    }


def test_a_sampler_that_stops_is_recorded(eventually, monkeypatch):
    bench, entries = open_recorded(RecordLevel.RESULTS, reference.HARNESS_IO)
    with bench:
        bench.wait_until(bench.signals.Mode, "standby", timeout=2.0)

        def boom(key, value):
            raise RuntimeError("boom")

        monkeypatch.setattr(bench._buffer, "deliver", boom)
        eventually(lambda: any(e.kind == EQUIPMENT_FAILURE for e in entries))
    (failure,) = [e for e in entries if e.kind == EQUIPMENT_FAILURE]
    assert failure.text == "io stopped sampling: RuntimeError: boom"


def test_with_no_recorder_unexpected_events_are_logged_as_warnings(mux_bench, caplog):
    bench, send = mux_bench
    send([7, 1, 0, 0, 0, 0, 0, 0])
    send([0, 5, 0, 0, 0, 0, 0, 0])
    bench.wait_until(bench.signals.C, 5, timeout=1.0)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == [
        (
            "can0: the DUT sent a frame that doesn't match the DBC: 0x003 [8] 07 01 00 00 00 00 00 00: "
            "message MuxOut: expected multiplexer id 0, but got 7"
        )
    ]


# --- levels and entries --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "level"),
    [
        ("results", RecordLevel.RESULTS),
        ("Stimuli", RecordLevel.STIMULI),
        (" debug ", RecordLevel.DEBUG),
    ],
)
def test_levels_are_named(name, level):
    assert RecordLevel.named(name) is level


def test_an_unknown_level_lists_the_levels():
    with pytest.raises(
        ValueError,
        match="unknown test record level 'verbose'; the levels are results, stimuli, debug",
    ):
        RecordLevel.named("verbose")


def test_entries_render_with_the_time_and_become_plain_dicts():
    at = time.mktime((2026, 11, 15, 14, 3, 7, 0, 0, -1)) + 0.215
    entry = RecordEntry(WRITE, RecordLevel.STIMULI, "write X = 1 on io.out1", {"signal": "X"}, at)
    assert render([entry, entry]) == (
        "14:03:07.215 write X = 1 on io.out1\n14:03:07.215 write X = 1 on io.out1"
    )
    assert entry.as_dict() == {
        "kind": "write",
        "level": "stimuli",
        "at": at,
        "text": "write X = 1 on io.out1",
        "signal": "X",
    }
    assert not entry.unexpected
