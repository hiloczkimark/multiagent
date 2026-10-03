"""CMS adapters. The pipeline only ever creates or updates *drafts*.

MockCMS stands in for a real CMS: posts go to cms_mock/posts/<post_id>/ with
the article JSON, the cover and a preview page, and cms_mock/index.json lists
them. It has no way to make a post live, by design: a human publishes. Posts are
keyed by run id, so publishing the same run again updates its draft instead of
creating a duplicate. A real adapter (WordPress, Ghost, ...) implements
publish_draft the same way.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from . import render
from .schemas import Article


@dataclass
class PublishResult:
    cms: str
    post_id: str
    status: str          # always "draft"
    url: str
    created: bool        # False when an existing draft was updated
    revision: int


class CMSAdapter(Protocol):
    name: str

    def publish_draft(self, article: Article, assets: dict[str, Path]) -> PublishResult: ...


class MockCMS:
    name = "mock"

    def __init__(self, root: Path):
        self.root = root

    def publish_draft(self, article: Article, assets: dict[str, Path]) -> PublishResult:
        post_id = f"post-{article.run_id}"
        post_dir = self.root / "posts" / post_id
        post_path = post_dir / "post.json"
        now = datetime.now(timezone.utc).isoformat()
        previous = json.loads(post_path.read_text(encoding="utf-8")) if post_path.exists() else None

        post_dir.mkdir(parents=True, exist_ok=True)
        for name, src in assets.items():
            shutil.copyfile(src, post_dir / name)
        post = {
            "id": post_id,
            "status": "draft",
            "created_at": previous["created_at"] if previous else now,
            "updated_at": now,
            "revision": previous["revision"] + 1 if previous else 1,
            "needs_review": bool(article.review_notes),
            "article": article.model_dump(),
        }
        post_path.write_text(json.dumps(post, indent=2, ensure_ascii=False), encoding="utf-8")
        preview = post_dir / "preview.html"
        preview.write_text(render.preview_page(article.title, article.standfirst, article.cover.file,
                                               article.cover.alt_text, article.body_html,
                                               article.review_notes), encoding="utf-8")
        self._index(post)
        return PublishResult(cms=self.name, post_id=post_id, status="draft", url=preview.resolve().as_uri(),
                             created=previous is None, revision=post["revision"])

    def list_posts(self) -> list[dict]:
        index = self.root / "index.json"
        return json.loads(index.read_text(encoding="utf-8")) if index.exists() else []

    def _index(self, post: dict) -> None:
        a = post["article"]
        entry = {"id": post["id"], "status": post["status"], "title": a["title"], "slug": a["slug"],
                 "updated_at": post["updated_at"], "revision": post["revision"],
                 "needs_review": post["needs_review"]}
        posts = [p for p in self.list_posts() if p["id"] != post["id"]] + [entry]
        posts.sort(key=lambda p: p["updated_at"], reverse=True)
        (self.root / "index.json").write_text(json.dumps(posts, indent=2, ensure_ascii=False), encoding="utf-8")


def get_cms(name: str, root: Path) -> CMSAdapter:
    if name == "mock":
        return MockCMS(root)
    raise ValueError(f"no CMS adapter named {name!r}")


def result_dict(result: PublishResult) -> dict:
    return asdict(result)
