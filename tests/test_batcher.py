"""Batcher tests with a fake underlying SolarClient (no network)."""

from __future__ import annotations

import threading
import time

from gcos.backend.batcher import BatchingSolarClient, TokenBucket
from gcos.backend.solar_client import ChatResult


class FakeInner:
    """Stand-in for SolarClient. Sleeps `delay_s` to simulate an API call."""
    def __init__(self, delay_s: float = 0.05) -> None:
        self.delay_s = delay_s
        self.calls = 0
        self.max_in_flight_observed = 0
        self.in_flight = 0
        self._lock = threading.Lock()

    def chat(self, messages, *, temperature=0.2, max_tokens=1024, timeout=30.0):
        with self._lock:
            self.in_flight += 1
            if self.in_flight > self.max_in_flight_observed:
                self.max_in_flight_observed = self.in_flight
        try:
            time.sleep(self.delay_s)
            with self._lock:
                self.calls += 1
            return ChatResult(content="ok", prompt_tokens=1,
                              completion_tokens=1, total_tokens=2, model="fake")
        finally:
            with self._lock:
                self.in_flight -= 1


def _batched(max_concurrent=2, rate=100.0, delay=0.05) -> BatchingSolarClient:
    inner = FakeInner(delay_s=delay)
    return BatchingSolarClient(
        max_concurrent=max_concurrent,
        rate_per_s=rate,
        underlying=inner,
    ), inner


def test_basic_chat_delegates_to_inner():
    bc, inner = _batched()
    r = bc.chat([{"role": "user", "content": "hi"}])
    assert r.content == "ok"
    assert inner.calls == 1
    assert bc.stats["total_calls"] == 1


def test_concurrency_cap_is_enforced():
    bc, inner = _batched(max_concurrent=3, rate=1000.0, delay=0.1)

    def call():
        bc.chat([{"role": "user", "content": "x"}])

    threads = [threading.Thread(target=call) for _ in range(10)]
    for t in threads: t.start()
    for t in threads: t.join()

    assert inner.calls == 10
    assert inner.max_in_flight_observed <= 3
    assert bc.stats["peak_in_flight"] <= 3


def test_token_bucket_smooths_burst():
    bucket = TokenBucket(rate_per_s=10.0, burst=2)
    # Drain burst
    assert bucket.acquire(2, timeout=0.01)
    # Next acquire must wait ~0.1s for refill
    start = time.monotonic()
    assert bucket.acquire(1, timeout=1.0)
    elapsed = time.monotonic() - start
    assert elapsed >= 0.08, f"expected ~0.1s wait, got {elapsed}"


def test_stats_reports_wait_and_calls():
    bc, _ = _batched(max_concurrent=1, rate=1000.0, delay=0.02)
    for _ in range(3):
        bc.chat([{"role": "user", "content": "x"}])
    s = bc.stats
    assert s["total_calls"] == 3
    assert s["total_chat_s"] > 0
    assert s["peak_in_flight"] >= 1


def test_error_is_recorded_and_reraised():
    class Boom:
        def chat(self, *a, **kw):
            raise RuntimeError("kaboom 429 rate limit hit")

    bc = BatchingSolarClient(max_concurrent=1, rate_per_s=100.0, underlying=Boom())
    raised = False
    try:
        bc.chat([{"role": "user", "content": "x"}])
    except RuntimeError as e:
        raised = True
        assert "kaboom" in str(e)
    assert raised
    s = bc.stats
    assert "kaboom" in s["last_error"]
    assert s["last_429_ts"] is not None
