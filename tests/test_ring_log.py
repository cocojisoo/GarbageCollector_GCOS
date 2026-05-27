import logging

from gcos.kernel.ring_log import RingTraceLog


def test_append_and_snapshot():
    r = RingTraceLog(capacity=4)
    r.append("info", "first")
    r.append("info", "second", pid=1)
    snap = r.snapshot()
    assert [e["msg"] for e in snap] == ["first", "second"]
    assert snap[1]["pid"] == 1


def test_capacity_drops_oldest():
    r = RingTraceLog(capacity=3)
    for i in range(5):
        r.append("info", f"msg-{i}")
    snap = r.snapshot()
    assert [e["msg"] for e in snap] == ["msg-2", "msg-3", "msg-4"]
    assert len(r) == 3


def test_limit_clips_snapshot():
    r = RingTraceLog(capacity=10)
    for i in range(8):
        r.append("info", f"msg-{i}")
    last3 = r.snapshot(limit=3)
    assert [e["msg"] for e in last3] == ["msg-5", "msg-6", "msg-7"]


def test_as_logging_handler_captures_records():
    r = RingTraceLog(capacity=10)
    logger = logging.getLogger("gcos.testchannel")
    logger.setLevel(logging.INFO)
    r.attach_to_logger("gcos.testchannel")
    try:
        logger.info("hello")
        logger.warning("watch out")
        logger.error("boom")
    finally:
        r.detach()

    snap = r.snapshot()
    msgs = [e["msg"] for e in snap]
    assert "hello" in msgs
    assert "watch out" in msgs
    assert "boom" in msgs
    kinds = [e["kind"] for e in snap]
    assert "info" in kinds and "warning" in kinds and "error" in kinds


def test_clear_empties_buffer():
    r = RingTraceLog(capacity=4)
    r.append("info", "x")
    r.clear()
    assert len(r) == 0
    assert r.snapshot() == []
