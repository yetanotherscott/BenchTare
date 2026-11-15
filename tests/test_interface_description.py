"""Loading interface descriptions: inline signals, DBC imports and overrides."""

import textwrap

import pytest

from benchtare.dut import load_interface_description
from benchtare.errors import ConfigError
from benchtare.signals import Direction, SignalType

# A small DBC. REFDEV is the DUT. It receives Command, sends Status, and has nothing to do with
# Unrelated, which goes from OTHER to BENCH.
DBC = """\
VERSION ""

NS_ :

BS_:

BU_: REFDEV BENCH OTHER

BO_ 256 Command: 8 BENCH
 SG_ Setpoint : 0|16@1+ (0.1,0) [0|6553.5] "degC" REFDEV
 SG_ ModeRequest : 16|2@1+ (1,0) [0|3] "" REFDEV
 SG_ Enable : 18|1@1+ (1,0) [0|1] "" REFDEV
 SG_ BenchOnly : 24|8@1+ (1,0) [0|255] "rpm" BENCH

BO_ 257 Status: 8 REFDEV
 SG_ Measured : 0|16@1+ (0.1,0) [0|6553.5] "degC" BENCH
 SG_ Mode : 16|2@1+ (1,0) [0|3] "" BENCH
 SG_ Uptime : 24|16@1+ (1,0) [0|65535] "s" BENCH

BO_ 512 Unrelated: 8 OTHER
 SG_ Speed : 0|16@1+ (1,0) [0|65535] "rpm" BENCH

VAL_ 256 ModeRequest 0 "standby" 1 "run" ;
VAL_ 257 Mode 0 "standby" 1 "run" 2 "fault" ;
"""

# A second DBC that reuses the name Mode and has a numeric signal with no unit.
DBC_EXTRA = """\
VERSION ""

NS_ :

BS_:

BU_: REFDEV BENCH

BO_ 768 Diag: 8 REFDEV
 SG_ Mode : 0|8@1+ (1,0) [0|255] "x" BENCH
 SG_ Counter : 8|8@1+ (1,0) [0|255] "" BENCH
"""


@pytest.fixture
def write(tmp_path):
    def _write(name, text):
        path = tmp_path / name
        path.write_text(textwrap.dedent(text))
        return path

    return _write


def problems_of(excinfo):
    return [str(p) for p in excinfo.value.problems]


def test_inline_signals_only(write):
    path = write(
        "dut.yaml",
        """\
        dut: bench-psu
        signals:
          supply_voltage: {type: float, unit: V, direction: input}
          supply_ok: {type: bool, direction: output}
          state: {type: enum, values: [idle, busy], direction: output}
          count: {type: int, unit: none, direction: output}
        """,
    )
    description = load_interface_description(path)

    assert description.name == "bench-psu"
    assert list(description.signals) == ["supply_voltage", "supply_ok", "state", "count"]
    voltage = description.signals.supply_voltage
    assert (voltage.type, voltage.unit, voltage.direction) == (
        SignalType.FLOAT,
        "V",
        Direction.INPUT,
    )
    assert description.signals.state.values == ("idle", "busy")
    assert description.signals.count.unit is None
    assert description.files == (path,)
    assert description.can_signals == {}


def test_dbc_only(write):
    write("ref.dbc", DBC)
    path = write(
        "dut.yaml",
        """\
        dut: reference
        can: {node: REFDEV, dbc: ref.dbc}
        """,
    )
    description = load_interface_description(path)
    signals = description.signals

    assert set(signals) == {"Setpoint", "ModeRequest", "Enable", "Measured", "Mode", "Uptime"}
    assert signals.Setpoint.type is SignalType.FLOAT
    assert signals.Setpoint.unit == "degC"
    assert signals.Enable.type is SignalType.BOOL
    assert signals.Uptime.type is SignalType.INT
    assert signals.Mode.type is SignalType.ENUM
    assert signals.Mode.values == ("standby", "run", "fault")
    assert description.can_node == "REFDEV"
    assert description.files == (path, path.parent / "ref.dbc")

    ref = description.can_signals["Measured"]
    assert (ref.message, ref.frame_id, ref.signal) == ("Status", 257, "Measured")


