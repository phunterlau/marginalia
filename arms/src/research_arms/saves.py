"""Human-selected exact answer excerpts; never whole-session promotion."""
import json

from .registry import Unavailable, snowflake


def save_excerpt(registry, actor, channel, guild, answer_message_id, *, revision, start, end,
                 confirm=False, digest=None):
    snowflake(answer_message_id)
    if type(confirm) is not bool: raise ValueError("Invalid save confirmation")
    if confirm and (not isinstance(digest, str) or len(digest) != 64):
        raise ValueError("Preview the excerpt and confirm its exact digest")
    with registry.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute("SELECT t.id,t.conversation_id FROM outbox o JOIN turns t ON t.id=o.turn_id JOIN conversations c ON c.id=t.conversation_id WHERE o.discord_message_id=? AND o.state='DELIVERED' AND c.channel_id=? AND c.guild_id IS ?", (answer_message_id, channel, guild)).fetchall()
        if len(rows) != 1: raise Unavailable()
        turn = rows[0]
        _, scope = registry._authorized(db, turn["conversation_id"], actor, channel, guild)
        job = db.execute("SELECT state,payload_json FROM discussion_jobs WHERE turn_id=? ORDER BY CAST(json_extract(payload_json,'$.revision') AS INTEGER) DESC LIMIT 1", (turn["id"],)).fetchone()
        if job is None or job["state"] != "COMPLETE" or json.loads(job["payload_json"])["revision"] != revision:
            raise ValueError("Discussion projection unavailable or stale; inspect /discussed")
        args = {"revision": revision, "start": start, "end": end}
        if confirm:
            return registry.spaces.save_discussion_excerpt(scope, turn["id"], **args,
                expected_digest=digest, actor="discord:" + actor)
        return registry.spaces.read(scope, scope.writable_space, "discussion_excerpt", turn["id"], **args)
