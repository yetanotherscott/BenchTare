"""Reading the YAML files a team writes, with errors that say where the problem is."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from benchtare.errors import ConfigError, Problem


def read_mapping(path: Path, *, kind: str) -> dict[str, Any]:
    """Read a YAML file whose top level must be a mapping. Raise ``ConfigError`` otherwise."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(
            path,
            [Problem("file not found", hint=f"the path resolved to {path.resolve()}")],
            kind=kind,
        ) from None
    except OSError as exc:
        raise ConfigError(
            path, [Problem(f"can't read the file: {exc.strerror}")], kind=kind
        ) from None

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"line {mark.line + 1}, column {mark.column + 1}" if mark else None
        detail = getattr(exc, "problem", None) or str(exc)
        raise ConfigError(
            path,
            [
                Problem(
                    f"YAML syntax error: {detail}",
                    where=where,
                    hint="unclosed brackets or braces, indentation, and the ':' after each key",
                )
            ],
            kind=kind,
        ) from None

    if data is None:
        raise ConfigError(path, [Problem("the file is empty")], kind=kind)
    if not isinstance(data, dict):
        raise ConfigError(
            path,
            [
                Problem(
                    f"the top level must be a set of 'key: value' entries, not a {_kind_of(data)}"
                )
            ],
            kind=kind,
        )
    return data


def _kind_of(value: object) -> str:
    if isinstance(value, list):
        return "list"
    if isinstance(value, str):
        return "single piece of text"
    return type(value).__name__
