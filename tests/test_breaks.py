"""Recorded breaks: the proof that BenchTare's own tests can fail.

Each break in ``tests/breaks/*.yaml`` names a behaviour, an exact change to the source that
breaks it, and the tests that must then fail. ``pytest -m breaks`` applies each break to a copy
of the repository and runs its tests there: every named test must fail, and must pass in an
unbroken copy. This is BenchTare's test check, applied to its own suite. See docs/testing.md.

The breaks are slow, so a usual ``pytest`` leaves them out. It does check, quickly, that every
break is well formed and still applies to the source as it is now, so a change to the code that
leaves a break behind fails at once.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
BREAKS_DIR = ROOT / "tests" / "breaks"

# What a copy needs to run the tests: the tests read the changelog, docs and project file.
COPIED = ("src", "tests", "docs", "pyproject.toml", "README.md", "CHANGELOG.md", "LICENSE")
FIELDS = ("id", "guards", "file", "find", "replace", "caught_by")
RUN_TIMEOUT = 600  # seconds for one run of a break's tests


@dataclass(frozen=True)
class Break:
    id: str
    guards: str  # the behaviour the break takes away
    file: str  # the source file it changes, relative to the repository, with / separators
    find: str  # text that occurs exactly once in that file
    replace: str  # what it becomes
    caught_by: tuple[str, ...]  # pytest node ids: each must fail with the break applied
    where: str  # "breaks/selftest.yaml, entry 3", for messages

    def path(self, root: Path) -> Path:
        return root.joinpath(*self.file.split("/"))


def load_breaks(directory: Path = BREAKS_DIR) -> tuple[list[Break], list[str]]:
    """Every well-formed break, and what is wrong with the rest."""
    breaks: list[Break] = []
    problems: list[str] = []
    seen: dict[str, str] = {}
    for path in sorted(directory.glob("*.yaml")):
        name = f"{directory.name}/{path.name}"
        try:
            entries = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            problems.append(f"{name}: not valid YAML: {exc}")
            continue
        if not isinstance(entries, list):
            problems.append(f"{name}: should be a list of breaks")
            continue
        for number, entry in enumerate(entries, start=1):
            where = f"{name}, entry {number}"
            wrong = _entry_problems(entry)
            if not wrong and entry["id"] in seen:
                wrong = [f"id '{entry['id']}' is already used by {seen[entry['id']]}"]
            if wrong:
                problems.extend(f"{where}: {problem}" for problem in wrong)
                continue
            seen[entry["id"]] = where
            breaks.append(
                Break(
                    entry["id"],
                    entry["guards"],
                    entry["file"],
                    entry["find"],
                    entry["replace"],
                    tuple(entry["caught_by"]),
                    f"{where} ({entry['id']})",
                )
            )
    return breaks, problems


def _entry_problems(entry: object) -> list[str]:
    if not isinstance(entry, dict):
        return ["should be a mapping with " + ", ".join(FIELDS)]
    problems = [f"missing '{field}'" for field in FIELDS if field not in entry]
    problems += [f"unknown field '{field}'" for field in entry if field not in FIELDS]
    if problems:
        return problems
    for field in ("id", "guards", "file", "find"):
        if not isinstance(entry[field], str) or not entry[field].strip():
            problems.append(f"'{field}' should be non-empty text")
    if not isinstance(entry["replace"], str):
        problems.append("'replace' should be text (it may be empty)")
    elif entry["replace"] == entry["find"]:
        problems.append("'replace' is the same as 'find', so nothing would break")
    if isinstance(entry["id"], str) and not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", entry["id"]):
        problems.append(f"id '{entry['id']}' should be lower-case words joined by '-'")
    if isinstance(entry["file"], str) and not entry["file"].startswith("src/"):
        problems.append(f"'file' should be a source file under src/, not '{entry['file']}'")
    tests = entry["caught_by"]
    if not isinstance(tests, list) or not tests:
        problems.append("'caught_by' should list at least one test")
    else:
        for test in tests:
            if not isinstance(test, str) or "::" not in test:
                problems.append(f"'caught_by' should name single tests (file::test), not {test!r}")
            elif test.startswith("tests/test_breaks.py"):
                problems.append(f"'caught_by' can't name the breaks' own tests: {test}")
    return problems


def application_problem(brk: Break, root: Path) -> str | None:
    """Why ``brk`` can't be applied to the repository at ``root``; None if it can."""
    path = brk.path(root)
    if not path.is_file():
        return f"{brk.where}: {brk.file} doesn't exist"
    found = path.read_text(encoding="utf-8").count(brk.find)
    if found != 1:
        return (
            f"{brk.where}: its 'find' text occurs {found} times in {brk.file}, not once. If the "
            "code changed, change the break to match it, so it still takes away: " + brk.guards
        )
    return None


