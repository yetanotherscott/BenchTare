# BenchTare

A self-verifying HIL/SIL test framework for embedded systems, built on pytest.

**Status: planning.** Nothing here is usable yet. The 0.0.1 release exists to
establish the package name while the first slice is built.

## What it will do

BenchTare gives an embedded team a complete test bench out of the box, for
hardware or simulation, and proves that its own results are reliable.

- You describe your device's interfaces (a DBC file or a YAML signal list) and
  your bench wiring. You write plain pytest tests against named signals.
- Before each run, the bench "tares" itself: a known stimulus must produce the
  known response, or the run does not start.
- The same tests run against a simulated backend or real hardware.
- Every run is recorded in a database with a manifest of what was tested.
- Requirements are optional. When you have them, tests trace to them.

## Documents

- [Design brief](docs/design-brief.md): goals, non-goals, architecture, prior art.
- [First-slice plan](docs/first-slice-plan.md): what is being built first, and in what order.

## License

MIT. Improvements are welcome back as pull requests, and are not required.
