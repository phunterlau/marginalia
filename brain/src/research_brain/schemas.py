"""Strict extraction schemas and fail-closed validators."""

from __future__ import annotations

import re
from typing import Any

from .models import EvidenceRefV1, MathCardV1, MethodCardV1


BLOCK_REFERENCE = re.compile(r"\bblock_[A-Za-z0-9_?-]+")


def _array(item: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": item}


STRING_ARRAY = _array({"type": "string"})
NULLABLE_BOOL = {"type": ["boolean", "null"]}
EVIDENCE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"block_id": {"type": "string"}, "relation": {"type": "string"}},
    "required": ["block_id", "relation"],
}

METHOD_EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "cards": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "name": {"type": "string"}, "problem": {"type": "string"},
                "mechanism": {"type": "string"}, "procedure": STRING_ARRAY,
                "inputs": STRING_ARRAY, "outputs": STRING_ARRAY,
                "gradients_required": NULLABLE_BOOL, "training_required": NULLABLE_BOOL,
                "activation_access": NULLABLE_BOOL, "weight_access": NULLABLE_BOOL,
                "assumptions": STRING_ARRAY, "failure_modes": STRING_ARRAY,
                "scientific_moves": STRING_ARRAY,
                "evidence": _array(EVIDENCE_SCHEMA),
            },
            "required": ["name", "problem", "mechanism", "procedure", "inputs", "outputs",
                         "gradients_required", "training_required", "activation_access", "weight_access",
                         "assumptions", "failure_modes", "scientific_moves", "evidence"],
        }},
    },
    "required": ["cards"],
}

MATH_EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "cards": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "name": {"type": "string"}, "equation_block_id": {"type": "string"},
                "semantic_gloss": {"type": "string"}, "role": {"type": "string"},
                "symbols": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {"symbol": {"type": "string"}, "meaning": {"type": "string"}},
                    "required": ["symbol", "meaning"],
                }},
                "assumptions": STRING_ARRAY, "affordances": STRING_ARRAY,
                "failure_modes": STRING_ARRAY, "math_move": {"type": "string"},
                "context_block_ids": STRING_ARRAY,
            },
            "required": ["name", "equation_block_id", "semantic_gloss", "role", "symbols",
                         "assumptions", "affordances", "failure_modes", "math_move", "context_block_ids"],
        }},
    },
    "required": ["cards"],
}


def validate_schema_shape(value: Any, schema: dict[str, Any], path: str = "output") -> None:
    """Validate the finite JSON Schema subset used by the two extraction contracts.

    The provider's strict mode is not a substitute for local validation of mocked,
    corrupted or unexpectedly shaped responses.
    """
    kind = schema["type"]
    kinds = kind if isinstance(kind, list) else [kind]
    actual = "null" if value is None else {dict: "object", list: "array", str: "string", bool: "boolean", int: "integer", float: "number"}.get(type(value))
    if actual not in kinds:
        raise ValueError(f"{path}: invalid JSON type")
    if actual == "object":
        properties = schema["properties"]
        if not set(schema.get("required", [])) <= value.keys():
            raise ValueError(f"{path}: missing required fields")
        if schema.get("additionalProperties") is False and not value.keys() <= properties.keys():
            raise ValueError(f"{path}: unexpected fields")
        for key, item in value.items():
            validate_schema_shape(item, properties[key], f"{path}.{key}")
    elif actual == "array":
        for index, item in enumerate(value):
            validate_schema_shape(item, schema["items"], f"{path}[{index}]")


