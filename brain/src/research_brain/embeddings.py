"""Explicit embedding indexing with immutable representation provenance."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from typing import Any, Callable

from .ids import stable_id
from .store.sqlite import SQLiteStore, utc_now


@dataclass(frozen=True)
class EmbeddingPlan:
    target: str
    model: str
    representation_version: str
    target_count: int
    input_characters: int
    expected_calls: int
    live: bool


@dataclass(frozen=True)
class EmbeddingResult:
    plan: EmbeddingPlan
    run_id: str | None
    created_representations: int
    cached: bool


class EmbeddingIndexer:
    def __init__(self, store: SQLiteStore, provider_factory: Callable[..., Any] | None = None):
        self.store = store
        self.provider_factory = provider_factory

    def index(self, target: str, *, live: bool = False, force: bool = False,
              batch_size: int = 64) -> EmbeddingResult:
        document_id = None if target == "all" else target
        model = os.getenv("RESEARCH_EMBED_MODEL", "text-embedding-3-small")
        version = "semantic-v1"
        records = self.store.embedding_targets(document_id)
        if not force:
            records = self.store.unembedded_targets(records, model=model, version=version)
        plan = EmbeddingPlan(target, model, version, len(records), sum(len(item["text"]) for item in records),
                             (len(records) + batch_size - 1) // batch_size, live)
        if not live:
            return EmbeddingResult(plan, None, 0, False)
        if not records:
            return EmbeddingResult(plan, None, 0, True)
        if not os.getenv("OPENAI_API_KEY") and self.provider_factory is None:
            raise RuntimeError("OPENAI_API_KEY is required for --live embedding indexing")
        digest = hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()
        run_id = stable_id(
            "gen", "embeddings", model, version, "float32-vector-v1", digest,
            utc_now() if force else "canonical",
        )
        run_id, created = self.store.begin_generation(
            run_id=run_id, task="embeddings", provider="openai", model=model, reasoning_effort=None,
            prompt_version=version, schema_version="float32-vector-v1", input_digest=digest,
            block_ids=[item["object_id"] for item in records if item["object_type"] == "document_block"],
            request={"batch_size": batch_size, "encoding_format": "float", "store": False}, force=force,
        )
        if not created:
            return EmbeddingResult(plan, run_id, 0, True)
        try:
            provider = self.provider_factory(model=model) if self.provider_factory else None
            if provider is None:
                from .openai_provider import OpenAIEmbeddingProvider
                provider = OpenAIEmbeddingProvider(model=model)
            output: list[dict[str, Any]] = []
            attempt = 0
            for start in range(0, len(records), batch_size):
                batch = records[start:start + batch_size]
                attempt += 1
                started = utc_now()
                try:
                    vectors = provider.embed([item["text"] for item in batch])
                    if len(vectors) != len(batch) or any(not vector for vector in vectors):
                        raise ValueError("embedding provider returned the wrong number of vectors")
                except Exception as exc:
                    self.store.record_attempt(run_id=run_id, number=attempt, started_at=started,
                                              outcome="fatal_error", status_code=getattr(exc, "status_code", None),
                                              error={"type": type(exc).__name__, "message": str(exc)})
                    raise
                self.store.record_attempt(run_id=run_id, number=attempt, started_at=started, outcome="success")
                for item, vector in zip(batch, vectors, strict=True):
                    output.append({**item, "vector": vector,
                                   "input_digest": hashlib.sha256(item["text"].encode()).hexdigest()})
            inserted = self.store.save_embeddings(output, model=model, version=version)
            self.store.finish_generation(run_id, status="complete", output={"representation_count": inserted})
            return EmbeddingResult(plan, run_id, inserted, False)
        except Exception as exc:
            self.store.finish_generation(run_id, status="failed",
                                         error={"type": type(exc).__name__, "message": str(exc)})
            raise
