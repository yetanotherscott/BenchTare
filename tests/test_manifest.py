"""The run manifest (files and their hashes, git state) and the run's environment."""

import hashlib
import platform
import shutil
import subprocess

import pytest

from benchtare.results.manifest import (
    TEST,
    detect_ci,
    environment,
    git_state,
    manifest_files,
    packages,
    shown_path,
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- files -------------------------------------------------------------------------------------


def test_files_are_hashed_with_paths_relative_to_rootdir(tmp_path):
    (tmp_path / "tests").mkdir()
    test = tmp_path / "tests" / "test_x.py"
    test.write_bytes(b"def test_x():\n    pass\n")
    (entry,) = manifest_files(tmp_path, [(test, TEST)])
    assert entry.path == "tests/test_x.py"  # "/" on every OS
    assert entry.role == "test"
    assert entry.sha256 == sha256(test.read_bytes())
    assert entry.size == len(test.read_bytes())


def test_changing_a_file_changes_its_hash(tmp_path):
    test = tmp_path / "test_x.py"
    test.write_text("def test_x():\n    assert 1\n")
    (before,) = manifest_files(tmp_path, [(test, TEST)])
    test.write_text("def test_x():\n    assert 2\n")
    (after,) = manifest_files(tmp_path, [(test, TEST)])
    assert before.sha256 != after.sha256


def test_a_file_outside_rootdir_keeps_its_absolute_path(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "shared.dbc"
    outside.write_text('VERSION ""\n')
    assert shown_path(outside, root) == outside.resolve().as_posix()


def test_a_file_listed_twice_is_kept_once_and_a_missing_file_is_left_out(tmp_path):
    file = tmp_path / "pyproject.toml"
    file.write_text("[tool.pytest.ini_options]\n")
    entries = manifest_files(
        tmp_path, [(file, "ini"), (file, "test"), (tmp_path / "deleted.py", "test")]
    )
    assert [(e.path, e.role) for e in entries] == [("pyproject.toml", "ini")]


# --- git -------------------------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git isn't installed")

    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "bench@example.invalid")
    git("config", "user.name", "Bench")
    (tmp_path / "test_x.py").write_text("x = 1\n")
    git("add", ".")
    git("commit", "-q", "-m", "first")
    return tmp_path


def test_the_git_commit_is_recorded(repo):
    commit, dirty = git_state(repo)
    assert commit is not None and len(commit) == 40
    assert dirty is False


def test_uncommitted_changes_are_noticed(repo):
    (repo / "test_x.py").write_text("x = 2\n")
    assert git_state(repo)[1] is True


def test_a_folder_that_is_not_a_repository_has_no_commit(tmp_path):
    assert git_state(tmp_path) == (None, None)


# --- the environment -------------------------------------------------------------------------------


def test_the_environment_describes_the_machine_and_python():
    env = environment(environ={})
    assert env["os"] == platform.system()
    assert env["python_version"] == platform.python_version()
    assert env["python_implementation"] == platform.python_implementation()
    assert env["cpu_count"] and env["encoding"] and env["timezone"]
    assert env["ci"] is None


def test_environment_variable_values_are_never_recorded():
    env = environment(environ={"GITHUB_ACTIONS": "true", "API_TOKEN": "s3cr3t-value"})
    assert env["ci"] == "github-actions"
    assert "s3cr3t-value" not in repr(env)


@pytest.mark.parametrize(
    ("environ", "ci"),
    [
        ({"GITLAB_CI": "true", "CI": "true"}, "gitlab-ci"),
        ({"JENKINS_URL": "https://ci.invalid"}, "jenkins"),
        ({"CI": "1"}, "ci"),
        ({"CI": ""}, None),
        ({}, None),
    ],
)
def test_the_ci_system_is_told_by_the_variables_it_sets(environ, ci):
    assert detect_ci(environ) == ci


def test_installed_packages_are_listed_with_versions():
    found = dict(packages())
    assert found["pytest"] == pytest.__version__
    assert "python-can" in found  # normalised names
