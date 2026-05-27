"""RingTraceLog — fixed-size circular buffer of recent kernel events.

Like Linux's dmesg. Bounded, never grows unbounded, no disk I/O. Useful for
the dashboard's "recent events" pane and for `dmesg` in the REPL.

Two usage modes:

  1. Direct `log.append("event", "pid=3 -> pid=2 piped 303B")` calls from the
     kernel internals.

  2. As a Python `logging.Handler` — attach it and *every* log record from
     `gcos.*` gets a copy:

        ring = RingTraceLog(256)
        ring.attach_to_logger("gcos")

Records are stored as small dicts so the SSE / REST layers can JSON-encode
the snapshot directly.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Optional


class RingTraceLog(logging.Handler):
    def __init__(self, capacity: int = 256, level: int = logging.INFO) -> None:
        super().__init__(level=level)
        self.capacity = capacity
        self._buf: deque[dict] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._attached_to: Optional[logging.Logger] = None

    # --- direct API --------------------------------------------------------

    def append(self, kind: str, msg: str, **fields: Any) -> None:
        rec = {
            "ts": time.time(),
            "kind": kind,
            "msg": msg,
            **fields,
        }
        with self._lock:
            self._buf.append(rec)

    def snapshot(self, limit: Optional[int] = None) -> list[dict]:
        with self._lock:
            items = list(self._buf)
        if limit is not None:
            items = items[-limit:]
        return items

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._buf)

    # --- logging.Handler ---------------------------------------------------

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record) if self.formatter else record.getMessage()
        except Exception:  # pragma: no cover - defensive
            msg = repr(record.msg)
        self.append(
            kind=record.levelname.lower(),
            msg=msg,
            logger=record.name,
            level=record.levelname,
        )

    def attach_to_logger(self, name: str = "gcos") -> None:
        logger = logging.getLogger(name)
        # Lower the logger level if it filters above our threshold —
        # otherwise INFO records never reach our handler.
        if logger.level == logging.NOTSET or logger.level > self.level:
            logger.setLevel(self.level)
        if self not in logger.handlers:
            logger.addHandler(self)
        self._attached_to = logger

    def detach(self) -> None:
        if self._attached_to is not None:
            try:
                self._attached_to.removeHandler(self)
            except Exception:  # pragma: no cover
                pass
            self._attached_to = None
