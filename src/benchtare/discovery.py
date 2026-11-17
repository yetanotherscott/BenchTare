"""Finding backends: BenchTare's own and plugins alike, through entry points.

Every backend, built in or not, is registered in the ``benchtare.backends`` entry-point group
under the name a bench file uses. BenchTare's own ``sim`` and ``sim_can`` are registered in its
own ``pyproject.toml``, so they are found exactly as a plugin is.

Each backend is checked as it is found: it must load, its class's ``name`` must match its entry
point, and it must declare a valid ``uuid`` and a supported ``api_version``. One that fails is
kept, with what is wrong, and reported only if a bench file names it, so an unused broken plugin
never stops a run. See "Backends and transports" in docs/model.md.
"""

from __future__ import annotations

import difflib
import functools
import importlib.metadata
import logging
import uuid
from dataclasses import dataclass, replace
from typing import Any, Literal

from benchtare import __version__
from benchtare.api import API_VERSION
from benchtare.backends.base import BackendType
from benchtare.errors import BenchTareError

log = logging.getLogger(__name__)

BACKEND_GROUP = "benchtare.backends"
SUPPORTED_API_VERSIONS = frozenset({API_VERSION})
BUILT_IN_BACKENDS = ("sim", "sim_can")

# Backends that known packages provide, so a bench file naming one that isn't installed can be
# told what to install. Filled in as the example plugins are published.
KNOWN_PACKAGES: dict[str, str] = {}

NEW_UUID = 'python -c "import uuid; print(uuid.uuid4())"'
_KINDS = ("channels", "bus")

PluginKind = Literal["backend", "transport"]


class PluginError(BenchTareError):
    """A backend named in a bench file can't be used."""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        self.message = message
        self.hint = hint
        super().__init__(message)


@dataclass(frozen=True)
class Plugin:
    """A backend found through its entry point: where it came from, and whether it can be used.

    ``problem`` says what is wrong with one that can't be used (it completes "it ..."), and
    ``hint`` what to do about it. ``uuid`` is in its canonical form, or None when the backend
    declares no valid UUID.
    """

    kind: PluginKind
    name: str
    target: str
    distribution: str | None
    distribution_version: str | None
    cls: BackendType | None = None
    uuid: str | None = None
    api_version: int | None = None
    problem: str | None = None
    hint: str | None = None

    @property
    def source(self) -> str:
        """Where it came from, for messages: its package and version, or its entry point."""
        if self.distribution is None:
            return f"entry point {self.target}"
        version = f" {self.distribution_version}" if self.distribution_version else ""
        return f"package {self.distribution}{version}"

    def record(self) -> dict[str, Any]:
        """The plugin as a run's results record it."""
        return {
            "uuid": self.uuid,
            "name": self.name,
            "kind": self.kind,
            "api_version": self.api_version,
            "distribution": self.distribution,
            "distribution_version": self.distribution_version,
        }


@functools.cache
def installed_backends() -> tuple[Plugin, ...]:
    """Every backend registered in the ``benchtare.backends`` group, usable or not.

    Read once per process. Tests that install a backend call ``installed_backends.cache_clear()``.
    """
    entry_points = importlib.metadata.entry_points(group=BACKEND_GROUP)
    found = tuple(_load(entry_point) for entry_point in sorted(entry_points, key=_order))
    for plugin in found:
        if plugin.problem is None:
            log.debug("found backend %s (%s, UUID %s)", plugin.name, plugin.source, plugin.uuid)
        else:
            log.warning(
                "backend '%s' from %s can't be used: it %s",
                plugin.name,
                plugin.source,
                plugin.problem,
            )
    return found


def backend_names() -> list[str]:
    """The names of the installed backends, sorted, for hints."""
    return sorted({plugin.name for plugin in installed_backends()})


def find_backend(reference: str) -> Plugin:
    """The backend a bench file names, by name or by UUID. Raise ``PluginError`` if it can't be
    used: not installed, broken, or ambiguous."""
    plugins = installed_backends()
    wanted = _as_uuid(reference)
    if wanted is not None:
        matches = [plugin for plugin in plugins if plugin.uuid == wanted]
        if not matches:
            raise PluginError(
                f"no installed backend has the UUID {wanted}",
                hint=f"check the UUID, or install the package that provides it; {_listing()}",
            )
        if len(matches) > 1:
            raise _copied(matches[0], matches[1])
    else:
        matches = [plugin for plugin in plugins if plugin.name == reference]
        if not matches:
            raise PluginError(f"unknown backend '{reference}'", hint=_unknown_hint(reference))
        if len(matches) > 1:
            raise _clash(reference, matches)
    chosen = matches[0]
    if chosen.problem is not None:
        raise PluginError(
            f"backend '{chosen.name}' from {chosen.source} can't be used: it {chosen.problem}",
            hint=chosen.hint,
        )
    twin = next(
        (plugin for plugin in plugins if plugin is not chosen and plugin.uuid == chosen.uuid),
        None,
    )
    if twin is not None:
        raise _copied(chosen, twin)
    return chosen


def _order(entry_point: importlib.metadata.EntryPoint) -> tuple[str, str]:
    return (entry_point.name, entry_point.value)


