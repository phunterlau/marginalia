from copy import deepcopy

import pytest

from research_brain import extraction
from research_brain.schemas import (METHOD_EXTRACTION_SCHEMA, MATH_EXTRACTION_SCHEMA,
                                    validate_schema_shape)
from test_milestones import FakeExtractionProvider


def test_method_content_only_schema_and_application_context():
    before = deepcopy(METHOD_EXTRACTION_SCHEMA)
    chunk = [{"block_id": "block_valid", "raw_text": "paired contrast"}]
    schema = extraction._chunk_schema("methods", chunk)
    output = FakeExtractionProvider().extract(schema_name="MethodCardV1", schema=schema,
                                             evidence=extraction._provider_context(chunk))["output"]
    validate_schema_shape(output, schema)
    assert "evidence" not in output["cards"][0]
    attached = extraction._attach_context("methods", output, chunk)
    assert attached["cards"][0]["evidence"] == [{"block_id": "block_valid", "relation": "source_context_only"}]
    assert "evidence" not in output["cards"][0]  # Raw response is not rewritten.
    output["cards"][0]["evidence"] = []
    with pytest.raises(ValueError, match="unexpected"):
        validate_schema_shape(output, schema)
    assert METHOD_EXTRACTION_SCHEMA == before


@pytest.mark.parametrize("context", [[], [{"block_id": "block_context"}]])
def test_math_citation_domains_and_empty_context(context):
    before = deepcopy(MATH_EXTRACTION_SCHEMA)
    chunk = [{"block_id": "block_eq", "raw_latex": "x=y", "context": context}]
    schema = extraction._chunk_schema("math", chunk)
    sent = extraction._provider_context(chunk)
    assert "block_id" not in str(sent)
    output = FakeExtractionProvider().extract(schema_name="MathCardV1", schema=schema, evidence=sent)["output"]
    validate_schema_shape(output, schema)
    # Context restrictions must not leak into other shared array schema fields.
    output["cards"][0]["assumptions"] = ["ordinary prose"]
    validate_schema_shape(output, schema)
    attached = extraction._attach_context("math", output, chunk)
    assert attached["cards"][0]["equation_block_id"] == "block_eq"
    assert attached["cards"][0]["context_block_ids"] == [r["block_id"] for r in context]
    assert "equation_block_id" not in output["cards"][0]
    with pytest.raises(ValueError, match="one equation"):
        extraction._attach_context("math", output, chunk * 2)
    output["cards"][0]["equation_block_id"] = "block_context"
    with pytest.raises(ValueError, match="unexpected"):
        validate_schema_shape(output, schema)
    assert MATH_EXTRACTION_SCHEMA == before


def test_packing_bounds_total_citation_ids_including_context():
    records = [{"block_id": f"block_eq_{i}", "section_path": "s",
                "context": [{"block_id": f"block_context_{i}_{j}"} for j in range(4)]}
               for i in range(120)]
    chunks = extraction._pack_records(records)
    assert [record for chunk in chunks for record in chunk] == records
    assert [len(extraction._chunk_ids(chunk)) for chunk in chunks] == [250, 250, 100]
    for chunk in chunks:
        extraction._chunk_schema("math", chunk)
    with pytest.raises(ValueError, match="bounds"):
        extraction._chunk_schema("math", records)


def test_content_only_pipeline_keeps_exact_math_and_raw_responses(tmp_path):
    import json
    from research_brain import Brain
    from test_milestones import PAPER
    source = tmp_path / "paper.md"
    source.write_text(PAPER + "\n\n$$\nz = x + y\n$$\n")

    class ContentOnly(FakeExtractionProvider):
        def extract(self, **request):
            assert "block_id" not in json.dumps(request["evidence"])
            if request["schema_name"] == "MathCardV1":
                assert len(request["evidence"]) == 1
            return super().extract(**request)

    brain = Brain(tmp_path / "brain", extraction_provider_factory=ContentOnly)
    doc = brain.ingest(source)
    methods = brain.extract("methods", doc.document_id, live=True)
    method = brain.get_research_object(methods.object_ids[0])
    assert method["structured"]["citation_mode"] == "source_context_only"
    assert {item["relation"] for item in method["evidence"]} == {"source_context_only"}
    math = brain.extract("math", doc.document_id, live=True)
    assert len(math.object_ids) == 2  # Equal names must not merge distinct equations.
    for object_id in math.object_ids:
        card = brain.get_research_object(object_id)
        data = card["structured"]
        assert data["exact_latex"] == brain.get_evidence(data["equation_block_id"])["raw_latex"]
        assert card["review_state"] == "UNREVIEWED"
    with brain.store.connect() as db:
        row = db.execute("SELECT raw_output_json,request_json FROM generation_runs WHERE id=?", (methods.run_id,)).fetchone()
        raw = json.loads(row[0])
        assert "evidence" not in raw["chunk_outputs"][0]["cards"][0]
        assert raw["cards"][0]["evidence"]
        assert json.loads(row[1])["source_contexts"]
    assert brain.recall("paired", kinds=["method_card", "math_card"]) == []
