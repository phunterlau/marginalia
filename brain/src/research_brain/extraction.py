"""Explicit, ledgered extraction of typed research cards."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
import time
from typing import Any, Callable

from .ids import stable_id
from .models import MathCardV1, MethodCardV1
from .schemas import (MATH_EXTRACTION_SCHEMA, METHOD_EXTRACTION_SCHEMA,
                      validate_math_cards, validate_method_cards)
from .store.sqlite import SQLiteStore, utc_now


MAX_CHARS = 180_000
PROMPT_VERSION = "evidence-cards-v2"


@dataclass(frozen=True)
class ExtractionPlan:
    task: str
    document_id: str
    compilation_id: str
    model: str
    reasoning_effort: str
    schema_version: str
    block_ids: tuple[str, ...]
    input_characters: int
    expected_calls: int
    live: bool


@dataclass(frozen=True)
class ExtractionResult:
    plan: ExtractionPlan
    run_id: str | None
    cached: bool
    object_ids: tuple[str, ...]


METHOD_INSTRUCTIONS = """Extract paper-specific methods only. Every claim must cite one or more supplied block_id values. Every block identifier mentioned anywhere must exactly match a supplied block_id; include the principal supporting blocks in the formal evidence array. Use null when an access requirement is not established. Do not infer experimental success or requirements that the evidence does not support."""
MATH_INSTRUCTIONS = """Interpret important displayed equations. Reference exactly one supplied equation_block_id and only supplied context block IDs. Do not reproduce or alter LaTeX; the application copies canonical LaTeX from evidence after validation."""


def _evidence_record(block: dict[str, Any]) -> dict[str, Any]:
    return {key: block.get(key) for key in
            ("id", "block_type", "section_path", "raw_text", "raw_latex", "source_member",
             "line_start", "line_end")}


def _method_chunks(blocks: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    size = 0
    for block in blocks:
        record = _evidence_record(block)
        record["block_id"] = record.pop("id")
        item_size = len(record.get("raw_text") or "") + 256
        if current and size + item_size > MAX_CHARS:
            chunks.append(current)
            current, size = [], 0
        current.append(record)
        size += item_size
    if current:
        chunks.append(current)
    return chunks


def _math_chunks(blocks: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    for index, equation in enumerate(blocks):
        if equation["block_type"] != "equation" or not equation.get("raw_latex"):
            continue
        section = equation.get("section_path")
        context: list[dict[str, Any]] = []
        for direction in (-1, 1):
            cursor = index + direction
            found = 0
            while 0 <= cursor < len(blocks) and found < 2:
                candidate = blocks[cursor]
                if candidate.get("section_path") != section:
                    break
                if candidate["block_type"] == "paragraph":
                    context.append(candidate)
                    found += 1
                cursor += direction
        record = _evidence_record(equation)
        record["block_id"] = record.pop("id")
        record["context"] = []
        for item in sorted(context, key=lambda value: value["ordinal"]):
            context_record = _evidence_record(item)
            context_record["block_id"] = context_record.pop("id")
            record["context"].append(context_record)
        records.append(record)
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    size = 0
    for record in records:
        item_size = len(json.dumps(record, ensure_ascii=False))
        if current and size + item_size > MAX_CHARS:
            chunks.append(current)
            current, size = [], 0
        current.append(record)
        size += item_size
    if current:
        chunks.append(current)
    return chunks


class Extractor:
    def __init__(self, store: SQLiteStore, provider_factory: Callable[..., Any] | None = None):
        self.store = store
        self.provider_factory = provider_factory

    def extract(self, task: str, document_id: str, *, compilation_id: str | None = None,
                live: bool = False, force: bool = False) -> ExtractionResult:
        if task not in {"methods", "math"}:
            raise ValueError("task must be methods or math")
        blocks = self.store.get_blocks(document_id, compilation_id=compilation_id)
        if not blocks:
            raise LookupError(f"No blocks found for document: {document_id}")
        compilation_id = blocks[0]["compilation_id"]
        chunks = _method_chunks(blocks) if task == "methods" else _math_chunks(blocks)
        model = os.getenv("RESEARCH_EXTRACT_MODEL", "gpt-5.6-luna")
        effort = os.getenv("RESEARCH_REASONING_EFFORT", "medium")
        schema_version = "MethodCardV1" if task == "methods" else "MathCardV1"
        block_ids = tuple(block["id"] for block in blocks)
        plan = ExtractionPlan(task, document_id, compilation_id, model, effort, schema_version,
                              block_ids, sum(len(json.dumps(chunk, ensure_ascii=False)) for chunk in chunks),
                              len(chunks), live)
        if not live:
            return ExtractionResult(plan, None, False, ())
        if not os.getenv("OPENAI_API_KEY") and self.provider_factory is None:
            raise RuntimeError("OPENAI_API_KEY is required for --live extraction")
        input_digest = hashlib.sha256(json.dumps(chunks, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        run_id = stable_id(
            "gen", task, input_digest, model, effort, PROMPT_VERSION, schema_version,
            utc_now() if force else "canonical",
        )
        run_id, created = self.store.begin_generation(
            run_id=run_id, task=task, provider="openai", model=model, reasoning_effort=effort,
            prompt_version=PROMPT_VERSION, schema_version=schema_version, input_digest=input_digest,
            block_ids=block_ids, request={"store": False, "chunk_count": len(chunks)}, force=force,
        )
        if not created:
            with self.store.connect() as connection:
                ids = tuple(row[0] for row in connection.execute(
                    "SELECT id FROM research_objects WHERE extraction_run_id=? ORDER BY created_at, id", (run_id,)))
            return ExtractionResult(plan, run_id, True, ids)
        schema = METHOD_EXTRACTION_SCHEMA if task == "methods" else MATH_EXTRACTION_SCHEMA
        instructions = METHOD_INSTRUCTIONS if task == "methods" else MATH_INSTRUCTIONS
        outputs: list[Any] = []
        merged: dict[str, Any] | None = None
        response_ids: list[str] = []
        total_usage: dict[str, int] = {}
        attempt_number = 0
        try:
            if self.provider_factory:
                provider = self.provider_factory(model=model, reasoning_effort=effort)
            else:
                from .openai_provider import OpenAIResponsesProvider
                provider = OpenAIResponsesProvider(model=model, reasoning_effort=effort)
            for chunk in chunks:
                for retry in range(3):
                    attempt_number += 1
                    started = utc_now()
                    try:
                        response = provider.extract(schema_name=schema_version, schema=schema,
                                                    instructions=instructions, evidence=chunk)
                        outputs.append(response["output"])
                        response_ids.append(response.get("response_id", ""))
                        for key, value in response.get("usage", {}).items():
                            if isinstance(value, int):
                                total_usage[key] = total_usage.get(key, 0) + value
                        self.store.record_attempt(run_id=run_id, number=attempt_number, started_at=started,
                                                  outcome="success", usage=response.get("usage", {}))
                        break
                    except Exception as exc:
                        status = getattr(exc, "status_code", None)
                        retryable = status in {429, 500, 502, 503, 504} or status is None
                        self.store.record_attempt(run_id=run_id, number=attempt_number, started_at=started,
                                                  outcome="retryable_error" if retryable else "fatal_error",
                                                  status_code=status,
                                                  error={"type": type(exc).__name__, "message": str(exc)})
                        if retry == 2 or not retryable:
                            raise
                        time.sleep(2 ** retry)
            merged_cards: list[dict[str, Any]] = []
            seen_cards: set[tuple[str, tuple[str, ...]]] = set()
            for output in outputs:
                for card in output.get("cards", []):
                    evidence_ids = tuple(sorted(
                        ref.get("block_id", "") for ref in card.get("evidence", [])
                    )) if isinstance(card, dict) else ()
                    key = (str(card.get("name", "")).casefold().strip(), evidence_ids) if isinstance(card, dict) else ("", ())
                    if key not in seen_cards:
                        seen_cards.add(key)
                        merged_cards.append(card)
            merged = {"cards": merged_cards}
            block_map = {block["id"]: block for block in blocks}
            cards: list[MethodCardV1] | list[MathCardV1]
            cards = validate_method_cards(merged, set(block_map)) if task == "methods" else validate_math_cards(merged, block_map)
            objects = []
            for card in cards:
                structured = asdict(card)
                if task == "methods":
                    evidence = [(ref.block_id, ref.relation) for ref in card.evidence]
                    body = f"{card.problem}\n\nMechanism: {card.mechanism}"
                    kind = "method_card"
                else:
                    evidence = [(card.equation_block_id, "defines")]
                    evidence.extend((item, "context") for item in card.context_block_ids)
                    body = f"{card.semantic_gloss}\n\n{card.exact_latex}"
                    kind = "math_card"
                objects.append({"kind": kind, "title": card.name, "body": body,
                                "structured": {"schema": schema_version, **structured}, "evidence": evidence})
            created_objects = self.store.create_extracted_objects(objects, run_id=run_id)
            self.store.finish_generation(run_id, status="complete", response_id=json.dumps(response_ids),
                                         output=merged, usage=total_usage)
            return ExtractionResult(plan, run_id, False, tuple(item.id for item in created_objects))
        except Exception as exc:
            self.store.finish_generation(run_id, status="failed",
                                         response_id=json.dumps(response_ids) if response_ids else None,
                                         output=merged if merged is not None else ({"chunk_outputs": outputs} if outputs else None),
                                         usage=total_usage or None,
                                         error={"type": type(exc).__name__, "message": str(exc)})
            raise
