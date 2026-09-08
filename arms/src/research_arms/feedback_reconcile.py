"""Complete bounded reaction snapshots; partial reads never withdraw signals."""
import asyncio
import hashlib
import json
from urllib.parse import quote

from .feedback import EMOJIS, target
from .registry import Unavailable, encode, now, snowflake


class FeedbackReconciler:
    """One exact target per pump iteration; reconnects restart idempotently."""
    def __init__(self, client):
        self.client, self.pending, self.phase, self.cursor = client, False, 2, 0
        self.upper = (0, 0)

    def restart(self): self.pending = True

    async def work_once(self):
        registry = self.client.registry
        if self.pending:
            with registry.connect(readonly=True) as db:
                self.upper = (db.execute("SELECT COALESCE(MAX(rowid),0) FROM outbox").fetchone()[0],
                    db.execute("SELECT COALESCE(MAX(rowid),0) FROM paper_threads").fetchone()[0])
            self.pending, self.phase, self.cursor = False, 0, 0
        while self.phase < 2:
            with registry.connect(readonly=True) as db:
                if self.phase == 0:
                    row = db.execute("SELECT o.rowid AS cursor,p.discord_user AS actor,c.guild_id AS guild,c.channel_id AS channel,o.discord_message_id AS message FROM outbox o JOIN turns t ON t.id=o.turn_id JOIN conversations c ON c.id=t.conversation_id JOIN principals p ON p.principal=t.author WHERE o.rowid>? AND o.rowid<=? AND o.state='DELIVERED' ORDER BY o.rowid LIMIT 1", (self.cursor, self.upper[self.phase])).fetchone()
                else:
                    row = db.execute("SELECT t.rowid AS cursor,p.discord_user AS actor,t.guild_id AS guild,t.parent_id AS channel,t.starter_id AS message FROM paper_threads t JOIN conversations c ON c.id=t.conversation_id JOIN principals p ON p.principal=c.creator WHERE t.rowid>? AND t.rowid<=? AND t.state='COMPLETE' ORDER BY t.rowid LIMIT 1", (self.cursor, self.upper[self.phase])).fetchone()
            if row is None:
                self.phase, self.cursor = self.phase + 1, 0
                continue
            args = {k: row[k] for k in ("actor", "guild", "channel", "message")}
            try:
                async with self.client.feedback_lock:
                    result = await reconcile(self.client, **args)
            except Exception:
                result = {"status": "NEEDS_ATTENTION"}
            with registry.connect() as db:
                db.execute("INSERT INTO events(kind,subject,at) VALUES ('feedback_scan_checked',?,?)",
                    (encode({"guild_id": row["guild"], "channel_id": row["channel"], "message_id": row["message"], **result}), now()))
            self.cursor = row["cursor"]
            return result
        return None


async def users(rest, channel, message, emoji, reaction_type):
    found, after = set(), 0
    for _ in range(10):
        route = f"channels/{channel}/messages/{message}/reactions/{quote(emoji, safe='')}?limit=100&type={reaction_type}"
        if after: route += f"&after={after}"
        response = await rest.get(route)
        if response.status_code != 200 or len(response.content) > 262144: raise Unavailable()
        data = response.json()
        if not isinstance(data, list) or len(data) > 100: raise Unavailable()
        ids = []
        for item in data:
            if not isinstance(item, dict) or type(item.get("bot", False)) is not bool: raise Unavailable()
            ident = snowflake(item.get("id"))
            if int(ident) <= after or ident in ids: raise Unavailable()
            ids.append(ident)
            if not item.get("bot", False): found.add(ident)
        if len(data) < 100: return found
        after = max(map(int, ids))
    raise ValueError("Reaction list exceeds reconciliation bound")


async def reconcile(client, *, actor, guild, channel, message):
    """Caller serializes against raw reaction events with feedback_lock."""
    space = target(client.registry, guild, channel, message)
    if space is None: return {"status": "UNTRACKED"}
    async with asyncio.timeout(60):
        await client.access.authorize(actor, channel_id=channel, guild_id=guild, expected_space=space)
        with client.registry.connect(readonly=True) as db:
            principal = client.registry._principal(db, actor)["principal"]
            registered = {row["discord_user"]: row["principal"] for row in db.execute("SELECT * FROM principals")}
        scope = client.registry.spaces.scope(principal, conversation_id="feedback-reconcile", writable_space=space)
        observed = {}
        for emoji in EMOJIS:
            normal = await users(client.rest, channel, message, emoji, 0)
            burst = await users(client.rest, channel, message, emoji, 1)
            observed[emoji] = normal | burst
        eligible = {}
        for user in sorted(set().union(*observed.values()) & registered.keys()):
            if user == str(client.user.id): continue
            await client.access.authorize(user, channel_id=channel, guild_id=guild, expected_space=space)
            eligible[user] = client.registry.spaces.scope(registered[user], conversation_id="feedback-reconcile", writable_space=space)
        await client.access.authorize(actor, channel_id=channel, guild_id=guild, expected_space=space)
        return apply(client.registry, scope, guild, channel, message, observed, eligible)


def apply(registry, scope, guild, channel, message, observed, eligible):
    with registry.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        with registry.spaces.connect() as policy:
            policy.execute("BEGIN IMMEDIATE")
            registry.spaces.validate(scope)
            for participant in eligible.values(): registry.spaces.validate(participant)
            desired = {}
            for emoji, users_in_list in observed.items():
                for user in users_in_list & eligible.keys():
                    principal = eligible[user].principal
                    key = hashlib.sha256(encode([scope.writable_space, principal, guild, channel, message, emoji]).encode()).hexdigest()
                    desired[key] = {"key": key, "space_id": scope.writable_space, "principal": principal,
                        "guild_id": guild, "channel_id": channel, "message_id": message,
                        "emoji": emoji, "meaning": EMOJIS[emoji], "active": True}
            rows = db.execute("SELECT subject FROM events WHERE kind='feedback_signal' AND json_extract(subject,'$.space_id')=? AND json_extract(subject,'$.guild_id') IS ? AND json_extract(subject,'$.channel_id')=? AND json_extract(subject,'$.message_id')=? ORDER BY id DESC", (scope.writable_space, guild, channel, message)).fetchall()
            old = {}
            for row in rows:
                value = json.loads(row[0])
                old.setdefault(value["key"], value)
            count = 0
            for key in sorted(old.keys() | desired.keys()):
                active = key in desired
                if old.get(key, {}).get("active", False) == active: continue
                value = {**(desired[key] if active else old[key]), "active": active,
                    "recorded_at": now(), "reconciled": True}
                db.execute("INSERT INTO events(kind,subject,at) VALUES ('feedback_signal',?,?)", (encode(value), value["recorded_at"]))
                count += 1
            db.execute("INSERT INTO events(kind,subject,at) VALUES ('feedback_reconciled',?,?)",
                (encode({"space_id": scope.writable_space, "channel_id": channel, "message_id": message, "changes": count}), now()))
            return {"status": "COMPLETE", "changes": count}
