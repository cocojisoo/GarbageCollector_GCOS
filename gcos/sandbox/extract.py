"""Code extraction from LLM output.

LLM responses typically wrap code in triple-backtick fences:

    Sure, here is the code:

    ```python
    print("hello")
    ```

We pick the *first* python (or unlabeled) fence. If none, we return None.
Future M4 work may need multiple blocks, but M3 only runs one block per step.
"""

from __future__ import annotations

import re
from typing import Optional


# (?s) makes . match newlines. Lazy match so we stop at the first closing fence.
_FENCE = re.compile(
    r"```(?P<lang>[a-zA-Z0-9_+\-]*)\s*\n(?P<body>.*?)```",
    re.DOTALL,
)


def extract_python(text: str) -> Optional[str]:
    """Return the body of the first python (or unlabeled) code fence.

    Preference order:
      1. ```python ... ```
      2. ```py ... ```
      3. unlabeled ``` ... ```
    """
    if not text:
        return None
    matches = list(_FENCE.finditer(text))
    if not matches:
        return None
    preferred = {"python": 1, "py": 2, "": 3}
    matches.sort(key=lambda m: preferred.get(m.group("lang").lower(), 99))
    return matches[0].group("body").strip("\n")
