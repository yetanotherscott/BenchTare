"""The version and the changelog agree, so no release goes out without its changelog entry."""

import re
from pathlib import Path

import benchtare

ROOT = Path(__file__).resolve().parents[1]

# The kinds of change Keep a Changelog names, and no others.
SECTIONS = {"Added", "Changed", "Deprecated", "Removed", "Fixed", "Security"}


def changelog_problems(text: str, version: str) -> list[str]:
    """What stops ``text``, a Keep a Changelog file, from covering a release of ``version``."""
    problems = []
    if not re.search(r"^## \[Unreleased\]$", text, re.MULTILINE):
        problems.append("no '## [Unreleased]' section")
    entry = rf"^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}$"
    if not re.search(entry, text, re.MULTILINE):
        problems.append(f"no '## [{version}] - YYYY-MM-DD' section")
    if not re.search(rf"^\[{re.escape(version)}\]: https://", text, re.MULTILINE):
        problems.append(f"no '[{version}]: ' link at the end")
    for heading in re.findall(r"^### (.+)$", text, re.MULTILINE):
        if heading not in SECTIONS:
            problems.append(f"'### {heading}' isn't one of Keep a Changelog's sections")
    return problems


def test_pyproject_has_the_package_version() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    found = re.search(r'^version = "(.+)"$', pyproject, re.MULTILINE)
    assert found, "pyproject.toml has no version line"
    assert found.group(1) == benchtare.__version__, (
        "pyproject.toml and src/benchtare/__init__.py give different versions: change both"
    )


def test_the_changelog_covers_this_version() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert changelog_problems(changelog, benchtare.__version__) == []


def test_a_changelog_missing_the_version_is_caught() -> None:
    changelog = "# Changelog\n\n## [0.0.1] - 2026-11-16\n\n### Improved\n\n- Things.\n"
    assert changelog_problems(changelog, "0.0.2") == [
        "no '## [Unreleased]' section",
        "no '## [0.0.2] - YYYY-MM-DD' section",
        "no '[0.0.2]: ' link at the end",
        "'### Improved' isn't one of Keep a Changelog's sections",
    ]
