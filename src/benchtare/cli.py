"""The ``benchtare`` command.

    benchtare init [FOLDER]
    benchtare selftest [pytest options]
    benchtare check-hygiene [pytest options]
    benchtare check-tests [pytest options]

``init`` writes a new project into FOLDER (by default the current folder): the quickstart, on
the simulated reference DUT. It never overwrites a file. Exit codes: 0 when the project was
written, 1 when it wasn't.

The other three run through pytest, so they find the interface description, bench file and harness file
exactly as a test run does: from ``--dut``, ``--bench`` and ``--harness``, or from the ini
file. Any other options go to pytest: test paths and ``-k`` choose tests, ``-c`` chooses the ini
file, and ``--log-cli-level=DEBUG`` shows BenchTare's own log.

``selftest`` runs BenchTare's self-test and lists every check. Exit codes: 0 when it passed, 1
when it failed, 2 when its results couldn't be stored, and 4 when it couldn't start
(BenchTare isn't configured, or a file is wrong).

``check-hygiene`` runs the hygiene check (``pytest --check-hygiene``): the tests, then each one
that passed again, checking that it repeats, leaves the DUT's ``clean_state:`` clean and starts
from a meaningful state. Exit codes: 0 when every test passed and is clean, 1 when a test failed
or isn't clean, 2 when the run stopped, 4 when it couldn't start, and 5 when no tests were
collected.

``check-tests`` runs the test check (``pytest --check-tests``): the tests, then each one that
passed again with what it reads corrupted, reporting any that can't fail. Exit codes: 0 when
every test passed and every one checked can fail, 1 when a test failed or can't fail, 2 when the
run stopped, 4 when it couldn't start, and 5 when no tests were collected.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from benchtare import __version__
from benchtare.scaffold import InitError, init

INIT_HELP = "write a new project, on the simulated reference DUT, to try BenchTare out"

SELFTEST_HELP = "check the bench and the harness, without running any test"
CHECK_TESTS_HELP = "prove each test can fail, by rerunning it with what it reads corrupted"
CHECK_HYGIENE_HELP = "prove each test stands on its own: it repeats, and leaves the DUT clean"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="benchtare",
        description="BenchTare: a self-verifying HIL/SIL test framework, built on pytest. "
        "Tests run with pytest itself.",
    )
    parser.add_argument("--version", action="version", version=f"benchtare {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)
    init_command = commands.add_parser(
        "init",
        help=INIT_HELP,
        description=f"Write the quickstart: {INIT_HELP}. It is a complete project, with an "
        "interface description, a bench file, a harness file, staging fixtures and one test. It "
        "never overwrites a file.",
        epilog="Exit codes: 0 written, 1 not written.",
    )
    init_command.add_argument(
        "folder",
        nargs="?",
        default=".",
        type=Path,
        help="where to write it, created if needed (default: the current folder)",
    )
    commands.add_parser(
        "selftest",
        help=SELFTEST_HELP,
        description=f"Run BenchTare's self-test: {SELFTEST_HELP}. It uses the same files and "
        "settings as a test run, and passes any other options to pytest (for example --dut, "
        "--bench and --harness, or -c to choose the ini file).",
        epilog="Exit codes: 0 passed, 1 failed, 2 results couldn't be stored, 4 couldn't start.",
    )
    commands.add_parser(
        "check-hygiene",
        help=CHECK_HYGIENE_HELP,
        description=f"Run the hygiene check: {CHECK_HYGIENE_HELP}. The tests run as usual "
        "first; each one that passed runs again straight after itself. Each test must pass "
        "again, leave every clean_state item in the interface description clean, and start "
        "from a meaningful state: no wait_until already satisfied before its write. It uses the "
        "same files and settings as a test run, and passes any other options to pytest.",
        epilog="Exit codes: 0 every test passed and is clean, 1 a test failed or isn't clean, "
        "2 stopped, 4 couldn't start, 5 no tests.",
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
    if args.command == "init":
        if rest:
            parser.error(f"init takes only a folder, not {' '.join(rest)}")
        return _init(args.folder)
    if args.command == "selftest":
        return int(pytest.main(["--selftest-only", "-p", "no:cacheprovider", *rest]))
    if args.command == "check-hygiene":
        return int(pytest.main(["--check-hygiene", "-p", "no:cacheprovider", *rest]))
    if args.command == "check-tests":
        return int(pytest.main(["--check-tests", "-p", "no:cacheprovider", *rest]))
    parser.error(f"unknown command {args.command}")  # argparse rejects these first
    return 2  # pragma: no cover


def _init(folder: Path) -> int:
    try:
        written = init(folder)
    except InitError as exc:
        print(f"benchtare init: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(
            f"benchtare init: couldn't write the project to {folder}: {exc}. Check the "
            "folder's path, and that it can be written to.",
            file=sys.stderr,
        )
        return 1
    here = folder.resolve() == Path.cwd().resolve()
    print(
        f"Wrote a BenchTare project to {folder}: {len(written)} files, on the simulated "
        "reference DUT. To run it:"
    )
    print()
    if not here:
        print(f"    cd {folder}")
    print("    pytest                     # the self-test, then the test")
    print("    benchtare check-hygiene    # the test stands on its own")
    print("    benchtare check-tests      # the test can fail at every check")
    print()
    print(
        f"{folder / 'README.md'} says what each file is, and what to break to see each check work."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