BREAKS, PROBLEMS = load_breaks()


# --- a copy of the repository, and its tests run there ---------------------------------------------


def copy_repository(root: Path) -> Path:
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", "*.egg-info")
    root.mkdir(parents=True)
    for name in COPIED:
        source = ROOT / name
        if source.is_dir():
            shutil.copytree(source, root / name, ignore=ignore)
        else:
            shutil.copy2(source, root / name)
    return root


def environment(root: Path) -> dict[str, str]:
    """The environment for running Python on the copy: its src/ comes first on the path."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(root / "src"), env.get("PYTHONPATH")]))
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


@dataclass(frozen=True)
class Run:
    exit_code: int
    outcomes: dict[tuple[str, str], str]  # (classname, name) -> passed, failed, error, skipped
    output: str

    def outcome(self, test: str) -> str:
        """The outcome of a named test, or of all its parameter sets if it names none: 'failed'
        only if each one failed, and 'not run' if pytest didn't report it."""
        classname, name = junit_address(test)
        found = [
            outcome
            for (case_class, case_name), outcome in self.outcomes.items()
            if case_class == classname
            and (case_name == name or ("[" not in name and case_name.startswith(name + "[")))
        ]
        if not found:
            return "not run"
        for worst in ("passed", "skipped", "error"):
            if worst in found:
                return worst
        return "failed"

    def tail(self, lines: int = 40) -> str:
        return "\n".join(self.output.strip().splitlines()[-lines:])


def junit_address(test: str) -> tuple[str, str]:
    """A node id as pytest's JUnit XML names it: ``tests/test_x.py::TestY::test_z[a]`` is
    ``("tests.test_x.TestY", "test_z[a]")``."""
    path, bracket, params = test.partition("[")
    names = path.split("::")
    names[0] = re.sub(r"\.py$", "", names[0].replace("/", "."))
    names[-1] += bracket + params
    return ".".join(names[:-1]), names[-1]


