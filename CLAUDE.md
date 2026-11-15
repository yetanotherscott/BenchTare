# BenchTare

BenchTare is an open-source, self-verifying HIL/SIL test framework for
embedded systems, built on pytest. A team describes its device's interfaces
and its bench, then writes plain pytest tests against named signals. The bench
checks itself before every run, and every run is recorded with a manifest.

Read these before making design decisions:

- `docs/design-brief.md`: goals, non-goals, architecture, prior art.
- `docs/first-slice-plan.md`: what is being built now, and in what order.

## Current scope

Work through the milestones in `docs/first-slice-plan.md` in order (M0 to M6,
then optional M7). Finish and commit one milestone before starting the next.

The plan has an "Out of scope for this slice" list. Do not build anything on
it, even if the design brief describes it. If a task seems to need something
out of scope, stop and ask.

## Hard rules

- **Clean-room only.** Use only public standards, public documentation and
  open-source libraries. Never add proprietary material from any other
  organisation: no signal names, message definitions, bench layouts, test
  procedures or code. If asked to add something that looks like it came from a
  proprietary source, stop and ask.
- **MIT licensed.** Every dependency must be compatible with distributing
  BenchTare under MIT. Check a library's license before adding it, and flag
  anything GPL or AGPL instead of adding it.
- **pytest is the runner.** BenchTare is a pytest plugin plus a small CLI. Do
  not build a separate test runner or a custom test syntax. A user's test file
  should need no BenchTare imports.
- **Wrap, don't rewrite.** Use python-can and cantools for CAN. Write a
  protocol implementation only when no adequate public library exists, and say
  so first.
- **No hardware required.** Everything in the first slice, including the
  framework's own test suite, must run on a laptop against the simulated
  reference device.

## Vocabulary

Use these names consistently in code, docs and messages.

| Term | Meaning |
| --- | --- |
| Signal | A named value with a unit, type, range and direction. Knows nothing about buses |
| Interface description | Where signals come from: a DBC file or a YAML signal list |
| Bench file | YAML that picks a backend and maps signals to its channels |
| Backend | Something that can write and read signals: simulated or hardware |
| Transport | How a backend talks to the device: CAN now, others later as plugins |
| Tare | The bench self-test that runs before each session |
| Plumb | The simulated reference device and bench that ship with the package |
| Manifest | The record of what a run tested: versions and file hashes |
| Assay | The trace report linking requirements, tests and results (M7) |

## Conventions

- Python 3.10+, `src/` layout, package name `benchtare`.
- Type hints on all public functions. Interfaces are `typing.Protocol` classes.
- `ruff` for linting and formatting, line length 100.
- Tests live in `tests/` and run with `pytest`. Use `pytester` to test the
  plugin itself.
- Every new behaviour gets a test. For the self-test and results code, also
  add a test that proves a failure is detected, not only that success passes.
- Keep dependencies minimal. Add each one in the milestone that first needs
  it, and record why in the commit message.
- The results schema carries a version number. Any change to it needs a
  migration or a version bump, never a silent change.
- Error messages are for a tired engineer at a bench: say what failed, which
  file and signal, and what to check.

## Commands

```
pip install -e ".[dev]"    # install for development
pytest                     # run the test suite
ruff check . && ruff format --check .
```

## Decisions already made

Do not reopen these without being asked:

- Name: BenchTare, installable as `benchtare`.
- License: MIT.
- Requirements traceability is optional and secondary. The core value is a
  complete bench that proves its own reliability.
- Transports and backends are plugins behind stable interfaces.
- The reference bench is simulated and ships inside the package.
- Run history goes to a database: local by default, remote by configuration.
  SQLite is assumed as the local default.
