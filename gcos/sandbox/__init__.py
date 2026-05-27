"""GCOS sandbox — policy gate + sandbox runners + code extractor.

`make_runner()` is the factory the executor uses:

  - If env `GCOS_SANDBOX=subprocess` is set, force the weak runner.
  - If env `GCOS_SANDBOX=docker` is set, require Docker (raise if missing).
  - Otherwise: use Docker if available, fall back to Subprocess with a warning.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from gcos.sandbox.docker_runner import DockerSandboxRunner
from gcos.sandbox.extract import extract_python
from gcos.sandbox.policy_gate import (
    Decision,
    GateResult,
    check,
    scan_code,
    scan_prompt,
)
from gcos.sandbox.runner import SandboxResult, SandboxRunner
from gcos.sandbox.subprocess_runner import SubprocessSandboxRunner


log = logging.getLogger(__name__)


def make_runner(preference: Optional[str] = None) -> SandboxRunner:
    """Return a SandboxRunner per env / preference."""
    pref = (preference or os.getenv("GCOS_SANDBOX", "auto")).lower()

    if pref == "subprocess":
        return SubprocessSandboxRunner()

    if pref == "docker":
        if not DockerSandboxRunner.is_available():
            raise RuntimeError(
                "GCOS_SANDBOX=docker requested but Docker daemon isn't reachable."
            )
        return DockerSandboxRunner()

    # auto: docker if present, else subprocess (with warning, see runner ctor)
    if DockerSandboxRunner.is_available():
        log.info("sandbox.make_runner: using Docker (auto)")
        return DockerSandboxRunner()
    log.warning("sandbox.make_runner: Docker unavailable, falling back to subprocess")
    return SubprocessSandboxRunner()


__all__ = [
    "Decision",
    "GateResult",
    "SandboxResult",
    "SandboxRunner",
    "DockerSandboxRunner",
    "SubprocessSandboxRunner",
    "check",
    "extract_python",
    "make_runner",
    "scan_code",
    "scan_prompt",
]
