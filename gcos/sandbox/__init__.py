"""GCOS sandbox — policy gate + sandbox runners + code extractor.

`make_runner()` is the factory the executor uses:

  - If env `GCOS_SANDBOX=subprocess` is set, force the weak runner.
  - If env `GCOS_SANDBOX=docker` is set, require Docker (raise if missing).
  - Otherwise (auto): use Docker if available; if not, the behaviour depends on
    the fail-closed policy (see `make_runner` / D11).
"""

from __future__ import annotations

import logging
import os
import time
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


_DEGRADE_BANNER = (
    "\n"
    "  ************************************************************************\n"
    "  * GCOS SANDBOX DEGRADED: Docker unavailable — falling back to the     *\n"
    "  * subprocess runner, which provides NO real isolation. LLM-generated  *\n"
    "  * code runs in this Python interpreter, guarded ONLY by the regex      *\n"
    "  * policy gate (which is bypassable). Do NOT run untrusted code or a    *\n"
    "  * grading demo like this. Set GCOS_SANDBOX_FAILCLOSED=1 to refuse      *\n"
    "  * instead of degrading, or start Docker.                              *\n"
    "  ************************************************************************"
)


def _failclosed_default() -> bool:
    return os.getenv("GCOS_SANDBOX_FAILCLOSED", "0").lower() in ("1", "true", "yes")


def make_runner(
    preference: Optional[str] = None, *, fail_closed: Optional[bool] = None,
) -> SandboxRunner:
    """Return a SandboxRunner per env / preference.

    When Docker is unavailable in `auto` mode, the fallback to the weak
    subprocess runner is *loud* (a banner, not a one-line warning) so a degraded
    isolation posture can never go unnoticed (D11). If `fail_closed` (or env
    GCOS_SANDBOX_FAILCLOSED) is set, the factory refuses instead of degrading.
    """
    pref = (preference or os.getenv("GCOS_SANDBOX", "auto")).lower()
    if fail_closed is None:
        fail_closed = _failclosed_default()

    if pref == "subprocess":
        return SubprocessSandboxRunner()

    if pref == "docker":
        if not DockerSandboxRunner.is_available():
            raise RuntimeError(
                "GCOS_SANDBOX=docker requested but Docker daemon isn't reachable."
            )
        return DockerSandboxRunner()

    # auto: docker if present, else subprocess — but never silently.
    if DockerSandboxRunner.is_available():
        log.info("sandbox.make_runner: using Docker (auto)")
        return DockerSandboxRunner()

    if fail_closed:
        raise RuntimeError(
            "Sandbox fail-closed: Docker is unavailable and "
            "GCOS_SANDBOX_FAILCLOSED is set, so untrusted code execution is "
            "refused. Start Docker or explicitly set GCOS_SANDBOX=subprocess."
        )
    log.warning(_DEGRADE_BANNER)
    return SubprocessSandboxRunner()


# Cache the (cheap-but-not-free) Docker availability probe so a frequently
# polled status endpoint doesn't ping the daemon on every call — but with a
# short TTL so a posture change (Docker stops mid-run) is reflected within a few
# seconds instead of being pinned to the boot-time value (review #7).
_DOCKER_AVAILABLE: Optional[bool] = None
_DOCKER_PROBE_TS: float = 0.0
_DOCKER_PROBE_TTL_S: float = 5.0


def sandbox_info(preference: Optional[str] = None, *, refresh: bool = False) -> dict:
    """Describe the *effective* sandbox posture, for status/dashboard (D11).

    Surfaces a `degraded`/`isolation` field so reviewers can see at a glance
    whether real isolation is in force or the weak fallback is active. The
    Docker probe is cached with a short TTL so the reported posture tracks the
    same live check `make_runner` uses, rather than going stale after boot."""
    global _DOCKER_AVAILABLE, _DOCKER_PROBE_TS
    now = time.monotonic()
    if refresh or _DOCKER_AVAILABLE is None or (now - _DOCKER_PROBE_TS) > _DOCKER_PROBE_TTL_S:
        _DOCKER_AVAILABLE = DockerSandboxRunner.is_available()
        _DOCKER_PROBE_TS = now
    pref = (preference or os.getenv("GCOS_SANDBOX", "auto")).lower()

    if pref == "subprocess":
        runner, isolation = "subprocess", "WEAK"
    elif pref == "docker":
        runner, isolation = ("docker", "STRONG") if _DOCKER_AVAILABLE else ("unavailable", "NONE")
    else:  # auto
        runner, isolation = ("docker", "STRONG") if _DOCKER_AVAILABLE else ("subprocess", "WEAK")

    return {
        "preference": pref,
        "docker_available": _DOCKER_AVAILABLE,
        "runner": runner,
        "isolation": isolation,
        "degraded": isolation in ("WEAK", "NONE"),
        "fail_closed": _failclosed_default(),
    }


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
    "sandbox_info",
    "scan_code",
    "scan_prompt",
]