def _strings(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a list of strings")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_bool(value: Any, field: str) -> bool | None:
    if value is not None and not isinstance(value, bool):
        raise ValueError(f"{field} must be boolean or null")
    return value


def _embedded_block_refs(value: Any) -> set[str]:
    if isinstance(value, str):
        return set(BLOCK_REFERENCE.findall(value))
    if isinstance(value, list):
        return set().union(*(_embedded_block_refs(item) for item in value), set())
    if isinstance(value, dict):
        return set().union(*(_embedded_block_refs(item) for item in value.values()), set())
    return set()


def validate_method_cards(payload: Any, allowed_blocks: set[str]) -> list[MethodCardV1]:
    if not isinstance(payload, dict) or not isinstance(payload.get("cards"), list):
        raise ValueError("method extraction must contain a cards array")
    result: list[MethodCardV1] = []
    for raw in payload["cards"]:
        if not isinstance(raw, dict):
            raise ValueError("method card must be an object")
        evidence: list[EvidenceRefV1] = []
        for ref in raw.get("evidence", []):
            if not isinstance(ref, dict) or ref.get("block_id") not in allowed_blocks:
                raise ValueError("method card references evidence outside the extraction input")
            evidence.append(EvidenceRefV1(ref["block_id"], _text(ref.get("relation"), "relation")))
        if not evidence:
            raise ValueError("method card requires at least one evidence reference")
        narrative = {key: value for key, value in raw.items() if key != "evidence"}
        narrative_refs = _embedded_block_refs(narrative)
        invalid_refs = sorted(narrative_refs - allowed_blocks)
        if invalid_refs:
            raise ValueError(f"method card narrative references unknown evidence: {invalid_refs[0]}")
        linked = {ref.block_id for ref in evidence}
        evidence.extend(
            EvidenceRefV1(block_id, "Referenced in structured card narrative.")
            for block_id in sorted(narrative_refs - linked)
        )
        result.append(MethodCardV1(
            name=_text(raw.get("name"), "name"), problem=_text(raw.get("problem"), "problem"),
            mechanism=_text(raw.get("mechanism"), "mechanism"), procedure=_strings(raw.get("procedure"), "procedure"),
            inputs=_strings(raw.get("inputs"), "inputs"), outputs=_strings(raw.get("outputs"), "outputs"),
            gradients_required=_optional_bool(raw.get("gradients_required"), "gradients_required"),
            training_required=_optional_bool(raw.get("training_required"), "training_required"),
            activation_access=_optional_bool(raw.get("activation_access"), "activation_access"),
            weight_access=_optional_bool(raw.get("weight_access"), "weight_access"),
            assumptions=_strings(raw.get("assumptions"), "assumptions"),
            failure_modes=_strings(raw.get("failure_modes"), "failure_modes"),
            scientific_moves=_strings(raw.get("scientific_moves"), "scientific_moves"), evidence=evidence,
        ))
    return result


def validate_math_cards(payload: Any, blocks: dict[str, dict[str, Any]]) -> list[MathCardV1]:
    if not isinstance(payload, dict) or not isinstance(payload.get("cards"), list):
        raise ValueError("math extraction must contain a cards array")
    result: list[MathCardV1] = []
    for raw in payload["cards"]:
        if not isinstance(raw, dict):
            raise ValueError("math card must be an object")
        equation_id = _text(raw.get("equation_block_id"), "equation_block_id")
        equation = blocks.get(equation_id)
        if not equation or equation["block_type"] != "equation" or not equation.get("raw_latex"):
            raise ValueError("math card must reference an input equation with exact LaTeX")
        context = _strings(raw.get("context_block_ids"), "context_block_ids")
        if any(item not in blocks for item in context):
            raise ValueError("math card context references a block outside the extraction input")
        narrative = {
            key: value for key, value in raw.items()
            if key not in {"equation_block_id", "context_block_ids"}
        }
        invalid_refs = sorted(_embedded_block_refs(narrative) - {equation_id, *context})
        if invalid_refs:
            raise ValueError(f"math card narrative references unlinked evidence: {invalid_refs[0]}")
        raw_symbols = raw.get("symbols")
        if not isinstance(raw_symbols, list) or not all(
            isinstance(item, dict) and isinstance(item.get("symbol"), str) and isinstance(item.get("meaning"), str)
            for item in raw_symbols
        ):
            raise ValueError("symbols must be a list of symbol/meaning objects")
        symbols = {item["symbol"]: item["meaning"] for item in raw_symbols}
        result.append(MathCardV1(
            name=_text(raw.get("name"), "name"), equation_block_id=equation_id,
            exact_latex=equation["raw_latex"], semantic_gloss=_text(raw.get("semantic_gloss"), "semantic_gloss"),
            role=_text(raw.get("role"), "role"), symbols=symbols,
            assumptions=_strings(raw.get("assumptions"), "assumptions"),
            affordances=_strings(raw.get("affordances"), "affordances"),
            failure_modes=_strings(raw.get("failure_modes"), "failure_modes"),
            math_move=_text(raw.get("math_move"), "math_move"), context_block_ids=context,
        ))
    return result
