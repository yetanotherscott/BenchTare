"""DUT identity (the interface description's identity: list, bench.identify, reading identity
signals) and the equipment's own identity, as the bench collects them."""

import textwrap

import pytest

from benchtare import reference
from benchtare.backends.base import BenchError
from benchtare.bench.runtime import open_bench
from benchtare.dut.description import load_interface_description
from benchtare.dut.identity import IdentityItem
from benchtare.errors import ConfigError
from benchtare.record import IDENTIFY, IDENTITY_CHANGE

SIGNALS = """\
dut: x
signals:
  Ver: {type: int, unit: none, direction: output}
  Cmd: {type: int, unit: none, direction: input}
"""


@pytest.fixture
def load(tmp_path):
    def _load(identity_yaml):
        path = tmp_path / "x.dut.yaml"
        path.write_text(SIGNALS + textwrap.dedent(identity_yaml))
        return load_interface_description(path).identity

    return _load


def open_reference(harness=reference.HARNESS_CAN, **device_options):
    return open_bench(
        reference.path(reference.DUT),
        reference.path(reference.BENCH),
        reference.path(harness),
        device_options=device_options or None,
    )


# --- the identity: list ----------------------------------------------------------------------------


def test_identity_can_be_a_plain_list_of_names(load):
    assert load("identity: [hardware_revision, bootloader]\n") == {
        "hardware_revision": IdentityItem("hardware_revision"),
        "bootloader": IdentityItem("bootloader"),
    }


def test_identity_items_can_name_a_signal_to_read(load):
    identity = load(
        """
        identity:
          hardware_revision:
          software: {signal: Ver, within: 0.5}
        """
    )
    assert identity["software"] == IdentityItem("software", signal="Ver", within=0.5)
    assert identity["hardware_revision"].signal is None


def test_no_identity_is_fine(load):
    assert load("") == {}


@pytest.mark.parametrize(
    ("identity_yaml", "message"),
    [
        ("identity: yes\n", "'identity:' must list the items to record, not true/false"),
        ("identity: [a, a]\n", "identity: 'a' is listed twice"),
        ("identity: [bad name]\n", "'bad name' can't name an identity item"),
        ("identity:\n  1:\n", "identity: an item's name was read as a number"),
        ("identity:\n  sw: {signal: Vr}\n", "signal 'Vr' isn't in this interface description"),
        ("identity:\n  sw: {signal: Cmd}\n", "signal 'Cmd' is a DUT input"),
        ("identity:\n  sw: {signal: Ver, within: 0}\n", "'within:' must be a number of seconds"),
        ("identity:\n  sw: {sginal: Ver}\n", "unknown key 'sginal' in identity: sw"),
        ("identity:\n  sw: 5\n", "identity: sw: must be empty, or say how to read it"),
    ],
)
def test_a_wrong_identity_list_is_reported(load, identity_yaml, message):
    with pytest.raises(ConfigError) as info:
        load(identity_yaml)
    assert message in str(info.value)


def test_a_near_miss_signal_name_is_suggested(load):
    with pytest.raises(ConfigError, match="did you mean 'Ver'"):
        load("identity:\n  sw: {signal: Vr}\n")


def test_the_reference_lists_its_software_version():
    description = load_interface_description(reference.path(reference.DUT))
    assert description.identity == {"software": IdentityItem("software", signal="SwVersion")}


# --- collecting values -----------------------------------------------------------------------------


@pytest.mark.parametrize("harness", [reference.HARNESS_CAN, reference.HARNESS_IO])
def test_identity_signals_are_read_before_the_first_test(harness):
    with open_reference(harness) as bench:
        bench.read_identity()
        assert bench.identity == {"software": "102"}
        (value,) = bench.identity_values()
    assert (value.name, value.value, value.source, value.expected) == (
        "software",
        "102",
        "signal",
        True,
    )


def test_any_name_can_be_identified_and_several_at_once():
    with open_reference() as bench:
        bench.identify("hardware_revision", "C")
        bench.identify({"motor.firmware": "2.3.1", "pump.firmware": "1.0"})
        assert bench.identity == {
            "hardware_revision": "C",
            "motor.firmware": "2.3.1",
            "pump.firmware": "1.0",
        }
        assert [v.expected for v in bench.identity_values()] == [False, False, False]
        assert [v.source for v in bench.identity_values()] == ["code"] * 3


def test_values_are_stored_as_text():
    with open_reference() as bench:
        bench.identify("build", 4711)
        assert bench.identity["build"] == "4711"


