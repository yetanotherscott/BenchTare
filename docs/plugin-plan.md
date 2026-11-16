# BenchTare Plugin Plan

**Status: draft, subject to change. Step 0, the plan as a whole, is
confirmed (16 Nov 2026); step 1 is next.** This plan is worked through one step
at a time. Each step ends at a **check-in**, where the decisions it depends on
are confirmed before any code is written. The first design matters: it can
change later, but changes get painful once anyone has adopted it.

The terms used here are defined in [model.md](model.md). Where this plan adds
a term, it says so.

## Aims

1. **A realistic target.** An embedded Linux DUT running in QEMU and built with
   Yocto. It runs real software, with real boot times, real timing and real
   operating-system state, and it still needs no hardware.
2. **Plugin interfaces, proven by use.** The pieces that reach that target (a
   SocketCAN backend, a QEMU machine backend, serial and SSH) are built as
   example plugins, exactly as an outside team would build them, using only the
   public plugin API.

## How decisions are marked

- **Decided:** confirmed by the user.
- **Proposed:** a starting point, to be confirmed at the check-in of the step
  that needs it.
- **Open:** needs thinking before the step that depends on it.

## Decided

1. **Backend and transport** get a working definition now (see below). It is
   expected to evolve as the examples are built.
2. **Every claim a plugin makes is either verified or asserted,** and plugins
   are treated like the rest of the stack: verifiable as far as possible, with
   what can only be asserted labelled as such.
3. **Plugins are identified by UUID,** so two plugins can't be confused. A
   collision is an error that says what to do to resolve it.
4. **Plugins can be separate distributions.** BenchTare may ship core plugins,
   or plugins of very high quality, itself.
5. **Plugins are graded bronze, silver, gold and platinum** (platinum highest),
   like Home Assistant's integration quality scale, but on verifiability, not on
   any subjective measure. An organisation can always see what it takes to
   reach platinum. This needs real thinking, and will evolve.
6. **The example DUT is embedded Linux built with Yocto, in a separate
   repository** that BenchTare references. Open to change.
7. **LGPL dependencies are acceptable** when they don't directly affect
   consumers of BenchTare's distribution, as with python-can. GPL and AGPL
   dependencies are flagged, as before.
8. **This work happens on the `plugins` branch,** merged from `main`
   regularly, while the first slice is released on `main`.

Confirmed at step 0:

9. **The aims and what is not in this plan** stand as written.
10. **Measure first.** Steps 1 to 5 build the means of measuring plugins before
    steps 6 to 10 build the plugins. The plugin API stays provisional until
    step 7's SocketCAN plugin, the first separate distribution, has used it.
11. **Versioning.** Nothing is released from this branch. It first merges into
    `main` after step 7, released as 0.1.0: the results schema and how
    backends are found both change, so under Semantic Versioning before 1.0.0
    it is a minor release. Steps 8 to 10 merge as each one finishes. Each step
    adds its notable changes under `## [Unreleased]` in `CHANGELOG.md`.
12. **The acceptance check by hand gates the first merge.**
13. **BenchTare is standalone.** It defines its own plugin API, rather than
    building on Jumpstarter's driver model. Jumpstarter stays a possible host
    for equipment, reached later through a backend plugin.
14. **Step 1 gives "transport" one meaning everywhere:** in `model.md`, in
    `CLAUDE.md`'s vocabulary and decisions, and in the design brief's goals
    and decisions, not in `model.md` alone.
15. **The design brief's later stages follow this plan.** Its stage 2 is this
    work.

## Working definitions (proposed)

### Backend and transport

- **Backend:** code that drives one kind of equipment: a CAN adapter, a serial
  port, an SSH connection, a QEMU machine. It opens, closes, checks itself and
  moves values.
- **Transport** (new meaning): the encoding between signals and what a bus
  carries: DBC frames on CAN, text commands on a serial line, commands and files
  over SSH.
- A **channels** backend carries plain values on named channels, and needs no
  transport. A **bus** backend carries one transport. Today's two kinds of
  backend become this distinction, and CAN with DBC becomes the first
  transport.

