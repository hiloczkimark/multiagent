"""Stage 6 - Format: package the edited article for the CMS.

Haiku writes the SEO fields and cover alt text; everything else is code:
citations renumbered by first appearance, a "Sources" list, Markdown and HTML
bodies, a standalone preview page, and review notes that say why a human
should look before publishing (open editor issues, failed spot-checks,
placeholder cover...).
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import ValidationError

from .. import config, llm, render
from ..runstore import RunStore
from ..schemas import Article, Cover, Draft, ResearchBrief, SeoMeta, strict_json_schema
from ..tracking import Tracker
from . import write

STAGE = "format"
AGENT = "seo"
OUTPUT_FORMAT = {"type": "json_schema", "schema": strict_json_schema(SeoMeta)}


class FormatError(RuntimeError):
    pass


def run(store: RunStore, tracker: Tracker) -> Article:
    brief = store.read_model("01_research.json", ResearchBrief)
    draft = store.read_model("03_edited.json", Draft)
    cover = store.read_json("04_cover.json")
    with tracker.stage(STAGE):
        seo = clean_seo(_seo(draft, cover.get("prompt", ""), tracker), draft.title)
        body, refs, unknown = render.renumber_citations(draft.body_markdown.strip(), brief)
        body_md = body + "\n\n" + render.references_markdown(refs)
        article = Article(
            run_id=store.run_id, title=draft.title, standfirst=draft.standfirst,
            slug=seo.slug, meta_title=seo.meta_title, meta_description=seo.meta_description, tags=seo.tags,
            body_markdown=body_md, body_html=render.markdown_to_html(body_md),
            cover=Cover(file=cover["file"], alt_text=seo.cover_alt_text,
                        placeholder=cover["placeholder"], provider=cover["provider"]),
            references=refs, word_count=write.word_count(body),
            review_notes=review_notes(store, cover, unknown),
        )
        store.write_model("05_article.json", article)
        store.path("05_article.md").write_text(full_markdown(article), encoding="utf-8")
        store.path("05_article.html").write_text(render.preview_page(
            article.title, article.standfirst, article.cover.file, article.cover.alt_text,
            article.body_html, article.review_notes), encoding="utf-8")
    return article


def full_markdown(a: Article) -> str:
    return (f"# {a.title}\n\n*{a.standfirst}*\n\n![{a.cover.alt_text}]({a.cover.file})\n\n"
            f"{a.body_markdown.strip()}\n")


def _seo(draft: Draft, image_prompt: str, tracker: Tracker) -> SeoMeta:
    lines = draft.body_markdown.strip().splitlines()
    opening = next((line for line in lines if line.strip() and not line.startswith("#")), "")
    headings = [line.lstrip("# ").strip() for line in lines if line.startswith("## ")]
    opening = re.sub(r"\[\d+\]", "", opening)  # (outside the f-string: Python < 3.12 has no backslashes there)
    content = (f"Headline: {draft.title}\nStandfirst: {draft.standfirst}\n"
               f"Opening: {opening}\nSections: {'; '.join(headings)}\n"
               f"Cover image prompt: {image_prompt}")
    response = llm.create(
        tracker, stage=STAGE, agent=AGENT, model=config.MODELS[AGENT],
        max_tokens=1000,
        system=llm.load_prompt(AGENT),
        messages=[{"role": "user", "content": content}],
        output_config={"format": OUTPUT_FORMAT},
    )
    if response.stop_reason in ("refusal", "max_tokens"):
        raise FormatError(f"SEO call stopped with {response.stop_reason}")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        return SeoMeta.model_validate_json(text)
    except ValidationError as e:
        raise FormatError(f"SEO output did not match the schema: {e}") from e


# --- Clean-up and review notes ---------------------------------------------------

def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:80] or "article"


def _trim(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0].rstrip(",;:-")
    return cut + "…"


def clean_seo(seo: SeoMeta, title: str) -> SeoMeta:
    """Enforce the limits the model was asked for; it usually gets them right."""
    tags = list(dict.fromkeys(t.strip().lower() for t in seo.tags if t.strip()))[:6]
    return SeoMeta(
        meta_title=_trim(seo.meta_title or title, 60),
        meta_description=_trim(seo.meta_description, 155),
        slug=slugify(seo.slug or title),
        tags=tags,
        cover_alt_text=_trim(seo.cover_alt_text, 125),
    )


def review_notes(store: RunStore, cover: dict[str, Any], unknown_citations: list[int]) -> list[str]:
    notes: list[str] = []
    if store.exists("eval_report.json"):
        report = store.read_json("eval_report.json")
        blocking = [i for i in report["issues"] if i["status"] == "open" and i["severity"] in ("critical", "major")]
        for i in blocking:
            notes.append(f"Editor issue left open ({i['severity']}): {i['problem']}")
        for p in report.get("final_draft_checks", {}).get("problems", []):
            notes.append(f"Draft check: {p}")
    if store.exists("claims.json"):
        claims = store.read_json("claims.json")
        for c in claims["claims"]:
            if c["verdict"] in ("unsupported", "contradicted"):
                notes.append(f"Spot-check {c['verdict']}: \"{c['sentence']}\" ({c['explanation']})")
        if not claims["claims"]:
            notes.append("Spot-check found no cited sentences to verify.")
    if unknown_citations:
        notes.append(f"Citations to unknown sources were removed: {unknown_citations}")
    if cover.get("placeholder"):
        why = f" ({cover['error']})" if cover.get("error") else " (no image provider configured)"
        notes.append("Cover image is a placeholder" + why)
    return notes
