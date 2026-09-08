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
