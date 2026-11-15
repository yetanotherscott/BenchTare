"""BenchTare: a self-verifying HIL/SIL test framework for embedded systems.

What happened at the bench during a test goes into the test record (``benchtare.record``).
BenchTare's own log, for debugging BenchTare, uses the standard library's ``logging`` under the
``benchtare`` logger. It adds no handler of its own, not even a ``NullHandler``: with logging
left unconfigured, Python still prints BenchTare's warnings and errors (a bus that stopped
receiving, for example) to stderr.
"""

from benchtare.errors import BenchTareWarning, EquipmentWarning, FrameMismatchWarning

__version__ = "0.0.1"

__all__ = ["BenchTareWarning", "EquipmentWarning", "FrameMismatchWarning", "__version__"]
