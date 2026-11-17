"""BenchTare's plugin API: everything a plugin may import from BenchTare.

A plugin is a separately installed package that adds a backend (and, later, a transport). It
imports only from this module, so BenchTare's internals can change without breaking it.

A backend is a class registered in the ``benchtare.backends`` entry-point group, under the name
a bench file uses::

    [project.entry-points."benchtare.backends"]
    my_daq = "my_package.daq:MyDaq"

The class follows ``BackendType``: it declares ``name`` (the entry point's name), ``uuid``
(generated once by its author, for example with ``python -c "import uuid; print(uuid.uuid4())"``,
and never changed), ``api_version`` (the ``API_VERSION`` it was written for), ``kind``
(``"channels"`` or ``"bus"``) and ``simulated``. Its instances follow ``Backend``, or
``BusBackend`` for a bus. See "Backends and transports" in docs/model.md.

This API is provisional until the first release that includes it. After that, a change that
breaks plugins bumps ``API_VERSION``, and BenchTare refuses a plugin written for a version it
doesn't support.
"""

from benchtare.backends.base import (
    Backend,
    BackendType,
    BenchError,
    Binding,
    BusBackend,
    ChannelCaps,
    EquipmentIdentity,
    EquipmentKind,
    Receiver,
    WriteError,
)
from benchtare.dut.description import CanSignalRef
from benchtare.errors import Problem
from benchtare.record import (
    EQUIPMENT_FAILURE,
    FRAME_MISMATCH,
    FRAME_RECEIVED,
    FRAME_SENT,
    RecordEntry,
    RecordLevel,
)
from benchtare.signals.model import Signal

API_VERSION = 1
"""The plugin API version this BenchTare provides. A plugin declares the one it targets."""

__all__ = [
    "API_VERSION",
    "EQUIPMENT_FAILURE",
    "FRAME_MISMATCH",
    "FRAME_RECEIVED",
    "FRAME_SENT",
    "Backend",
    "BackendType",
    "BenchError",
    "Binding",
    "BusBackend",
    "CanSignalRef",
    "ChannelCaps",
    "EquipmentIdentity",
    "EquipmentKind",
    "Problem",
    "Receiver",
    "RecordEntry",
    "RecordLevel",
    "Signal",
    "WriteError",
]
