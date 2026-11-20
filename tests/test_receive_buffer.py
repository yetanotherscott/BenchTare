"""The receive buffer, which implements the read rule. Deterministic: no simulation, no timing."""

import threading
import time

from benchtare.backends.base import ReceiveBuffer


def test_first_value_after_write_is_returned():
    buffer = ReceiveBuffer()
    with buffer.writing():
        pass
    buffer.deliver("Measured", 1)
    buffer.deliver("Measured", 2)
    assert buffer.first_after_write("Measured", timeout=0).value == 1


def test_value_from_before_the_write_is_never_returned():
    buffer = ReceiveBuffer()
    buffer.deliver("Measured", "stale")
    with buffer.writing():
        pass
    assert buffer.first_after_write("Measured", timeout=0.05) is None
    buffer.deliver("Measured", "fresh")
    assert buffer.first_after_write("Measured", timeout=0).value == "fresh"


def test_reply_delivered_during_the_write_counts_as_after_it():
    # A fast DUT can answer before write() has returned. Its delivery waits for the lock and
    # then counts as after the write, so it isn't missed.
    buffer = ReceiveBuffer()
    buffer.deliver("Measured", "stale")
    replied = threading.Event()

    def reply():
        buffer.deliver("Measured", "reply")
        replied.set()

    with buffer.writing():
        thread = threading.Thread(target=reply)
        thread.start()
        assert not replied.wait(0.05)  # blocked until the write finishes
    thread.join(1)
    assert buffer.first_after_write("Measured", timeout=0).value == "reply"


def test_reply_delivered_before_read_is_called_is_not_missed():
    buffer = ReceiveBuffer()
    with buffer.writing():
        pass
    buffer.deliver("Measured", "early")
    # read() is called only now.
    assert buffer.first_after_write("Measured", timeout=0).value == "early"


def test_first_after_write_waits_for_a_value():
    buffer = ReceiveBuffer()
    with buffer.writing():
        pass
    timer = threading.Timer(0.05, buffer.deliver, args=("Measured", 7))
    timer.start()
    assert buffer.first_after_write("Measured", timeout=2).value == 7


def test_first_after_write_times_out():
    buffer = ReceiveBuffer()
    assert buffer.first_after_write("Measured", timeout=0.02) is None


def test_wait_for_match_skips_non_matching_values():
    buffer = ReceiveBuffer()
    with buffer.writing():
        pass
    for value in (0, 10, 41.9, 42.1):
        buffer.deliver("Measured", value)
    match = buffer.wait_for_match("Measured", lambda v: v > 42, timeout=0)
    assert match.value == 42.1


def test_wait_for_match_ignores_values_from_before_the_write():
    buffer = ReceiveBuffer()
    buffer.deliver("Mode", "run")
    with buffer.writing():
        pass
    assert buffer.wait_for_match("Mode", lambda v: v == "run", timeout=0.02) is None


def after_checking(checked, count, deliver):
    """Deliver once the wait has checked ``count`` values, so it is already waiting: the
    outcome doesn't depend on how quickly the thread runs."""

    def run():
        deadline = time.monotonic() + 2
        while len(checked) < count and time.monotonic() < deadline:
            time.sleep(0.001)
        deliver()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_wait_for_match_waits_for_later_values():
    buffer = ReceiveBuffer()
    with buffer.writing():
        pass
    buffer.deliver("Mode", "standby")
    checked = []

    def is_run(value):
        checked.append(value)
        return value == "run"

    thread = after_checking(checked, 1, lambda: buffer.deliver("Mode", "run"))
    assert buffer.wait_for_match("Mode", is_run, timeout=2).value == "run"
    thread.join(2)
    assert checked == ["standby", "run"]


def test_wait_for_match_checks_each_value_once_in_order():
    # So a test can count values with a predicate, such as five heartbeats.
    buffer = ReceiveBuffer()
    with buffer.writing():
        pass
    buffer.deliver("Mode", "a")
    buffer.deliver("Mode", "b")
    checked = []

    def third(value):
        checked.append(value)
        return len(checked) == 3

    thread = after_checking(checked, 2, lambda: buffer.deliver("Mode", "c"))
    assert buffer.wait_for_match("Mode", third, timeout=2).value == "c"
    thread.join(2)
    assert checked == ["a", "b", "c"]


def test_last_remembers_values_across_writes():
    buffer = ReceiveBuffer()
    buffer.deliver("Mode", "standby")
    with buffer.writing():
        pass
    assert buffer.last("Mode").value == "standby"
    assert buffer.last("Other") is None


def test_keys_are_independent():
    buffer = ReceiveBuffer()
    with buffer.writing():
        pass
    buffer.deliver("A", 1)
    assert buffer.first_after_write("B", timeout=0.02) is None
