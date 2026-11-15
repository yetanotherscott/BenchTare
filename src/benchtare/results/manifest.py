"""The run manifest, and the machine and software environment a run happened in.

- **Files.** Every file that decided what a run tested, with its SHA-256: the interface
  description and its DBC files, the bench and harness files, the test files, the
  ``conftest.py`` files and the ini file. Paths are relative to pytest's rootdir with ``/``
  separators, so they mean the same on every OS.
- **Git.** The project's commit, and whether it had uncommitted changes.
- **Environment.** The machine's name, OS, processor, Python build, text encoding, timezone and
  CI system, and every installed Python package with its version.

Environment variables are never recorded: they often hold secrets. Only the names of the
variables CI systems set are checked, to tell which CI system a run was under.
"""

from __future__ import annotations

import hashlib
import locale
import os
import platform
import re
import subprocess
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from importlib.metadata import distributions
from pathlib import Path
from typing import Any

INTERFACE = "interface"
DBC = "dbc"
BENCH = "bench"
HARNESS = "harness"
TEST = "test"
CONFTEST = "conftest"
INI = "ini"

# The variable each CI system sets, and the name it is recorded under. Only their presence is
# checked, never their values.
CI_SYSTEMS = (
    ("GITHUB_ACTIONS", "github-actions"),
    ("GITLAB_CI", "gitlab-ci"),
    ("JENKINS_URL", "jenkins"),
    ("BUILDKITE", "buildkite"),
    ("CIRCLECI", "circleci"),
    ("TF_BUILD", "azure-pipelines"),
    ("BITBUCKET_BUILD_NUMBER", "bitbucket-pipelines"),
    ("TEAMCITY_VERSION", "teamcity"),
    ("TRAVIS", "travis"),
    ("CI", "ci"),  # set by many CI systems; the generic answer when no other is found
)

_GIT_TIMEOUT = 5.0  # seconds; a slow or hung git must not hold up the run


@dataclass(frozen=True)
class ManifestFile:
    path: str  # relative to rootdir with "/" separators, or absolute when outside it
    role: str
    sha256: str
    size: int


def manifest_files(rootdir: Path, files: Iterable[tuple[Path, str]]) -> list[ManifestFile]:
    """Hash each (path, role). A path listed twice is kept once, with its first role. A file
    that can't be read (deleted during the run, say) is left out."""
    seen: set[str] = set()
    result: list[ManifestFile] = []
    for path, role in files:
        shown = shown_path(path, rootdir)
        if shown in seen:
            continue
        try:
            digest, size = _hash(path)
        except OSError:
            continue
        seen.add(shown)
        result.append(ManifestFile(shown, role, digest, size))
    return result


def shown_path(path: Path, rootdir: Path) -> str:
    """``path`` relative to ``rootdir`` with ``/`` separators, or absolute when outside it."""
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(Path(rootdir).resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def git_state(directory: Path) -> tuple[str | None, bool | None]:
    """The commit checked out in ``directory`` and whether there are uncommitted changes, or
    (None, None) when it isn't a git repository or git isn't installed."""
    commit = _git(directory, "rev-parse", "HEAD")
    if commit is None:
        return None, None
    status = _git(directory, "status", "--porcelain", "--untracked-files=no")
    return commit, None if status is None else bool(status)


def environment(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The machine and system a run happens on. See ``run_environment`` in the schema."""
    environ = os.environ if environ is None else environ
    return {
        "host": platform.node() or None,
        "os": platform.system() or "unknown",
        "os_release": platform.release() or None,
        "os_version": platform.version() or None,
        "architecture": platform.machine() or None,
        "processor": platform.processor() or None,
        "cpu_count": os.cpu_count(),
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "python_build": ", ".join((*platform.python_build(), platform.python_compiler())),
        "encoding": locale.getpreferredencoding(False),
        "timezone": _timezone(),
        "ci": detect_ci(environ),
    }


def detect_ci(environ: Mapping[str, str]) -> str | None:
    """The CI system a run is under, from the names of the variables it sets; None if none."""
    for variable, name in CI_SYSTEMS:
        if environ.get(variable):
            return name
    return None


def packages() -> list[tuple[str, str]]:
    """Every installed Python package, by normalised name, with its version."""
    found: dict[str, str] = {}
    for dist in distributions():
        name = dist.metadata["Name"] if dist.metadata else None
        if not name:
            continue
        found.setdefault(_normalise(name), dist.version or "")
    return sorted(found.items())


def _hash(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(1 << 16), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _git(directory: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=directory,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def _timezone() -> str:
    local = time.localtime()
    name = time.tzname[1 if local.tm_isdst > 0 else 0]
    return f"{name} {time.strftime('%z', local)}".strip()


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()
