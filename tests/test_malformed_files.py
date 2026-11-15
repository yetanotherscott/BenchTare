"""Malformed interface descriptions, bench files and harness files.

Every malformed file must give a ConfigError that says what is wrong, never a traceback and
never a silently wrong setup.
"""

import textwrap

import pytest

from benchtare import reference
from benchtare.bench import load_bench_file
from benchtare.dut import load_interface_description
from benchtare.errors import ConfigError
from benchtare.harness import load_harness_file

REF_DUT = load_interface_description(reference.path(reference.DUT))
REF_BENCH = load_bench_file(reference.path(reference.BENCH))

LOADERS = {
    "dut": load_interface_description,
    "bench": load_bench_file,
    "harness": lambda path: load_harness_file(path, REF_DUT, REF_BENCH),
}
VALID = {
    "dut": "dut: x\nsignals:\n  a: {type: bool, direction: input}\n",
    "bench": "bench: b\nequipment:\n  can0: {backend: sim_can}\n",
    "harness": "harness: h\nconnections:\n  Setpoint: can0\n",
}
KIND_NAMES = {
    "dut": "an interface description",
    "bench": "a bench file",
    "harness": "a harness file",
}


def load(kind, tmp_path, content, name="file.yaml"):
    path = tmp_path / name
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(textwrap.dedent(content))
    return LOADERS[kind](path)


def error_text(kind, tmp_path, content, name="file.yaml"):
    with pytest.raises(ConfigError) as info:
        load(kind, tmp_path, content, name)
    return str(info.value)


# --- shared by all three kinds of file ------------------------------------------------------------


@pytest.mark.parametrize("kind", LOADERS)
def test_valid_minimal_files_load(kind, tmp_path):
    load(kind, tmp_path, VALID[kind])


@pytest.mark.parametrize("kind", LOADERS)
def test_duplicate_top_level_key_names_both_lines(kind, tmp_path):
    first_line = VALID[kind].splitlines()[0]
    text = error_text(kind, tmp_path, first_line + "\n" + VALID[kind])
    assert "is given twice in the same section, on line 1 and line 2" in text
    assert "YAML would silently keep only the last one" in text


@pytest.mark.parametrize(
    "kind, content, key",
    [
        (
            "dut",
            (
                "dut: x\nsignals:\n  a: {type: bool, direction: input}\n"
                "  a: {type: bool, direction: output}\n"
            ),
            "'a'",
        ),
        (
            "bench",
            "bench: b\nequipment:\n  can0: {backend: sim_can}\n  can0: {backend: sim_can}\n",
            "'can0'",
        ),
        (
            "harness",
            "harness: h\nconnections:\n  supply_enable: io.out1\n  supply_enable: io.out2\n",
            "'supply_enable'",
        ),
    ],
)
def test_duplicate_nested_key_is_rejected_even_when_both_are_valid(kind, tmp_path, content, key):
    # Plain YAML would keep the second silently: a copy-paste slip would rewire the setup.
    text = error_text(kind, tmp_path, content)
    assert f"{key} is given twice in the same section, on line 3 and line 4" in text


def test_every_duplicate_is_reported(tmp_path):
    content = (
        "harness: h\nconnections:\n  Setpoint: can0\n  Setpoint: can0\n  Mode: can0\n  Mode: can0\n"
    )
    with pytest.raises(ConfigError) as info:
        load("harness", tmp_path, content)
    assert len(info.value.problems) == 2


def test_yaml_merge_keys_are_not_duplicates(tmp_path):
    bench = load(
        "bench",
        tmp_path,
        """\
        bench: b
        equipment:
          io: &io {backend: sim, channels: {a: {read: true}}}
          io2:
            <<: *io
            channels: {b: {drive: true}}
        """,
    )
    assert list(bench.equipment["io2"].channels) == ["b"]


@pytest.mark.parametrize("kind", LOADERS)
def test_file_that_is_not_utf8_gives_the_line(kind, tmp_path):
    content = VALID[kind].encode() + b"# caf\xe9 in Latin-1\n"
    text = error_text(kind, tmp_path, content)
    lines = VALID[kind].count("\n") + 1
    assert f"line {lines}: isn't UTF-8 text: byte 0xe9 can't be read" in text
    assert "save the file as UTF-8" in text


