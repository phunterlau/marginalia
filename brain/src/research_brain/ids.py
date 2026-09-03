"""Deterministic identifiers used by the durable ledger."""

from __future__ import annotations

import hashlib


def digest(*parts: str | bytes, length: int = 24) -> str:
    value = hashlib.sha256()
    for part in parts:
        if isinstance(part, str):
            part = part.encode("utf-8")
        value.update(len(part).to_bytes(8, "big"))
        value.update(part)
    return value.hexdigest()[:length]


def stable_id(prefix: str, *parts: str | bytes) -> str:
    return f"{prefix}_{digest(*parts)}"
