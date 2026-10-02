"""Thin wrapper around the Anthropic client that records every call's cost."""

from __future__ import annotations

import time
from functools import lru_cache
from typing import Any

import anthropic

from .config import PROMPTS_DIR, REQUEST_TIMEOUT_S
from .tracking import Tracker


@lru_cache(maxsize=1)
def client() -> anthropic.Anthropic:
    # Resolves ANTHROPIC_API_KEY (or an `ant auth login` profile) from the environment.
    return anthropic.Anthropic(timeout=REQUEST_TIMEOUT_S, max_retries=2)


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def create(tracker: Tracker, *, stage: str, agent: str, model: str, **kwargs: Any) -> Any:
    """`client().messages.create` plus a cost/time record in costs.jsonl."""
    t0 = time.monotonic()
    response = client().messages.create(model=model, **kwargs)
    tracker.record_response(
        stage=stage, agent=agent, model=model, response=response,
        duration_s=time.monotonic() - t0,
    )
    return response
