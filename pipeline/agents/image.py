"""Stage 4 - Cover image: Haiku writes the image prompt, an ImageProvider draws it.

Runs in the background while the editor works (it only needs the first draft's
headline and standfirst). A failed generation falls back to a placeholder and
is recorded in 04_cover.json instead of failing the run.
"""

from __future__ import annotations

import time

from .. import config, images, llm
from ..config import ImageSettings
from ..runstore import RunStore
from ..schemas import Draft
from ..tracking import Tracker

STAGE = "image"
AGENT = "image_prompt"


def run(store: RunStore, tracker: Tracker, settings: ImageSettings | None = None) -> dict:
    settings = settings or config.IMAGE
    draft = store.read_model("02_draft.json", Draft)
    with tracker.stage(STAGE, background=True):
        prompt = image_prompt(draft, tracker)
        error = None
        t0 = time.monotonic()
        try:
            img = images.get_provider(settings).generate(prompt)
        except images.ImageError as e:
            error = str(e)
            img = images.PlaceholderImageProvider(settings).generate(prompt)
        if img.cost_usd or img.provider != "placeholder":
            tracker.record_flat(stage=STAGE, agent="image", model=img.model, cost_usd=img.cost_usd,
                                duration_s=time.monotonic() - t0, size=settings.size, quality=settings.quality)
        filename = f"04_cover.{img.ext}"
        store.path(filename).write_bytes(img.data)
        meta = {
            "file": filename, "provider": img.provider, "model": img.model, "size": settings.size,
            "prompt": prompt, "revised_prompt": img.revised_prompt, "cost_usd": img.cost_usd,
            "placeholder": img.provider == "placeholder", "error": error,
        }
        store.write_json("04_cover.json", meta)
    return meta


def image_prompt(draft: Draft, tracker: Tracker) -> str:
    response = llm.create(
        tracker, stage=STAGE, agent=AGENT, model=config.MODELS[AGENT],
        max_tokens=400,
        system=llm.load_prompt(AGENT),
        messages=[{"role": "user", "content": f"Headline: {draft.title}\nStandfirst: {draft.standfirst}"}],
    )
    text = " ".join(b.text for b in response.content if b.type == "text").strip()
    # A refusal or empty reply still gets a usable, safe prompt.
    return text or f"A clean, modern editorial illustration about {draft.title}, no text, landscape format."
