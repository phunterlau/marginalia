"""Deterministic active-frontier ResearchPacket composition."""

from __future__ import annotations

from typing import Any, Sequence
import re

from .frontier import text, QUESTION_RELATIONS
from .models import ResearchPacketV1, RetrievalFiltersV1, SearchHitV2
from .retrieval import Retriever
from .store import SQLiteStore


CONTEXT_MODES = {"recall", "analysis", "critique", "brainstorm", "decision"}
FRONTIER_KINDS = {
    "research_question", "hypothesis", "observation", "interpretation",
    "decision", "experiment", "experiment_result", "transfer_hypothesis",
}
NEGATIVE_DISPOSITIONS = {
    "tried_and_failed", "considered_but_rejected", "not_applicable",
    "insufficient_evidence", "too_expensive", "did_not_discriminate_hypotheses",
    "contradicted_by_experiment", "superseded",
}

STOP_WORDS = {'the','and','are','was','were','what','which','why','how','this','that','with',
              'from','have','has','had','about','would','could','should','into','for','our','did',
              'does','been','their','there','might','than','its','not','but','current'}


def _terms(value: str) -> set[str]:
    return {word for word in re.findall(r'[a-z0-9]+', value.lower())
            if len(word) > 2 and word not in STOP_WORDS}


def _rank_records(records: list[dict[str, Any]], question: str) -> list[dict[str, Any]]:
    """Exact lexical overlap, with stable identities for ties; no model reranking."""
    terms = _terms(question)
    return sorted(records, key=lambda r: (
        -len(terms & _terms(f"{r.get('title') or ''} {r['body']} {r['kind'].replace('_', ' ')}")),
        r['id'],
    ))


def _short(value: Any, maximum: int = 1_200) -> Any:
    if isinstance(value, str) and len(value) > maximum:
        return value[:maximum] + "…"
    return value


