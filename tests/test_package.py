"""Smoke tests for the installed package."""

from importlib.metadata import version

import benchtare


def test_version_matches_installed_metadata() -> None:
    assert benchtare.__version__ == version("benchtare")
