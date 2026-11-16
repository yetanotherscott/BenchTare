"""``benchtare init``: a new project, copied from the quickstart that ships in the package.

The quickstart is a complete BenchTare project on the simulated reference DUT, so it needs no
hardware: an interface description and the DBC it imports, a bench file, a harness file, a
``conftest.py`` with staging fixtures, one test, a ``pytest.ini`` naming the three files, and a
README saying what each file is.

Its bench file, harness file and DBC are the reference's own; a test keeps them equal. Its
interface description is the reference's plus ``clean_state:``, so the hygiene check has
something to check.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

QUICKSTART = "quickstart"


class InitError(Exception):
    """``benchtare init`` refused, and wrote nothing."""


def quickstart_files() -> dict[str, bytes]:
    """The quickstart's files, by name.

    Only its files are copied: folders (such as ``__pycache__`` or ``.benchtare``, left by a run
    inside the package) and hidden files are skipped.
    """
    folder = files(__package__) / QUICKSTART
    return {
        entry.name: entry.read_bytes()
        for entry in folder.iterdir()
        if entry.is_file() and not entry.name.startswith(".")
    }


def init(target: Path) -> list[Path]:
    """Copy the quickstart into ``target``, creating the folder if needed.

    Other files already in ``target`` are left alone, but if any file the quickstart would write
    is already there, nothing is written and ``InitError`` says which. Returns the files written.
    """
    contents = quickstart_files()
    if target.exists() and not target.is_dir():
        raise InitError(
            f"{target} is a file, not a folder. Give benchtare init a folder for the new project."
        )
    taken = sorted(name for name in contents if (target / name).exists())
    if taken:
        raise InitError(
            f"{target} already has {_listed(taken)}. benchtare init never overwrites a file: "
            "give it a new or empty folder, or move those files aside."
        )
    target.mkdir(parents=True, exist_ok=True)
    written = []
    for name in sorted(contents):
        path = target / name
        try:
            with path.open("xb") as file:  # "x": never overwrite, even one created just now
                file.write(contents[name])
        except FileExistsError:
            raise InitError(
                f"{path} appeared while benchtare init was writing the project; it was left as it "
                f"was. Already written: {_listed([p.name for p in written]) or 'nothing'}."
            ) from None
        written.append(path)
    return written


def _listed(names: list[str]) -> str:
    return ", ".join(names)
