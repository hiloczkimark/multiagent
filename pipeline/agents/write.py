"""Stage 2 - Writer: turns the research brief into a cited Markdown draft.

One structured-output call (JSON matching `Draft`). The draft is checked in
code before it's accepted: citation ids must exist, length must be in range and
enough sources must be cited. A draft that fails goes back once with the
problems listed. Numbers that don't appear in the brief are recorded for the
editor and verifier but don't block the draft (rounding makes them fuzzy).
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError

from .. import config, llm
from ..config import WriterSettings
from ..runstore import RunStore
from ..schemas import Draft, ResearchBrief, strict_json_schema
from ..tracking import Tracker

STAGE = "write"
AGENT = "writer"

OUTPUT_FORMAT = {"type": "json_schema", "schema": strict_json_schema(Draft)}

_CITATION = re.compile(r"\[(\d+)\]")
_WORD = re.compile(r"[^\W_](?:[\w'’-]|[.,](?=\d))*")  # 4.2 and 1,339 are one word
_NUMBER = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)*")


class WriteError(RuntimeError):
    pass


def run(store: RunStore, tracker: Tracker, settings: WriterSettings | None = None) -> Draft:
    settings = settings or config.WRITER
    brief = store.read_model("01_research.json", ResearchBrief)
    with tracker.stage(STAGE):
        draft, checks = _write(brief, tracker, settings)
        store.write_model("02_draft.json", draft)
        store.path("02_draft.md").write_text(render_markdown(draft), encoding="utf-8")
        store.write_json("02_draft_checks.json", checks)
    return draft


def system_prompt(settings: WriterSettings) -> str:
    return llm.load_prompt(AGENT).format(
        min_words=settings.min_words, max_words=settings.max_words,
        target_words=(settings.min_words + settings.max_words) // 20 * 10,
    )


def _write(brief: ResearchBrief, tracker: Tracker, settings: WriterSettings) -> tuple[Draft, dict[str, Any]]:
    system = system_prompt(settings)
    messages: list[dict[str, Any]] = [{"role": "user", "content": render_brief(brief)}]
    sent_back: list[list[str]] = []  # problems of each draft that went back for revision

    for attempt in range(1, settings.max_attempts + 1):
        response = llm.create(
            tracker, stage=STAGE, agent=AGENT, model=config.MODELS[AGENT],
            max_tokens=8000,
            system=system,
            messages=messages,
            output_config={"effort": settings.effort, "format": OUTPUT_FORMAT},
            cache_control={"type": "ephemeral"},
        )
        if response.stop_reason == "refusal":
            raise WriteError(f"model refused: {getattr(response, 'stop_details', None)!r}")
        if response.stop_reason == "max_tokens":
            raise WriteError("hit max_tokens before finishing the draft")

        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            draft = Draft.model_validate_json(text)
        except ValidationError as e:  # structured output makes this unlikely
            raise WriteError(f"draft did not match the schema: {e}") from e

        checks = check_draft(draft, brief, settings)
        checks["attempts"] = attempt
        checks["sent_back"] = sent_back
        if not checks["problems"] or attempt == settings.max_attempts:
            return draft, checks
        sent_back.append(checks["problems"])
        messages += [
            {"role": "assistant", "content": response.content},
            {"role": "user", "content": _revision_request(checks["problems"])},
        ]
    raise AssertionError("unreachable")


def render_brief(brief: ResearchBrief) -> str:
    return "Write the article for this research brief.\n\n" + brief.model_dump_json(indent=1)


def render_markdown(draft: Draft) -> str:
    return f"# {draft.title}\n\n*{draft.standfirst}*\n\n{draft.body_markdown.strip()}\n"


def _revision_request(problems: list[str]) -> str:
    return ("The draft can't be accepted yet:\n" + "\n".join(f"- {p}" for p in problems)
            + "\n\nReturn the full revised article, fixing only these problems.")


# --- Checks ----------------------------------------------------------------

def word_count(markdown: str) -> int:
    return len(_WORD.findall(_CITATION.sub(" ", markdown)))


def check_draft(draft: Draft, brief: ResearchBrief, settings: WriterSettings) -> dict[str, Any]:
    body = draft.body_markdown
    known = {s.id for s in brief.sources}
    cited = sorted({int(n) for n in _CITATION.findall(body)})
    unknown = [n for n in cited if n not in known]
    words = word_count(body)
    sections = sum(1 for line in body.splitlines() if line.startswith("## "))
    min_cited = min(3, len(known))

    problems: list[str] = []
    if unknown:
        problems.append(f"citations {unknown} don't match any source id in the brief "
                        f"(valid ids: {sorted(known)}); cite only those")
    lo, hi = settings.min_words, settings.max_words
    if words < lo * (1 - settings.word_slack):
        problems.append(f"the body is {words} words; it must be {lo}-{hi}")
    elif words > hi * (1 + settings.word_slack):
        problems.append(f"the body is {words} words; it must be {lo}-{hi}, so cut it down")
    if len(set(cited) & known) < min_cited:
        problems.append(f"only {len(set(cited) & known)} sources are cited; cite at least {min_cited} "
                        "of the brief's sources where they support your sentences")
    if sections == 0:
        problems.append("the body has no '## ' section headings")

    return {
        "problems": problems,
        "word_count": words,
        "sections": sections,
        "cited_source_ids": cited,
        "uncited_source_ids": sorted(known - set(cited)),
        "unknown_citation_ids": unknown,
        "unsupported_numbers": unsupported_numbers(draft, brief),
    }


def _value(token: str) -> float | None:
    if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?", token):  # 1,339,000 or 34,555.5
        token = token.replace(",", "")
    try:
        return float(token.replace(",", "."))  # 98,6 written European-style
    except ValueError:
        return None


def _numbers(text: str) -> list[tuple[str, float]]:
    out = []
    for token in _NUMBER.findall(_CITATION.sub(" ", text)):
        value = _value(token.rstrip(".,"))
        if value is not None:
            out.append((token.rstrip(".,"), value))
    return out


def unsupported_numbers(draft: Draft, brief: ResearchBrief) -> list[str]:
    """Numbers in the draft that no number in the brief explains.

    Small counts (<= 10) are skipped, years must match exactly, anything else
    may be rounded by up to 1.5%. A soft signal for the editor, not proof.
    """
    brief_text = json.dumps(brief.model_dump(include={"key_points", "sources", "open_questions"}),
                            ensure_ascii=False)
    known = [v for _, v in _numbers(brief_text)]
    article = " ".join([draft.title, draft.standfirst, draft.body_markdown])
    missing: list[str] = []
    for token, v in _numbers(article):
        if v <= 10 and v == int(v):
            continue
        if 1800 <= v <= 2100 and v == int(v):
            ok = v in known
        else:
            ok = any(abs(v - k) <= 0.015 * max(abs(k), 1e-9) for k in known)
        if not ok and token not in missing:
            missing.append(token)
    return missing
