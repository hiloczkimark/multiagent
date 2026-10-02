"""Researcher loop against a scripted fake API - no network, no spend."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from conftest import make_brief_dict, resp
from pipeline import config
from pipeline.agents import research
from pipeline.schemas import ResearchBrief
from pipeline.tracking import Tracker

# The fake briefs' quotes aren't in any transcript, so skip the in-loop quote
# check except in the tests that exercise it.
NO_QUOTE_CHECK = replace(config.RESEARCH, quote_rejections=0)


def fetched(url, text):
    return {"type": "web_fetch_tool_result", "content": {
        "type": "web_fetch_result", "url": url,
        "content": {"type": "document", "source": {"type": "text", "data": text}},
    }}


def printed(stdout):
    return {"type": "code_execution_tool_result",
            "content": {"type": "code_execution_result", "stdout": stdout, "return_code": 0}}


def submit(input_, id_="tu_1"):
    return SimpleNamespace(type="tool_use", name="submit_brief", id=id_, input=input_)


def test_pause_then_invalid_then_valid(store, scripted):
    queue, requests = scripted
    bad = make_brief_dict()
    bad["key_points"][0]["source_ids"] = [42]
    queue += [
        resp("pause_turn", [SimpleNamespace(type="text", text="searching")]),
        resp("tool_use", [submit(bad, "tu_bad")]),
        resp("tool_use", [submit(make_brief_dict(), "tu_good")]),
    ]
    tracker = Tracker(store)
    brief = research.run(store, tracker, NO_QUOTE_CHECK)

    assert isinstance(brief, ResearchBrief)
    assert len(requests) == 3
    # The rejection went back as an error tool_result naming the problem.
    last_user = requests[2]["messages"][-1]["content"][0]
    assert last_user["is_error"] and "unknown source ids" in last_user["content"]
    # Every call was costed and attributed.
    assert tracker.by_agent()["researcher"]["calls"] == 3
    assert store.exists("01_research.json") and store.exists("01_research_checks.json")
    meta = store.read_json("01_research_meta.json")
    assert (meta["turns"], meta["submissions"], meta["schema_rejections"]) == (3, 2, 1)


def test_escapes_copied_into_quotes_are_decoded(store, scripted):
    queue, _ = scripted
    d = make_brief_dict()
    d["sources"][0]["quotes"] = ["Metro: \\u20ac104.39 billion"]
    queue += [resp("tool_use", [submit(d)])]
    brief = research.run(store, Tracker(store), NO_QUOTE_CHECK)
    assert brief.sources[0].quotes == ["Metro: €104.39 billion"]
    assert "€104.39" in store.path("01_research.json").read_text(encoding="utf-8")


def test_prose_answer_gets_nudged(store, scripted):
    queue, requests = scripted
    queue += [
        resp("end_turn", [SimpleNamespace(type="text", text="Here is what I found...")]),
        resp("tool_use", [submit(make_brief_dict())]),
    ]
    research.run(store, Tracker(store), NO_QUOTE_CHECK)
    assert requests[1]["messages"][-1]["content"] == research.NUDGE


def test_gives_up_after_max_turns(store, scripted):
    queue, _ = scripted
    queue += [resp("pause_turn", []), resp("pause_turn", [])]
    with pytest.raises(research.ResearchError, match="no valid brief"):
        research.run(store, Tracker(store), replace(NO_QUOTE_CHECK, max_turns=2))
    assert store.read_json("timings.json")["research"]["status"] == "failed"


def test_ungrounded_quotes_are_sent_back_once(store, scripted):
    queue, requests = scripted
    # Only source 1's quote is in anything the model saw.
    page = fetched("https://example.com/1", "Quote number 1 from the page.")
    page_block = SimpleNamespace(type=page["type"], model_dump=lambda mode=None: page)
    seen = resp("tool_use", [page_block, submit(make_brief_dict(), "tu_1")])
    queue += [seen, resp("tool_use", [submit(make_brief_dict(), "tu_2")])]
    research.run(store, Tracker(store))  # default settings: one quote rejection allowed

    assert len(requests) == 2
    feedback = requests[1]["messages"][-1]["content"][0]
    assert feedback["is_error"] and feedback["tool_use_id"] == "tu_1"
    assert "Quote number 2" in feedback["content"] and "Quote number 1" not in feedback["content"]
    # Second submission is accepted even though it's still ungrounded; the
    # checks file records that for the verifier.
    assert store.read_json("01_research_meta.json")["quote_rejections"] == 1
    assert store.read_json("01_research_checks.json")["summary"]["not_found"] == 2


def test_tools_follow_settings():
    basic = research.tools_for(config.RESEARCH_PRESETS["tuned_basic"])
    assert [t.get("type") for t in basic[:2]] == ["web_search_20250305", "web_fetch_20250910"]
    tuned = research.tools_for(config.RESEARCH_PRESETS["tuned"])
    assert tuned[0]["max_uses"] == 3 and tuned[1]["max_content_tokens"] == 10000
    for preset in config.RESEARCH_PRESETS.values():
        prompt = research.system_prompt(preset)
        assert "{max_" not in prompt and "{tool_notes}" not in prompt


def test_check_quotes_statuses():
    d = make_brief_dict(n_sources=5)
    d["sources"][1]["quotes"] = ["Snippet text for two"]
    d["sources"][2]["quotes"] = ["Quote number 1 from the page."]  # really from source 1
    d["sources"][3]["quotes"] = ["Hidden snippet"]
    brief = ResearchBrief.model_validate(d)
    transcript = [{"role": "assistant", "content": [
        fetched("https://example.com/1", "Intro.  Quote   number 1 from the page. More."),
        printed('[{"title": "Two", "url": "https://example.com/2", "content": "Snippet text \\u00b7 for two"}]'),
        {"type": "web_search_tool_result", "content": [{"type": "web_search_result", "url": "https://example.com/4"}]},
    ]}]
    statuses = [q["status"] for q in research.check_quotes(brief, transcript)["quotes"]]
    assert statuses == ["found_in_page", "found_in_snippet", "misattributed", "unverifiable", "not_found"]


def test_live_page_confirms_or_refutes_snippet_quotes(live_pages):
    d = make_brief_dict()
    d["sources"][0]["quotes"] = ["Metro: \\u20ac104.39 billion (US$123.46 billion)"]  # escape copied from JSON
    brief = ResearchBrief.model_validate(d)
    transcript = [{"type": "web_search_tool_result", "caller": {"type": "direct"}, "content": [
        {"type": "web_search_result", "url": f"https://example.com/{i}"} for i in (1, 2, 3)]}]
    live_pages["https://example.com/1"] = "GDP (nominal, 2024)\n• Metro  €104.39 billion (US$123.46 billion)"
    live_pages["https://example.com/2"] = "A page that says something else."
    # example.com/3 fails to load
    live = research.live_pages_for(brief, transcript, cache={})
    statuses = [q["status"] for q in research.check_quotes(brief, transcript, live)["quotes"]]
    assert statuses == ["found_live", "not_on_page", "unverifiable"]


def test_live_pages_are_fetched_once(monkeypatch):
    calls = []
    monkeypatch.setattr(research.livepages, "fetch_texts",
                        lambda urls: calls.append(sorted(urls)) or {u: None for u in urls})
    brief = ResearchBrief.model_validate(make_brief_dict())
    cache: dict = {}
    research.live_pages_for(brief, [], cache)
    research.live_pages_for(brief, [], cache)
    assert calls == [[f"https://example.com/{i}" for i in (1, 2, 3)], []]


def test_html_to_text_drops_scripts_and_keeps_table_text():
    from pipeline import livepages
    text = livepages.html_to_text(
        "<html><script>var x = 'Quote';</script><table><tr><th>• Metro</th>"
        "<td>€104.39&nbsp;billion</td></tr></table><p>It&#x27;s here.</p></html>")
    assert "var x" not in text
    assert research._norm("Metro: €104.39 billion") in research._norm(text)
    assert "It's here." in text


def test_wikipedia_footnotes_in_html_are_ignored():
    from pipeline import livepages
    text = livepages.html_to_text(
        '<td>$240 billion (nominal; 2025)<sup class="reference"><a><span class="cite-bracket">&#91;</span>'
        '4<span class="cite-bracket">&#93;</span></a></sup><br>$460 billion (PPP; 2025)</td>')
    assert research._norm("$240 billion (nominal, 2025); $460 billion (PPP, 2025)") in research._norm(text)


def test_footnote_markers_are_ignored():
    page = "GDP  $240 billion (nominal; 2025)[4]  $460 billion (PPP; 2025)[4] GDP[1] Total €24.190 billion"
    assert research._norm("GDP: +$240 billion (nominal, 2025); +$460 billion (PPP, 2025)") in research._norm(page)
    assert research._norm("GDP - • Total: €24.190 billion") in research._norm(page)


def test_direct_search_results_are_unverifiable():
    # Basic tools mark results as {"type": "direct"}; their snippets are encrypted.
    brief = ResearchBrief.model_validate(make_brief_dict())
    transcript = [{"type": "web_search_tool_result", "caller": {"type": "direct"},
                   "content": [{"type": "web_search_result", "url": "https://example.com/1"}]}]
    assert research.check_quotes(brief, transcript)["quotes"][0]["status"] == "unverifiable"


def test_search_results_read_by_encrypted_code_are_unverifiable():
    brief = ResearchBrief.model_validate(make_brief_dict())
    transcript = [
        {"type": "web_search_tool_result", "caller": {"type": "code_execution_20260120", "tool_id": "exec_1"},
         "content": [{"type": "web_search_result", "url": "https://example.com/1"}]},
        {"type": "code_execution_tool_result", "tool_use_id": "exec_1",
         "content": {"type": "code_execution_result", "encrypted_stdout": "Eo4B...", "return_code": 0}},
    ]
    assert research.check_quotes(brief, transcript)["quotes"][0]["status"] == "unverifiable"


def test_search_results_read_by_code_are_not_hidden():
    brief = ResearchBrief.model_validate(make_brief_dict())
    transcript = [{"type": "web_search_tool_result", "caller": {"type": "code_execution_20260120"},
                   "content": [{"type": "web_search_result", "url": "https://example.com/1"}]}]
    # The model only saw what its code printed, and it printed nothing.
    assert research.check_quotes(brief, transcript)["quotes"][0]["status"] == "not_found"


def test_check_quotes_ignores_markdown_links_and_cite_markers():
    d = make_brief_dict()
    d["sources"][0]["quotes"] = ["It is Hungary's primate city with 1.7 million inhabitants and belongs to the narrow group"]
    brief = ResearchBrief.model_validate(d)
    page = ("It is Hungary's [primate city](https://en.wikipedia.org/wiki/Primate_city) with 1.7 million "
            "inhabitants [[17]](./Budapest#cite_note-18) and belongs to the narrow group "
            "of [GDP over US$120 billion](https://x/List_(GDP))")
    transcript = [{"type": "web_fetch_result", "url": "https://example.com/1",
                   "content": {"source": {"data": page}}}]
    results = research.check_quotes(brief, transcript)["quotes"]
    assert results[0]["status"] == "found_in_page"
