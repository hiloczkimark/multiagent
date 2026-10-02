"""Central configuration: models, prices, tool caps, and cost/time budgets.

Prices are Anthropic first-party list prices (USD per million tokens) and must be
kept in sync with https://platform.claude.com/docs/en/about-claude/pricing.
"""

from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = ROOT / "runs"
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


# --- Models per agent ------------------------------------------------------

MODELS = {
    "researcher": "claude-sonnet-5",
    "writer": "claude-sonnet-5",
    "editor": "claude-opus-5",
    "verifier": "claude-haiku-4-5",
    "seo": "claude-haiku-4-5",
    "image_prompt": "claude-haiku-4-5",
}


# --- Pricing ---------------------------------------------------------------

@dataclass(frozen=True)
class ModelPrice:
    input_per_mtok: float
    output_per_mtok: float

    # Cache writes (5-minute TTL) bill at 1.25x input, cache reads at 0.1x input.
    @property
    def cache_write_per_mtok(self) -> float:
        return self.input_per_mtok * 1.25

    @property
    def cache_read_per_mtok(self) -> float:
        return self.input_per_mtok * 0.1


PRICES = {
    "claude-opus-5": ModelPrice(5.00, 25.00),
    "claude-opus-5-5": ModelPrice(4.00, 20.00),
    "claude-sonnet-5": ModelPrice(2.00, 10.00),
    "claude-haiku-4-5": ModelPrice(1.00, 5.00),
}

WEB_SEARCH_PRICE_PER_REQUEST = 10.00 / 1000  # $10 per 1,000 searches
# Web fetch has no per-request fee; fetched content is billed as input tokens.


# --- Research settings (the main lever on research cost and latency) ------

@dataclass(frozen=True)
class ResearchSettings:
    name: str
    prompt: str = "researcher"
    # "dynamic": web_*_20260209, the model filters results with code before reading them.
    # "basic": web_search_20250305 / web_fetch_20250910, results go straight into context.
    tools: str = "dynamic"
    effort: str = "low"
    max_searches: int = 3
    max_fetches: int = 3  # the brief needs >= 3 sources, and quotes must come from fetched pages
    fetch_max_content_tokens: int = 10000
    max_turns: int = 6  # includes pause_turn continuations and nudges
    quote_rejections: int = 1  # times a brief with ungrounded quotes is sent back


RESEARCH_PRESETS = {
    # What step 1 shipped with; kept so the benchmark has a reference point.
    "baseline": ResearchSettings(
        "baseline", prompt="researcher_v1", effort="medium", max_searches=5, max_fetches=4,
        fetch_max_content_tokens=5000, quote_rejections=0,
    ),
    "tuned": ResearchSettings("tuned"),
    # Basic tools put whole fetched pages into context, so cap them tighter.
    "tuned_basic": ResearchSettings("tuned_basic", tools="basic", fetch_max_content_tokens=6000),
}
RESEARCH = RESEARCH_PRESETS["tuned"]


# --- Writer settings -------------------------------------------------------

@dataclass(frozen=True)
class WriterSettings:
    effort: str = "low"
    # A higher floor made thin briefs come back padded with unsourced claims.
    min_words: int = 450
    max_words: int = 900
    word_slack: float = 0.15  # drafts within 15% of the range are accepted, not sent back
    max_attempts: int = 2     # first draft + one revision when checks fail


WRITER = WriterSettings()


# --- Budgets (Definition of Done: < $0.75 and < 3 min end to end) ----------

RUN_BUDGET_USD = 0.75
RUN_SOFT_BUDGET_USD = 0.70  # above this, optional work (2nd edit round) is skipped
RUN_TIME_BUDGET_S = 180

# Per-stage targets. Exceeding one doesn't fail the run; it tells the agent to
# wrap up and shows up red on the dashboard.
STAGE_BUDGET_USD = {
    "research": 0.25,
    "write": 0.06,
}
STAGE_TIME_BUDGET_S = {
    "research": 60,
    "write": 30,
}

# Acceptance bar for the research stage, checked by `cli.py bench`.
RESEARCH_TARGETS = {
    "median_s": 60,
    "max_s": 75,
    "cost_usd": 0.20,          # per run
    "grounded_ratio": 0.80,    # quotes found in a page or snippet the model saw
    "ungrounded": 0,           # not_found + misattributed quotes in accepted briefs
}

# Per-request HTTP timeout. Generous enough for a server-side search loop.
REQUEST_TIMEOUT_S = 120.0
