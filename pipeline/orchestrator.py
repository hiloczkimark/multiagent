"""Runs the stages in order. Plain code, not an LLM: the sequence is fixed.

Each stage persists its artifact, so `resume(run_id, from_stage=...)` can
re-run from any stage using the artifacts already on disk. A background stage
starts in a thread and the following stages carry on; the run waits for it
at the end, and only that wait counts towards the run's time.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable

from .agents import edit, image, research, verify, write
from .runstore import RunStore
from .tracking import Tracker


@dataclass(frozen=True)
class Stage:
    name: str
    artifact: str
    run: Callable[[RunStore, Tracker], object]
    background: bool = False


# Later steps append: format, publish.
STAGES: list[Stage] = [
    Stage("research", "01_research.json", research.run),
    Stage("write", "02_draft.json", write.run),
    Stage("image", "04_cover.json", image.run, background=True),  # needs the draft, not the edit
    Stage("edit", "03_edited.json", edit.run),
    Stage("verify", "claims.json", verify.run),
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
    background: list[tuple[Stage, threading.Thread, list[BaseException]]] = []
    for stage in stages:
        if stage.background:
            errors: list[BaseException] = []
            thread = threading.Thread(target=_run_background, args=(stage, store, tracker, errors),
                                      name=f"stage-{stage.name}", daemon=True)
            print(f"[{store.run_id}] {stage.name} started in the background", flush=True)
            thread.start()
            background.append((stage, thread, errors))
            continue
        _run_one(stage, store, tracker)

    for stage, thread, errors in background:
        if thread.is_alive():
            with tracker.stage(f"wait_{stage.name}"):
                thread.join()
            print(f"[{store.run_id}] waited {tracker.timings[f'wait_{stage.name}']['duration_s']:.1f}s "
                  f"for {stage.name}", flush=True)
        if errors:
            raise errors[0]


def _run_one(stage: Stage, store: RunStore, tracker: Tracker) -> None:
    print(f"[{store.run_id}] {stage.name} ...", flush=True)
    stage.run(store, tracker)
    t = tracker.timings[stage.name]
    print(
        f"[{store.run_id}] {stage.name} done in {t['duration_s']:.1f}s, "
        f"${tracker.stage_usd(stage.name):.4f} (run total ${tracker.total_usd:.4f})",
        flush=True,
    )


def _run_background(stage: Stage, store: RunStore, tracker: Tracker, errors: list[BaseException]) -> None:
    try:
        _run_one(stage, store, tracker)
    except BaseException as e:  # re-raised on the main thread when the run waits for it
        errors.append(e)
