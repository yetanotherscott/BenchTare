# Changelog

All notable changes to BenchTare are recorded here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and BenchTare adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While the version is 0.y.z, BenchTare is in initial development and anything
may change, as Semantic Versioning allows. Until 1.0.0, a release that breaks
something you rely on bumps y, and any other release bumps z.

## [Unreleased]

### Added

- `benchtare.api`, the plugin API: everything a backend plugin may import, at
  plugin API version 1. It is provisional until the release that first
  includes it.
- Backends are found through the `benchtare.backends` entry points, so an
  installed plugin package's backends can be named in a bench file.
  BenchTare's own `sim` and `sim_can` are found the same way.
- Each backend declares a UUID and the plugin API version it was written for.
  A bench file can name a backend by its UUID, to settle two packages
  providing the same name. Clashing names, copied UUIDs, broken plugins and
  unsupported API versions are refused with what to do.
- Results schema version 4: each run records the plugins it used, with their
  UUIDs and the packages they came from. Older databases are upgraded, and
  their runs get no plugin rows.

### Changed

- Backends are no longer a fixed list inside BenchTare. A source checkout must
  be installed (`pip install -e .`) for its own backends to be found.
- The documentation gives "transport" one meaning: the encoding between a
  DUT's signals and what a bus carries, such as DBC frames on CAN. CAN
  interfaces, serial ports and other equipment are reached through backends.
  It also defines verified, attested and asserted, ahead of results carrying
  those labels.

## [0.0.2] - 2026-11-16

### Added

- This changelog, linked from PyPI's page.

### Changed

- The licence is declared as the SPDX expression `MIT`, so PyPI shows "MIT"
  instead of the whole licence text. The LICENSE file still ships in both
  packages.
- PyPI's page links to the documentation, and its classifiers list each
  supported Python version, the CAN and embedded-systems topics, and the
  console tool.

## [0.0.1] - 2026-11-16

The first slice. Everything in it runs on a laptop, against a simulated DUT.

### Added

- A pytest plugin. Describe the DUT, the bench and the harness in three files,
  then write plain pytest tests against named signals through the `bench` and
  `signals` fixtures, with no BenchTare imports.
- Interface descriptions: the DUT's signals listed inline, imported from DBC
  files, or both, each with its type, unit and direction.
- The self-test before every run: the bench check, and the harness check's
  loopbacks and DUT presence checks.
- `benchtare check-hygiene`, which proves each test repeats, leaves the DUT
  clean, and starts from a meaningful state.
- `benchtare check-tests`, which proves each test can fail at every check, by
  corrupting what it reads, one reading at a time.
- A local results database (SQLite) recording every run: each test's record,
  and a manifest of versions, file hashes, the machine, the bench equipment and
  the DUT's identity.
- Simulated equipment (the `sim` and `sim_can` backends), the reference DUT,
  bench and harness, and DUT models written in Python by subclassing
  `benchtare.DutModel`.
- `benchtare init`, which writes a quickstart project on the reference DUT, and
  `benchtare selftest`, the self-test on its own.
- Support for Linux, Windows and macOS, with Python 3.10 or later and pytest
  7.4 or later.

[Unreleased]: https://github.com/yetanotherscott/BenchTare/compare/v0.0.2...HEAD
[0.0.2]: https://github.com/yetanotherscott/BenchTare/compare/v0.0.1...v0.0.2
[0.0.1]: https://github.com/yetanotherscott/BenchTare/releases/tag/v0.0.1
