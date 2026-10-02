"""Benchmark bookkeeping with the researcher stubbed out - no network, no spend."""

from conftest import make_brief_dict
from pipeline import bench, config
from pipeline.agents import research


def fake_research(store, tracker, settings):
    if store.topic == "boom":
        raise research.ResearchError("no valid brief")
    with tracker.stage("research"):
        store.write_json("01_research.json", make_brief_dict())
        store.write_json("01_research_checks.json", {"summary": {
            "found_in_page": 2, "found_in_snippet": 1, "found_live": 1, "misattributed": 0,
            "not_on_page": 0, "unverifiable": 0, "not_found": 0, "grounded_ratio": 1.0}})
        store.write_json("01_research_meta.json", {"settings": {}, "turns": 1, "submissions": 1,
                                                   "schema_rejections": 0, "quote_rejections": 0})


def test_bench_records_every_run_and_reports(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(research, "run", fake_research)

    out = bench.run_bench(["baseline", "tuned"], ["topic a", "boom"])

    rows = __import__("json").loads((out / "bench.json").read_text(encoding="utf-8"))
    assert [(r["preset"], r["topic"]) for r in rows] == [
        ("baseline", "topic a"), ("tuned", "topic a"), ("baseline", "boom"), ("tuned", "boom")]
    assert rows[2]["error"].startswith("ResearchError")
    report = capsys.readouterr().out
    assert "FAIL: errors" in report and "error [tuned] boom" in report
