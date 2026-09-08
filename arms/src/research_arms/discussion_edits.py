"""Verified tracked-message edits; never modifies or replays a Pi turn."""
from datetime import datetime, timezone
import json

from .discord_io import assemble_question, download_chunks, read_answer_message
from .registry import Unavailable, encode, now, snowflake


def timestamp(value):
    if not isinstance(value, str) or len(value) > 50: raise ValueError("Invalid edit timestamp")
    result = datetime.fromisoformat(value)
    if result.tzinfo is None: raise ValueError("Edit timestamp needs timezone")
    return result.astimezone(timezone.utc).isoformat(timespec="microseconds")


def targets(registry, guild, channel, message):
    with registry.connect(readonly=True) as db:
        return [dict(row) for row in db.execute(
            "SELECT DISTINCT t.id,t.scope_json,p.discord_user,c.space_id,CASE WHEN t.question_channel_id=? AND t.discord_message_id=? THEN 'question' ELSE 'answer' END AS field FROM turns t JOIN conversations c ON c.id=t.conversation_id JOIN principals p ON p.principal=t.author LEFT JOIN outbox o ON o.turn_id=t.id WHERE c.guild_id IS ? AND ((t.question_channel_id=? AND t.discord_message_id=?) OR (c.channel_id=? AND o.discord_message_id=?))",
            (channel, message, guild, channel, message, channel, message))]


def revise(registry, target, edited_at, content=None):
    edited_at = timestamp(edited_at)
    field = target["field"]
    if field not in {"question", "answer"}: raise ValueError("Invalid edit field")
    if content is not None and (not isinstance(content, str) or not content.strip() or len(content) > (20000 if field == "question" else 200000) or "\x00" in content):
        raise ValueError("Edited content exceeds bounds")
    with registry.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("SELECT * FROM discussion_edits WHERE turn_id=? AND field=? ORDER BY edited_at DESC,verified DESC LIMIT 1", (target["id"], field)).fetchone()
        if old and (edited_at < old["edited_at"] or edited_at == old["edited_at"] and (content is None or old["verified"])):
            return False
        db.execute("INSERT INTO discussion_edits VALUES (?,?,?,?,?)", (target["id"], field, edited_at, content, int(content is not None)))
        row = db.execute("SELECT * FROM discussion_jobs WHERE turn_id=? ORDER BY CAST(json_extract(payload_json,'$.revision') AS INTEGER) DESC LIMIT 1", (target["id"],)).fetchone()
        if row is None: return True  # Confirmation atomically reads this immutable edit ledger.
        payload = json.loads(row["payload_json"])
        if payload["deleted"]: return False
        previous = payload.get(field + "_edited_at")
        if previous and (edited_at < previous or edited_at == previous and (content is None or not payload.get(field + "_unavailable"))): return False
        payload.update(revision=payload["revision"] + 1, recorded_at=now())
        payload[field + "_edited_at"] = edited_at
        payload[field + "_unavailable"] = content is None
        if content is not None: payload[field] = content
        ident = target["id"] + ":" + str(payload["revision"])
        db.execute("INSERT INTO discussion_jobs VALUES (?,?,?,?,'QUEUED',?)",
            (ident, target["id"], row["scope_json"], encode(payload), now()))
        db.execute("INSERT INTO events(kind,subject,at) VALUES (?,?,?)",
            ("discussion_edit_verified" if content is not None else "discussion_edit_pending", ident, now()))
        return True


async def observe(client, *, guild, channel, message, edited_at):
    snowflake(channel), snowflake(message)
    if guild is not None: snowflake(guild)
    edited_at = timestamp(edited_at)
    selected = targets(client.registry, guild, channel, message)
    if not selected: return  # Never fetch or retain casual untracked messages.
    for target in selected: revise(client.registry, target, edited_at)
    # Pending edits are removed from search until complete content is verified.
    # Failed/oversized reads leave that explicit unavailable revision in place.
    for target in selected:
        await client.access.authorize(target["discord_user"], channel_id=channel, guild_id=guild, expected_space=target["space_id"])
    response = await client.rest.get(f"channels/{channel}/messages/{message}")
    if response.status_code != 200: raise Unavailable()
    data = response.json()
    if data.get("id") != message or data.get("channel_id") != channel or data.get("webhook_id"): raise Unavailable()
    latest = timestamp(data.get("edited_timestamp"))
    if latest < edited_at: raise Unavailable()
    for target in selected:
        expected = target["discord_user"] if target["field"] == "question" else str(client.user.id)
        if data.get("author", {}).get("id") != expected: raise Unavailable()
        if target["field"] == "question":
            content = data.get("content")
            if not isinstance(content, str): raise Unavailable()
            content = content.replace(f"<@{client.user.id}>", "").replace(f"<@!{client.user.id}>", "").strip()
            content = await assemble_question(content, data.get("attachments", []), download_chunks)
        else:
            content = await read_answer_message(data, download_chunks)
        await client.access.authorize(target["discord_user"], channel_id=channel, guild_id=guild, expected_space=target["space_id"])
        revise(client.registry, target, latest, content)
