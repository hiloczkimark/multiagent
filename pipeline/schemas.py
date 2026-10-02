"""Typed contracts passed between pipeline stages.

Every stage reads the previous stage's model from disk and writes its own, so a
run can be resumed from any stage. Later steps add EditReport, etc.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator


class Source(BaseModel):
    id: int = Field(description="1-based id; the writer cites it as [id].")
    url: str
    title: str
    publisher: str = Field(description="Site or organisation that published it.")
    published: str = Field(description="Publication date as YYYY-MM-DD, or 'unknown'.")
    quotes: list[str] = Field(
        description="Verbatim excerpts from the page that support the key points. "
        "Copy exactly; do not paraphrase."
    )


class KeyPoint(BaseModel):
    claim: str = Field(description="One factual claim, stated plainly.")
    source_ids: list[int] = Field(description="Ids of the sources whose quotes support this claim.")


class ResearchBrief(BaseModel):
    topic: str
    angle: str = Field(description="The specific angle or thesis the article should take.")
    audience: str = Field(description="Who the article is for.")
    key_points: list[KeyPoint]
    sources: list[Source]
    open_questions: list[str] = Field(
        description="Things the research could not settle; the writer must not state these as fact."
    )

    @model_validator(mode="after")
    def _check_grounding(self) -> "ResearchBrief":
        problems: list[str] = []
        ids = [s.id for s in self.sources]
        if len(ids) != len(set(ids)):
            problems.append("source ids must be unique")
        if len(self.sources) < 3:
            problems.append(f"need at least 3 sources, got {len(self.sources)}")
        if len(self.key_points) < 4:
            problems.append(f"need at least 4 key points, got {len(self.key_points)}")
        for s in self.sources:
            if not s.quotes:
                problems.append(f"source {s.id} has no supporting quotes")
        known = set(ids)
        for i, kp in enumerate(self.key_points, 1):
            if not kp.source_ids:
                problems.append(f"key point {i} cites no source")
            missing = [sid for sid in kp.source_ids if sid not in known]
            if missing:
                problems.append(f"key point {i} cites unknown source ids {missing}")
        if problems:
            raise ValueError("; ".join(problems))
        return self


class Draft(BaseModel):
    title: str = Field(description="Headline, at most about 70 characters.")
    standfirst: str = Field(description="One or two sentences under the headline saying what the article covers.")
    body_markdown: str = Field(
        description="Article body in Markdown: '## ' section headings, no H1, no reference list. "
        "Every factual sentence ends with citations like [1] or [1][3] using source ids from the brief."
    )


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON schema for a model, shaped for `strict: true` tool use.

    Strict mode needs `additionalProperties: false` on every object. Validators
    (like ResearchBrief._check_grounding) don't appear in the schema; they run
    client-side when the tool input is validated.
    """

    def fix(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object":
                node["additionalProperties"] = False
            for value in node.values():
                fix(value)
        elif isinstance(node, list):
            for value in node:
                fix(value)

    schema = model.model_json_schema()
    fix(schema)
    return schema
