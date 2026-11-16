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
- `docs/first-slice-plan.md`: what is being built now, and in what order.

## Current scope

Work through the milestones in `docs/first-slice-plan.md` in order (M0 is done;
M1 to M9, then optional M10). Finish and commit one milestone before starting
the next.

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
| Backend | Code that drives one kind of equipment: simulated now, hardware later |
| Transport | How a backend reaches the DUT: CAN now, others later as plugins |
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
- Keep dependencies minimal. Add each one in the milestone that first needs
  it, and record why in the commit message.
- The results schema carries a version number. Any change to it needs a
  migration or a version bump, never a silent change.
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
- Transports and backends are plugins behind stable interfaces.
- The reference DUT, bench and harness are simulated and ship inside the
  package.
- One install: python-can (LGPL-3.0, an unmodified dependency) and cantools
  are regular dependencies, not extras.
- Run history goes to a database: local by default, remote by configuration.
  SQLite is assumed as the local default.
- BenchTare verifies five areas, each on its own and in this order: the bench,
  the harness, test hygiene, test logic, and the DUT. The DUT is the area of
  interest; the other four are there so its results reflect physical reality.
  Each has its own check, verdict, report and stored results, and fails only
  for its own area. A lower area's failure makes the areas above it "not
  checked, because of" it, never a verdict of their own. See "What BenchTare
  verifies" in `docs/model.md`.
