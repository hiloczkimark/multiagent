"""Thin wrapper around the Anthropic client that records every call's cost."""

from __future__ import annotations

import time
from functools import lru_cache
from typing import Any

import anthropic

from .config import PRICES, PROMPTS_DIR, REQUEST_TIMEOUT_S
from .tracking import Tracker

# Server-side refusal fallback: if a model's safety classifier declines, the API
# reruns the request on Anthropic's recommended fallback model instead.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


@lru_cache(maxsize=1)
def client() -> anthropic.Anthropic:
    # Resolves ANTHROPIC_API_KEY (or an `ant auth login` profile) from the environment.
    return anthropic.Anthropic(timeout=REQUEST_TIMEOUT_S, max_retries=2)


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def create(tracker: Tracker, *, stage: str, agent: str, model: str,
           fallbacks: bool = False, **kwargs: Any) -> Any:
    """`client().messages.create` plus a cost/time record in costs.jsonl."""
    t0 = time.monotonic()
    if fallbacks:
        response = client().beta.messages.create(
            model=model, betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
    else:
        response = client().messages.create(model=model, **kwargs)
    # A fallback may have answered; price the call by the model that did.
    served_by = getattr(response, "model", None)
    priced_as = served_by if isinstance(served_by, str) and served_by in PRICES else model
    tracker.record_response(
        stage=stage, agent=agent, model=priced_as, response=response,
        duration_s=time.monotonic() - t0,
        **({"requested_model": model} if priced_as != model else {}),
    )
    return response
