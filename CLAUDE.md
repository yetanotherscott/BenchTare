# BenchTare

BenchTare is an open-source, self-verifying HIL/SIL test framework for
embedded systems, built on pytest. A team describes its device (the DUT), its
bench and the harness between them, then writes plain pytest tests against
named signals. The bench and harness check themselves before every run, and
every run is recorded with a manifest.

Read these before making design decisions:

- `docs/model.md`: what BenchTare models and what each file is for. This is
  the reference for terms and concepts.
- `docs/design-brief.md`: goals, non-goals, architecture, prior art.
- `docs/first-slice-plan.md`: what was built for the first slice, and in what
  order.
- `docs/plugin-plan.md`: on the `plugins` branch, what is being built now: the
  plugin interfaces and the example plugins, one step at a time.

## Current scope

The milestones in `docs/first-slice-plan.md` are done through M9. What's left
of the slice is the acceptance check, by hand. M10 (requirements and the trace
report) is optional: start it only when asked. Finish and commit one milestone
before starting the next.

On the `plugins` branch, work follows `docs/plugin-plan.md` instead, one step
at a time. At each step's check-in, confirm its decisions with the user before
writing code. The plan is a draft and will change.

Each plan says what not to build, even where the design brief describes it.
On `main`, that is the first-slice plan's "Out of scope for this slice" list.
On the `plugins` branch, it is the plugin plan's "Not in this plan" list
instead: the plugin plan builds some things the first-slice list rules out,
such as SocketCAN, serial and SSH. If a task seems to need something out of
scope, stop and ask.

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
- **Linux, Windows and macOS.** CI tests all three. No OS-specific calls,
  paths through `pathlib`, and no reliance on an OS's own error wording.

## Vocabulary

Use these names consistently in code, docs and messages.

