# How BenchTare Tests Itself

BenchTare asks a team to prove its bench, its harness, its tests' hygiene and
its tests' logic before trusting a result about the DUT. BenchTare's own test
suite is held to the same standard. This document says how, area by area, and
where it doesn't yet reach it.

## The five areas, for BenchTare's own suite

| Area | What BenchTare asks of a team | How BenchTare's own suite meets it |
| --- | --- | --- |
| **Bench** | Every equipment item works | The suite runs on Linux, Windows and macOS, on every supported Python version, and on the oldest pytest BenchTare supports (CI's `test` and `oldest-pytest` jobs) |
| **Harness** | Everything is wired as described, and the DUT is there | The quickstart runs from the built wheel, installed in a fresh virtual environment outside the checkout, on all three systems, so nothing comes from the source tree (CI's `quickstart` job) |
| **Test hygiene** | Each test stands on its own | No test's outcome depends on how quickly the simulated DUT answers: the whole suite also runs against a slowed DUT (CI's `slowed-dut` job; see [Timing](#timing)) |
| **Test logic** | Every check in each test can fail | Recorded breaks: each takes one behaviour away from the source and names the tests that must then fail (CI's `breaks` job; see [Recorded breaks](#recorded-breaks)) |
| **The DUT** | The team's tests pass | BenchTare's own tests pass, on every push to every branch |

CI runs on every push to every branch, `main` included, and every run is
checked. **A failure is never ignored**, rerun until it passes, or put down to
a slow runner without finding its cause. A test that fails only sometimes
depends on timing or on order: it is fixed so its outcome doesn't, and the fix
is shown by reproducing the failure first.

## Recorded breaks

A test that still passes when the behaviour it guards is taken away proves
nothing. BenchTare's test check proves a team's tests can fail by corrupting
what they read. BenchTare's own tests are proven the same way, by breaking the
code they test. Each break is recorded in the repository, so anyone can see
what each test is shown to catch, and rerun the proof.

### A break

The breaks are in `tests/breaks/`, one file per area of BenchTare:

```yaml
- id: loopback-drives-one-level
  guards: A loopback is driven both ways, so a line stuck at one level is caught
  file: src/benchtare/selftest/checks.py
  find: "LOOPBACK_VALUES = (True, False)"
  replace: "LOOPBACK_VALUES = (True,)"
  caught_by:
    - tests/test_selftest.py::test_a_loopback_stuck_at_one_level_is_caught
```

| Field | What it is |
| --- | --- |
| `id` | A short name, unique across all the files: lower-case words joined by `-` |
| `guards` | The behaviour the break takes away, said as what BenchTare does |
| `file` | The source file it changes, under `src/` |
| `find` | Text that occurs exactly once in that file. A block scalar (`\|2`, with `\|2-` to leave off the final line break) keeps a multi-line change's indentation exact |
| `replace` | What that text becomes. It may be empty, to remove it |
| `caught_by` | The tests that must fail with the break applied, each named as `file::test` |

### How a break is proven

`pytest -m breaks` runs every break; `-k` chooses some, by id
(`pytest -m breaks -k loopback`). For each break:

1. **The repository is copied,** and the copy's `src/` comes first on the
   Python path, so the copy is what the tests run. This is checked once: the
   copy's tests must import BenchTare from the copy.
2. **Every named test must pass in an unbroken copy** first, so a failure with
   the break applied is the break's doing.
3. **The break is applied** to a fresh copy, and only its named tests run.
4. **Each named test must fail.** An error doesn't count: a fixture that
   broke, or a test file that couldn't be imported, shows the break stopped
   the test from running, not that the test checks what was taken away. A
   test named without its parameters must fail with every parameter set.

A break that isn't caught fails with the behaviour it took away, what each
named test did instead, and the end of the run's output.

Every usual `pytest` run also checks, quickly, that each break is well formed,
that its `find` text still occurs exactly once, and that each test it names
exists. So a change to the code that leaves a break behind fails at once, on
every system. Change the break to match the new code, so it still takes the
same behaviour away.

### Writing breaks

- **Every new test comes with at least one break** that it catches, in the
  same commit. The exception is a test that guards no behaviour of the code in
  `src/`, such as the changelog's format.
- **A break is a plausible fault:** a condition inverted or dropped, a check
  skipped, a value off by one, a step left out. A break that only makes the
  code crash proves the code runs, not that a test checks what it does.
- **Name only the tests that must fail.** Each one named must fail, so a list
  of tests that might fail is no proof.
- **A break that isn't caught has found a gap.** Strengthen the test, or write
  the test that was missing, then run the break again. Don't change the break
  to one the tests happen to catch.

## Timing

The simulated reference DUT reports its mode every 0.05 s and answers a
setpoint after 0.02 s. On a fast machine every value arrives at once; on a busy
CI runner a value can take far longer. A test that waits a short time for a
value passes on the first and fails on the second, now and then.

CI's `slowed-dut` job makes that failure happen every time. It runs
`tools/slow_reference_dut.py`, which changes the reference DUT's defaults in
the CI checkout, so it reports its mode every 0.3 s and answers after 0.6 s.
The script checks that its change reached what the tests import. Then the job
runs the whole suite. Nothing is added to BenchTare for this: the change is
made only in a throwaway copy.

To run it yourself, on a checkout you can undo:

```
python tools/slow_reference_dut.py
pytest
git checkout src/benchtare/reference/device.py
```

**Writing tests whose outcome doesn't depend on timing:**

- Wait for what you expect with a generous timeout, such as 2 s. A wait
  returns as soon as the value arrives, so this costs nothing when it does.
- To show something does *not* happen, first wait generously for something
  that does, then make the short wait. `wait_until` checks each value that
  arrived after the last write, in order, so its outcome no longer depends on
  how quickly the values came.
- Count events by waiting for them, not by sleeping and then counting.
- In a test of waiting itself, deliver the later value only once the wait
  has started, not on a timer (see `after_checking` in
  `tests/test_receive_buffer.py`). On a slow runner a timer can fire before
  the wait begins, and then a wait that never waits still passes: the test
  passes either way, but catches the break only on a fast machine.

## Where it falls short

What isn't covered yet, so the gaps are visible rather than hidden:

- **Recorded breaks cover only some of BenchTare so far.** The table below
  lists each area. The rest of the first slice is being covered area by area.
- **Breaks and the slowed DUT run only on Linux.** The usual suite runs on all
  three systems.
- **Test order isn't checked.** The suite always runs in the same order, so a
  test that depends on an earlier one would pass unnoticed.
- **A team's staging fixtures aren't covered by the test check.** It corrupts
  only what a test's body reads; staging and teardown read real values. This
  is a separate design, still to come.

### Breaks, area by area

| Area | Breaks file | Covered |
| --- | --- | --- |
| Self-test: the bench and harness checks, and how a run uses them | `tests/breaks/selftest.yaml` | Yes |
| Results: the database, its schema and migrations, the manifest, and how a run is stored | `tests/breaks/results.yaml` | Yes |
| Signals and the interface description | `tests/breaks/signals.yaml` | Yes |
| The bench and harness files | `tests/breaks/files.yaml` | Yes |
| Backends, the read rule and `wait_until` | `tests/breaks/backends.yaml` | Yes |
| The reference DUT and DUT models | `tests/breaks/dut.yaml` | Yes |
| The pytest plugin and the test record | `tests/breaks/plugin.yaml` | Yes |
| The hygiene check | `tests/breaks/hygiene.yaml` | Yes |
| The test check | `tests/breaks/testcheck.yaml` | Yes |
| The CLI and `benchtare init` | | Not yet |
