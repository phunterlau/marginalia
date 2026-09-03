"""Headless facade used by Python, CLI, Pi, Codex, and future clients."""

from __future__ import annotations

from pathlib import Path
import os
from typing import Any, Sequence

from .embeddings import EmbeddingIndexer, EmbeddingResult
from .extraction import ExtractionResult, Extractor
from .ingest import Ingestor
from .models import IngestResult, ResearchObject, RetrievalFiltersV1, SearchHitV2
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
    ) -> ResearchObject:
        structured = {
            "question": question,
            "status": status,
            "constraints": list(constraints),
            "available_access": list(available_access),
            "desired_output": desired_output,
            "schema": "ResearchQuestionV1",
        }
        return self.create_research_object(
            kind="research_question", title=title or question[:120], body=question,
            structured=structured, origin="USER_STATED", review_state="ACCEPTED",
        )

    def record_usage_episode(self, body: str, *, structured: dict[str, Any], title: str | None = None) -> ResearchObject:
        payload = {"schema": "UsageEpisodeV1", **structured}
        return self.create_research_object(kind="usage_episode", title=title, body=body, structured=payload)

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

    def get_history(self, object_id: str) -> list[dict[str, Any]]:
        return self.store.history(object_id)
