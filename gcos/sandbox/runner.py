"""Sandbox runner interface.

Two concrete impls live next to this file:

- `DockerSandboxRunner` — hardened, real isolation. Used in demos / grading.
- `SubprocessSandboxRunner` — weak isolation. CI / dev fallback. Prints a
  one-time warning when constructed so nobody mistakes it for safe.

Both expose the same `.run_python(code, *, timeout) -> SandboxResult`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class SandboxResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_s: float
    killed_by_timeout: bool = False
    runner: str = ""               # "docker" or "subprocess"

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.killed_by_timeout

    def short_summary(self) -> str:
        if self.killed_by_timeout:
            return f"[{self.runner}] TIMEOUT after {self.duration_s:.1f}s"
        tag = "OK" if self.ok else f"EXIT {self.exit_code}"
        return f"[{self.runner}] {tag} in {self.duration_s:.2f}s"


class SandboxRunner(ABC):
    """Anything that can take Python source and run it under isolation."""

    name: str = "base"

    @abstractmethod
    def run_python(self, code: str, *, timeout: float = 5.0,
                   cpu_shares: Optional[int] = None) -> SandboxResult:
        """Run `code` under isolation. `cpu_shares` (when the runner supports it)
        sets the container's relative CFS weight, so a higher-priority agent's
        sandboxed code gets a proportionally larger CPU share when containers
        compete — real per-agent kernel scheduling for the live CPU-bound work."""
        ...

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<{type(self).__name__} name={self.name!r}>"
