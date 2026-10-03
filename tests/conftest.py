import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import livepages, llm  # noqa: E402
from pipeline.runstore import RunStore  # noqa: E402


def resp(stop_reason, content, searches=1):
    """A fake Messages API response."""
    usage = SimpleNamespace(input_tokens=1000, output_tokens=200, cache_creation_input_tokens=0,
                            cache_read_input_tokens=0,
                            server_tool_use=SimpleNamespace(web_search_requests=searches, web_fetch_requests=0))
    return SimpleNamespace(stop_reason=stop_reason, content=content, usage=usage)


@pytest.fixture
def scripted(monkeypatch):
    """Replace the API client with a queue of canned responses."""
    queue, requests = [], []

    class FakeMessages:
        def create(self, **kwargs):
            # Snapshot: the agent keeps appending to the same list after the call.
            requests.append({**kwargs, "messages": list(kwargs["messages"])})
            return queue.pop(0)

    fake = SimpleNamespace(messages=FakeMessages(), beta=SimpleNamespace(messages=FakeMessages()))
    monkeypatch.setattr(llm, "client", lambda: fake)
    return queue, requests


@pytest.fixture(autouse=True)
def no_openai_key(monkeypatch):
    """Image tests must never reach OpenAI; tests that need a key set a fake one."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def live_pages(monkeypatch):
    """No network in tests: every live page 'fails to load' unless a test fills this dict."""
    pages: dict[str, str] = {}
    monkeypatch.setattr(livepages, "fetch_texts", lambda urls: {u: pages.get(u) for u in urls})
    return pages


@pytest.fixture
def store(tmp_path):
    return RunStore.create("Test topic", runs_dir=tmp_path)


def make_brief_dict(n_sources=3, n_points=4):
    return {
        "topic": "Test topic",
        "angle": "An angle",
        "audience": "General readers",
        "key_points": [{"claim": f"Claim {i}", "source_ids": [1 + i % n_sources]} for i in range(n_points)],
        "sources": [
            {
                "id": i, "url": f"https://example.com/{i}", "title": f"Source {i}",
                "publisher": "Example", "published": "2026-01-01",
                "quotes": [f"Quote number {i} from the page."],
            }
            for i in range(1, n_sources + 1)
        ],
        "open_questions": [],
    }
