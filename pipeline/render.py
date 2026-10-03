"""Turning the edited draft into publishable Markdown and HTML.

Citations are renumbered by first appearance so readers see [1], [2], [3] in
order, and only cited sources go in the reference list. The Markdown-to-HTML
converter covers what the writer produces (headings, paragraphs, lists,
emphasis, links, citations); everything is HTML-escaped first.
"""

from __future__ import annotations

import html
import re

from .schemas import Reference, ResearchBrief

_CITATION = re.compile(r"\[(\d+)\]")


def renumber_citations(body: str, brief: ResearchBrief) -> tuple[str, list[Reference], list[int]]:
    """Returns the body with citations renumbered, the reference list, and any ids
    the brief doesn't know (those markers are dropped)."""
    sources = {s.id: s for s in brief.sources}
    # The researcher sometimes lists one page twice under two ids: cite it once.
    first_with_url: dict[str, int] = {}
    canonical = {s.id: first_with_url.setdefault(s.url, s.id) for s in brief.sources}
    order: dict[int, int] = {}
    unknown: list[int] = []
    for m in _CITATION.finditer(body):
        sid = int(m.group(1))
        if sid in sources:
            order.setdefault(canonical[sid], len(order) + 1)
        elif sid not in unknown:
            unknown.append(sid)

    def swap(m: re.Match) -> str:
        sid = int(m.group(2))
        if sid not in canonical:
            return ""  # dropped along with its leading space
        return f"{m.group(1)}[{order[canonical[sid]]}]"

    def dedupe(text: str) -> str:  # "[2][2]" after merging -> "[2]"
        return re.sub(r"(\[\d+\])(?:\1)+", r"\1", text)

    refs = [Reference(number=n, source_id=sid, title=sources[sid].title, publisher=sources[sid].publisher,
                      published=sources[sid].published, url=sources[sid].url)
            for sid, n in sorted(order.items(), key=lambda kv: kv[1])]
    return dedupe(re.sub(r"(\s*)\[(\d+)\]", swap, body)), refs, unknown


def references_markdown(refs: list[Reference]) -> str:
    lines = ["## Sources", ""]
    for r in refs:
        date = f", {r.published}" if r.published and r.published != "unknown" else ""
        lines.append(f"{r.number}. [{r.title}]({r.url}) — {r.publisher}{date}")
    return "\n".join(lines)


# --- Markdown -> HTML ----------------------------------------------------------

_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])")
_CODE = re.compile(r"`([^`]+)`")
_ESCAPED_CITATION = re.compile(r"\[(\d+)\]")


def inline_html(text: str) -> str:
    links: list[str] = []

    def keep_link(m: re.Match) -> str:
        links.append(f'<a href="{html.escape(m.group(2), quote=True)}">{html.escape(m.group(1))}</a>')
        return f"\x00{len(links) - 1}\x00"

    text = _LINK.sub(keep_link, text)       # before escaping, so URLs survive intact
    text = html.escape(text, quote=False)
    text = _CODE.sub(r"<code>\1</code>", text)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _ITALIC.sub(r"<em>\1</em>", text)
    text = _ESCAPED_CITATION.sub(r'<sup class="cite"><a href="#ref-\1">[\1]</a></sup>', text)
    return re.sub("\x00(\\d+)\x00", lambda m: links[int(m.group(1))], text)


def markdown_to_html(markdown: str) -> str:
    out: list[str] = []
    paragraph: list[str] = []
    list_kind: str | None = None
    in_sources = False  # numbered items under "## Sources" are citation targets

    def flush_paragraph() -> None:
        if paragraph:
            out.append(f"<p>{inline_html(' '.join(paragraph))}</p>")
            paragraph.clear()

    def close_list() -> None:
        nonlocal list_kind
        if list_kind:
            out.append(f"</{list_kind}>")
            list_kind = None

    for raw in markdown.splitlines():
        line = raw.strip()
        heading = re.match(r"(#{1,6})\s+(.*)", line)
        bullet = re.match(r"[-*]\s+(.*)", line)
        numbered = re.match(r"(\d+)\.\s+(.*)", line)
        if not line:
            flush_paragraph()
            close_list()
        elif heading:
            flush_paragraph()
            close_list()
            level = len(heading.group(1))
            in_sources = heading.group(2).strip().lower() == "sources"
            out.append(f"<h{level}>{inline_html(heading.group(2))}</h{level}>")
        elif bullet or numbered:
            flush_paragraph()
            kind = "ul" if bullet else "ol"
            if list_kind != kind:
                close_list()
                start = f' start="{numbered.group(1)}"' if numbered and numbered.group(1) != "1" else ""
                out.append(f"<{kind}{start}>")
                list_kind = kind
            item = bullet.group(1) if bullet else numbered.group(2)
            ident = f' id="ref-{numbered.group(1)}"' if numbered and in_sources else ""
            out.append(f"<li{ident}>{inline_html(item)}</li>")
        else:
            close_list()
            paragraph.append(line)
    flush_paragraph()
    close_list()
    return "\n".join(out)


def preview_page(title: str, standfirst: str, cover_file: str, cover_alt: str, body_html: str,
                 review_notes: list[str]) -> str:
    notes = ""
    if review_notes:
        items = "".join(f"<li>{html.escape(n)}</li>" for n in review_notes)
        notes = f'<aside class="review"><strong>Needs a human look before publishing:</strong><ul>{items}</ul></aside>'
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
  body {{ font: 18px/1.6 Georgia, serif; max-width: 42rem; margin: 2rem auto; padding: 0 1rem; color: #1d1d1f; background: #fff; }}
  h1 {{ font-size: 2.1rem; line-height: 1.2; margin-bottom: .4rem; }}
  .standfirst {{ font-size: 1.2rem; color: #444; margin-top: 0; }}
  img.cover {{ width: 100%; height: auto; border-radius: 4px; margin: 1rem 0; }}
  sup.cite a {{ text-decoration: none; font-size: .75em; }}
  aside.review {{ border-left: 4px solid #c77c02; background: #fff7e6; padding: .6rem 1rem; font: 15px/1.5 sans-serif; }}
</style></head>
<body><article>
{notes}
<h1>{html.escape(title)}</h1>
<p class="standfirst">{html.escape(standfirst)}</p>
<img class="cover" src="{html.escape(cover_file, quote=True)}" alt="{html.escape(cover_alt, quote=True)}">
{body_html}
</article></body></html>
"""
