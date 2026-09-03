"""Provider contracts for derived representations.

The durable Brain does not import a model SDK. Provider adapters implement this
protocol, and their outputs must be stored with model/schema provenance.
"""

from __future__ import annotations

from typing import Any, Protocol, Sequence


class ExtractionProvider(Protocol):
    def extract(self, *, schema_name: str, instructions: str, evidence: Sequence[dict[str, Any]]) -> dict[str, Any]: ...


class EmbeddingProvider(Protocol):
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...
