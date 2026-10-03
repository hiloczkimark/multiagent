"""Stage 3 - Editor: fact-check the draft against the brief's quotes, revise, re-check.

    review (Opus) -> revise (Sonnet) -> re-review (Opus) [-> revise -> re-review]

The re-review is what proves a fix: an issue counts as corrected only when the
next review marks it fixed. A revision happens only while critical or major
issues are open; round 2 also needs the projected run cost to stay under
RUN_SOFT_BUDGET_USD and the projected time to leave room for the later stages.
Everything caught and corrected goes to eval_report.json.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ValidationError

from .. import config, llm
from ..config import EditorSettings
from ..runstore import RunStore
from ..schemas import Draft, EditReview, ResearchBrief, Revision, strict_json_schema
from ..tracking import StageClock, Tracker
from . import write
from .research import _norm

STAGE = "edit"
EDITOR, REVISER = "editor", "reviser"
BLOCKING = ("critical", "major")


class EditError(RuntimeError):
    pass


def run(store: RunStore, tracker: Tracker, settings: EditorSettings | None = None) -> Draft:
    settings = settings or config.EDITOR
    brief = store.read_model("01_research.json", ResearchBrief)
    draft = store.read_model("02_draft.json", Draft)
    with tracker.stage(STAGE) as clock:
        final, report = _edit(store, brief, draft, tracker, clock, settings)
        store.write_model("03_edited.json", final)
        store.path("03_edited.md").write_text(write.render_markdown(final), encoding="utf-8")
        report["edit_seconds"] = round(clock.elapsed(), 1)
        store.write_json("eval_report.json", report)
    return final


def _edit(store: RunStore, brief: ResearchBrief, draft: Draft, tracker: Tracker,
          clock: StageClock, settings: EditorSettings) -> tuple[Draft, dict[str, Any]]:
    issues: dict[str, dict[str, Any]] = {}
    review = _review(brief, draft, [], tracker, settings)
    store.write_model("03_review_1.json", review)
    _add_new(issues, review, draft, review_no=1)

    current, revisions, decisions = draft, 0, []
    last_round: tuple[float, float] | None = None  # (cost, seconds) of the latest revise + re-review
    while True:
        open_issues = [i for i in issues.values() if i["status"] == "open"]
        if not any(i["severity"] in BLOCKING for i in open_issues):
            break
        if revisions >= settings.max_rounds:
            decisions.append(f"stopped after {revisions} rounds (max_rounds)")
            break
        if last_round is not None:
            allowed, why = _guard(tracker, clock, last_round, settings)
            decisions.append(why)
            if not allowed:
                break

        cost0, t0 = tracker.stage_usd(STAGE), clock.elapsed()
        revision = _revise(brief, current, open_issues, tracker, settings)
        revisions += 1
        current = revision.draft()
        store.write_model(f"03_draft_r{revisions}.json", revision)
        store.path(f"03_draft_r{revisions}.md").write_text(write.render_markdown(current), encoding="utf-8")
        for fix in revision.fixes:
            if fix.issue_id in issues:
                issues[fix.issue_id]["reviser_action"] = fix.action

        review = _review(brief, current, open_issues, tracker, settings)
        store.write_model(f"03_review_{revisions + 1}.json", review)
        _apply_statuses(issues, review, revisions)
        _add_new(issues, review, current, review_no=revisions + 1)
        last_round = (tracker.stage_usd(STAGE) - cost0, clock.elapsed() - t0)

    return current, _report(issues, revisions, decisions, review, current, brief, tracker)


# --- Calls -----------------------------------------------------------------

def _brief_block(brief: ResearchBrief) -> dict[str, Any]:
    # Identical in every review and revision of a run, so it's worth caching.
    return {"type": "text", "cache_control": {"type": "ephemeral"},
            "text": "Research brief:\n```json\n" + brief.model_dump_json(indent=1) + "\n```"}


def _issue_list(open_issues: list[dict[str, Any]]) -> str:
    keys = ("id", "severity", "category", "excerpt", "problem", "evidence", "fix")
    return json.dumps([{k: i[k] for k in keys} for i in open_issues], indent=1, ensure_ascii=False)


def _review(brief: ResearchBrief, draft: Draft, open_issues: list[dict[str, Any]],
            tracker: Tracker, settings: EditorSettings) -> EditReview:
    text = "Draft:\n```markdown\n" + write.render_markdown(draft) + "```"
    numbers = write.unsupported_numbers(draft, brief)
    if numbers:
        text += ("\n\nAn automated check found these numbers in the draft but not in the brief "
                 f"(possibly just rounding): {', '.join(numbers)}")
    if open_issues:
        text += ("\n\nThis is a re-review of a revised draft. Previously open issues "
                 "(give the status of each):\n" + _issue_list(open_issues))
    effort = settings.rereview_effort if open_issues else settings.review_effort
    return _call(EDITOR, EditReview, effort, tracker,
                 system=llm.load_prompt(EDITOR), content=[_brief_block(brief), {"type": "text", "text": text}],
                 fallbacks=True)


def _revise(brief: ResearchBrief, draft: Draft, open_issues: list[dict[str, Any]],
            tracker: Tracker, settings: EditorSettings) -> Revision:
    text = ("Current draft:\n```markdown\n" + write.render_markdown(draft) + "```\n\n"
            "Editor's issues:\n" + _issue_list(open_issues))
    return _call(REVISER, Revision, settings.revise_effort, tracker,
                 system=llm.load_prompt(REVISER), content=[_brief_block(brief), {"type": "text", "text": text}])


def _call(agent: str, schema: type[BaseModel], effort: str, tracker: Tracker, *,
          system: str, content: list[dict[str, Any]], fallbacks: bool = False) -> Any:
    response = llm.create(
        tracker, stage=STAGE, agent=agent, model=config.MODELS[agent],
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": content}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": strict_json_schema(schema)}},
        fallbacks=fallbacks,
    )
    if response.stop_reason == "refusal":
        raise EditError(f"{agent} refused: {getattr(response, 'stop_details', None)!r}")
    if response.stop_reason == "max_tokens":
        raise EditError(f"{agent} hit max_tokens")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        return schema.model_validate_json(text)
    except ValidationError as e:
        raise EditError(f"{agent} output did not match {schema.__name__}: {e}") from e


# --- Bookkeeping -----------------------------------------------------------

def _add_new(issues: dict[str, dict[str, Any]], review: EditReview, draft: Draft, review_no: int) -> None:
    text = _norm(write.render_markdown(draft))
    for n, issue in enumerate(review.issues, 1):
        issue_id = f"{review_no}.{n}"
        issues[issue_id] = {
            "id": issue_id, "found_in_review": review_no, **issue.model_dump(),
            # An excerpt that isn't in the draft means the editor misread it.
            "excerpt_in_draft": _norm(issue.excerpt).strip() in text,
            "status": "open", "fixed_in_round": None, "notes": [],
        }


def _apply_statuses(issues: dict[str, dict[str, Any]], review: EditReview, round_no: int) -> None:
    for st in review.previous_issues:
        issue = issues.get(st.id)
        if issue is None or issue["status"] != "open":
            continue
        issue["notes"].append(f"round {round_no}: {st.status}: {st.note}")
        if st.status == "fixed":
            issue["status"], issue["fixed_in_round"] = "fixed", round_no


def _guard(tracker: Tracker, clock: StageClock, last_round: tuple[float, float],
           settings: EditorSettings) -> tuple[bool, str]:
    round_cost, round_secs = last_round
    cost = tracker.total_usd + round_cost
    secs = tracker.seconds(exclude=STAGE) + clock.elapsed() + round_secs
    time_limit = config.RUN_TIME_BUDGET_S - settings.time_reserve_s
    if cost > config.RUN_SOFT_BUDGET_USD:
        return False, f"round 2 skipped: projected run cost ${cost:.3f} > ${config.RUN_SOFT_BUDGET_USD:.2f}"
    if secs > time_limit:
        return False, f"round 2 skipped: projected run time {secs:.0f}s > {time_limit:.0f}s"
    return True, f"round 2 allowed: projected run cost ${cost:.3f}, time {secs:.0f}s"


def _report(issues: dict[str, dict[str, Any]], revisions: int, decisions: list[str], last: EditReview,
            final: Draft, brief: ResearchBrief, tracker: Tracker) -> dict[str, Any]:
    all_issues = list(issues.values())
    fixed = [i for i in all_issues if i["status"] == "fixed"]
    still_open = [i for i in all_issues if i["status"] == "open"]
    return {
        "caught_and_corrected": bool(fixed),
        "counts": {
            "caught": len(all_issues),
            "corrected": len(fixed),
            "open": len(still_open),
            "open_blocking": sum(i["severity"] in BLOCKING for i in still_open),
        },
        "by_severity": {s: {"caught": sum(i["severity"] == s for i in all_issues),
                            "corrected": sum(i["severity"] == s for i in fixed)}
                        for s in ("critical", "major", "minor")},
        "revision_rounds": revisions,
        "reviews": revisions + 1,
        "round_decisions": decisions,
        "final_review_summary": last.summary,
        "final_draft_checks": write.check_draft(final, brief, config.WRITER),
        "edit_cost_usd": round(tracker.stage_usd(STAGE), 5),
        "issues": all_issues,
    }
