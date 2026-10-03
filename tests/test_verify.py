"""Verifier: claim picking, evidence, verdicts - no network, no spend."""

import json
from types import SimpleNamespace

from conftest import make_brief_dict, resp
from pipeline.agents import verify
from pipeline.schemas import Draft, ResearchBrief
from pipeline.tracking import Tracker

BODY = (
    "Opening line without facts.\n\n"
    "## Numbers\n\n"
    "Metro GDP was €104.39 billion [1]. Births fell to 686,061 in 2024.[2][3] "
    "The city is the capital [1].\n\n"
    "Per capita it was €34,555 [1]. Growth was strong [2]. Not cited here.\n"
)


def draft(body=BODY):
    return Draft(title="T", standfirst="S", body_markdown=body)


def test_extract_claims_keeps_cited_sentences_and_moves_trailing_citations():
    claims = verify.extract_claims(draft())
    assert [c["sentence"] for c in claims] == [
        "Metro GDP was €104.39 billion [1].",
        "Births fell to 686,061 in 2024[2][3].",
        "The city is the capital [1].",
        "Per capita it was €34,555 [1].",
        "Growth was strong [2].",
    ]
    assert claims[1]["source_ids"] == [2, 3]


def test_citation_after_a_closing_quote_ends_the_sentence():
    body = 'Immigration "remains resoundingly unpopular."[8] This shift is gradual [8]. It "matters" [2].'
    assert [c["sentence"] for c in verify.extract_claims(draft(body))] == [
        'Immigration "remains resoundingly unpopular[8]."',
        "This shift is gradual [8].",
        'It "matters" [2].',
    ]


def test_pick_prefers_numeric_claims_and_is_reproducible():
    claims = verify.extract_claims(draft())
    picked = verify.pick_claims(claims, 3, "run:0")
    assert {c["id"] for c in picked} == {1, 2, 4}  # the three with numbers
    assert picked == verify.pick_claims(claims, 3, "run:0")
    assert len(verify.pick_claims(claims, 4, "run:0")) == 4


def test_page_excerpts_center_on_the_claims_numbers():
    page = "x " * 1000 + "In 2024 the metro GDP reached €104.39 billion, a record." + " y" * 1000
    excerpts = verify.page_excerpts(page, "Metro GDP was €104.39 billion [1].")
    assert len(excerpts) == 1 and "€104.39 billion, a record" in excerpts[0]
    assert verify.page_excerpts("nothing relevant", "Metro GDP was €104.39 billion [1].") == []


def test_run_writes_claims_json_and_flags_failures(store, scripted, live_pages):
    queue, requests = scripted
    store.write_json("01_research.json", make_brief_dict())
    store.write_model("03_edited.json", draft())
    live_pages["https://example.com/1"] = "Metro GDP was €104.39 billion in 2024."
    verdicts = {"checks": [
        {"claim_id": 1, "verdict": "supported", "explanation": "matches", "evidence": "€104.39 billion"},
        {"claim_id": 2, "verdict": "contradicted", "explanation": "page says 686,000", "evidence": "686,000"},
        {"claim_id": 4, "verdict": "partially_supported", "explanation": "year missing", "evidence": "€34,555"},
    ]}
    queue.append(resp("end_turn", [SimpleNamespace(type="text", text=json.dumps(verdicts))], searches=0))

    result = verify.run(store, Tracker(store))

    assert result["status"] == "needs_review"
    assert result["summary"] == {"supported": 1, "partially_supported": 1, "unsupported": 0, "contradicted": 1}
    assert store.read_json("claims.json")["claims"][0]["pages_loaded"] == [1]
    prompt = requests[0]["messages"][0]["content"]
    assert "Page excerpts:" in prompt and "€104.39 billion in 2024" in prompt  # live page shown
    assert "effort" not in requests[0]["output_config"]                    # Haiku takes no effort


def test_missing_verdict_counts_as_unsupported(store, scripted):
    queue, _ = scripted
    store.write_json("01_research.json", make_brief_dict())
    store.write_model("03_edited.json", draft())
    queue.append(resp("end_turn", [SimpleNamespace(type="text", text=json.dumps({"checks": []}))], searches=0))
    result = verify.run(store, Tracker(store))
    assert result["status"] == "needs_review" and result["summary"]["unsupported"] == 3


def test_render_claims_marks_unknown_sources():
    brief = ResearchBrief.model_validate(make_brief_dict())
    text = verify.render_claims([{"id": 1, "sentence": "X [9].", "source_ids": [9]}], brief, {})
    assert "not in the brief" in text
