# BenchTare Design Brief: A Self-Verifying HIL/SIL Test Framework

This is a Markdown copy of the design brief, kept with the code so that the
goals and constraints travel with the repository. The terms it uses (DUT,
signal, bench, harness, staging, self-test, hygiene check, test check) are
defined in [model.md](model.md).

## Purpose

An open-source Python tool that gives an embedded team a complete test bench
out of the box, for hardware or simulation, and proves that its own results
are reliable.

A team supplies a description of its device's interfaces and its bench. The
tool supplies named signals, pytest fixtures, a bench self-test and a results
history.

Requirements are optional. When a team has them they are first-class: tests
link to them and every report traces to them.

It is for small embedded teams that need certification-grade evidence and
cannot afford a commercial HIL toolchain or a dedicated test-infrastructure
engineer. It should also hold up for enterprise teams in large organizations.

Status: the first slice is built and published on PyPI. The later stages are
proposals.

## Goals

1. **Complete out of the box.** A team installs the tool, describes its bench,
   and runs tests. Tests address named signals with units. Which bench channel
   carries each signal is declared in a harness file, not coded.
2. **Proves its own reliability.** A test's pass or fail is worth something
   only if everything under it works, so BenchTare verifies four areas beneath
   the DUT, each on its own: the bench equipment and the harness (a self-test
   before each run), test hygiene (each test repeats, leaves nothing behind and
   starts from a meaningful state) and test logic (a test check corrupts what
   tests read, one reading at a time, to show that each can fail at every
   verification step). Then a pass or a fail reflects the DUT. A run manifest
   records the framework version and the DUT, bench, harness and test files. A
   simulated reference DUT, bench and harness ship with the project and verify
   every framework change, like unit tests for the framework itself.
3. **Agnostic to target and hardware.** It works with any embedded target,
   whatever its software architecture. Transports are plugins: CAN, serial,
   sockets, MQTT, DAQ I/O or anything else. Backends are defined by capability,
   and the same test runs in simulation or on hardware.
4. **Zero to testing with no framework knowledge.** Tests are plain pytest. One
   command scaffolds a project, and the first test passes on a laptop with no
   hardware attached.
5. **Public protocols supported out of the box.** Any publicly available
   protocol can be added. "Supported" means tested in CI against a reference
   implementation.
6. **Requirements optional and first-class.** When a team has requirements, the
   tool imports them from open, text-based tools (Doorstop first) and from
   commercial tools through ReqIF. Tests and results trace to them in both
   directions.
7. **Results worth analysing over time.** Every run writes to a database with a
   stable schema, local by default and remote by configuration, so pass rates,
   durations and measured values can be tracked across runs.

## Non-goals

- **Not a requirements management tool.** It reads and links requirements.
  Authoring and review stay in the team's existing tool.
- **Not a real-time simulator.** It does stimulus-and-response functional
  testing. Closed-loop plant models at millisecond rates are out of scope.
- **Not a new test runner.** pytest runs the tests. The tool is a set of
  plugins and adapters.
- **Not a rewrite of working protocol libraries.** It wraps python-can,
  cantools, canopen and pymodbus first, and writes its own implementation only
  where no adequate public library exists.
- **Not a board farm.** Device leasing, flashing and lab scheduling are left to
  tools such as Jumpstarter and labgrid.
- **Not a write-back integrator for commercial tools.** Adapters for DOORS,
  Jama, Polarion and similar need access the project does not have. They are
  left to contributors who use those tools.

## How it works

The team declares its device's interfaces, its bench and the harness between
them, and optionally its requirements. Tests stage the device into the state
they need with ordinary pytest fixtures. The tool turns those into signal fixtures and requirement
IDs, so the only code the team writes is the tests.