def _compact_structured(value: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key == "evidence":
            continue
        if isinstance(item, list):
            result[key] = [_short(entry, 600) for entry in item[:8]]
        elif isinstance(item, dict):
            result[key] = {str(name): _short(entry, 600) for name, entry in list(item.items())[:20]}
        else:
            result[key] = _short(item)
    return result


def _compact_evidence(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value.get(key)
        for key in (
            "block_id", "relation", "document_id", "document_title", "version_label",
            "source_member", "line_start", "line_end", "block_type",
        )
        if value.get(key) is not None
    }


def _record_item(record: dict[str, Any], why: str) -> dict[str, Any]:
    return {
        "record_id": record["id"],
        "kind": record["kind"],
        "title": record.get("title"),
        "summary": record["body"][:1_200],
        "origin": record["origin"],
        "review_state": record["review_state"],
        "structured": _compact_structured(record.get("structured") or {}),
        "why_retrieved": why,
        "evidence": [_compact_evidence(item) for item in record.get("evidence", [])[:4]],
    }


def _hit_item(hit: SearchHitV2, why: str) -> dict[str, Any]:
    return {
        "record_id": hit.record_id,
        "kind": hit.kind,
        "title": hit.title,
        "summary": hit.text[:1_200],
        "origin": hit.origin,
        "review_state": hit.review_state,
        "structured": _compact_structured(hit.structured or {}),
        "why_retrieved": why,
        "evidence": [_compact_evidence(item) for item in hit.evidence[:4]],
    }


class ContextCompiler:
    def __init__(self, store: SQLiteStore, retriever: Retriever):
        self.store = store
        self.retriever = retriever

    def compile(
        self,
        question: str,
        *,
        thread_id: str | None = None,
        mode: str = "analysis",
        filters: RetrievalFiltersV1 | None = None,
        limit: int = 8,
        blind_first: str | None = None,
        query_vector: Sequence[float] | None = None,
    ) -> ResearchPacketV1:
        question = text(question, "question", maximum=2_000)
        if mode not in CONTEXT_MODES:
            raise ValueError(f"unknown context mode: {mode}")
        if not 1 <= limit <= 10:
            raise ValueError("context limit must be between 1 and 10")
        if mode == "brainstorm":
            blind_first = text(blind_first, "blind_first", maximum=20_000)
        elif blind_first is not None:
            raise ValueError("blind_first is only valid in brainstorm mode")

        frontier = None
        thread_records: list[dict[str, Any]] = []
        if thread_id is not None:
            frontier = self.store.get_object_record(thread_id)
            if frontier is None or frontier["kind"] != "research_thread":
                raise LookupError(f"Research thread not found: {thread_id}")
            thread_records = self.store.list_object_records(thread_id=thread_id)
            thread_records = _rank_records(thread_records, question)

        memory_hits = self.retriever.retrieve(
            question,
            kinds=["method_card", "math_card"],
            filters=filters,
            limit=20,
            reliable=True,
            query_vector=query_vector,
        )
        cards = [_hit_item(hit, "reviewed method or mathematics matching the question") for hit in memory_hits]
        frontier_items = [
            _record_item(record, "active object from the selected research frontier")
            for record in thread_records
            if record["kind"] in FRONTIER_KINDS
        ]
        # Keep a question's sparse lineage in the packet; do not invent relations
        # from co-occurrence or silently merge questions into a summary.
        for item in frontier_items:
            if item['kind'] == 'research_question':
                links = self.store.get_links(item['record_id'], relations=sorted(QUESTION_RELATIONS))
                item['question_links'] = [{key: link[key] for key in (
                    'source_id','target_id','relation','direction','origin','review_state'
                )} for link in links[:8]]
                item['question_links_omitted'] = max(0, len(links) - 8)

        # Explicit object evidence is not necessarily attached to the thread
        # itself (e.g. an E42 experiment_result). Include it adjacent to its
        # observation and keep the interpretation as a separately labeled object.
        expanded = []
        for item in frontier_items:
            expanded.append(item)
            if item['kind'] == 'observation':
                for ref in item['structured'].get('evidence_refs', []):
                    result = self.store.get_object_record(ref) if ref.startswith('obj_') else None
                    if result and result['kind'] == 'experiment_result' and result['structured'].get('thread_id') in {None,thread_id}:
                        expanded.append(_record_item(result, 'experiment result explicitly referenced by this observation'))
                expanded.extend(other for other in frontier_items if other['kind'] == 'interpretation'
                                and item['record_id'] in other['structured'].get('derived_from', []))
        frontier_items = expanded
        snapshots = [record for record in thread_records if record["kind"] == "frontier_snapshot"]
        if snapshots:
            latest = max(snapshots, key=lambda record: record["structured"].get("snapshot_number", 0))
            frontier_items.insert(0, _record_item(latest, "latest derived snapshot of the selected frontier"))
        histories = [
            _record_item(record, "prior attempt or disposition from this research thread")
            for record in thread_records
            if record["kind"] == "usage_episode"
        ]
        tensions = [
            _record_item(record, "unresolved conflict in the selected research frontier")
            for record in thread_records
            if record["kind"] == "tension" and record.get("structured", {}).get("status") == "unresolved"
        ]
        negative_history = [
            item for item in histories
            if item["structured"].get("disposition") in NEGATIVE_DISPOSITIONS
        ]
        observations = [item for item in frontier_items if item["kind"] == "observation"]
        hypotheses = [item for item in frontier_items if item['kind'] == 'hypothesis']

        if mode == "recall":
            priorities = [("historical_attempts", histories, 4), ("relevant_memory", cards, 4),
                          ("relevant_memory", frontier_items, 2)]
        elif mode == "critique":
            priorities = [("tensions", tensions, 3), ("counterevidence", negative_history, 3),
                          ("counterevidence", observations, 2), ("relevant_memory", hypotheses, 2),
                          ("relevant_memory", cards, 2)]
        elif mode == "decision":
            priorities = [("tensions", tensions, 3), ("historical_attempts", histories, 3),
                          ("relevant_memory", frontier_items, 3), ("relevant_memory", cards, 2)]
        elif mode == "brainstorm":
            priorities = [("historical_attempts", histories, 2), ("tensions", tensions, 2),
                          ("optional_distant_connections", cards, 6)]
        else:
            priorities = [("relevant_memory", frontier_items, 6), ("relevant_memory", cards, 2),
                          ("tensions", tensions, 2), ("historical_attempts", histories, 2)]

        buckets: dict[str, list[dict[str, Any]]] = {
            "relevant_memory": [], "historical_attempts": [], "counterevidence": [],
            "tensions": [], "optional_distant_connections": [],
        }
        seen: set[str] = set()
        for bucket, candidates, source_cap in priorities:
            added = 0
            for item in candidates:
                if len(seen) >= limit:
                    break
                if item["record_id"] in seen:
                    continue
                seen.add(item["record_id"])
                buckets[bucket].append(item)
                added += 1
                if added >= source_cap:
                    break

        evidence: dict[str, dict[str, Any]] = {}
        for bucket in buckets.values():
            for item in bucket:
                for ref in item["evidence"]:
                    block_id = ref.get("block_id")
                    if isinstance(block_id, str):
                        evidence[block_id] = ref

        compact_frontier = None
        if frontier is not None:
            compact_frontier = {
                "record_id": frontier["id"], "title": frontier.get("title"),
                "origin": frontier["origin"], "review_state": frontier["review_state"],
                "state": _compact_structured(frontier["structured"]),
            }
        mismatch = None
        if not seen:
            mismatch = "No compatible frontier object or reviewed memory was found in the local corpus."

        return ResearchPacketV1(
            mode=mode,
            question=question,
            thread_id=thread_id,
            frontier=compact_frontier,
            blind_first=blind_first,
            relevant_memory=tuple(buckets["relevant_memory"]),
            historical_attempts=tuple(buckets["historical_attempts"]),
            counterevidence=tuple(buckets["counterevidence"]),
            tensions=tuple(buckets["tensions"]),
            optional_distant_connections=tuple(buckets["optional_distant_connections"]),
            evidence_refs=tuple(evidence[key] for key in sorted(evidence)),
            corpus_mismatch=mismatch,
        )
