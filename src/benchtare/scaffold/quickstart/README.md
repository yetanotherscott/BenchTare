# A BenchTare quickstart

A complete BenchTare project, on the simulated reference DUT that ships with BenchTare: no
hardware needed.

| File | What it is |
| --- | --- |
| `reference.dut.yaml` | The interface description: the DUT's signals, and what shows it is clean |
| `reference.dbc` | The DUT's CAN messages, which the interface description imports |
| `bench-sim.yaml` | The bench file: a simulated CAN interface and simulated I/O |
| `harness-can.yaml` | The harness file: which signal is on which channel, and the simulated DUT |
| `conftest.py` | Staging: fixtures that put the DUT into the state a test needs |
| `test_reference.py` | One test |
| `pytest.ini` | Where BenchTare finds the three files |

Run it:

```bash
pytest                     # the self-test, then the test
benchtare check-hygiene    # the test stands on its own
benchtare check-tests      # the test can fail at every check
```

Then try breaking things, to see what each check catches:

- Change `42.0` in the test's `wait_until` to `43.0`: the test fails, and its record shows
  what the DUT reported.
- Replace the `wait_until` with `bench.read(signals.Measured, timeout=1.0)`, which checks
  nothing: the test still passes, and `benchtare check-tests` says it can't fail.
- Remove the lines after `yield` in `conftest.py`: the test still passes, and
  `benchtare check-hygiene` says it leaves the DUT in run mode.

Every run is stored in `.benchtare/results.sqlite3`. To learn what each file can hold, see
the BenchTare model: https://github.com/yetanotherscott/BenchTare/blob/main/docs/model.md
