"""Durable projection of confirmed bot exchanges into their original Brain space."""
import asyncio
import json

from research_brain.spaces import ContextScope
from .registry import now


class DiscussionWorker:
    def __init__(self, registry, access):
        self.registry, self.access = registry, access
        self.stopping = False

    async def work_once(self):
        if self.stopping: return None
        with self.registry.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM discussion_jobs WHERE state='RUNNING'").fetchone(): return None
            row = db.execute("SELECT * FROM discussion_jobs WHERE state='QUEUED' ORDER BY created_at,turn_id LIMIT 1").fetchone()
            if row is None: return None
            row = dict(row)
            db.execute("UPDATE discussion_jobs SET state='RUNNING' WHERE id=?", (row["id"],))
        try:
            data = json.loads(row["scope_json"])
            data["read_spaces"] = tuple(data["read_spaces"])
            scope = ContextScope(**data)
            payload = json.loads(row["payload_json"])
            retract = payload["deleted"] or payload.get("question_unavailable") or payload.get("answer_unavailable")
            if not retract: await self.access.authorize_turn(row["turn_id"])
            def project():
                if self.stopping: raise RuntimeError("Discussion worker stopping")
                if retract:
                    # Authenticated exact-message retractions only remove search
                    # visibility. Do not require a deleted author's membership
                    # to remain active to honor the tombstone in its old space.
                    return self.registry.spaces.open(scope.writable_space).record_discussion(payload)
                return self.registry.spaces.record_discussion(scope, payload)
            await asyncio.to_thread(project)
            with self.registry.connect() as db:
                db.execute("UPDATE discussion_jobs SET state='COMPLETE' WHERE id=?", (row["id"],))
                db.execute("INSERT INTO events(kind,subject,at) VALUES ('discussion_projected',?,?)", (row["turn_id"], now()))
            return row["turn_id"]
        except BaseException:
            with self.registry.connect() as db:
                db.execute("UPDATE discussion_jobs SET state='NEEDS_ATTENTION' WHERE id=?", (row["id"],))
                db.execute("INSERT INTO events(kind,subject,at) VALUES ('discussion_uncertain',?,?)", (row["turn_id"], now()))
            raise
