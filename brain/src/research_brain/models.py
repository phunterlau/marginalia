"""Small transport models; canonical records remain in SQLite."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ParsedBlock:
    block_type: str
    raw_text: str
    normalized_text: str
    section_path: str | None = None
    page: int | None = None
    raw_latex: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class DocumentCompilation:
    id: str
    document_version_id: str
    parser_name: str
    parser_version: str
    config_digest: str
    status: str
    diagnostics: dict[str, Any]
    created_at: str


@dataclass(frozen=True)
class GenerationRun:
    id: str
    task: str
    provider: str
    model: str
    reasoning_effort: str | None
    prompt_version: str
    schema_version: str
    input_digest: str
    status: str
    created_at: str
    completed_at: str | None = None


@dataclass(frozen=True)
class GenerationAttempt:
    id: str
    run_id: str
    attempt_number: int
    started_at: str
    outcome: str
    completed_at: str | None = None
    status_code: int | None = None


@dataclass(frozen=True)
class IngestResult:
    document_id: str
    document_version_id: str
    source_asset_id: str
    sha256: str
    block_count: int
    created_version: bool
    compilation_id: str | None = None
    created_compilation: bool = False


@dataclass(frozen=True)
class ResearchObject:
    id: str
    kind: str
    title: str | None
    body: str
    structured: dict[str, Any]
    origin: str
    review_state: str
    confidence: float | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class SearchHit:
    record_type: str
    record_id: str
    title: str | None
    text: str
    kind: str | None
    rank: float
    document_version_id: str | None = None


@dataclass(frozen=True)
class EvidenceRefV1:
    block_id: str
    relation: str


@dataclass(frozen=True)
class MethodCardV1:
    name: str
    problem: str
    mechanism: str
    procedure: list[str]
    inputs: list[str]
    outputs: list[str]
    gradients_required: bool | None
    training_required: bool | None
    activation_access: bool | None
    weight_access: bool | None
    assumptions: list[str]
    failure_modes: list[str]
    scientific_moves: list[str]
    evidence: list[EvidenceRefV1]


@dataclass(frozen=True)
class MathCardV1:
    name: str
    equation_block_id: str
    exact_latex: str
    semantic_gloss: str
    role: str
    symbols: dict[str, str]
    assumptions: list[str]
    affordances: list[str]
    failure_modes: list[str]
    math_move: str
    context_block_ids: list[str]


@dataclass(frozen=True)
class RetrievalFiltersV1:
    gradients_required: bool | None = None
    training_required: bool | None = None
    activation_access: bool | None = None
    weight_access: bool | None = None
    representation_kind: str | None = None
    origins: tuple[str, ...] = ()
    review_states: tuple[str, ...] = ()
    document_id: str | None = None
    version_label: str | None = None


@dataclass(frozen=True)
class SearchHitV2:
    record_type: str
    record_id: str
    title: str | None
    text: str
    kind: str | None
    fused_score: float
    lexical_rank: int | None = None
    semantic_rank: int | None = None
    origin: str | None = None
    review_state: str | None = None
    document_version_id: str | None = None
    source_locator: dict[str, Any] | None = None
    evidence: tuple[dict[str, Any], ...] = ()
    structured: dict[str, Any] | None = None

    @property
    def rank(self) -> float:
        """Compatibility alias for the former lexical SearchHit API."""
        return self.fused_score
