"""Shared API quota — the OS-wide budget for LLM calls.

Workers acquire(1) before each Solar call. If exhausted, the worker may
re-queue the agent in BLOCKED state and wait for a refund (or for an admin
to top up via /kernel/quota/topup).
"""

from __future__ import annotations

import threading


class Quota:
    """Mutex-protected integer counter. Think `Semaphore` but introspectable."""

    def __init__(self, total: int) -> None:
        if total < 0:
            raise ValueError("total must be >= 0")
        self._total = total
        self._remaining = total
        self._lock = threading.Lock()
        self._refunded_event = threading.Condition(self._lock)

    def acquire(self, n: int = 1) -> bool:
        """Reserve `n` units. Returns False (without blocking) if not enough."""
        with self._lock:
            if self._remaining < n:
                return False
            self._remaining -= n
            return True

    def acquire_blocking(self, n: int = 1, timeout: float | None = None) -> bool:
        """Block until `n` units are available (or timeout)."""
        with self._refunded_event:
            deadline_ok = self._refunded_event.wait_for(
                lambda: self._remaining >= n, timeout=timeout
            )
            if not deadline_ok:
                return False
            self._remaining -= n
            return True

    def refund(self, n: int = 1) -> None:
        with self._refunded_event:
            self._remaining = min(self._total, self._remaining + n)
            self._refunded_event.notify_all()

    def topup(self, n: int) -> None:
        """Add capacity (raises ceiling). Used by admin /quota/topup."""
        with self._refunded_event:
            self._total += n
            self._remaining += n
            self._refunded_event.notify_all()

    @property
    def remaining(self) -> int:
        with self._lock:
            return self._remaining

    @property
    def total(self) -> int:
        with self._lock:
            return self._total

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "remaining": self._remaining,
                "total": self._total,
                "used": self._total - self._remaining,
            }