| Equipment | Backend | Transport |
| --- | --- | --- |
| A CAN interface (`vcan`, later a real adapter) | SocketCAN | CAN with DBC |
| A serial console | Serial port | A line protocol |
| A network connection to the DUT | SSH | Commands and files |
| The QEMU machine itself | QEMU (channels: power) | None |

Open: does a transport own its own section of the interface description, as
`can:` does today? Is SSH one backend with one transport, or more than one?

### Verified, attested and asserted

Every result, record entry and plugin claim is one of:

- **Verified:** BenchTare proved it independently. A loopback read back what
  was driven; a transport encoded a value and decoded the same value back; a
  backend passed the conformance kit's read-rule tests.
- **Attested:** proven by a recorded procedure a person carried out, because
  only a person can: pulling a real cable to show its failure is reported. The
  record says what was done, by whom and when.
- **Asserted:** the plugin or the equipment says so, and BenchTare can only
  record it: a serial number, "healthy", a channel's capability with no
  loopback to prove it.

This applies to the core too. Today's bench check passes when each backend's
`check()` says it is healthy, which is asserted; the harness check's loopbacks
are verified. Results don't yet say which is which. Labelling them changes the
results schema, so it needs a version bump and a migration.

### Plugin identity

- Each plugin declares a UUID, generated once by its author and never changed.
  Bench files keep friendly names (`backend: socketcan`).
- **The same name from two plugins:** an error naming both distributions and
  both UUIDs. It says to uninstall one, or to name it by UUID.
- **The same UUID from two plugins:** an error saying one was almost certainly
  copied from the other, and that its author must generate a new UUID. A plugin
  template that generates a fresh UUID prevents most of these.
- Each run records every plugin it used: UUID, name, version and distribution.
  That is another results schema change.

Open: where the UUID is declared (a class attribute, or entry-point metadata),
and whether a bench file can name a plugin by UUID directly.

### The plugin API

- One public, versioned module is everything a plugin may import. Plugins
  declare which API version they target.
- The example plugins import only that module, and CI fails if one reaches
  into BenchTare's internals. That is what makes them honest examples.
- Built-in backends (`sim`, `sim_can`) are found through the same discovery as
  plugins.

Open: exactly what the API contains.

### Discovery

Entry-point groups, one per kind of plugin: `benchtare.backends` and
`benchtare.transports` to start with (names proposed). A bench file that names
a plugin that isn't installed gets an error saying which package provides it,
when that is known.

### The conformance kit

A test suite per kind of plugin that any author can run against their own
plugin. For a backend:

- the read rule: no value from before a write is returned, and no fast reply is
  missed;
- timeouts, and refusing a value the channel can't carry;
- `check()` and `identity()`;
- a failure reported when one is injected, through the plugin's fault hooks or
  simulated counterpart.

The kit does for plugins what the self-test does for a bench. The example
plugins pass it first.

### Grading (strawman, expected to change)

Each tier includes the one below it. Every rule is something a tool can check
or a record that exists, never a judgement call.

| Tier | Rules |
| --- | --- |
| Bronze | Has a UUID and declares its API version; is discovered and loads; declares its licence and its dependencies' licences; passes the basic conformance suite (opens, closes, describes itself) |
| Silver | Passes the full conformance suite for its kind; reports a failure when one is injected; writes unexpected events to the test record; runs in CI on the platforms it claims |
| Gold | Labels every claim verified, attested or asserted; ships a simulated counterpart or fault hooks, so the self-test is proven to catch it failing; its own tests catch every break in a standard set of deliberate breaks |
| Platinum | Verified against real equipment or a reference implementation, in CI or by a published, reproducible run with its manifest; nothing that could be verified is left asserted |

`benchtare grade` would print a plugin's tier and every rule it misses, so the
way to platinum is always visible. Plugins BenchTare ships are expected to be
platinum.