| Term | Meaning |
| --- | --- |
| DUT | The device under test, real or simulated |
| Signal | A named value on the DUT: name, type, unit and direction (`input` or `output`, from the DUT's point of view). No range. Knows nothing about buses |
| Interface description | One YAML file per DUT listing its signals: inline, imported from DBC files, or both |
| Bench | The equipment that drives and reads signals, real or simulated |
| Bench file | YAML listing the bench's equipment, with each item's backend and channels |
| Harness | What connects the bench to the DUT |
| Harness file | YAML mapping each signal to a bench channel, with loopback and DUT presence checks |
| Backend | Code that drives one kind of equipment (a CAN interface, a DAQ, a serial port): opens, closes, checks and describes it, and moves values on its channels. A channels backend carries plain values; a bus backend carries many signals through a transport. Simulated now, hardware later |
| Transport | The encoding between a DUT's signals and what a bus carries: DBC frames on CAN now, others later as plugins. It belongs to the DUT, in the interface description. CAN, serial and SSH are equipment, reached through backends, not transports |
| Plugin | A separately installable package that adds a backend or a transport through the public plugin API |
| Verified, attested, asserted | What each result and claim rests on: BenchTare proved it; a person carried out a recorded procedure; or the equipment, a plugin, the DUT or the team says so, and BenchTare only records it |
| Staging | Putting the DUT into the state a test needs, using ordinary pytest fixtures |
| Self-test | The bench check and harness check that run before each session |
| Hygiene check | The on-demand run that proves each test stands on its own: it repeats, leaves nothing behind, and starts from a meaningful state (M7) |
| Test check | The on-demand run that corrupts readings, one at a time, to prove each test can fail at every check |
| Reference | The simulated DUT, bench and harness that ship with the package |
| DUT model | A simulation of a DUT, written by subclassing `benchtare.DutModel` and named in the harness file's `simulate:`; the reference DUT is one |
| Test record | What happened at the bench during a test, kept with its result: readings, comparisons and outcomes, then stimuli, then frames |
| Manifest | The record of what a run tested: versions and file hashes |
| DUT identity | The DUT's hardware revision and software versions, recorded per run. The interface description lists the expected items by name, never their values |
| Trace report | The report linking requirements, tests and results (M10) |
| Clean state | The signals and state probes an interface description lists as showing the DUT is clean, by name; the hygiene check compares only these |
| Verification areas | Bench, harness, test hygiene, test logic and the DUT: what BenchTare verifies, each on its own, in that order |

## Conventions

- Python 3.10+, `src/` layout, package name `benchtare`.
- Type hints on all public functions. Interfaces are `typing.Protocol` classes.
- `ruff` for linting and formatting, line length 100.
- Tests live in `tests/` and run with `pytest`. Use `pytester` to test the
  plugin itself.
- Every new behaviour gets a test. For the self-test and results code, also
  add a test that proves a failure is detected, not only that success passes.
- A test failure is never ignored, rerun until it passes, or put down to a slow
  runner without finding its cause. A test that fails only sometimes depends on
  timing or order: fix it so its outcome doesn't, and show the fix by
  reproducing the failure first. Check CI on every branch pushed, `main`
  included, and report a failure as soon as it is found.
- Keep dependencies minimal. Add each one in the milestone that first needs
  it, and record why in the commit message.
- The results schema carries a version number. Any change to it needs a
  migration or a version bump, never a silent change.
- Versions follow Semantic Versioning: until 1.0.0, a release that breaks
  something users rely on bumps the minor version, and any other release bumps
  the patch version. Every notable change gets a line under `## [Unreleased]`
  in `CHANGELOG.md`, in the Keep a Changelog format.
- To release: set the version in `pyproject.toml` and
  `src/benchtare/__init__.py`, move the Unreleased lines under the new version
  and its date, and update the links at the end of the changelog. Merge into
  `main`, then publish a GitHub release tagged `v` plus the version. The
  publish workflow uploads it to PyPI once the `pypi` environment's reviewer
  approves.
- Error messages are for a tired engineer at a bench: say what failed, which
  file and signal, and what to check.
- Two records, kept apart. What happened at the bench during a test (reads,
  comparisons, writes, frames, anything unexpected) goes into the test record,
  and unexpected events are recorded at every level. BenchTare's own log is for
  debugging BenchTare: `logging.getLogger(__name__)`, under `benchtare`. Raise
  errors rather than logging them, and log anything that fails on a background
  thread where `check()` can also report it. See "The test record" in
  `docs/model.md`.
- BenchTare never judges a signal's value. Pass criteria live in test
  assertions, and any value a channel can carry may be written.
- Commit messages and PR descriptions carry no Claude attribution: no
  `Co-Authored-By` trailer and no "Generated with" line.
- Project documents describe the project only. They never mention anyone's
  personal circumstances or employment.

## Commands

```
pip install -e ".[dev]"    # install for development
pytest                     # run the test suite
ruff check . && ruff format --check .
benchtare init demo    # write the quickstart project into demo/
benchtare selftest --dut … --bench … --harness …   # the self-test alone
benchtare check-tests --dut … --bench … --harness …   # prove each test can fail
benchtare check-hygiene --dut … --bench … --harness …   # prove each test stands on its own
```

## Decisions already made

Do not reopen these without being asked:

- Name: BenchTare, installable as `benchtare`.
- License: MIT.
- Requirements traceability is optional and secondary. The core value is a
  complete bench that proves its own reliability.
- Backends and transports are plugins behind stable interfaces. A backend
  drives equipment; a transport is an encoding, such as DBC frames on CAN. See
  "Backends and transports" in `docs/model.md`.
- Every result and claim is verified, attested or asserted, and is to be
  labelled as such; results don't carry the labels yet. See "Verified, attested
  and asserted" in `docs/model.md`.
- The reference DUT, bench and harness are simulated and ship inside the
  package.
- One install: python-can (LGPL-3.0, an unmodified dependency) and cantools
  are regular dependencies, not extras.
- Run history goes to a database: local by default, remote by configuration.
  SQLite is the local default.
- BenchTare verifies five areas, each on its own and in this order: the bench,
  the harness, test hygiene, test logic, and the DUT. The DUT is the area of
  interest; the other four are there so its results reflect physical reality.
  Each has its own check, verdict, report and stored results, and fails only
  for its own area. A lower area's failure makes the areas above it "not
  checked, because of" it, never a verdict of their own. See "What BenchTare
  verifies" in `docs/model.md`.
