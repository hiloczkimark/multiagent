"""Fetch source pages ourselves so quotes can be checked against the real page.

The model often quotes search snippets, which reach it encrypted, so the
transcript alone can't confirm them. A plain GET of the cited page can. Pages
that block us, need JavaScript or aren't HTML/text (PDFs) come back as None and
their quotes stay "unverifiable".
"""

from __future__ import annotations

import concurrent.futures as cf
import urllib.request
from html.parser import HTMLParser

USER_AGENT = "Mozilla/5.0 (compatible; content-pipeline quote check)"
TIMEOUT_S = 8
MAX_BYTES = 4_000_000


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template"}
    BLOCK = {"p", "div", "br", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self.SKIP:
            self._skipping += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skipping:
            self._skipping -= 1

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.parts.append(data)


def html_to_text(html_text: str) -> str:
    parser = _TextExtractor()
    parser.feed(html_text)
    parser.close()
    # Inline tags don't separate words (block tags already added a newline).
    return "".join(parser.parts)


def fetch_text(url: str) -> str | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            kind = r.headers.get_content_type()
            if kind not in ("text/html", "text/plain", "application/xhtml+xml"):
                return None
            body = r.read(MAX_BYTES).decode(r.headers.get_content_charset() or "utf-8", errors="replace")
    except Exception:  # blocked, timed out, bad TLS...: the quote just stays unverifiable
        return None
    return body if kind == "text/plain" else html_to_text(body)


def fetch_texts(urls: list[str]) -> dict[str, str | None]:
    """url -> page text (or None), fetched in parallel."""
    urls = list(dict.fromkeys(urls))
    if not urls:
        return {}
    with cf.ThreadPoolExecutor(max_workers=min(8, len(urls))) as pool:
        return dict(zip(urls, pool.map(fetch_text, urls)))
