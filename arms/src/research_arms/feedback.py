"""Scoped reaction signals; no model dispatch or scientific review mutation."""
from datetime import datetime, timezone
import hashlib
import json

from .registry import encode, now, snowflake, Unavailable

EMOJIS = {"⭐": "bookmark", "🔥": "interest", "🔬": "deep_dive_pending", "❓": "clarification"}


def target(registry, guild, channel, message):
    snowflake(channel), snowflake(message)
    if guild is not None: snowflake(guild)
    with registry.connect(readonly=True) as db:
        rows = db.execute("SELECT c.space_id FROM outbox o JOIN turns t ON t.id=o.turn_id JOIN conversations c ON c.id=t.conversation_id WHERE o.state='DELIVERED' AND o.discord_message_id=? AND c.channel_id=? AND c.guild_id IS ? UNION SELECT space_id FROM paper_threads WHERE starter_id=? AND parent_id=? AND guild_id IS ? AND state='COMPLETE'",
            (message, channel, guild, message, channel, guild)).fetchall()
    return rows[0][0] if len(rows) == 1 else None


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
