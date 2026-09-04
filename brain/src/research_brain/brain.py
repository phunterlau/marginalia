"""Headless facade used by Python, CLI, Pi, Codex, and future clients."""

from __future__ import annotations

from pathlib import Path
import os
from typing import Any, Sequence

from .context import ContextCompiler
from .embeddings import EmbeddingIndexer, EmbeddingResult
from .extraction import ExtractionResult, Extractor
from .frontier import (HYPOTHESIS_STATUSES, QUESTION_RELATIONS, QUESTION_STATUSES,
                       TENSION_STATUSES, USAGE_DISPOSITIONS, enum, mapping, optional_text,
                       strings, text, thread_payload, update_thread_payload)
from .ingest import Ingestor
from .models import IngestResult, ResearchObject, ResearchPacketV1, RetrievalFiltersV1, SearchHitV2
from .retrieval import Retriever
from .store import SQLiteStore


class Brain:
    def __init__(self, root: str | Path = "data", *, extraction_provider_factory: Any = None,
                 embedding_provider_factory: Any = None):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = SQLiteStore(self.root / "brain.sqlite3")
        self.ingestor = Ingestor(self.store, self.root / "assets")
        self.extractor = Extractor(self.store, extraction_provider_factory)
        self.embedding_indexer = EmbeddingIndexer(self.store, embedding_provider_factory)
        self.retriever = Retriever(self.store)
        self.context_compiler = ContextCompiler(self.store, self.retriever)
        self.embedding_provider_factory = embedding_provider_factory

    def ingest(self, source: str | Path) -> IngestResult:
        return self.ingestor.ingest(source)

    def ingest_manifest(self, manifest: str | Path) -> IngestResult:
        return self.ingestor.ingest_manifest(manifest)

    def get_document(self, document_id: str) -> dict[str, Any] | None:
        return self.store.get_document(document_id)

    def get_evidence(self, block_id: str) -> dict[str, Any] | None:
        return self.store.get_evidence(block_id)

    def get_research_object(self, object_id: str) -> dict[str, Any] | None:
        return self.store.get_object_record(object_id)

    def review_research_object(self, object_id: str, *, review_state: str,
                               note: str | None = None, actor: str = "user") -> ResearchObject:
        return self.store.review_object(object_id, review_state=review_state, note=note, actor=actor)

    def extract(self, task: str, document_id: str, *, compilation_id: str | None = None,
                live: bool = False, force: bool = False) -> ExtractionResult:
        return self.extractor.extract(task, document_id, compilation_id=compilation_id,
                                      live=live, force=force)

    def index_embeddings(self, target: str, *, live: bool = False,
                         force: bool = False) -> EmbeddingResult:
        return self.embedding_indexer.index(target, live=live, force=force)

    def create_research_object(
        self,
        *,
        kind: str,
        body: str,
        title: str | None = None,
        structured: dict[str, Any] | None = None,
        origin: str = "USER_STATED",
        review_state: str = "ACCEPTED",
        confidence: float | None = None,
        evidence: Sequence[tuple[str, str, float | None]] = (),
        actor: str = "user",
    ) -> ResearchObject:
        return self.store.create_object(
            kind=kind, body=body, title=title, structured=structured, origin=origin,
            review_state=review_state, confidence=confidence, evidence=evidence, actor=actor,
        )

    def create_research_question(
        self,
        question: str,
        *,
        title: str | None = None,
        constraints: Sequence[str] = (),
        available_access: Sequence[str] = (),
        desired_output: str | None = None,
        status: str = "open",
        thread_id: str | None = None,
    ) -> ResearchObject:
        if thread_id is not None:
            self._require_object(thread_id, kind="research_thread")
        structured = {
            "question": text(question, "question"),
            "status": enum(status, "question status", QUESTION_STATUSES),
            "constraints": strings(constraints, "constraints"),
            "available_access": strings(available_access, "available_access"),
            "desired_output": optional_text(desired_output, "desired_output"),
            "thread_id": thread_id,
            "schema": "ResearchQuestionV1",
        }
        return self.create_research_object(
            kind="research_question", title=title or question[:120], body=question,
            structured=structured, origin="USER_STATED", review_state="ACCEPTED",
        )

    def create_thread(
        self,
        title: str,
        *,
        goal: str,
        status: str = "active",
        known: Sequence[str] = (),
        unknown: Sequence[str] = (),
        constraints: Sequence[str] = (),
        pending_decisions: Sequence[str] = (),
        pending_experiments: Sequence[str] = (),
    ) -> ResearchObject:
        title = text(title, "title", maximum=300)
        payload = thread_payload(
            goal=goal,
            status=status,
            known=known,
            unknown=unknown,
            constraints=constraints,
            pending_decisions=pending_decisions,
            pending_experiments=pending_experiments,
        )
        return self.create_research_object(
            kind="research_thread", title=title, body=payload["goal"], structured=payload,
            origin="USER_STATED", review_state="ACCEPTED",
        )

    def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        record = self.get_research_object(thread_id)
        return record if record and record["kind"] == "research_thread" else None

    def update_frontier(self, thread_id: str, changes: dict[str, Any], *, actor: str = "user") -> ResearchObject:
        thread = self._require_object(thread_id, kind="research_thread")
        payload = update_thread_payload(thread.structured, mapping(changes, "changes", required=True))
        return self.store.update_object_structured(
            thread_id, structured=payload, event_type="frontier_updated", changes=dict(changes),
            body=payload["goal"], actor=actor,
        )

    def create_hypothesis(
        self,
        statement: str,
        *,
        thread_id: str,
        status: str = "active",
        evidence_for: Sequence[str] = (),
        evidence_against: Sequence[str] = (),
        critical_unknowns: Sequence[str] = (),
        what_would_strengthen: Sequence[str] = (),
        what_would_weaken: Sequence[str] = (),
        killer_test: str | None = None,
        origin: str = "USER_STATED",
        review_state: str = "ACCEPTED",
    ) -> ResearchObject:
        self._require_object(thread_id, kind="research_thread")
        payload = {
            "schema": "HypothesisV2",
            "statement": text(statement, "statement"),
            "thread_id": thread_id,
            "status": enum(status, "hypothesis status", HYPOTHESIS_STATUSES),
            "evidence_for": self._validate_refs(evidence_for, "evidence_for"),
            "evidence_against": self._validate_refs(evidence_against, "evidence_against"),
            "critical_unknowns": strings(critical_unknowns, "critical_unknowns"),
            "what_would_strengthen": strings(what_would_strengthen, "what_would_strengthen"),
            "what_would_weaken": strings(what_would_weaken, "what_would_weaken"),
            "killer_test": optional_text(killer_test, "killer_test"),
        }
        return self.create_research_object(
            kind="hypothesis", title=statement[:120], body=payload["statement"], structured=payload,
            origin=origin, review_state=review_state,
        )

    def link_question(
        self,
        source_question_id: str,
        relation: str,
        target_object_id: str,
        *,
        metadata: dict[str, Any] | None = None,
        origin: str = "USER_STATED",
        review_state: str = "ACCEPTED",
        confidence: float | None = None,
        actor: str = "user",
    ) -> dict[str, Any]:
        source = self._require_object(source_question_id, kind="research_question")
        target = self._require_object(target_object_id)
        if source.id == target.id:
            raise ValueError("A research question cannot link to itself")
        relation = enum(relation, "question relation", QUESTION_RELATIONS)
        if relation in {"REFINES", "SPLITS_INTO", "SUPERSEDED_BY"} and target.kind != "research_question":
            raise ValueError(f"{relation} target must be a research_question")
        return self.store.create_link(
            source_id=source.id,
            relation=relation,
            target_id=target.id,
            metadata=mapping(metadata, "metadata"),
            origin=origin,
            review_state=review_state,
            confidence=confidence,
            actor=actor,
        )

    def get_question_genealogy(self, question_id: str) -> dict[str, Any]:
        question = self._require_object(question_id, kind="research_question")
        return {
            "question": self.get_research_object(question.id),
            "links": self.store.get_links(question.id, direction="both"),
        }

    def record_observation(
        self,
        statement: str,
        *,
        thread_id: str,
        conditions: dict[str, Any],
        evidence_refs: Sequence[str],
        origin: str = "EXPERIMENT_OBSERVED",
    ) -> ResearchObject:
        self._require_object(thread_id, kind="research_thread")
        refs = self._validate_refs(evidence_refs, "evidence_refs", required=True)
        payload = {
            "schema": "ObservationV1",
            "statement": text(statement, "statement"),
            "thread_id": thread_id,
            "conditions": mapping(conditions, "conditions", required=True),
            "evidence_refs": refs,
        }
        block_evidence = [(ref, "observed_in", None) for ref in refs if ref.startswith("block_")]
        return self.create_research_object(
            kind="observation", title=statement[:120], body=payload["statement"], structured=payload,
            origin=origin, review_state="ACCEPTED", evidence=block_evidence,
        )

    def record_interpretation(
        self,
        statement: str,
        *,
        thread_id: str,
        derived_from: Sequence[str],
        origin: str = "AGENT_INTERPRETED",
        review_state: str = "UNREVIEWED",
    ) -> ResearchObject:
        self._require_object(thread_id, kind="research_thread")
        refs = strings(derived_from, "derived_from")
        if not refs:
            raise ValueError("derived_from requires at least one observation")
        for ref in refs:
            self._require_object(ref, kind="observation")
        payload = {
            "schema": "InterpretationV1",
            "statement": text(statement, "statement"),
            "thread_id": thread_id,
            "derived_from": refs,
        }
        return self.create_research_object(
            kind="interpretation", title=statement[:120], body=payload["statement"], structured=payload,
            origin=origin, review_state=review_state,
        )

    def record_tension(
        self,
        statement: str,
        *,
        thread_id: str,
        side_a: Sequence[str],
        side_b: Sequence[str],
        possible_explanations: Sequence[str] = (),
        status: str = "unresolved",
    ) -> ResearchObject:
        self._require_object(thread_id, kind="research_thread")
        payload = {
            "schema": "TensionV1",
            "statement": text(statement, "statement"),
            "thread_id": thread_id,
            "side_a": self._validate_refs(side_a, "side_a", required=True),
            "side_b": self._validate_refs(side_b, "side_b", required=True),
            "possible_explanations": strings(possible_explanations, "possible_explanations"),
            "status": enum(status, "tension status", TENSION_STATUSES),
        }
        return self.create_research_object(
            kind="tension", title=statement[:120], body=payload["statement"], structured=payload,
            origin="USER_STATED", review_state="ACCEPTED",
        )

    def record_usage_episode(
        self,
        body: str,
        *,
        candidate: str | None = None,
        disposition: str | None = None,
        reason: str | None = None,
        what_would_reconsider: str | None = None,
        thread_id: str | None = None,
        structured: dict[str, Any] | None = None,
        title: str | None = None,
    ) -> ResearchObject:
        if structured is not None:
            payload = {"schema": "UsageEpisodeV1", **structured}
        else:
            if thread_id is None:
                raise ValueError("thread_id is required")
            self._require_object(thread_id, kind="research_thread")
            payload = {
                "schema": "UsageEpisodeV1",
                "candidate": text(candidate, "candidate"),
                "disposition": enum(disposition, "usage disposition", USAGE_DISPOSITIONS),
                "reason": text(reason, "reason"),
                "what_would_reconsider": text(what_would_reconsider, "what_would_reconsider"),
                "thread_id": thread_id,
            }
        return self.create_research_object(
            kind="usage_episode", title=title or payload.get("candidate"), body=text(body, "body"),
            structured=payload,
        )

    def _require_object(self, object_id: str, *, kind: str | None = None) -> ResearchObject:
        record = self.store.get_object(text(object_id, "object_id", maximum=100))
        if record is None:
            raise LookupError(f"Research object not found: {object_id}")
        if kind is not None and record.kind != kind:
            raise ValueError(f"Expected {kind}, got {record.kind}: {object_id}")
        return record

    def _validate_refs(self, refs: Sequence[str], label: str, *, required: bool = False) -> list[str]:
        result = strings(refs, label)
        if required and not result:
            raise ValueError(f"{label} requires at least one reference")
        for ref in result:
            if ref.startswith("block_"):
                if self.get_evidence(ref) is None:
                    raise LookupError(f"Evidence block not found: {ref}")
            elif ref.startswith("obj_"):
                self._require_object(ref)
            else:
                raise ValueError(f"{label} reference must be a block_ or obj_ id: {ref}")
        return result

    def _embedding_provider(self, semantic_live: bool) -> Any | None:
        if not semantic_live:
            return None
        model = os.getenv("RESEARCH_EMBED_MODEL", "text-embedding-3-small")
        if self.embedding_provider_factory:
            return self.embedding_provider_factory(model=model)
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is required for semantic-live retrieval")
        from .openai_provider import OpenAIEmbeddingProvider
        return OpenAIEmbeddingProvider(model=model)

    def search(self, query: str, *, kinds: Sequence[str] | None = None,
               filters: RetrievalFiltersV1 | None = None, limit: int = 10,
               semantic_live: bool = False, query_vector: Sequence[float] | None = None) -> list[SearchHitV2]:
        return self.retriever.retrieve(query, kinds=kinds, filters=filters, limit=limit,
                                       reliable=False, query_vector=query_vector,
                                       embedding_provider=self._embedding_provider(semantic_live))

    def recall(self, question: str, *, kinds: Sequence[str] | None = None,
               filters: RetrievalFiltersV1 | None = None, limit: int = 10,
               semantic_live: bool = False, query_vector: Sequence[float] | None = None) -> list[SearchHitV2]:
        return self.retriever.retrieve(question, kinds=kinds, filters=filters, limit=limit,
                                       reliable=True, query_vector=query_vector,
                                       embedding_provider=self._embedding_provider(semantic_live))

    def find_methods(self, problem_signature: str, *, filters: RetrievalFiltersV1 | None = None,
                     limit: int = 10, semantic_live: bool = False,
                     query_vector: Sequence[float] | None = None) -> list[SearchHitV2]:
        return self.recall(problem_signature, kinds=["method_card"], filters=filters, limit=limit,
                           semantic_live=semantic_live, query_vector=query_vector)

    def context(
        self,
        question: str,
        *,
        thread_id: str | None = None,
        mode: str = "analysis",
        filters: RetrievalFiltersV1 | None = None,
        limit: int = 8,
        blind_first: str | None = None,
        semantic_live: bool = False,
        query_vector: Sequence[float] | None = None,
    ) -> ResearchPacketV1:
        return self.context_compiler.compile(
            question, thread_id=thread_id, mode=mode, filters=filters, limit=limit,
            blind_first=blind_first, query_vector=query_vector,
            embedding_provider=self._embedding_provider(semantic_live),
        )

    def get_history(self, object_id: str) -> list[dict[str, Any]]:
        return self.store.history(object_id)
