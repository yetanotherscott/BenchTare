# Results schema

**Status: version 2 is built (M6).** Version 1 (M5) is the schema below;
version 2 adds the test check (see "Version 2" below), and M8 will add
version 3. A new database is created at version 1 and brought up through the
same migrations as an old one, so every database ends up the same. Changing it
means a new version and a migration, never a silent change.

## What it has to serve

| Who reads it | What they need |
| --- | --- |
| M5 | Each run, its manifest, each test's outcome, duration and test record, and the self-test result |
| M6 | Each test check, per test and mode, and when each test file was last checked (version 2) |
| M8 | The requirement IDs each test result covers |
| Later: analysis (stage 6) | Pass rates and durations over time, and trends in measured values, so readings must be queryable, not only stored as text |
| Later: remote database (stage 6) | Runs written locally can be copied to a shared database without ID clashes, so IDs are global and the SQL is plain |
| Later: GUI (stage 10) | Browse runs, results and records without parsing BenchTare's own files |
| Anyone chasing an intermittent failure | The machine and software environment each run had, the bench hardware and drivers, and the DUT's hardware revision and software versions, so a failure that only happens on one machine, adapter, firmware or package version can be found |

## Principles

- **One SQLite file**, `.benchtare/results.sqlite3` under pytest's rootdir by
  default. It is set with `--results PATH` or `benchtare_results` in the ini
  file.
- **Plain SQL**: tables, text, integers and reals only, so the same schema works
  on a server database later. Structured extras are JSON in TEXT columns.
- **Global run IDs**: a run's ID is a UUID, so runs from many benches can be
  merged. Rows inside a run are numbered within it.
- **Times** are UTC, ISO 8601, to the microsecond:
  `2026-11-15T18:52:09.123456Z`. They sort as text and read as dates.
- **Paths** are relative to pytest's rootdir, with `/` separators on every OS,
  so a results file means the same thing on Linux, Windows and macOS.
- **Written as the run goes.** The run is stored when it starts, each test when
  it finishes, and the run is marked finished at the end. A run that crashed
  stays marked `running`, with every test that finished.
- **Nothing is deleted.** There is no pruning in the first slice.
- **On by default.** Every run is stored. `--no-results` skips it, for
  throwaway runs while writing tests.
- **No secrets.** Environment variables are never recorded, since they often
  hold credentials. Nor are user names.

## Version 1 tables

### `meta`

Facts about the database itself, one row per key.

| Column | Type | Notes |
| --- | --- | --- |
| `key` | TEXT, primary key | `schema_version`, `created_at`, `created_by` |
| `value` | TEXT, not null | `1`; a time; `benchtare 0.1.0` |

`PRAGMA user_version` holds the same schema version, so SQLite can check it
without reading a table. The `meta` row is the copy a server database keeps.

### `runs`

One row per session: a test run, a self-test-only run (`benchtare selftest`), or
a test check (M6). The version and setup columns are the manifest's summary.

| Column | Type | Notes |
| --- | --- | --- |
| `id` | TEXT, primary key | UUID |
| `kind` | TEXT, not null | `tests`, `selftest` or `check-tests` |
| `status` | TEXT, not null | `running`, `finished`, or `stopped` (the self-test failed, or the run was interrupted) |
| `started_at` | TEXT, not null | |
| `finished_at` | TEXT | Null while running, or if it crashed |
| `exit_code` | INTEGER | pytest's exit code |
| `schema_version` | INTEGER, not null | The schema this run was written with |
| `record_level` | TEXT, not null | `results`, `stimuli` or `debug` |
| `dut` | TEXT, not null | Name from the interface description |
| `bench` | TEXT, not null | Name from the bench file |
| `harness` | TEXT, not null | Name from the harness file |
| `simulated_dut` | TEXT | The harness's `simulate:` value; null with a real DUT |
| `benchtare_version` | TEXT, not null | |
| `pytest_version` | TEXT, not null | |
| `args` | TEXT, not null | JSON: pytest's arguments as given |
| `git_commit` | TEXT | The project's commit, if it is a git repository |
| `git_dirty` | INTEGER | 1 if the working tree had uncommitted changes |
| `tests_passed`, `tests_failed`, `tests_errors`, `tests_skipped` | INTEGER | Filled when the run finishes |

