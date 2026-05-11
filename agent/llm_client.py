"""
llm_client.py — standalone Anthropic API wrapper.

Replaces PILK's `llm_ask` tool. Two functions:
- classify(prompt, label_set) — Haiku, returns one label from the set. Cheap.
- draft(system, messages) — Sonnet, returns the assistant text. Voice-grade.

Reads ANTHROPIC_API_KEY from the environment. Caller can override per call.

This file uses the official `anthropic` Python SDK. It does not retry the API
call directly — wrap it in your own retry logic if you need (the rolodex agent's
retry decorator handles that at the orchestration layer).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable

from anthropic import Anthropic, APIError

DEFAULT_HAIKU = "claude-haiku-4-5-20251001"
DEFAULT_SONNET = "claude-sonnet-4-6"

_client: Anthropic | None = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set. Add it to your .env or shell environment."
            )
        _client = Anthropic(api_key=key)
    return _client


@dataclass
class DraftResult:
    text: str
    model: str
    input_tokens: int
    output_tokens: int


def classify(
    prompt: str,
    *,
    labels: Iterable[str],
    model: str = DEFAULT_HAIKU,
    max_tokens: int = 16,
) -> str:
    """Classify `prompt` into one of `labels`. Returns the chosen label, uppercased.

    Falls back to the first label if the model returns something unrecognized.
    """
    label_list = list(labels)
    instruction = (
        f"Classify the following content into exactly one of these labels: "
        f"{', '.join(label_list)}. Reply with only the label, nothing else.\n\n"
        f"CONTENT:\n{prompt}\n\nLABEL:"
    )
    client = _get_client()
    try:
        msg = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": instruction}],
        )
    except APIError as e:
        raise RuntimeError(f"Anthropic API error: {e}") from e

    text = msg.content[0].text.strip().upper() if msg.content else ""
    for label in label_list:
        if label.upper() in text:
            return label.upper()
    return label_list[0].upper()


def draft(
    *,
    system: str,
    user: str,
    model: str = DEFAULT_SONNET,
    max_tokens: int = 200,
    temperature: float = 0.7,
    timeout: float | None = None,
) -> DraftResult:
    """Generate a draft message using Sonnet."""
    client = _get_client()
    if timeout is not None:
        client = client.with_options(timeout=timeout)
    try:
        msg = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except APIError as e:
        raise RuntimeError(f"Anthropic API error: {e}") from e

    text = msg.content[0].text.strip() if msg.content else ""
    return DraftResult(
        text=text,
        model=model,
        input_tokens=msg.usage.input_tokens,
        output_tokens=msg.usage.output_tokens,
    )


def health_check() -> tuple[bool, str]:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False, "ANTHROPIC_API_KEY not set"
    try:
        # cheapest possible call to verify auth works
        c = _get_client()
        c.messages.create(
            model=DEFAULT_HAIKU,
            max_tokens=1,
            messages=[{"role": "user", "content": "hi"}],
        )
        return True, "Anthropic API reachable"
    except Exception as e:
        return False, f"Anthropic API error: {e}"
