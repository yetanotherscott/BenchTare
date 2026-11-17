"""Finding backends through entry points: BenchTare's own and plugins alike, by name or by UUID,
and every way one can be wrong. The plugin API module is checked here too."""

import importlib
import logging
import shutil
import sqlite3
import sys
import textwrap

import pytest

from benchtare import __version__, api, discovery, reference
from benchtare.backends.sim import SimIO
from benchtare.backends.sim_can import SimCan
from benchtare.bench.config import load_bench_file
from benchtare.discovery import installed_backends
from benchtare.errors import ConfigError

LAB_UUID = "6a0f3c1e-8d2b-4e57-9b1a-3c4d5e6f7a80"
OTHER_UUID = "0d9e8f7a-6b5c-4d3e-8f2a-1b0c9d8e7f60"

# A plugin backend for these tests: the simulated I/O under another name and UUID.
LAB_IO = """
from benchtare.backends.sim import SimIO


class LabIO(SimIO):
    name = "lab_io"
    uuid = "{uuid}"
    api_version = 1
"""

# A class that declares itself, and nothing more: enough to be found, not to be used. It
# inherits nothing, so taking a declaration out leaves it missing.
DECLARED = """
class LabIO:
    name = "lab_io"
    uuid = "{uuid}"
    api_version = 1
    kind = "channels"
    simulated = True
"""


@pytest.fixture
def install(tmp_path):
    """Install a package of backends: a module and its dist-info on sys.path, as pip would.

    ``install(distribution, source, {entry point name: class name}, version="1.2.0")``.
    """
    site = tmp_path / "site"
    site.mkdir()
    sys.path.insert(0, str(site))
    modules = []

    def install(distribution, source, entry_points, *, version="1.2.0"):
        module = distribution.replace("-", "_")
        (site / f"{module}.py").write_text(textwrap.dedent(source))
        info = site / f"{module}-{version}.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {distribution}\nVersion: {version}\n"
        )
        lines = "".join(f"{name} = {module}:{cls}\n" for name, cls in entry_points.items())
        (info / "entry_points.txt").write_text(f"[benchtare.backends]\n{lines}")
        modules.append(module)
        importlib.invalidate_caches()
        installed_backends.cache_clear()

    yield install
    sys.path.remove(str(site))
    for module in modules:
        sys.modules.pop(module, None)
    importlib.invalidate_caches()
    installed_backends.cache_clear()


def bench_file(tmp_path, backend):
    path = tmp_path / "bench.yaml"
    path.write_text(
        f"bench: b\nequipment:\n  io:\n    backend: {backend}\n    channels: {{in1: {{read: true}}}}\n"
    )
    return path


def problems(tmp_path, backend):
    with pytest.raises(ConfigError) as info:
        load_bench_file(bench_file(tmp_path, backend))
    return str(info.value)


# --- the plugin API ---------------------------------------------------------------------------------


def test_the_api_module_offers_what_a_backend_needs():
    assert api.API_VERSION == 1
    for name in api.__all__:
        assert hasattr(api, name), name
    assert {"Backend", "BackendType", "BusBackend", "Binding", "ChannelCaps"} <= set(api.__all__)


# --- BenchTare's own backends ------------------------------------------------------------------------


def test_benchtares_own_backends_are_found_through_their_entry_points():
    found = {plugin.name: plugin for plugin in installed_backends()}
    assert found["sim"].cls is SimIO
    assert found["sim_can"].cls is SimCan
    for name in discovery.BUILT_IN_BACKENDS:
        plugin = found[name]
        assert plugin.problem is None
        assert plugin.distribution == "benchtare"
        assert plugin.api_version == api.API_VERSION
    assert found["sim"].uuid != found["sim_can"].uuid


def test_a_backend_can_be_named_by_its_uuid(tmp_path):
    bench = load_bench_file(bench_file(tmp_path, SimIO.uuid.upper()))
    assert bench.equipment["io"].backend == "sim"  # results and messages keep the name