```
 SUPPLIED BY THE TEAM
 +------------------------+   +------------------------+   +------------------------+
 | Requirements (optional)|   | Interface description  |   | Bench + harness files  |
 | Doorstop tree or ReqIF |   | one file per DUT; may  |   | equipment, and how it  |
 |                        |   | import DBC files       |   | connects to the DUT    |
 +-----------+------------+   +-----------+------------+   +-----------+------------+
             |                            |                            |
             v                            v                            v
 PROVIDED BY THE TOOL
 +------------------------+   +------------------------+   +------------------------+
 | Requirements importer  |   | Signal layer           |<->| Backend                |
 | loads IDs and text,    |   | named, typed signals   |   | simulated or real      |
 | validates test markers |   | with units, as fixtures|   | hardware, self-tested  |
 +-----------+------------+   +-----------+------------+   | before each run        |
             |                            |                +-----------+------------+
             | requirement IDs            | signal fixtures            ^
             |                            v                            | bus and I/O
             |                +------------------------+               v
             +--------------->| TESTS THE TEAM WRITES  |   +------------------------+
                              | plain pytest, tagged   |   | Device under test      |
                              | with IDs               |   | real ECU or simulated  |
                              +-----------+------------+   +------------------------+
                                          | results
                                          v
                              +------------------------+
                              | Evidence and history   |
                              | results, run manifest, |
                              | trace report, trends   |
                              +------------------------+
```

Before each run the bench and harness check themselves: the equipment reports
healthy, loopbacks read back what was driven, and the DUT is present.
Switching the bench and harness files moves the same tests between simulation
and hardware, over whichever transport plugin the bench uses. Every run ends
in a results record with a manifest of the framework version and the DUT,
bench, harness and test files.

**What the interface description is.** One file per DUT lists the signals it
exposes. It can list them directly, import them from DBC files, or both. DBC is
the common format for CAN traffic: message IDs, where each signal sits in the
bits, and its scaling and units. Importing one gives tests named signals with
no hand-written wrappers. Later, the same file will import CANopen EDS files
and Modbus register maps, and declare the standard protocols a DUT supports,
such as J1939, which bring their required messages with them.

## First slice

The first release is one thin path from signals to results, small enough to
finish in a few weeks.

1. Describe a DUT in one interface description: signals listed directly,
   imported from a DBC file, or both.
2. Run on simulated equipment (in-process I/O and a virtual CAN bus through
   python-can) with a simulated reference DUT answering.
3. Run the self-test before the tests: the bench equipment is healthy, the
   harness loopbacks read back, and the DUT is present.
4. Write a pytest test, with a staging fixture, that uses only signal names.
5. Record the run in a local database with a stable schema, with a manifest of
   the framework version and hashes of the DUT, bench, harness and test files.
6. Run the test check, which corrupts what the test reads and confirms that
   the test can fail at every check.
7. Run the hygiene check, which confirms that the test repeats, leaves nothing
   behind and starts from a meaningful state.
8. Optional: import requirements from a Doorstop tree, tag the test with a
   requirement ID, and get a trace report.

**Acceptance check:** a person who knows pytest and has never seen the tool
installs it, runs the scaffold command on the example project, and has a
passing test in under 15 minutes. The hygiene check then passes, and the test
check shows that the test can fail.

**Status:** items 1 to 7 are built, and run in CI on Linux, Windows and macOS.
Item 8 is optional (M10). BenchTare is on PyPI, and the acceptance check by
hand is still to do.

See [first-slice-plan.md](first-slice-plan.md) for the build order.

## Later stages

Each stage widens one area and leaves the others unchanged. Stage 2 is under
way, on the `plugins` branch, following [plugin-plan.md](plugin-plan.md); the
order of the rest is a proposal.

