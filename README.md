# BenchTare

A self-verifying HIL/SIL test framework for embedded systems, built on pytest.

**Status: planning.** Nothing here is usable yet. The 0.0.1 release exists to
establish the package name while the first slice is built.

## What it will do

BenchTare gives an embedded team a complete test bench out of the box, for
hardware or simulation, and proves that its own results are reliable.

- You describe your device (an interface description, which can import DBC
  files), your bench equipment, and the harness that connects them. You write
  plain pytest tests against named signals.
- It verifies everything under the DUT, each area on its own, so a pass or a
  fail reflects the device and not the setup:
  - before each run, the bench and the harness check themselves: equipment
    healthy, loopbacks reading back, DUT present. If not, the run doesn't
    start;
  - on demand, it proves each test can fail at every check, by corrupting what
    it reads, one reading at a time;
  - on demand (planned), it proves each test stands on its own: it repeats,
    leaves nothing behind and starts from a meaningful state.
- The same tests run on a simulated bench or on real hardware.
- Every run is recorded in a database with a manifest of what was tested.
- Requirements are optional. When you have them, tests trace to them.

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
