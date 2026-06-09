"""Sandbox/security-depth tests (category D): stronger (but still honest) gate
rules, fail-closed/degraded fallback, and posture introspection."""

from __future__ import annotations

import pytest

from gcos.sandbox import make_runner, sandbox_info
from gcos.sandbox.docker_runner import DockerSandboxRunner
from gcos.sandbox.policy_gate import scan_code
from gcos.sandbox.runner import SandboxRunner
from gcos.sandbox.subprocess_runner import SubprocessSandboxRunner


# --- D12: added gate rules catch cheap evasions, keep zero benign FPs --------

# Each snippet is chosen to isolate exactly one *new* rule (avoiding tokens that
# an earlier rule would match first — the gate returns the first match).
@pytest.mark.parametrize("code,rule", [
    ("import os\nos.popen('id').read()", "code.os_popen"),
    ("m = __import__('base64')\nprint(m)", "code.import_dynamic"),
    ("c = compile(src, '<s>', 'single')", "code.compile"),
    ("import pty\npty.spawn('/bin/sh')", "code.pty"),
    ("print(__builtins__)", "code.builtins_access"),
    ("g = globals()['data']\nprint(g)", "code.globals_index"),
    ("import os\nos.remove('/etc/hosts')", "code.fs_delete_abs"),
])
def test_added_rules_deny(code, rule):
    r = scan_code(code)
    assert not r.allowed
    assert r.rule == rule


@pytest.mark.parametrize("code", [
    "print('hello world')",
    "import math\nprint(math.sqrt(2))",
    "xs = [i*i for i in range(10)]\nprint(sum(xs))",
    "def fib(n):\n    a,b=0,1\n    for _ in range(n): a,b=b,a+b\n    return a\nprint(fib(10))",
    "import json\nprint(json.dumps({'a': 1}))",
    "from collections import Counter\nprint(Counter('abracadabra'))",
    # review #2: the builtin-compile rule must not false-match method calls.
    "import re\npat = re.compile(r'\\d+')\nprint(pat.findall('a1b2'))",
    "model.compile(optimizer='adam', loss='mse')",
])
def test_added_rules_do_not_flag_benign(code):
    assert scan_code(code).allowed


def test_compile_rule_still_catches_builtin_but_not_dotted():
    assert not scan_code("compile(src, '<s>', 'exec')").allowed   # builtin → DENY
    assert scan_code("re.compile(r'x')").allowed                  # method → ALLOW


def test_existing_rule_ids_still_fire_first():
    # The os-specific dynamic import must still win over the generic one.
    assert scan_code("m = __import__('os')\nm.system('id')").rule == "code.import_os_dynamic"


# --- D11: fail-closed / degraded fallback ------------------------------------

def test_sandbox_info_reports_posture():
    info = sandbox_info(refresh=True)
    assert set(info) >= {"runner", "isolation", "degraded", "docker_available"}
    if not info["docker_available"]:
        assert info["runner"] == "subprocess"
        assert info["isolation"] == "WEAK"
        assert info["degraded"] is True
    else:
        assert info["isolation"] == "STRONG"
        assert info["degraded"] is False


@pytest.mark.skipif(
    DockerSandboxRunner.is_available(), reason="needs Docker to be ABSENT"
)
def test_auto_falls_back_to_subprocess_when_not_failclosed():
    assert isinstance(make_runner("auto", fail_closed=False), SubprocessSandboxRunner)


@pytest.mark.skipif(
    DockerSandboxRunner.is_available(), reason="needs Docker to be ABSENT"
)
def test_fail_closed_refuses_when_docker_absent():
    with pytest.raises(RuntimeError, match="fail-closed"):
        make_runner("auto", fail_closed=True)


def test_subprocess_preference_always_weak_runner():
    r = make_runner("subprocess")
    assert isinstance(r, SubprocessSandboxRunner)
    assert isinstance(r, SandboxRunner)