The machine and software environment are in `run_environment` and
`run_packages`, the bench hardware in `run_equipment`, and the DUT's identity
in `run_dut_identity`, below.

### `run_environment`

The machine and system the run happened on, one row per run, so an
intermittent failure can be tied to a machine, an OS or a Python build.

| Column | Type | Notes |
| --- | --- | --- |
| `run_id` | TEXT, primary key | |
| `host` | TEXT | The machine's name |
| `os` | TEXT, not null | `Linux`, `Windows` or `Darwin` |
| `os_release` | TEXT | Such as `6.8.0-45-generic`, `11` or `24.1.0` |
| `os_version` | TEXT | The OS's full version string |
| `architecture` | TEXT | Such as `x86_64`, `AMD64` or `arm64` |
| `processor` | TEXT | The processor name, where the OS gives one |
| `cpu_count` | INTEGER | Logical CPUs |
| `python_implementation` | TEXT, not null | Such as `CPython` |
| `python_version` | TEXT, not null | Such as `3.12.3` |
| `python_build` | TEXT | Build number, date and compiler |
| `encoding` | TEXT | The default text encoding, such as `utf-8` or `cp1252` |
| `timezone` | TEXT | The local timezone's name and UTC offset |
| `ci` | TEXT | The CI system, if the run was under one (`github-actions`, `gitlab-ci`, `jenkins` and so on), found from the variables those systems set; null otherwise |

Total memory isn't recorded: the standard library can't read it the same way on
all three platforms.

### `run_packages`

Every Python package installed where the run happened, so "does this only fail
with cantools 40.x?" is a query.

| Column | Type | Notes |
| --- | --- | --- |
| `run_id` | TEXT, not null | |
| `name` | TEXT, not null | Normalised distribution name, such as `python-can` |
| `version` | TEXT, not null | |

Primary key: `run_id, name`.

### `run_equipment`

The bench equipment the run used to drive and read the DUT's signals, one row
per equipment item, as its backend describes it when the bench opens. Real
hardware fills in what it can report; a simulated backend says it is simulated
and leaves the hardware columns empty.

| Column | Type | Notes |
| --- | --- | --- |
| `run_id` | TEXT, not null | |
| `equipment` | TEXT, not null | Its name in the bench file, such as `can0` |
| `backend` | TEXT, not null | Such as `sim_can` or, later, `socketcan` |
| `kind` | TEXT, not null | `channels` or `bus` |
| `simulated` | INTEGER, not null | 1 for simulated equipment |
| `vendor` | TEXT | Such as the adapter's maker |
| `model` | TEXT | |
| `serial_number` | TEXT | |
| `firmware_version` | TEXT | The equipment's own firmware |
| `driver` | TEXT | The driver or library that talks to it |
| `driver_version` | TEXT | |
| `details` | TEXT | JSON: anything else the backend reports, such as a CAN bitrate or the channels in use |

Primary key: `run_id, equipment`.

### `run_dut_identity`

The DUT's identity for the run: its hardware revision and software versions
(bootloader, application, subsystems, and whatever else the DUT has). The
interface description lists the items every run is expected to collect, by name
only; any other item can be added during the run. Each value belongs to the
whole run, whenever it was collected.

