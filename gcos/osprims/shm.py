"""shm.py — IPC over real POSIX shared memory.

The MessageBus used `queue.Queue` — fine within one process, but it's not OS
IPC. This backs a mailbox with a `multiprocessing.shared_memory.SharedMemory`
segment (a real `shm_open(3)` object under /dev/shm on Linux), so two genuinely
separate OS processes can exchange bytes through kernel-managed shared memory —
the actual mechanism the project claims under "IPC".

A tiny single-producer/single-consumer ring lives in the segment: a header
(capacity, head, tail) plus a byte ring. Cross-process safe for the 1:1 pipe
pattern GCOS uses (producer -> consumer). Works on macOS and Linux.
"""

from __future__ import annotations

import logging
import struct
from multiprocessing import shared_memory
from typing import Optional


log = logging.getLogger(__name__)

_HDR = struct.Struct("<QQQ")  # capacity, head, tail (byte offsets, monotonic)


def _attach_untracked(name: str) -> "shared_memory.SharedMemory":
    """Attach an existing segment without registering it with the resource
    tracker (so a consumer never unlinks the producer's segment on exit)."""
    try:
        # Python 3.13+: the supported way to opt out of tracking.
        return shared_memory.SharedMemory(name=name, create=False, track=False)
    except TypeError:
        # Older Pythons lack the `track` kwarg: attach, then unregister from the
        # resource tracker. `_name` is the leading-slash key the tracker uses.
        shm = shared_memory.SharedMemory(name=name, create=False)
        try:
            from multiprocessing import resource_tracker
            resource_tracker.unregister(shm._name, "shared_memory")
        except Exception:  # noqa: BLE001 — best-effort; never fail an attach
            pass
        return shm


class ShmRing:
    """A byte ring buffer in a shared-memory segment (1 producer, 1 consumer).

    `create=True` allocates a new segment; another process attaches with the
    same `name` and `create=False`. Messages are length-prefixed frames so the
    consumer can recover record boundaries.
    """

    def __init__(self, name: Optional[str] = None, *, create: bool = True,
                 capacity: int = 1 << 16) -> None:
        self.capacity = capacity
        size = _HDR.size + capacity
        if create:
            self.shm = shared_memory.SharedMemory(name=name, create=True, size=size)
            _HDR.pack_into(self.shm.buf, 0, capacity, 0, 0)
        else:
            # Attach as a *consumer* without taking lifecycle ownership. Without
            # this, CPython's resource_tracker registers the attached segment in
            # the consumer process and UNLINKS it when the consumer exits — which
            # would destroy the producer's still-live segment out from under it
            # (and warn about a "leaked" object). Only the creator should unlink.
            self.shm = _attach_untracked(name)
            self.capacity = _HDR.unpack_from(self.shm.buf, 0)[0]
        self.name = self.shm.name
        self._created = create

    def _hdr(self) -> tuple[int, int, int]:
        return _HDR.unpack_from(self.shm.buf, 0)

    def _set(self, head: int, tail: int) -> None:
        cap = self.capacity
        _HDR.pack_into(self.shm.buf, 0, cap, head, tail)

    def _used(self) -> int:
        _cap, head, tail = self._hdr()
        return head - tail

    def send(self, payload: bytes) -> bool:
        """Enqueue a length-prefixed frame. False if it wouldn't fit (no overwrite)."""
        cap, head, tail = self._hdr()
        frame = struct.pack("<I", len(payload)) + payload
        if len(frame) > cap:
            raise ValueError("payload larger than ring capacity")
        if (head - tail) + len(frame) > cap:
            return False  # full — backpressure, like a full mailbox
        base = _HDR.size
        for i, b in enumerate(frame):
            self.shm.buf[base + (head + i) % cap] = b
        self._set(head + len(frame), tail)
        return True

    def recv(self) -> Optional[bytes]:
        """Dequeue one frame, or None if empty."""
        cap, head, tail = self._hdr()
        if head - tail < 4:
            return None
        base = _HDR.size
        lb = bytes(self.shm.buf[base + (tail + i) % cap] for i in range(4))
        (n,) = struct.unpack("<I", lb)
        if head - tail < 4 + n:
            return None  # partial frame (shouldn't happen with 1:1 framing)
        start = tail + 4
        data = bytes(self.shm.buf[base + (start + i) % cap] for i in range(n))
        self._set(head, tail + 4 + n)
        return data

    def pending_bytes(self) -> int:
        return self._used()

    def close(self) -> None:
        self.shm.close()
        if self._created:
            try:
                self.shm.unlink()
            except FileNotFoundError:
                pass

    def __enter__(self) -> "ShmRing":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
