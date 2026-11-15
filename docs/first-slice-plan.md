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
│   │   └── description.py   # interface description: inline signals, DBC imports
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
│   ├── testcheck/
│   │   └── corrupt.py       # shift and stale corruption, can-fail report
│   ├── results/
│   │   ├── schema.py        # the results schema, versioned
│   │   ├── manifest.py      # versions and file hashes
│   │   └── store.py         # ResultStore protocol and SQLite implementation
│   ├── scaffold/
│   │   └── quickstart/      # what `benchtare init` writes
│   ├── requirements/        # optional, milestone M8
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
- **`ResultStore`** (`results/store.py`): `start_run(manifest)`,
  `record(test_id, outcome, test_record, requirement_ids)`, `finish_run()`.
  SQLite is the only implementation in this slice.

## Order of work

Each milestone ends with passing tests and a commit. Do them in order.

### M0: Packaging skeleton (done)

- The package installs with `pip install -e .` and imports as `benchtare`.
- `pytest` and `ruff` run clean in CI (GitHub Actions).

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

### M5: Results and manifest

- Results schema with a schema version from day one (`PRAGMA user_version`
  plus a `schema_version` column). A database newer than the code is refused
  with a clear message. An older one goes through a migration list.
- Manifest: BenchTare version, Python version, a timestamp, and hashes of the
  interface description, its DBC files, the bench file, the harness file and
  each test file.
- SQLite store behind `ResultStore`, in `.benchtare/` by default and
  configurable.
- Tests:
  - two runs store two distinct manifests that read back correctly;
  - changing a test file changes its hash;
  - a newer-schema database is refused;
  - a failed test is stored as failed.

### M6: Test check

- A corrupting wrapper around the `bench` fixture, with two modes: `shift` and
  `stale`.
- `pytest --check-tests` and `benchtare check-tests` rerun each test once per
  mode. A test that still passes under every mode is reported as unable to
  fail. Read timeouts don't count.
- Store the results, and record in the manifest when each test file was last
  checked and whether it has changed since.
- Tests:
  - a test with a real assertion is reported as able to fail;
  - a test that asserts nothing, or ignores its reading, is reported as unable
    to fail;
  - a test that only times out isn't counted as able to fail.

### M7: Scaffold and acceptance

- `benchtare init <dir>` writes the packaged quickstart: an interface
  description, a bench file, a harness file, a `conftest.py` with a staging
  fixture, and one passing test, all on the reference.
- Write the quickstart section of the README.
- CI job:
  1. build the wheel;
  2. install it in a fresh virtual environment;
  3. run `benchtare init demo`, `cd demo`, `pytest`, which must pass;
  4. run `benchtare check-tests`, which must report the test as able to fail.
- **Acceptance check:** on a clean machine, `pip install benchtare`,
  `benchtare init demo`, `cd demo`, `pytest` gives a passing test in under 15
  minutes, for someone who knows pytest and has never seen the tool. The test
  check then shows that the test can fail.

### M8 (optional): Requirements and trace report

Only after M7 passes.

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
  run configurations (design brief, later stage 9). `benchtare init` (M7)
  writes the quickstart project only.
- Closed-loop or real-time simulation.

## Before the first release

- [x] Confirm `benchtare` is free on PyPI and on GitHub.
- [x] Create the GitHub repository.
- [ ] Publish the 0.0.1 placeholder to PyPI so that `pip install benchtare`
      resolves to this project.
- [ ] Spend an hour with jamb and Jumpstarter, to confirm the prior-art
      assessment from hands-on use.
