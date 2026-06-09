"""vmem.py — real demand paging with mmap + madvise.

The userspace pager called its `ContextPage` list "virtual memory" but it was
just a Python list in the heap. This backs page *bodies* with a real
file-backed `mmap`, so:

  - storing a page writes into a memory-mapped file (real page cache),
  - `page_out()` calls `madvise(MADV_DONTNEED)` — the kernel *drops the resident
    physical page*; the data survives only in the backing file,
  - the next `read()` of that page faults it back in from the file — a **real
    page fault**, counted on Linux via /proc/self/stat majflt/minflt.

So GCOS's "swap" stops being a JSON copy and becomes the OS mechanism it claims
to be: demand paging against a backing store, with the kernel doing the fault.
Portable across POSIX (madvise(MADV_DONTNEED) on Linux, MADV_FREE on Darwin).
"""

from __future__ import annotations

import logging
import mmap
import os
import tempfile
from dataclasses import dataclass
from typing import Optional


log = logging.getLogger(__name__)

PAGE = mmap.PAGESIZE  # real hardware page size (4096 on most hosts)


def _madv_dontneed() -> Optional[int]:
    # Prefer MADV_DONTNEED (Linux: drops the page, faults back from file).
    # Darwin lacks MADV_DONTNEED-with-reload semantics for file maps the same
    # way but exposes MADV_FREE; either gives us a real page-out lever.
    for name in ("MADV_DONTNEED", "MADV_FREE", "MADV_PAGEOUT"):
        v = getattr(mmap, name, None)
        if v is not None:
            return v
    return None


def _self_faults() -> tuple[int, int]:
    """(minflt, majflt) for this process from /proc/self/stat (Linux), else (0,0).
    Real kernel fault counters — our evidence that page-out/in actually faulted."""
    try:
        with open("/proc/self/stat", "r") as fh:
            fields = fh.read().rsplit(")", 1)[1].split()
        # After the comm field, index 0 = state; minflt is field 9, majflt 11
        # in the canonical proc(5) layout, i.e. offsets 7 and 9 in this slice.
        return (int(fields[7]), int(fields[9]))
    except (FileNotFoundError, IndexError, ValueError, PermissionError):
        return (0, 0)


@dataclass
class PageRef:
    offset: int   # byte offset into the mmap region
    length: int   # actual content length (<= slot size)
    slots: int    # number of PAGE-sized slots reserved
    resident: bool = True


class MmapPageStore:
    """A file-backed mmap region partitioned into page-sized slots.

    Pages are addressed by an opaque key. `page_out(key)` evicts the resident
    physical pages via madvise; `read(key)` faults them back in. `fault_stats()`
    reports app-level fault-ins plus real kernel min/maj fault deltas on Linux.
    """

    def __init__(self, capacity_pages: int = 4096, *, path: Optional[str] = None) -> None:
        self.capacity_bytes = capacity_pages * PAGE
        if path is None:
            fd, path = tempfile.mkstemp(prefix="gcos-vmem-", suffix=".swap")
            os.close(fd)
            self._owns_path = True
        else:
            self._owns_path = False
        self.path = path
        # Clean up the fd + owned temp file if mmap/open fails mid-construction,
        # since close()/__exit__ won't run when __init__ raises.
        self._fd = -1
        try:
            with open(path, "wb") as fh:
                fh.truncate(self.capacity_bytes)
            self._fd = os.open(path, os.O_RDWR)
            self._mm = mmap.mmap(self._fd, self.capacity_bytes)
        except BaseException:
            if self._fd != -1:
                os.close(self._fd)
            if self._owns_path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            raise
        self._madv = _madv_dontneed()
        self._next_off = 0
        self._pages: dict[str, PageRef] = {}
        self._fault_ins = 0
        self._page_outs = 0
        self._base_min, self._base_maj = _self_faults()

    # --- store / load ------------------------------------------------------

    def store(self, key: str, data: bytes) -> PageRef:
        slots = max(1, (len(data) + PAGE - 1) // PAGE)
        need = slots * PAGE
        if self._next_off + need > self.capacity_bytes:
            raise MemoryError("MmapPageStore out of slots (raise capacity_pages)")
        off = self._next_off
        self._next_off += need
        self._mm[off:off + len(data)] = data
        ref = PageRef(offset=off, length=len(data), slots=slots, resident=True)
        self._pages[key] = ref
        return ref

    def page_out(self, key: str) -> bool:
        """Evict the resident physical pages for `key` (madvise). The bytes
        remain in the backing file; the RAM is reclaimed by the kernel."""
        ref = self._pages.get(key)
        if ref is None or not ref.resident:
            return False
        if self._madv is not None:
            try:
                self._mm.madvise(self._madv, ref.offset, ref.slots * PAGE)
            except (OSError, ValueError) as e:
                log.debug("vmem: madvise failed for %s: %s", key, e)
        ref.resident = False
        self._page_outs += 1
        return True

    def read(self, key: str) -> bytes:
        """Read a page; if it was paged out this faults it back in from the file."""
        ref = self._pages.get(key)
        if ref is None:
            raise KeyError(key)
        if not ref.resident:
            self._fault_ins += 1
            ref.resident = True  # touching it below makes it resident again
        return bytes(self._mm[ref.offset:ref.offset + ref.length])

    # --- introspection -----------------------------------------------------

    def resident_pages(self) -> int:
        return sum(1 for r in self._pages.values() if r.resident)

    def fault_stats(self) -> dict:
        cur_min, cur_maj = _self_faults()
        return {
            "pages_tracked": len(self._pages),
            "resident": self.resident_pages(),
            "app_page_outs": self._page_outs,
            "app_fault_ins": self._fault_ins,
            "kernel_minflt_delta": max(0, cur_min - self._base_min),
            "kernel_majflt_delta": max(0, cur_maj - self._base_maj),
            "page_size": PAGE,
        }

    def close(self) -> None:
        try:
            self._mm.close()
        finally:
            os.close(self._fd)
            if self._owns_path:
                try:
                    os.unlink(self.path)
                except OSError:
                    pass

    def __enter__(self) -> "MmapPageStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
