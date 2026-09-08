"""Bounded reconnect scan of known messages, never a Discord history download."""
import asyncio

from .discussion_edits import observe
from .registry import encode, now, Unavailable
from .feedback import clear as clear_feedback


class DiscussionReconciler:
    def __init__(self, client):
        self.client = client
        self.cursor = 0
        self.upper = 0
        self.pending = False

    def restart(self):
        # Coalesce reconnects; reset only between complete per-turn checks.
        self.pending = True

    async def work_once(self):
        registry = self.client.registry
        if self.pending:
            with registry.connect(readonly=True) as db:
                self.upper = db.execute("SELECT COALESCE(MAX(rowid),0) FROM turns").fetchone()[0]
            self.cursor, self.pending = 0, False
        with registry.connect(readonly=True) as db:
            row = db.execute("SELECT t.rowid AS scan_id,t.id,t.question_channel_id,t.discord_message_id,c.channel_id,c.guild_id,c.space_id,p.discord_user,o.discord_message_id AS answer_id,EXISTS(SELECT 1 FROM events e WHERE e.subject=t.id AND e.kind='question_message') AS source_message FROM turns t JOIN conversations c ON c.id=t.conversation_id JOIN principals p ON p.principal=t.author LEFT JOIN outbox o ON o.turn_id=t.id AND o.state='DELIVERED' WHERE t.rowid>? AND t.rowid<=? ORDER BY t.rowid LIMIT 1", (self.cursor, self.upper)).fetchone()
        if row is None: return None
        row = dict(row)
        messages = []
        if row["source_message"] and row["question_channel_id"]:
            messages.append((row["question_channel_id"], row["discord_message_id"], row["discord_user"]))
        if row["answer_id"]:
            messages.append((row["channel_id"], row["answer_id"], str(self.client.user.id)))
        outcomes = []
        for channel, message, author in messages:
            try:
                async with asyncio.timeout(60):
                    outcomes.append(await self.check(row, channel, message, author))
            except Exception:
                outcomes.append("NEEDS_ATTENTION")
        with registry.connect() as db:
            db.execute("INSERT INTO events(kind,subject,at) VALUES ('discussion_reconciled',?,?)",
                (encode({"turn_id": row["id"], "outcomes": outcomes}), now()))
        self.cursor = row["scan_id"]
        return row["id"]

    async def check(self, row, channel, message, author):
        async def authorize():
            await self.client.access.authorize(row["discord_user"], channel_id=channel,
                guild_id=row["guild_id"], expected_space=row["space_id"])
        await authorize()
        response = await self.client.rest.get(f"channels/{channel}/messages/{message}")
        if len(response.content) > 262144: raise Unavailable()
        data = response.json()
        if not isinstance(data, dict): raise Unavailable()
        if response.status_code == 404 and data.get("code") == 10008:
            await authorize()
            lock = getattr(self.client, "feedback_lock", None)
            if lock is not None:
                async with lock:
                    clear_feedback(self.client.registry, guild=row["guild_id"], channel=channel, message=message)
            else:
                clear_feedback(self.client.registry, guild=row["guild_id"], channel=channel, message=message)
            self.client.registry.discussion_message_deleted(guild_id=row["guild_id"], channel_id=channel, message_id=message)
            return "DELETED"
        if response.status_code != 200: raise Unavailable()
        if data.get("id") != message or data.get("channel_id") != channel or data.get("webhook_id") or data.get("author", {}).get("id") != author:
            raise Unavailable()
        await authorize()
        if data.get("edited_timestamp"):
            # Re-fetch through the canonical bounded reader; a racing newer edit
            # is handled by its timestamp checks and immutable revision ledger.
            await observe(self.client, guild=row["guild_id"], channel=channel,
                message=message, edited_at=data["edited_timestamp"])
            return "EDIT_CHECKED"
        return "UNCHANGED"
