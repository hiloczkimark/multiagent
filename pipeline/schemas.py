"""Typed contracts passed between pipeline stages.

Every stage reads the previous stage's model from disk and writes its own, so a
run can be resumed from any stage.
"""

from __future__ import annotations

from typing import Any, Literal

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


Severity = Literal["critical", "major", "minor"]
IssueCategory = Literal[
    "wrong_fact",          # contradicts the quotes (number, year, unit, scope, ranking, comparison)
    "unsupported",         # no quote supports it, incl. facts from outside the brief
    "wrong_citation",      # the cited source doesn't say it (another source might)
    "open_question_as_fact",
    "brief_error",         # repeats a key point that the brief's own quotes contradict
    "clarity",             # misleading or confusing wording, not a factual error
]


class ReviewIssue(BaseModel):
    category: IssueCategory
    severity: Severity = Field(description="critical: factually wrong. major: unsupported or miscited. "
                                           "minor: clarity or emphasis.")
    excerpt: str = Field(description="The problematic passage, copied exactly from the draft.")
    problem: str = Field(description="What is wrong, in one or two sentences.")
    evidence: str = Field(description="What the brief's quotes actually say (quote them), or 'no quote supports this'.")
    fix: str = Field(description="A concrete fix that stays within the evidence: correct, soften, re-cite or remove.")


class IssueStatus(BaseModel):
    id: str = Field(description="Id of an issue from the previous review.")
    status: Literal["fixed", "not_fixed"]
    note: str = Field(description="One sentence on what changed, or what is still wrong.")


class EditReview(BaseModel):
    previous_issues: list[IssueStatus] = Field(
        description="Status of every issue listed as previously open, in the revised draft. Empty on a first review."
    )
    issues: list[ReviewIssue] = Field(description="New problems only; don't repeat previously open issues.")
    summary: str = Field(description="One or two sentences on the draft's factual state.")


class Fix(BaseModel):
    issue_id: str
    action: Literal["corrected", "removed", "softened", "recited", "kept"]
    note: str = Field(description="What you changed; for 'kept', why the issue doesn't apply.")


class Revision(Draft):
    fixes: list[Fix] = Field(description="One entry per issue you were given.")

    def draft(self) -> Draft:
        return Draft(title=self.title, standfirst=self.standfirst, body_markdown=self.body_markdown)


class ClaimVerdict(BaseModel):
    claim_id: int
    verdict: Literal["supported", "partially_supported", "unsupported", "contradicted"]
    explanation: str = Field(description="One or two sentences: what matches and what doesn't.")
    evidence: str = Field(description="The decisive passage, copied exactly from a quote or page excerpt, "
                                      "or 'none' if nothing addresses the claim.")


class Verification(BaseModel):
    checks: list[ClaimVerdict]


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
