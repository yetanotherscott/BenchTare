"""The ``benchtare`` command.

    benchtare selftest [pytest options]

``selftest`` runs BenchTare's self-test through pytest, so it finds the interface description,
bench file and harness file exactly as a test run does: from ``--dut``, ``--bench`` and
``--harness``, or from the ini file. It lists every check. Any other options go to pytest:
``-c`` chooses the ini file, and ``--log-cli-level=DEBUG`` shows BenchTare's own log.

Exit codes: 0 when the self-test passed, 1 when it failed, 2 when its results couldn't be
stored, and 4 when it couldn't start (BenchTare isn't configured, or a file is wrong).
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

import pytest

from benchtare import __version__

SELFTEST_HELP = "check the bench and the harness, without running any test"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="benchtare",
        description="BenchTare: a self-verifying HIL/SIL test framework, built on pytest. "
        "Tests run with pytest itself.",
    )
    parser.add_argument("--version", action="version", version=f"benchtare {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)
    commands.add_parser(
        "selftest",
        help=SELFTEST_HELP,
        description=f"Run BenchTare's self-test: {SELFTEST_HELP}. It uses the same files and "
        "settings as a test run, and passes any other options to pytest (for example --dut, "
        "--bench and --harness, or -c to choose the ini file).",
        epilog="Exit codes: 0 passed, 1 failed, 2 results couldn't be stored, 4 couldn't start.",
    )
    args, rest = parser.parse_known_args(argv)
    if args.command == "selftest":
        return int(pytest.main(["--selftest-only", "-p", "no:cacheprovider", *rest]))
    parser.error(f"unknown command {args.command}")  # argparse rejects these first
    return 2  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
