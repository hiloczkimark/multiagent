"""Citation renumbering and Markdown -> HTML."""

from conftest import make_brief_dict
from pipeline import render
from pipeline.schemas import ResearchBrief


def brief():
    return ResearchBrief.model_validate(make_brief_dict(n_sources=3))


def test_citations_renumbered_by_first_appearance_and_unknown_dropped():
    body, refs, unknown = render.renumber_citations("A [3]. B [1][3]. C [9]. D [1].", brief())
    assert body == "A [1]. B [2][1]. C. D [2]."
    assert [(r.number, r.source_id) for r in refs] == [(1, 3), (2, 1)]   # source 2 uncited: not listed
    assert unknown == [9]


def test_sources_with_the_same_url_share_one_number():
    d = make_brief_dict(n_sources=3)
    d["sources"][2]["url"] = d["sources"][0]["url"]  # source 3 is source 1 again
    body, refs, _ = render.renumber_citations("A [2]. B [3]. C [1][3].", ResearchBrief.model_validate(d))
    assert body == "A [1]. B [2]. C [2]."
    assert [(r.number, r.source_id) for r in refs] == [(1, 2), (2, 1)]


def test_references_markdown_skips_unknown_dates():
    _, refs, _ = render.renumber_citations("A [1].", brief())
    refs[0].published = "unknown"
    assert render.references_markdown(refs) == "## Sources\n\n1. [Source 1](https://example.com/1) — Example"


def test_markdown_to_html_blocks_and_inlines():
    md = ("Intro with **bold**, *italic* and a [link](https://x.org/a?b=1&c=2) [1].\n"
          "Same paragraph <script>.\n\n"
          "## Section\n\n- one\n- two\n\n"
          "## Sources\n\n1. [Title](https://e.com) — Pub\n2. Other")
    html = render.markdown_to_html(md)
    assert html.splitlines() == [
        '<p>Intro with <strong>bold</strong>, <em>italic</em> and a '
        '<a href="https://x.org/a?b=1&amp;c=2">link</a> <sup class="cite"><a href="#ref-1">[1]</a></sup>. '
        'Same paragraph &lt;script&gt;.</p>',
        "<h2>Section</h2>",
        "<ul>", "<li>one</li>", "<li>two</li>", "</ul>",
        "<h2>Sources</h2>",
        "<ol>", '<li id="ref-1"><a href="https://e.com">Title</a> — Pub</li>', '<li id="ref-2">Other</li>', "</ol>",
    ]


def test_numbered_lists_outside_sources_get_no_anchors():
    assert 'id="ref' not in render.markdown_to_html("## Steps\n\n1. a\n2. b")


def test_preview_page_shows_review_notes_escaped():
    page = render.preview_page("T & co", "S", "04_cover.svg", 'alt "x"', "<p>b</p>", ["Check <this>"])
    assert "Needs a human look" in page and "Check &lt;this&gt;" in page
    assert 'alt="alt &quot;x&quot;"' in page and "<title>T &amp; co</title>" in page
    assert "Needs a human look" not in render.preview_page("T", "S", "c", "a", "", [])
