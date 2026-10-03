"""Stage ordering, background stages and run time accounting - no API calls."""

import threading
import time

import pytest

from pipeline import orchestrator
from pipeline.orchestrator import Stage


def test_background_stage_overlaps_and_only_the_wait_counts(store):
    events = []
    image_started = threading.Event()

    def slow_image(store, tracker):
        with tracker.stage("image", background=True):
            image_started.set()
            time.sleep(0.3)
            events.append("image done")

    def edit(store, tracker):
        with tracker.stage("edit"):
            assert image_started.wait(1)  # image is running while edit runs
            time.sleep(0.1)
            events.append("edit done")

    stages = [Stage("image", "04_cover.json", slow_image, background=True), Stage("edit", "03_edited.json", edit)]
    orchestrator._run_stages(store, stages)

    timings = store.read_json("timings.json")
    assert events == ["edit done", "image done"]
    assert timings["image"]["background"] is True
    assert 0.1 <= timings["wait_image"]["duration_s"] < 0.3   # waited only for the remainder
    from pipeline.tracking import Tracker
    total = Tracker(store).total_seconds
    assert total == pytest.approx(timings["edit"]["duration_s"] + timings["wait_image"]["duration_s"])


def test_background_failure_is_raised_after_the_foreground_finishes(store):
    done = []

    def broken(store, tracker):
        raise RuntimeError("image blew up")

    def edit(store, tracker):
        with tracker.stage("edit"):
            done.append("edit")

    with pytest.raises(RuntimeError, match="image blew up"):
        orchestrator._run_stages(store, [Stage("image", "x", broken, background=True), Stage("edit", "y", edit)])
    assert done == ["edit"]


def test_stage_waits_for_the_background_stage_it_needs(store):
    events = []

    def slow_image(store, tracker):
        with tracker.stage("image", background=True):
            time.sleep(0.2)
            events.append("image")

    def stage(name):
        def run(store, tracker):
            with tracker.stage(name):
                events.append(name)
        return run

    orchestrator._run_stages(store, [
        Stage("image", "a", slow_image, background=True),
        Stage("edit", "b", stage("edit")),
        Stage("format", "c", stage("format"), after=("image",)),
        Stage("publish", "d", stage("publish")),
    ])
    assert events == ["edit", "image", "format", "publish"]
    assert "wait_image" in store.read_json("timings.json")


def test_pipeline_order():
    assert orchestrator.STAGE_NAMES == ["research", "write", "image", "edit", "verify", "format", "publish"]
    assert [s.name for s in orchestrator.STAGES if s.background] == ["image"]
    assert orchestrator.STAGES[orchestrator.STAGE_NAMES.index("format")].after == ("image",)
