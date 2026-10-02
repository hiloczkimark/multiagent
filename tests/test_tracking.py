from types import SimpleNamespace

import pytest

from pipeline.tracking import Tracker, price_usage


def usage(**kw):
    server = SimpleNamespace(web_search_requests=kw.pop("searches", 0), web_fetch_requests=kw.pop("fetches", 0))
    base = dict(input_tokens=0, output_tokens=0, cache_creation_input_tokens=0, cache_read_input_tokens=0)
    base.update(kw)
    return SimpleNamespace(server_tool_use=server, **base)


def test_price_sonnet_tokens_and_searches():
    cost, counts = price_usage("claude-sonnet-5", usage(input_tokens=1_000_000, output_tokens=100_000, searches=3))
    # $2 input + $1 output + 3 x $0.01 searches
    assert cost == pytest.approx(3.03)
    assert counts["web_search_requests"] == 3


def test_price_cache_multipliers():
    cost, _ = price_usage("claude-opus-5", usage(cache_creation_input_tokens=1_000_000, cache_read_input_tokens=1_000_000))
    assert cost == pytest.approx(5 * 1.25 + 5 * 0.1)


def test_missing_server_tool_use_is_zero():
    u = SimpleNamespace(input_tokens=10, output_tokens=10, cache_creation_input_tokens=None,
                        cache_read_input_tokens=None, server_tool_use=None)
    cost, counts = price_usage("claude-haiku-4-5", u)
    assert counts["web_search_requests"] == 0 and cost > 0


def test_tracker_per_agent_and_reload(store):
    t = Tracker(store)
    resp = SimpleNamespace(usage=usage(input_tokens=1000, output_tokens=100), stop_reason="end_turn")
    t.record_response(stage="research", agent="researcher", model="claude-sonnet-5", response=resp, duration_s=1.0)
    t.record_response(stage="research", agent="researcher", model="claude-sonnet-5", response=resp, duration_s=2.0)
    t.record_flat(stage="image", agent="image", model="dall-e", cost_usd=0.04, duration_s=5.0)

    agents = t.by_agent()
    assert agents["researcher"]["calls"] == 2
    assert agents["researcher"]["api_seconds"] == pytest.approx(3.0)
    assert agents["image"]["cost_usd"] == pytest.approx(0.04)

    # A resumed run sees earlier spend.
    assert Tracker(store).total_usd == pytest.approx(t.total_usd)


def test_stage_timing_written_even_on_failure(store):
    t = Tracker(store)
    with pytest.raises(RuntimeError):
        with t.stage("research"):
            raise RuntimeError("boom")
    assert store.read_json("timings.json")["research"]["status"] == "failed"
