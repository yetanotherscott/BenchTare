# BenchTare Design Brief: A Self-Verifying HIL/SIL Test Framework

This is a Markdown copy of the design brief, kept with the code so that the
goals and constraints travel with the repository. The terms it uses (DUT,
signal, bench, harness, staging, self-test, test check) are defined in
[model.md](model.md).

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

Status: second draft.

## Goals

1. **Complete out of the box.** A team installs the tool, describes its bench,
   and runs tests. Tests address named signals with units. Which bench channel
   carries each signal is declared in a harness file, not coded.
2. **Proves its own reliability.** Before each run, a self-test checks the
   bench equipment and the harness that connects it to the DUT. A run manifest
   records the framework version and the DUT, bench, harness and test files. A
   test check corrupts what tests read, to show that each test can fail. A
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
   the test can fail.
7. Optional: import requirements from a Doorstop tree, tag the test with a
   requirement ID, and get a trace report.

**Acceptance check:** a person who knows pytest and has never seen the tool
installs it, runs the scaffold command on the example project, and has a
passing test in under 15 minutes. The test check then shows that the test can
fail.

See [first-slice-plan.md](first-slice-plan.md) for the build order.

## Later stages

Each stage widens one area and leaves the others unchanged. The order is a
proposal.

| Stage | Area | Adds |
| --- | --- | --- |
| 2 | Backends | First hardware: SocketCAN plus two dissimilar low-cost DAQ devices, to prove the capability interface |
| 3 | Transports | Transport plugin interface, with serial, TCP/UDP sockets and MQTT as the first plugins |
| 4 | Self-verification | Standard interface fault stimuli, such as dropped or stopped CAN messages; the shipped reference extended to cover each new backend and transport |
| 5 | Requirements | ReqIF, StrictDoc and Sphinx-Needs importers; trace report |
| 6 | Analysis | Remote database option; trends for pass rate, duration and measured values |
| 7 | Protocols | CANopen and Modbus, each with a CI reference; J1939 and ISOBUS considered after that |
| 8 | Community | Backend and adapter guides; write-back adapters from contributors |

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
  limit. The first slice limits it to a self-test, a run manifest and one
  injected fault.

**Decided**

- The license is MIT, for the widest adoption. Sharing improvements back is
  encouraged, not required.
- The working name is BenchTare, installable as `benchtare`.
- Transports are plugins, and the framework does not care which one a bench
  uses.
- The reference bench is simulated and ships with the project.
- Run history goes to a database, local by default and remote by
  configuration.

**Open questions**

- [x] Confirm the name is free on PyPI and GitHub. (Reserving it on PyPI is
      still to do.)
- [ ] Standalone project, or built on Jumpstarter's driver model?
- [ ] Which two dissimilar low-cost DAQ devices prove the capability interface first?
- [ ] Which transport plugin ships second, after virtual CAN?
- [ ] Is a hardware reference bench needed later?
- [ ] Which local database is the default? (SQLite is assumed in the first-slice plan.)
