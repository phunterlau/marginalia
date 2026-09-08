"""Space-local discussion projection, deliberately separate from scientific recall."""
from datetime import datetime
import hashlib
import json
import re


FIELDS = {"id", "revision", "conversation_id", "author", "question", "answer", "guild_id",
          "channel_id", "message_id", "answer_message_id", "pi_entry_id", "deleted", "recorded_at"}
IDENTITY = ("id", "conversation_id", "author", "guild_id", "channel_id", "message_id", "answer_message_id", "pi_entry_id")
OPTIONAL = {"question_channel_id", "question_edited_at", "answer_edited_at", "question_unavailable", "answer_unavailable"}


def validate(record):
    if not isinstance(record, dict) or set(record) - OPTIONAL != FIELDS: raise ValueError("Invalid discussion record")
    for key in ("question_unavailable", "answer_unavailable"):
        if key in record and type(record[key]) is not bool: raise ValueError("Invalid edit availability")
    for key in ("question_edited_at", "answer_edited_at"):
        if key in record and (not isinstance(record[key], str) or len(record[key]) > 50 or datetime.fromisoformat(record[key]).tzinfo is None):
            raise ValueError("Invalid edit timestamp")
    if record.get("question_channel_id") is not None and (not isinstance(record["question_channel_id"], str) or not re.fullmatch(r"[0-9]{1,20}", record["question_channel_id"])):
        raise ValueError("Invalid question channel")
    for key in ("id", "conversation_id", "author", "pi_entry_id"):
        if not isinstance(record[key], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", record[key]):
            raise ValueError("Invalid discussion identity")
    for key in ("guild_id", "channel_id", "message_id", "answer_message_id"):
        if key == "guild_id" and record[key] is None: continue
        if not isinstance(record[key], str) or not re.fullmatch(r"[0-9]{1,20}", record[key]):
            raise ValueError("Invalid Discord identity")
    if type(record["revision"]) is not int or not 1 <= record["revision"] <= 1_000_000_000 or type(record["deleted"]) is not bool:
        raise ValueError("Invalid discussion revision")
    for key, bound in (("question", 20000), ("answer", 200000)):
        if not isinstance(record[key], str) or len(record[key]) > bound or "\x00" in record[key]:
            raise ValueError("Discussion text exceeds bound")
    if not record["deleted"] and not record["question"].strip(): raise ValueError("Missing question")
    if not isinstance(record["recorded_at"], str) or len(record["recorded_at"]) > 50:
        raise ValueError("Invalid timestamp")
    if datetime.fromisoformat(record["recorded_at"]).tzinfo is None: raise ValueError("Timestamp must include timezone")


def record(store, payload):
    """Append immutable revisions; only the latest non-deleted revision is searchable."""
    validate(payload)
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    if len(encoded.encode()) > 900000: raise ValueError("Discussion payload exceeds byte bound")
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("SELECT digest FROM discussion_revisions WHERE record_id=? AND revision=?",
                         (payload["id"], payload["revision"])).fetchone()
        if old:
            if old[0] != digest: raise ValueError("Discussion revision conflicts with recorded content")
            return {"id": payload["id"], "revision": payload["revision"], "created": False}
        head = db.execute("SELECT r.* FROM discussion_heads h JOIN discussion_revisions r ON r.record_id=h.record_id AND r.revision=h.revision WHERE h.record_id=?", (payload["id"],)).fetchone()
        if head:
            previous = json.loads(head["payload_json"])
            if (any(previous[key] != payload[key] for key in IDENTITY)
                    or previous.get("question_channel_id", previous["channel_id"]) != payload.get("question_channel_id", payload["channel_id"])):
                raise ValueError("Discussion identity changed")
        db.execute("INSERT INTO discussion_revisions VALUES (?,?,?,?,?)",
            (payload["id"], payload["revision"], encoded, digest, payload["recorded_at"]))
        if head is None or payload["revision"] > head["revision"]:
            db.execute("INSERT INTO discussion_heads VALUES (?,?) ON CONFLICT(record_id) DO UPDATE SET revision=excluded.revision",
                (payload["id"], payload["revision"]))
            db.execute("DELETE FROM discussion_fts WHERE record_id=?", (payload["id"],))
            if not payload["deleted"] and not payload.get("question_unavailable") and not payload.get("answer_unavailable"):
                db.execute("INSERT INTO discussion_fts VALUES (?,?,?)", (payload["id"], payload["question"], payload["answer"]))
    return {"id": payload["id"], "revision": payload["revision"], "created": True}


def search(store, query, *, limit=5):
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 2000 or type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("Discussion query must be 1..2000 characters; limit 1..20")
    tokens = re.findall(r"\w+", query, flags=re.UNICODE)
    if not tokens or len(tokens) > 64: raise ValueError("Discussion query must contain 1..64 terms")
    expression = " AND ".join('"' + token.replace('"', '""') + '"' for token in tokens)
    with store.connect() as db:
        rows = db.execute("SELECT r.payload_json,bm25(discussion_fts) AS rank FROM discussion_fts JOIN discussion_heads h ON h.record_id=discussion_fts.record_id JOIN discussion_revisions r ON r.record_id=h.record_id AND r.revision=h.revision WHERE discussion_fts MATCH ? ORDER BY rank,h.record_id LIMIT ?",
                          (expression, limit + 1)).fetchall()
    items = []
    for row in rows[:limit]:
        value = json.loads(row["payload_json"])
        guild = value["guild_id"] or "@me"
        items.append({key: value[key] for key in ("id", "revision", "conversation_id", "author", "recorded_at", "pi_entry_id")}
            | {"question": value["question"][:1000], "answer": value["answer"][:2000],
               "omitted_question_characters": max(0, len(value["question"]) - 1000),
               "omitted_answer_characters": max(0, len(value["answer"]) - 2000),
               "question_url": (f"https://discord.com/channels/{guild}/{value.get('question_channel_id', value['channel_id'])}/{value['message_id']}"
                                if value.get("question_channel_id", value["channel_id"]) else None),
               "answer_url": f"https://discord.com/channels/{guild}/{value['channel_id']}/{value['answer_message_id']}",
               "record_type": "discussion", "scientific_review_state": "NOT_SCIENTIFIC_MEMORY"})
        items[-1]["question_author"] = value["author"]
        items[-1]["answer_author"] = "assistant"
        items[-1]["question_edited_at"] = value.get("question_edited_at")
        items[-1]["answer_edited_at"] = value.get("answer_edited_at")
        items[-1]["edit_notice"] = ("The question changed after submission; the assistant answered an earlier version."
            if value.get("question_edited_at") else "The answer message was edited after delivery."
            if value.get("answer_edited_at") else None)
    return {"items": items, "more_available": len(rows) > limit,
            "notice": "Search covers recorded bot-directed exchanges only. No match does not prove a topic was never discussed. Discussion is not accepted scientific evidence."}