| Stage | Area | Adds |
| --- | --- | --- |
| 2 | Plugins | A versioned plugin API, with plugins identified by UUID and found by discovery; every claim a plugin makes labelled verified, attested or asserted; a conformance kit, and grading from bronze to platinum on verifiability; CAN with DBC as the first transport. Then example plugins built only on the public API, as an outside team would: SocketCAN on a virtual bus, a QEMU machine backend for an embedded Linux example DUT built with Yocto, serial, and SSH, whose state probes give test hygiene real operating-system state; and a plugin template and guide |
| 3 | Hardware and more transports | First hardware: SocketCAN on a real adapter, plus two dissimilar low-cost DAQ devices, to prove the capability interface. The test check on real equipment needs an explicit opt-in, since a corrupted reading can lead a test to send a stimulus it normally wouldn't. The hygiene check is run on a real DUT, as acceptance. TCP/UDP sockets and MQTT as plugins, on stage 2's interfaces |
| 4 | Self-verification | Standard interface fault stimuli, such as dropped or stopped CAN messages; the shipped reference extended to cover each new backend and transport. For test hygiene: a more realistic simulated reference (measurement noise, settling ramps, a fault that latches until a supply cycle, a free-running counter, state only a probe can read), beyond stage 2's example DUT, running tests in other orders, and active disruption on DUT models. For the test check: replaying recorded readings, for signals that need it |
| 5 | Requirements | ReqIF, StrictDoc and Sphinx-Needs importers, feeding the trace report M10 builds from Doorstop |
| 6 | Analysis | Remote database option; trends for pass rate, duration and measured values |
| 7 | Protocols | CANopen and Modbus, each with a CI reference; J1939 and ISOBUS considered after that. Each standard brings the valid ranges and special values (such as "error" and "not available") it defines for its signals: tests can read them instead of retyping the standard, and an interface description can override them per signal |
| 8 | Community | Guides for backends and adapters, beyond stage 2's plugin guide; write-back adapters from contributors |
| 9 | Test development in IDEs | Setup for writing and debugging tests in common IDEs, such as VS Code and PyCharm: schemas so the editor completes and checks interface descriptions, bench files and harness files; completion of signal names in tests; run and debug configurations; `benchtare init` writing them for a new project |
| 10 | GUI | A graphical front end for people who don't live in a terminal, for example to run the self-test and tests, follow a run, and browse results and test records. It drives pytest and reads the results database; it doesn't replace either. Scope to be decided |
| 11 | Platforms | Linux, Windows and macOS supported on real hardware as well as in simulation: hardware backends that work on each (SocketCAN, from stage 2, is Linux-only, so Windows and macOS need vendor CAN adapters through python-can's interfaces, such as PCAN or Kvaser); install and setup guides for each |
| 12 | User guide | A comprehensive user guide with worked examples, beyond the README quickstart: recommended ways to do common things, built from the topics collected below |
| 13 | Test design | Helpers for boundary tests: from a signal's range, given by a standard (stage 7) or by the engineer, whose range always wins, the classic values: above and below the range, its maximum and minimum, and seeded random values within it, with the seed stored in the run so a failure can be repeated. Ranges only help write tests: BenchTare still never judges a reading by them |

Stages 9 to 13 are on the horizon rather than planned in detail. None is
needed for the first slice, but the first slice shouldn't make them harder: the
file formats, the CLI and the results schema are what they will build on. The
first slice already runs on all three platforms: CI tests it on Linux, Windows
and macOS, and the code stays portable (no OS-specific calls, paths through
`pathlib`).

**User guide topics so far.** Recommended practices that come up while the
first slice is designed, kept here until the guide is written:

- **DUT identity in CI/CD pipelines.** A pipeline knows which firmware build
  and hardware it is testing. It can pass them on the command line
  (`pytest --identify application=$FIRMWARE_VERSION`, with the shell filling in
  the variable), or a session fixture in `conftest.py` can read them from
  environment variables and call `bench.identify(...)`. BenchTare never reads
  environment variables itself: the team's code chooses which to record.
- **Timing requirements are timeouts.** Set each `wait_until` and `read`
  timeout from the requirement, not from what is comfortable: a response that
  arrives after the timeout fails the test, so the timeout is the timing check.
  The test record keeps how long each response took, for trends.
- **Boundary tests are ordinary tests.** Values above and below a range, at its
  limits and in between come from the requirement or the standard, and go in
  a parametrized test. The test check then proves each case can fail.
- **Wait for states that last, not short pulses.** A sampled channel, on a
  DAQ or the simulated `sim` equipment, can miss a value that lasts less than
  a sample period or two. Wait for a state the DUT holds, or for the pulse's
  effect, rather than for the pulse itself.

## Prior art

No existing tool found combines a signal layer, a self-verifying bench and
optional traceability. Several cover one part well, and the plan is to
integrate with them, not compete.

This table comes from documentation and search results read on 14 Nov 2026,
not from hands-on evaluation.

| Tool | Covers | Relationship |
| --- | --- | --- |
| [jamb](https://jamb.readthedocs.io/en/latest/user-guide/pytest-integration.html) | pytest requirement markers, trace matrix and test records for IEC 62304 | Closest on traceability. Reuse its marker convention if compatible |
| [reqcov](https://pypi.org/project/reqcov/) | Requirement coverage from JUnit results, as a CLI and GitHub Action | Possible consumer of the results file |
| [Sphinx-Test-Reports](https://sphinx-test-reports.readthedocs.io/en/latest/) | Imports JUnit results into Sphinx-Needs and links them to requirements | Emit JUnit properties it can map |
| [StrictDoc](https://strictdoc.readthedocs.io/en/stable/) | Text-based requirements with traceability to code, tests and coverage | Import source and report target |
| [LOBSTER](https://github.com/bmw-software-engineering/lobster/wiki) | BMW's traceability report across requirements, code and tests | Reference for the trace model. AGPL licensed |
| [Jumpstarter](https://github.com/jumpstarter-dev/jumpstarter-python) | Board-farm HIL with drivers for CAN, UART, GPIO and power | Possible host for backends. No signal layer found |
| [OpenTAP](https://opentap.io/) | Test sequencer for bench instruments, C# with Python plugins | Different audience |
| [py-micro-hil](https://pypi.org/project/py-micro-hil/) | Small HIL framework for Raspberry Pi peripherals | Similar aim at smaller scope |

## Risks and open questions

**Risks**

- **Breadth.** The scope now includes self-verification, several transports and
  run analysis, which is a lot for a small project. The first slice and
  the plugin interfaces are the mitigation, and each later stage widens one
  area only.
- **Protocol overlap.** J1939 and ISOBUS are left out of the early stages. They
  can be added later from the public standards.
- **Write-back cost.** Each commercial requirements tool has its own API, and
  the project has no way to test against them. Write-back adapters are left to
  contributors who use those tools.
- **Crowded traceability space.** Several pytest traceability plugins appeared
  in 2026. Traceability is a secondary feature here. The value is a complete
  bench that proves its own reliability.
- **Self-verification depth.** Proving a test bench reliable can grow without
  limit. The first slice limits it to the self-test, the hygiene check and the
  test check, against simulated equipment and DUT models, plus a run manifest.
  Deeper checks, such as active disruption and a more realistic reference, are
  later stage 4.

**Decided**

- The license is MIT, for the widest adoption. Sharing improvements back is
  encouraged, not required.
- The working name is BenchTare, installable as `benchtare`.
- Transports are plugins, and the framework does not care which one a bench
  uses.
- The reference bench is simulated and ships with the project.
- Run history goes to a database, local by default and remote by
  configuration. The local database is SQLite.

**Open questions**

- [x] Confirm the name is free on PyPI and GitHub, and reserve it: BenchTare
      is published on PyPI.
- [x] Standalone project, or built on Jumpstarter's driver model? Standalone,
      with its own plugin API (plugin plan, step 0). Jumpstarter stays a
      possible host for equipment, through a backend plugin.
- [ ] Which two dissimilar low-cost DAQ devices prove the capability interface first?
- [x] Which transport plugin ships second, after virtual CAN? The plugin plan
      builds serial and SSH next, as example plugins (stage 2).
- [ ] Is a hardware reference bench needed later?
- [x] Which local database is the default? SQLite: the first slice is built on
      it.
- [ ] Which IDEs get a ready-made setup first?
- [ ] What should the GUI cover first, and should it be a desktop app or a local web page?
- [x] When should CI start testing on Windows and macOS? From the first slice:
      CI tests all three.
