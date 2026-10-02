"""Stage 1 - Researcher: web search + fetch, returns a validated ResearchBrief.

The model finishes by calling the client-side `submit_brief` tool (strict
schema), which is how we get structured output in the same request that uses
the server-side web tools. Before a brief is accepted its quotes are checked
against the text the model actually saw, and ungrounded ones are sent back.
"""

from __future__ import annotations

import html
import re
from dataclasses import asdict
from datetime import date
from typing import Any

from pydantic import ValidationError

from .. import config, livepages, llm
from ..config import ResearchSettings
from ..runstore import RunStore
from ..schemas import ResearchBrief, strict_json_schema
from ..tracking import StageClock, Tracker

STAGE = "research"
AGENT = "researcher"

SUBMIT_TOOL = {
    "name": "submit_brief",
    "description": "Submit the finished research brief. Call exactly once, when research is complete.",
    "strict": True,
    "input_schema": strict_json_schema(ResearchBrief),
}

TOOL_TYPES = {
    "dynamic": ("web_search_20260209", "web_fetch_20260209"),
    "basic": ("web_search_20250305", "web_fetch_20250910"),
}

NUDGE = (
    "Stop researching now and call submit_brief with the best brief you can "
    "build from the evidence you already have."
)

# Quote statuses that make a brief go back to the model.
UNGROUNDED = ("not_found", "misattributed", "not_on_page")


class ResearchError(RuntimeError):
    pass


def run(store: RunStore, tracker: Tracker, settings: ResearchSettings | None = None) -> ResearchBrief:
    settings = settings or config.RESEARCH
    live_cache: dict[str, str | None] = {}
    with tracker.stage(STAGE) as clock:
        brief, transcript, meta = _research(store.topic, tracker, clock, settings, live_cache)
        brief = _decode_escapes(brief)
        checks = check_quotes(brief, transcript, live_pages_for(brief, transcript, live_cache))
        store.write_model("01_research.json", brief)
        store.write_json("01_research_transcript.json", transcript)
        store.write_json("01_research_checks.json", checks)
        store.write_json("01_research_meta.json", {"settings": asdict(settings), **meta})
    return brief


def tools_for(settings: ResearchSettings) -> list[dict[str, Any]]:
    search_type, fetch_type = TOOL_TYPES[settings.tools]
    return [
        {"type": search_type, "name": "web_search", "max_uses": settings.max_searches},
        {
            "type": fetch_type,
            "name": "web_fetch",
            "max_uses": settings.max_fetches,
            "max_content_tokens": settings.fetch_max_content_tokens,
        },
        SUBMIT_TOOL,
    ]


def system_prompt(settings: ResearchSettings) -> str:
    return llm.load_prompt(settings.prompt).format(
        max_searches=settings.max_searches,
        max_fetches=settings.max_fetches,
        tool_notes=llm.load_prompt(f"researcher_tools_{settings.tools}").strip(),
    )


