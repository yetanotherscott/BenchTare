"""BenchTare: a self-verifying HIL/SIL test framework for embedded systems.

What happened at the bench during a test goes into the test record (``benchtare.record``).
BenchTare's own log, for debugging BenchTare, uses the standard library's ``logging`` under the
``benchtare`` logger. It adds no handler of its own, not even a ``NullHandler``: with logging
left unconfigured, Python still prints BenchTare's warnings and errors (a bus that stopped
receiving, for example) to stderr.

PYTEST_DONT_REWRITE: BenchTare's own asserts are internal checks, so pytest needn't rewrite
them. The marker also stops a warning when ``benchtare selftest``, having imported this package,
starts pytest.
"""

from benchtare.errors import (
    BenchTareWarning,
    DutIdentityWarning,
    EquipmentWarning,
    FrameMismatchWarning,
)

__version__ = "0.0.1"

__all__ = [
    "BenchTareWarning",
    "DutIdentityWarning",
    "EquipmentWarning",
    "FrameMismatchWarning",
    "__version__",
]
