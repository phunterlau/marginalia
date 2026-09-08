"""Checkpointed Discord starter + thread creation for a pinned shared paper."""
import hashlib
import re

from .absorption import ScopedAbsorption
from .registry import Unavailable, encode, now, snowflake


class PaperThreads:
    def __init__(self, registry, access, client, bot_user_id, *, service_factory=ScopedAbsorption):
        self.registry, self.access, self.client = registry, access, client
        self.bot_user_id, self.service_factory = snowflake(bot_user_id), service_factory

    def _state(self, ident, expected, state, **fields):
        if set(fields) - {"starter_id", "thread_id", "conversation_id"}: raise ValueError("Invalid checkpoint")
        with self.registry.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            updates = ",".join(["state=?"] + [key + "=?" for key in fields])
            count = db.execute(f"UPDATE paper_threads SET {updates} WHERE id=? AND state=?",
                (state, *fields.values(), ident, expected)).rowcount
            if count != 1: raise Unavailable()
            db.execute("INSERT INTO events(kind,subject,at) VALUES (?, ?, ?)", ("paper_thread_" + state.lower(), ident, now()))

    async def ensure(self, actor, guild, parent, space, job_id):
        snowflake(actor), snowflake(guild), snowflake(parent)
        await self.access.authorize(actor, channel_id=parent, guild_id=guild, expected_space=space, require_thread_creation=True)
        service = self.service_factory(self.registry, actor, space)
        job = service.jobs.show(job_id)
        document, revision = job["plan"]["document_id"], job["plan"]["source"]["version_label"]
        if not isinstance(revision, str) or not re.fullmatch(r"v[1-9][0-9]*", revision): raise Unavailable()
        metadata = service.jobs.brain.get_document(document)
        if metadata is None: raise Unavailable()
        title = " ".join(metadata["title"].split())[:300]
        ident = "paper_thread_" + hashlib.sha256(encode([space, guild, parent, document, revision]).encode()).hexdigest()[:32]
        with self.registry.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO paper_threads VALUES (?,?,?,?,?,?,'READY',NULL,NULL,NULL,?)",
                       (ident, space, guild, parent, document, revision, now()))
            row = dict(db.execute("SELECT * FROM paper_threads WHERE id=?", (ident,)).fetchone())
        if row["state"] == "COMPLETE":
            await self.access.authorize(actor, channel_id=row["thread_id"], guild_id=guild, expected_space=space)
            return row
        if row["state"] != "READY":
            raise ValueError("Paper thread requires reconciliation; no automatic resend")
        self._state(ident, "READY", "MESSAGE_SENDING")
        try:
            nonce = str(int(hashlib.sha256(ident.encode()).hexdigest()[:16], 16))
            response = await self.client.post(f"channels/{parent}/messages", json={
                "content": f"Shared: {space}\nPaper: {title} ({revision})\nDocument: {document}\nSource is available. Card extraction/review status must be checked separately.",
                "allowed_mentions": {"parse": [], "replied_user": False}, "flags": 4100,
                "nonce": nonce, "enforce_nonce": True})
            if response.status_code != 200: raise Unavailable()
            message = response.json()
            if message.get("channel_id") != parent or message.get("author", {}).get("id") != self.bot_user_id or str(message.get("nonce")) != nonce:
                raise Unavailable()
            starter = snowflake(message["id"])
            self._state(ident, "MESSAGE_SENDING", "MESSAGE_READY", starter_id=starter)
            await self.access.authorize(actor, channel_id=parent, guild_id=guild, expected_space=space, require_thread_creation=True)
            self._state(ident, "MESSAGE_READY", "THREAD_SENDING")
            response = await self.client.post(f"channels/{parent}/messages/{starter}/threads",
                json={"name": f"{title[:75]} {revision}".replace("@", ""), "auto_archive_duration": 1440})
            if response.status_code not in (200, 201): raise Unavailable()
            thread = response.json()
            if thread.get("id") != starter or thread.get("parent_id") != parent or thread.get("guild_id") != guild or thread.get("type") not in (10, 11):
                raise Unavailable()
            self._state(ident, "THREAD_SENDING", "THREAD_READY", thread_id=starter)
            await self.access.authorize(actor, channel_id=starter, guild_id=guild, expected_space=space)
            conversation = self.registry.new_conversation(actor, channel_id=starter, guild_id=guild,
                parent_channel_id=parent, name=f"Paper {revision}", request_id=starter)
            self.registry.select_conversation(conversation, actor, channel_id=starter, guild_id=guild)
            self._state(ident, "THREAD_READY", "COMPLETE", conversation_id=conversation)
            return {"id": ident, "thread_id": starter, "conversation_id": conversation, "state": "COMPLETE"}
        except BaseException:
            with self.registry.connect() as db:
                db.execute("UPDATE paper_threads SET state='NEEDS_ATTENTION' WHERE id=? AND state!='COMPLETE'", (ident,))
                db.execute("INSERT INTO events(kind,subject,at) VALUES ('paper_thread_uncertain',?,?)", (ident, now()))
            raise
