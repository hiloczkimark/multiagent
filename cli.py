"""Command line entry point.

    python cli.py run "topic"
    python cli.py resume <run_id> [--from research]
    python cli.py costs <run_id>
    python cli.py bench [--presets baseline,tuned] [--topics-file topics.txt]
    python cli.py bench --report runs/bench-<timestamp>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

from pipeline import bench, config, orchestrator
from pipeline.agents import research
from pipeline.runstore import RunStore
from pipeline.tracking import Tracker


def print_costs(store: RunStore) -> None:
    tracker = Tracker(store)
    rows = sorted(tracker.by_agent().items(), key=lambda kv: -kv[1]["cost_usd"])
    print(f"\nRun {store.run_id}: {store.topic}")
    print(f"{'agent':<14}{'calls':>6}{'in tok':>10}{'out tok':>9}{'search':>8}{'fetch':>7}{'secs':>7}{'cost $':>9}")
    for agent, a in rows:
        print(
            f"{agent:<14}{int(a['calls']):>6}{int(a['input_tokens']):>10}{int(a['output_tokens']):>9}"
            f"{int(a['web_searches']):>8}{int(a['web_fetches']):>7}{a['api_seconds']:>7.1f}{a['cost_usd']:>9.4f}"
        )
    total, secs = tracker.total_usd, tracker.total_seconds
    cost_ok = "PASS" if total < config.RUN_BUDGET_USD else "FAIL"
    time_ok = "PASS" if secs < config.RUN_TIME_BUDGET_S else "FAIL"
    print(f"\ntotal cost ${total:.4f} (budget ${config.RUN_BUDGET_USD:.2f}) {cost_ok}")
    print(f"total time {secs:.1f}s (budget {config.RUN_TIME_BUDGET_S}s) {time_ok}")
    if store.exists("01_research_checks.json"):
        s = store.read_json("01_research_checks.json")["summary"]
        if "found_in_page" in s:
            counts = ", ".join(f"{s.get(k, 0)} {k}" for k in research.STATUSES if s.get(k))
            print(f"research quotes: {counts} ({s['grounded_ratio']:.0%} grounded)")
        else:  # runs from before the checker learned about snippets
            print(f"research quotes: {s['found']} found, {s['not_found']} not found, "
                  f"{s['unverifiable']} unverifiable")
    if store.exists("02_draft_checks.json"):
        c = store.read_json("02_draft_checks.json")
        print(f"draft: {c['word_count']} words, {c['sections']} sections, cites {c['cited_source_ids']}, "
              f"{c['attempts']} attempt(s); numbers not in brief: {c['unsupported_numbers'] or 'none'}")
        for p in c["problems"]:
            print(f"  unresolved: {p}")


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="pipeline")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="run the pipeline for a topic")
    p_run.add_argument("topic")

    p_resume = sub.add_parser("resume", help="resume a run")
    p_resume.add_argument("run_id")
    p_resume.add_argument("--from", dest="from_stage", choices=orchestrator.STAGE_NAMES)

    p_costs = sub.add_parser("costs", help="per-agent cost and time breakdown for a run")
    p_costs.add_argument("run_id")

    p_bench = sub.add_parser("bench", help="benchmark research presets against the stage targets")
    p_bench.add_argument("--presets", default=",".join(config.RESEARCH_PRESETS),
                         help="comma-separated preset names (default: all)")
    p_bench.add_argument("--topics-file", help="one topic per line (default: 5 built-in topics)")
    p_bench.add_argument("--report", metavar="BENCH_DIR", help="reprint the report of an earlier benchmark")

    args = parser.parse_args(argv)
    if args.cmd == "bench":
        if args.report:
            bench.print_report(Path(args.report))
            return 0
        presets = [p.strip() for p in args.presets.split(",") if p.strip()]
        unknown = [p for p in presets if p not in config.RESEARCH_PRESETS]
        if unknown:
            parser.error(f"unknown presets {unknown}; choose from {list(config.RESEARCH_PRESETS)}")
        topics = bench.DEFAULT_TOPICS
        if args.topics_file:
            topics = [t.strip() for t in Path(args.topics_file).read_text(encoding="utf-8").splitlines() if t.strip()]
        bench.run_bench(presets, topics)
        return 0
    if args.cmd == "run":
        store = orchestrator.run(args.topic)
    elif args.cmd == "resume":
        store = orchestrator.resume(args.run_id, args.from_stage)
    else:
        store = RunStore.open(args.run_id)
    print_costs(store)
    return 0


if __name__ == "__main__":
    sys.exit(main())
