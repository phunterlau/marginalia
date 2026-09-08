"""Read-only destination-scoped research commands; no Pi or paid query calls."""
import asyncio
from dataclasses import asdict, is_dataclass
import json


def transport(value):
    if is_dataclass(value): value = asdict(value)
    if isinstance(value, dict):
        return {key: transport(item) for key, item in value.items()
                if key not in {"local_path", "session_path", "source_path", "root"}}
    if isinstance(value, (tuple, list)): return [transport(item) for item in value]
    return value


async def recall(registry, actor, destination, question):
    if not isinstance(question, str) or not question.strip() or len(question) > 2000 or "\x00" in question:
        raise ValueError("Recall question must contain 1..2000 characters")
    with registry.connect(readonly=True) as db:
        principal = registry._principal(db, actor)["principal"]
    space = destination["space_id"]
    scope = registry.spaces.scope(principal, conversation_id="research-recall", writable_space=space)
    result = await asyncio.to_thread(registry.spaces.read, scope, space, "recall", question, limit=3, semantic_live=False)
    result = transport(result)
    result.update(lane="reliable", notice="Source evidence and accepted research records only. Acceptance is source-faithfulness review, not universal scientific correctness. Semantic retrieval uses an existing cached query embedding when available; no paid query call is made.")
    if not result["result"]:
        result["notice"] += " No matching reliable memory in this space; this is not evidence that no relevant method exists."
    # Never silently truncate canonical evidence or structured constraints.
    if len(json.dumps(result, ensure_ascii=False).encode()) > 66000:
        raise ValueError("Recall result exceeds Discord bound; narrow the query or use the local reader")
    return result
