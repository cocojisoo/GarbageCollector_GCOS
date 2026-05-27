import threading
import time

import pytest

from gcos.kernel.quota import Quota


def test_initial_state():
    q = Quota(10)
    assert q.remaining == 10
    assert q.total == 10
    assert q.snapshot() == {"remaining": 10, "total": 10, "used": 0}


def test_acquire_decrements():
    q = Quota(5)
    assert q.acquire(2) is True
    assert q.remaining == 3
    assert q.acquire(3) is True
    assert q.remaining == 0


def test_acquire_fails_when_insufficient():
    q = Quota(2)
    assert q.acquire(3) is False
    assert q.remaining == 2  # nothing consumed on failure


def test_refund_caps_at_total():
    q = Quota(5)
    q.acquire(3)
    q.refund(10)
    assert q.remaining == 5  # capped


def test_topup_raises_ceiling():
    q = Quota(5)
    q.acquire(5)
    q.topup(10)
    assert q.total == 15
    assert q.remaining == 10


def test_acquire_blocking_unblocks_on_refund():
    q = Quota(2)
    q.acquire(2)  # exhaust

    result: list = []

    def waiter():
        ok = q.acquire_blocking(1, timeout=2.0)
        result.append(ok)

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.1)  # let waiter park
    q.refund(1)
    t.join(timeout=3.0)
    assert result == [True]
    assert q.remaining == 0


def test_acquire_blocking_times_out():
    q = Quota(1)
    q.acquire(1)
    assert q.acquire_blocking(1, timeout=0.1) is False


def test_negative_total_rejected():
    with pytest.raises(ValueError):
        Quota(-1)


def test_concurrent_acquires_dont_double_spend():
    q = Quota(100)
    barrier = threading.Barrier(20)
    wins = [0]
    lock = threading.Lock()

    def worker():
        barrier.wait()
        if q.acquire(1):
            with lock:
                wins[0] += 1

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert wins[0] == 20
    assert q.remaining == 80
