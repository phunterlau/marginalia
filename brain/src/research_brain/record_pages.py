"""Bounded, lossless field pages with explicit version and continuation metadata."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any


def page(record: dict[str, Any], field: str, *, offset: int = 0, limit: int = 20,
         item_index: int | None = None, char_offset: int = 0, char_limit: int = 8000,
         expected_version: str | None = None) -> dict[str, Any]:
    if not re.fullmatch(r"(?:structured(?:\.[A-Za-z0-9_]{1,80})?|body|evidence|raw_text|raw_latex|normalized_text)", field):
        raise ValueError("Unsupported record field")
    for value, low, high in ((offset, 0, 1_000_000), (limit, 1, 100), (char_offset, 0, 100_000_000), (char_limit, 1, 16_000)):
        if type(value) is not int or not low <= value <= high:
            raise ValueError("Invalid page bounds")
    if item_index is not None and (type(item_index) is not int or not 0 <= item_index <= 1_000_000):
        raise ValueError("Invalid item index")
    version = hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if expected_version is not None and expected_version != version:
        raise ValueError("Record changed; restart pagination with its current version")
    container = record.get("structured", {}) if field.startswith("structured.") else record
    key = field.split(".", 1)[-1]
    if key not in container:
        raise LookupError("Field unavailable")
    value = container[key]
    result = {"record_id": record.get("id") or record.get("block_id"), "field": field,
              "version": version, "origin": record.get("origin"), "review_state": record.get("review_state")}
    if str(result["record_id"]).startswith("block_"):
        result.update(record_type="document_block", origin="SOURCE_EXPLICIT",
                      source_locator={key: record.get(key) for key in ("document_id", "version_label", "source_member", "compilation_id", "line_start", "line_end", "char_start", "char_end", "raw_sha256")})
    else:
        result["record_type"] = "research_object"
    if isinstance(value, dict):
        value = [{"key": k, "value": v} for k, v in sorted(value.items())]
        result["collection_type"] = "mapping"
    if isinstance(value, list) and item_index is None:
        items = []
        for item in value[offset:offset + limit]:
            if len(json.dumps([*items, item], ensure_ascii=False)) > 24_000:
                break
            items.append(item)
        next_offset = offset + len(items)
        result.update(items=items, offset=offset, total=len(value), returned=len(items),
                      omitted=max(0, len(value) - next_offset),
                      next_offset=next_offset if next_offset < len(value) else None,
                      requires_item_index=next_offset if not items and next_offset < len(value) else None)
        return result
    if item_index is not None:
        if not isinstance(value, list) or item_index >= len(value):
            raise LookupError("List item unavailable")
        value = value[item_index]
        result["item_index"] = item_index
    serialized = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    fragment = serialized[char_offset:char_offset + char_limit]
    end = char_offset + len(fragment)
    result.update(fragment=fragment, encoding="text" if isinstance(value, str) else "json",
                  char_offset=char_offset, total_characters=len(serialized), returned_characters=len(fragment),
                  omitted_characters=max(0, len(serialized) - end), next_char_offset=end if end < len(serialized) else None)
    return result
