import pytest
from pydantic import ValidationError

from conftest import make_brief_dict
from pipeline.schemas import ResearchBrief, strict_json_schema


def test_valid_brief():
    brief = ResearchBrief.model_validate(make_brief_dict())
    assert len(brief.sources) == 3


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d["sources"].pop(), "at least 3 sources"),
    (lambda d: d["key_points"][0].update(source_ids=[99]), "unknown source ids"),
    (lambda d: d["key_points"][0].update(source_ids=[]), "cites no source"),
    (lambda d: d["sources"][0].update(quotes=[]), "no supporting quotes"),
    (lambda d: d["sources"][1].update(id=1), "unique"),
])
def test_grounding_rules(mutate, message):
    d = make_brief_dict()
    mutate(d)
    with pytest.raises(ValidationError, match=message):
        ResearchBrief.model_validate(d)


def test_strict_schema_closes_every_object():
    schema = strict_json_schema(ResearchBrief)

    def objects(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                yield node
            for v in node.values():
                yield from objects(v)
        elif isinstance(node, list):
            for v in node:
                yield from objects(v)

    found = list(objects(schema))
    assert len(found) >= 3  # brief, source, key point
    assert all(o["additionalProperties"] is False for o in found)
