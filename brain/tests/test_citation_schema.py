from copy import deepcopy

import pytest

from research_brain import extraction
from research_brain.schemas import (METHOD_EXTRACTION_SCHEMA, MATH_EXTRACTION_SCHEMA,
                                    validate_schema_shape)
from test_milestones import FakeExtractionProvider


def test_method_citation_enum_is_exact_and_does_not_mutate_template():
    before = deepcopy(METHOD_EXTRACTION_SCHEMA)
    chunk = [{"block_id": "block_valid", "raw_text": "paired contrast"}]
    schema = extraction._chunk_schema("methods", chunk)
    output = FakeExtractionProvider().extract(schema_name="MethodCardV1", evidence=chunk)["output"]
    validate_schema_shape(output, schema)
    for invalid in ["block_vali", "block_other_compilation", "block_other_section"]:
        output["cards"][0]["evidence"][0]["block_id"] = invalid
        with pytest.raises(ValueError, match="enum"):
            validate_schema_shape(output, schema)
    assert METHOD_EXTRACTION_SCHEMA == before


@pytest.mark.parametrize("context", [[], [{"block_id": "block_context"}]])
def test_math_citation_domains_and_empty_context(context):
    before = deepcopy(MATH_EXTRACTION_SCHEMA)
    chunk = [{"block_id": "block_eq", "raw_latex": "x=y", "context": context}]
    schema = extraction._chunk_schema("math", chunk)
    output = FakeExtractionProvider().extract(schema_name="MathCardV1", evidence=chunk)["output"]
    validate_schema_shape(output, schema)
    # Context restrictions must not leak into other shared array schema fields.
    output["cards"][0]["assumptions"] = ["ordinary prose"]
    validate_schema_shape(output, schema)
    output["cards"][0]["context_block_ids"] = ["block_eq"]
    with pytest.raises(ValueError, match="enum|too many"):
        validate_schema_shape(output, schema)
    output["cards"][0]["context_block_ids"] = []
    output["cards"][0]["equation_block_id"] = "block_context"
    with pytest.raises(ValueError, match="enum"):
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
