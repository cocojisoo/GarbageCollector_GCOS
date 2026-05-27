"""Rate-limited / concurrency-capped Solar wrapper.

Solar Pro 3 has no batch endpoint, so what "batching" means in GCOS is:

  1. A **concurrency semaphore** caps the number of in-flight chat calls — the
     kernel never overloads the API even if 50 agents wake up at once.

  2. A **token-bucket rate limiter** smooths requests across a 1-second window
     (default 5 req/s) so we stay under Upstage's per-second limit without
     bursting.

  3. **Stats** counters expose `in_flight`, `total_calls`, `wait_seconds`,
     `last_429_ts` to the dashboard — letting reviewers see the OS managing
     the LLM "device".

From an agent's point of view this is just another `SolarClient` — same
`.chat(messages, ...)` signature. The throttling is invisible except via
latency spent inside `acquire()`.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from gcos.backend.solar_client import ChatResult, SolarClient, SolarConfig


log = logging.getLogger(__name__)


@dataclass
class BatcherStats:
    total_calls: int = 0
    total_wait_s: float = 0.0
    total_chat_s: float = 0.0
    in_flight: int = 0
    peak_in_flight: int = 0
    last_429_ts: Optional[float] = None
    last_error: Optional[str] = None

    def snapshot(self) -> dict:
        return {
            "total_calls": self.total_calls,
            "total_wait_s": round(self.total_wait_s, 3),
            "avg_wait_ms": round(
                (self.total_wait_s / self.total_calls * 1000) if self.total_calls else 0.0,
                2,
            ),
            "total_chat_s": round(self.total_chat_s, 3),
            "in_flight": self.in_flight,
            "peak_in_flight": self.peak_in_flight,
            "last_429_ts": self.last_429_ts,
            "last_error": self.last_error,
        }


class TokenBucket:
    """Classic per-second token bucket."""

    def __init__(self, rate_per_s: float, burst: Optional[int] = None) -> None:
        self.rate = rate_per_s
        self.capacity = float(burst if burst is not None else max(1, int(rate_per_s)))
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, n: int = 1, timeout: Optional[float] = None) -> bool:
        deadline = (time.monotonic() + timeout) if timeout is not None else None
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                self._last = now
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
                if self._tokens >= n:
                    self._tokens -= n
                    return True
                missing = n - self._tokens
                wait = missing / self.rate
            if deadline is not None and time.monotonic() + wait > deadline:
                return False
            time.sleep(min(wait, 0.25))


class BatchingSolarClient:
    """Thread-safe Solar wrapper with concurrency cap + token bucket."""

    def __init__(
        self,
        *,
        config: Optional[SolarConfig] = None,
        max_concurrent: int = 4,
        rate_per_s: float = 5.0,
        burst: Optional[int] = None,
        underlying: Optional[SolarClient] = None,
    ) -> None:
        self._inner = underlying or SolarClient(config=config)
        self._sem = threading.BoundedSemaphore(max_concurrent)
        self._bucket = TokenBucket(rate_per_s=rate_per_s, burst=burst)
        self._stats = BatcherStats()
        self._stats_lock = threading.Lock()
        self.max_concurrent = max_concurrent
        self.rate_per_s = rate_per_s

    @property
    def stats(self) -> dict:
        with self._stats_lock:
            return self._stats.snapshot()

    @property
    def config(self):  # passthrough for code that introspects .config
        return self._inner.config

    def chat(self, messages, *, temperature: float = 0.2,
             max_tokens: int = 1024, timeout: float = 30.0) -> ChatResult:
        t0 = time.monotonic()
        # Order matters: rate-limit first (cheap), then take a concurrency slot.
        self._bucket.acquire(1)
        self._sem.acquire()
        t1 = time.monotonic()
        with self._stats_lock:
            self._stats.in_flight += 1
            if self._stats.in_flight > self._stats.peak_in_flight:
                self._stats.peak_in_flight = self._stats.in_flight
            self._stats.total_wait_s += (t1 - t0)
            self._stats.total_calls += 1

        try:
            result = self._inner.chat(
                messages, temperature=temperature, max_tokens=max_tokens, timeout=timeout,
            )
            with self._stats_lock:
                self._stats.total_chat_s += (time.monotonic() - t1)
                self._stats.last_error = None
            return result
        except Exception as e:  # noqa: BLE001
            with self._stats_lock:
                self._stats.last_error = f"{type(e).__name__}: {e}"
                msg = str(e)
                if "429" in msg or "rate" in msg.lower():
                    self._stats.last_429_ts = time.time()
            raise
        finally:
            with self._stats_lock:
                self._stats.in_flight -= 1
            self._sem.release()