@pytest.mark.parametrize("kind", LOADERS)
def test_utf8_with_byte_order_mark_loads(kind, tmp_path):
    load(kind, tmp_path, b"\xef\xbb\xbf" + VALID[kind].encode())


@pytest.mark.parametrize("kind", LOADERS)
def test_dbc_file_given_by_extension(kind, tmp_path):
    text = error_text(kind, tmp_path, reference.path(reference.DBC).read_text(), "x.dbc")
    assert f"this is a DBC file, not {KIND_NAMES[kind]}" in text


@pytest.mark.parametrize("kind", LOADERS)
def test_dbc_file_given_by_content(kind, tmp_path):
    text = error_text(kind, tmp_path, reference.path(reference.DBC).read_text(), "x.yaml")
    assert f"this is a DBC file, not {KIND_NAMES[kind]}" in text


@pytest.mark.parametrize("kind, other", [(k, o) for k in LOADERS for o in LOADERS if k != o])
def test_wrong_kind_of_file_is_named(kind, other, tmp_path):
    text = error_text(kind, tmp_path, VALID[other])
    assert f"this looks like {KIND_NAMES[other]}" in text
    assert f"not {KIND_NAMES[kind]}" in text
    assert "Check: which file was given as the" in text


@pytest.mark.parametrize("kind", LOADERS)
def test_directory_instead_of_file(kind, tmp_path):
    folder = tmp_path / "folder.yaml"
    folder.mkdir()
    with pytest.raises(ConfigError, match="can't read the file"):
        LOADERS[kind](folder)


# --- a corpus of malformed files: each must give a ConfigError with a plain message ---------------