def test_direction_comes_from_dbc_nodes(write):
    write("ref.dbc", DBC)
    path = write("dut.yaml", "dut: reference\ncan: {node: REFDEV, dbc: [ref.dbc]}\n")
    signals = load_interface_description(path).signals

    # Received by the DUT: an input. Sent by the DUT: an output.
    assert signals.Setpoint.direction is Direction.INPUT
    assert signals.Measured.direction is Direction.OUTPUT
    # Signals the DUT neither sends nor receives are not imported.
    assert "BenchOnly" not in signals
    assert "Speed" not in signals


def test_inline_and_dbc_mixed(write):
    write("ref.dbc", DBC)
    path = write(
        "dut.yaml",
        """\
        dut: reference
        can: {node: REFDEV, dbc: ref.dbc}
        signals:
          supply_enable: {type: bool, direction: input}
        """,
    )
    signals = load_interface_description(path).signals
    assert "Setpoint" in signals
    assert signals.supply_enable.direction is Direction.INPUT


def test_overrides_change_direction_unit_and_name(write):
    write("ref.dbc", DBC)
    write("extra.dbc", DBC_EXTRA)
    path = write(
        "dut.yaml",
        """\
        dut: reference
        can: {node: REFDEV, dbc: [ref.dbc, extra.dbc]}
        overrides:
          Uptime: {direction: input, unit: min}
          Diag.Mode: {name: DiagMode}
          Counter: {unit: none}
        """,
    )
    description = load_interface_description(path)
    signals = description.signals

    assert signals.Uptime.direction is Direction.INPUT
    assert signals.Uptime.unit == "min"
    assert signals.DiagMode.unit == "x"
    assert signals.Mode.type is SignalType.ENUM  # the original Mode is untouched
    assert signals.Counter.unit is None
    # A renamed signal still knows its name in the DBC.
    assert description.can_signals["DiagMode"].signal == "Mode"


# --- Problems -----------------------------------------------------------------------------------


def test_duplicate_name_across_sources_names_both_places(write):
    write("ref.dbc", DBC)
    path = write(
        "dut.yaml",
        """\
        dut: reference
        can: {node: REFDEV, dbc: ref.dbc}
        signals:
          Setpoint: {type: float, unit: V, direction: input}
        """,
    )
    with pytest.raises(ConfigError) as info:
        load_interface_description(path)
    (problem,) = problems_of(info)
    assert "signal 'Setpoint'" in problem
    assert "ref.dbc, message Command" in problem
    assert "dut.yaml, signals:" in problem


def test_duplicate_name_across_dbc_files_suggests_rename(write):
    write("ref.dbc", DBC)
    write("extra.dbc", DBC_EXTRA)
    path = write(
        "dut.yaml",
        """\
        dut: reference
        can: {node: REFDEV, dbc: [ref.dbc, extra.dbc]}
        overrides:
          Counter: {unit: none}
        """,
    )
    with pytest.raises(ConfigError) as info:
        load_interface_description(path)
    (problem,) = problems_of(info)
    assert "defined more than once" in problem
    assert "Diag.Mode: {name: NewName}" in problem


def test_unknown_type_is_rejected(write):
    path = write("dut.yaml", "dut: x\nsignals:\n  v: {type: double, unit: V, direction: input}\n")
    with pytest.raises(ConfigError) as info:
        load_interface_description(path)
    (problem,) = problems_of(info)
    assert "signal 'v'" in problem
    assert "unknown type 'double'" in problem
    assert "float, int, bool, enum" in problem


