# The BenchTare Model

This document defines what BenchTare models and what each file a team writes is
for. Code, docs and error messages use these terms. When a design question
comes up, this is the reference.

## The parts of a system test

```
  TESTS             SIGNALS                BENCH                HARNESS              DUT
  plain pytest  ->  named values on   ->   equipment that  ===  what connects  ===  the device
  + staging         the DUT                drives and reads     the bench to         under test,
  fixtures                                 signals              the DUT              real or simulated

  test files,       interface              bench file           harness file
  conftest.py       description
```

| Part | What it is | Described by |
| --- | --- | --- |
| **DUT** | The device under test, real or simulated | Interface description |
| **Signal** | A named value on the DUT | Interface description |
| **Bench** | The equipment that drives and reads signals: a CAN interface, a DAQ, relays, a power supply, or a simulation of them | Bench file |
| **Harness** | What connects the bench to the DUT: which signal is on which bench channel | Harness file |
| **Staging** | Getting the DUT into the state a test needs: powered, booted, in the right mode | pytest fixtures |
| **Tests** | Stimuli written to signals, and assertions on what is read back | pytest test files |

The split matters because each part changes on its own:

- The same tests run on any bench and harness that can reach the DUT's signals:
  a simulation on a laptop, a rack in the lab, a rack somewhere else.
- One bench is reused across many DUTs. Only the harness file changes.
- A new revision of the DUT changes its interface description, not the bench.

## Signals

A signal has four properties:

| Property | Meaning |
| --- | --- |
| Name | Unique within the DUT. Tests refer to signals by name |
| Type | `float`, `int`, `bool` or `enum` (with its list of values) |
| Unit | For example `V`, `degC`, `rpm`. Required for `float` and `int`: write `none` for a numeric signal that has no unit, such as a count. Optional for `bool` and `enum` |
| Direction | From the DUT's point of view: `input` (the bench drives it) or `output` (the bench reads it) |

A signal has no valid range. What counts as a correct value is decided by a
test's assertions, not by the signal. BenchTare never refuses to send a value
because of what it is: testing how a DUT handles a bad input means sending the
bad input. A write fails only if the channel physically cannot carry the value,
for example a value too wide for its bits in a CAN frame.

A signal knows nothing about buses or channels. That's the harness's job.

## The interface description

One YAML file per DUT lists every signal the DUT exposes. Signals can be listed
directly, imported from DBC files, or both.

```yaml
# reference.dut.yaml
dut: reference

can:
  node: REFDEV            # the DUT's node name in the DBC files
  dbc: [reference.dbc]    # signals in these files become DUT signals

signals:                  # signals listed directly
  supply_enable: {type: bool, direction: input}
  supply_ok: {type: bool, direction: output}

overrides:                # adjust imported signals
  Setpoint: {unit: degC}

identity:                 # what each run records about the DUT: see below
  hardware_revision:
  bootloader: {signal: BootloaderVersion}

standards: []             # reserved: see below
```

For an imported DBC signal, the direction comes from the DBC: a signal in a
message the DUT's node sends is an `output`, and one the DUT's node receives is
an `input`. Signals the DUT's node neither sends nor receives are not imported,
so a DBC for a whole bus can be used as it is.

An `overrides` entry can change an imported signal's `direction` or `unit`, or
give it a new `name`. It names the signal by its DBC name, or as
`Message.Signal` when the same name appears in more than one message. A
numeric signal with no unit in the DBC needs one set here, either a real unit
or `none`.

Leaving a numeric unit out is an error, so a forgotten unit is caught.
Writing `unit: none` says on purpose that the signal has no unit.

Signal names must be unique across all sources, and the error for a clash
names both places it was defined. Names must be valid Python identifiers,
because tests write `signals.<name>`.

**Identity.** `identity:` lists what each run should record about the DUT:
its hardware revision and software versions, such as bootloader, application
and subsystem versions. It names the items and never holds their values, so
the file doesn't change when the DUT's software does. An item that names a DUT
output is read before the first test. Any other value comes from the team's
code (`bench.identify("hardware_revision", "C")`) or the command line
(`--identify hardware_revision=C`), at any point in the run. The list is a
minimum: any other name can be recorded during the run too, such as the
versions of subsystems the DUT reports. A listed item that is never collected
is recorded as missing, with a `benchtare.DutIdentityWarning`, and so is a
value that changes during the run. Tests can read the values collected so far
from `bench.identity`. The values are stored with the run's results; see
[results-schema.md](results-schema.md). The reference DUT lists one item,
`software`, read from its `SwVersion` signal.