def _load(entry_point: importlib.metadata.EntryPoint) -> Plugin:
    distribution = getattr(entry_point, "dist", None)
    found = Plugin(
        kind="backend",
        name=entry_point.name,
        target=entry_point.value,
        distribution=distribution.metadata["Name"] if distribution is not None else None,
        distribution_version=distribution.version if distribution is not None else None,
    )
    try:
        cls = entry_point.load()
    except Exception as exc:  # noqa: BLE001 - any failure to import a plugin is reported, not raised
        return _broken(
            found,
            f"couldn't be imported ({type(exc).__name__}: {exc})",
            f"check that {found.source} is installed properly, or uninstall it",
        )
    if not isinstance(cls, type):
        return _broken(
            found,
            f"names {type(cls).__name__} {entry_point.value}, not a class",
            "its entry point must name the backend's class; that is for its author to fix",
        )
    declared = getattr(cls, "name", None)
    if declared != entry_point.name:
        return _broken(
            found,
            f"is registered as '{entry_point.name}', but its class names itself {declared!r}",
            "the entry point's name and the class's 'name' must match; that is for its author "
            "to fix",
        )
    found = replace(found, cls=cls)
    raw_uuid = getattr(cls, "uuid", None)
    if not isinstance(raw_uuid, str):
        return _broken(
            found,
            "declares no UUID",
            f"its author must give its class a  uuid = '...'  generated once with  {NEW_UUID}",
        )
    canonical = _as_uuid(raw_uuid)
    if canonical is None:
        return _broken(
            found,
            f"declares the UUID {raw_uuid!r}, which isn't a UUID",
            f"its author must generate one with  {NEW_UUID}",
        )
    if canonical == str(uuid.UUID(int=0)):
        return _broken(
            found,
            "declares the nil UUID, a placeholder",
            f"its author must generate one with  {NEW_UUID}",
        )
    found = replace(found, uuid=canonical)
    version = getattr(cls, "api_version", None)
    if isinstance(version, bool) or not isinstance(version, int):
        return _broken(
            found,
            "doesn't declare which plugin API version it was written for",
            f"its author must give its class an  api_version = {API_VERSION}  (or the version it "
            "was written for)",
        )
    found = replace(found, api_version=version)
    if version not in SUPPORTED_API_VERSIONS:
        newest = max(SUPPORTED_API_VERSIONS)
        if version > newest:
            return _broken(
                found,
                f"was written for plugin API version {version}, and this BenchTare "
                f"({__version__}) supports up to version {newest}",
                "upgrade BenchTare",
            )
        return _broken(
            found,
            f"was written for plugin API version {version}, which this BenchTare "
            f"({__version__}) no longer supports",
            f"upgrade {found.distribution or 'the plugin'}",
        )
    kind = getattr(cls, "kind", None)
    if kind not in _KINDS:
        return _broken(
            found,
            f"declares the kind {kind!r}; it must be 'channels' or 'bus'",
            "that is for its author to fix",
        )
    if not isinstance(getattr(cls, "simulated", None), bool):
        return _broken(
            found,
            "doesn't say whether it is simulated",
            "its author must give its class a  simulated = True  or  False",
        )
    return found


def _broken(plugin: Plugin, problem: str, hint: str) -> Plugin:
    return replace(plugin, problem=problem, hint=hint)


def _as_uuid(text: str) -> str | None:
    """The canonical form of a UUID written in the usual 8-4-4-4-12 form, or None."""
    if len(text) != 36 or text.count("-") != 4:
        return None
    try:
        return str(uuid.UUID(text))
    except ValueError:
        return None


def _copied(first: Plugin, second: Plugin) -> PluginError:
    return PluginError(
        f"backend '{first.name}' from {first.source} and backend '{second.name}' from "
        f"{second.source} declare the same UUID, {first.uuid}: one was almost certainly copied "
        "from the other",
        hint=f"uninstall one of them; the author of the copy must generate a new UUID with  "
        f"{NEW_UUID}",
    )


def _clash(name: str, matches: list[Plugin]) -> PluginError:
    listed = "; ".join(
        f"{plugin.source} (UUID {plugin.uuid or 'not declared'})" for plugin in matches
    )
    example = next((plugin.uuid for plugin in matches if plugin.uuid), None)
    by_uuid = f", for example  backend: {example}" if example else ""
    return PluginError(
        f"backend '{name}' is provided by {len(matches)} installed packages: {listed}",
        hint=f"uninstall all but one of them, or name the one you mean by its UUID{by_uuid}",
    )


def _unknown_hint(name: str) -> str:
    if name in KNOWN_PACKAGES:
        package = KNOWN_PACKAGES[name]
        return f"it comes from the package {package}: install it with  pip install {package}"
    names = backend_names()
    if not set(BUILT_IN_BACKENDS) <= set(names):
        return (
            "BenchTare's own backends weren't found either, so its installation is incomplete: "
            "reinstall BenchTare (in a source checkout,  pip install -e . )"
        )
    close = difflib.get_close_matches(name, names, n=1)
    return f"did you mean '{close[0]}'?" if close else _listing()


def _listing() -> str:
    return f"installed backends: {', '.join(backend_names())}"
