"""clean_state: in the interface description: what shows the DUT is clean, for the hygiene
check. The check itself is in test_check_hygiene.py and test_hygiene.py."""

import textwrap

import pytest

from benchtare.dut.description import load_interface_description
from benchtare.errors import ConfigError
from benchtare.hygiene import DEFAULT_SETTLE, INPUT, MISSING, OUTPUT, PROBE, CleanStateItem

SIGNALS = """\
dut: x
signals:
  Mode: {type: enum, values: [standby, run], direction: output}
  Count: {type: int, unit: none, direction: output}
  Temp: {type: float, unit: degC, direction: output}
  Enable: {type: bool, direction: input}
"""


@pytest.fixture
def load(tmp_path):
    def _load(clean_state_yaml):
        path = tmp_path / "x.dut.yaml"
        path.write_text(SIGNALS + textwrap.dedent(clean_state_yaml))
        return load_interface_description(path).clean_state

    return _load


def test_clean_state_can_be_a_plain_list_of_names(load):
    items = load("clean_state: [Mode, Enable]\n")
    assert items == {
        "Mode": CleanStateItem("Mode", OUTPUT),
        "Enable": CleanStateItem("Enable", INPUT),
    }
    assert not items["Mode"].has_clean
    assert items["Mode"].settle == DEFAULT_SETTLE


def test_a_clean_value_can_be_given_the_short_way(load):
    items = load(
        """
        clean_state:
          Mode: standby
          Enable: false
          Temp: 20
          Count: 0
        """
    )
    assert {name: item.clean for name, item in items.items()} == {
        "Mode": "standby",
        "Enable": False,
        "Temp": 20.0,
        "Count": 0,
    }
    assert isinstance(items["Temp"].clean, float)  # a float signal's clean value is a float


def test_options_give_a_clean_value_a_tolerance_and_a_settle_time(load):
    items = load(
        """
        clean_state:
          Temp: {clean: 20.0, tolerance: 0.5, settle: 3}
          Count: {settle: 0}
        """
    )
    assert items["Temp"] == CleanStateItem("Temp", OUTPUT, 0.5, 3.0, 20.0)
    assert items["Count"] == CleanStateItem("Count", OUTPUT, 0.0, 0.0, MISSING)


def test_a_state_probe_is_marked_as_one(load):
    items = load(
        """
        clean_state:
          fault_log: {probe: true}
          stored_faults: {probe: true, clean: 0, tolerance: 0}
        """
    )
    assert items["fault_log"] == CleanStateItem("fault_log", PROBE)
    assert items["stored_faults"].clean == 0


@pytest.mark.parametrize(
    ("clean_state_yaml", "message"),
    [
        ("clean_state: Mode\n", "'clean_state:' must list what shows the DUT is clean, not text"),
        (
            "clean_state: [Mod]\n",
            "clean_state: Mod: 'Mod' isn't a signal in this interface description",
        ),
        ("clean_state: [Mode, Mode]\n", "clean_state: 'Mode' is listed twice"),
        (
            "clean_state:\n  Mode: {probe: true}\n",
            "clean_state: Mode: 'Mode' is a signal, so it isn't a state probe",
        ),
        (
            "clean_state:\n  log: {probe: maybe}\n",
            "clean_state: log: 'probe:' must be true or false",
        ),
        (
            "clean_state:\n  Mode: {tolerance: 1}\n",
            "'tolerance:' is for numbers, and Mode is enum, which always compares exactly",
        ),
        (
            "clean_state:\n  Temp: {tolerance: -1}\n",
            "clean_state: Temp: 'tolerance:' must be a number, 0 or more",
        ),
        (
            "clean_state:\n  Temp: {settle: forever}\n",
            "clean_state: Temp: 'settle:' must be a number of seconds, 0 or more",
        ),
        (
            "clean_state:\n  Mode: idle\n",
            "its clean value 'idle' isn't one of Mode's values: standby, run",
        ),
        ("clean_state:\n  Enable: 0\n", "its clean value 0 isn't true or false"),
        (
            "clean_state:\n  Count: 1.5\n",
            "its clean value 1.5 isn't a whole number, and Count is int",
        ),
        (
            "clean_state:\n  Temp: warm\n",
            "its clean value 'warm' isn't a number, and Temp is float",
        ),
        (
            "clean_state:\n  Temp: [1, 2]\n",
            "must be empty, its clean value, or its options, not a list",
        ),
        ("clean_state:\n  Temp: {clean: 20, colour: red}\n", "'colour'"),
    ],
    ids=[
        "not-a-list",
        "unknown-signal",
        "twice",
        "signal-marked-probe",
        "probe-not-bool",
        "tolerance-on-enum",
        "negative-tolerance",
        "bad-settle",
        "enum-value",
        "bool-value",
        "int-value",
        "float-value",
        "list",
        "unknown-option",
    ],
)
def test_mistakes_are_named(load, clean_state_yaml, message):
    with pytest.raises(ConfigError) as info:
        load(clean_state_yaml)
    assert message in str(info.value)


def test_a_near_miss_says_what_was_meant_or_how_to_mark_a_probe(load):
    with pytest.raises(ConfigError) as info:
        load("clean_state: [Mod]\n")
    text = str(info.value)
    assert "did you mean 'Mode'?" in text
    assert "for state no signal shows, mark it as a state probe:  Mod: {probe: true}" in text


def test_no_clean_state_is_fine(load):
    assert load("") == {}