def test_a_uuid_no_backend_has_is_reported(tmp_path):
    message = problems(tmp_path, OTHER_UUID)
    assert f"no installed backend has the UUID {OTHER_UUID}" in message
    assert "installed backends: sim, sim_can" in message


# --- plugins ------------------------------------------------------------------------------------------


def test_a_plugin_backend_is_found_by_name_with_the_package_it_came_from(tmp_path, install):
    install("lab-io-pkg", LAB_IO.format(uuid=LAB_UUID), {"lab_io": "LabIO"})
    item = load_bench_file(bench_file(tmp_path, "lab_io")).equipment["io"]
    assert item.backend == "lab_io"
    assert item.plugin.record() == {
        "uuid": LAB_UUID,
        "name": "lab_io",
        "kind": "backend",
        "api_version": 1,
        "distribution": "lab-io-pkg",
        "distribution_version": "1.2.0",
    }


def test_a_run_on_a_plugin_backend_records_the_plugin(pytester, install):
    install("lab-io-pkg", LAB_IO.format(uuid=LAB_UUID), {"lab_io": "LabIO"})
    for name in (reference.DUT, reference.DBC, reference.HARNESS_IO):
        shutil.copy(reference.path(name), pytester.path / name)
    bench = reference.path(reference.BENCH).read_text()
    assert bench.count("backend: sim\n") == 1
    (pytester.path / reference.BENCH).write_text(
        bench.replace("backend: sim\n", "backend: lab_io\n")
    )
    pytester.makepyfile(test_x="def test_x(bench, signals):\n    pass\n")
    result = pytester.runpytest(
        f"--dut={reference.DUT}", f"--bench={reference.BENCH}", f"--harness={reference.HARNESS_IO}"
    )
    result.assert_outcomes(passed=1)
    db = sqlite3.connect(pytester.path / ".benchtare" / "results.sqlite3")
    rows = db.execute("SELECT name, distribution, distribution_version FROM run_plugins").fetchall()
    db.close()
    assert sorted(rows) == [
        ("lab_io", "lab-io-pkg", "1.2.0"),
        ("sim_can", "benchtare", __version__),
    ]


def test_a_name_two_packages_provide_is_refused_naming_both(tmp_path, install):
    install("lab-io-pkg", LAB_IO.format(uuid=LAB_UUID), {"lab_io": "LabIO"})
    install("other-pkg", LAB_IO.format(uuid=OTHER_UUID), {"lab_io": "LabIO"}, version="3.0")
    message = problems(tmp_path, "lab_io")
    assert "backend 'lab_io' is provided by 2 installed packages" in message
    assert f"package lab-io-pkg 1.2.0 (UUID {LAB_UUID})" in message
    assert f"package other-pkg 3.0 (UUID {OTHER_UUID})" in message
    assert "uninstall all but one of them, or name the one you mean by its UUID" in message


def test_naming_by_uuid_settles_a_clash_of_names(tmp_path, install):
    install("lab-io-pkg", LAB_IO.format(uuid=LAB_UUID), {"lab_io": "LabIO"})
    install("other-pkg", LAB_IO.format(uuid=OTHER_UUID), {"lab_io": "LabIO"})
    item = load_bench_file(bench_file(tmp_path, OTHER_UUID)).equipment["io"]
    assert (item.backend, item.plugin.distribution) == ("lab_io", "other-pkg")


def test_a_copied_uuid_is_refused_whichever_name_is_used(tmp_path, install):
    copied = LAB_IO.format(uuid=LAB_UUID).replace('"lab_io"', '"copy_io"')
    install("lab-io-pkg", LAB_IO.format(uuid=LAB_UUID), {"lab_io": "LabIO"})
    install("copy-pkg", copied, {"copy_io": "LabIO"})
    for backend in ("lab_io", "copy_io", LAB_UUID):
        message = problems(tmp_path, backend)
        assert f"declare the same UUID, {LAB_UUID}: one was almost certainly copied" in message
        assert "package lab-io-pkg 1.2.0" in message and "package copy-pkg 1.2.0" in message
        assert "the author of the copy must generate a new UUID" in message


