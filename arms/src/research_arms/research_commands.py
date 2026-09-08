"""Read-only destination-scoped research commands; no Pi or paid query calls."""
import asyncio
from dataclasses import asdict, is_dataclass
import json
import re

from research_brain.models import RetrievalFiltersV1


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


async def compare(registry, actor, destination, papers, question):
    """Build a revision-pinned evidence dossier, not a generated judgment."""
    if not isinstance(question, str) or not question.strip() or len(question) > 2000 or "\x00" in question:
        raise ValueError("Comparison question must contain 1..2000 characters")
    if not isinstance(papers, str) or len(papers) > 500: raise ValueError("Invalid paper selections")
    pins = papers.split()
    if not 2 <= len(pins) <= 4 or len(set(pins)) != len(pins):
        raise ValueError("Select 2..4 distinct document@vN pins")
    if any(not re.fullmatch(r"doc_[A-Za-z0-9_-]{1,90}@v[1-9][0-9]{0,5}", pin) for pin in pins):
        raise ValueError("Use exact document@vN pins")
    with registry.connect(readonly=True) as db:
        principal = registry._principal(db, actor)["principal"]
    space = destination["space_id"]
    scope = registry.spaces.scope(principal, conversation_id="research-compare", writable_space=space)
    async def read(operation, *args, **kwargs):
        return (await asyncio.to_thread(registry.spaces.read, scope, space, operation, *args, **kwargs))["result"]
    selected = []
    # Resolve every pin before retrieval. No fallback to latest or other spaces.
    for pin in pins:
        document_id, revision = pin.split("@")
        document = await read("get_document", document_id)
        versions = [v for v in document["versions"] if v["version_label"] == revision] if document else []
        if len(versions) != 1: raise ValueError("Selected paper revision unavailable")
        selected.append({"space_id": space, "document_id": document_id, "revision": revision,
            "document_version_id": versions[0]["id"], "title": document["title"]})
    for paper in selected:
        hits = await read("recall", question, filters=RetrievalFiltersV1(document_id=paper["document_id"],
            version_label=paper["revision"]), limit=3, semantic_live=False)
        paper["evidence"] = transport(hits)
        paper["match_found"] = bool(hits)
    registry.spaces.validate(scope, space_id=space)
    result = {"space_id": space, "kind": "revision_pinned_comparison_dossier", "lane": "reliable",
        "question": question, "papers": selected,
        "notice": "Evidence grouped by exact selected revisions, not a model-generated comparative judgment. Empty groups indicate a corpus/retrieval mismatch, not method inferiority. No paid query calls; uncached questions use lexical retrieval. Review labels and constraints remain attached to each hit."}
    if len(json.dumps(result, ensure_ascii=False).encode()) > 66000:
        raise ValueError("Comparison exceeds Discord bound; narrow the question")
    return result
