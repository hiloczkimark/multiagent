"""Research-stage benchmark: run topics under several presets, compare to targets.

Runs go to runs/bench-<timestamp>/<preset>/<run_id>/ and every result is
appended to bench.json as it lands, so an interrupted benchmark keeps what it
already paid for. Topics are interleaved across presets so a slow patch of API
latency doesn't land on one preset only.
"""

from __future__ import annotations

import json
import statistics
from datetime import date, datetime
from pathlib import Path
from typing import Any

from . import config
from .agents import research
from .runstore import RunStore
from .tracking import Tracker

DEFAULT_TOPICS = [
    "GDP of Budapest",
    "How heat pumps work in cold climates",
    "The state of solid-state batteries for electric cars",
    "Why coral reefs bleach",
    "How mRNA vaccines are made",
]


def run_bench(presets: list[str], topics: list[str]) -> Path:
    out_dir = config.RUNS_DIR / f"bench-{datetime.now():%Y%m%d-%H%M%S}"
    out_dir.mkdir(parents=True)
    rows: list[dict[str, Any]] = []
    total = len(presets) * len(topics)
    for topic in topics:
        for name in presets:
            row = _run_one(out_dir / name, name, topic)
            rows.append(row)
            (out_dir / "bench.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
            status = row["error"] or (
                f"{row['summary']['grounded_ratio']:.0%} grounded, "
                f"{row['meta']['submissions']} submission(s)"
            )
            print(f"[{len(rows)}/{total}] {name:<12} {row['duration_s']:>6.1f}s "
                  f"${row['cost_usd']:.3f}  {topic[:40]:<40}  {status}", flush=True)
    print_report(out_dir)
    return out_dir


def _run_one(runs_dir: Path, preset: str, topic: str) -> dict[str, Any]:
    runs_dir.mkdir(parents=True, exist_ok=True)
    store = RunStore.create(topic, runs_dir=runs_dir)
    tracker = Tracker(store)
    error = None
    try:
        research.run(store, tracker, config.RESEARCH_PRESETS[preset])
    except Exception as e:  # a failed run is a benchmark result, not a crash
        error = f"{type(e).__name__}: {e}"[:300]
    row: dict[str, Any] = {
        "preset": preset, "topic": topic, "run_id": store.run_id, "error": error,
        "duration_s": tracker.timings.get("research", {}).get("duration_s", 0.0),
        "cost_usd": round(tracker.total_usd, 5),
        "web_searches": sum(r.web_search_requests for r in tracker.records),
        "web_fetches": sum(r.web_fetch_requests for r in tracker.records),
    }
    if error is None:
        row["summary"] = store.read_json("01_research_checks.json")["summary"]
        row["meta"] = {k: v for k, v in store.read_json("01_research_meta.json").items() if k != "settings"}
        row["sources"] = _source_ages(store.read_json("01_research.json")["sources"])
    return row


def _source_ages(sources: list[dict[str, Any]]) -> dict[str, int]:
    cutoff = date.today().replace(year=date.today().year - 2)
    old = unknown = 0
    for s in sources:
        try:
            old += date.fromisoformat(s["published"]) < cutoff
        except ValueError:
            unknown += 1
    return {"count": len(sources), "older_than_2y": old, "date_unknown": unknown}


def print_report(out_dir: Path) -> None:
    rows = json.loads((out_dir / "bench.json").read_text(encoding="utf-8"))
    t = config.RESEARCH_TARGETS
    print(f"\nBenchmark {out_dir.name}  (targets: median <= {t['median_s']}s, max <= {t['max_s']}s, "
          f"cost <= ${t['cost_usd']:.2f}, grounded >= {t['grounded_ratio']:.0%}, ungrounded = {t['ungrounded']})")
    print(f"{'preset':<13}{'ok':>5}{'median s':>10}{'max s':>8}{'mean $':>8}{'max $':>8}"
          f"{'grounded':>10}{'ungrnd':>8}{'unverif':>8}{'1st try':>8}{'old src':>8}  verdict")
    for preset in dict.fromkeys(r["preset"] for r in rows):
        runs = [r for r in rows if r["preset"] == preset]
        ok = [r for r in runs if r["error"] is None]
        secs = [r["duration_s"] for r in runs]
        costs = [r["cost_usd"] for r in runs]
        quotes = {k: sum(r["summary"].get(k, 0) for r in ok) for k in research.STATUSES}
        n_quotes = sum(quotes.values())
        grounded = sum(quotes[k] for k in research.GROUNDED) / n_quotes if n_quotes else 0.0
        ungrounded = sum(quotes[k] for k in research.UNGROUNDED)
        first_try = sum(r["meta"]["submissions"] == 1 for r in ok)
        old = sum(r["sources"]["older_than_2y"] for r in ok)
        fails = [name for name, bad in [
            ("errors", len(ok) < len(runs)),
            ("median", statistics.median(secs) > t["median_s"]),
            ("max", max(secs) > t["max_s"]),
            ("cost", max(costs) > t["cost_usd"]),
            ("grounded", grounded < t["grounded_ratio"]),
            ("ungrounded", ungrounded > t["ungrounded"]),
        ] if bad]
        verdict = "PASS" if not fails else "FAIL: " + ", ".join(fails)
        print(f"{preset:<13}{len(ok):>2}/{len(runs):<2}{statistics.median(secs):>10.1f}{max(secs):>8.1f}"
              f"{statistics.mean(costs):>8.3f}{max(costs):>8.3f}{grounded:>10.0%}{ungrounded:>8}"
              f"{quotes['unverifiable']:>8}{first_try:>5}/{len(ok):<2}{old:>8}  {verdict}")
    for r in rows:
        if r["error"]:
            print(f"  error [{r['preset']}] {r['topic']}: {r['error']}")