| Column | Type | Notes |
| --- | --- | --- |
| `run_id` | TEXT, not null | |
| `name` | TEXT, not null | Such as `bootloader` or `motor.firmware` |
| `seq` | INTEGER, not null | 1 for the first value; a later, different value of the same item gets the next number |
| `expected` | INTEGER, not null | 1 if the interface description lists the item |
| `value` | TEXT | Null when an expected item was never collected |
| `source` | TEXT, not null | `signal` (read from a DUT output), `code` (given by the team's code), `command-line`, or `missing` |
| `at` | TEXT | When it was collected; null when missing |

Primary key: `run_id, name, seq`.

How the values are collected is described under "DUT identity" below.

### `manifest_files`

Every file that decided what the run tested, with its hash.

| Column | Type | Notes |
| --- | --- | --- |
| `run_id` | TEXT, not null | References `runs.id` |
| `path` | TEXT, not null | Relative to rootdir, `/` separators. A file outside rootdir keeps its absolute path |
| `role` | TEXT, not null | `interface`, `dbc`, `bench`, `harness`, `test`, `conftest` or `ini` |
| `sha256` | TEXT, not null | Of the file's bytes |
| `size` | INTEGER, not null | Bytes |

Primary key: `run_id, path`.

`conftest.py` files are included because staging fixtures live there, so
changing one can change results while the test files stay the same. The ini
file is included because it sets the record level and which files BenchTare
loads.

"Has this test file changed since it was last checked?" is then a comparison of
two runs' `manifest_files` rows, so the manifest needs no extra columns for M6.

### `selftests` and `selftest_checks`

The self-test, when it ran. A run where no test used the bench has no row.

`selftests`: `run_id` (primary key), `passed` (INTEGER), `started_at`,
`duration` (REAL, seconds).

`selftest_checks`: `run_id`, `seq` (order, from 1), `kind` (`equipment`,
`simulation`, `loopback`, `presence` or `bench`), `name`, `outcome` (`passed`,
`failed` or `skipped`), `detail`, `hint`. Primary key: `run_id, seq`.

### `test_results`

One row per test that ran.

| Column | Type | Notes |
| --- | --- | --- |
| `run_id` | TEXT, not null | |
| `seq` | INTEGER, not null | Order the tests ran in, from 1 |
| `nodeid` | TEXT, not null | pytest's ID, such as `tests/test_x.py::test_setpoint[42]` |
| `file` | TEXT, not null | The test file, as in `manifest_files.path` |
| `outcome` | TEXT, not null | `passed`, `failed`, `error` (setup or teardown failed), `skipped`, `xfailed` or `xpassed`, as pytest's summary counts them |
| `message` | TEXT | The one-line reason for a failure, error or skip |
| `details` | TEXT | The full failure report, as pytest prints it |
| `started_at` | TEXT, not null | |
| `setup_duration`, `call_duration`, `teardown_duration` | REAL | Seconds; null for a phase that didn't run |
| `properties` | TEXT | JSON: values from `record_property` |

Primary key: `run_id, seq`. Unique: `run_id, nodeid`.

### `record_entries`

The test record, one row per entry, for every test and for the self-test.

| Column | Type | Notes |
| --- | --- | --- |
| `run_id` | TEXT, not null | |
| `seq` | INTEGER, not null | Order in the run, from 1 |
| `test_seq` | INTEGER | The test it belongs to; null before the first test and after the last |
| `phase` | TEXT, not null | `setup`, `call`, `teardown`, `pretest` (the self-test and reading the DUT's identity, before the first test) or `after-last-test` |
| `at` | TEXT, not null | |
| `kind` | TEXT, not null | `read`, `wait_until`, `assert`, `result`, `write`, `identify`, `identity_change`, `frame_mismatch`, `equipment_failure`, `frame_sent`, `frame_received` |
| `level` | TEXT, not null | `results`, `stimuli` or `debug` |
| `text` | TEXT, not null | The line people read |
| `signal` | TEXT | Signal name, or channel for a loopback |
| `endpoint` | TEXT | Bench channel or bus |
| `unit` | TEXT | |
| `value` | TEXT | JSON, so `42`, `42.0`, `true` and `"run"` keep their types |
| `value_num` | REAL | The value as a number when it is one (bools as 0 and 1), for trends |
| `data` | TEXT | JSON: the entry's other facts (what was expected, the outcome, time waited, frame bytes) |

Primary key: `run_id, seq`. Indexes: `run_id, test_seq` (one test's record),
and `signal, at` (a signal's readings over time).

## DUT identity

The interface description lists the items every run is expected to collect,
by name only. It never holds their values:

```yaml
identity:
  hardware_revision:                 # given by the team's code or the command line
  bootloader: {signal: BootloaderVersion}
  application: {signal: AppVersion}  # read from this DUT output before the first test
```

Values can be entered in three ways, as the DUT and the bench allow:

- **From a signal.** An expected item that names a DUT output is read
  automatically once the self-test has passed, before the first test, and
  stored as text.
- **From the team's code**, at any point in the run, typically from a session
  fixture in `conftest.py`: `bench.identify("hardware_revision", "C")`, or
  several at once with a mapping. Versions that need a protocol to read (a
  diagnostic request, for example) are collected this way.
- **From the command line**, for what a person reads off a label:
  `pytest --identify hardware_revision=C`, repeatable.

In a CI/CD pipeline, the build being tested is usually known from environment
variables the pipeline sets. The recommended way to record it is either of the
last two: `--identify application=$FIRMWARE_VERSION` on the command line, or a
session fixture that reads the variables and calls `bench.identify`. BenchTare
never reads environment variables itself (see "No secrets" above). This is one
of the user guide's topics (design brief, later stage 12).

The list is a minimum, not a limit:

- **Any name can be added.** `bench.identify("motor.firmware", "2.3.1")`
  records an item the interface description doesn't list, so a DUT that
  reports its subsystems, or a version that only matters for some runs, needs
  no change to the file. Dotted names group related items.
- **Missing items.** Only listed items are expected. A listed item still
  without a value when the run ends is stored as missing and raised as a
  pytest warning. The tests' outcomes don't change. If a name that was recorded
  looks like the missing one (`bootloadr` for `bootloader`), the warning says
  so, since a typo in an added name would otherwise go unnoticed.
- **Changes during the run.** If an item gets a different value later in the
  run (a test that reflashes the DUT, say), both values are kept, in order, and
  the change is recorded as an unexpected event.
- **Tests can read them.** `bench.identity` gives the values collected so far,
  so a test can check a version like any other reading. BenchTare itself never
  judges them.

## Version 2 (M6): the test check

Migration 1 to 2 adds:

- **`test_checks`**: one row per rerun of a test check run (`runs.kind` is
  `check-tests`): the control, and each checked reading under each mode.

  | Column | Type | Notes |
  | --- | --- | --- |
  | `run_id` | TEXT, not null | |
  | `test_seq` | INTEGER, not null | The test, as `test_results.seq` |
  | `reading` | INTEGER, not null | The checked reading corrupted, from 1 in the order the test took them; 0 for the control |
  | `signal` | TEXT | The reading's signal; null for the control |
  | `location` | TEXT | The test's file and line that took it, such as `test_x.py:14`; null for the control |
  | `mode` | TEXT, not null | `control`, `shift` or `stale` |
  | `outcome` | TEXT, not null | `failed` (the step caught it), `passed`, `inconclusive` or `not applied`; for the control, `passed` (it repeats) or `inconclusive` |
  | `detail` | TEXT, not null | Where and why it failed, what it read when it passed, or why it proves nothing |

  Primary key: `run_id, test_seq, reading, mode`.
- **`test_results.check_verdict`** (TEXT): `able to fail`, `unable to fail`,
  `inconclusive` or `not checked`; null outside a test check run.
- **`manifest_files.last_checked_at`** (TEXT) and
  **`manifest_files.unchanged_since_check`** (INTEGER, 0 or 1), for test files:
  when the latest finished test check of the same DUT ran on that path, and
  whether the file's hash is the same now. Both null when it was never checked.
- **Each rerun's record** goes into `record_entries` with the phase named after
  the rerun: `control setup`, `control call`, `reading 2 shift call`,
  `reading 2 stale teardown`, and so on. A corrupted reading's `value` is what
  the test was given; `data` holds `corruption` (the mode) and `received`
  (what arrived).
- **Each reading's step**, in every run: `data.where` is the test's file and
  line that took it, and `data.checked` is `false` for a reading taken with
  `check=False`.

Version 2 also tells two `wait_until` outcomes apart in `data.outcome`:
`not reached` when values arrived but none matched (the test fails like an
`assert`), and `timeout` when nothing arrived. In version 1 rows, `timeout`
covers both.

## Later versions (designed now, built in their milestones)

- **Version 3, M8** — `test_requirements`: `run_id`, `test_seq`,
  `requirement_id`.

## Versions and migrations

- **A new file** is created at the current version.
- **An older file** is copied to `results.sqlite3.v<N>.bak` and then migrated
  step by step, inside one transaction. A failed migration leaves the original
  untouched.
- **A newer file** (written by a newer BenchTare) is refused, and nothing is
  written: "this results database is schema 3, and this BenchTare reads up to
  2. Check: upgrade BenchTare, or point `--results` at another file."
- **A file that isn't a BenchTare database**, or is corrupt, is refused with a
  message naming the file, and left exactly as it was. (Switching a file to
  SQLite's WAL mode rewrites its header, so that happens only once the file is
  known to be a BenchTare database.)
- **Two runs at once** on one file wait for each other (SQLite's WAL mode and a
  five-second busy timeout). That includes two runs that find the file new or
  older at the same moment: deciding whether to create or upgrade it happens
  while holding the write lock, so one run does it while the other waits, then
  finds it done. Running tests in parallel within one run is out of scope.
- **A write that fails stops the run.** If the file can't be written (another
  program holds it for longer than the timeout, the disk is full, the file has
  become read-only), the run stops with a message naming the file, what was
  being stored and what to check, and exits with code 2. A run that can't
  start its record (when the session starts) is a usage error, code 4. What
  was stored before stays, and the run stays `running`, like a run that
  crashed. A write that fails at the end, after every test has run, is shown in
  the summary and also gives code 2: a run whose results aren't kept doesn't
  pass.

## Where the file can be

- **Any folder on a local disk.** Folders that don't exist are made, and paths
  with spaces or non-ASCII characters work.
- **Relative paths:** a relative `--results` is from where pytest runs; a
  relative `benchtare_results` is from the folder of the ini file; the default
  is `.benchtare/results.sqlite3` under pytest's rootdir. `~` is the home
  folder.
- **Not a network drive.** SQLite can't safely share one file between machines
  over a network file system, and WAL mode needs every run on the same machine.
  Runs from several benches are collected in one place by the remote database,
  later.

## Decisions settled

1. **Readings are rows.** Each record entry is a row with the signal and value
   in columns, so trends in measured values are a query.
2. **The manifest covers the whole setup:** the interface description, DBC
   files, bench file, harness file, test files, `conftest.py` files and the ini
   file, plus the git commit and whether it had uncommitted changes. Each run
   also records its machine and software environment (`run_environment`,
   `run_packages`), for tracking down intermittent failures.
3. **The machine's name is recorded.** User names are not.
4. **On by default**, with `--no-results` to skip a run.
5. **Bench hardware is recorded**: each equipment item's vendor, model, serial
   number, firmware, driver and driver version, as far as its backend can
   report them (`run_equipment`).
6. **DUT identity is recorded per run** (`run_dut_identity`). The interface
   description lists the expected items by name; values come from signals, the
   team's code or the command line; any other name can be added during the run.
7. **A missing expected item is recorded and warned about**; outcomes don't
   change. Only items the interface description lists are expected.
8. **All of this is built in M5.**
