"""Scoped reaction signals; no model dispatch or scientific review mutation."""
from datetime import datetime, timezone
import hashlib
import json

from .registry import encode, now, snowflake, Unavailable
from research_brain.spaces import ContextScope

EMOJIS = {"⭐": "bookmark", "🔥": "interest", "🔬": "deep_dive_pending", "❓": "clarification"}


async def observe(client, payload, *, active):
    """Called only for authenticated Gateway events, serialized by the client."""
    if payload.emoji.id is not None or str(payload.emoji) not in EMOJIS: return False
    actor, channel, message = str(payload.user_id), str(payload.channel_id), str(payload.message_id)
    guild = str(payload.guild_id) if payload.guild_id else None
    if client.user is None or actor == str(client.user.id): return False
    if getattr(getattr(payload, "member", None), "bot", False): return False
    space = target(client.registry, guild, channel, message)
    if space is None: return False
    with client.registry.connect(readonly=True) as db:
        principal = client.registry._principal(db, actor)["principal"]
    if active:
        await client.access.authorize(actor, channel_id=channel, guild_id=guild, expected_space=space)
        scope = client.registry.spaces.scope(principal, conversation_id="feedback", writable_space=space)
    else:
        # This token cannot add/read anything; it identifies only an existing
        # actor's withdrawal after permissions may have been revoked.
        scope = ContextScope(principal, "withdrawal-only", "feedback", space, (space,), -1)
    return record(client.registry, scope, guild=guild, channel=channel, message=message,
        emoji=str(payload.emoji), active=active)


def target(registry, guild, channel, message):
    snowflake(channel), snowflake(message)
    if guild is not None: snowflake(guild)
    with registry.connect(readonly=True) as db:
        rows = db.execute("SELECT c.space_id FROM outbox o JOIN turns t ON t.id=o.turn_id JOIN conversations c ON c.id=t.conversation_id WHERE o.state='DELIVERED' AND o.discord_message_id=? AND c.channel_id=? AND c.guild_id IS ? UNION SELECT space_id FROM paper_threads WHERE starter_id=? AND parent_id=? AND guild_id IS ? AND state='COMPLETE'",
            (message, channel, guild, message, channel, guild)).fetchall()
    return rows[0][0] if len(rows) == 1 else None


def clear(registry, *, guild, channel, message, emoji=None):
    """Authenticated raw clear/delete event: retract only already-known signals."""
    snowflake(channel), snowflake(message)
    if guild is not None: snowflake(guild)
    if emoji is not None and emoji not in EMOJIS: return 0
    with registry.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute("SELECT subject FROM events WHERE kind='feedback_signal' AND json_extract(subject,'$.guild_id') IS ? AND json_extract(subject,'$.channel_id')=? AND json_extract(subject,'$.message_id')=? ORDER BY id DESC", (guild, channel, message)).fetchall()
        seen, count = set(), 0
        for row in rows:
            value = json.loads(row[0])
            if value["key"] in seen: continue
            seen.add(value["key"])
            if not value["active"] or (emoji is not None and value["emoji"] != emoji): continue
            value.update(active=False, recorded_at=now(), removal_reason="message_or_reaction_clear")
            db.execute("INSERT INTO events(kind,subject,at) VALUES ('feedback_signal',?,?)", (encode(value), value["recorded_at"]))
            count += 1
        return count


def record(registry, scope, *, guild, channel, message, emoji, active):
    if emoji not in EMOJIS or type(active) is not bool: raise ValueError("Unsupported feedback signal")
    space = target(registry, guild, channel, message)
    if space != scope.writable_space: raise Unavailable()
    key = hashlib.sha256(encode([space, scope.principal, guild, channel, message, emoji]).encode()).hexdigest()
    with registry.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        with registry.spaces.connect() as policy:
            policy.execute("BEGIN IMMEDIATE")
            if active: registry.spaces.validate(scope, space_id=space)
            # Removing a known signal may withdraw it after membership revocation.
            old = db.execute("SELECT subject FROM events WHERE kind='feedback_signal' AND json_extract(subject,'$.key')=? ORDER BY id DESC LIMIT 1", (key,)).fetchone()
            if (old is None and not active) or (old and json.loads(old[0])["active"] == active): return False
            value = {"key": key, "space_id": space, "principal": scope.principal,
                "guild_id": guild, "channel_id": channel, "message_id": message,
                "emoji": emoji, "meaning": EMOJIS[emoji], "active": active, "recorded_at": now()}
            db.execute("INSERT INTO events(kind,subject,at) VALUES ('feedback_signal',?,?)", (encode(value), value["recorded_at"]))
            return True


def view(registry, scope, *, shared=False, limit=20, at=None):
    if type(shared) is not bool or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Invalid feedback view")
    registry.spaces.validate(scope)
    if shared != scope.audience.startswith("shared:"): raise Unavailable()
    at = at or datetime.now(timezone.utc)
    if not isinstance(at, datetime) or at.tzinfo is None: raise ValueError("Timezone-aware time required")
    with registry.connect(readonly=True) as db:
        if shared:
            rows = db.execute("SELECT subject FROM events WHERE kind='feedback_signal' AND json_extract(subject,'$.space_id')=? AND json_extract(subject,'$.emoji')='🔥' ORDER BY id DESC", (scope.writable_space,)).fetchall()
        else:
            placeholders = ",".join("?" for _ in scope.read_spaces)
            rows = db.execute(f"SELECT subject FROM events WHERE kind='feedback_signal' AND json_extract(subject,'$.principal')=? AND json_extract(subject,'$.space_id') IN ({placeholders}) ORDER BY id DESC", (scope.principal, *scope.read_spaces)).fetchall()
    seen, items = set(), []
    for row in rows:
        value = json.loads(row[0])
        if value["key"] in seen: continue
        seen.add(value["key"])
        if not value["active"] or value["space_id"] not in scope.read_spaces: continue
        if shared:
            if value["space_id"] != scope.writable_space or value["emoji"] != "🔥": continue
            try:
                registry.spaces.scope(value["principal"], conversation_id="feedback-check", writable_space=value["space_id"])
            except PermissionError: continue
        elif value["principal"] != scope.principal: continue
        age = max(0, (at - datetime.fromisoformat(value["recorded_at"])).total_seconds())
        items.append({"space_id": value["space_id"], "message_id": value["message_id"], "channel_id": value["channel_id"],
            "guild_id": value["guild_id"], "emoji": value["emoji"], "meaning": value["meaning"],
            "score": 2 ** (-age / (14 * 86400)) if value["emoji"] == "🔥" else 1.0})
    if shared:
        groups = {}
        for item in items:
            key = (item["space_id"], item["guild_id"], item["channel_id"], item["message_id"])
            group = groups.setdefault(key, {**item, "score": 0.0, "contributors": 0})
            group["score"] += item["score"]
            group["contributors"] += 1
        items = list(groups.values())
    items.sort(key=lambda item: (-item["score"], item["space_id"], item["message_id"], item["emoji"]))
    registry.spaces.validate(scope)
    return {"audience": scope.audience, "items": items[:limit], "omitted_items": max(0, len(items) - limit),
        "notice": "Interest signals only, not scientific acceptance. Deep-dive signals are pending requests; no model work is scheduled."}
