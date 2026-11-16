# BenchTare First-Slice Plan

This is the build plan for the first release. It turns the "First slice"
section of the [design brief](design-brief.md) into a repository layout, a
module list and an order of work. The terms used here are defined in
[model.md](model.md).

The first slice proves one thin path: **signals in, verified results out, with
no hardware attached.** Anything not listed here is out of scope until the
slice passes its acceptance check.

## Assumptions to confirm

These were not decided in the brief. The plan assumes them so that work can
start, and each is easy to change early.

| Assumption | Why | Change cost |
| --- | --- | --- |
| SQLite is the default local database | It is in the Python standard library, so there is nothing to install | Low, if the store sits behind an interface |
| Python 3.10 or newer | Structural pattern matching and modern typing | Low |
| Linux, Windows and macOS, tested in CI | The first slice is pure Python on simulated equipment, so it costs little to keep all three working from the start | Low now, higher once hardware backends exist |
| Standalone project, not built on Jumpstarter | Keeps the first slice free of a large dependency | Medium |

## Dependencies

- **PyYAML** (MIT) reads the interface description, bench and harness files.
- **cantools** (MIT) reads DBC files and encodes and decodes CAN frames.
- **python-can** (LGPL-3.0-only) provides the virtual CAN bus. cantools
  requires it too. It is an ordinary, separately installed dependency, never
  vendored or modified, so BenchTare itself stays MIT.

All three are regular dependencies. One `pip install benchtare` gives every
backend in the package.

## Repository layout

```
benchtare/
├── CLAUDE.md
├── LICENSE
├── README.md
├── pyproject.toml
├── docs/
│   ├── design-brief.md
│   ├── model.md
│   └── first-slice-plan.md
├── src/benchtare/
│   ├── __init__.py
│   ├── signals/
│   │   └── model.py         # Signal and SignalSet
│   ├── dut/
│   │   ├── description.py   # interface description: inline signals, DBC imports
│   │   └── identity.py      # the DUT's identity: expected items and collected values
│   ├── errors.py            # ConfigError and Problem: what failed, where, what to check
│   ├── bench/
│   │   ├── config.py        # bench file: equipment, backends, channels
│   │   └── runtime.py       # Bench: write, wait_until, read
│   ├── harness/
│   │   └── config.py        # harness file: connections, loopbacks, presence checks
│   ├── backends/
│   │   ├── base.py          # Backend protocol, receive buffer, read rule
│   │   ├── sim.py           # in-process simulated I/O
│   │   ├── sim_can.py       # virtual CAN bus through python-can
│   │   └── _can_codec.py    # DBC encoding and frame receiving, shared by both CAN sides
│   ├── sim/
│   │   ├── registry.py      # simulated DUTs a harness can plug in
│   │   └── simulation.py    # wires a simulated DUT to simulated equipment
│   ├── reference/
│   │   ├── device.py        # the simulated reference DUT
│   │   ├── reference.dut.yaml
│   │   ├── reference.dbc
│   │   ├── bench-sim.yaml   # simulated bench: virtual CAN plus simulated I/O
│   │   ├── harness-can.yaml # CAN signals on the virtual bus
│   │   └── harness-io.yaml  # every signal on simulated I/O, no CAN
│   ├── selftest/
│   │   └── checks.py        # bench check and harness check
│   ├── testcheck.py         # the test check: corruption modes and verdicts
│   ├── results/
│   │   ├── schema.py        # the results schema, versioned
│   │   ├── manifest.py      # versions and file hashes
│   │   └── store.py         # ResultStore protocol and SQLite implementation
│   ├── scaffold/
│   │   └── quickstart/      # what `benchtare init` writes
│   ├── requirements/        # optional, milestone M10
│   ├── record.py            # the test record: levels, entries
│   ├── plugin.py            # pytest plugin: options, fixtures, hooks
│   └── cli.py               # init, selftest, check-tests, report
└── tests/                   # BenchTare's own tests, run against the reference
```

Everything a user needs ships inside the package, including the reference and
the scaffold template, and is located with `importlib.resources`.

## Key interfaces

