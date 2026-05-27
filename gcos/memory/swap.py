"""SwapEvictionPolicy — write oldest pages to disk for later reload.

This is the closest thing GCOS has to OS swap: when context overflows and
LRU has hit its min_keep floor, we serialize a batch of pages to a JSON
file under `logs/swap/<pid>/` and remove them from the in-memory PCB.

Reloading is currently manual (debug only) via `swap_in(pcb, swap_dir)` —
M5 may add an automatic prefetcher.
"""

from __future__ import annotations

import json
import logging
import pathlib
import time
from typing import Optional

from gcos.kernel.pcb import AgentControlBlock, ContextPage
from gcos.memory.policy import EvictionPolicy


log = logging.getLogger(__name__)


DEFAULT_SWAP_DIR = pathlib.Path("logs") / "swap"


def _serialize(page: ContextPage) -> dict:
    return {
        "role": page.role,
        "content": page.content,
        "tokens": page.tokens,
        "pinned": page.pinned,
        "summarized": page.summarized,
        "last_access": page.last_access,
    }


def _deserialize(d: dict) -> ContextPage:
    return ContextPage(
        role=d["role"], content=d["content"], tokens=d.get("tokens", 0),
        pinned=d.get("pinned", False), summarized=d.get("summarized", False),
        last_access=d.get("last_access", time.time()),
    )


class SwapEvictionPolicy(EvictionPolicy):
    name = "swap"

    def __init__(self, swap_dir: pathlib.Path | str = DEFAULT_SWAP_DIR,
                 batch_size: int = 2, min_keep: int = 2) -> None:
        self.swap_dir = pathlib.Path(swap_dir)
        self.batch_size = batch_size
        self.min_keep = min_keep

    def evict(self, pcb: AgentControlBlock, *, client: Optional[object] = None) -> int:
        non_pinned = [p for p in pcb.context_pages if not p.pinned]
        if len(non_pinned) <= self.min_keep:
            return 0

        non_pinned.sort(key=lambda p: p.last_access)
        batch = non_pinned[: self.batch_size]

        target_dir = self.swap_dir / str(pcb.pid)
        target_dir.mkdir(parents=True, exist_ok=True)
        # One JSON file per swap-out event so we don't overwrite history
        ts = f"{time.time():.6f}"
        path = target_dir / f"{ts}.json"
        path.write_text(
            json.dumps([_serialize(p) for p in batch], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        for p in batch:
            pcb.context_pages.remove(p)

        log.info("swap-out: PID %d wrote %d pages to %s", pcb.pid, len(batch), path)
        return len(batch)


def swap_in(pcb: AgentControlBlock,
            swap_dir: pathlib.Path | str = DEFAULT_SWAP_DIR) -> int:
    """Load all swapped pages for a PID back into context_pages (debug helper)."""
    target_dir = pathlib.Path(swap_dir) / str(pcb.pid)
    if not target_dir.is_dir():
        return 0
    loaded = 0
    for fpath in sorted(target_dir.iterdir()):
        if fpath.suffix != ".json":
            continue
        for d in json.loads(fpath.read_text(encoding="utf-8")):
            pcb.context_pages.append(_deserialize(d))
            loaded += 1
    return loaded
