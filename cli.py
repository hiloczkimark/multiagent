"""Command line entry point.

    python cli.py run "topic"
    python cli.py resume <run_id> [--from research]
    python cli.py costs <run_id>
    python cli.py spotcheck <run_id> [--fresh]
    python cli.py cms
    python cli.py bench [--presets baseline,tuned] [--topics-file topics.txt]
    python cli.py bench --report runs/bench-<timestamp>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

from pipeline import bench, cms, config, orchestrator
from pipeline.agents import research, verify
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
    if store.exists("eval_report.json"):
        r = store.read_json("eval_report.json")
        n = r["counts"]
        print(f"edit: {n['caught']} issues caught, {n['corrected']} corrected, {n['open']} open "
              f"({n['open_blocking']} critical/major) in {r['revision_rounds']} round(s); "
              f"caught and corrected: {'YES' if r['caught_and_corrected'] else 'no'}")
        for d in r["round_decisions"]:
            print(f"  {d}")
    if store.exists("04_cover.json"):
        c = store.read_json("04_cover.json")
        note = f" (fell back: {c['error'][:120]})" if c.get("error") else ""
        print(f"cover: {c['file']} from {c['provider']}/{c['model']}, ${c['cost_usd']:.3f}{note}")
    if store.exists("claims.json"):
        c = store.read_json("claims.json")
        counts = ", ".join(f"{n} {k}" for k, n in c["summary"].items() if n)
        print(f"spot-check: {len(c['claims'])} claims, {counts} -> {c['status'].upper()} "
              f"(details: python cli.py spotcheck {store.run_id})")
    if store.exists("06_published.json"):
        p = store.read_json("06_published.json")
        print(f"published: {p['cms']} {p['status'].upper()} {p['post_id']} (revision {p['revision']}) "
              f"{'NEEDS REVIEW' if p['needs_review'] else 'ready for an editor'}\n  preview: {p['url']}")
        for note in p["review_notes"]:
            print(f"  - {note}")


def print_posts() -> None:
    posts = cms.get_cms(config.CMS, config.CMS_MOCK_DIR).list_posts()
    if not posts:
        print("no posts yet")
    for p in posts:
        flag = "needs review" if p["needs_review"] else "ok"
        print(f"{p['updated_at'][:16]}  {p['status']:<6} r{p['revision']:<2} {flag:<12} {p['title'][:60]}  ({p['id']})")


def print_spotcheck(store: RunStore) -> None:
    c = store.read_json("claims.json")
    print(f"\nSpot-check of {store.run_id}: {len(c['claims'])} of {c['candidates']} cited sentences "
          f"(seed {c['seed']}) -> {c['status'].upper()}")
    for claim in c["claims"]:
        print(f"\n[{claim['verdict'].upper()}] claim {claim['id']}, sources {claim['source_ids']} "
              f"(live page read for {claim['pages_loaded'] or 'none'})")
        print(f"  {claim['sentence']}")
        print(f"  why: {claim['explanation']}")
        print(f"  evidence: {claim['evidence']}")


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

    sub.add_parser("cms", help="list the draft posts in the mock CMS")

    p_spot = sub.add_parser("spotcheck", help="show the verified claims of a run, or check fresh ones")
    p_spot.add_argument("run_id")
    p_spot.add_argument("--fresh", action="store_true", help="verify a new random set of claims (~$0.01)")

    p_bench = sub.add_parser("bench", help="benchmark research presets against the stage targets")
    p_bench.add_argument("--presets", default=",".join(config.RESEARCH_PRESETS),
                         help="comma-separated preset names (default: all)")
    p_bench.add_argument("--topics-file", help="one topic per line (default: 5 built-in topics)")
    p_bench.add_argument("--report", metavar="BENCH_DIR", help="reprint the report of an earlier benchmark")

    args = parser.parse_args(argv)
    if args.cmd == "cms":
        print_posts()
        return 0
    if args.cmd == "spotcheck":
        store = RunStore.open(args.run_id)
        if args.fresh or not store.exists("claims.json"):
            seed = store.read_json("claims.json")["seed"] + 1 if store.exists("claims.json") else 0
            verify.run(store, Tracker(store), seed=seed)
        print_spotcheck(store)
        return 0
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