def run_tests(root: Path, tests: list[str], junit: Path) -> Run:
    command = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q"]
    command += ["-o", "junit_family=xunit2", f"--junitxml={junit}", *tests]
    try:
        process = subprocess.run(
            command,
            check=False,
            cwd=root,
            env=environment(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=RUN_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        return Run(-1, {}, f"{output}\nstill running after {RUN_TIMEOUT} s, so it was stopped")
    outcomes: dict[tuple[str, str], str] = {}
    if junit.exists():
        for case in ET.parse(junit).iter("testcase"):
            kinds = {child.tag for child in case}
            outcome = (
                "failed"
                if "failure" in kinds
                else "error"
                if "error" in kinds
                else "skipped"
                if "skipped" in kinds
                else "passed"
            )
            outcomes[(case.get("classname", ""), case.get("name", ""))] = outcome
    return Run(process.returncode, outcomes, process.stdout + process.stderr)


# --- checked on every run: each break is well formed and still applies --------------------------


def test_every_break_is_well_formed():
    assert BREAKS, "no breaks found in tests/breaks/"
    assert not PROBLEMS, "Fix these in tests/breaks/:\n" + "\n".join(PROBLEMS)


@pytest.mark.parametrize("brk", BREAKS, ids=[brk.id for brk in BREAKS])
def test_every_break_still_applies(brk):
    assert application_problem(brk, ROOT) is None, application_problem(brk, ROOT)


def test_every_named_test_exists():
    tests = sorted({test for brk in BREAKS for test in brk.caught_by})
    process = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--collect-only", "-q", *tests],
        check=False,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert process.returncode == 0, (
        "A break names a test that doesn't exist; see 'caught_by' in tests/breaks/:\n"
        + "\n".join((process.stdout + process.stderr).strip().splitlines()[-10:])
    )


# --- the runner's own judgement -----------------------------------------------------------------

GOOD = {
    "id": "a-break",
    "guards": "something",
    "file": "src/benchtare/x.py",
    "find": "a",
    "replace": "b",
    "caught_by": ["tests/test_x.py::test_y"],
}


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"guards": None}, "missing 'guards'"),
        ({"why": "x"}, "unknown field 'why'"),
        ({"replace": "a"}, "'replace' is the same as 'find', so nothing would break"),
        ({"id": "A Break"}, "id 'A Break' should be lower-case words joined by '-'"),
        ({"file": "tests/test_x.py"}, "'file' should be a source file under src/"),
        ({"find": "  "}, "'find' should be non-empty text"),
        ({"caught_by": []}, "'caught_by' should list at least one test"),
        ({"caught_by": ["tests/test_x.py"]}, "'caught_by' should name single tests"),
        ({"caught_by": ["tests/test_breaks.py::test_x"]}, "can't name the breaks' own tests"),
    ],
)
def test_a_wrong_entry_is_reported(change, problem):
    entry = {**GOOD, **change}
    entry = {key: value for key, value in entry.items() if value is not None}
    assert any(problem in found for found in _entry_problems(entry)), _entry_problems(entry)
    assert _entry_problems(GOOD) == []


def test_breaks_are_read_from_every_file_and_ids_must_be_unique(tmp_path):
    entry = yaml.safe_dump([GOOD])
    (tmp_path / "a.yaml").write_text(entry)
    (tmp_path / "b.yaml").write_text(entry)
    (tmp_path / "c.yaml").write_text("{not: a list}")
    breaks, problems = load_breaks(tmp_path)
    assert [brk.where for brk in breaks] == [f"{tmp_path.name}/a.yaml, entry 1 (a-break)"]
    assert problems == [
        (
            f"{tmp_path.name}/b.yaml, entry 1: id 'a-break' is already used by "
            f"{tmp_path.name}/a.yaml, entry 1"
        ),
        f"{tmp_path.name}/c.yaml: should be a list of breaks",
    ]


@pytest.mark.parametrize(("source", "found"), [(None, None), ("a", 1), ("", 0), ("a a", 2)])
def test_a_break_applies_only_where_its_text_occurs_once(tmp_path, source, found):
    brk = Break(**{**GOOD, "caught_by": ()}, where="here")
    if source is not None:
        (tmp_path / "src" / "benchtare").mkdir(parents=True)
        (tmp_path / "src" / "benchtare" / "x.py").write_text(source)
    problem = application_problem(brk, tmp_path)
    if source is None:
        assert problem == "here: src/benchtare/x.py doesn't exist"
    elif found == 1:
        assert problem is None
    else:
        assert problem.startswith(f"here: its 'find' text occurs {found} times in src/")


@pytest.mark.parametrize(
    ("test", "address"),
    [
        ("tests/test_x.py::test_y", ("tests.test_x", "test_y")),
        ("tests/test_x.py::test_y[a-b]", ("tests.test_x", "test_y[a-b]")),
        ("tests/test_x.py::TestZ::test_y[1.5]", ("tests.test_x.TestZ", "test_y[1.5]")),
    ],
)
def test_a_node_id_is_found_by_its_junit_address(test, address):
    assert junit_address(test) == address