CORPUS = [
    # kind, content, expected text
    ("bench", "", "the file is empty"),
    ("bench", "# nothing\n", "the file is empty"),
    ("bench", "- a\n", "not a list"),
    (
        "bench",
        "bench: b\nequipment:\n\tio: {backend: sim}\n",
        "line 3, column 1: YAML syntax error",
    ),
    ("bench", "bench: a\n---\nbench: b\n", "YAML syntax error"),
    ("bench", "bench: b\nequipment: [io]\n", "'equipment:' must list"),
    ("bench", "bench: b\nequipment:\n  io: sim\n", "must have properties"),
    ("bench", "bench: b\nequipment:\n  io: {backend: [sim]}\n", "must name a backend, not a list"),
    ("bench", "bench: b\nequipment:\n  1: {backend: sim_can}\n", "read as a number (1)"),
    ("bench", "bench: b\nequipment:\n  on: {backend: sim_can}\n", "read as true/false"),
    ("bench", "bench: b\nequipment:\n  io: {backend: sim, channels: [a]}\n", "needs 'channels:'"),
    (
        "bench",
        "bench: b\nequipment:\n  io: {backend: sim, channels: {on: {read: true}}}\n",
        "a channel name was read as true/false",
    ),
    ("bench", "bench: b\nequipment:\n  io: {backend: sim, channels: {a: true}}\n", "must say"),
    (
        "bench",
        "bench: b\nequipment:\n  io: {backend: sim, channels: {a: {drive: 'yes'}}}\n",
        "must be drivable, readable or both",
    ),
    (
        "bench",
        "bench: b\nequipment:\n  can0: {backend: sim_can, bus: 5}\n",
        "'bus:' must be a name",
    ),
    ("bench", "bench: 7\nequipment:\n  can0: {backend: sim_can}\n", "'bench:' must name the bench"),
    ("harness", "harness: h\nconnections: [Setpoint]\n", "must map signal names"),
    ("harness", "harness: h\nconnections: {Setpoint: [can0]}\n", "must name an equipment item"),
    ("harness", "harness: h\nconnections: {Setpoint: }\n", "must name an equipment item"),
    ("harness", "harness: h\nconnections: {on: can0}\n", "a signal name was read as true/false"),
    ("harness", "harness: h\nconnections: {supply_enable: io.}\n", "'io' has channels"),
    ("harness", "harness: h\nconnections: {supply_enable: .out1}\n", "no equipment ''"),
    ("harness", "harness: h\nconnections: {supply_enable: io.out1.x}\n", "no channel 'out1.x'"),
    ("harness", "harness: h\nconnections: {}\nloopbacks: {drive: io.out4}\n", "must be a list"),
    ("harness", "harness: h\nconnections: {}\nloopbacks: [io.out4]\n", "must be {drive:"),
    (
        "harness",
        "harness: h\nconnections: {}\nloopbacks: [{drive: 1, read: 2}]\n",
        "must name an equipment item",
    ),
    ("harness", "harness: h\nconnections: {}\npresence: can0\n", "'presence:' must be a list"),
    (
        "harness",
        "harness: h\nconnections: {}\npresence: [{bus: [can0], within: 1}]\n",
        "'bus:' must be a name, not a list",
    ),
    (
        "harness",
        "harness: h\nconnections: {}\npresence: [{signal: , within: 1}]\n",
        "'signal:' must be a name, not nothing",
    ),
    (
        "harness",
        "harness: h\nconnections: {}\npresence: [{bus: can0, within: '1'}]\n",
        "'within:' must be a number",
    ),
    (
        "harness",
        "harness: h\nconnections: {}\nsimulate: [reference]\n",
        "must name a simulated DUT, not a list",
    ),
    ("harness", "harness: [h]\nconnections: {}\n", "'harness:' must name the harness"),
    ("dut", "dut: [x]\nsignals: {a: {type: bool, direction: input}}\n", "'dut:' must name"),
    ("dut", "dut: x\ncan: [REFDEV]\n", "'can:' must contain"),
    ("dut", "dut: x\ncan: {node: 5, dbc: x.dbc}\n", "node name in the DBC, not a number (5)"),
    ("dut", "dut: x\ncan: {node: REFDEV, dbc: []}\n", "'can: dbc:' is an empty list"),
    ("dut", "dut: x\ncan: {node: REFDEV, dbc: 5}\n", "must be a file name or a list"),
    ("dut", "dut: x\nsignals: [a]\n", "must map each signal name"),
    ("dut", "dut: x\nsignals:\n  a:\n", "must have properties"),
    (
        "dut",
        "dut: x\nsignals:\n  on: {type: bool, direction: input}\n",
        "a signal name was read as true/false",
    ),
    (
        "dut",
        "dut: x\nsignals:\n  ~: {type: bool, direction: input}\n",
        "a signal name was read as nothing",
    ),
    (
        "dut",
        "dut: x\nsignals: {a: {type: [bool], direction: input}}\n",
        "type must be one of float, int, bool, enum, not a list",
    ),
    (
        "dut",
        "dut: x\nsignals: {a: {type: Float, unit: V, direction: input}}\n",
        "did you mean 'float'? (written in lower case)",
    ),
    (
        "dut",
        "dut: x\nsignals: {a: {type: bool, direction: [input]}}\n",
        "direction must be input or output, not a list",
    ),
    ("dut", "dut: x\nsignals: {a: {type: bool, direction: in}}\n", "did you mean 'input'?"),
    (
        "dut",
        "dut: x\nsignals: {a: {type: float, unit: [V], direction: input}}\n",
        "unit must be text",
    ),
    (
        "dut",
        "dut: x\nsignals: {a: {type: enum, values: 'a,b', direction: input}}\n",
        "values must be a list",
    ),
    ("dut", "dut: x\nsignals: {'my sig': {type: bool, direction: input}}\n", "can't be used"),
    (
        "dut",
        "dut: x\nsignals: {a: {type: bool, direction: input}}\noverrides: [x]\n",
        "'overrides:' must map",
    ),
    (
        "dut",
        "dut: x\nsignals: {a: {type: bool, direction: input}}\noverrides: {5: {unit: V}}\n",
        "a signal name was read as a number (5)",
    ),
    (
        "dut",
        "dut: x\nstandards: j1939\nsignals: {a: {type: bool, direction: input}}\n",
        "'standards:' must be a list",
    ),
    (
        "dut",
        "dut: x\nstandards: [~]\nsignals: {a: {type: bool, direction: input}}\n",
        "each entry must name a standard, not nothing",
    ),
]


@pytest.mark.parametrize(
    "kind, content, expected", CORPUS, ids=[f"{k}-{i}" for i, (k, _, _) in enumerate(CORPUS)]
)
def test_malformed_file_gives_a_plain_message(kind, content, expected, tmp_path):
    text = error_text(kind, tmp_path, content)
    assert expected in text
    # Messages describe what YAML read in words, not as Python values.
    assert "['" not in text and "'None'" not in text and "'True'" not in text
