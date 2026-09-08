"""Trusted local Pi session branching primitive, not an authorization interface.

The fork coordinator authorizes the source and destination, stops the source
worker, and durably stages the destination binding before invoking this.
Never pass Discord/model supplied paths to this function.
"""
import asyncio
import json
from pathlib import Path
import re
import uuid


def verify_completed_fork(*, source, candidate, session_id, entry_id):
    """Read-only verification of SDK-created fork files before local adoption.

    Paths are trusted coordinator inputs, never Discord arguments. Parsing raw
    JSON avoids SessionManager.open implicitly migrating older session files.
    """
    def read(path):
        path = Path(path)
        if not path.is_absolute() or path.is_symlink() or path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError("Invalid session file")
        with path.open("rb") as stream: data = stream.read(20 * 1024 * 1024 + 1)
        if len(data) > 20 * 1024 * 1024: raise ValueError("Session exceeds bound")
        values = [json.loads(line) for line in data.splitlines() if line.strip()]
        if not values or len(values) > 100000 or any(not isinstance(value, dict) for value in values):
            raise ValueError("Invalid session entries")
        header, entries = values[0], values[1:]
        if header.get("type") != "session" or header.get("version") != 3:
            raise ValueError("Unsupported session version; explicit migration required")
        ids = [entry.get("id") for entry in entries]
        if any(not isinstance(ident, str) or not ident for ident in ids) or len(set(ids)) != len(ids):
            raise ValueError("Invalid session entry identities")
        return header, entries
    head, entries = read(source)
    fork_head, fork_entries = read(candidate)
    if set(fork_head) != {"type", "version", "id", "timestamp", "cwd", "parentSession"}:
        raise ValueError("Unexpected fork header fields")
    if head.get("id") != session_id or fork_head.get("id") == session_id:
        raise ValueError("Session identity mismatch")
    fork_id = fork_head.get("id")
    if not isinstance(fork_id, str) or str(uuid.UUID(fork_id)) != fork_id:
        raise ValueError("Invalid fork session ID")
    if fork_head.get("parentSession") != str(Path(source)) or fork_head.get("cwd") != head.get("cwd"):
        raise ValueError("Fork source mismatch")
    by_id = {entry["id"]: entry for entry in entries}
    target = by_id.get(entry_id, {})
    if target.get("type") != "message" or target.get("message", {}).get("role") != "assistant" or target["message"].get("stopReason") != "stop":
        raise ValueError("Completed assistant entry required")
    branch, seen, cursor = [], set(), entry_id
    while cursor is not None:
        if cursor in seen or cursor not in by_id: raise ValueError("Broken source branch")
        seen.add(cursor)
        item = by_id[cursor]
        branch.append(item)
        cursor = item.get("parentId")
    expected, parent = [], None
    for item in reversed(branch):
        if item["type"] == "label": continue
        expected.append({**item, "parentId": parent})
        parent = item["id"]
    actual = [item for item in fork_entries if item.get("type") != "label"]
    if actual != expected: raise ValueError("Fork branch content differs")
    expected_labels = {}
    for item in entries:
        if item.get("type") == "label":
            if item.get("label"): expected_labels[item.get("targetId")] = item
            else: expected_labels.pop(item.get("targetId"), None)
    expected_labels = {key: value for key, value in expected_labels.items() if key in {item["id"] for item in expected}}
    labels = [item for item in fork_entries if item.get("type") == "label"]
    if len(labels) != len(expected_labels): raise ValueError("Fork labels differ")
    seen_labels = set()
    for label in labels:
        original = expected_labels.get(label.get("targetId"))
        if original is None or label["targetId"] in seen_labels or label.get("parentId") != parent:
            raise ValueError("Invalid fork label chain")
        if set(label) != {"type", "id", "parentId", "timestamp", "targetId", "label"} or any(label.get(k) != original.get(k) for k in ("timestamp", "targetId", "label")):
            raise ValueError("Fork label content differs")
        seen_labels.add(label["targetId"])
        parent = label["id"]
    if fork_entries != actual + labels: raise ValueError("Fork labels are not a trailing chain")
    return {"session_id": fork_id, "entry_id": entry_id, "retained_entries": len(expected), "path": str(Path(candidate))}


async def fork_completed_session(*, node, sdk_module, source, destination, session_id, entry_id):
    node, sdk_module, source, destination = map(Path, (node, sdk_module, source, destination))
    if not all(path.is_absolute() for path in (node, sdk_module, source, destination)):
        raise ValueError("Trusted absolute paths required")
    if str(uuid.UUID(session_id)) != session_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", entry_id):
        raise ValueError("Exact session and entry identifiers required")
    source, sdk_module = source.resolve(strict=True), sdk_module.resolve(strict=True)
    # Dedicated new directory prevents accidental overwrite or reuse of a
    # partially created destination. Partial output remains local for recovery.
    destination.mkdir(parents=True, mode=0o700, exist_ok=False)
    destination = destination.resolve()
    helper = Path(__file__).parent / "integrations" / "fork-session.mjs"
    process = await asyncio.create_subprocess_exec(str(node), str(helper), cwd=destination,
        env={}, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, limit=16001)
    try:
        payload = json.dumps({"sdk_module": str(sdk_module), "source": str(source),
            "destination": str(destination), "session_id": session_id, "entry_id": entry_id}).encode()
        if len(payload) > 16000:
            raise ValueError("Fork request exceeds bound")
        process.stdin.write(payload)
        await process.stdin.drain()
        process.stdin.close()
        line = await asyncio.wait_for(process.stdout.readline(), 20)
        await asyncio.wait_for(process.wait(), 5)
        if process.returncode or len(line) > 16000:
            raise ValueError("Session fork unavailable")
        result = json.loads(line)
        result_path = Path(result["path"]).resolve(strict=True)
        if result_path.parent != destination or result["entry_id"] != entry_id:
            raise ValueError("Fork identity mismatch")
        if str(uuid.UUID(result["session_id"])) != result["session_id"] or result["session_id"] == session_id:
            raise ValueError("Fork session identity mismatch")
        return result
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
