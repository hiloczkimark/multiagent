"""Runs the stages in order. Plain code, not an LLM: the sequence is fixed.

Each stage persists its artifact, so `resume(run_id, from_stage=...)` can
re-run from any stage using the artifacts already on disk.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .agents import edit, research, write
from .runstore import RunStore
from .tracking import Tracker


@dataclass(frozen=True)
class Stage:
    name: str
    artifact: str
    run: Callable[[RunStore, Tracker], object]


# Later steps append: verify, image, format, publish.
STAGES: list[Stage] = [
    Stage("research", "01_research.json", research.run),
    Stage("write", "02_draft.json", write.run),
    Stage("edit", "03_edited.json", edit.run),
]
STAGE_NAMES = [s.name for s in STAGES]


def run(topic: str) -> RunStore:
    store = RunStore.create(topic)
    _run_stages(store, STAGES)
    return store


def resume(run_id: str, from_stage: str | None = None) -> RunStore:
    store = RunStore.open(run_id)
    if from_stage is None:
        # Pick up at the first stage whose artifact is missing.
        pending = [s for s in STAGES if not store.exists(s.artifact)]
    else:
        if from_stage not in STAGE_NAMES:
            raise ValueError(f"unknown stage {from_stage!r}; choose from {STAGE_NAMES}")
        pending = STAGES[STAGE_NAMES.index(from_stage):]
    _run_stages(store, pending)
    return store


def _run_stages(store: RunStore, stages: list[Stage]) -> None:
    tracker = Tracker(store)
    for stage in stages:
        print(f"[{store.run_id}] {stage.name} ...", flush=True)
        stage.run(store, tracker)
        t = tracker.timings[stage.name]
        print(
            f"[{store.run_id}] {stage.name} done in {t['duration_s']:.1f}s, "
            f"${tracker.stage_usd(stage.name):.4f} (run total ${tracker.total_usd:.4f})",
            flush=True,
        )
