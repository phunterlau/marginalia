"""Epistemic retrieval lanes with deterministic reciprocal-rank fusion."""

from __future__ import annotations

import json
import math
import os
from typing import Any, Sequence

from .models import RetrievalFiltersV1, SearchHitV2
from .store.sqlite import SQLiteStore


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        return -1.0
    norm = math.sqrt(sum(value * value for value in left)) * math.sqrt(sum(value * value for value in right))
    return sum(a * b for a, b in zip(left, right, strict=True)) / norm if norm else -1.0


class Retriever:
    def __init__(self, store: SQLiteStore):
        self.store = store

    def retrieve(self, query: str, *, kinds: Sequence[str] | None = None,
                 filters: RetrievalFiltersV1 | None = None, limit: int = 10,
                 reliable: bool = False, query_vector: Sequence[float] | None = None,
                 embedding_provider: Any | None = None) -> list[SearchHitV2]:
        filters = filters or RetrievalFiltersV1()
        lexical = self._lexical(query, kinds=kinds, candidate_limit=50)
        model = os.getenv("RESEARCH_EMBED_MODEL", "text-embedding-3-small")
        vectors = self.store.semantic_vectors(model=model)
        if kinds:
            allowed_objects = self.store.object_ids_for_kinds(kinds)
            vectors = [item for item in vectors if item[0] == "research_object" and item[1] in allowed_objects]
        if query_vector is None and vectors and embedding_provider is not None:
            query_vector = embedding_provider.embed([query])[0]
        semantic: list[tuple[str, str]] = []
        if query_vector is not None:
            compatible = [item for item in vectors if len(item[2]) == len(query_vector)]
            ranked = sorted(compatible, key=lambda item: (-_cosine(query_vector, item[2]), item[1]))[:50]
            semantic = [(item[0], item[1]) for item in ranked]
        scores: dict[tuple[str, str], float] = {}
        ranks: dict[tuple[str, str], list[int | None]] = {}
        for index, key in enumerate(lexical, 1):
            scores[key] = scores.get(key, 0.0) + 1.0 / (60 + index)
            ranks.setdefault(key, [None, None])[0] = index
        for index, key in enumerate(semantic, 1):
            scores[key] = scores.get(key, 0.0) + 1.0 / (60 + index)
            ranks.setdefault(key, [None, None])[1] = index
        hits: list[SearchHitV2] = []
        for key in sorted(scores, key=lambda item: (-scores[item], item[1])):
            hit = self._hydrate(key, scores[key], ranks[key][0], ranks[key][1])
            if hit and self._allowed(hit, filters, reliable, kinds):
                hits.append(hit)
                if len(hits) >= limit:
                    break
        return hits

    def _lexical(self, query: str, *, kinds: Sequence[str] | None, candidate_limit: int) -> list[tuple[str, str]]:
        fts = self.store._fts_query(query)
        ranked: list[tuple[float, str, str]] = []
        with self.store.connect() as connection:
            sql = "SELECT object_id, kind, bm25(object_fts) rank FROM object_fts WHERE object_fts MATCH ?"
            params: list[Any] = [fts]
            if kinds:
                sql += f" AND kind IN ({','.join('?' for _ in kinds)})"
                params.extend(kinds)
            sql += " ORDER BY rank LIMIT ?"
            params.append(candidate_limit)
            ranked.extend((row["rank"], "research_object", row["object_id"]) for row in connection.execute(sql, params))
            if not kinds:
                ranked.extend((row["rank"], "document_block", row["block_id"]) for row in connection.execute(
                    """SELECT f.block_id, bm25(block_fts) rank
                       FROM block_fts f
                       JOIN document_blocks b ON b.id=f.block_id
                       JOIN document_versions v ON v.id=b.document_version_id
                       WHERE block_fts MATCH ? AND b.compilation_id = (
                           SELECT c.id FROM document_compilations c
                           WHERE c.document_version_id=v.id AND c.status='complete'
                           ORDER BY c.created_at DESC, c.id DESC LIMIT 1
                       )
                       ORDER BY rank LIMIT ?""",
                    (fts, candidate_limit)))
        return [(kind, identifier) for _, kind, identifier in sorted(ranked, key=lambda item: (item[0], item[2]))[:candidate_limit]]

    def _hydrate(self, key: tuple[str, str], score: float, lexical_rank: int | None,
                 semantic_rank: int | None) -> SearchHitV2 | None:
        record_type, identifier = key
        if record_type == "research_object":
            record = self.store.get_object_record(identifier)
            if not record:
                return None
            return SearchHitV2(record_type, identifier, record["title"], record["body"], record["kind"],
                               score, lexical_rank, semantic_rank, record["origin"], record["review_state"],
                               evidence=tuple(record["evidence"]), structured=record["structured"])
        evidence = self.store.get_evidence(identifier)
        if not evidence:
            return None
        locator = {key: evidence.get(key) for key in
                   ("document_id", "document_title", "version_label", "source_member", "line_start", "line_end")}
        return SearchHitV2(record_type, identifier, evidence.get("section_path"), evidence["normalized_text"],
                           evidence["block_type"], score, lexical_rank, semantic_rank,
                           "SOURCE_EXPLICIT", "ACCEPTED", evidence["document_version_id"], locator)

    @staticmethod
    def _allowed(hit: SearchHitV2, filters: RetrievalFiltersV1, reliable: bool,
                 kinds: Sequence[str] | None) -> bool:
        if kinds and hit.kind not in kinds:
            return False
        if filters.representation_kind and hit.kind != filters.representation_kind:
            return False
        if filters.origins and hit.origin not in filters.origins:
            return False
        if filters.review_states and hit.review_state not in filters.review_states:
            return False
        if hit.record_type == "research_object":
            if reliable and hit.review_state != "ACCEPTED":
                return False
        if filters.document_id:
            locator = hit.source_locator or {}
            evidence_docs = {item.get("document_id") for item in hit.evidence}
            if locator.get("document_id") != filters.document_id and filters.document_id not in evidence_docs:
                return False
        if filters.version_label:
            locator = hit.source_locator or {}
            evidence_versions = {item.get("version_label") for item in hit.evidence}
            if locator.get("version_label") != filters.version_label and filters.version_label not in evidence_versions:
                return False
        boolean_filters = {
            "gradients_required": filters.gradients_required,
            "training_required": filters.training_required,
            "activation_access": filters.activation_access,
            "weight_access": filters.weight_access,
        }
        if hit.record_type == "research_object" and any(value is not None for value in boolean_filters.values()):
            structured = hit.structured or {}
            if any(value is not None and structured.get(name) is not value
                   for name, value in boolean_filters.items()):
                return False
        return True
