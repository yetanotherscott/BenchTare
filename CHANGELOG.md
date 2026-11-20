# Changelog

All notable changes to BenchTare are recorded here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and BenchTare adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While the version is 0.y.z, BenchTare is in initial development and anything
may change, as Semantic Versioning allows. Until 1.0.0, a release that breaks
something you rely on bumps y, and any other release bumps z.

## [Unreleased]

### Added

- `docs/testing.md`: how BenchTare tests itself, holding its own suite to
  the standard it asks of a team's tests. Recorded breaks prove each test
  can fail, and CI runs the whole suite against a slowed reference DUT, so
  no test's outcome depends on timing.

### Changed

- The documentation now says that a function given to `wait_until` is also
  called once on the value from before the write, to tell whether the DUT
  was already there (the hygiene check's meaningful start), so it should
  have no side effects; `check=False` leaves that call out.

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
