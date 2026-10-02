"""Writer stage against a scripted fake API - no network, no spend."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from conftest import make_brief_dict, resp
from pipeline import config
from pipeline.agents import write
from pipeline.schemas import Draft, ResearchBrief
from pipeline.tracking import Tracker

SETTINGS = replace(config.WRITER, min_words=20, max_words=60)


def body(words=30, cites="[1][2][3]"):
    filler = " ".join(["word"] * (words - 5))
    return f"Opening line {cites}.\n\n## A section\n\n{filler} and more here."


def draft_json(**overrides):
    d = {"title": "A title", "standfirst": "What this covers.", "body_markdown": body()}
    d.update(overrides)
    return json.dumps(d)


def reply(text):
    return resp("end_turn", [SimpleNamespace(type="text", text=text)], searches=0)


@pytest.fixture
def briefed(store):
    store.write_json("01_research.json", make_brief_dict())
    return store


def test_writes_draft_and_artifacts(briefed, scripted):
    queue, requests = scripted
    queue.append(reply(draft_json()))
    draft = write.run(briefed, Tracker(briefed), SETTINGS)

    assert isinstance(draft, Draft)
    assert len(requests) == 1
    assert requests[0]["output_config"]["format"]["type"] == "json_schema"
    assert '"key_points"' in requests[0]["messages"][0]["content"]  # the brief is in the prompt
    md = briefed.path("02_draft.md").read_text(encoding="utf-8")
    assert md.startswith("# A title\n\n*What this covers.*")
    checks = briefed.read_json("02_draft_checks.json")
    assert checks["problems"] == [] and checks["attempts"] == 1
    assert checks["cited_source_ids"] == [1, 2, 3]
    assert Tracker(briefed).by_agent()["writer"]["calls"] == 1


def test_bad_draft_is_sent_back_once(briefed, scripted):
    queue, requests = scripted
    queue += [reply(draft_json(body_markdown=body(cites="[1][9]"))), reply(draft_json())]
    write.run(briefed, Tracker(briefed), SETTINGS)

    assert len(requests) == 2
    feedback = requests[1]["messages"][-1]["content"]
    assert "[9]" in feedback and "only 1 sources are cited" in feedback
    checks = briefed.read_json("02_draft_checks.json")
    assert checks["attempts"] == 2 and checks["problems"] == []
    assert any("[9]" in p for p in checks["sent_back"][0])


def test_last_attempt_is_kept_with_its_problems(briefed, scripted):
    queue, _ = scripted
    short = draft_json(body_markdown="Too short [1].")
    queue += [reply(short), reply(short)]
    write.run(briefed, Tracker(briefed), SETTINGS)
    problems = briefed.read_json("02_draft_checks.json")["problems"]
    assert any("words" in p for p in problems) and any("section" in p for p in problems)


def test_refusal_fails_the_stage(briefed, scripted):
    queue, _ = scripted
    queue.append(resp("refusal", [], searches=0))
    with pytest.raises(write.WriteError, match="refused"):
        write.run(briefed, Tracker(briefed), SETTINGS)
    assert briefed.read_json("timings.json")["write"]["status"] == "failed"


def test_word_count_ignores_citations_and_markup():
    assert write.word_count("## Heading\n\nGDP rose 4.2% in 2024 [1][2]. Budapest’s share [3].") == 8


def test_unsupported_numbers():
    d = make_brief_dict()
    d["key_points"][0]["claim"] = "Metro GDP was €104.39 billion in 2024, about 1,339,000 people."
    brief = ResearchBrief.model_validate(d)
    text = ("About €104 billion in 2024 [1]. Some 1.34 million people [1]. "
            "It was 2018 data [2]. Growth hit 7.5% across 12 regions [3]. Three of 5 cities.")
    draft = Draft(title="GDP", standfirst="x", body_markdown=text)
    # 104 is 104.39 rounded and 2024 is in the brief; "5" is a small count. "1.34 million"
    # restates 1,339,000 in another unit, which we don't model, so it's flagged too.
    assert write.unsupported_numbers(draft, brief) == ["1.34", "2018", "7.5", "12"]
