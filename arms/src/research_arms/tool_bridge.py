"""Private Unix-socket bridge for one worker's current scope-bound turn."""
import asyncio
import json
import os
from pathlib import Path
import secrets
import tempfile

from .registry import Unavailable


class ToolBridge:
    MAX_REQUEST = 90_000
    OPERATIONS = {"research_recall": "recall", "research_evidence": "get_evidence",
                  "research_object": "get_research_object"}

    def __init__(self, registry):
        self.registry = registry
        self.turn_id = None
        self.token = None
        self.server = None
        self.temporary = None
        self.clients = set()

    async def start(self):
        if self.server is not None:
            raise RuntimeError("Bridge already started")
        # Short path respects macOS Unix socket limits. Directory is mode 0700.
        self.temporary = tempfile.TemporaryDirectory(prefix="arms-tools-", dir="/private/tmp")
        self.path = Path(self.temporary.name) / "rpc.sock"
        self.auth_path = Path(self.temporary.name) / "auth.json"
        self.server = await asyncio.start_unix_server(self._client, path=self.path, limit=self.MAX_REQUEST + 1)
        return self

    def bind(self, turn_id):
        """Supervisor-only; token rotates each turn, including same-session turns."""
        with self.registry.connect(readonly=True) as db:
            row, _ = self.registry._turn_scope(db, turn_id)
            if row["status"] != "RUNNING":
                raise Unavailable()
        self.turn_id = turn_id
        self.token = secrets.token_hex(32)
        auth = {"socket": str(self.path), "token": self.token}
        stage = self.auth_path.with_suffix(".tmp")
        descriptor = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(auth, stream)
        os.replace(stage, self.auth_path)
        return auth

    def unbind(self):
        self.turn_id = self.token = None

    async def _client(self, reader, writer):
        if len(self.clients) >= 8:
            writer.close()
            return
        self.clients.add(writer)
        response = {"ok": False, "error": "Research resource unavailable"}
        try:
            frame = await asyncio.wait_for(reader.readline(), 10)
            if not frame.endswith(b"\n") or len(frame) > self.MAX_REQUEST:
                raise ValueError("Invalid frame")
            request = json.loads(frame)
            if not isinstance(request, dict) or set(request) != {"token", "tool", "space_id", "arguments"}:
                raise ValueError("Invalid request")
            token, turn = self.token, self.turn_id
            if not token or not isinstance(request["token"], str) or not secrets.compare_digest(token, request["token"]):
                raise Unavailable()
            operation = self.OPERATIONS.get(request["tool"])
            if operation is None or not isinstance(request["space_id"], str):
                raise Unavailable()
            args = request["arguments"]
            if not isinstance(args, dict):
                raise ValueError("Invalid arguments")
            key = "query" if operation == "recall" else "id"
            if key not in args or set(args) - ({key, "limit", "kinds"} if key == "query" else {key}):
                raise ValueError("Invalid arguments")
            result = await asyncio.wait_for(asyncio.to_thread(self.registry.read, turn, request["space_id"],
                operation, args[key], **{k: v for k, v in args.items() if k != key}), 20)
            if token != self.token or turn != self.turn_id:
                raise Unavailable()
            response = {"ok": True, "data": result}
        except Exception:
            # No titles, counts, exception text, paths or key material on failure.
            pass
        try:
            payload = json.dumps(response, ensure_ascii=False).encode() + b"\n"
            if len(payload) > 66_000:
                payload = b'{"ok":false,"error":"Research result exceeds limit"}\n'
            writer.write(payload)
            await asyncio.wait_for(writer.drain(), 5)
        except (ConnectionError, asyncio.TimeoutError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            finally:
                self.clients.discard(writer)

    async def close(self):
        self.unbind()
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        for writer in list(self.clients):
            writer.close()
        if self.temporary:
            self.temporary.cleanup()
            self.temporary = None
