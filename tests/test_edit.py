"""Editor loop against a scripted fake API - no network, no spend.

test_seeded_fault_is_caught_and_corrected is the Definition of Done check that
the eval loop catches an issue and corrects it, with the evidence in
eval_report.json.
"""

import json
from types import SimpleNamespace

import pytest

from conftest import make_brief_dict, resp
from pipeline import config
from pipeline.agents import edit
from pipeline.tracking import Tracker

GOOD = "Budapest's GDP per capita was 145% of the EU average in 2018 [1]."
SEEDED = "Budapest had the largest such gap in the EU, smaller only than Luxembourg's [2]."
BODY = "Opening [1][2][3].\n\n## Section\n\n" + GOOD + " " + SEEDED


def reply(obj, model=None):
    r = resp("end_turn", [SimpleNamespace(type="text", text=json.dumps(obj))], searches=0)
    r.model = model
    return r


def issue(excerpt=SEEDED, severity="critical", category="wrong_fact"):
    return {"category": category, "severity": severity, "excerpt": excerpt,
            "problem": "Prague's gap (192 vs 121) is larger, so Budapest ranks third.",
            "evidence": '"Luxembourg (263% ...), ahead of Prague (192% ...) and Budapest (145% ...)"',
            "fix": "Say Budapest had one of the largest gaps, after Luxembourg and Prague."}


def review(issues=(), previous=()):
    return {"previous_issues": list(previous), "issues": list(issues), "summary": "..."}


def revision(body, fixes):
    return {"title": "T", "standfirst": "S", "body_markdown": body, "fixes": fixes}


@pytest.fixture
def drafted(store):
    store.write_json("01_research.json", make_brief_dict())
    store.write_json("02_draft.json", {"title": "T", "standfirst": "S", "body_markdown": BODY})
    return store


def test_seeded_fault_is_caught_and_corrected(drafted, scripted):
    queue, requests = scripted
    fixed_body = BODY.replace(SEEDED, "Budapest had one of the EU's largest such gaps, after Luxembourg and Prague [2].")
    queue += [
        reply(review([issue()])),
        reply(revision(fixed_body, [{"issue_id": "1.1", "action": "corrected", "note": "ranked third"}])),
        reply(review(previous=[{"id": "1.1", "status": "fixed", "note": "now third"}])),
    ]
    final = edit.run(drafted, Tracker(drafted))

    assert SEEDED not in final.body_markdown
    report = drafted.read_json("eval_report.json")
    assert report["caught_and_corrected"] is True
    assert report["counts"] == {"caught": 1, "corrected": 1, "open": 0, "open_blocking": 0}
    assert report["revision_rounds"] == 1 and report["reviews"] == 2
    seeded = report["issues"][0]
    assert seeded["excerpt_in_draft"] and seeded["fixed_in_round"] == 1 and seeded["reviser_action"] == "corrected"

    editor_call, reviser_call, rereview_call = requests
    assert editor_call["model"] == config.MODELS["editor"] and editor_call["fallbacks"] == "default"
    assert "fallbacks" not in reviser_call                      # Sonnet reviser: plain endpoint
    assert '"1.1"' in rereview_call["messages"][0]["content"][1]["text"]  # re-review lists the open issue
    for name in ("03_review_1.json", "03_draft_r1.json", "03_draft_r1.md", "03_review_2.json", "03_edited.md"):
        assert drafted.exists(name)


def test_clean_draft_is_not_revised(drafted, scripted):
    queue, requests = scripted
    queue.append(reply(review()))
    final = edit.run(drafted, Tracker(drafted))
    assert final.body_markdown == BODY and len(requests) == 1
    report = drafted.read_json("eval_report.json")
    assert report["caught_and_corrected"] is False and report["revision_rounds"] == 0


def test_minor_issues_alone_dont_trigger_a_revision(drafted, scripted):
    queue, requests = scripted
    queue.append(reply(review([issue(GOOD, "minor", "clarity")])))
    edit.run(drafted, Tracker(drafted))
    assert len(requests) == 1
    assert drafted.read_json("eval_report.json")["counts"]["open"] == 1


def test_second_round_runs_then_stops_at_max_rounds(drafted, scripted):
    queue, requests = scripted
    queue += [
        reply(review([issue()])),
        reply(revision(BODY, [{"issue_id": "1.1", "action": "kept", "note": "disagree"}])),
        reply(review(previous=[{"id": "1.1", "status": "not_fixed", "note": "still wrong"}])),
        reply(revision(BODY, [{"issue_id": "1.1", "action": "kept", "note": "disagree"}])),
        reply(review(previous=[{"id": "1.1", "status": "not_fixed", "note": "still wrong"}])),
    ]
    edit.run(drafted, Tracker(drafted))
    report = drafted.read_json("eval_report.json")
    assert len(requests) == 5 and report["revision_rounds"] == 2
    assert report["round_decisions"][0].startswith("round 2 allowed")
    assert "max_rounds" in report["round_decisions"][1]
    assert report["counts"]["open_blocking"] == 1 and report["caught_and_corrected"] is False


def test_round_two_is_skipped_when_projected_cost_is_too_high(drafted, scripted):
    queue, requests = scripted
    tracker = Tracker(drafted)
    tracker.record_flat(stage="research", agent="researcher", model="x", cost_usd=0.69, duration_s=1)
    queue += [
        reply(review([issue()])),
        reply(revision(BODY, [{"issue_id": "1.1", "action": "kept", "note": "?"}])),
        reply(review(previous=[{"id": "1.1", "status": "not_fixed", "note": "still wrong"}])),
    ]
    edit.run(drafted, tracker)
    report = drafted.read_json("eval_report.json")
    assert len(requests) == 3 and report["revision_rounds"] == 1
    assert "skipped: projected run cost" in report["round_decisions"][0]


def test_new_issue_from_a_revision_is_tracked(drafted, scripted):
    queue, _ = scripted
    new_body = BODY.replace(SEEDED, "Budapest's gap trailed Luxembourg and Prague [3].")
    queue += [
        reply(review([issue()])),
        reply(revision(new_body, [{"issue_id": "1.1", "action": "corrected", "note": ""}])),
        reply(review([issue("Budapest's gap trailed Luxembourg and Prague [3].", "major", "wrong_citation")],
                     previous=[{"id": "1.1", "status": "fixed", "note": ""}])),
        reply(revision(new_body.replace("[3]", "[2]"), [{"issue_id": "2.1", "action": "recited", "note": ""}])),
        reply(review(previous=[{"id": "2.1", "status": "fixed", "note": ""}])),
    ]
    edit.run(drafted, Tracker(drafted))
    report = drafted.read_json("eval_report.json")
    assert report["counts"]["corrected"] == 2 and report["revision_rounds"] == 2
    assert [i["id"] for i in report["issues"]] == ["1.1", "2.1"]


def test_misquoted_excerpt_is_flagged(drafted, scripted):
    queue, _ = scripted
    queue.append(reply(review([issue("A sentence that is not in the draft.", "minor", "clarity")])))
    edit.run(drafted, Tracker(drafted))
    assert drafted.read_json("eval_report.json")["issues"][0]["excerpt_in_draft"] is False


def test_refusal_fallback_is_priced_as_the_serving_model(drafted, scripted):
    queue, _ = scripted
    queue.append(reply(review(), model="claude-opus-4-8"))
    tracker = Tracker(drafted)
    edit.run(drafted, tracker)
    rec = tracker.records[-1]
    assert rec.model == "claude-opus-4-8" and rec.extra["requested_model"] == config.MODELS["editor"]
