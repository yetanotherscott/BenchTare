"""The ``benchtare`` command.

    benchtare selftest [pytest options]
    benchtare check-tests [pytest options]

Both run through pytest, so they find the interface description, bench file and harness file
exactly as a test run does: from ``--dut``, ``--bench`` and ``--harness``, or from the ini
file. Any other options go to pytest: test paths and ``-k`` choose tests, ``-c`` chooses the ini
file, and ``--log-cli-level=DEBUG`` shows BenchTare's own log.

``selftest`` runs BenchTare's self-test and lists every check. Exit codes: 0 when it passed, 1
when it failed, 2 when its results couldn't be stored, and 4 when it couldn't start
(BenchTare isn't configured, or a file is wrong).

``check-tests`` runs the test check (``pytest --check-tests``): the tests, then each one that
passed again with what it reads corrupted, reporting any that can't fail. Exit codes: 0 when
every test passed and every one checked can fail, 1 when a test failed or can't fail, 2 when the
run stopped, 4 when it couldn't start, and 5 when no tests were collected.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

import pytest

from benchtare import __version__

SELFTEST_HELP = "check the bench and the harness, without running any test"
CHECK_TESTS_HELP = "prove each test can fail, by rerunning it with what it reads corrupted"


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
    commands.add_parser(
        "check-tests",
        help=CHECK_TESTS_HELP,
        description=f"Run the test check: {CHECK_TESTS_HELP}. The tests run as usual first; each "
        "one that passed runs again with its readings shifted to wrong values, then with the "
        "values from before its writes, and any test that still passes is reported as unable "
        "to fail. It uses the same files and settings as a test run, and passes any other "
        "options to pytest (for example test paths or -k to choose tests).",
        epilog="Exit codes: 0 every test passed and can fail, 1 a test failed or can't fail, "
        "2 stopped, 4 couldn't start, 5 no tests.",
    )
    args, rest = parser.parse_known_args(argv)
    if args.command == "selftest":
        return int(pytest.main(["--selftest-only", "-p", "no:cacheprovider", *rest]))
    if args.command == "check-tests":
        return int(pytest.main(["--check-tests", "-p", "no:cacheprovider", *rest]))
    parser.error(f"unknown command {args.command}")  # argparse rejects these first
    return 2  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
