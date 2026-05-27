"""Monotonic, thread-safe PID allocator."""

from __future__ import annotations

import itertools
import threading


class PidAllocator:
    """Hands out fresh PIDs. PID 0 is reserved (init / idle)."""

    def __init__(self, start: int = 1) -> None:
        self._counter = itertools.count(start)
        self._lock = threading.Lock()

    def next(self) -> int:
        with self._lock:
            return next(self._counter)

    def peek(self) -> int:
        """Return the *next* PID without consuming it.

        Useful when two agents need to know each other's PIDs at spawn time
        (e.g. a producer/consumer pair wired by pipe_to and input_from).
        """
        with self._lock:
            pid = next(self._counter)
            # Replay: rebuild the counter so the same PID comes out next call.
            self._counter = itertools.count(pid)
            return pid


_default = PidAllocator()


def next_pid() -> int:
    """Module-level convenience."""
    return _default.next()