def _research(topic: str, tracker: Tracker, clock: StageClock, settings: ResearchSettings,
              live_cache: dict[str, str | None]) -> tuple[ResearchBrief, list[Any], dict[str, int]]:
    tools, system = tools_for(settings), system_prompt(settings)
    messages: list[dict[str, Any]] = [{
        "role": "user",
        "content": f"Today's date is {date.today():%Y-%m-%d}.\n\nTopic: {topic}",
    }]
    meta = {"turns": 0, "submissions": 0, "schema_rejections": 0, "quote_rejections": 0}

    for _turn in range(settings.max_turns):
        meta["turns"] += 1
        response = llm.create(
            tracker, stage=STAGE, agent=AGENT, model=config.MODELS[AGENT],
            max_tokens=16000,
            system=system,
            tools=tools,
            messages=messages,
            output_config={"effort": settings.effort},
            cache_control={"type": "ephemeral"},
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "pause_turn":
            # Server-side tool loop hit its iteration limit; resend to resume.
            continue

        if response.stop_reason == "tool_use":
            call = next((b for b in response.content
                         if b.type == "tool_use" and b.name == "submit_brief"), None)
            if call is None:
                raise ResearchError(f"unexpected client tool call: {response.content!r}")
            meta["submissions"] += 1
            try:
                brief = ResearchBrief.model_validate(call.input)
            except ValidationError as e:
                # Tell the model what's wrong; it gets another turn to fix it.
                meta["schema_rejections"] += 1
                _reject(messages, call.id, f"The brief was rejected: {e}. "
                        "Fix these problems and call submit_brief again.")
                continue
            transcript = _jsonable(messages)
            if meta["quote_rejections"] < settings.quote_rejections:
                live = live_pages_for(brief, transcript, live_cache)
                bad = [q for q in check_quotes(brief, transcript, live)["quotes"] if q["status"] in UNGROUNDED]
                if bad:
                    meta["quote_rejections"] += 1
                    _reject(messages, call.id, _quote_feedback(bad))
                    continue
            return brief, transcript, meta

        if response.stop_reason == "end_turn":
            # Answered in prose instead of submitting.
            messages.append({"role": "user", "content": NUDGE})
            continue

        if response.stop_reason == "refusal":
            raise ResearchError(f"model refused: {getattr(response, 'stop_details', None)!r}")
        if response.stop_reason == "max_tokens":
            raise ResearchError("hit max_tokens before submitting a brief")
        raise ResearchError(f"unhandled stop_reason {response.stop_reason!r}")

    raise ResearchError(
        f"no valid brief after {settings.max_turns} turns "
        f"({clock.elapsed():.0f}s, ${tracker.stage_usd(STAGE):.3f})"
    )


def _decode_escapes(brief: ResearchBrief) -> ResearchBrief:
    """Turn literal \\u20ac-style escapes (copied from printed JSON) back into characters."""
    def fix(node: Any) -> Any:
        if isinstance(node, str):
            return _JSON_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), node)
        if isinstance(node, dict):
            return {k: fix(v) for k, v in node.items()}
        if isinstance(node, list):
            return [fix(v) for v in node]
        return node
    return ResearchBrief.model_validate(fix(brief.model_dump()))


def _reject(messages: list[dict[str, Any]], tool_use_id: str, text: str) -> None:
    messages.append({"role": "user", "content": [{
        "type": "tool_result", "tool_use_id": tool_use_id, "is_error": True, "content": text,
    }]})


def _quote_feedback(bad: list[dict[str, Any]]) -> str:
    lines = []
    reasons = {
        "misattributed": "appears in another source, not this one",
        "not_on_page": "is not on this source's page",
        "not_found": "does not appear in any text you saw",
    }
    for q in bad:
        why = reasons[q["status"]]
        lines.append(f'- source {q["source_id"]}: "{q["quote"]}" ({why})')
    return (
        "The brief was rejected because these quotes are not verbatim copies of text from "
        "their own source:\n" + "\n".join(lines) + "\n\nReplace each one with an exact copy from "
        "that source, move it to the source it came from, or remove it (and any key point that "
        "only it supports). Do not search again. Then call submit_brief again."
    )


# --- Quote check -----------------------------------------------------------

_CITE_MARKER = re.compile(r"\[\[\d+\]\]\([^)]*\)")           # Wikipedia-style [[16]](./Page#cite_note-17)
_MD_LINK = re.compile(r"\[([^\]]*)\]\((?:[^()]|\([^)]*\))*\)")  # [text](url), url may contain (...)
_JSON_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")
_FOOTNOTE = re.compile(r"\[\s*(?:\d{1,3}|[a-z]|note \d+|citation needed)\s*\]", re.IGNORECASE)  # [4], [a], [note 2]


def _norm(text: str) -> str:
    """Reduce text to its sequence of words, padded with spaces.

    Pages arrive as Markdown or HTML, snippets with entities, JSON escapes and
    " · " between excerpts, and the model sometimes copies escapes into its
    quotes. Comparing word sequences ignores all of that (and punctuation,
    bullets, table layout) while still requiring the same words in the same order.
    """
    text = html.unescape(_unescape_json(text))
    text = _CITE_MARKER.sub(" ", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _FOOTNOTE.sub(" ", text)
    return " " + " ".join(re.findall(r"\w+", text.lower())) + " "


def _unescape_json(text: str) -> str:
    # The model often prints raw JSON, so snippet text shows up with escapes.
    text = _JSON_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), text)
    return text.replace('\\"', '"').replace("\\n", "\n").replace("\\t", "\t")


