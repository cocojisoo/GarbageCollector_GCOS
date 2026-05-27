"""Upstage Solar Pro 3 client.

Solar is OpenAI-API-compatible — we use the official `openai` SDK with the
base_url swapped to Upstage. This module is the "device driver" in OS terms:
the rest of GCOS only talks to `SolarClient.chat()`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv
from openai import OpenAI


load_dotenv()


@dataclass
class SolarConfig:
    api_key: str = field(default_factory=lambda: os.getenv("UPSTAGE_API_KEY", ""))
    base_url: str = field(default_factory=lambda: os.getenv("UPSTAGE_BASE_URL", "https://api.upstage.ai/v1"))
    model: str = field(default_factory=lambda: os.getenv("UPSTAGE_MODEL", "solar-pro2"))

    def validate(self) -> None:
        if not self.api_key:
            raise RuntimeError(
                "UPSTAGE_API_KEY is not set. Copy .env.example to .env and fill it in."
            )


@dataclass
class ChatResult:
    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    model: str = ""

    @property
    def tokens(self) -> int:
        return self.total_tokens


class SolarClient:
    """Thin wrapper around the OpenAI SDK pointed at Upstage."""

    def __init__(self, config: Optional[SolarConfig] = None) -> None:
        self.config = config or SolarConfig()
        self.config.validate()
        self._client = OpenAI(
            api_key=self.config.api_key,
            base_url=self.config.base_url,
        )

    def chat(
        self,
        messages: list[dict],
        *,
        temperature: float = 0.2,
        max_tokens: int = 1024,
        timeout: float = 30.0,
    ) -> ChatResult:
        """Single non-streaming chat completion.

        `messages` follows the OpenAI format: [{"role": "user", "content": "..."}].
        """
        resp = self._client.chat.completions.create(
            model=self.config.model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )
        choice = resp.choices[0]
        usage = getattr(resp, "usage", None)
        return ChatResult(
            content=choice.message.content or "",
            prompt_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
            total_tokens=getattr(usage, "total_tokens", 0) if usage else 0,
            model=self.config.model,
        )
