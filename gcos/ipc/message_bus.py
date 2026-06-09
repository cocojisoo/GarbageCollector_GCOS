"""Per-PID message bus — the OS's IPC pipe.

The model is mailboxes, not channels:

    bus.send(target_pid, payload)         # producer drops a message
    bus.recv(my_pid, timeout=2.0)         # consumer pulls one

Each mailbox is a bounded queue (default 64). Worker threads use `recv` with
a timeout to avoid hard-blocking — if the upstream agent dies, the consumer
notices via timeout and can be reaped.

A `payload` is a dict — typically:
    {"from_pid": 3, "kind": "result", "content": "...", "ts": 1700000000.0}
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Optional


log = logging.getLogger(__name__)


class MessageBus:
    def __init__(self, mailbox_capacity: int = 64) -> None:
        self.cap = mailbox_capacity
        self._mailboxes: dict[int, queue.Queue] = {}
        self._lock = threading.Lock()

    def _mailbox(self, pid: int) -> queue.Queue:
        with self._lock:
            mb = self._mailboxes.get(pid)
            if mb is None:
                mb = queue.Queue(maxsize=self.cap)
                self._mailboxes[pid] = mb
            return mb

    # --- producer side -----------------------------------------------------

    def send(
        self,
        target_pid: int,
        payload: dict[str, Any],
        *,
        block: bool = False,
        timeout: Optional[float] = None,
    ) -> bool:
        """Deliver a message to a mailbox.

        Default is non-blocking (returns False if the mailbox is full). Pass
        `block=True` (with an optional timeout) for backpressure — the producer
        waits for room rather than silently dropping, so a lost pipe message
        can't strand a downstream consumer in WAITING forever (E16)."""
        mb = self._mailbox(target_pid)
        try:
            if block:
                mb.put(payload, timeout=timeout)
            else:
                mb.put_nowait(payload)
            log.debug("bus.send: -> %d %s", target_pid,
                      {k: v for k, v in payload.items() if k != "content"})
            return True
        except queue.Full:
            log.warning("bus.send: mailbox %d full, dropping message", target_pid)
            return False

    # --- consumer side -----------------------------------------------------

    def recv(self, pid: int, timeout: Optional[float] = None) -> Optional[dict]:
        """Blocking recv with timeout. Returns None on timeout."""
        mb = self._mailbox(pid)
        try:
            return mb.get(timeout=timeout)
        except queue.Empty:
            return None

    def has_pending(self, pid: int) -> bool:
        with self._lock:
            mb = self._mailboxes.get(pid)
            return mb is not None and not mb.empty()

    # --- helpers -----------------------------------------------------------

    def make_result(self, from_pid: int, content: str) -> dict:
        return {
            "from_pid": from_pid,
            "kind": "result",
            "content": content,
            "ts": time.time(),
        }

    def drop_mailbox(self, pid: int) -> None:
        with self._lock:
            self._mailboxes.pop(pid, None)

    def snapshot(self) -> dict[int, int]:
        """For dashboards: {pid: pending_count}."""
        with self._lock:
            return {pid: mb.qsize() for pid, mb in self._mailboxes.items()}


def resolve_input_placeholder(prompt: str, input_payload: Optional[dict]) -> str:
    """Substitute `{INPUT}` in a prompt with the content of an incoming msg."""
    if "{INPUT}" not in prompt:
        return prompt
    content = (input_payload or {}).get("content", "")
    return prompt.replace("{INPUT}", content)