**Standards.** A DUT may support standard protocols, such as J1939, that require
a defined set of messages. The `standards:` key will declare these, and
BenchTare will bring in that standard's signals. No standard is available in the
first slice, so any value is rejected with "not available in this version".
Before a standard is added, its source needs checking: some are freely
published, and some (such as SAE's J1939 definitions) are copyrighted.

## The bench file

The bench file lists the equipment. Each item has a backend (the code that
drives that kind of equipment) and channels. A channel can be driven, read, or
both. A bus such as CAN is a single channel. The interface description says
where each signal sits in the frames.

```yaml
# bench-sim.yaml
bench: laptop-sim

equipment:
  can0:
    backend: sim_can      # a virtual CAN bus: one channel, the bus itself
  io:
    backend: sim          # simulated I/O
    channels:
      out1: {drive: true}
      out2: {drive: true}
      out3: {drive: true}
      out4: {drive: true}
      in1: {read: true}
      in2: {read: true}
      in3: {read: true}
      in4: {read: true}
```

A bench file describes equipment only. It names no DUT signals, so it can be
reused for any DUT.

The first slice has two backends, both simulated. Hardware backends come later
behind the same interface.

- **`sim`** is simulated I/O with named channels. Its read channels are sampled
  continuously, like a DAQ.
- **`sim_can`** is a virtual CAN bus. Each bench gets a bus of its own, so two
  benches in one process never hear each other. Like a real interface, it
  carries CAN FD frames only with `fd: true`. It ignores python-can's own
  configuration (`~/.canrc`, `can.ini`, `CAN_*` environment variables), so a
  setting on one machine can't change how the simulation behaves.

The CAN details that matter:

- **Message lookup.** Messages are matched by name, and received frames by ID
  and frame type. A standard and an extended frame with the same ID number
  never mix.
- **CAN FD.** A CAN FD message is one the DBC marks as FD, or one longer than
  8 bytes. The harness file refuses to put one on a bus that isn't in FD mode.
- **Multiplexing.** Signals in multiplexed messages can be read. They can't be
  written on a bus yet, because a frame has to carry the right multiplexer
  value. The harness file refuses a multiplexed DUT input on a bus, rather than
  letting a write silently leave it out.

## The harness file

The harness file connects the bench to the DUT. It says which bench channel
each signal is on, which loopbacks the harness provides, and how to tell that
the DUT is present.

```yaml
# harness-can.yaml
harness: reference-can

connections:              # signal: equipment[.channel]
  Setpoint: can0
  ModeRequest: can0
  Measured: can0
  Mode: can0
  supply_enable: io.out1
  supply_ok: io.in1

loopbacks:                # a bench output wired straight back to a bench input
  - {drive: io.out4, read: io.in4}

presence:                 # how to tell the DUT is connected and alive
  - {bus: can0, within: 1.0}        # any DUT traffic on the bus
  - {signal: Mode, within: 2.0}     # this signal is received

simulate: reference       # only for simulated setups: the DUT model to run
```

Every signal used by a test needs a connection. A DUT `input` must be connected
to a channel the bench can drive, and a DUT `output` to one it can read. Each
channel carries one signal or one loopback end, while a bus carries many. Only
signals imported from a DBC file can go on a CAN bus. A DBC signal can still
be wired to a plain channel: the DBC says how it is encoded on CAN, and the
harness says where it goes. The harness file is checked against both the
interface description and the bench file when it loads, and an error names
the file, the signal and the channel.

The reference ships two harnesses for the same DUT and bench:
`harness-can.yaml` (above), and `harness-io.yaml`, which puts every signal on a
simulated I/O channel and uses no CAN at all.

With real hardware the harness is a physical thing and the file describes it.
With a simulated bench, the simulation builds its wiring from the harness file
and runs the DUT model named by `simulate:` (see "DUT models" below).

## When a file is wrong

The interface description, the bench file and the harness file are all checked
when they load. Every problem in a file is reported together, with the file,
the signal or entry, and what to check. A wrong file never produces a traceback,
and it is never silently accepted. Some of the checks go beyond plain YAML:

- **A key given twice in the same section** is an error, with both line
  numbers. Plain YAML would silently keep the second one, so one copy-paste
  slip could rewire a signal. `<<: *anchor` merges may still be overridden.
- **A file that isn't UTF-8 text** is reported with the line of the first bad
  byte.
- **The wrong kind of file** is named. For example, a harness file given as the
  bench file, or a DBC file given where a YAML file belongs.
- **Values YAML reads unexpectedly are explained.** Unquoted `on`, `off`, `yes`,
  `no`, `true`, `false` and `null` become values, not names. The error says to
  quote them.
- **Near misses get a suggestion**, such as `Float` for `float`, `in` for
  `input`, or `can1` when the bench has `can0`.

## Staging

Most tests need the DUT in a particular state first. Staging is done with
ordinary pytest fixtures in the team's `conftest.py`:

```python
# conftest.py
import pytest


@pytest.fixture
def running(bench, signals):
    """The DUT has booted and is in run mode."""
    bench.read(signals.Mode, timeout=2.0)  # it has booted once it reports a mode
    bench.write(signals.ModeRequest, "run")
    bench.wait_until(signals.Mode, "run", timeout=1.0)
```

If a staging fixture fails, pytest reports an **error**, not a test failure. A
DUT that never got ready is not counted against the test that needed it.

**Staging starts from whatever state the DUT is in.** The bench is opened once
per test run, and so is the DUT, real or simulated. The DUT keeps its state
from one test to the next. A staging fixture must reach the state its test needs
from any starting point. That's why the example waits for *a* mode report, not
for `standby`: after a test that left the DUT running, it would never see
`standby` again.

## Tests

Tests are plain pytest. They take the `bench` and `signals` fixtures, plus any
staging fixtures, and need no BenchTare imports.

```python
# test_setpoint.py
import pytest


def test_setpoint_is_tracked(running, bench, signals):
    bench.write(signals.Setpoint, 42.0)
    bench.wait_until(signals.Measured, pytest.approx(42.0, abs=0.5), timeout=1.0)
```

The `bench` fixture provides:

| Call | What it does |
| --- | --- |
| `bench.write(signal, value)` | Drive a DUT input |
| `bench.wait_until(signal, expected, timeout)` | Wait for a value of the signal, received after the bench's most recent write, that equals `expected` (`pytest.approx` works), or for which `expected` returns true if it is a function. Returns that value. **Use this to check how the DUT reacts** |
| `bench.read(signal, timeout)` | Return the first value of the signal received after the bench's most recent write, waiting up to `timeout` seconds. A value from before the write is never returned, and a fast reply is never missed |

Each `read` and `wait_until` in a test is one of its verification steps, and the
test check (below) proves each one can fail. `check=False` on either marks a
reading the test deliberately doesn't check, such as a measurement kept only for
trends: `bench.read(signals.SupplyVoltage, timeout=1.0, check=False)`.

**Why responses use `wait_until`.** The first value after a write is often
sent before the DUT has reacted. This happens when the DUT reports an output
cyclically, when a DAQ samples a channel continuously, or when a CAN frame
carries several signals and the DUT answers each frame. `read` is right for a
state that has already settled, or a protocol where each request gets exactly
one answer. To check a reaction, wait for it.

**When the DUT answers wrongly, the test fails; when nothing arrives, it's a
timeout.** These are reported differently, because they point at different
things:

- **Values arrived, but none was what `wait_until` expected in time.** The DUT
  answered with the wrong value, or later than the timeout, and the test fails
  like a failed `assert` (`ComparisonFailed`): "Measured did not reach 99.0 ±
  0.5 within 0.3 s (on can0): 29 values arrived after the last write, the last
  42.0 degC". Set each timeout from the requirement, and it is the test's
  timing check: a response after it fails the test, whatever the reason.
- **Nothing arrived at all** (`ReadTimeout`). The error names the signal, its
  channel and the timeout, gives the last value received and how long ago, and
  says what to check: wiring, the bus, whether the DUT is running. If the
  equipment reports a fault, the error says that instead.

Writing a DUT output, or reading a DUT input, is refused. Tests drive inputs and
read outputs.

**What can be written.** A value must be of the signal's kind. That is checked
before any equipment sees the value, so a simulated channel refuses exactly
what real hardware would.

| Signal type | Takes |
| --- | --- |
| `float` | Any number. NaN and infinity are passed on, and the channel decides whether it can carry them |
| `int` | A whole number (`2` or `2.0`, not `2.5`) |
| `bool` | `True`, `False`, `0` or `1` |
| `enum` | One of its value names, or a raw whole number to send a code the DUT doesn't define |

A DBC may give several codes the same name, such as `Reserved`. The signal
lists the name once. Writing an ambiguous name is refused with the codes it
could mean, and the raw code works.

**Timeouts** are a number of seconds, 0 or more. There is no "wait forever", so
a missing DUT can't hang a run. A timeout of 0 looks only at what has already
arrived.

**DUT faults are stimuli.** To test how the DUT handles a fault, the test writes
the input that represents it: an implausible sensor value, a mode request the
DUT should refuse. BenchTare does not model faults specific to any DUT.
Standard faults for standard interfaces, such as dropping or stopping a CAN
message, may be added later as stimuli the bench can produce.

Everything a test does at the bench, including what its staging fixtures do,
goes into its test record. See "The test record" below.

## Running the tests

pytest needs the three files: the interface description, the bench file and the
harness file. Give them on the command line:

```
pytest --dut reference.dut.yaml --bench bench-sim.yaml --harness harness-can.yaml
```

or in the ini file, so a plain `pytest` works:

```toml
# pyproject.toml
[tool.pytest.ini_options]
benchtare_dut = "reference.dut.yaml"
benchtare_bench = "bench-sim.yaml"
benchtare_harness = "harness-can.yaml"
```

- **Paths.** Command-line paths are relative to where pytest is run, and ini
  paths to the ini file. The command line wins over the ini file.
- **Header.** The run's header names the DUT, bench and harness it used, the
  test record level, and where the run's results are stored.
- **Checked at the start.** The files are loaded and checked when pytest
  starts. A wrong file stops the run with its problems, before any test runs.
- **Opened when needed.** The bench is opened the first time a test asks for
  `bench`, and closed when the run ends. Tests that only use `signals` don't
  open the equipment.
- **All three or none.** Setting only some of the files is an error. Setting
  none leaves BenchTare out of the run entirely, so installing it never
  changes another project's tests.
- **Clean failures.** When a check fails, the failure points at the test's own
  line. BenchTare's internals are left out of the traceback.
- **Results.** Every run is stored in the results database (see "Results and
  the manifest"). The header says where, and gives the run's ID.

## The test record

Each test gets a record of what happened at the bench while it ran. It is
evidence about the DUT, kept with the test's result. It is separate from
BenchTare's own log (below), and each has its own verbosity.

| Level | What the record keeps |
| --- | --- |
| `results` | Every value read; every comparison (`wait_until`, with what it expected and what it got, and passing `assert` statements, see below); every timeout; each phase's outcome |
| `stimuli` | Also every value written. This is the default |
| `debug` | Also every CAN frame the bench sent, and every frame it received for a connected signal |

Choose the level with `--bench-record=LEVEL`, or `benchtare_record` in the ini
file.

- **Unexpected events are recorded at every level.** A frame from the DUT that
  doesn't match the DBC, and equipment that stops working during a test, are
  always recorded, so no setting hides them. Every bad frame is recorded, not
  just the first.
- **They are also pytest warnings.** A test during which they happened gets a
  `benchtare.FrameMismatchWarning` or `benchtare.EquipmentWarning`, listed in
  the run's warnings summary. The test's outcome doesn't change, because pass
  criteria live in the tests. To make such a test fail, run pytest with
  `-W error::benchtare.FrameMismatchWarning`. Bad frames that arrive between
  tests count against the next test. Any that arrive after the last test are
  printed at the end of the run.
- **Only connected messages are checked.** The bench decodes the messages that
  carry a signal the harness connects. A frame for any other message isn't
  checked against the DBC.
- **A failed test shows its record**, phase by phase, after the failure. Add
  `-rP` to see the records of passing tests too.

```
---------------------------- BenchTare record setup ----------------------------
10:38:35.195 read Mode = 'standby' on can0 after 0.100 s
10:38:35.195 write ModeRequest = 'run' on can0
10:38:35.205 wait_until Mode, expecting 'run': got 'run' on can0 after 0.010 s
10:38:35.205 setup passed
---------------------------- BenchTare record call -----------------------------
10:38:35.206 write Setpoint = 42.0 degC on can0
10:38:35.506 wait_until Measured, expecting 99.0 ± 0.5: not reached within 0.3 s on can0; 29 values arrived, the last 42.0 degC
10:38:35.531 call failed: benchtare.backends.base.ComparisonFailed: Measured did not reach 99.0 ± 0.5 within 0.3 s (on can0): 29 values arrived after the last write, the last 42.0 degC
```

- **Each report carries its phase's record** as `report.benchtare_record`: a
  list of plain dicts with the kind of entry, its level, the time, the text, and
  the facts (signal, value, unit, channel, and so on). M5 stores them with the
  results.
- **Passing asserts** are recorded when the ini file sets pytest's
  `enable_assertion_pass_hook = true`. Without it, the record has the values a
  test read and its outcome, but not each comparison a plain `assert` made.
  pytest caches rewritten test files, so clear `__pycache__` after changing the
  setting.
- Values a test adds with pytest's `record_property` stay in
  `report.user_properties`.

## BenchTare's own log

BenchTare also logs with Python's standard `logging`, under the `benchtare`
logger. That log is for debugging BenchTare itself: loading files, opening and
closing equipment, receive threads. pytest's own options set its verbosity:
`--log-level`, `--log-cli-level=INFO` to watch it live, or `log_file` with
`log_file_level = DEBUG` to keep it. BenchTare doesn't turn it up.

| Level | What is logged |
| --- | --- |
| `DEBUG` | Each CAN frame a backend sends; virtual buses opened and closed; the DBC signals an import skipped; frames that don't match the DBC, at most once a second per frame ID |
| `INFO` | Each file loaded, with what it holds and what each DBC import took; the bench opening and closing; the simulated DUT starting and stopping; simulated failures and cut wires |
| `WARNING` | Unexpected events when nothing is keeping a test record (a script using `open_bench` without pytest); a frame the simulated DUT couldn't decode |
| `ERROR` | Equipment that stopped working in the background; an action of the simulated DUT that failed, with its traceback |

- **Errors are raised, not logged.** A wrong file, a write the channel can't
  carry and a read that times out are exceptions that say what failed and what
  to check.
- **Failures in the background are logged and reported.** Equipment that stops
  working between reads (a CAN bus that stops receiving, a sampler that stops)
  is logged when it happens, goes into the test record, and its `check()`
  reports it from then on. So the next read that times out names the real
  cause, such as `equipment 'can0' reports: stopped receiving: ...`, instead
  of suggesting the DUT is unplugged.
- **A frame that doesn't match the DBC is skipped**, and the bus keeps
  receiving. One bad frame never stops the bench from reading the rest.

## What BenchTare verifies

A test's pass or fail is only worth something if everything under it works.
BenchTare verifies five areas, each a different question with its own check:

| | Area | The question | Verified by | When |
| --- | --- | --- | --- | --- |
| 1 | **Bench** | Is every equipment item working? | The bench check (self-test) | Before every session |
| 2 | **Harness** | Is everything wired as the harness file says, and is the DUT there? | The harness check (self-test) | Before every session |
| 3 | **Test hygiene** | Do setup and teardown leave the DUT in a known state, so each test stands on its own? | The hygiene check | On demand |
| 4 | **Test logic** | Can every check in each test fail? | The test check | On demand |
| 5 | **The DUT** | Does the DUT do what its requirements say? | The team's tests | Every run |

- **Each area is verified on its own,** by its own check, with its own verdict,
  its own report and its own stored results. A check fails only for its own
  area: the harness check never fails because of broken equipment, and the test
  check never fails because a test doesn't repeat.
- **They build on each other, in that order.** A failure in a lower area makes
  the areas above it "not checked, because of" that area, never a verdict of
  their own. A failed bench check leaves the harness checks on that equipment
  not checked, and no test runs; a test that doesn't repeat gets no test-logic
  verdict.
- **The DUT is the area of interest; the other four are there so that its
  results can be trusted.** When this session's bench and harness checks
  passed, and each test's hygiene and logic have been checked and its file
  hasn't changed since, a pass or a fail reflects the DUT, not the setup around
  it. A usual run says which of these is missing.

### Bench and harness: the self-test (before every session)

The self-test runs once the tests are collected, before the first one. It is
two checks with two verdicts: the bench check, then the harness check. If
either fails, the session stops, no tests run, and the output says which check
failed, what failed in it and what to check. This catches a disconnected cable,
a missing DUT, or a DUT that never booted, before those turn into confusing
test failures.

| Check | Passes when |
| --- | --- |
| Bench: each equipment item | It opened, and reports itself healthy |
| Bench: the simulated DUT, if any | It is running, and none of its own actions failed |
| Harness: each loopback | Driving `True`, then `False`, each reads back within 1 s. A cut wire and a line stuck at either level both fail |
| Harness: each presence check on a bus | Any frame arrives on the bus within its `within:` seconds |
| Harness: each presence check on a signal | Any value of the signal arrives within its `within:` seconds |

A harness check on equipment that failed the bench check is not checked rather
than failed: a broken item says nothing about the wiring.

- **Only when the bench is needed.** The self-test runs, and the bench opens,
  only if a collected test uses the `bench` fixture. Tests that only use
  `signals` can still run without the equipment. `--collect-only` runs neither.
- **What it shows.** A passing self-test shows two lines before the first test,
  one per check; `-v` lists every check. A failing one lists every check of
  both, with what to check for each failure, and the run stops with exit code 2
  (pytest's "interrupted"):

```
========================= BenchTare bench check passed =========================
PASSED  equipment can0: sim_can, healthy
PASSED  equipment io: sim, healthy
PASSED  simulated DUT reference: running
======================== BenchTare harness check failed ========================
FAILED  loopback io.out4 -> io.in4: drove True on io.out4, but io.in4 didn't read it back within 1 s; it read nothing
        Check: the loopback wire from io.out4 to io.in4 (harness file harness-can.yaml)
PASSED  DUT traffic on can0: frames received
PASSED  DUT sends Mode: Mode = 'standby' on can0
```

- **Unexpected events during the self-test**, such as a frame that doesn't
  match the DBC, are warned about like those during a test, but don't fail it:
  the self-test checks the bench and harness, not the DUT's behaviour.

**`benchtare selftest`** runs the self-test on its own, without running any
test. It runs through pytest (`pytest --selftest-only`), so it finds the three
files exactly as a test run does: from `--dut`, `--bench` and `--harness`, or
from the ini file. Other options go to pytest. It lists every check, and exits
0 when the self-test passed, 1 when it failed, 2 when its results couldn't be
stored, and 4 when it couldn't start (BenchTare isn't configured, or a file is
wrong).

### Test hygiene: the hygiene check (on demand)

A test stands on its own when its staging puts the DUT into a known state and
its teardown leaves it clean. What "clean" means is the team's to say: the
interface description's `clean_state:` lists what shows it, by name, with the
clean value where the team knows one:

```yaml
clean_state:
  Mode: standby                 # its clean value
  supply_enable: false
  Measured: {clean: 0.0, tolerance: 0.1, settle: 2.0}
  boot_count:                   # no clean value: compared before and after
  fault_log: {probe: true}      # state no signal shows
```

Only these items are compared, so nothing is tested beyond what matters.

- **A clean value**, where given, is what the item must be after teardown. It is
  the team's definition of clean, not a judgement of the DUT's behaviour.
  Without one, the item must be back where it was before setup, which has two
  limits: an item with no known value before setup (an input the session
  hasn't written yet) can't be judged, and the repeat run proves nothing for
  it, since it starts from the test's own end state. Give inputs a clean value:
  the bench always knows what it wrote.
- **`tolerance:`** says how far apart two numbers may be and still be the same,
  for a noisy reading. **`settle:`** gives an output or a probe time to come
  back after teardown (default 1 s).
- **An output's** value is the latest one received; **an input's** is the one
  the bench last wrote, so an input never written has no known value: write it
  in staging, so every test starts from a known state.
- **State probes** cover state no signal shows: a file on an embedded Linux
  target, a parameter in NVM, a stored fault code. The team writes a
  `pytest_benchtare_state(bench, config)` hook in `conftest.py` that returns
  named values, reached however it likes (SSH, a serial console, UDS);
  BenchTare calls it before setup and after teardown, and compares the names
  listed with `probe: true`.

**The three rules,** each with its own verdict line per test:

| Rule | Passes when |
| --- | --- |
| It repeats | Run again straight after itself, it passes and takes the same readings in the same order |
| It leaves the DUT clean | After each run's teardown, every `clean_state:` item is at its clean value, or back where it was before setup |
| It starts from a meaningful state | No `wait_until` in its body was already satisfied by the value from before the test's most recent write, in either run |

**Only the test that left a mess is blamed.** A test that finds an item dirty,
because an earlier test left it so, and doesn't change it, isn't counted
against: "Mode was already 'run' before setup, not its clean value 'standby':
an earlier test left it so". A test whose staging or teardown puts it back is
clean. The test that left it is the one reported: "Mode was still 'run' 1 s
after teardown, not its clean value 'standby' (it was 'standby' before setup)".

The third rule judges only waits, because a wait says the test expects the DUT
to react; a `read` after writing an unrelated signal says nothing. It looks at
the repeat run too, where the test starts from its own end state: a staging
that doesn't set a starting point of its own shows up there.

`benchtare check-hygiene` (or `pytest --check-hygiene`) runs the tests as
usual, then runs each one that passed again straight after itself. A test that
isn't clean makes the exit code 1; one that didn't pass to begin with isn't
checked. The report lists what needs attention; `-v` shows every rule and every
item compared. Its results are stored, and a usual run ends with a line naming
any test file that was never hygiene-checked, or has changed since.

The hygiene check and the test check are separate runs, so each area's verdict
stands on its own. Meanwhile, the test check still notices a test that doesn't
repeat, and reports its logic as not checked, because of test hygiene.

### Test logic: the test check (on demand)

A test that passes whatever the DUT does proves nothing, and neither does a
test that fails at its first check but would pass whatever its later checks
read. The test check proves that each test can fail at **every** verification
step. It corrupts what the test reads, not the DUT, so it works for any DUT,
and it needs no ranges: any clearly wrong value proves a check is doing
something.

| Mode | The DUT fault it stands for | What the corrupted reading gives the test |
| --- | --- | --- |
| `shift` | A wrong value | A number moved by its own size (at least 1), a bool inverted, an enum's next state |
| `stale` | The DUT ignored the command | The value from before the bench's most recent write |

**How it walks through a test.** The tests run as usual. Then each test that
passed runs again:

1. **The control,** with nothing corrupted. It must pass and take the same
   readings in the same order.
2. **One rerun per reading and mode,** with only that reading corrupted. Every
   earlier step reads real values, so the test gets as far as the corrupted
   step, and the check sees whether that step's check catches it.

A test with *n* readings runs 2 + 2*n* times. Staging (setup) and teardown
always read the real values, so the DUT is put into the same state each time.

**Every failure counted is a real failure.** A rerun's failure counts as proof
only when it is the corrupted reading's doing: it happened at that step, or
after it ("failed later, at test_x.py:14, after reading 3"), and the reading
really was changed.

**A test that doesn't repeat isn't judged.** If the control fails or takes
other readings, staging fails on a rerun, or a rerun fails before its
corrupted step or with nothing corrupted, the test doesn't repeat: that is a
test hygiene problem, and none of its reruns can be trusted. Its logic is
"not checked, because of test hygiene", with what happened, and the walk stops
there. A rerun that ends because nothing arrived (a `ReadTimeout` fails every
test, good or bad) is inconclusive.

**Verdicts.** A reading can fail if some mode makes the test fail there. A
test:

- **can fail** only if every checked reading can. A mode a reading still passes
  is listed, because it names a fault that step would miss: "shift: passed,
  reading Measured 84.0 degC instead of the 42.0 degC that arrived" means its
  check accepts a clearly wrong value.
- **can't fail** if any reading can't: "UNABLE TO FAIL at 1 of 3 readings",
  with the step's line. The run's exit code is 1, even though every test
  passed. This is the only verdict that fails the test check, since it is the
  only one about test logic.
- **is inconclusive** if a reading's reruns proved nothing.
- **isn't checked** if it didn't pass to begin with, doesn't use the bench,
  takes no checked reading in its body, or doesn't repeat (test hygiene).

**When a mode can't change a reading,** it is "not applied", with why: a value
that can't be shifted, no value before a write, or, for `stale`, the value
before the write being the same as what arrived ("it was already 42.0 degC
before the write, so stale can't change this step"). When the write was meant
to change that signal, the staging left the DUT where the test expects it: a
hygiene question, which the hygiene check will ask.

The report lists only what needs attention; `-v` shows every step, the control
and where each rerun failed.

The test check takes several times as long as a usual run, so it runs on demand
(`benchtare check-tests` or `pytest --check-tests`), not every session. Its
verdicts, every rerun's result and every rerun's record are stored. Each run's
manifest records when each test file was last checked and whether it has
changed since, and a usual run ends with a line naming any test file that was
never checked or has changed since its last check.

Ranges are not needed here, and BenchTare doesn't know them. Boundary values
(above, below, at the limits of a range) are stimuli for tests the engineer
writes from the requirement; the test check then proves each one can fail.
Ranges from protocol standards, and helpers that generate boundary values, are
on the roadmap (design brief, later stages 7 and 13).

### The DUT: the team's tests

This is the area of interest. Tests are plain pytest, run as usual, and their
results are stored. A usual run's summary says which of the four areas
beneath it are covered: the self-test of this session, and which test files
have never had their logic checked, or have changed since.

## Results and the manifest

Every session is stored in a local SQLite database, using a versioned schema.
Each run stores:

- each test's outcome, durations, failure report and test record;
- the self-test, and the record of everything before the first test;
- a **manifest**: the BenchTare and pytest versions, the time, the project's git
  commit, and hashes of the interface description, its DBC files, the bench
  file, the harness file, each test file, each `conftest.py` and the ini file;
- the bench equipment, as each backend describes it: vendor, model, serial
  number, firmware and driver, as far as it can tell;
- the DUT's identity (see "Identity" above);
- the machine and software it ran on: host, OS, Python build and every
  installed package.

| Option | Ini setting | What it does |
| --- | --- | --- |
| `--results PATH` | `benchtare_results` | Where the database is. Default `.benchtare/results.sqlite3` under pytest's rootdir. A relative `--results` is from where pytest runs, and a relative `benchtare_results` from the ini file's folder. Missing folders are made |
| `--no-results` | | Don't store this run, for throwaway runs while writing tests |
| `--identify NAME=VALUE` | | Part of the DUT's identity, such as `hardware_revision=C`; repeatable |

A run is written as it goes, so a run that crashed still has every test that
finished, and is marked `running` (a power cut can lose the last moments, but
never damages the file). A later run only adds rows: it never changes
or removes an earlier run's. Two runs can share one file at the same time. A
database written by a newer BenchTare is refused, and nothing is written to it.
An older one is backed up and upgraded. A file that isn't a BenchTare database
is refused and left as it was. Runs that only collect tests (`--collect-only`)
aren't stored. Keep the database on a local disk, not a network drive: SQLite
can't safely share a file between machines.

If the database can't be written during a run (another program holds it for
more than five seconds, or the disk is full), the run stops with a message
saying which file, what was being stored and what to check, and exits with
code 2. A run whose results aren't kept doesn't pass, so this happens even
when every test passed.

The manifest makes a run reproducible. It shows which DUT description, bench
and harness a result came from, and whether anything has changed since. Each
run also records the machine and software it ran on, to help track down
failures that only happen on one machine or with one package version. The
tables are set out in [results-schema.md](results-schema.md).

## The reference

BenchTare ships a simulated **reference** DUT inside the package, in
`benchtare/reference/`, along with these files:

- its interface description, `reference.dut.yaml`;
- `reference.dbc`;
- one bench file, `bench-sim.yaml`;
- two harness files, `harness-can.yaml` and `harness-io.yaml`.

It's used for the quickstart (`benchtare init`) and for BenchTare's own test
suite, so nothing needs hardware.

The reference DUT behaves simply and predictably:

- It boots after a short delay, then reports `Mode` every heartbeat, starting
  in `standby`.
- Writing `ModeRequest = run` moves it to `run`. Undefined codes are ignored.
- It answers each `Setpoint` it receives with `Measured`, after a short delay:
  the setpoint in `run`, and 0.0 in `standby`.
- `supply_ok` follows `supply_enable`.

The device is a DUT model like any team's (see "DUT models" below), and deals
only in signal values. The simulation wires it to the bench by following the
harness file, so the same device runs behind a virtual CAN bus or behind
simulated I/O channels.

Like a well-behaved real device, it ignores inputs it can't use. If one of its
own actions fails anyway, it keeps running and records the error.
`bench.check()` lists that error under `simulated DUT`, and so do read
timeouts, so a bug in the simulation is never mistaken for a slow DUT.

For BenchTare's own tests only, the simulation can cut a simulated wire, unplug
the DUT from a bus, or fail a simulated equipment item. That proves the
self-test catches real problems.

## DUT models

A DUT model describes how a DUT reacts to its inputs, so the tests, the
self-test, the test check and the hygiene check can all run without hardware.
The reference DUT is one. A team writes its own by subclassing
`benchtare.DutModel`, in a Python file in its project:

```python
# models/thermostat.py
from benchtare import DutModel


class Thermostat(DutModel):
    signals = ("heater_enable", "target", "temperature", "heating")

    def __init__(self, publish, *, step=1.0):
        super().__init__(publish)
        self.step = step

    def started(self):  # the DUT is powered
        self.temperature, self.enabled, self.target = 20.0, False, 20.0
        self.every(0.01, self.tick)

    def on_input(self, name, value):  # an input arrived
        if name == "heater_enable":
            self.enabled = bool(value)
        elif name == "target":
            self.target = float(value)

    def tick(self):
        goal = self.target if self.enabled else 20.0
        if self.temperature != goal:
            change = min(self.step, abs(goal - self.temperature))
            self.temperature += change if goal > self.temperature else -change
        self.publish("temperature", self.temperature)
        self.publish("heating", self.enabled and self.temperature < self.target)
```

The harness file names it, with the path relative to the harness file, and
options for its `__init__` if it takes any:

```yaml
simulate: models/thermostat.py:Thermostat
# or
simulate:
  model: models/thermostat.py:Thermostat
  options: {step: 5.0}
```

- **What a model writes:** `started()`, called once it starts, and
  `on_input(name, value)`, called for each input the bench drives. It should
  ignore what a real DUT would ignore, rather than raise.
- **What a model calls:** `publish(name, value)` to send an output;
  `after(delay, action, *args)` to do something later; and `every(period,
  action, *args)` for something cyclic.
- **One thread.** All of a model's code runs on one thread, in order, so a
  model needs no locks.
- **Its mistakes are reported, not hidden.** An exception in a model, or a
  value published for something that isn't one of the DUT's outputs, is
  recorded; the model keeps running, and the self-test's bench check fails
  with what happened. A model's `signals` are checked against the interface
  description when the harness file loads, and a model file that can't be
  imported, or a class that isn't a `DutModel`, is reported with the file and
  line.
- **A model, not the DUT.** Running the checks against a model is safe: the
  test check's corrupted readings and anything a test sends in response reach
  the model, never real equipment. A model only knows what its author wrote
  into it, though: the hygiene check against a model says nothing about state
  the model doesn't keep, such as a real DUT's NVM or files.

## What BenchTare doesn't do

- **It isn't a test runner.** pytest runs the tests.
- **It doesn't model DUT faults.** Those are stimuli a test writes.
- **It doesn't judge signal values.** Pass criteria live in assertions.
- **It isn't a real-time simulator or a board farm.** See the
  [design brief](design-brief.md).
