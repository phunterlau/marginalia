"""Durable source-only submission queue. This module never executes paid work."""
import asyncio
from dataclasses import asdict
import hashlib
import json
from urllib.parse import urlsplit

from research_brain.ingest import arxiv_identity
from research_brain.jobs import SpendingLimits
from research_brain.spaces import ContextScope
from .absorption import ScopedAbsorption
from .registry import Unavailable, encode, now, snowflake


class PaperSubmissions:
    def __init__(self, registry, access, *, service_factory=ScopedAbsorption):
        self.registry, self.access, self.service_factory = registry, access, service_factory

    def enqueue(self, actor, channel, guild, space, request_id, url, *, limits):
        snowflake(request_id), snowflake(actor), snowflake(channel)
        if guild is not None: snowflake(guild)
        if not isinstance(limits, SpendingLimits): raise ValueError("Explicit spending limits required")
        if not isinstance(url, str) or len(url) > 2048: raise ValueError("Invalid arXiv URL")
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname not in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}
                or parsed.username or parsed.password or parsed.port not in (None, 443) or not arxiv_identity(url)):
            raise ValueError("An HTTPS arXiv URL is required")
        ident = "submission_" + hashlib.sha256(encode([actor, channel, guild, request_id]).encode()).hexdigest()[:32]
        with self.registry.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            principal = self.registry._principal(db, actor)["principal"]
            scope = self.registry.spaces.scope(principal, conversation_id=ident, writable_space=space)
            self.registry.spaces.validate(scope, maintainer=True)
            existing = db.execute("SELECT * FROM paper_submissions WHERE id=?", (ident,)).fetchone()
            if existing:
                if existing["url"] != url or existing["limits_json"] != encode(asdict(limits)) or json.loads(existing["scope_json"])["writable_space"] != space:
                    raise ValueError("Duplicate submission changed")
                return ident
            queued = db.execute("SELECT COUNT(*) FROM paper_submissions WHERE actor=? AND state IN ('QUEUED','RUNNING')", (actor,)).fetchone()[0]
            total = db.execute("SELECT COUNT(*) FROM paper_submissions WHERE state IN ('QUEUED','RUNNING')").fetchone()[0]
            if queued >= 10 or total >= 100: raise ValueError("Source queue is full")
            db.execute("INSERT INTO paper_submissions VALUES (?,?,?,?,?,?,?,'QUEUED',NULL,?)",
                (ident, actor, guild, channel, encode(asdict(scope)), url, encode(asdict(limits)), now()))
            db.execute("INSERT INTO events(kind,subject,at) VALUES ('paper_submitted',?,?)", (ident, now()))
        return ident

    def _scope(self, row):
        data = json.loads(row["scope_json"])
        data["read_spaces"] = tuple(data["read_spaces"])
        scope = ContextScope(**data)
        self.registry.spaces.validate(scope, maintainer=True)
        return scope

    def show(self, ident, actor, channel, guild, space):
        with self.registry.connect(readonly=True) as db:
            row = db.execute("SELECT * FROM paper_submissions WHERE id=?", (ident,)).fetchone()
            if row is None or row["channel_id"] != channel or row["guild_id"] != guild:
                raise Unavailable()
            principal = self.registry._principal(db, actor)["principal"]
            scope = self.registry.spaces.scope(principal, conversation_id=ident, writable_space=space)
            self.registry.spaces.validate(scope, maintainer=True)
            if json.loads(row["scope_json"])["writable_space"] != space: raise Unavailable()
            return {"submission_id": ident, "space_id": space, "state": row["state"], "job_id": row["job_id"],
                    "notice": "Source preparation only. Paid work requires a separate exact-plan approval."}

    async def work_once(self):
        with self.registry.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM paper_submissions WHERE state='RUNNING'").fetchone(): return None
            row = db.execute("SELECT * FROM paper_submissions WHERE state='QUEUED' ORDER BY created_at,id LIMIT 1").fetchone()
            if row is None: return None
            row = dict(row)
            db.execute("UPDATE paper_submissions SET state='RUNNING' WHERE id=?", (row["id"],))
            db.execute("INSERT INTO events(kind,subject,at) VALUES ('source_dispatched',?,?)", (row["id"], now()))
        try:
            scope = self._scope(row)
            await self.access.authorize(row["actor"], channel_id=row["channel_id"], guild_id=row["guild_id"], expected_space=scope.writable_space)
            def submit():
                self._scope(row)
                service = self.service_factory(self.registry, row["actor"], scope.writable_space, create=True)
                return service.submit(row["url"], limits=SpendingLimits(**json.loads(row["limits_json"])))
            result = await asyncio.to_thread(submit)
            await self.access.authorize(row["actor"], channel_id=row["channel_id"], guild_id=row["guild_id"], expected_space=scope.writable_space)
            self._scope(row)
            with self.registry.connect() as db:
                db.execute("UPDATE paper_submissions SET state='SOURCE_READY',job_id=? WHERE id=?", (result["job_id"], row["id"]))
                db.execute("INSERT INTO events(kind,subject,at) VALUES ('source_prepared',?,?)", (row["id"], now()))
        except BaseException:
            with self.registry.connect() as db:
                db.execute("UPDATE paper_submissions SET state='NEEDS_ATTENTION' WHERE id=?", (row["id"],))
                db.execute("INSERT INTO events(kind,subject,at) VALUES ('source_uncertain',?,?)", (row["id"], now()))
            raise
        return row["id"]