Open: thresholds (for example, how many deliberate breaks make a "standard
set"); the format of an attested procedure; whether grade reports are
published anywhere.

## The example target

- **A separate repository** (name to be decided) holds a Yocto layer with the
  reference DUT application: the reference DUT rewritten as a small C program
  on SocketCAN, speaking the reference DBC. It is also the worked example of a
  team's own project: its interface description, bench and harness files and
  tests live there too.
- **Images are built occasionally, not on every CI run.** Yocto builds take
  hours and tens of GB of disk. The repository publishes images as releases,
  with Yocto's licence manifest and the sources the GPL requires. BenchTare's
  CI downloads one pinned by its hash, and the hash goes into the run's DUT
  identity, so which image was tested is verified, not asserted.
- **The CAN path needs a Linux host.** QEMU connects its emulated CAN bus to
  the host only through SocketCAN (`vcan`). Tests that use it run in Linux CI
  only, everything else stays on Linux, Windows and macOS. This is an
  exception to a hard rule, so it is proposed, not decided.
- Open: which QEMU machine (`qemux86-64` is faster where KVM is available;
  `qemuarm64` is more like a real target), and whether GitHub's Linux runners
  provide `vcan` and KVM.

## Steps

Each step ends with tests passing, a commit on the branch, and a check-in.

| Step | What it delivers | Check-in confirms |
| --- | --- | --- |
| 0 | This plan (confirmed) | The plan as a whole |
| 1 | The definitions in `model.md`, `CLAUDE.md` and the design brief: backend and transport; verified, attested and asserted; how the core's current checks are labelled | The definitions |
| 2 | Plugin API, identity and discovery: the versioned API module, UUIDs, entry points, the built-in backends found the same way, and each run recording its plugins (a results schema change, with a new version and a migration) | The API surface, where UUIDs live, the schema change |
| 3 | Verified and asserted in the core: self-test results and records labelled, with the bench check's asserted health kept apart from its verified checks | The wording, the schema change |
| 4 | The conformance kit for backends, run against `sim` and `sim_can` | The rule list |
| 5 | Grading, first version: `benchtare grade`, with each tier's rules drawn from the kit and the plugin's metadata | The tiers and their rules |
| 6 | CAN with DBC extracted as the first transport, behind the transport interface, with no change in behaviour: the whole suite passes unchanged | The transport interface |
| 7 | The SocketCAN backend as a separate distribution in this repository, tested on `vcan` and graded | Where it lives; the Linux-only exception |
| 8 | The QEMU machine backend (power through QEMU's control socket, QMP) and the example repository: the existing reference tests pass unchanged against the Yocto image, with only the bench and harness files swapped | The repository, the image, CI |
| 9 | Serial and SSH: a serial backend with a line transport, an SSH backend with a command-and-file transport, and an SSH state-probe plugin, so the hygiene check runs against real operating-system state | Each interface |
| 10 | A plugin template that generates a fresh UUID, and a guide to writing plugins built from the examples | The template and the guide |

Steps 1 to 5 build the means of measuring plugins before the plugins being
measured.

## Before the first merge into `main`

After step 7, and then:

- The acceptance check by hand is done. The first slice is already on PyPI, as
  0.0.1 and 0.0.2.
- `CLAUDE.md`'s scope and the first-slice plan's out-of-scope list are updated
  to match whatever this plan has become, and the design brief's stage 2 says
  what has merged.
- The changelog's Unreleased lines become 0.1.0, which is released from
  `main`.

## Not in this plan

Real hardware backends and DAQ devices (design brief, stage 3); TCP/UDP
sockets and MQTT (stage 3); CANopen, Modbus, J1939 and other protocols; a
remote database; a GUI; requirements and the trace report (M10, stage 5); a
public registry of plugins; a backend built on Jumpstarter.

## Licensing

- **QEMU** (GPL-2.0) is an external tool. BenchTare never links or ships it.
- **The guest image** contains GPL software (the Linux kernel, BusyBox and
  more). It is never in BenchTare's package. The example repository publishes
  it with Yocto's licence manifest and the corresponding sources.
- **Python libraries,** each checked before it is added: pyserial (BSD) for
  serial; for SSH, paramiko (LGPL-2.1, acceptable under decision 7) or the
  system's `ssh` command; for QMP, a library, or its JSON protocol spoken
  directly.
- The grading checks every plugin's dependency licences.
