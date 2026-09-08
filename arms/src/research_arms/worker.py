"""Authorized queue-to-Pi execution; transport delivery remains a separate step.

Use one supervisor per registry. Restart recovery must verify that old workers
have stopped before clearing/quarantining existing RUNNING claims.
"""
import asyncio
from dataclasses import asdict
import hashlib
import fcntl
import json
import os
import time

from .pi_rpc import PiRPC, PiProtocolError
from .registry import Unavailable
from .tool_bridge import ToolBridge


def final_answer(result):
    """Select a completed assistant response from this dispatch, not old history."""
    entries = result.get("entries", [])
    assistants = [entry for entry in entries if entry.get("type") == "message"
                  and entry.get("message", {}).get("role") == "assistant"]
    if not assistants:
        raise PiProtocolError("No completed assistant answer")
    entry = assistants[-1]
    message = entry["message"]
    if message.get("stopReason") != "stop":
        raise PiProtocolError("Assistant response did not finish normally")
    content = message.get("content", [])
    if any(part.get("type") == "toolCall" for part in content):
        raise PiProtocolError("Unresolved tool call")
    answer = "\n".join(part["text"] for part in content if part.get("type") == "text")
    return answer, entry["id"]


class Supervisor:
    def __init__(self, registry, executable, *, agent_directory=None, client_factory=PiRPC.start,
                 bridge_factory=ToolBridge, idle_seconds=600, clock=time.monotonic, authorize_turn=None):
        self.registry, self.executable = registry, executable
        self.agent_directory = agent_directory
        self.client_factory, self.bridge_factory = client_factory, bridge_factory
        self.idle_seconds, self.clock = idle_seconds, clock
        self.authorize_turn = authorize_turn
        self.workers = {}
        self.lock = asyncio.Lock()
        self.closed = False
        self.ownership = os.open(registry.root / "worker.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.ownership, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with registry.connect(readonly=True) as db:
                if db.execute("SELECT 1 FROM turns WHERE status='RUNNING' LIMIT 1").fetchone():
                    raise RuntimeError("Existing running claims require verified worker recovery")
        except BaseException:
            os.close(self.ownership)
            self.ownership = None
            raise

    async def _retire(self, conversation):
        worker = self.workers.pop(conversation, None)
        if worker:
            worker["bridge"].unbind()
            try:
                await worker["client"].close()
            finally:
                await worker["bridge"].close()

    async def maintain(self):
        """Call periodically even with no new questions; closes revoked/idle Pi."""
        revoked = set(self.registry.revoke_stale())
        async with self.lock:
            for conv, worker in list(self.workers.items()):
                if self.authorize_turn is not None:
                    try:
                        await asyncio.wait_for(self.authorize_turn(worker["turn"]), 30)
                    except Exception:
                        self.registry.quarantine_turn(worker["turn"])
                        await self._retire(conv)
                        continue
                if worker["session"] in revoked or (not worker["busy"] and self.clock() - worker["last"] >= self.idle_seconds):
                    await self._retire(conv)

    async def run_once(self):
        if self.closed:
            raise RuntimeError("Supervisor closed")
        await self.maintain()
        async with self.lock:
            turn_id = self.registry.claim()
            if turn_id is None:
                return None
            try:
                if self.authorize_turn is not None:
                    await asyncio.wait_for(self.authorize_turn(turn_id), 30)
                with self.registry.connect(readonly=True) as db:
                    turn, scope = self.registry._turn_scope(db, turn_id)
                    conv = db.execute("SELECT * FROM conversations WHERE id=?", (turn["conversation_id"],)).fetchone()
                    anchor = db.execute("SELECT answer FROM turns WHERE id=?", (turn["anchor_turn_id"],)).fetchone() if turn["anchor_turn_id"] else None
                    previous = db.execute("SELECT pi_entry_id FROM turns WHERE conversation_id=? AND status='ANSWERED' ORDER BY created_at DESC,id DESC LIMIT 1", (conv["id"],)).fetchone()
                    if previous is None:
                        previous = db.execute("SELECT t.pi_entry_id FROM session_forks f JOIN turns t ON t.id=f.turn_id WHERE f.target_id=? AND f.state='COMPLETE'", (conv["id"],)).fetchone()
                    payload = {"scope": asdict(scope), "author": turn["author"], "question": turn["prompt"]}
                    if anchor:
                        payload["quoted_reply_anchor"] = anchor[0][:4000]
                        payload["anchor_omitted_characters"] = max(0, len(anchor[0]) - 4000)
                conversation = conv["id"]
                if conversation not in self.workers:
                    # Bound the total process count, including idle sessions.
                    if len(self.workers) >= 2:
                        idle = [key for key, value in self.workers.items() if not value["busy"]]
                        if not idle:
                            raise RuntimeError("Worker capacity invariant violated")
                        await self._retire(min(idle, key=lambda key: self.workers[key]["last"]))
                    # Hash only the storage partition name; scope is never selected by a model path.
                    partition = hashlib.sha256(conv["space_id"].encode()).hexdigest()
                    directory = self.registry.root / "sessions" / partition / conversation
                    bridge = await self.bridge_factory(self.registry).start()
                    bridge.authorize_turn = self.authorize_turn
                    try:
                        bridge.bind(turn_id)
                        client = await self.client_factory(self.executable, directory, conv["pi_session_id"],
                            agent_directory=self.agent_directory, tool_auth_file=bridge.auth_path)
                    except BaseException:
                        await bridge.close()
                        raise
                    self.workers[conversation] = {"client": client, "bridge": bridge,
                        "session": conv["pi_session_id"], "busy": False, "last": self.clock()}
                worker = self.workers[conversation]
                worker["busy"] = True
                worker["turn"] = turn_id
                worker["bridge"].bind(turn_id)
            except BaseException:
                self.registry.quarantine_turn(turn_id)
                if "conversation" in locals():
                    await self._retire(conversation)
                raise
        try:
            # Cursor precedes dispatch; only newly appended entries can become this answer.
            state = await worker["client"].command("get_entries")
            if previous and previous[0] not in {entry.get("id") for entry in state.get("entries", [])}:
                raise PiProtocolError("Prior answer missing from Pi session; explicit recovery required")
            if self.authorize_turn is not None:
                await asyncio.wait_for(self.authorize_turn(turn_id), 30)
            self.registry.dispatch_turn(turn_id)
            result = await worker["client"].prompt(json.dumps(payload, ensure_ascii=False), since=state.get("leafId"))
            answer, entry_id = final_answer(result)
            if self.authorize_turn is not None:
                await asyncio.wait_for(self.authorize_turn(turn_id), 30)
            self.registry.save_answer(turn_id, answer, entry_id)
            return turn_id
        except BaseException:
            self.registry.quarantine_turn(turn_id)
            async with self.lock:
                await self._retire(conversation)
            raise
        finally:
            worker["bridge"].unbind()
            worker["busy"] = False
            worker["last"] = self.clock()

    async def close(self):
        async with self.lock:
            self.closed = True
            for conv in list(self.workers):
                await self._retire(conv)
            if self.ownership is not None:
                os.close(self.ownership)
                self.ownership = None

    async def stop(self, conversation, discord_user, *, channel_id, guild_id=None):
        async with self.lock:
            result = self.registry.stop_conversation(conversation, discord_user,
                                                     channel_id=channel_id, guild_id=guild_id)
            worker = self.workers.get(conversation)
            if worker:
                worker["bridge"].unbind()
                try:
                    # Pi clears its own pending queue before aborting. Closing
                    # below also handles a hung or rejected abort without reuse.
                    await asyncio.wait_for(worker["client"].abort(), 5)
                except Exception:
                    pass
                finally:
                    await self._retire(conversation)
            return result
