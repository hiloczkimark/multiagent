"""Stage 5 - Verifier: spot-check claims of the edited article (Definition of Done: 3).

Claims are picked in code: cited sentences, numeric ones first (they are the
riskiest), in an order seeded by the run id so a spot-check is reproducible and
a fresh seed draws different claims. Haiku judges each claim against its
sources' quotes and against excerpts of the live source pages, which is
independent of the researcher's choice of quotes. Results go to claims.json;
anything unsupported or contradicted marks the run "needs_review".
"""

from __future__ import annotations

import random
import re
from typing import Any

from pydantic import ValidationError

from .. import config, livepages, llm
from ..runstore import RunStore
from ..schemas import Draft, ResearchBrief, Verification, strict_json_schema
from ..tracking import Tracker
from . import write

STAGE = "verify"
AGENT = "verifier"
OUTPUT_FORMAT = {"type": "json_schema", "schema": strict_json_schema(Verification)}
FAILING = ("unsupported", "contradicted")

_CITES_AFTER_STOP = re.compile(r"([.!?])([\"”’']?)((?:\[\d+\])+)")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|(?<=[.!?][\"”’'])\s+")


class VerifyError(RuntimeError):
    pass


def run(store: RunStore, tracker: Tracker, seed: int = 0, n_claims: int | None = None) -> dict[str, Any]:
    n_claims = n_claims or config.VERIFY_CLAIMS
    brief = store.read_model("01_research.json", ResearchBrief)
    article = store.read_model("03_edited.json", Draft)
    with tracker.stage(STAGE):
        candidates = extract_claims(article)
        chosen = pick_claims(candidates, n_claims, f"{store.run_id}:{seed}")
        pages = livepages.fetch_texts(sorted({brief_url(brief, sid) for c in chosen for sid in c["source_ids"]}
                                             - {None}))
        verdicts = _judge(chosen, brief, pages, tracker) if chosen else {}
        checked = [{**c, **verdicts.get(c["id"], {"verdict": "unsupported", "explanation": "no verdict returned",
                                                  "evidence": "none"}),
                    "pages_loaded": [sid for sid in c["source_ids"] if pages.get(brief_url(brief, sid))]}
                   for c in chosen]
        summary = {v: sum(c["verdict"] == v for c in checked)
                   for v in ("supported", "partially_supported", "unsupported", "contradicted")}
        result = {
            "status": "needs_review" if any(c["verdict"] in FAILING for c in checked) or not checked else "pass",
            "seed": seed, "candidates": len(candidates), "summary": summary, "claims": checked,
        }
        store.write_json("claims.json", result)
    return result


# --- Picking claims ----------------------------------------------------------

def extract_claims(draft: Draft) -> list[dict[str, Any]]:
    """Every sentence of the body that cites a source."""
    claims = []
    for line in draft.body_markdown.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = _CITES_AFTER_STOP.sub(r"\3\1\2", line)  # 'Welfare.[2]' -> 'Welfare[2].', 'x."[8]' -> 'x[8]."'
        for sentence in _SENTENCE_END.split(line):
            ids = sorted({int(n) for n in re.findall(r"\[(\d+)\]", sentence)})
            if ids:
                claims.append({"id": len(claims) + 1, "sentence": sentence.strip(), "source_ids": ids})
    return claims


def pick_claims(claims: list[dict[str, Any]], n: int, seed: str) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    numeric = [c for c in claims if re.search(r"\d", re.sub(r"\[\d+\]", "", c["sentence"]))]
    other = [c for c in claims if c not in numeric]
    rng.shuffle(numeric)
    rng.shuffle(other)
    return sorted((numeric + other)[:n], key=lambda c: c["id"])


# --- Evidence ----------------------------------------------------------------

def brief_url(brief: ResearchBrief, source_id: int) -> str | None:
    return next((s.url for s in brief.sources if s.id == source_id), None)


def page_excerpts(page: str, sentence: str, max_windows: int = 2) -> list[str]:
    """Windows of the page around the claim's numbers (or, failing that, its rarer words)."""
    text = re.sub(r"\s+", " ", page)
    lower = text.lower()
    terms = [tok for tok, _ in write._numbers(sentence) if len(tok) > 1]
    terms += sorted({w for w in re.findall(r"[a-z][a-z'-]{6,}", sentence.lower())}, key=len, reverse=True)
    half = config.VERIFY_PAGE_EXCERPT_CHARS // 2
    windows: list[tuple[int, int]] = []
    for term in terms:
        i = lower.find(term.lower())
        if i < 0 or any(a <= i <= b for a, b in windows):
            continue
        windows.append((max(0, i - half), min(len(text), i + half)))
        if len(windows) == max_windows:
            break
    return ["…" + text[a:b] + "…" for a, b in sorted(windows)]


def render_claims(claims: list[dict[str, Any]], brief: ResearchBrief, pages: dict[str, str | None]) -> str:
    sources = {s.id: s for s in brief.sources}
    parts = []
    for c in claims:
        lines = [f"## Claim {c['id']}", c["sentence"]]
        for sid in c["source_ids"]:
            s = sources.get(sid)
            if s is None:
                lines.append(f"\n### Source [{sid}]: not in the brief (no evidence)")
                continue
            lines.append(f"\n### Source [{sid}]: {s.title} ({s.publisher}, {s.published}) {s.url}")
            lines += ["Quotes:"] + [f'- "{q}"' for q in s.quotes]
            page = pages.get(s.url)
            excerpts = page_excerpts(page, c["sentence"]) if page else []
            if excerpts:
                lines += ["Page excerpts:"] + [f"- {e}" for e in excerpts]
            else:
                lines.append("Page excerpts: none (page not loaded or no matching passage)")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


# --- Judging -------------------------------------------------------------------

def _judge(claims: list[dict[str, Any]], brief: ResearchBrief, pages: dict[str, str | None],
           tracker: Tracker) -> dict[int, dict[str, str]]:
    response = llm.create(
        tracker, stage=STAGE, agent=AGENT, model=config.MODELS[AGENT],
        max_tokens=3000,
        system=llm.load_prompt(AGENT),
        messages=[{"role": "user", "content": render_claims(claims, brief, pages)}],
        output_config={"format": OUTPUT_FORMAT},
    )
    if response.stop_reason in ("refusal", "max_tokens"):
        raise VerifyError(f"verifier stopped with {response.stop_reason}")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        result = Verification.model_validate_json(text)
    except ValidationError as e:
        raise VerifyError(f"verifier output did not match the schema: {e}") from e
    return {v.claim_id: v.model_dump(exclude={"claim_id"}) for v in result.checks}
