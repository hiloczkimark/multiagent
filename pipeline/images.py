"""Image generation behind a small interface, so the provider can be swapped.

OpenAIImageProvider calls the OpenAI Images API over plain HTTPS (no extra
dependency). PlaceholderImageProvider makes a neutral SVG, used when there is
no OPENAI_API_KEY or when generation fails, so a run never breaks on the image.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from .config import IMAGE_PRICES, ImageSettings

OPENAI_IMAGES_URL = "https://api.openai.com/v1/images/generations"


class ImageError(RuntimeError):
    pass


@dataclass
class GeneratedImage:
    data: bytes
    ext: str                    # file extension: "png", "svg"
    provider: str
    model: str
    cost_usd: float
    revised_prompt: str | None = None


class ImageProvider(Protocol):
    name: str

    def generate(self, prompt: str) -> GeneratedImage: ...


class OpenAIImageProvider:
    name = "openai"

    def __init__(self, api_key: str, settings: ImageSettings):
        self.api_key = api_key
        self.settings = settings

    def generate(self, prompt: str) -> GeneratedImage:
        s = self.settings
        body = {"model": s.model, "prompt": prompt, "n": 1, "size": s.size, "quality": s.quality}
        if s.model.startswith("dall-e"):
            body["response_format"] = "b64_json"  # gpt-image models always return base64
        req = urllib.request.Request(
            OPENAI_IMAGES_URL, data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=s.timeout_s) as r:
                payload = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:500]
            raise ImageError(f"OpenAI images API returned {e.code}: {detail}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            raise ImageError(f"OpenAI images API unreachable: {e}") from e
        try:
            item = payload["data"][0]
            data = base64.b64decode(item["b64_json"])
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise ImageError(f"unexpected OpenAI images response: {str(payload)[:300]}") from e
        return GeneratedImage(
            data=data, ext="png", provider=self.name, model=s.model,
            cost_usd=IMAGE_PRICES.get((s.model, s.quality, s.size), 0.0),
            revised_prompt=item.get("revised_prompt"),
        )


class PlaceholderImageProvider:
    name = "placeholder"

    def __init__(self, settings: ImageSettings):
        self.width, self.height = (int(x) for x in settings.size.split("x"))

    def generate(self, prompt: str) -> GeneratedImage:
        w, h = self.width, self.height
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
            '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
            '<stop offset="0" stop-color="#2f4858"/><stop offset="1" stop-color="#86bbd8"/>'
            '</linearGradient></defs>'
            f'<rect width="{w}" height="{h}" fill="url(#g)"/>'
            f'<text x="{w // 2}" y="{h // 2}" fill="#ffffff" fill-opacity="0.7" font-family="sans-serif" '
            f'font-size="{h // 18}" text-anchor="middle">Cover image placeholder</text></svg>'
        )
        return GeneratedImage(data=svg.encode("utf-8"), ext="svg", provider=self.name,
                              model="placeholder", cost_usd=0.0)


def get_provider(settings: ImageSettings) -> ImageProvider:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if settings.provider == "placeholder" or (settings.provider == "auto" and not key):
        return PlaceholderImageProvider(settings)
    if not key:
        raise ImageError("image provider 'openai' needs OPENAI_API_KEY")
    return OpenAIImageProvider(key, settings)
