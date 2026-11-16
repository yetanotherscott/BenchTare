"""``benchtare init`` and the quickstart project it writes.

The quickstart is a new user's first sight of BenchTare, so these tests run it as a user would:
``benchtare init``, then ``pytest`` and both checks, each as a separate process. They also make
the three breaks its README suggests, and check that each is caught as the README says.
"""

import sys
from pathlib import Path

import pytest

from benchtare import reference, scaffold
from benchtare.dut.description import load_interface_description
from benchtare.hygiene import INPUT, OUTPUT
from benchtare.scaffold import InitError, init, quickstart_files

PROJECT_FILES = {
    "README.md",
    "reference.dut.yaml",
    "reference.dbc",
    "bench-sim.yaml",
    "harness-can.yaml",
    "conftest.py",
    "test_reference.py",
    "pytest.ini",
}


def benchtare(pytester, *args):
    return pytester.run(sys.executable, "-m", "benchtare", *args)


def run_pytest(pytester):
    return pytester.run(sys.executable, "-m", "pytest", "-p", "no:cacheprovider")


# --- the quickstart's files -------------------------------------------------------------------


def test_the_quickstart_has_every_file_a_project_needs():
    assert set(quickstart_files()) == PROJECT_FILES


@pytest.mark.parametrize("name", [reference.DBC, reference.BENCH, reference.HARNESS_CAN])
def test_the_quickstart_uses_the_references_own_files(name):
    assert quickstart_files()[name] == reference.path(name).read_bytes()


def test_the_quickstart_interface_description_is_the_references_plus_clean_state(tmp_path):
    ours = quickstart_files()[reference.DUT]
    assert ours.startswith(reference.path(reference.DUT).read_bytes())
    (tmp_path / reference.DUT).write_bytes(ours)
    (tmp_path / reference.DBC).write_bytes(quickstart_files()[reference.DBC])
    clean_state = load_interface_description(tmp_path / reference.DUT).clean_state
    assert {name: (item.kind, item.clean) for name, item in clean_state.items()} == {
        "Mode": (OUTPUT, "standby"),
        "Setpoint": (INPUT, 0.0),
        "Measured": (OUTPUT, 0.0),
    }


def test_only_the_quickstarts_files_are_copied_not_what_a_run_left_there(tmp_path, monkeypatch):
    folder = tmp_path / "quickstart"
    folder.mkdir()
    (folder / "conftest.py").write_bytes(b"# staging\n")  # bytes: no \r\n on Windows
    for left in ("__pycache__", ".benchtare", ".pytest_cache"):
        (folder / left).mkdir()
        (folder / left / "x").write_text("left by a run\n")
    (folder / ".DS_Store").write_text("left by an OS\n")
    monkeypatch.setattr(scaffold, "files", lambda package: tmp_path)
    assert quickstart_files() == {"conftest.py": b"# staging\n"}


def test_the_quickstart_tests_need_no_benchtare_imports():
    for name in ("conftest.py", "test_reference.py"):
        assert b"benchtare" not in quickstart_files()[name].lower(), name


# --- init ---------------------------------------------------------------------------------------


def test_init_writes_the_quickstart_into_a_new_folder(tmp_path):
    target = tmp_path / "new" / "demo"
    written = init(target)
    assert {path.name for path in written} == PROJECT_FILES
    assert all(path.parent == target for path in written)
    for name, data in quickstart_files().items():
        assert (target / name).read_bytes() == data


def test_init_leaves_other_files_in_the_folder_alone(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "notes.txt").write_text("mine\n")
    init(tmp_path)
    assert (tmp_path / "notes.txt").read_text() == "mine\n"
    assert {path.name for path in tmp_path.iterdir()} == PROJECT_FILES | {".git", "notes.txt"}


def test_init_never_overwrites_a_file_and_writes_nothing_instead(tmp_path):
    (tmp_path / "conftest.py").write_text("# mine\n")
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    with pytest.raises(InitError, match=r"already has conftest\.py, pytest\.ini\. .*overwrites"):
        init(tmp_path)
    assert (tmp_path / "conftest.py").read_text() == "# mine\n"
    assert (tmp_path / "pytest.ini").read_text() == "[pytest]\n"
    assert {path.name for path in tmp_path.iterdir()} == {"conftest.py", "pytest.ini"}


def test_init_refuses_a_file_given_as_the_folder(tmp_path):
    (tmp_path / "demo").write_text("a file\n")
    with pytest.raises(InitError, match="is a file, not a folder"):
        init(tmp_path / "demo")
    assert (tmp_path / "demo").read_text() == "a file\n"


def test_init_never_overwrites_a_file_that_appears_while_it_writes(tmp_path, monkeypatch):
    target = tmp_path / "demo"
    mkdir = Path.mkdir

    def mkdir_then_someone_writes(self, *args, **kwargs):
        mkdir(self, *args, **kwargs)
        if self == target:
            (target / "pytest.ini").write_text("theirs\n")

    monkeypatch.setattr(Path, "mkdir", mkdir_then_someone_writes)
    with pytest.raises(InitError, match=r"pytest\.ini appeared .*Already written: README\.md"):
        init(target)
    assert (target / "pytest.ini").read_text() == "theirs\n"


