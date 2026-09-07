"""Opt-in OpenAI adapters. Importing Research Brain never requires the SDK."""

from __future__ import annotations

import json
from typing import Any, Sequence


class OpenAIResponsesProvider:
    name = "openai"

    def __init__(self, *, model: str, reasoning_effort: str = "medium", max_output_tokens: int = 16_384):
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("Live extraction requires: pip install 'research-brain[openai]'") from exc
        self.model = model
        self.reasoning_effort = reasoning_effort
        if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 65_536:
            raise ValueError("Invalid output token ceiling")
        self.max_output_tokens = max_output_tokens
        self.client = OpenAI(max_retries=0, timeout=120)

    def extract(self, *, schema_name: str, schema: dict[str, Any], instructions: str,
                evidence: Sequence[dict[str, Any]]) -> dict[str, Any]:
        response = self.client.responses.create(
            model=self.model,
            reasoning={"effort": self.reasoning_effort},
            instructions=instructions,
            input=json.dumps(list(evidence), ensure_ascii=False),
            text={"format": {"type": "json_schema", "name": schema_name,
                              "strict": True, "schema": schema}},
            store=False,
            max_output_tokens=self.max_output_tokens,
        )
        return {
            "response_id": response.id,
            "output": json.loads(response.output_text),
            "usage": response.usage.model_dump() if response.usage else {},
        }


class OpenAIEmbeddingProvider:
    name = "openai"

    def __init__(self, *, model: str):
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("Live embeddings require: pip install 'research-brain[openai]'") from exc
        self.model = model
        self.client = OpenAI(max_retries=0)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        response = self.client.embeddings.create(model=self.model, input=list(texts), encoding_format="float")
        return [item.embedding for item in response.data]