def test_inline_numeric_signal_without_unit_is_rejected(write):
    path = write("dut.yaml", "dut: x\nsignals:\n  v: {type: float, direction: input}\n")
    with pytest.raises(ConfigError, match=r"signal 'v'.*float signal has no unit[\s\S]*unit: none"):
        load_interface_description(path)


@pytest.mark.parametrize("unit", ["none", "None", "NONE"])
def test_unit_none_declares_a_unitless_numeric_signal(write, unit):
    path = write(
        "dut.yaml", f"dut: x\nsignals:\n  n: {{type: int, unit: {unit}, direction: output}}\n"
    )
    assert load_interface_description(path).signals.n.unit is None


@pytest.mark.parametrize("unit", ["null", "~", "''"])
def test_empty_unit_counts_as_missing(write, unit):
    # 'unit: null' is not a declaration that there is no unit; only 'unit: none' is.
    path = write(
        "dut.yaml", f"dut: x\nsignals:\n  n: {{type: int, unit: {unit}, direction: output}}\n"
    )
    with pytest.raises(ConfigError, match="signal 'n'"):
        load_interface_description(path)


def test_bool_and_enum_may_say_unit_none(write):
    path = write(
        "dut.yaml",
        """\
        dut: x
        signals:
          b: {type: bool, unit: none, direction: input}
          e: {type: enum, values: [a, b], unit: none, direction: input}
        """,
    )
    signals = load_interface_description(path).signals
    assert signals.b.unit is None
    assert signals.e.unit is None


def test_override_unit_needs_a_value(write):
    write("extra.dbc", DBC_EXTRA)
    path = write(
        "dut.yaml",
        "dut: x\ncan: {node: REFDEV, dbc: extra.dbc}\noverrides:\n  Counter: {unit: null}\n",
    )
    with pytest.raises(ConfigError, match="'unit:' has no value; give a unit, or none"):
        load_interface_description(path)


def test_dbc_signal_without_unit_hints_at_overrides(write):
    write("extra.dbc", DBC_EXTRA)
    path = write("dut.yaml", "dut: x\ncan: {node: REFDEV, dbc: extra.dbc}\n")
    with pytest.raises(ConfigError) as info:
        load_interface_description(path)
    (problem,) = problems_of(info)
    assert "signal 'Counter' (extra.dbc, message Diag)" in problem
    assert "Diag.Counter: {unit: none}" in problem


def test_all_problems_are_reported_together(write):
    path = write(
        "dut.yaml",
        """\
        dut: x
        signals:
          a: {type: float, direction: input}
          b: {type: wobbly, direction: input}
          c: {type: bool, direction: sideways}
        """,
    )
    with pytest.raises(ConfigError) as info:
        load_interface_description(path)
    assert len(info.value.problems) == 3
    assert "3 problems in the interface description" in str(info.value)


def test_missing_type_and_direction_are_reported(write):
    path = write("dut.yaml", "dut: x\nsignals:\n  a: {unit: V}\n")
    with pytest.raises(ConfigError) as info:
        load_interface_description(path)
    text = "\n".join(problems_of(info))
    assert "has no type" in text
    assert "has no direction" in text


def test_unquoted_on_off_enum_values_explain_yaml(write):
    path = write(
        "dut.yaml", "dut: x\nsignals:\n  s: {type: enum, values: [off, on], direction: input}\n"
    )
    with pytest.raises(ConfigError, match="quote them"):
        load_interface_description(path)


def test_standards_are_rejected_for_now(write):
    path = write(
        "dut.yaml",
        "dut: x\nstandards: [j1939]\nsignals:\n  s: {type: bool, direction: input}\n",
    )
    with pytest.raises(ConfigError, match="standard 'j1939' is not available in this version"):
        load_interface_description(path)


def test_empty_standards_list_is_fine(write):
    path = write(
        "dut.yaml", "dut: x\nstandards: []\nsignals:\n  s: {type: bool, direction: input}\n"
    )
    assert "s" in load_interface_description(path).signals


