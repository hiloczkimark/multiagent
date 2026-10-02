"""Cost and time tracking for every API call and stage.

Each call is appended to runs/<id>/costs.jsonl, tagged with stage and agent, so
the dashboard can break spend down per agent. Stage wall-clock times go to
runs/<id>/timings.json.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator

from .config import PRICES, RUN_BUDGET_USD, STAGE_TIME_BUDGET_S, WEB_SEARCH_PRICE_PER_REQUEST
from .runstore import RunStore


@dataclass
class CallRecord:
    ts: str
    stage: str
    agent: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    web_search_requests: int
    web_fetch_requests: int
    duration_s: float
    cost_usd: float
    stop_reason: str | None = None
    request_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _get(obj: Any, name: str) -> int:
    value = getattr(obj, name, None) if obj is not None else None
    return int(value or 0)


def price_usage(model: str, usage: Any) -> tuple[float, dict[str, int]]:
    """Dollar cost of one response's `usage`, plus the normalised counts."""
    price = PRICES[model]
    server = getattr(usage, "server_tool_use", None)
    counts = {
        "input_tokens": _get(usage, "input_tokens"),
        "output_tokens": _get(usage, "output_tokens"),
        "cache_write_tokens": _get(usage, "cache_creation_input_tokens"),
        "cache_read_tokens": _get(usage, "cache_read_input_tokens"),
        "web_search_requests": _get(server, "web_search_requests"),
        "web_fetch_requests": _get(server, "web_fetch_requests"),
    }
    cost = (
        counts["input_tokens"] * price.input_per_mtok
        + counts["output_tokens"] * price.output_per_mtok
        + counts["cache_write_tokens"] * price.cache_write_per_mtok
        + counts["cache_read_tokens"] * price.cache_read_per_mtok
    ) / 1_000_000
    cost += counts["web_search_requests"] * WEB_SEARCH_PRICE_PER_REQUEST
    return cost, counts


class Tracker:
    def __init__(self, store: RunStore):
        self.store = store
        self.costs_path = store.path("costs.jsonl")
        self.records: list[CallRecord] = []
        if self.costs_path.exists():  # resumed run: keep earlier stages' spend
            for line in self.costs_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self.records.append(CallRecord(**json.loads(line)))
        self.timings: dict[str, dict[str, Any]] = (
            store.read_json("timings.json") if store.exists("timings.json") else {}
        )

    # --- cost ---------------------------------------------------------------

    def record_response(self, *, stage: str, agent: str, model: str, response: Any,
                        duration_s: float, **extra: Any) -> CallRecord:
        cost, counts = price_usage(model, response.usage)
        rec = CallRecord(
            ts=datetime.now(timezone.utc).isoformat(),
            stage=stage,
            agent=agent,
            model=model,
            duration_s=round(duration_s, 3),
            cost_usd=round(cost, 6),
            stop_reason=getattr(response, "stop_reason", None),
            request_id=getattr(response, "_request_id", None),
            extra=extra,
            **counts,
        )
        self._append(rec)
        return rec

    def record_flat(self, *, stage: str, agent: str, model: str, cost_usd: float,
                    duration_s: float, **extra: Any) -> CallRecord:
        """For non-token-priced calls (e.g. image generation)."""
        rec = CallRecord(
            ts=datetime.now(timezone.utc).isoformat(), stage=stage, agent=agent, model=model,
            input_tokens=0, output_tokens=0, cache_write_tokens=0, cache_read_tokens=0,
            web_search_requests=0, web_fetch_requests=0,
            duration_s=round(duration_s, 3), cost_usd=round(cost_usd, 6), extra=extra,
        )
        self._append(rec)
        return rec

    def _append(self, rec: CallRecord) -> None:
        self.records.append(rec)
        with self.costs_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(rec)) + "\n")

    @property
    def total_usd(self) -> float:
        return sum(r.cost_usd for r in self.records)

    def stage_usd(self, stage: str) -> float:
        return sum(r.cost_usd for r in self.records if r.stage == stage)

    def remaining_usd(self) -> float:
        return RUN_BUDGET_USD - self.total_usd

    def by_agent(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for r in self.records:
            a = out[r.agent]
            a["calls"] += 1
            a["cost_usd"] += r.cost_usd
            a["input_tokens"] += r.input_tokens + r.cache_write_tokens + r.cache_read_tokens
            a["output_tokens"] += r.output_tokens
            a["web_searches"] += r.web_search_requests
            a["web_fetches"] += r.web_fetch_requests
            a["api_seconds"] += r.duration_s
        return {k: dict(v) for k, v in out.items()}

    # --- time ---------------------------------------------------------------

    @contextmanager
    def stage(self, name: str) -> Iterator["StageClock"]:
        clock = StageClock(name, STAGE_TIME_BUDGET_S.get(name))
        started_at = datetime.now(timezone.utc).isoformat()
        status = "ok"
        try:
            yield clock
        except BaseException:
            status = "failed"
            raise
        finally:
            elapsed = clock.elapsed()
            self.timings[name] = {
                "started_at": started_at,
                "duration_s": round(elapsed, 2),
                "budget_s": clock.budget_s,
                "over_budget": clock.budget_s is not None and elapsed > clock.budget_s,
                "status": status,
            }
            self.store.write_json("timings.json", self.timings)

    @property
    def total_seconds(self) -> float:
        return sum(t["duration_s"] for t in self.timings.values())


class StageClock:
    def __init__(self, name: str, budget_s: float | None):
        self.name = name
        self.budget_s = budget_s
        self._t0 = time.monotonic()

    def elapsed(self) -> float:
        return time.monotonic() - self._t0

    def over_budget(self) -> bool:
        return self.budget_s is not None and self.elapsed() > self.budget_s
