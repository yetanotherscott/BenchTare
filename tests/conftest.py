import textwrap
import time

import can
import pytest

from benchtare.bench.runtime import open_bench

pytest_plugins = ["pytester"]


@pytest.fixture(autouse=True)
def _utf8_subprocess_output(monkeypatch):
    """pytester reads a subprocess's output as UTF-8, but on Windows Python writes it in the
    local code page, so a character such as the ± in pytest.approx can't be read back. Only the
    output's encoding changes; files are still read and written as the code under test says."""
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")


# A DUT output in a multiplexed message: a frame with a multiplexer value the DBC doesn't define
# can't be decoded, which is how these tests make the DUT send a frame that doesn't match.
MUX_DBC = """\
VERSION ""

NS_ :

BS_:

BU_: DUT BENCH

BO_ 3 MuxOut: 8 DUT
 SG_ OutSel M : 0|8@1+ (1,0) [0|255] "x" BENCH
 SG_ C m0 : 8|8@1+ (1,0) [0|255] "x" BENCH
"""

MUX_FILES = {
    "x.dbc": MUX_DBC,
    "x.dut.yaml": "dut: x\ncan: {node: DUT, dbc: x.dbc}\n",
    "bench.yaml": "bench: b\nequipment:\n  can0: {backend: sim_can}\n",
    "harness.yaml": "harness: h\nconnections:\n  C: can0\n",
}


@pytest.fixture
def mux_bench(tmp_path):
    """A bench listening for a multiplexed DUT output, and a way to send frames as the DUT.

    There is no simulated DUT: the test sends the frames. Yields (bench, send).
    """
    for name, text in MUX_FILES.items():
        (tmp_path / name).write_text(textwrap.dedent(text))
    with open_bench(
        tmp_path / "x.dut.yaml", tmp_path / "bench.yaml", tmp_path / "harness.yaml"
    ) as bench:
        bus = can.Bus(
            interface="virtual", channel=bench._backends["can0"].bus_name, ignore_config=True
        )

        def send(data):
            bus.send(can.Message(arbitration_id=3, data=bytes(data), is_extended_id=False))

        try:
            yield bench, send
        finally:
            bus.shutdown()


@pytest.fixture
def eventually():
    """Wait for a background thread to make ``condition()`` true."""

    def wait(condition, timeout=2.0):
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                raise AssertionError("condition not met in time")
            time.sleep(0.01)

    return wait


@pytest.fixture
def mux_message():
    """The multiplexed message, as cantools loads it."""
    import cantools

    return cantools.database.load_string(MUX_DBC, "dbc").get_message_by_name("MuxOut")
