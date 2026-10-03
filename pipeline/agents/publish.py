"""Stage 7 - Publish: send the article to the CMS as a draft (never live)."""

from __future__ import annotations

from .. import cms, config
from ..runstore import RunStore
from ..schemas import Article
from ..tracking import Tracker

STAGE = "publish"


def run(store: RunStore, tracker: Tracker) -> dict:
    article = store.read_model("05_article.json", Article)
    with tracker.stage(STAGE):
        adapter = cms.get_cms(config.CMS, config.CMS_MOCK_DIR)
        result = adapter.publish_draft(article, {article.cover.file: store.path(article.cover.file)})
        published = {**cms.result_dict(result), "needs_review": bool(article.review_notes),
                     "review_notes": article.review_notes}
        store.write_json("06_published.json", published)
    return published