Get these right before writing implementations. Later stages add
implementations without changing them.

- **`Signal`** (`signals/model.py`): frozen. Name, type (`float`, `int`,
  `bool`, `enum` with values), unit (None when there is none; the interface
  description requires numeric signals to declare one or `none`), direction
  (`input` or `output`, from the DUT's point of view). No range. `SignalSet`
  gives attribute access by name and enforces unique names.
- **Interface description** (`dut/description.py`): loads one YAML file per
  DUT into a `SignalSet`, combining inline signals, DBC imports (direction from
  sender and receiver nodes) and overrides. `standards:` is accepted as a key,
  but any value is rejected for now.
- **`Backend`** (`backends/base.py`): a `typing.Protocol`.
  - `open(bindings, receiver)` binds the backend's channels to DUT signals and
    loopback ends. A binding carries the signal's CAN encoding when it has
    one, which a bus backend uses and a plain I/O backend ignores.
  - `close()`.
  - `check()` returns the problems found.
  - `write(binding, value)` raises `WriteError` only when the channel can't
    carry the value.
  - Received values go to the receiver. That is the bench's `ReceiveBuffer`,
    which implements the read rule with a bench-wide sequence counter, not
    wall-clock time. Writes and deliveries share its lock.
  - The backend's class declares its kind (`channels` or `bus`), whether it
    is simulated, and how to check its bench-file options.
- **`Bench`** (`bench/runtime.py`, the `bench` fixture): routes each signal
  through the harness to its equipment and backend.
  - It offers `write`, `wait_until` and `read`. `wait_until` is the way to
    check a reaction. `read` returns the first value received after the
    bench's most recent write.
  - When the harness says `simulate:`, it starts the `Simulation`
    (`sim/simulation.py`), which wires the simulated DUT to the simulated
    equipment.
- **`ResultStore`** (`results/store.py`): `start_run`, then `add_environment`,
  `add_manifest_files`, `add_equipment`, `add_selftest`, `add_test`,
  `add_record` and `add_identity` as the run goes, and `finish_run`. SQLite is
  the only implementation in this slice. M10 adds requirement IDs.
- **`Backend.identity()`**: the equipment's vendor, model, serial number,
  firmware and driver, as far as the backend can tell.

## Order of work

Each milestone ends with passing tests and a commit. Do them in order.

### M0: Packaging skeleton (done)

- The package installs with `pip install -e .` and imports as `benchtare`.
- `pytest` and `ruff` run clean in CI (GitHub Actions). Since M4, CI runs the
  tests on Linux, Windows and macOS, on every branch.

### M1: Interface description and signals (done)

- `Signal` and `SignalSet`.
- Load an interface description with inline signals, DBC imports and
  overrides. Add PyYAML, cantools and python-can as dependencies.
- Tests:
  - load inline signals only, DBC signals only, and a mix of both;
  - a name defined twice is rejected, and the message names both places;
  - an unknown type, and a numeric signal with no unit, are rejected;
  - direction comes from the DBC nodes and can be overridden;
  - any `standards:` value is rejected with a clear message;
  - every error names the file and the signal.

### M2: Bench, harness, backends and the reference DUT (done)

- Load the bench file and the harness file. Check the harness against the
  interface description and the bench file: every connection names a real
  signal and channel, inputs go to drivable channels, and outputs go to
  readable ones.
- `Backend` protocol, the receive buffer and the read rule.
- The `sim` and `sim_can` backends. `sim_can` gives each `open()` its own
  virtual bus name unless the bench file sets one, so two benches in one
  process never share a bus.
- The reference DUT, with its interface description, DBC, one bench file and
  two harness files: one with the CAN signals on a virtual bus, one with
  every signal on simulated I/O. The DUT boots, moves from standby to run on
  request, answers each setpoint with the measured value after a short delay,
  and has `supply_ok` follow `supply_enable`. The simulation builds its
  wiring from the harness file. For tests only, it can cut a wire, unplug the
  DUT from a bus, or fail an equipment item.
- Tests:
  - a stimulus produces the expected response on both backends;
  - a stale value is never returned, and a fast reply is never missed;
  - `wait_until` succeeds, and times out with a clear message;
  - a value too wide for its bits fails with a clear error;
  - two open `sim_can` benches can't hear each other;
  - a harness that connects a DUT input to a read-only channel is rejected.

### M3: pytest plugin (done)

- Register the plugin entry point in `pyproject.toml`. pytest becomes a
  runtime dependency, `pytest>=7.4`, and CI tests that floor as well as the
  latest pytest.
- Options `--dut`, `--bench` and `--harness`, plus the matching
  `benchtare_dut`, `benchtare_bench` and `benchtare_harness` ini settings,
  which also cover `[tool.pytest.ini_options]`.
  - Command-line paths resolve from where pytest runs, and ini paths from the
    ini file.
  - The files are checked at start-up, and a wrong file is a usage error
    before any test runs.
- Fixtures: `bench` and `signals`, both session-scoped. The bench opens on
  first use and closes when the run ends. With nothing configured, the plugin
  does nothing, and asking for `bench` fails with a message saying what to
  set.
- Record every read as a measurement on each test report, by phase. (Since
  replaced by the test record, `report.benchtare_record`.) `record_property`
  values stay in `report.user_properties`. M5 stores both.
- Tests (pytester):
  - a staging fixture and the example test from [model.md](model.md) pass
    against the reference;
  - a staging fixture that fails is reported as an error, not a failure;
  - nothing happens when nothing is configured.
  - Tests that use `sim_can` run with `runpytest_subprocess`.

### Test record and logging (done, between M3 and M4)

- **The test record** (`record.py`): what happened at the bench during each
  test, with three levels: `results` (readings, comparisons, outcomes),
  `stimuli` (also writes; the default) and `debug` (also frames). Set with
  `--bench-record` or `benchtare_record`. Kept per phase on each report as
  `report.benchtare_record`, which replaces the M3 measurements list, and shown
  with a failed test. Passing asserts are recorded when pytest's
  `enable_assertion_pass_hook` is on. See "The test record" in
  [model.md](model.md).
- **Unexpected events are never hidden.** A frame from the DUT that doesn't
  match the DBC, and equipment that stops working, are recorded at every level
  and raised as pytest warnings for the test (`FrameMismatchWarning`,
  `EquipmentWarning`). The outcome is unchanged; `-W error::...` makes them
  fail. Events between tests go to the next test; after the last, to the end of
  the run.
- **BenchTare's own log**: standard `logging` under `benchtare`, for debugging
  BenchTare, with pytest's usual log options. The three files are loaded at
  session start, not in `pytest_configure`, so pytest's live log and log file
  capture the loading. No new dependency.
- CAN frames are received on BenchTare's own thread (`FrameReceiver`) instead
  of python-can's `Notifier`. A frame the handler can't decode, such as a
  multiplexer value the DBC doesn't define, used to end reception for the rest
  of the run with nothing said. Now it is recorded and skipped. A bus failure
  stops reception, is logged and recorded, and is reported by `check()`. The
  same goes for the `sim` sampler and the simulated DUT's CAN port.
- Tests: what each level keeps; every bad frame recorded; warnings per test,
  between tests and after the last; `-W error` makes them fail; a failed test
  shows its record; a failed bus, DUT port and sampler are each caught; a
  healthy run logs no warnings and leaves no threads.
- Deferred: the bench checks frames against the DBC only for messages that
  carry a connected signal. Checking every message the DUT sends, connected or
  not, is a possible later step.

### M4: Self-test (done)

- `selftest/checks.py`: the bench check (each equipment item's `check()`, and
  the simulated DUT) and the harness check (each loopback reads back `True`
  then `False` within 1 s; each presence check sees bus traffic or a value of
  its signal within `within:`). Checks on failed equipment are skipped.
- The plugin runs it after collection, before the first test, when any
  collected test uses `bench`. A failure lists every check and stops the run
  with `pytest.exit` (exit code 2). No tests run.
- `benchtare selftest` runs `pytest --selftest-only`, so it reads the same
  options and ini file as a test run, and pytest's own log options apply. Exit
  codes: 0 passed, 1 failed, 4 couldn't start. The `benchtare` console script
  is registered, and `python -m benchtare` works too.
- The bench gained channel-level access for loopback ends (`drive_channel`,
  `wait_channel`), `seen` for presence checks, and bus backends a
  `wait_for_traffic` method (`BusBackend`).
- Tests: the healthy reference passes on both harnesses; failed equipment, a
  failed bus, a cut loopback, a stuck loopback, a DUT unplugged from the bus, a
  DUT that never boots, a stopped simulation and a bench that won't open are
  each caught; no tests run after a failure; the self-test doesn't run when no
  test uses the bench or when only collecting; the CLI exit codes are right.

### M5: Results and manifest (done)

Built from [results-schema.md](results-schema.md), version 1. Results go to
`results/schema.py` (creating, refusing and migrating the database),
`results/manifest.py` (file hashes, git state, environment, packages) and
`results/store.py` (`ResultStore` and `SQLiteStore`). DUT identity is in
`dut/identity.py`, `Bench.identify`, `Bench.read_identity`, and the
interface description's `identity:`. The reference DUT now reports
`SwVersion` (102) and lists it as its `software` identity item; the simulated
bench gained a fifth input, `io.in5`, so the IO harness carries it too.

- Results schema with a schema version from day one (`PRAGMA user_version`
  plus a `schema_version` column). A database newer than the code is refused
  with a clear message. An older one goes through a migration list.
- Manifest: BenchTare, pytest and Python versions, a timestamp, the git
  commit, and hashes of the interface description, its DBC files, the bench
  file, the harness file, each test file, each `conftest.py` and the ini file.
- The run's environment: the machine, OS, Python build and installed packages.
- Bench hardware: backends report each equipment item's vendor, model, serial
  number, firmware and driver (`identity()` on the Backend protocol); the
  simulated backends report themselves as simulated.
- DUT identity: the interface description's `identity:` list, read from
  signals before the first test, `bench.identify()` for values from the team's
  code (any name, listed or not), and `--identify NAME=VALUE`. A listed item
  never collected is stored as missing, with a warning.
- SQLite store behind `ResultStore`, at `.benchtare/results.sqlite3` by
  default; `--results PATH` or `benchtare_results` moves it, and
  `--no-results` skips storing a run.
- Tests:
  - two runs store two distinct manifests that read back correctly;
  - changing a test file or a `conftest.py` changes its hash;
  - a newer-schema database is refused, and an older one is migrated;
  - a failed test is stored as failed, and a crashed run stays `running`;
  - readings can be queried across runs;
  - identity values from a signal, from code and from the command line are
    stored; an added name is stored; a missing listed item is stored as
    missing and warned about.

### M6: Test check (done)

- `benchtare/testcheck.py`: the two modes, `shift` (a wrong value) and `stale`
  (the value from before the most recent write), and the verdicts. The bench
  logs each reading a test's body takes as a verification step, with the test's
  line, and corrupts one chosen step while a mode is set; the receive buffer
  keeps each value as it was before the most recent write. `check=False` marks
  a reading the test deliberately doesn't check.
- `wait_until` tells a failed comparison from a timeout: values that arrived
  but never matched raise `ComparisonFailed`, an `AssertionError`, and only
  nothing arriving at all is a `ReadTimeout`. This applies to every run, not
  only the test check, so a timeout set from a requirement is the test's timing
  check.
- `pytest --check-tests` and `benchtare check-tests` run each test as usual,
  then walk through each one that passed: a control rerun with nothing
  corrupted, which must pass and take the same readings, then one rerun per
  reading and mode with only that reading corrupted, through pytest's own
  `runtestprotocol`. A test can fail only if every checked reading can (exit
  code 1 otherwise). A rerun's failure counts only if it happened at or after
  the corrupted step with something corrupted; anything else is inconclusive
  with the reason, so every failure counted is real.
- Results schema version 2, the first real migration: `test_checks` (per
  rerun: reading, signal, location, mode, outcome), `test_results.check_verdict`,
  and each test file's last check in `manifest_files`. Each rerun's record is
  stored too. A usual run ends with a line naming test files that were never
  checked or have changed since.
- Tests:
  - a test with a real check at every step is reported as able to fail;
  - a test with one unchecked step, one that ignores its reading, or one that
    accepts almost anything, is reported as unable to fail at that step, or
    shows the mode it passes;
  - a failure before the corrupted step, a failure with nothing corrupted, a
    rerun where nothing arrives, a test that doesn't repeat and one that takes
    different readings are all inconclusive, never able to fail;
  - a failure caught at a later step counts, and says where;
  - staging reads real values, failing tests aren't rerun, reruns aren't
    counted as tests, and `check=False` readings are left out;
  - a version 1 database is upgraded with its runs kept.
- Decided during M6 (see the design brief): no ranges and no `late` mode. A
  timeout set from the requirement is the timing check, boundary values belong
  in tests, and ranges from standards plus boundary-value helpers are later
  stages 7 and 13.

### M7: Test hygiene (done)

The third of the areas BenchTare verifies (see "What BenchTare verifies" in
`docs/model.md`): that each test stands on its own, checked on its own, with
its own verdicts, report and stored results. M7 gets the logic of the checks
right; a more realistic reference to exercise them against comes later.

- `clean_state:` in the interface description lists what shows the DUT is
  clean, by name, with its clean value where the team knows one (`Mode:
  standby`), `tolerance:` for noisy numbers and `settle:` for time to come back.
  Items without a clean value are compared before setup and after teardown.
  State probes (`probe: true`) are values a `pytest_benchtare_state` hook in
  `conftest.py` returns, for state no signal shows.
- `benchtare/hygiene.py`: the rules, verdicts and the judgement of each item,
  as pure functions. The bench remembers the value last written to each input,
  and notes a `wait_until` already satisfied before the most recent write.
- `pytest --check-hygiene` and `benchtare check-hygiene` run each test as
  usual, then again straight after itself, and judge three rules: it repeats,
  it leaves the DUT clean, it starts from a meaningful state. Only the test that
  left an item dirty is blamed: one that finds it dirty and leaves it so isn't,
  and one that puts it back is clean. A test that isn't clean makes the exit
  code 1. Not combined with `--check-tests` in one run.
- Results schema version 3: `hygiene_checks`, `hygiene_items`,
  `test_results.hygiene_verdict`, and each test file's last hygiene check in
  `manifest_files`. A usual run's closing lines cover both on-demand checks.
- Tests, against the reference as it is: a clean test; a test that leaves an
  input or an output dirty; one that finds it dirty and leaves it, or cleans it
  up; one that doesn't repeat; a wait already satisfied before its write, in
  the first run or the repeat; a state probe; tolerance and settle with values
  that differ or arrive late; and each rule broken on purpose.

### M8: DUT model plugins (done)

The reference DUT was a model already: `reference/device.py` described how it
reacts to stimuli, and the harness named it with `simulate:`. M8 lets a team do
the same for its own DUT, so the tests, the self-test, the test check and the
hygiene check all run without hardware.

- `benchtare.DutModel` (`benchtare/sim/model.py`), generalised from the
  reference device: a team overrides `started()` and `on_input(name, value)`,
  and calls `publish`, `after` and `every`. All of a model's code runs on one
  thread, so it needs no locks. An error in a model is recorded, the model keeps
  running, and the self-test's bench check reports it. The reference DUT is now
  a `DutModel` too, so the whole suite exercises the base class.
- The harness file's `simulate:` names a model that ships with BenchTare
  (`reference`), or a team's own as `models/my_dut.py:MyDut`, a Python file
  relative to the harness file, imported without needing a place on the path;
  a mapping form gives the model options. Mistakes are reported with the file
  and line: no such file, a class that isn't there or isn't a `DutModel`, an
  import error, signals the interface description doesn't have, options the
  model doesn't take.
- A model that publishes something that isn't one of the DUT's outputs is
  reported by the bench check, instead of the value silently going nowhere.
- Tests: the base class (order, one thread, `after`, `every`, errors, options);
  naming models and every mistake in it; and a team-style thermostat project,
  with its own interface description, bench file, harness and model, through
  the tests, the self-test, the test check and the hygiene check.
- Found along the way: a `wait_until` whose readings the test check had
  corrupted counted as "nothing corrupted" when its last value happened to be
  unchanged, which made a good test look like a hygiene problem. It now counts
  any value the wait saw.
- Later, not in M8: active disruption for hygiene (putting the model into
  another state before a test's setup, to see whether staging recovers it),
  opt-in and on models only.

### M9: Scaffold and acceptance

- `benchtare init <dir>` writes the packaged quickstart: an interface
  description, a bench file, a harness file, a `conftest.py` with a staging
  fixture, and one passing test, all on the reference.
- Write the quickstart section of the README.
- CI job:
  1. build the wheel;
  2. install it in a fresh virtual environment;
  3. run `benchtare init demo`, `cd demo`, `pytest`, which must pass;
  4. run `benchtare check-hygiene`, which must pass;
  5. run `benchtare check-tests`, which must report the test as able to fail.
- **Acceptance check:** on a clean machine, `pip install benchtare`,
  `benchtare init demo`, `cd demo`, `pytest` gives a passing test in under 15
  minutes, for someone who knows pytest and has never seen the tool. The
  hygiene check then passes, and the test check shows that the test can fail.

### M10 (optional): Requirements and trace report

Only after M9 passes.

- First decide how to read Doorstop trees. Either read its YAML item files
  directly with PyYAML, or depend on `doorstop`, which is LGPLv3 and brings
  heavy dependencies.
- A `requirement` marker: `@pytest.mark.requirement("SRS-001")`. Check the
  convention used by jamb first and match it if practical.
- Warn on a marker that names an unknown ID.
- Store requirement IDs with each result.
- `benchtare report`: an HTML and Markdown trace report. It lists each
  requirement with its tests and latest outcome, requirements with no test,
  and tests with no requirement.

## Out of scope for this slice

Do not build these yet, even where the design brief describes them:

- Real hardware backends and DAQ devices.
- Transports other than virtual CAN (serial, sockets, MQTT).
- Protocols above raw CAN signals (CANopen, Modbus, UDS, J1939, ISOBUS),
  including any `standards:` value in an interface description.
- Interface-level fault stimuli, such as dropping or stopping a CAN message.
- Writing multiplexed CAN signals. Reading them works, and the harness file
  refuses a multiplexed DUT input on a bus.
- A remote database.
- ReqIF, StrictDoc or Sphinx-Needs importers.
- Write-back to any requirements tool.
- Trend analysis and dashboards.
- A GUI (design brief, later stage 10).
- IDE setup for test development, such as editor schemas for the YAML files or
  run configurations (design brief, later stage 9). `benchtare init` (M9)
  writes the quickstart project only.
- A full user guide with worked examples (design brief, later stage 12). M9
  writes the README quickstart only; recommended practices found along the way
  go on the brief's list of guide topics.
- Platform-specific hardware support and setup guides (design brief, later
  stage 11). The first slice is tested on Linux, Windows and macOS: keep it
  portable, with no OS-specific calls and paths through `pathlib`.
- Closed-loop or real-time simulation.
- Ranges on signals, and helpers that generate boundary values from them
  (design brief, later stages 7 and 13). The test check (M6) needs no ranges.
- A more realistic reference for exercising test hygiene: measurement noise,
  settling ramps, a fault that latches until a supply cycle, a free-running
  counter, and state only a probe can read (design brief, later stage 4).
- Transports beyond virtual CAN, including SSH (design brief, later stage 3).
  M7's state probes are hooks the team writes, reached however it likes.
- Replaying recorded readings in the test check, for signals that need it;
  running tests in another order; active disruption for hygiene (design brief,
  later stage 4).

## Before the first release

- [x] Confirm `benchtare` is free on PyPI and on GitHub.
- [x] Create the GitHub repository.
- [ ] Publish the 0.0.1 placeholder to PyPI so that `pip install benchtare`
      resolves to this project.
- [ ] Spend an hour with jamb and Jumpstarter, to confirm the prior-art
      assessment from hands-on use.
