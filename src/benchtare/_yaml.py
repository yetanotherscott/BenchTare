"""Reading the YAML files a team writes, with errors that say where the problem is.

On top of plain YAML parsing, ``read_mapping``:

- rejects a key given twice in the same section, with both line numbers. Plain YAML silently
  keeps the last one, so a copy-paste slip would rewire a signal without a word;
- reports a file that isn't UTF-8 text, with the line of the first bad byte;
- recognises the wrong kind of file (a harness file given as the bench file, a DBC file given
  as the interface description) and says so.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from benchtare.errors import ConfigError, Problem

# The key each kind of file starts with, used to recognise a file given in the wrong place.
KIND_KEYS = {
    "interface description": "dut",
    "bench file": "bench",
    "harness file": "harness",
}

YAML_VALUE_WORDS = "YAML reads unquoted on, off, yes, no, true, false and null as values"


class _StrictLoader(yaml.SafeLoader):
    """A safe loader that records keys given twice in one mapping."""

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self.duplicates: list[tuple[Any, int, int]] = []

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: dict[Any, int] = {}
        for key_node, _ in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                continue  # '<<: *anchor' merges are meant to be overridden
            key = self.construct_object(key_node, deep=True)
            try:
                hash(key)
            except TypeError:
                continue  # the base class reports unhashable keys
            line = key_node.start_mark.line + 1
            if key in seen:
                self.duplicates.append((key, seen[key], line))
            else:
                seen[key] = line
        return super().construct_mapping(node, deep=deep)


def read_mapping(path: Path, *, kind: str) -> dict[Any, Any]:
    """Read a YAML file whose top level must be a mapping. Raise ``ConfigError`` otherwise."""

    def fail(problem: Problem) -> ConfigError:
        return ConfigError(path, [problem], kind=kind)

    def not_a_dbc() -> ConfigError:
        return fail(
            Problem(
                f"this is a DBC file, not {a(kind)}",
                hint="DBC files are listed under 'can: dbc:' in the interface description; "
                f"give the {kind} (.yaml) here",
            )
        )

    if path.suffix.lower() == ".dbc":
        raise not_a_dbc()

    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise fail(
            Problem("file not found", hint=f"the path resolved to {path.resolve()}")
        ) from None
    except OSError as exc:
        raise fail(Problem(f"can't read the file: {exc.strerror or exc}")) from None

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        line = raw[: exc.start].count(b"\n") + 1
        raise fail(
            Problem(
                f"isn't UTF-8 text: byte 0x{raw[exc.start]:02x} can't be read",
                where=f"line {line}",
                hint="save the file as UTF-8; most editors offer this under 'Save with encoding'",
            )
        ) from None

    if text.lstrip("﻿ \t\r\n").startswith(_DBC_STARTS):
        raise not_a_dbc()

    loader = _StrictLoader(text)
    try:
        data = loader.get_single_data()
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"line {mark.line + 1}, column {mark.column + 1}" if mark else None
        detail = getattr(exc, "problem", None) or str(exc)
        raise fail(
            Problem(
                f"YAML syntax error: {detail}",
                where=where,
                hint="unclosed brackets or braces, indentation, and the ':' after each key",
            )
        ) from None
    finally:
        loader.dispose()

    if loader.duplicates:
        raise ConfigError(
            path,
            [
                Problem(
                    f"{_show_key(key)} is given twice in the same section, on line {first} "
                    f"and line {second}",
                    where=f"line {second}",
                    hint="remove one or rename it; YAML would silently keep only the last one",
                )
                for key, first, second in loader.duplicates
            ],
            kind=kind,
        )

    if data is None:
        raise fail(Problem("the file is empty"))
    if not isinstance(data, dict):
        raise fail(
            Problem(f"the top level must be a set of 'key: value' entries, not {kind_of(data)}")
        )

    own_key = KIND_KEYS.get(kind)
    if own_key is not None and own_key not in data:
        for other_kind, other_key in KIND_KEYS.items():
            if other_kind != kind and other_key in data:
                raise fail(
                    Problem(
                        f"this looks like {a(other_kind)} (it has '{other_key}:'), not {a(kind)}",
                        hint=f"which file was given as the {kind}",
                    )
                )
    return data


# How a DBC file starts; used to recognise one given where a YAML file belongs.
_DBC_STARTS = ('VERSION "', "NS_ :", "BS_:", "BU_:", "BO_ ")


def a(noun: str) -> str:
    """'a bench file', 'an interface description'."""
    return f"{'an' if noun[:1].lower() in 'aeiou' else 'a'} {noun}"


def kind_of(value: object) -> str:
    """Describe what YAML made of a value, for messages: 'a list', 'a number', ..."""
    if value is None:
        return "nothing (an empty value)"
    if isinstance(value, bool):
        return f"true/false ({str(value).lower()})"
    if isinstance(value, (int, float)):
        return f"a number ({value})"
    if isinstance(value, str):
        return "text"
    if isinstance(value, list):
        return "a list"
    if isinstance(value, dict):
        return "a set of 'key: value' entries"
    return type(value).__name__


def _show_key(key: Any) -> str:
    return f"'{key}'" if isinstance(key, str) else f"the key {key!r}"
