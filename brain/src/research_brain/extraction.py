"""Explicit, ledgered extraction of typed research cards."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from copy import deepcopy
import hashlib
import json
import os
import time
from typing import Any, Callable

from .ids import stable_id
from .models import MathCardV1, MethodCardV1
from .schemas import (MATH_EXTRACTION_SCHEMA, METHOD_EXTRACTION_SCHEMA,
                      validate_math_cards, validate_method_cards, validate_schema_shape)
from .store.sqlite import SQLiteStore, utc_now


MAX_CHARS = 180_000
PROMPT_VERSION = "evidence-cards-v6-source-context-no-generated-citations"
MAX_CITATION_IDS = 250


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


METHOD_INSTRUCTIONS = """Extract paper-specific methods from the supplied source context. Produce readable card content only; do not generate citations, block IDs, or evidence references. Source context is recorded by the application, not selected by you. Use null when an access requirement is not established. Do not infer experimental success or requirements that the context does not support."""
MATH_INSTRUCTIONS = """Interpret the single supplied displayed equation and its surrounding context. Produce readable card content only; do not generate citations, block IDs, or evidence references. Do not reproduce or alter LaTeX; the application copies the exact equation from its source. Return no cards if there is no meaningful interpretation to extract."""


def _chunk_ids(records: list[dict[str, Any]]) -> set[str]:
    return {item["block_id"] for record in records for item in [record, *record.get("context", [])]}


def _chunk_schema(task: str, chunk: list[dict[str, Any]]) -> dict[str, Any]:
    """The model supplies content only; provenance is attached by the caller."""
    ids = _chunk_ids(chunk)
    if not ids or len(ids) > MAX_CITATION_IDS:
        raise ValueError("Extraction chunk exceeds citation enum bounds")
    schema = deepcopy(METHOD_EXTRACTION_SCHEMA if task == "methods" else MATH_EXTRACTION_SCHEMA)
    card = schema["properties"]["cards"]["items"]
    for field in (["evidence"] if task == "methods" else ["equation_block_id", "context_block_ids"]):
        del card["properties"][field]
        card["required"].remove(field)
    return schema


def _provider_context(chunk: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Do not ask the model to copy identifiers it does not need to see."""
    def strip(value):
        if isinstance(value, dict):
            return {key: strip(item) for key, item in value.items() if key != "block_id"}
        if isinstance(value, list):
            return [strip(item) for item in value]
        return value
    return strip(chunk)


def _attach_context(task: str, output: dict[str, Any], chunk: list[dict[str, Any]]) -> dict[str, Any]:
    result = deepcopy(output)
    for card in result["cards"]:
        if task == "methods":
            card["evidence"] = [{"block_id": key, "relation": "source_context_only"}
                                for key in sorted(_chunk_ids(chunk))]
        else:
            if len(chunk) != 1:
                raise ValueError("Citation-free math extraction requires one equation per call")
            card["equation_block_id"] = chunk[0]["block_id"]
            card["context_block_ids"] = sorted({r["block_id"] for r in chunk[0].get("context", [])})
    return result


def _evidence_record(block: dict[str, Any]) -> dict[str, Any]:
    return {key: block.get(key) for key in
            ("id", "block_type", "section_path", "raw_text", "raw_latex", "source_member",
             "line_start", "line_end")}


def _serialized_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False))


def _split_prose(record: dict[str, Any], maximum: int) -> list[dict[str, Any]]:
    if _serialized_size(record) <= maximum:
        return [record]
    if record.get("raw_latex") or record.get("block_type") == "equation":
        raise ValueError(f"Canonical equation exceeds extraction input limit: {record['block_id']}")
    raw = record.get("raw_text") or ""
    result = []
    offset = 0
    while offset < len(raw):
        lo, hi = 1, len(raw) - offset
        best = None
        while lo <= hi:
            count = (lo + hi) // 2
            excerpt = {**record, "raw_text": raw[offset:offset + count],
                       "excerpt_char_start": offset, "excerpt_char_end": offset + count}
            if _serialized_size(excerpt) <= maximum:
                best = excerpt
                lo = count + 1
            else:
                hi = count - 1
        if best is None:
            raise ValueError("Evidence locator exceeds extraction input limit")
        result.append(best)
        offset = best["excerpt_char_end"]
    return result


