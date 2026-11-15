"""BenchTare: a self-verifying HIL/SIL test framework for embedded systems.

BenchTare logs with the standard library's ``logging``, under the ``benchtare`` logger. It adds
no handler of its own, not even a ``NullHandler``: with logging left unconfigured, Python still
prints BenchTare's warnings and errors (a bus that stopped receiving, for example) to stderr.
"""

__version__ = "0.0.1"
