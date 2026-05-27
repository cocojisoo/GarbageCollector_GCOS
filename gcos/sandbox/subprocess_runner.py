"""SubprocessSandboxRunner — weak-isolation dev / CI fallback.

DO NOT USE THIS IN PRODUCTION. Anything the LLM emits runs in your real Python
interpreter. We deliberately use a fresh temp directory and `cwd=tempdir`, but
the script can still read/write your filesystem, hit the network, and call any
syscall the user account can call. The policy gate (gcos.sandbox.policy_gate)
is the only safety net here.

This runner exists so we can:
  - run the CI test suite without Docker installed,
  - let students try GCOS on a laptop that doesn't have Docker,
  - keep DockerSandboxRunner pluggable (same interface).

The grading-time demo MUST be done with DockerSandboxRunner.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from gcos.sandbox.runner import SandboxResult, SandboxRunner


log = logging.getLogger(__name__)


_WARNED = False


class SubprocessSandboxRunner(SandboxRunner):
    name = "subprocess"

    def __init__(self, python_path: str | None = None) -> None:
        global _WARNED
        if not _WARNED:
            log.warning(
                "SubprocessSandboxRunner provides NO real isolation. "
                "Use DockerSandboxRunner for any demo or untrusted code."
            )
            _WARNED = True
        self.python_path = python_path or sys.executable

    def run_python(self, code: str, *, timeout: float = 5.0) -> SandboxResult:
        workdir = Path(tempfile.mkdtemp(prefix="gcos-sbx-"))
        script = workdir / "main.py"
        script.write_text(code, encoding="utf-8")

        start = time.monotonic()
        killed = False
        try:
            proc = subprocess.run(
                [self.python_path, str(script)],
                cwd=str(workdir),
                capture_output=True,
                text=True,
                timeout=timeout,
                # A very minimal env — drops anything user-specific
                env={"PATH": "", "PYTHONIOENCODING": "utf-8"},
            )
            stdout, stderr, exit_code = proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired as e:
            killed = True
            stdout = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            stderr = e.stderr.decode("utf-8", "replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
            exit_code = -1
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

        return SandboxResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            duration_s=time.monotonic() - start,
            killed_by_timeout=killed,
            runner=self.name,
        )
