"""On-disk layout for a run: runs/<run_id>/<artifact files>."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from .config import RUNS_DIR

M = TypeVar("M", bound=BaseModel)


def new_run_id(topic: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")[:40] or "run"
    return f"{datetime.now():%Y%m%d-%H%M%S}-{slug}"


class RunStore:
    def __init__(self, run_id: str, runs_dir: Path = RUNS_DIR):
        self.run_id = run_id
        self.dir = runs_dir / run_id

    @classmethod
    def create(cls, topic: str, runs_dir: Path = RUNS_DIR) -> "RunStore":
        store = cls(new_run_id(topic), runs_dir)
        store.dir.mkdir(parents=True, exist_ok=False)
        store.write_json("run.json", {
            "run_id": store.run_id,
            "topic": topic,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        return store

    @classmethod
    def open(cls, run_id: str, runs_dir: Path = RUNS_DIR) -> "RunStore":
        store = cls(run_id, runs_dir)
        if not (store.dir / "run.json").exists():
            raise FileNotFoundError(f"no run named {run_id!r} in {runs_dir}")
        return store

    @property
    def topic(self) -> str:
        return self.read_json("run.json")["topic"]

    def path(self, name: str) -> Path:
        return self.dir / name

    def exists(self, name: str) -> bool:
        return self.path(name).exists()

    def write_json(self, name: str, data: Any) -> Path:
        p = self.path(name)
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        return p

    def read_json(self, name: str) -> Any:
        return json.loads(self.path(name).read_text(encoding="utf-8"))

    def write_model(self, name: str, model: BaseModel) -> Path:
        p = self.path(name)
        p.write_text(model.model_dump_json(indent=2), encoding="utf-8")
        return p

    def read_model(self, name: str, cls: type[M]) -> M:
        return cls.model_validate_json(self.path(name).read_text(encoding="utf-8"))