# --- the command ------------------------------------------------------------------------------


def test_init_command_writes_the_project_and_says_how_to_run_it(pytester):
    result = benchtare(pytester, "init", "demo")
    assert result.ret == 0
    assert {path.name for path in (pytester.path / "demo").iterdir()} == PROJECT_FILES
    result.stdout.fnmatch_lines(
        [
            "Wrote a BenchTare project to demo: 8 files, on the simulated reference DUT.*",
            "    cd demo",
            "    pytest *",
            "    benchtare check-hygiene *",
            "    benchtare check-tests *",
            "*README.md says what each file is*",
        ]
    )


def test_init_command_writes_into_the_current_folder_by_default(pytester):
    result = benchtare(pytester, "init")
    assert result.ret == 0
    assert {path.name for path in pytester.path.iterdir()} >= PROJECT_FILES
    assert "cd " not in result.stdout.str()  # already there


def test_init_command_refuses_to_overwrite(pytester):
    (pytester.path / "demo").mkdir()
    (pytester.path / "demo" / "conftest.py").write_text("# mine\n")
    result = benchtare(pytester, "init", "demo")
    assert result.ret == 1
    result.stderr.fnmatch_lines(["benchtare init: demo already has conftest.py. *overwrites*"])
    assert result.stdout.str() == ""
    assert (pytester.path / "demo" / "conftest.py").read_text() == "# mine\n"


def test_init_command_says_what_failed_when_it_cant_write(pytester):
    (pytester.path / "a-file").write_text("not a folder\n")
    result = benchtare(pytester, "init", str(Path("a-file") / "demo"))
    assert result.ret == 1
    result.stderr.fnmatch_lines(["benchtare init: couldn't write the project to *demo: *"])


def test_init_command_takes_only_a_folder(pytester):
    result = benchtare(pytester, "init", "demo", "--dut", "x.yaml")
    assert result.ret == 2
    assert "init takes only a folder" in result.stderr.str()
    assert not (pytester.path / "demo").exists()


# --- the project it writes, run as a user would -----------------------------------------------


@pytest.fixture
def project(pytester):
    assert benchtare(pytester, "init").ret == 0
    return pytester


def edit(path, old, new):
    text = path.read_text()
    assert old in text, f"{path.name} no longer has {old!r}"
    path.write_text(text.replace(old, new))


def test_the_quickstart_passes_its_test_and_both_checks(project):
    tests = run_pytest(project)
    assert tests.ret == 0
    tests.stdout.fnmatch_lines(
        ["*benchtare bench check passed*", "*benchtare harness check passed*", "*1 passed*"]
    )

    hygiene = benchtare(project, "check-hygiene")
    assert hygiene.ret == 0
    hygiene.stdout.fnmatch_lines(["Hygiene check: 1 clean"])

    check = benchtare(project, "check-tests")
    assert check.ret == 0
    check.stdout.fnmatch_lines(["Test check: 1 able to fail"])

    assert (project.path / ".benchtare" / "results.sqlite3").is_file()


README = quickstart_files()["README.md"].decode()
WAIT = "bench.wait_until(signals.Measured, pytest.approx(42.0, abs=0.5), timeout=1.0)"
READ = "bench.read(signals.Measured, timeout=1.0)"


def test_the_readme_break_a_wrong_value_fails_the_test(project):
    assert "Change `42.0` in the test's `wait_until` to `43.0`: the test fails" in README
    edit(project.path / "test_reference.py", "approx(42.0", "approx(43.0")
    result = run_pytest(project)
    assert result.ret == 1
    # pytest.approx shows the tolerance as 0.5, or as 5.0e-01 before pytest 8
    result.stdout.fnmatch_lines(["*Measured did not reach 43.0 ± *the last 42.0 degC*"])


def test_the_readme_break_a_read_that_checks_nothing_is_unable_to_fail(project):
    assert f"Replace the `wait_until` with `{READ}`" in README
    edit(project.path / "test_reference.py", WAIT, READ)
    assert run_pytest(project).ret == 0
    result = benchtare(project, "check-tests")
    assert result.ret == 1
    result.stdout.fnmatch_lines(["Test check: 1 unable to fail"])


def test_the_readme_break_no_teardown_leaves_the_dut_unclean(project):
    assert "Remove the lines after `yield` in `conftest.py`" in README
    conftest = project.path / "conftest.py"
    conftest.write_text(conftest.read_text().split("    yield\n")[0] + "    yield\n")
    assert run_pytest(project).ret == 0
    result = benchtare(project, "check-hygiene")
    assert result.ret == 1
    result.stdout.fnmatch_lines(["*Mode was still 'run'*", "Hygiene check: 1 not clean"])