def _pack_records(records: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for record in records:
        if _serialized_size([record]) > MAX_CHARS:
            raise ValueError("Evidence bundle exceeds extraction input limit")
        if current and (record.get("section_path") != current[-1].get("section_path")
                        or _serialized_size([*current, record]) > MAX_CHARS
                        or len(_chunk_ids([*current, record])) > MAX_CITATION_IDS):
            chunks.append(current)
            current = []
        current.append(record)
    if current:
        chunks.append(current)
    return chunks


def _method_chunks(blocks: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    records = []
    for block in blocks:
        if block["block_type"] == "comment":
            continue
        if block["block_type"] == "paragraph" and not any(line.strip() and not line.lstrip().startswith("%") for line in block["raw_text"].splitlines()):
            continue
        record = _evidence_record(block)
        record["block_id"] = record.pop("id")
        records.extend(_split_prose(record, MAX_CHARS - 2))
    return [chunk for chunk in _pack_records(records) if any(r["block_type"] != "heading" for r in chunk)]


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
        contexts = record.pop("context")
        bundle = {**record, "context": []}
        available = MAX_CHARS - _serialized_size([bundle]) - 4
        if available < 256:
            raise ValueError(f"Canonical equation exceeds extraction input limit: {record['block_id']}")
        for context_record in contexts:
            for part in _split_prose(context_record, available):
                if _serialized_size([{**bundle, "context": [*bundle["context"], part]}]) > MAX_CHARS:
                    records.append(bundle)
                    bundle = {**record, "context": []}
                bundle["context"].append(part)
        records.append(bundle)
    # Each interpretation has one unambiguous canonical equation without asking
    # the model to identify it. Oversized context may yield multiple bounded calls.
    return [[record] for record in records]


class Extractor:
    def __init__(self, store: SQLiteStore, provider_factory: Callable[..., Any] | None = None):
        self.store = store
        self.provider_factory = provider_factory

    def extract(self, task: str, document_id: str, *, compilation_id: str | None = None,
                live: bool = False, force: bool = False, model: str | None = None,
                reasoning_effort: str | None = None, run_callback: Callable[[str], None] | None = None) -> ExtractionResult:
        if task not in {"methods", "math"}:
            raise ValueError("task must be methods or math")
        blocks = self.store.get_blocks(document_id, compilation_id=compilation_id)
        if not blocks:
            raise LookupError(f"No blocks found for document: {document_id}")
        compilation_id = blocks[0]["compilation_id"]
        chunks = _method_chunks(blocks) if task == "methods" else _math_chunks(blocks)
        model = model or os.getenv("RESEARCH_EXTRACT_MODEL", "gpt-5.6-luna")
        effort = reasoning_effort or os.getenv("RESEARCH_REASONING_EFFORT", "medium")
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
            block_ids=block_ids, request={"store": False, "chunk_count": len(chunks), "max_output_tokens": 16_384,
                                         "citation_mode": "source_context_only",
                                         "source_contexts": [sorted(_chunk_ids(chunk)) for chunk in chunks]}, force=force,
        )
        if run_callback:
            run_callback(run_id)
        if not created:
            with self.store.connect() as connection:
                ids = tuple(row[0] for row in connection.execute(
                    "SELECT id FROM research_objects WHERE extraction_run_id=? ORDER BY created_at, id", (run_id,)))
            return ExtractionResult(plan, run_id, True, ids)
        schema = METHOD_EXTRACTION_SCHEMA if task == "methods" else MATH_EXTRACTION_SCHEMA
        instructions = METHOD_INSTRUCTIONS if task == "methods" else MATH_INSTRUCTIONS
        outputs: list[Any] = []
        attributed_outputs: list[dict[str, Any]] = []
        merged: dict[str, Any] | None = None
        response_ids: list[str] = []
        total_usage: dict[str, int] = {}
        attempt_number = 0
        block_map = {block["id"]: block for block in blocks}
        try:
            if self.provider_factory:
                provider = self.provider_factory(model=model, reasoning_effort=effort)
            else:
                from .openai_provider import OpenAIResponsesProvider
                provider = OpenAIResponsesProvider(model=model, reasoning_effort=effort)
            for chunk in chunks:
                chunk_schema = _chunk_schema(task, chunk)
                allowed_ids = _chunk_ids(chunk)
                chunk_blocks = {key: block_map[key] for key in allowed_ids}
                for retry in range(3):
                    attempt_number += 1
                    started = utc_now()
                    self.store.dispatch_attempt(run_id=run_id, number=attempt_number, started_at=started)
                    try:
                        response = provider.extract(schema_name=schema_version, schema=chunk_schema,
                                                    instructions=instructions, evidence=_provider_context(chunk))
                        outputs.append(response["output"])
                        response_ids.append(response.get("response_id", ""))
                        for key, value in response.get("usage", {}).items():
                            if isinstance(value, int):
                                total_usage[key] = total_usage.get(key, 0) + value
                        self.store.record_attempt(run_id=run_id, number=attempt_number, started_at=started,
                                                  outcome="success", usage=response.get("usage", {}))
                        break
                    except Exception as exc:
                        returned = getattr(exc, "response_payload", None)
                        if returned is not None:
                            outputs.append(returned)
                            response_ids.append(returned.get("response_id", ""))
                            for key, value in returned.get("usage", {}).items():
                                if isinstance(value, int):
                                    total_usage[key] = total_usage.get(key, 0) + value
                        status = getattr(exc, "status_code", None)
                        retryable = status == 429 or isinstance(status, int) and 500 <= status <= 599
                        self.store.record_attempt(run_id=run_id, number=attempt_number, started_at=started,
                                                  outcome="retryable_error" if retryable else "fatal_error",
                                                  status_code=status,
                                                  usage=returned.get("usage", {}) if returned else None,
                                                  error={"type": type(exc).__name__, "message": str(exc)})
                        if retry == 2 or not retryable:
                            raise
                        time.sleep(2 ** retry)
                # A returned response is ledgered above before local validation.
                # Stop before spending on later chunks when this one is invalid;
                # retain all outputs but commit no cards from a partial task.
                validate_schema_shape(outputs[-1], chunk_schema)
                attributed = _attach_context(task, outputs[-1], chunk)
                if task == "methods":
                    validate_method_cards(attributed, allowed_ids)
                else:
                    validate_math_cards(attributed, chunk_blocks)
                attributed_outputs.append(attributed)
            merged_cards: list[dict[str, Any]] = []
            seen_cards: set[tuple[str, tuple[str, ...]]] = set()
            block_map = {block["id"]: block for block in blocks}
            for output in attributed_outputs:
                # Validate each response before merging: malformed chunks must
                # not disappear into a seemingly valid empty or partial result.
                validate_schema_shape(output, schema)
                if task == "methods":
                    validate_method_cards(output, set(block_map))
                else:
                    validate_math_cards(output, block_map)
                for card in output.get("cards", []):
                    evidence_ids = tuple(sorted(
                        ref.get("block_id", "") for ref in card.get("evidence", [])
                    )) if task == "methods" else (card["equation_block_id"], *sorted(card["context_block_ids"]))
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
                    evidence = [(card.equation_block_id, "canonical_equation_source")]
                    evidence.extend((item, "source_context_only") for item in card.context_block_ids)
                    body = f"{card.semantic_gloss}\n\n{card.exact_latex}"
                    kind = "math_card"
                objects.append({"kind": kind, "title": card.name, "body": body,
                                "structured": {"schema": schema_version, **structured,
                                               "citation_mode": "source_context_only",
                                               "evidence_notice": "Application-attached input context; not claim-level citations or verified support."},
                                "evidence": evidence})
            created_objects = self.store.create_extracted_objects(objects, run_id=run_id,
                completion={"response_ids": response_ids,
                            "output": {**merged, "chunk_outputs": outputs, "citation_mode": "source_context_only"},
                            "usage": total_usage})
            return ExtractionResult(plan, run_id, False, tuple(item.id for item in created_objects))
        except Exception as exc:
            self.store.finish_generation(run_id, status="failed",
                                         response_id=json.dumps(response_ids) if response_ids else None,
                                         output={**(merged or {}), "chunk_outputs": outputs,
                                                 "citation_mode": "source_context_only"} if outputs else None,
                                         usage=total_usage or None,
                                         error={"type": type(exc).__name__, "message": str(exc)})
            raise