JUDGED = """
import pytest

def test_fails():
    assert 1 == 2

@pytest.fixture
def broken():
    raise RuntimeError("the fixture broke")

def test_errors(broken):
    pass

def test_passes():
    pass

@pytest.mark.parametrize("n", [1, 2])
def test_all_fail(n):
    assert n == 0

@pytest.mark.parametrize("n", [1, 2])
def test_one_fails(n):
    assert n == 1
"""


def test_only_a_failure_counts_as_caught(tmp_path):
    # A real run, read back from its JUnit XML: an error in a fixture is not a failure, and a
    # test named without its parameters fails only if every parameter set did.
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_judged.py").write_text(JUDGED)
    run = run_tests(tmp_path, ["tests/test_judged.py"], tmp_path / "junit.xml")
    assert run.exit_code == 1
    assert {
        name: run.outcome(f"tests/test_judged.py::{name}")
        for name in (
            "test_fails",
            "test_errors",
            "test_passes",
            "test_all_fail",
            "test_one_fails",
            "test_one_fails[2]",
            "test_missing",
        )
    } == {
        "test_fails": "failed",
        "test_errors": "error",
        "test_passes": "passed",
        "test_all_fail": "failed",
        "test_one_fails": "passed",
        "test_one_fails[2]": "failed",
        "test_missing": "not run",
    }


# --- pytest -m breaks: each break is caught -----------------------------------------------------


@pytest.fixture(scope="session")
def unbroken(tmp_path_factory):
    """Every named test passes in an unbroken copy, so a failure with a break applied is the
    break's doing. Also proves the copy, not the installed package, is what the tests run."""
    root = copy_repository(tmp_path_factory.mktemp("unbroken") / "repo")
    where = subprocess.run(
        [sys.executable, "-c", "import benchtare; print(benchtare.__file__)"],
        check=False,
        cwd=root,
        env=environment(root),
        capture_output=True,
        text=True,
    )
    imported = Path(where.stdout.strip())
    assert imported.is_relative_to(root / "src"), (
        f"the copy's tests would run the package at {imported}, not the copy in {root / 'src'}"
    )
    tests = sorted({test for brk in BREAKS for test in brk.caught_by})
    run = run_tests(root, tests, root / "junit.xml")
    not_passed = {test: run.outcome(test) for test in tests if run.outcome(test) != "passed"}
    assert run.exit_code == 0 and not not_passed, (
        "These named tests don't pass in an unbroken copy, so the breaks can't be judged:\n"
        + "\n".join(f"  {test}: {outcome}" for test, outcome in not_passed.items())
        + "\n\n"
        + run.tail()
    )
    return root


@pytest.mark.breaks
@pytest.mark.parametrize("brk", BREAKS, ids=[brk.id for brk in BREAKS])
def test_the_break_is_caught(brk, unbroken, tmp_path):
    root = copy_repository(tmp_path / "repo")
    assert application_problem(brk, root) is None, application_problem(brk, root)
    path = brk.path(root)
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace(brk.find, brk.replace), encoding="utf-8")
    assert path.read_text(encoding="utf-8") != text

    run = run_tests(root, list(brk.caught_by), tmp_path / "junit.xml")

    # Only a test failure counts. An error (in a fixture, or collecting the tests) shows the
    # break stopped the test from running, not that the test checks what was taken away.
    missed = {test: run.outcome(test) for test in brk.caught_by if run.outcome(test) != "failed"}
    assert not missed, (
        f"{brk.where}: not caught. With this behaviour taken away:\n  {brk.guards}\n"
        + "\n".join(
            f"  {test} {outcome}, but should have failed" for test, outcome in missed.items()
        )
        + "\n\n"
        + run.tail()
    )