def test_the_same_value_again_changes_nothing():
    with open_reference() as bench:
        entries = []
        bench.recorder = entries.append
        bench.identify("bootloader", "1.4.2")
        bench.identify("bootloader", "1.4.2")
        assert len(bench.identity_values()) == 1
        assert [e.kind for e in entries] == [IDENTIFY]
        assert entries[0].text == "identify bootloader = '1.4.2' (from the team's code)"


def test_a_change_during_the_run_keeps_both_values_and_is_unexpected():
    with open_reference() as bench:
        entries = []
        bench.recorder = entries.append
        bench.identify("bootloader", "1.4.2")
        bench.identify("bootloader", "1.5.0")
        assert bench.identity == {"bootloader": "1.5.0"}
        assert [v.value for v in bench.identity_values()] == ["1.4.2", "1.5.0"]
    change = next(e for e in entries if e.kind == IDENTITY_CHANGE)
    assert change.unexpected
    assert change.text == (
        "the DUT's bootloader changed during the run: it was '1.4.2', and is now '1.5.0' "
        "(from the team's code)"
    )


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("", "1", "an identity item needs a name"),
        ("bad name", "1", "'bad name' can't name an identity item"),
        ("bootloader", None, "identity item 'bootloader' needs a value"),
    ],
)
def test_a_wrong_identity_is_refused(name, value, message):
    with open_reference() as bench, pytest.raises(BenchError, match=message):
        bench.identify(name, value)


# --- what is missing, and why ----------------------------------------------------------------------


def write_reference_with(tmp_path, identity_yaml, harness_text=None):
    for name in (reference.DUT, reference.DBC, reference.BENCH, reference.HARNESS_CAN):
        (tmp_path / name).write_text(reference.path(name).read_text())
    dut = tmp_path / reference.DUT
    text = dut.read_text().replace("identity:\n  software: {signal: SwVersion}\n", identity_yaml)
    dut.write_text(text)
    if harness_text is not None:
        (tmp_path / reference.HARNESS_CAN).write_text(harness_text)
    return open_bench(dut, tmp_path / reference.BENCH, tmp_path / reference.HARNESS_CAN)


def test_listed_items_without_a_value_are_missing(tmp_path):
    identity = "identity:\n  software: {signal: SwVersion}\n  bootloader:\n"
    with write_reference_with(tmp_path, identity) as bench:
        bench.read_identity()
        assert bench.missing_identity() == {"bootloader": ""}
        bench.identify("bootloader", "1.4.2")
        assert bench.missing_identity() == {}


def test_a_signal_that_is_not_connected_says_so(tmp_path):
    harness = reference.path(reference.HARNESS_CAN).read_text().replace("  SwVersion: can0\n", "")
    identity = "identity:\n  software: {signal: SwVersion}\n"
    with write_reference_with(tmp_path, identity, harness) as bench:
        bench.read_identity()
        assert bench.missing_identity() == {
            "software": "its signal SwVersion isn't connected in harness file harness-can.yaml"
        }


def test_a_signal_that_never_arrives_says_so(tmp_path):
    identity = "identity:\n  software: {signal: SwVersion, within: 0.1}\n"
    with write_reference_with(tmp_path, identity) as bench:
        bench.simulation.cut("can0")
        bench.read_identity()
        assert bench.missing_identity() == {
            "software": "no value of SwVersion arrived within 0.1 s"
        }


# --- the equipment's identity ----------------------------------------------------------------------


def test_simulated_equipment_reports_itself():
    with open_reference() as bench:
        equipment = {item["equipment"]: item for item in bench.equipment()}
    can0, io = equipment["can0"], equipment["io"]
    assert (can0["backend"], can0["kind"], can0["simulated"]) == ("sim_can", "bus", True)
    assert can0["driver"] == "python-can virtual bus" and can0["driver_version"]
    assert can0["vendor"] is None and can0["serial_number"] is None  # no hardware to ask
    assert can0["details"]["fd"] is False
    assert (io["backend"], io["driver"]) == ("sim", "benchtare sim")
    assert io["details"]["channels"]["out4"] == "drive"
    assert io["details"]["channels"]["in5"] == "read"


def test_a_backend_that_cannot_describe_itself_is_noted_not_fatal(monkeypatch):
    with open_reference() as bench:

        def broken():
            raise OSError("adapter unplugged")

        monkeypatch.setattr(bench.backend("can0"), "identity", broken)
        can0 = next(item for item in bench.equipment() if item["equipment"] == "can0")
    assert can0["details"] == {"identity_error": "OSError: adapter unplugged"}
    assert can0["driver"] is None
