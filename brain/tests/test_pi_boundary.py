from __future__ import annotations

from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
EXTENSION = (PROJECT / "integrations" / "pi" / "research-brain.ts").read_text(encoding="utf-8")
INSTRUCTIONS = (PROJECT / "integrations" / "pi" / "RESEARCH_ASSISTANT.md").read_text(encoding="utf-8")


def test_pi_recall_exposes_all_bounded_retrieval_filters() -> None:
    for field in (
        "gradients_required",
        "training_required",
        "activation_access",
        "weight_access",
        "representation_kind",
        "origins",
        "review_states",
        "document_id",
        "version_label",
    ):
        assert f"{field}: Type.Optional" in EXTENSION
    assert 'args.push("--filters", JSON.stringify(params.filters))' in EXTENSION
    assert "additionalProperties: false" in EXTENSION
    assert 'Type.Literal("SOURCE_EXPLICIT")' in EXTENSION
    assert 'Type.Literal("INVALIDATED")' in EXTENSION


def test_pi_boundary_uses_fixed_execfile_and_bounded_valid_json() -> None:
    assert "execFile(" in EXTENSION
    assert "exec(" not in EXTENSION
    assert "MAX_RECALL_LIMIT = 8" in EXTENSION
    assert "MAX_EVIDENCE_PER_HIT = 4" in EXTENSION
    assert "MAX_TOOL_OUTPUT_CHARS = 64_000" in EXTENSION
    assert "JSON.stringify(compact.slice(0, returned), null, 2)" in EXTENSION
    assert "stdout.slice(" not in EXTENSION


def test_pi_instructions_require_filters_and_corpus_mismatch() -> None:
    assert "encode them in the tool's structured `filters`" in INSTRUCTIONS
    assert "A false access constraint excludes cards where the field" in INSTRUCTIONS
    assert "the corpus does not establish it" in INSTRUCTIONS
    assert "keep `limit` at most 5" in INSTRUCTIONS


def test_pi_exposes_bounded_frontier_context_without_mutation_tools() -> None:
    assert 'name: "research_context"' in EXTENSION
    assert 'args = ["context", params.question' in EXTENSION
    assert 'Type.Literal("critique")' in EXTENSION
    assert 'Type.Literal("brainstorm")' in EXTENSION
    assert 'blind_first: Type.String' in EXTENSION
    assert 'if ("blind_first" in params)' in EXTENSION
    assert "Never send `blind_first` in another mode" in INSTRUCTIONS
    assert "Research packet exceeds the tool output limit" in EXTENSION
    for forbidden in ("research_ingest", "research_extract", "research_review", "research_delete"):
        assert forbidden not in EXTENSION