@pytest.mark.parametrize(
    ("change", "problem", "hint"),
    [
        (
            ("api_version = 1", "api_version = 2"),
            "it was written for plugin API version 2, and this BenchTare (",
            "upgrade BenchTare",
        ),
        (
            ("api_version = 1", "api_version = 0"),
            "it was written for plugin API version 0, which this BenchTare (",
            "upgrade broken-pkg",
        ),
        (("api_version = 1", ""), "it doesn't declare which plugin API version", "api_version = 1"),
        (
            (f'uuid = "{LAB_UUID}"', ""),
            "it declares no UUID",
            "uuid = '...'  generated once with",
        ),
        (
            (LAB_UUID, "not-a-uuid"),
            "it declares the UUID 'not-a-uuid', which isn't a UUID",
            "generate one with",
        ),
        (
            (LAB_UUID, "00000000-0000-0000-0000-000000000000"),
            "it declares the nil UUID, a placeholder",
            "generate one with",
        ),
        (
            ('name = "lab_io"', 'name = "labio"'),
            "it is registered as 'lab_io', but its class names itself 'labio'",
            "must match",
        ),
        (
            ("class LabIO:", "import no_such_module\n\n\nclass LabIO:"),
            "it couldn't be imported (ModuleNotFoundError: No module named 'no_such_module')",
            "check that package broken-pkg 1.2.0 is installed properly, or uninstall it",
        ),
        (
            ('kind = "channels"', 'kind = "serial"'),
            "it declares the kind 'serial'; it must be 'channels' or 'bus'",
            "for its author to fix",
        ),
        (
            ("simulated = True", ""),
            "it doesn't say whether it is simulated",
            "simulated = True  or  False",
        ),
    ],
)
def test_a_plugin_that_cant_be_used_is_refused_with_what_is_wrong(
    tmp_path, install, change, problem, hint
):
    old, new = change
    source = DECLARED.format(uuid=LAB_UUID)
    assert old in source
    install("broken-pkg", source.replace(old, new), {"lab_io": "LabIO"})
    message = problems(tmp_path, "lab_io")
    assert "backend 'lab_io' from package broken-pkg 1.2.0 can't be used: " + problem in message
    assert hint in message


def test_an_entry_point_that_names_something_other_than_a_class_is_refused(tmp_path, install):
    install("broken-pkg", "def lab_io():\n    pass\n", {"lab_io": "lab_io"})
    message = problems(tmp_path, "lab_io")
    assert "can't be used: it names function broken_pkg:lab_io, not a class" in message


def test_a_broken_plugin_that_isnt_used_stops_nothing_but_is_logged(install, caplog):
    install("broken-pkg", "import no_such_module\n", {"lab_io": "LabIO"})
    with caplog.at_level(logging.WARNING, logger="benchtare"):
        bench = load_bench_file(reference.path(reference.BENCH))
    assert {item.backend for item in bench.equipment.values()} == {"sim", "sim_can"}
    assert "backend 'lab_io' from package broken-pkg 1.2.0 can't be used" in caplog.text


# --- a backend that isn't installed --------------------------------------------------------------------


def test_a_backend_from_a_known_package_says_what_to_install(tmp_path, monkeypatch):
    monkeypatch.setitem(discovery.KNOWN_PACKAGES, "socketcan", "benchtare-socketcan")
    message = problems(tmp_path, "socketcan")
    assert "unknown backend 'socketcan'" in message
    assert "it comes from the package benchtare-socketcan: install it with" in message
    assert "pip install benchtare-socketcan" in message


def test_a_misspelt_backend_gets_a_suggestion(tmp_path):
    assert "did you mean 'sim_can'?" in problems(tmp_path, "simcan")


def test_an_installation_missing_benchtares_own_backends_is_called_incomplete(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(discovery, "installed_backends", lambda: ())
    message = problems(tmp_path, "sim")
    assert "unknown backend 'sim'" in message
    assert "BenchTare's own backends weren't found either, so its installation is incomplete" in (
        message
    )