def _evidence(transcript: list[Any]) -> tuple[dict[str, str], list[str], set[str]]:
    """What the model saw, as far as we can tell from the transcript.

    - pages: url -> text of every fetched page.
    - outputs: stdout of the model's code, which is how it reads search results
      and pages when the web tools run under code execution ("dynamic" tools).
    - hidden: urls of search results whose snippets reached the model in a form
      we can't read: handed to it directly (always encrypted), or read by code
      whose output came back encrypted. Quotes from these can't be checked.
    """
    pages: dict[str, str] = {}
    outputs: list[str] = []
    searches: list[tuple[bool, str | None, list[str]]] = []  # (direct?, calling code execution id, urls)
    encrypted_runs: set[str] = set()                    # code executions with encrypted stdout

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            kind = node.get("type")
            if kind == "web_fetch_result" and node.get("url"):
                data = ((node.get("content") or {}).get("source") or {}).get("data")
                if isinstance(data, str):
                    pages[node["url"]] = data
            elif kind == "web_search_tool_result":
                caller = node.get("caller") or {}
                urls = [r["url"] for r in node.get("content") or [] if isinstance(r, dict) and r.get("url")]
                searches.append((caller.get("type", "direct") == "direct", caller.get("tool_id"), urls))
            elif kind == "code_execution_tool_result":
                result = node.get("content") or {}
                if isinstance(result, dict) and result.get("encrypted_stdout"):
                    encrypted_runs.add(node.get("tool_use_id"))
            if isinstance(node.get("stdout"), str) and node["stdout"]:
                outputs.append(_unescape_json(node["stdout"]))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(transcript)
    hidden = {url for direct, code_id, urls in searches
              if direct or code_id in encrypted_runs for url in urls}
    return pages, outputs, hidden


STATUSES = ("found_in_page", "found_in_snippet", "found_live", "misattributed",
            "not_on_page", "unverifiable", "not_found")
GROUNDED = ("found_in_page", "found_in_snippet", "found_live")


def check_quotes(brief: ResearchBrief, transcript: list[Any],
                 live: dict[str, str | None] | None = None) -> dict[str, Any]:
    """Is each quote verbatim (word for word) on its source?

    `live` maps source urls to page text we fetched ourselves (None = couldn't).

    found_in_page     in the page the model fetched for its source
    found_in_snippet  in printed output that names its source's url (search snippet)
    found_live        on its source's page as we fetched it
    misattributed     in text the model saw or we fetched, but not for this source
    not_on_page       we loaded its source's page and the quote isn't there
    unverifiable      source only seen as an encrypted snippet, and we couldn't load the page
    not_found         nowhere the model could have read it
    """
    live = live or {}
    pages, outputs, hidden = _evidence(transcript)
    norm_pages = {url: _norm(text) for url, text in pages.items()}
    norm_live = {url: _norm(text) for url, text in live.items() if text}
    norm_outputs = [(out, _norm(out)) for out in outputs]
    everything = list(norm_pages.values()) + list(norm_live.values()) + [n for _, n in norm_outputs]

    results = []
    for s in brief.sources:
        for q in s.quotes:
            nq = _norm(q)
            if not nq.strip():
                status = "not_found"
            elif nq in norm_pages.get(s.url, ""):
                status = "found_in_page"
            elif any(nq in n and s.url in raw for raw, n in norm_outputs):
                status = "found_in_snippet"
            elif nq in norm_live.get(s.url, ""):
                status = "found_live"
            elif any(nq in text for text in everything):
                status = "misattributed"
            elif s.url in norm_live:
                status = "not_on_page"
            elif s.url in hidden and s.url not in pages:
                status = "unverifiable"
            else:
                status = "not_found"
            results.append({"source_id": s.id, "url": s.url, "quote": q, "status": status})

    summary: dict[str, Any] = {k: sum(r["status"] == k for r in results) for k in STATUSES}
    grounded = sum(summary[k] for k in GROUNDED)
    summary["grounded_ratio"] = round(grounded / len(results), 3) if results else 0.0
    return {"summary": summary, "fetched_urls": sorted(pages), "live_urls": sorted(live), "quotes": results}


def live_pages_for(brief: ResearchBrief, transcript: list[Any],
                   cache: dict[str, str | None]) -> dict[str, str | None]:
    """Fetch (once) the pages of sources whose quotes the transcript can't confirm."""
    first_pass = check_quotes(brief, transcript)["quotes"]
    wanted = {q["url"] for q in first_pass if q["status"] not in GROUNDED}
    missing = [url for url in wanted if url not in cache]
    cache.update(livepages.fetch_texts(missing))
    return {url: cache[url] for url in wanted}


def _jsonable(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj
