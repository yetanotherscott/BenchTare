# BenchTare First-Slice Plan

This is the build plan for the first release. It turns the "First slice"
section of the [design brief](design-brief.md) into a repository layout, a
module list and an order of work.

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
| Internal names: `tare` is the self-test, `plumb` is the reference bench, `assay` is the report | A consistent vocabulary | Low, before the first public release |
| Standalone project, not built on Jumpstarter | Keeps the first slice free of a large dependency | Medium |

## Repository layout

```
benchtare/
├── CLAUDE.md
├── LICENSE
├── README.md
├── pyproject.toml
├── docs/
│   ├── design-brief.md
│   └── first-slice-plan.md
├── src/benchtare/
│   ├── __init__.py
│   ├── signals/
│   │   ├── model.py        # Signal: name, unit, type, range, direction
│   │   ├── yaml_list.py    # load signals from a hand-written YAML list
│   │   └── dbc.py          # generate signals from a DBC file (cantools)
│   ├── bench/
│   │   └── config.py       # load the bench file: signal -> backend channel
│   ├── backends/
│   │   ├── base.py         # Backend protocol, defined by capability
│   │   └── sim_can.py      # virtual CAN bus through python-can
│   ├── tare/
│   │   └── selftest.py     # known stimulus -> known response, before each run
│   ├── plumb/
│   │   ├── device.py       # the simulated reference device
│   │   ├── plumb.dbc       # its interface description
│   │   └── bench.yaml      # its bench file
│   ├── results/
│   │   ├── schema.py       # the stable results schema, versioned
│   │   ├── manifest.py     # framework version, test and bench file hashes
│   │   └── store.py        # ResultStore interface + SQLite implementation
│   ├── requirements/       # optional, milestone M7
│   │   ├── doorstop.py     # read requirement IDs and text from a Doorstop tree
│   │   └── trace.py        # the trace ("assay") report
│   ├── plugin.py           # pytest plugin: fixtures, markers, hooks
│   └── cli.py              # `benchtare init`, `benchtare tare`, `benchtare report`
├── tests/                  # the framework's own tests, run against plumb
└── examples/quickstart/    # what `benchtare init` generates
```

## Key interfaces

Get these four right before writing implementations. Everything else hangs off
them, and later stages add implementations without changing them.

- **`Signal`** (`signals/model.py`): an immutable description of one named
  value with a unit, a type, a valid range and a direction (stimulus or
  response). It knows nothing about buses or channels.
- **`Backend`** (`backends/base.py`): a `typing.Protocol` with `write(signal,
  value)`, `read(signal, timeout)`, `open()` and `close()`, plus a declared set
  of capabilities. Simulation and hardware both implement it.
- **`ResultStore`** (`results/store.py`): `start_run(manifest)`,
  `record(test_id, outcome, measurements, requirement_ids)`, `finish_run()`.
  SQLite is the only implementation in this slice.
- **Bench file** (`bench/config.py`): a YAML file that names the backend and
  maps each signal to a channel on it. This file, and the interface
  description, are the only things a team writes besides tests.

## Order of work

Each milestone ends with passing tests and a commit. Do them in order.

### M0: Packaging skeleton

- The package installs with `pip install -e .` and imports as `benchtare`.
- `pytest` and `ruff` run clean in CI (GitHub Actions) on an empty test suite.
- Already in place: `pyproject.toml`, `LICENSE`, `README.md`,
  `src/benchtare/__init__.py`.

### M1: Signals

- `Signal` model with validation (unit present, range sane, name unique).
- Load a signal list from YAML.
- Generate signals from a DBC file with cantools. Add `cantools` and `PyYAML`
  as dependencies.
- Tests: round-trip a small YAML list and a small DBC file.

### M2: Backend and reference device

- `Backend` protocol.
- `sim_can` backend on a python-can virtual bus. Add `python-can` as a
  dependency.
- `plumb`: a simulated device with a handful of signals and simple, documented
  behaviour (for example, a commanded setpoint echoed back as a measured value
  after a fixed delay). Ship its DBC and bench file inside the package.
- Tests: write a stimulus, read the expected response, through the protocol
  only.

### M3: pytest plugin

- Register the plugin entry point in `pyproject.toml`.
- Options: `--bench <file>` and an equivalent `pyproject.toml`/ini setting.
- Fixtures: `bench` (the opened backend) and `signals` (attribute access by
  signal name).
- A test should read like this, with no BenchTare imports:

  ```python
  def test_setpoint_is_tracked(bench, signals):
      bench.write(signals.setpoint, 42.0)
      assert bench.read(signals.measured, timeout=1.0) == pytest.approx(42.0, abs=0.5)
  ```

- Tests: run a sample test module against plumb with `pytester`.

### M4: Tare (the bench self-test)

- A self-test definition in the bench file: a list of stimulus/expected-response
  pairs with tolerances.
- The plugin runs it at session start. If it fails, the session aborts with a
  clear message and no tests run.
- `benchtare tare` runs it alone from the command line.
- Tests: a healthy plumb passes; a plumb with an injected fault is caught.

### M5: Results and manifest

- Results schema, with a schema version field from day one.
- Manifest: BenchTare version, Python version, hashes of the bench file, the
  interface description and each test file, and a timestamp.
- SQLite store behind `ResultStore`. Default location is a `.benchtare/`
  directory in the project; make it configurable.
- The plugin records every test outcome and any measurements a test reports.
- Tests: run twice, confirm two runs with distinct manifests are stored and can
  be read back.

### M6: Scaffold and acceptance

- `benchtare init` writes `examples/quickstart` into a new directory: an
  interface description, a bench file pointing at plumb, and one passing test.
- Register the `benchtare` console script.
- Fault injection for the acceptance check: an option that makes plumb
  misbehave in a named way, so the quickstart test visibly fails.
- Write the quickstart section of the README.
- **Acceptance check:** on a clean machine, `pip install benchtare`,
  `benchtare init demo`, `cd demo`, `pytest` gives a passing test in under 15
  minutes for someone who knows pytest and has never seen the tool. Running
  again with the injected fault makes the test fail.

### M7 (optional): Requirements and trace report

Only after M6 passes.

- A `requirement` marker: `@pytest.mark.requirement("SRS-001")`. Check the
  convention used by jamb first and match it if practical.
- Read IDs and text from a Doorstop tree. Warn on a marker that names an
  unknown ID.
- Store requirement IDs with each result.
- `benchtare report`: an HTML and Markdown report listing each requirement with
  its tests and latest outcome, requirements with no test, and tests with no
  requirement.

## Out of scope for this slice

Do not build these yet, even where the design brief describes them:

- Real hardware backends and DAQ devices.
- Transports other than virtual CAN (serial, sockets, MQTT).
- Protocols above raw CAN signals (CANopen, Modbus, UDS, J1939, ISOBUS).
- A remote database.
- ReqIF, StrictDoc or Sphinx-Needs importers.
- Write-back to any requirements tool.
- Trend analysis and dashboards.
- Closed-loop or real-time simulation.

## Before the first public commit

- [ ] Confirm `benchtare` is free on PyPI and on GitHub.
- [ ] Create the GitHub repository.
- [ ] Publish the 0.0.1 placeholder to PyPI so that `pip install benchtare`
      resolves to this project.
- [ ] Spend an hour with jamb and Jumpstarter, to confirm the prior-art
      assessment from hands-on use.
