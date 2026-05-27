"""SubprocessSandboxRunner — weak-isolation tests.

These tests are *not* security tests. They only check that the runner is a
correct implementation of the SandboxRunner contract (captures stdout/stderr,
honors timeout, reports exit code). Real isolation lives in DockerSandboxRunner.
"""

from __future__ import annotations

from gcos.sandbox.runner import SandboxResult
from gcos.sandbox.subprocess_runner import SubprocessSandboxRunner


def test_run_simple_print():
    r = SubprocessSandboxRunner().run_python("print('hello')")
    assert isinstance(r, SandboxResult)
    assert r.ok
    assert r.runner == "subprocess"
    assert r.exit_code == 0
    assert r.stdout.strip() == "hello"
    assert r.stderr == ""
    assert not r.killed_by_timeout


def test_run_python_with_computation():
    r = SubprocessSandboxRunner().run_python(
        "print(sum(i*i for i in range(5)))"
    )
    assert r.ok
    assert r.stdout.strip() == "30"


def test_nonzero_exit_reported():
    r = SubprocessSandboxRunner().run_python(
        "import sys; sys.stderr.write('boom\\n'); sys.exit(7)"
    )
    assert r.ok is False
    assert r.exit_code == 7
    assert "boom" in r.stderr


def test_timeout_kills_long_running():
    r = SubprocessSandboxRunner().run_python(
        "import time; time.sleep(5); print('should not reach')",
        timeout=0.4,
    )
    assert r.killed_by_timeout is True
    assert r.ok is False
    assert "should not reach" not in r.stdout


def test_short_summary_strings():
    ok = SandboxResult(stdout="", stderr="", exit_code=0, duration_s=0.1, runner="subprocess")
    bad = SandboxResult(stdout="", stderr="", exit_code=1, duration_s=0.1, runner="subprocess")
    timed = SandboxResult(stdout="", stderr="", exit_code=-1, duration_s=2.0,
                          killed_by_timeout=True, runner="subprocess")
    assert "OK" in ok.short_summary()
    assert "EXIT 1" in bad.short_summary()
    assert "TIMEOUT" in timed.short_summary()
