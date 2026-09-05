"""Offline, fail-closed acceptance checks for ResearchPacket composition.

This evaluates structural evidence coverage, not scientific validity or answer quality.
Fixtures are synthetic and always live in a temporary database.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import time
from typing import Any

from .brain import Brain
from .context import _compact_structured

BUCKETS = (
    "relevant_memory",
    "historical_attempts",
    "counterevidence",
    "tensions",
    "optional_distant_connections",
)
FIELD_KINDS = {
    "constraints": "research_thread",
    "unknown": "research_thread",
    "disposition": "usage_episode",
    "reason": "usage_episode",
    "what_would_reconsider": "usage_episode",
    "critical_unknowns": "hypothesis",
    "what_would_weaken": "hypothesis",
    "killer_test": "hypothesis",
    "mapping": "transfer_hypothesis",
    "why_promising": "transfer_hypothesis",
    "mismatches": "transfer_hypothesis",
    "proposed_test": "transfer_hypothesis",
    **{
        key: "frontier_snapshot"
        for key in (
            "established",
            "unresolved",
            "important_tensions",
            "negative_evidence",
            "pending_discriminating_experiments",
        )
    },
}
CASE_KEYS = {
    "name",
    "question",
    "mode",
    "limit",
    "must_include_kinds",
    "must_include_fields",
    "must_label_separately",
    "must_prioritize",
    "must_include_relations",
}


@dataclass(frozen=True)
class FrontierEvaluationReport:
    schema: str
    fixture: str | None
    spec_sha256: str
    database_sha256: str
    cases: int
    passed: bool
    database_unchanged: bool
    results: tuple[dict[str, Any], ...]
    limitations: tuple[str, ...] = (
        "Structural coverage only; not a judged usefulness evaluation.",
        "No model calls or real-card acceptance.",
    )


def load_spec(path: str | Path) -> dict[str, Any]:
    spec = json.loads(Path(path).read_text())
    if not isinstance(spec, dict) or spec.get("schema") != "FrontierEvaluationV1":
        raise ValueError("Expected FrontierEvaluationV1")
    if set(spec) - {"schema", "cases"}:
        raise ValueError("Unknown frontier evaluation specification fields")
    cases = spec.get("cases")
    if not isinstance(cases, list) or not 1 <= len(cases) <= 100:
        raise ValueError("Expected 1–100 frontier evaluation cases")
    names = set()
    for case in cases:
        if not isinstance(case, dict) or set(case) - CASE_KEYS:
            raise ValueError("Unknown frontier evaluation case fields")
        if (
            not isinstance(case.get("name"), str)
            or not case["name"]
            or case["name"] in names
        ):
            raise ValueError("Case names must be nonempty and unique")
        names.add(case["name"])
        if (
            not isinstance(case.get("question"), str)
            or not 1 <= len(case["question"]) <= 2000
        ):
            raise ValueError("Case question must contain 1–2000 characters")
        if case.get("mode", "analysis") not in {
            "analysis",
            "recall",
            "critique",
            "decision",
        }:
            raise ValueError("Invalid frontier evaluation mode")
        if (
            type(case.get("limit", 10)) is not int
            or not 1 <= case.get("limit", 10) <= 10
        ):
            raise ValueError("Case limit must be between 1 and 10")
        for key in CASE_KEYS - {"name", "question", "mode", "limit"}:
            if key in case and (
                not isinstance(case[key], list)
                or not all(isinstance(x, str) and x for x in case[key])
            ):
                raise ValueError(f"{key} must be a string array")
        if not case.get("must_include_kinds"):
            raise ValueError("Each case requires must_include_kinds")
        if set(case.get("must_include_fields", [])) - FIELD_KINDS.keys():
            raise ValueError("Unknown required field")
        if set(case.get("must_prioritize", [])) - {
            "counterevidence",
            "possible_explanations",
            "failure_modes",
        }:
            raise ValueError("Unknown priority assertion")
        if set(case.get("must_label_separately", [])) - {"interpretation"}:
            raise ValueError("Unknown separate-label assertion")
        if set(case.get("must_include_relations", [])) - {
            "MOTIVATED_BY",
            "REFINES",
            "SPLITS_INTO",
            "ANSWERED_BY",
            "SUPERSEDED_BY",
        }:
            raise ValueError("Unknown relation assertion")
    return spec


def packet_records(packet: dict[str, Any]) -> list[dict[str, Any]]:
    records = [item for bucket in BUCKETS for item in packet[bucket]]
    if packet["frontier"]:
        frontier = packet["frontier"]
        records.append(
            {**frontier, "kind": "research_thread", "structured": frontier["state"]}
        )
    return records


def check_packet(
    brain: Brain, packet: dict[str, Any], case: dict[str, Any]
) -> list[str]:
    failures = []
    records = packet_records(packet)
    kinds = {item["kind"] for item in records}
    for kind in case["must_include_kinds"]:
        if kind not in kinds:
            failures.append(f"missing kind: {kind}")
    for field in case.get("must_include_fields", []):
        if not any(
            r["kind"] == FIELD_KINDS[field] and r["structured"].get(field)
            for r in records
        ):
            failures.append(f"missing nonempty field: {FIELD_KINDS[field]}.{field}")
    ids = [r["record_id"] for r in records if r["kind"] != "research_thread"]
    if len(ids) > case.get("limit", 10) or len(set(ids)) != len(ids):
        failures.append("packet budget or deduplication violation")
    if len(json.dumps(packet)) > 64000:
        failures.append("packet exceeds Pi output budget")
    for r in records:
        source = brain.get_research_object(r["record_id"])
        if not source:
            failures.append(f"unknown record: {r['record_id']}")
            continue
        if (r["kind"], r["origin"], r["review_state"]) != (
            source["kind"],
            source["origin"],
            source["review_state"],
        ):
            failures.append(f"epistemic label mismatch: {r['record_id']}")
        if r["structured"] != _compact_structured(source["structured"]):
            failures.append(f"structured content mismatch: {r['record_id']}")
        if (
            r["kind"] in {"method_card", "math_card"}
            and r["review_state"] != "ACCEPTED"
        ):
            failures.append(f"unreviewed card leakage: {r['record_id']}")
        if source["structured"].get("thread_id") not in {None, packet["thread_id"]}:
            failures.append(f"cross-thread leakage: {r['record_id']}")
    for ref in packet["evidence_refs"]:
        original = brain.get_evidence(ref["block_id"])
        if not original or any(
            ref.get(k) != original.get(k)
            for k in (
                "document_id",
                "source_member",
                "version_label",
                "line_start",
                "line_end",
            )
            if k in ref
        ):
            failures.append(f"invalid evidence locator: {ref['block_id']}")
    if "interpretation" in case.get("must_label_separately", []):
        observations = [r for r in records if r["kind"] == "observation"]
        interpretations = [r for r in records if r["kind"] == "interpretation"]
        if not interpretations or any(
            "interpretation" in r["structured"] for r in observations
        ):
            failures.append("interpretation is absent or conflated with observation")
        observation_ids = {r["record_id"] for r in observations}
        if any(
            not observation_ids.intersection(r["structured"].get("derived_from", []))
            for r in interpretations
        ):
            failures.append("interpretation has no included observation parent")
        result_ids = {
            r["record_id"] for r in records if r["kind"] == "experiment_result"
        }
        if not any(
            result_ids.intersection(r["structured"].get("evidence_refs", []))
            for r in observations
        ):
            failures.append("observation is missing its experiment result")
    for assertion in case.get("must_prioritize", []):
        candidates = (
            packet["counterevidence"]
            if assertion == "counterevidence"
            else packet["tensions"]
            if assertion == "possible_explanations"
            else packet["relevant_memory"]
        )
        if (
            not candidates
            or assertion != "counterevidence"
            and not any(r["structured"].get(assertion) for r in candidates)
        ):
            failures.append(f"missing priority content: {assertion}")
    actual_relations = set()
    for r in records:
        if r["kind"] != "research_question":
            continue
        canonical = brain.store.get_links(r["record_id"])
        for link in r.get("question_links", []):
            if not any(
                all(
                    link.get(k) == original.get(k)
                    for k in (
                        "source_id",
                        "target_id",
                        "relation",
                        "origin",
                        "review_state",
                        "direction",
                    )
                )
                for original in canonical
            ):
                failures.append("question genealogy does not match stored links")
            actual_relations.add(link["relation"])
    for relation in case.get("must_include_relations", []):
        if relation not in actual_relations:
            failures.append(f"missing question relation: {relation}")
    return failures


def evaluate_frontier(
    brain: Brain, spec_path: str | Path, *, thread_id: str, fixture: str | None = None
) -> FrontierEvaluationReport:
    spec = load_spec(spec_path)
    before = hashlib.sha256(brain.store.path.read_bytes()).hexdigest()
    results = []
    for case in spec["cases"]:
        started = time.perf_counter()
        packet = asdict(
            brain.context(
                case["question"],
                thread_id=thread_id,
                mode=case.get("mode", "analysis"),
                limit=case.get("limit", 10),
            )
        )
        elapsed = (time.perf_counter() - started) * 1000
        failures = check_packet(brain, packet, case)
        # Equal result-count lexical baseline, with no query embeddings/provider calls.
        raw = brain.retriever.retrieve(
            case["question"], limit=case.get("limit", 10), reliable=True
        )
        raw_kinds = {hit.kind for hit in raw}
        results.append(
            {
                "name": case["name"],
                "passed": not failures,
                "failures": failures,
                "packet_ms": elapsed,
                "packet": packet,
                "raw_top_k_ids": [hit.record_id for hit in raw],
                "raw_missing_kinds": sorted(
                    set(case["must_include_kinds"]) - raw_kinds
                ),
            }
        )
    unchanged = before == hashlib.sha256(brain.store.path.read_bytes()).hexdigest()
    return FrontierEvaluationReport(
        "FrontierEvaluationReportV1",
        fixture,
        hashlib.sha256(Path(spec_path).read_bytes()).hexdigest(),
        before,
        len(results),
        unchanged and all(r["passed"] for r in results),
        unchanged,
        tuple(results),
    )


def seed_frontier_fixture(brain: Brain) -> str:
    """Synthetic research history, not a representation of actual experiments."""
    paper = brain.root / "synthetic-covariance.md"
    paper.write_text(
        "# SYNTHETIC covariance device\n\nCovariance eigenvectors rank activation variance, not causal effect.\n"
    )
    document = brain.ingest(paper)
    block = next(
        b
        for b in brain.store.get_blocks(document.document_id)
        if b["block_type"] == "paragraph"
    )
    thread = brain.create_thread(
        "SYNTHETIC: Principled perturbation directions",
        goal="Construct principled perturbation directions without gradients.",
        known=["Forward-only activation access is available."],
        constraints=["No gradients", "Equal norm"],
        unknown=["Does activation variance track causal importance?"],
        pending_experiments=[
            "E43: compare covariance directions with matched random controls"
        ],
    )
    question = brain.create_research_question(
        "Which principled perturbation direction beats matched random controls?",
        thread_id=thread.id,
    )
    hypothesis = brain.create_hypothesis(
        "The current covariance hypothesis: high variance predicts useful perturbation directions.",
        thread_id=thread.id,
        critical_unknowns=["Causal relevance under matched norm"],
        what_would_weaken=["No gain over random controls"],
        killer_test="E43 shows no gain over matched random directions.",
    )
    result = brain.create_research_object(
        kind="experiment_result",
        title="E42 actual result",
        body="SYNTHETIC E42: covariance directions changed the logit gap by 0.03 versus random 0.04.",
        structured={
            "schema": "ExperimentResultV1",
            "experiment": "E42",
            "covariance_gap": 0.03,
            "random_gap": 0.04,
        },
        origin="EXPERIMENT_OBSERVED",
    )
    observation = brain.record_observation(
        "E42 actually observed a weak covariance effect independent of interpretation.",
        thread_id=thread.id,
        conditions={"norm": 1, "dataset": "SYNTHETIC"},
        evidence_refs=[result.id],
    )
    brain.record_interpretation(
        "E42 interpretation: variance may be observational rather than causal.",
        thread_id=thread.id,
        derived_from=[observation.id],
    )
    brain.record_tension(
        "The current covariance hypothesis may be wrong: high variance but weak effects.",
        thread_id=thread.id,
        side_a=[hypothesis.id],
        side_b=[observation.id],
        possible_explanations=["Variance does not imply causal relevance"],
    )
    brain.record_usage_episode(
        "Already tried a variance-derived basis in synthetic E42.",
        candidate="variance-derived basis",
        disposition="insufficient_evidence",
        reason="Weak effect",
        what_would_reconsider="Replicated gain over random controls",
        thread_id=thread.id,
    )
    method = brain.create_research_object(
        kind="method_card",
        title="SYNTHETIC covariance mathematical device",
        body="Covariance hypothesis failure modes and mathematical transfer device.",
        structured={
            "schema": "MethodCardV1",
            "failure_modes": ["Variance is not causal relevance"],
        },
        origin="USER_STATED",
        evidence=[(block["id"], "defines", None)],
    )
    brain.create_transfer_hypothesis(
        target_question_id=question.id,
        source_object_id=method.id,
        mapping_claims={"eigenvectors": "perturbation directions"},
        why_promising=["forward-only computation"],
        mismatches=["variance does not establish causality"],
        proposed_test="Test the mathematical device transfer with matched controls.",
        thread_id=thread.id,
    )
    parent = brain.create_research_question(
        "How did the current research question arise?", thread_id=thread.id
    )
    child = brain.create_research_question(
        "Which layer should the question refine?", thread_id=thread.id
    )
    for relation, target in [
        ("MOTIVATED_BY", observation.id),
        ("REFINES", parent.id),
        ("SPLITS_INTO", child.id),
        ("ANSWERED_BY", result.id),
        ("SUPERSEDED_BY", child.id),
    ]:
        brain.link_question(question.id, relation, target)
    brain.create_frontier_snapshot(thread.id)
    # Same-thread recency distractors and foreign-thread overlap exercise selection boundaries.
    for i in range(12):
        brain.create_research_object(
            kind="decision",
            title=f"Synthetic housekeeping {i}",
            body="Choose a notebook color.",
            structured={"thread_id": thread.id},
        )
    foreign = brain.create_thread(
        "Unrelated thread", goal="Unrelated covariance hypothesis"
    )
    brain.create_hypothesis(
        "E42 covariance hypothesis perturbation directions", thread_id=foreign.id
    )
    brain.create_research_object(
        kind="method_card",
        title="UNREVIEWED synthetic covariance speculation",
        body="covariance hypothesis mathematical device",
        origin="AGENT_EXTRACTED",
        review_state="UNREVIEWED",
    )
    return thread.id
