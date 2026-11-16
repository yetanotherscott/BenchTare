# BenchTare

A self-verifying HIL/SIL test framework for embedded systems, built on pytest.

**Status: in development, not released yet.** The first slice is built:
everything below runs on a laptop, against a simulated reference DUT or a
team's own DUT model. Hardware backends come after it. It runs on Linux,
Windows and macOS, with Python 3.10 or later and pytest 7.4 or later.

## What it does

BenchTare gives an embedded team a complete test bench, for hardware or
simulation, and proves that its own results can be trusted.

- **You describe the setup, and write plain pytest.** An interface description
  lists your device's signals (inline, or imported from DBC files); a bench
  file lists your equipment; a harness file says which signal is on which
  channel. Tests use named signals through a `bench` fixture, and need no
  BenchTare imports.
- **It verifies everything under the device, each area on its own,** so a pass
  or a fail reflects the device and not the setup around it:

  | Area | Verified by | When |
  | --- | --- | --- |
  | The bench | The self-test's bench check: every equipment item is healthy | Before every run |
  | The harness | The self-test's harness check: loopbacks read back, the DUT is present | Before every run |
  | Test hygiene | `benchtare check-hygiene`: each test repeats, leaves the DUT clean, and starts from a meaningful state | On demand |
  | Test logic | `benchtare check-tests`: each test can fail at every check, proven by corrupting what it reads, one reading at a time | On demand |
  | The DUT | Your tests | Every run |

  A failure in a lower area makes the areas above it "not checked", never a
  verdict of their own.
- **It runs without hardware.** A simulated reference DUT, bench and harness
  ship with the package, and you can describe your own DUT as a model in
  Python, so tests and checks run on a laptop or in CI.
- **Every run is recorded** in a local results database: each test's outcome
  and every reading, comparison and stimulus, with a manifest of what was
  tested (file hashes, git commit), the machine it ran on, the bench equipment,
  and the DUT's hardware and software versions.
- **Requirements are optional,** and planned: tests will trace to them when you
  have them.

## Quickstart

BenchTare isn't on PyPI yet, so install it from GitHub, preferably into a
virtual environment. Then write the quickstart project and run it:

```bash
pip install git+https://github.com/yetanotherscott/BenchTare
benchtare init demo
cd demo
pytest
```

`benchtare init` writes a complete project on the simulated reference DUT, so
no hardware is needed: an interface description, a bench file, a harness file,
staging fixtures in `conftest.py`, and one test. `pytest` runs the self-test,
then the test. Two more commands check the test itself:

```bash
benchtare check-hygiene
benchtare check-tests
```

The first proves the test stands on its own, and the second proves it can
fail. The project's own README says what each file is, and suggests three ways
to break the project, each caught by a different check.

## Commands

BenchTare is a pytest plugin, so tests run with `pytest`. Give it the three
files on the command line, or set them in your pytest configuration:

```bash
pytest --dut my.dut.yaml --bench bench.yaml --harness harness.yaml
```

Three more commands use the same files and settings:

| Command | What it does |
| --- | --- |
| `benchtare selftest` | The self-test alone: the bench check and the harness check |
| `benchtare check-tests` | The test check: proves each test can fail at every check |
| `benchtare check-hygiene` | The hygiene check: proves each test stands on its own |

## Documents

- [Model](docs/model.md): what BenchTare models, and what each file you write is for.
- [Design brief](docs/design-brief.md): goals, non-goals, architecture, prior art.
- [First-slice plan](docs/first-slice-plan.md): what is being built first, and in what order.
- [Results schema](docs/results-schema.md): what each run stores in the results database.

## License

MIT. Improvements are welcome back as pull requests, and are not required.

BenchTare depends on [python-can](https://github.com/hardbyte/python-can), which
is licensed LGPL-3.0. It is installed as a separate, unmodified package and is
not bundled with BenchTare, so BenchTare's own code remains MIT.

## How this project uses AI

BenchTare is not handwritten. An AI coding assistant writes most of the code,
tests and documentation. A person sets the goals, makes the design decisions,
and decides what is merged.

It is partly an experiment, to find out what it takes to get AI-generated code
you could trust. The same rules apply to all of it:

- Every behaviour has tests, and the whole suite runs in CI on Linux, Windows
  and macOS.
- Each check BenchTare makes is itself tested by breaking it on purpose, to
  prove that the failure it exists for would be caught.
- Clean-room only: public standards, public documentation and open-source
  libraries, with each dependency's licence checked against MIT.

This covers the whole project; individual commits are not labelled.