def test_unknown_top_level_key_suggests_fix(write):
    path = write("dut.yaml", "dut: x\nsignal:\n  s: {type: bool, direction: input}\n")
    with pytest.raises(ConfigError) as info:
        load_interface_description(path)
    assert any(
        "unknown key 'signal'" in p and "did you mean 'signals'" in p for p in problems_of(info)
    )


def test_missing_dut_name(write):
    path = write("dut.yaml", "signals:\n  s: {type: bool, direction: input}\n")
    with pytest.raises(ConfigError, match="'dut:' must name the DUT"):
        load_interface_description(path)


def test_no_signals_at_all(write):
    path = write("dut.yaml", "dut: x\n")
    with pytest.raises(ConfigError, match="the DUT has no signals"):
        load_interface_description(path)


def test_node_not_in_dbc_lists_nodes(write):
    write("ref.dbc", DBC)
    path = write("dut.yaml", "dut: x\ncan: {node: ECU1, dbc: ref.dbc}\n")
    with pytest.raises(ConfigError) as info:
        load_interface_description(path)
    (problem,) = problems_of(info)
    assert "node 'ECU1' is not in ref.dbc" in problem
    assert "REFDEV, BENCH, OTHER" in problem


def test_missing_node_lists_nodes(write):
    write("ref.dbc", DBC)
    path = write("dut.yaml", "dut: x\ncan: {dbc: ref.dbc}\n")
    with pytest.raises(
        ConfigError, match=r"no 'node:' entry[\s\S]*nodes found: BENCH, OTHER, REFDEV"
    ):
        load_interface_description(path)


def test_missing_dbc_file_shows_resolved_path(write, tmp_path):
    path = write("dut.yaml", "dut: x\ncan: {node: REFDEV, dbc: nowhere.dbc}\n")
    with pytest.raises(ConfigError) as info:
        load_interface_description(path)
    (problem,) = problems_of(info)
    assert "DBC file 'nowhere.dbc' not found" in problem
    assert str(tmp_path / "nowhere.dbc") in problem


def test_unreadable_dbc_reports_the_parser_error(write):
    write("bad.dbc", "BO_ garbage\n")
    path = write("dut.yaml", "dut: x\ncan: {node: REFDEV, dbc: bad.dbc}\n")
    with pytest.raises(ConfigError, match="can't read DBC file 'bad.dbc'.*line 1"):
        load_interface_description(path)


def test_override_of_signal_the_dut_does_not_use(write):
    write("ref.dbc", DBC)
    path = write(
        "dut.yaml",
        "dut: x\ncan: {node: REFDEV, dbc: ref.dbc}\noverrides:\n  Speed: {unit: rpm}\n",
    )
    with pytest.raises(ConfigError) as info:
        load_interface_description(path)
    (problem,) = problems_of(info)
    assert "ref.dbc, message Unrelated" in problem
    assert "neither sends nor receives it" in problem


def test_override_of_unknown_signal_suggests_close_names(write):
    write("ref.dbc", DBC)
    path = write(
        "dut.yaml",
        "dut: x\ncan: {node: REFDEV, dbc: ref.dbc}\noverrides:\n  Setpiont: {unit: V}\n",
    )
    with pytest.raises(ConfigError, match="did you mean Setpoint"):
        load_interface_description(path)


def test_override_of_inline_signal_is_rejected(write):
    path = write(
        "dut.yaml",
        "dut: x\nsignals:\n  s: {type: bool, direction: input}\noverrides:\n  s: {direction: output}\n",
    )
    with pytest.raises(ConfigError, match="change it there instead"):
        load_interface_description(path)


def test_override_cannot_change_type(write):
    write("ref.dbc", DBC)
    path = write(
        "dut.yaml",
        "dut: x\ncan: {node: REFDEV, dbc: ref.dbc}\noverrides:\n  Uptime: {type: float}\n",
    )
    with pytest.raises(ConfigError, match="can't override 'type'"):
        load_interface_description(path)


