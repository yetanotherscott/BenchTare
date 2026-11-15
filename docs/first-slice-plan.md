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
│   ├── bench/
│   │   └── config.py        # bench file: equipment, backends, channels
│   ├── harness/
│   │   └── config.py        # harness file: connections, loopbacks, presence checks
│   ├── backends/
│   │   ├── base.py          # Backend protocol, receive buffer, read rule
│   │   ├── sim.py           # in-process simulated equipment
│   │   └── sim_can.py       # virtual CAN bus through python-can
│   ├── reference/
│   │   ├── device.py        # the simulated reference DUT
│   │   ├── reference.dut.yaml
│   │   ├── reference.dbc
│   │   ├── bench-sim.yaml   # simulated bench: virtual CAN plus simulated I/O
│   │   └── harness-sim.yaml
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
  `bool`, `enum` with values), unit (optional for `bool` and `enum`), direction
  (`input` or `output`, from the DUT's point of view). No range. `SignalSet`
  gives attribute access by name and enforces unique names.
- **Interface description** (`dut/description.py`): loads one YAML file per
  DUT into a `SignalSet`, combining inline signals, DBC imports (direction from
  sender and receiver nodes) and overrides. `standards:` is accepted as a key,
  but any value is rejected for now.
- **`Backend`** (`backends/base.py`): a `typing.Protocol` with `open()`,
  `close()`, `check()` (returns the problems found), the channels it offers and
  whether each can be driven or read, `write(signal, channel, value)` and
  `read(signal, channel, after, timeout)`. A bus backend uses the signal's
  encoding, and a plain I/O backend ignores it. A shared receive buffer
  implements the read rule using a bench-wide sequence counter, not wall-clock
  time.
- **`Bench`** (the `bench` fixture): routes each signal through the harness to
  its equipment and backend. It offers `write`, `read` and `wait_until`.
  `read` returns the first value received after the bench's most recent
  write.
- **`ResultStore`** (`results/store.py`): `start_run(manifest)`,
  `record(test_id, outcome, measurements, requirement_ids)`, `finish_run()`.
  SQLite is the only implementation in this slice.

## Order of work

Each milestone ends with passing tests and a commit. Do them in order.

### M0: Packaging skeleton (done)

- The package installs with `pip install -e .` and imports as `benchtare`.
- `pytest` and `ruff` run clean in CI (GitHub Actions).

### M1: Interface description and signals

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

### M2: Bench, harness, backends and the reference DUT

- Load the bench file and the harness file. Check the harness against the
  interface description and the bench file: every connection names a real
  signal and channel, inputs go to drivable channels, and outputs go to
  readable ones.
- `Backend` protocol, the receive buffer and the read rule.
- The `sim` and `sim_can` backends. `sim_can` gives each `open()` its own
  virtual bus name unless the bench file sets one, so two benches in one
  process never share a bus.
- The reference DUT, with its interface description, DBC, bench file and
  harness file. It boots, moves from standby to run on request, echoes the
  setpoint as the measured value after a fixed delay, and has `supply_ok`
  follow `supply_enable`. The simulated bench builds its wiring from the
  harness file. For tests only, it can cut a wire or fail an equipment item.
- Tests:
  - a stimulus produces the expected response on both backends;
  - a stale value is never returned, and a fast reply is never missed;
  - `wait_until` succeeds, and times out with a clear message;
  - a value too wide for its bits fails with a clear error;
  - two open `sim_can` benches can't hear each other;
  - a harness that connects a DUT input to a read-only channel is rejected.

### M3: pytest plugin

- Register the plugin entry point in `pyproject.toml`.
- Options `--dut`, `--bench` and `--harness`, plus the matching
  `benchtare_dut`, `benchtare_bench` and `benchtare_harness` ini settings,
  which also cover `[tool.pytest.ini_options]`.
- Fixtures: `bench` and `signals`. With nothing configured, the plugin does
  nothing, and asking for `bench` fails with a message saying what to set.
- Record every read as a measurement, along with any `record_property` values.
- Tests (pytester):
  - a staging fixture and the example test from [model.md](model.md) pass
    against the reference;
  - a staging fixture that fails is reported as an error, not a failure;
  - nothing happens when nothing is configured.
  - Tests that use `sim_can` run with `runpytest_subprocess`.

### M4: Self-test

- Run the bench check and the harness check (loopbacks and presence) at
  session start. If either fails, stop with `pytest.exit` and a message that
  says what failed and what to check. No tests run.
- `benchtare selftest` runs the self-test alone. Register the `benchtare`
  console script.
- Tests:
  - a healthy reference setup passes;
  - a failed equipment item, a cut loopback wire and a missing DUT are each
    caught;
  - no tests run after a self-test failure;
  - the CLI exit codes are right.

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
- A remote database.
- ReqIF, StrictDoc or Sphinx-Needs importers.
- Write-back to any requirements tool.
- Trend analysis and dashboards.
- Closed-loop or real-time simulation.

## Before the first release

- [x] Confirm `benchtare` is free on PyPI and on GitHub.
- [x] Create the GitHub repository.
- [ ] Publish the 0.0.1 placeholder to PyPI so that `pip install benchtare`
      resolves to this project.
- [ ] Spend an hour with jamb and Jumpstarter, to confirm the prior-art
      assessment from hands-on use.
