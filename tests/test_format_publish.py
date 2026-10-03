"""Format and publish stages - no network, no spend."""

import json
from types import SimpleNamespace

import pytest

from conftest import make_brief_dict, resp
from pipeline import cms, config
from pipeline.agents import format as fmt
from pipeline.agents import publish
from pipeline.schemas import Article, SeoMeta
from pipeline.tracking import Tracker

SEO = {"meta_title": "Why coral reefs bleach", "meta_description": "Heat stress breaks the partnership "
       "between corals and algae.", "slug": "Why Coral Reefs Bleach!", "tags": ["Coral", "oceans", "coral"],
       "cover_alt_text": "Illustration of a pale reef under bright sunlight"}


@pytest.fixture
def edited(store):
    store.write_json("01_research.json", make_brief_dict())
    store.write_json("03_edited.json", {"title": "Reefs", "standfirst": "S",
                                        "body_markdown": "Opening [2].\n\n## Why\n\nHeat [1][2]. Odd [7]."})
    store.write_json("04_cover.json", {"file": "04_cover.svg", "placeholder": True, "provider": "placeholder",
                                       "prompt": "a pale reef", "error": None})
    store.path("04_cover.svg").write_text("<svg/>", encoding="utf-8")
    store.write_json("eval_report.json", {"issues": [
        {"status": "open", "severity": "major", "problem": "Claim X is unsupported."},
        {"status": "open", "severity": "minor", "problem": "Wording."},
        {"status": "fixed", "severity": "critical", "problem": "Fixed one."},
    ], "final_draft_checks": {"problems": []}})
    store.write_json("claims.json", {"claims": [
        {"sentence": "Heat [1][2].", "verdict": "contradicted", "explanation": "page differs"},
        {"sentence": "Opening [2].", "verdict": "supported", "explanation": "ok"},
    ]})
    return store


def seo_reply(seo=SEO):
    return resp("end_turn", [SimpleNamespace(type="text", text=json.dumps(seo))], searches=0)


def test_format_packages_the_article(edited, scripted):
    queue, requests = scripted
    queue.append(seo_reply())
    article = fmt.run(edited, Tracker(edited))

    assert article.slug == "why-coral-reefs-bleach" and article.tags == ["coral", "oceans"]
    assert article.body_markdown.startswith("Opening [1].\n\n## Why\n\nHeat [2][1]. Odd.")
    assert "## Sources\n\n1. [Source 2](https://example.com/2)" in article.body_markdown
    assert [r.source_id for r in article.references] == [2, 1]
    assert '<sup class="cite"><a href="#ref-1">[1]</a></sup>' in article.body_html
    notes = article.review_notes
    assert any("Editor issue left open (major): Claim X" in n for n in notes)
    assert not any("Wording" in n or "Fixed one" in n for n in notes)       # minor/fixed issues don't block
    assert any(n.startswith("Spot-check contradicted") for n in notes)
    assert any("unknown sources were removed: [7]" in n for n in notes)
    assert any("placeholder" in n for n in notes)
    for name in ("05_article.json", "05_article.md", "05_article.html"):
        assert edited.exists(name)
    assert "Opening: Opening ." in requests[0]["messages"][0]["content"]  # citations stripped for the SEO model


def test_clean_seo_enforces_limits():
    long = SeoMeta(meta_title="x" * 80, meta_description="word " * 60, slug="", tags=[" A ", "a", "b"],
                   cover_alt_text="y " * 100)
    s = fmt.clean_seo(long, "My Title")
    assert len(s.meta_title) <= 60 and len(s.meta_description) <= 155 and len(s.cover_alt_text) <= 125
    assert s.meta_description.endswith("…") and s.slug == "my-title" and s.tags == ["a", "b"]


def test_publish_creates_then_updates_one_draft(edited, scripted, tmp_path, monkeypatch):
    queue, _ = scripted
    queue.append(seo_reply())
    monkeypatch.setattr(config, "CMS_MOCK_DIR", tmp_path / "cms")
    fmt.run(edited, Tracker(edited))

    first = publish.run(edited, Tracker(edited))
    second = publish.run(edited, Tracker(edited))

    assert first["status"] == second["status"] == "draft"
    assert first["created"] is True and second["created"] is False and second["revision"] == 2
    assert first["needs_review"] is True and first["review_notes"]
    post_dir = tmp_path / "cms" / "posts" / f"post-{edited.run_id}"
    assert (post_dir / "04_cover.svg").exists() and (post_dir / "preview.html").exists()
    post = json.loads((post_dir / "post.json").read_text(encoding="utf-8"))
    assert post["status"] == "draft" and Article.model_validate(post["article"]).slug == "why-coral-reefs-bleach"
    posts = cms.MockCMS(tmp_path / "cms").list_posts()
    assert len(posts) == 1 and posts[0]["revision"] == 2


def test_mock_cms_cannot_publish_live():
    assert not any(name.startswith("publish") and name != "publish_draft" for name in dir(cms.MockCMS))
