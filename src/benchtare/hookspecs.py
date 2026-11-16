"""Hooks a team can implement in its ``conftest.py``. No import is needed: pytest finds a
function by its name.

See "Test hygiene" in docs/model.md.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from benchtare.bench.runtime import Bench


@pytest.hookspec
def pytest_benchtare_state(bench: Bench, config: pytest.Config) -> Mapping[str, Any] | None:
    """State no signal shows, for the hygiene check: return the current value of each state
    probe this ``conftest.py`` knows, by name, such as ``{"fault_log": read_fault_log()}``.

    Reach it however the DUT allows: SSH to an embedded Linux target, a serial console, UDS.
    BenchTare calls this before a test's setup and after its teardown, and compares the names
    the interface description's ``clean_state:`` lists with ``probe: true``. Several
    ``conftest.py`` files may each return some of the names.
    """
