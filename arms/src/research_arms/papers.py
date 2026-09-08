"""Read-only paper operations, explicitly confined to the current space."""
import json
import re

from .registry import Unavailable


def read_paper(registry, actor, destination, operation, identifier):
    if operation not in {"status", "brief", "cards", "evidence"}:
        raise Unavailable()
    prefix = "block" if operation == "evidence" else "doc"
    if not isinstance(identifier, str) or not re.fullmatch(prefix + r"_[A-Za-z0-9_-]{1,90}", identifier):
        raise Unavailable()
    with registry.connect(readonly=True) as db:
        principal = registry._principal(db, actor)["principal"]
    scope = registry.spaces.scope(principal, conversation_id="paper-read",
        writable_space=destination["space_id"])
    method = {"status": "paper_overview", "brief": "paper_overview", "cards": "paper_cards", "evidence": "read_evidence_field"}[operation]
    options = {"char_limit": 1000} if operation == "evidence" else {"limit": 2} if operation == "cards" else {}
    result = registry.spaces.read(scope, destination["space_id"], method, identifier, **options)
    if len(json.dumps(result).encode()) > 16000:
        raise ValueError("Paper result exceeds bound")
    return result
