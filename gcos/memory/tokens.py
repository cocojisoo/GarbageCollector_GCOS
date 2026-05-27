"""Token counting.

Solar doesn't publish its tokenizer, so we use a rough char-based estimate
(~4 chars per token for English / mixed text). The real token count from
Solar's `usage` response is used when available — pages populated by the
executor carry the authoritative count. This helper is only for pages we
haven't roundtripped through Solar yet (e.g., system prompts, summaries).
"""

from __future__ import annotations


def estimate_tokens(text: str) -> int:
    """Char-based estimate, ~4 chars per token. Never returns 0 for non-empty."""
    if not text:
        return 0
    return max(1, len(text) // 4)
