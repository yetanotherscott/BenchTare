"""Loading bench files."""

import textwrap

import pytest

from benchtare import reference
from benchtare.backends.base import ChannelCaps
from benchtare.bench import load_bench_file
from benchtare.errors import ConfigError


@pytest.fixture
def bench_file(tmp_path):
    def _write(text):
        path = tmp_path / "bench.yaml"
        path.write_text(textwrap.dedent(text))
        return path

    return _write


def test_reference_bench_loads():
    bench = load_bench_file(reference.path(reference.BENCH))
    assert bench.name == "laptop-sim"
    assert set(bench.equipment) == {"can0", "io"}
    assert bench.equipment["can0"].kind == "bus"
    assert bench.equipment["can0"].channels == {}
    io = bench.equipment["io"]
    assert io.kind == "channels"
    assert io.channels["out1"] == ChannelCaps(drive=True)
    assert io.channels["in1"] == ChannelCaps(read=True)
    assert all(item.simulated for item in bench.equipment.values())


def test_channel_can_be_both_drivable_and_readable(bench_file):
    path = bench_file(
        """\
        bench: b
        equipment:
          io: {backend: sim, channels: {line: {drive: true, read: true}}}
        """
    )
    assert load_bench_file(path).equipment["io"].channels["line"] == ChannelCaps(True, True)


def test_unknown_backend_suggests_close_name(bench_file):
    path = bench_file("bench: b\nequipment:\n  can0: {backend: simcan}\n")
    with pytest.raises(ConfigError, match="(?s)unknown backend 'simcan'.*did you mean 'sim_can'"):
        load_bench_file(path)


def test_missing_backend(bench_file):
    path = bench_file("bench: b\nequipment:\n  can0: {bus: x}\n")
    with pytest.raises(ConfigError, match="(?s)has no 'backend:'.*backends: sim, sim_can"):
        load_bench_file(path)


def test_sim_needs_channels(bench_file):
    path = bench_file("bench: b\nequipment:\n  io: {backend: sim}\n")
    with pytest.raises(ConfigError, match="a sim backend needs 'channels:'"):
        load_bench_file(path)


def test_channel_must_be_drivable_or_readable(bench_file):
    path = bench_file("bench: b\nequipment:\n  io: {backend: sim, channels: {x: {drive: false}}}\n")
    with pytest.raises(ConfigError, match="channel 'x' must be drivable, readable or both"):
        load_bench_file(path)


def test_channel_with_unknown_capability(bench_file):
    path = bench_file("bench: b\nequipment:\n  io: {backend: sim, channels: {x: {write: true}}}\n")
    with pytest.raises(ConfigError, match=r"channel 'x' must say \{drive: true\}"):
        load_bench_file(path)


def test_sim_can_has_no_channels(bench_file):
    path = bench_file(
        "bench: b\nequipment:\n  can0: {backend: sim_can, channels: {a: {read: true}}}\n"
    )
    with pytest.raises(ConfigError, match="(?s)unknown option 'channels'.*has no channels"):
        load_bench_file(path)


@pytest.mark.parametrize("name", ["can.0", "my bus"])
def test_equipment_names_cannot_contain_dots_or_spaces(bench_file, name):
    path = bench_file(f"bench: b\nequipment:\n  '{name}': {{backend: sim_can}}\n")
    with pytest.raises(ConfigError, match="can't be used"):
        load_bench_file(path)


def test_bench_file_names_no_signals(bench_file):
    # A signal-to-channel mapping belongs in the harness file, not here.
    path = bench_file("bench: b\nequipment:\n  can0: {backend: sim_can}\nconnections: {}\n")
    with pytest.raises(ConfigError, match="unknown key 'connections'"):
        load_bench_file(path)


def test_needs_name_and_equipment(bench_file):
    path = bench_file("equipment: {}\n")
    with pytest.raises(ConfigError) as info:
        load_bench_file(path)
    text = str(info.value)
    assert "'bench:' must name the bench" in text
    assert "'equipment:' must list" in text
    assert "2 problems in the bench file" in text