def test_ambiguous_override_asks_for_message_name(write):
    write("ref.dbc", DBC)
    write("extra.dbc", DBC_EXTRA)
    path = write(
        "dut.yaml",
        "dut: x\ncan: {node: REFDEV, dbc: [ref.dbc, extra.dbc]}\noverrides:\n  Mode: {unit: x}\n",
    )
    with pytest.raises(
        ConfigError, match="matches more than one imported signal: Status.Mode, Diag.Mode"
    ):
        load_interface_description(path)


def test_file_not_found(tmp_path):
    with pytest.raises(ConfigError, match="file not found"):
        load_interface_description(tmp_path / "missing.yaml")


def test_yaml_syntax_error_gives_line(write):
    path = write("dut.yaml", "dut: x\nsignals:\n  s: {type: bool, direction: input\n")
    with pytest.raises(ConfigError, match="line 4, column 1: YAML syntax error"):
        load_interface_description(path)


def test_top_level_must_be_a_mapping(write):
    path = write("dut.yaml", "- a\n- b\n")
    with pytest.raises(ConfigError, match="not a list"):
        load_interface_description(path)


def test_error_message_names_file_signal_and_what_to_check(write):
    path = write("dut.yaml", "dut: x\nsignals:\n  v: {type: float, direction: input}\n")
    with pytest.raises(ConfigError) as info:
        load_interface_description(path)
    text = str(info.value)
    assert str(path) in text
    assert "signal 'v'" in text
    assert "Check:" in text


# --- malformed DBC references -------------------------------------------------------------------


def test_dbc_path_that_is_a_directory(write, tmp_path):
    (tmp_path / "folder.dbc").mkdir()
    path = write("dut.yaml", "dut: x\ncan: {node: REFDEV, dbc: folder.dbc}\n")
    with pytest.raises(ConfigError, match="can't read DBC file 'folder.dbc': Is a directory"):
        load_interface_description(path)


def test_dbc_listed_twice_is_one_problem(write):
    write("ref.dbc", DBC)
    path = write("dut.yaml", "dut: x\ncan: {node: REFDEV, dbc: [ref.dbc, ./ref.dbc]}\n")
    with pytest.raises(ConfigError) as info:
        load_interface_description(path)
    (problem,) = problems_of(info)
    assert "DBC file './ref.dbc' is listed more than once (also as 'ref.dbc')" in problem


def test_dbc_the_dut_node_does_not_use(write):
    # LONELY is a node in the DBC but neither sends nor receives anything, so nothing imports.
    write(
        "lonely.dbc",
        'VERSION ""\n\nNS_ :\n\nBS_:\n\nBU_: REFDEV LONELY\n\n'
        'BO_ 1 M: 1 REFDEV\n SG_ S : 0|8@1+ (1,0) [0|255] "x" REFDEV\n',
    )
    path = write("dut.yaml", "dut: x\ncan: {node: LONELY, dbc: lonely.dbc}\n")
    with pytest.raises(ConfigError) as info:
        load_interface_description(path)
    (problem,) = problems_of(info)
    assert "imports no signals: node 'LONELY' neither sends nor receives any message" in problem


def test_dbc_empty_list(write):
    path = write("dut.yaml", "dut: x\ncan: {node: REFDEV, dbc: []}\n")
    with pytest.raises(ConfigError, match="'can: dbc:' is an empty list"):
        load_interface_description(path)


def test_node_that_is_not_text(write):
    write("ref.dbc", DBC)
    path = write("dut.yaml", "dut: x\ncan: {node: [REFDEV], dbc: ref.dbc}\n")
    with pytest.raises(ConfigError) as info:
        load_interface_description(path)
    (problem,) = problems_of(info)  # not also "has no 'node:' entry"
    assert "must be the DUT's node name in the DBC, not a list" in problem
