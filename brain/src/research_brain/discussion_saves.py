"""Explicit, revision-checked retention of exact assistant excerpts as unreviewed notes."""
import hashlib
import json
import re

from .ids import stable_id
from .store.sqlite import utc_now


def prepare(db, record_id, revision, start, end):
    if not isinstance(record_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", record_id):
        raise ValueError("Invalid discussion ID")
    if type(revision) is not int or type(start) is not int or type(end) is not int or not 0 <= start < end or end - start > 8000:
        raise ValueError("Select 1..8000 decoded characters with an exact revision")
    row = db.execute("SELECT r.payload_json,r.digest FROM discussion_heads h JOIN discussion_revisions r ON r.record_id=h.record_id AND r.revision=h.revision WHERE h.record_id=? AND h.revision=?", (record_id, revision)).fetchone()
    if row is None: raise ValueError("Discussion revision unavailable or stale")
    source = json.loads(row[0])
    if source["deleted"] or source.get("question_unavailable") or source.get("answer_unavailable") or end > len(source["answer"]):
        raise ValueError("Discussion excerpt unavailable")
    body = source["answer"][start:end]
    if not body.strip(): raise ValueError("Empty excerpt")
    provenance = {"schema": "SavedDiscussionExcerptV1", "discussion_id": record_id,
        "discussion_revision": revision, "discussion_digest": row[1], "character_start": start,
        "character_end": end, "conversation_id": source["conversation_id"], "pi_entry_id": source["pi_entry_id"],
        "question_author": source["author"], "answer_author": "assistant",
        "answer_url": f"https://discord.com/channels/{source['guild_id'] or '@me'}/{source['channel_id']}/{source['answer_message_id']}",
        "answer_edited_at": source.get("answer_edited_at"),
        "notice": "Selected discussion text, not verified paper evidence or an observed experiment."}
    digest = hashlib.sha256(json.dumps({"body": body, "provenance": provenance}, sort_keys=True).encode()).hexdigest()
    return {"digest": digest, "body": body, "provenance": provenance,
        "origin": "AGENT_INTERPRETED", "review_state": "UNREVIEWED"}


def excerpt(store, record_id, *, revision, start, end, expected_digest=None, actor="user"):
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE" if expected_digest is not None else "BEGIN")
        preview = prepare(db, record_id, revision, start, end)
        if expected_digest is None: return preview
        if expected_digest != preview["digest"]: raise ValueError("Excerpt approval digest changed")
        if not isinstance(actor, str) or not 1 <= len(actor) <= 100: raise ValueError("Invalid save actor")
        ident = stable_id("obj", "saved-discussion", expected_digest)
        old = db.execute("SELECT origin,review_state FROM research_objects WHERE id=?", (ident,)).fetchone()
        if old: return {"object_id": ident, "created": False, "origin": old[0], "review_state": old[1]}
        title = "Saved discussion excerpt"
        structured = json.dumps({**preview["provenance"], "save_digest": expected_digest, "saved_by": actor}, sort_keys=True)
        timestamp = utc_now()
        db.execute("INSERT INTO research_objects(id,kind,title,body,structured_json,origin,review_state,confidence,created_at,updated_at) VALUES (?,'note',?,?,?,'AGENT_INTERPRETED','UNREVIEWED',NULL,?,?)",
            (ident, title, preview["body"], structured, timestamp, timestamp))
        db.execute("INSERT INTO object_fts(object_id,kind,title,body,structured) VALUES (?,'note',?,?,?)",
            (ident, title, preview["body"], structured))
        store._append_event(db, "discussion_excerpt_saved", ident, {"digest": expected_digest}, actor)
        return {"object_id": ident, "created": True, "origin": "AGENT_INTERPRETED", "review_state": "UNREVIEWED"}
