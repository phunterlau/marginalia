import json
import pytest

from research_brain import Brain
from research_brain.context import _record_item


def test_complete_controls_with_explicit_compact_omission(tmp_path):
    brain = Brain(tmp_path)
    controls = [f"control-{i}" for i in range(23)]
    obj = brain.create_research_object(kind="experiment_result", body="test", structured={"controls": controls})
    compact = _record_item(brain.get_research_object(obj.id), "test")
    assert compact["structured_omissions"]["/controls"] == {"total": 23, "returned": 8, "omitted": 15}
    result = []
    offset, version = 0, None
    while offset is not None:
        page = brain.read_object_field(obj.id, "structured.controls", offset=offset, limit=7, expected_version=version)
        result.extend(page["items"])
        offset, version = page["next_offset"], page["version"]
    assert result == controls


def test_large_item_round_trips_without_truncation(tmp_path):
    brain = Brain(tmp_path)
    item = {"long": "é😀" * 20000}
    obj = brain.create_research_object(kind="note", body="test", structured={"values": [item]})
    page = brain.read_object_field(obj.id, "structured.values")
    assert page["requires_item_index"] == 0
    fragments, offset = [], 0
    while offset is not None:
        fragment = brain.read_object_field(obj.id, "structured.values", item_index=0, char_offset=offset, expected_version=page["version"])
        fragments.append(fragment["fragment"])
        offset = fragment["next_char_offset"]
    assert json.loads("".join(fragments)) == item


def test_review_change_invalidates_page_version(tmp_path):
    brain = Brain(tmp_path)
    obj = brain.create_research_object(kind="note", body="test", structured={"values": list(range(10))})
    page = brain.read_object_field(obj.id, "structured.values")
    brain.review_research_object(obj.id, review_state="DISPUTED", note="changed")
    with pytest.raises(ValueError, match="changed"):
        brain.read_object_field(obj.id, "structured.values", expected_version=page["version"])


def test_invalid_fields_and_bounds_fail_closed(tmp_path):
    brain = Brain(tmp_path)
    obj = brain.create_research_object(kind="note", body="test")
    for field in ("../secret", "local_path", "structured.a.b"):
        with pytest.raises(ValueError):
            brain.read_object_field(obj.id, field)
    with pytest.raises(ValueError):
        brain.read_object_field(obj.id, "body", char_limit=100000)


def test_structured_mapping_can_enumerate_all_keys(tmp_path):
    brain = Brain(tmp_path)
    values = {f"field_{i:03}": i for i in range(35)}
    obj = brain.create_research_object(kind="note", body="test", structured=values)
    first = brain.read_object_field(obj.id, "structured", limit=20)
    second = brain.read_object_field(obj.id, "structured", offset=first["next_offset"], expected_version=first["version"])
    assert {item["key"]: item["value"] for item in first["items"] + second["items"]} == values


def test_exact_evidence_fragments_keep_locator(tmp_path):
    brain = Brain(tmp_path / "brain")
    source = tmp_path / "source.txt"
    source.write_text("exact evidence " * 1000)
    doc = brain.ingest(source)
    block = brain.store.get_blocks(doc.document_id)[0]
    fragments, offset, version = [], 0, None
    while offset is not None:
        result = brain.read_evidence_field(block["id"], char_offset=offset, char_limit=500, expected_version=version)
        fragments.append(result["fragment"])
        offset, version = result["next_char_offset"], result["version"]
        assert result["origin"] == "SOURCE_EXPLICIT"
        assert result["source_locator"]["raw_sha256"] == block["raw_sha256"]
    assert "".join(fragments) == block["raw_text"]
