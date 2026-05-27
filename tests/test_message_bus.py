import threading
import time

from gcos.ipc.message_bus import MessageBus, resolve_input_placeholder


def test_send_then_recv_returns_payload():
    bus = MessageBus()
    bus.send(2, {"from_pid": 1, "kind": "result", "content": "hi"})
    msg = bus.recv(2, timeout=0.5)
    assert msg["content"] == "hi"
    assert msg["from_pid"] == 1


def test_recv_returns_none_on_timeout():
    bus = MessageBus()
    start = time.monotonic()
    assert bus.recv(99, timeout=0.1) is None
    assert (time.monotonic() - start) >= 0.05


def test_send_unblocks_a_blocked_recv():
    bus = MessageBus()
    received = []

    def consumer():
        msg = bus.recv(7, timeout=1.0)
        received.append(msg)

    t = threading.Thread(target=consumer)
    t.start()
    time.sleep(0.05)
    bus.send(7, {"from_pid": 1, "kind": "result", "content": "ok"})
    t.join(timeout=2.0)
    assert received and received[0]["content"] == "ok"


def test_has_pending_reflects_state():
    bus = MessageBus()
    assert bus.has_pending(5) is False
    bus.send(5, {"from_pid": 2, "kind": "result", "content": "x"})
    assert bus.has_pending(5) is True
    bus.recv(5, timeout=0.1)
    assert bus.has_pending(5) is False


def test_bounded_mailbox_drops_overflow():
    bus = MessageBus(mailbox_capacity=2)
    assert bus.send(1, {"kind": "x", "content": "1", "from_pid": 0})
    assert bus.send(1, {"kind": "x", "content": "2", "from_pid": 0})
    assert bus.send(1, {"kind": "x", "content": "3", "from_pid": 0}) is False


def test_resolve_input_placeholder_substitutes():
    assert resolve_input_placeholder(
        "Use this: {INPUT}", {"content": "DATA"}
    ) == "Use this: DATA"


def test_resolve_input_placeholder_no_substitute_when_absent():
    assert resolve_input_placeholder("no placeholder", {"content": "X"}) == "no placeholder"
    assert resolve_input_placeholder("hi", None) == "hi"


def test_snapshot_lists_pending_counts():
    bus = MessageBus()
    bus.send(1, {"kind": "r", "content": "a", "from_pid": 0})
    bus.send(1, {"kind": "r", "content": "b", "from_pid": 0})
    bus.send(2, {"kind": "r", "content": "c", "from_pid": 0})
    snap = bus.snapshot()
    assert snap[1] == 2
    assert snap[2] == 1
