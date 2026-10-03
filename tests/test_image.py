"""Cover image stage and providers - no network, no spend."""

import base64
import io
import json
import urllib.error
from types import SimpleNamespace

import pytest

from conftest import resp
from pipeline import config, images
from pipeline.agents import image
from pipeline.tracking import Tracker

PNG = b"\x89PNG fake bytes"


@pytest.fixture
def drafted(store):
    store.write_json("02_draft.json", {"title": "Why reefs bleach", "standfirst": "Heat stress.", "body_markdown": "x"})
    return store


def prompt_reply(text="A calm reef illustration."):
    return resp("end_turn", [SimpleNamespace(type="text", text=text)], searches=0)


class FakeHTTP:
    def __init__(self, payload=None, error=None):
        self.payload, self.error, self.requests = payload, error, []

    def __call__(self, req, timeout):
        self.requests.append(json.loads(req.data))
        if self.error:
            raise self.error
        return io.BytesIO(json.dumps(self.payload).encode())


def test_without_a_key_a_placeholder_is_used(drafted, scripted):
    queue, _ = scripted
    queue.append(prompt_reply())
    tracker = Tracker(drafted)
    meta = image.run(drafted, tracker)
    assert meta["placeholder"] and meta["file"] == "04_cover.svg" and meta["error"] is None
    assert drafted.path("04_cover.svg").read_bytes().startswith(b"<svg")
    assert meta["prompt"] == "A calm reef illustration."
    assert tracker.timings["image"]["background"] is True
    assert [r.agent for r in tracker.records] == ["image_prompt"]  # no image cost recorded


def test_openai_image_is_saved_and_costed(drafted, scripted, monkeypatch):
    queue, _ = scripted
    queue.append(prompt_reply())
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    http = FakeHTTP({"data": [{"b64_json": base64.b64encode(PNG).decode(), "revised_prompt": "r"}]})
    monkeypatch.setattr(images.urllib.request, "urlopen", http)
    tracker = Tracker(drafted)

    meta = image.run(drafted, tracker)

    assert drafted.path("04_cover.png").read_bytes() == PNG
    assert meta["provider"] == "openai" and meta["cost_usd"] == 0.08 and meta["revised_prompt"] == "r"
    assert http.requests[0] == {"model": "dall-e-3", "prompt": "A calm reef illustration.", "n": 1,
                                "size": "1792x1024", "quality": "standard", "response_format": "b64_json"}
    assert tracker.stage_usd("image") == pytest.approx(0.08 + tracker.records[0].cost_usd)


def test_openai_failure_falls_back_to_placeholder(drafted, scripted, monkeypatch):
    queue, _ = scripted
    queue.append(prompt_reply())
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    err = urllib.error.HTTPError(images.OPENAI_IMAGES_URL, 400, "Bad", {}, io.BytesIO(b'{"error":"content policy"}'))
    monkeypatch.setattr(images.urllib.request, "urlopen", FakeHTTP(error=err))

    meta = image.run(drafted, Tracker(drafted))

    assert meta["placeholder"] and "400" in meta["error"] and "content policy" in meta["error"]


def test_gpt_image_models_are_not_sent_response_format(monkeypatch):
    http = FakeHTTP({"data": [{"b64_json": base64.b64encode(PNG).decode()}]})
    monkeypatch.setattr(images.urllib.request, "urlopen", http)
    settings = config.ImageSettings(model="gpt-image-1", quality="medium", size="1536x1024")
    images.OpenAIImageProvider("sk-test", settings).generate("p")
    assert "response_format" not in http.requests[0]


def test_empty_prompt_reply_gets_a_safe_default(drafted, scripted):
    queue, _ = scripted
    queue.append(prompt_reply(""))
    assert "Why reefs bleach" in image.run(drafted, Tracker(drafted))["prompt"]
