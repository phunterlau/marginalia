"""Durable conversation bindings with fail-closed, per-turn Brain scopes.

Administrative methods are trusted local operations, not remote endpoints.
The future transport must authenticate Discord events before using this API.
No model conversation summarization or Brain writes live in this module.
"""
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid

from research_brain.spaces import ContextScope, SpaceRegistry


class Unavailable(PermissionError):
    def __init__(self):
        super().__init__("Conversation or resource unavailable")


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def now():
    return datetime.now(timezone.utc).isoformat()


def snowflake(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,20}", value):
        raise ValueError("Invalid Discord identifier")
    return value


def visibility(scope):
    data = asdict(scope)
    # Acting authors differ in shared sessions; visibility never does.
    del data["principal"]
    return hashlib.sha256(encode(data).encode()).hexdigest()


class ArmsRegistry:
    VERSION = 1

    def __init__(self, root, spaces: SpaceRegistry, *, create=False):
        self.root = Path(root).resolve()
        self.spaces = spaces
        if create:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "arms.sqlite3"
        if create and not self.path.exists():
            with self.connect(create=True) as db:
                db.executescript("""
                    CREATE TABLE meta(version INTEGER NOT NULL);
                    INSERT INTO meta VALUES (1);
                    CREATE TABLE principals(discord_user TEXT PRIMARY KEY, principal TEXT UNIQUE NOT NULL,
                                            personal_space TEXT NOT NULL);
                    CREATE TABLE channels(guild_id TEXT NOT NULL, channel_id TEXT NOT NULL,
                                          space_id TEXT NOT NULL, PRIMARY KEY(guild_id,channel_id));
                    CREATE TABLE conversations(
                        id TEXT PRIMARY KEY, creator TEXT NOT NULL, guild_id TEXT, channel_id TEXT NOT NULL,
                        space_id TEXT NOT NULL, audience TEXT NOT NULL, reads_json TEXT NOT NULL,
                        policy_version INTEGER NOT NULL, visibility_digest TEXT NOT NULL,
                        pi_session_id TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'OPEN', created_at TEXT NOT NULL);
                    CREATE TABLE turns(
                        id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
                        discord_message_id TEXT NOT NULL, author TEXT NOT NULL, scope_json TEXT NOT NULL,
                        prompt TEXT NOT NULL, anchor_turn_id TEXT REFERENCES turns(id),
                        status TEXT NOT NULL DEFAULT 'QUEUED', pi_entry_id TEXT, answer TEXT,
                        created_at TEXT NOT NULL, UNIQUE(conversation_id,discord_message_id));
                    CREATE TABLE outbox(
                        id TEXT PRIMARY KEY, turn_id TEXT UNIQUE NOT NULL REFERENCES turns(id),
                        state TEXT NOT NULL DEFAULT 'PENDING', discord_message_id TEXT,
                        created_at TEXT NOT NULL);
                    CREATE TABLE events(id INTEGER PRIMARY KEY, kind TEXT NOT NULL,
                                        subject TEXT NOT NULL, at TEXT NOT NULL);
                """)
        with self.connect(readonly=True) as db:
            if [r[0] for r in db.execute("SELECT version FROM meta")] != [self.VERSION]:
                raise ValueError("Incompatible Arms registry; explicit migration required")

    @contextmanager
    def connect(self, *, readonly=False, create=False):
        db = sqlite3.connect(f"{self.path.as_uri()}?mode={'ro' if readonly else 'rwc' if create else 'rw'}",
                             uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
        if not readonly:
            db.execute("PRAGMA journal_mode=WAL")
        try:
            yield db
            if not readonly:
                db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def register_principal(self, discord_user, principal, personal_space):
        """Trusted local registration. Never accept a caller-supplied principal in chat."""
        snowflake(discord_user)
        space = self.spaces.get(personal_space)
        if space["kind"] != "personal" or space["owner"] != principal:
            raise Unavailable()
        with self.connect() as db:
            db.execute("INSERT INTO principals VALUES (?,?,?)", (discord_user, principal, personal_space))

    def bind_channel(self, guild_id, channel_id, space_id):
        """Trusted local administration: shared channels may never bind personal storage."""
        snowflake(guild_id), snowflake(channel_id)
        if self.spaces.get(space_id)["kind"] != "shared":
            raise Unavailable()
        with self.connect() as db:
            db.execute("INSERT INTO channels VALUES (?,?,?)", (guild_id, channel_id, space_id))

    def _principal(self, db, discord_user):
        snowflake(discord_user)
        row = db.execute("SELECT * FROM principals WHERE discord_user=?", (discord_user,)).fetchone()
        if row is None:
            raise Unavailable()
        return row

    def new_conversation(self, discord_user, *, channel_id, guild_id=None,
                         parent_channel_id=None, name="Research", read_spaces=()):
        snowflake(channel_id)
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
            raise ValueError("Conversation name must be 1..100 characters")
        if len(read_spaces) > 16:
            raise ValueError("Too many attached spaces")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            principal = self._principal(db, discord_user)
            if guild_id is None:
                if parent_channel_id is not None:
                    raise Unavailable()
                space = principal["personal_space"]
            else:
                snowflake(guild_id)
                target = snowflake(parent_channel_id or channel_id)
                binding = db.execute("SELECT space_id FROM channels WHERE guild_id=? AND channel_id=?",
                                     (guild_id, target)).fetchone()
                if binding is None:
                    raise Unavailable()
                space = binding[0]
            conversation = "conv_" + uuid.uuid4().hex
            try:
                scope = self.spaces.scope(principal["principal"], conversation_id=conversation,
                                          writable_space=space, read_spaces=tuple(read_spaces))
            except (PermissionError, LookupError, ValueError):
                raise Unavailable() from None
            db.execute("INSERT INTO conversations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (conversation, principal["principal"], guild_id, channel_id, space, scope.audience,
                        encode(scope.read_spaces), scope.policy_version, visibility(scope), str(uuid.uuid4()),
                        name.strip(), "OPEN", now()))
            return conversation

    def _authorized(self, db, conversation_id, discord_user, channel_id, guild_id):
        principal = self._principal(db, discord_user)
        row = db.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone()
        if row is None or row["status"] != "OPEN" or row["channel_id"] != channel_id or row["guild_id"] != guild_id:
            raise Unavailable()
        try:
            scope = self.spaces.scope(principal["principal"], conversation_id=conversation_id,
                                      writable_space=row["space_id"], read_spaces=tuple(json.loads(row["reads_json"])))
            if visibility(scope) != row["visibility_digest"]:
                raise Unavailable()
        except (PermissionError, LookupError, ValueError):
            raise Unavailable() from None
        return row, scope

    def enqueue(self, conversation_id, discord_user, *, channel_id, guild_id=None,
                message_id, prompt, anchor_turn_id=None):
        """Only authenticated, bot-directed turns should reach this method."""
        snowflake(message_id)
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 20_000:
            raise ValueError("Question must contain 1..20000 characters")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            _, scope = self._authorized(db, conversation_id, discord_user, channel_id, guild_id)
            existing = db.execute("SELECT * FROM turns WHERE conversation_id=? AND discord_message_id=?",
                                  (conversation_id, message_id)).fetchone()
            if existing:
                if existing["author"] != scope.principal or existing["prompt"] != prompt or existing["anchor_turn_id"] != anchor_turn_id:
                    raise ValueError("Duplicate message changed; explicit edit handling required")
                return existing["id"]
            if anchor_turn_id:
                anchor = db.execute("SELECT 1 FROM turns WHERE id=? AND conversation_id=? AND status='ANSWERED'",
                                    (anchor_turn_id, conversation_id)).fetchone()
                if anchor is None:
                    raise Unavailable()
            turn = "turn_" + uuid.uuid4().hex
            db.execute("INSERT INTO turns(id,conversation_id,discord_message_id,author,scope_json,prompt,anchor_turn_id,created_at) VALUES (?,?,?,?,?,?,?,?)",
                       (turn, conversation_id, message_id, scope.principal, encode(asdict(scope)), prompt, anchor_turn_id, now()))
            return turn

    def _turn_scope(self, db, turn_id):
        row = db.execute("SELECT t.*,c.status AS conversation_status,c.visibility_digest FROM turns t JOIN conversations c ON c.id=t.conversation_id WHERE t.id=?", (turn_id,)).fetchone()
        if row is None or row["conversation_status"] != "OPEN":
            raise Unavailable()
        data = json.loads(row["scope_json"])
        data["read_spaces"] = tuple(data["read_spaces"])
        scope = ContextScope(**data)
        try:
            self.spaces.validate(scope)
            if visibility(scope) != row["visibility_digest"]:
                raise Unavailable()
        except (PermissionError, LookupError, ValueError):
            raise Unavailable() from None
        return row, scope

    def claim(self):
        """One active turn per conversation, at most two across this registry."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT COUNT(*) FROM turns WHERE status='RUNNING'").fetchone()[0] >= 2:
                return None
            queued = db.execute("SELECT * FROM turns t WHERE status='QUEUED' AND NOT EXISTS (SELECT 1 FROM turns active WHERE active.conversation_id=t.conversation_id AND active.status='RUNNING') ORDER BY created_at,id").fetchall()
            for row in queued:
                try:
                    self._turn_scope(db, row["id"])
                except Unavailable:
                    db.execute("UPDATE turns SET status='REVOKED' WHERE id=?", (row["id"],))
                    continue
                db.execute("UPDATE turns SET status='RUNNING' WHERE id=?", (row["id"],))
                return row["id"]
        return None

    def save_answer(self, turn_id, answer, pi_entry_id):
        if not isinstance(answer, str) or len(answer.encode()) > 200_000 or not answer.strip():
            raise ValueError("Answer must contain 1..200000 UTF-8 bytes")
        if not isinstance(pi_entry_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", pi_entry_id):
            raise ValueError("Invalid Pi entry ID")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row, _ = self._turn_scope(db, turn_id)
            if row["status"] != "RUNNING":
                raise Unavailable()
            db.execute("UPDATE turns SET answer=?,pi_entry_id=?,status='ANSWERED' WHERE id=?", (answer, pi_entry_id, turn_id))
            db.execute("INSERT INTO outbox(id,turn_id,created_at) VALUES (?,?,?)", ("send_" + uuid.uuid4().hex, turn_id, now()))

    def dispatch_turn(self, turn_id):
        """Durable dispatch boundary; a turn can be dispatched only once."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row, scope = self._turn_scope(db, turn_id)
            if row["status"] != "RUNNING" or db.execute(
                    "SELECT 1 FROM events WHERE kind='pi_dispatched' AND subject=?", (turn_id,)).fetchone():
                raise Unavailable()
            db.execute("INSERT INTO events(kind,subject,at) VALUES ('pi_dispatched',?,?)", (turn_id, now()))
            return scope

    def quarantine_turn(self, turn_id):
        """Preserve uncertain context and prevent later queued turns resuming it."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
            if row is None or row["status"] != "RUNNING":
                return
            db.execute("UPDATE conversations SET status='NEEDS_ATTENTION' WHERE id=? AND status='OPEN'", (row["conversation_id"],))
            db.execute("UPDATE turns SET status='NEEDS_ATTENTION' WHERE conversation_id=? AND status IN ('QUEUED','RUNNING')", (row["conversation_id"],))
            db.execute("INSERT INTO events(kind,subject,at) VALUES ('pi_uncertain',?,?)", (turn_id, now()))

    def read(self, turn_id, space_id, operation, *args, **kwargs):
        """Worker-only capability: no caller-provided scope or principal."""
        if operation not in {"search", "recall", "get_document", "get_evidence", "get_research_object"}:
            raise Unavailable()
        if len(args) != 1 or not isinstance(args[0], str):
            raise ValueError("Exactly one text argument is required")
        if operation in {"search", "recall"}:
            if not args[0].strip() or len(args[0]) > 20_000 or set(kwargs) - {"limit", "kinds"}:
                raise ValueError("Invalid retrieval request")
            if type(kwargs.get("limit", 10)) is not int or not 1 <= kwargs.get("limit", 10) <= 50:
                raise ValueError("Result limit must be 1..50")
            kinds = kwargs.get("kinds")
            if kinds is not None and (not isinstance(kinds, (list, tuple)) or len(kinds) > 20
                                      or any(not isinstance(k, str) or not re.fullmatch(r"[a-z_]{1,50}", k) for k in kinds)):
                raise ValueError("Invalid record kinds")
        elif kwargs or not re.fullmatch(r"(?:doc|block|obj)_[A-Za-z0-9_-]{1,90}", args[0]):
            raise ValueError("Invalid record reference")
        with self.connect(readonly=True) as db:
            row, scope = self._turn_scope(db, turn_id)
            if row["status"] != "RUNNING":
                raise Unavailable()
        try:
            result = self.spaces.read(scope, space_id, operation, *args, **kwargs)
        except (PermissionError, LookupError, ValueError):
            raise Unavailable() from None
        def clean(item):
            if is_dataclass(item):
                item = asdict(item)
            if isinstance(item, dict):
                return {k: clean(v) for k, v in item.items() if k not in {"local_path", "session_path", "root"}}
            if isinstance(item, (list, tuple)):
                return [clean(v) for v in item]
            return item
        result = clean(result)
        if len(encode(result).encode()) > 64_000:
            raise ValueError("Result exceeds bound; use a smaller query or field page")
        with self.connect(readonly=True) as db:
            self._turn_scope(db, turn_id)
        return result

    def revoke_stale(self):
        """Quarantine contaminated scopes and return session IDs for supervisor abort.

        This closes capabilities immediately; it does not itself own or kill Pi.
        Existing transcripts and failed/answered turns retain their original scope.
        """
        sessions = []
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for conversation in db.execute("SELECT * FROM conversations WHERE status='OPEN'").fetchall():
                scope = ContextScope(conversation["creator"], conversation["audience"], conversation["id"],
                                     conversation["space_id"], tuple(json.loads(conversation["reads_json"])),
                                     conversation["policy_version"])
                try:
                    self.spaces.validate(scope)
                except (PermissionError, LookupError, ValueError):
                    sessions.append(conversation["pi_session_id"])
                    db.execute("UPDATE conversations SET status='REVOKED' WHERE id=?", (conversation["id"],))
                    db.execute("UPDATE turns SET status='REVOKED' WHERE conversation_id=? AND status IN ('QUEUED','RUNNING')", (conversation["id"],))
                    db.execute("UPDATE outbox SET state='REVOKED' WHERE state!='DELIVERED' AND turn_id IN (SELECT id FROM turns WHERE conversation_id=?)", (conversation["id"],))
                    db.execute("INSERT INTO events(kind,subject,at) VALUES ('scope_revoked',?,?)", (conversation["id"], now()))
        return sessions

    def begin_delivery(self, turn_id):
        """Claim one pending delivery. The adapter must revalidate at send time.

        SENDING/UNKNOWN are never automatically returned for another send.
        Transport must use this stable nonce and suppress generated mentions.
        """
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            turn, _ = self._turn_scope(db, turn_id)
            if turn["status"] != "ANSWERED":
                raise Unavailable()
            outbox = db.execute("SELECT * FROM outbox WHERE turn_id=?", (turn_id,)).fetchone()
            if outbox is None or outbox["state"] != "PENDING":
                return None
            destination = db.execute("SELECT guild_id,channel_id FROM conversations WHERE id=?", (turn["conversation_id"],)).fetchone()
            db.execute("UPDATE outbox SET state='SENDING' WHERE id=?", (outbox["id"],))
            db.execute("INSERT INTO events(kind,subject,at) VALUES ('delivery_dispatched',?,?)", (outbox["id"], now()))
            return {"delivery_id": outbox["id"], "turn_id": turn_id, "answer": turn["answer"],
                    "guild_id": destination["guild_id"], "channel_id": destination["channel_id"],
                    "nonce": str(int(hashlib.sha256(outbox["id"].encode()).hexdigest()[:16], 16))}

    def validate_delivery(self, delivery_id):
        """Last local authorization check before transport; not a send itself."""
        with self.connect(readonly=True) as db:
            row = db.execute("SELECT * FROM outbox WHERE id=?", (delivery_id,)).fetchone()
            if row is None or row["state"] != "SENDING":
                raise Unavailable()
            self._turn_scope(db, row["turn_id"])

    def delivery_unknown(self, delivery_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE outbox SET state='UNKNOWN' WHERE id=? AND state='SENDING'", (delivery_id,)).rowcount
            if changed:
                db.execute("INSERT INTO events(kind,subject,at) VALUES ('delivery_uncertain',?,?)", (delivery_id, now()))

    def confirm_delivery(self, delivery_id, discord_message_id, *, reconciled=False):
        """Record a confirmed remote outcome, even if access was revoked meanwhile.

        This emits no message and exposes no answer. Reconciliation requires a
        transport-verified remote message, never an assumption that a send failed.
        """
        snowflake(discord_message_id)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM outbox WHERE id=?", (delivery_id,)).fetchone()
            if row is None:
                raise Unavailable()
            if row["state"] == "DELIVERED":
                if row["discord_message_id"] != discord_message_id:
                    raise ValueError("Delivery confirmation conflicts")
                return
            if row["state"] not in {"SENDING", "UNKNOWN", "REVOKED"} or row["state"] != "SENDING" and not reconciled:
                raise ValueError("Explicit reconciliation required")
            db.execute("UPDATE outbox SET state='DELIVERED',discord_message_id=? WHERE id=?", (discord_message_id, delivery_id))
            db.execute("INSERT INTO events(kind,subject,at) VALUES (?, ?, ?)",
                       ("delivery_reconciled" if reconciled else "delivery_confirmed", delivery_id, now()))

    def recover_stopped_workers(self, *, confirmed_stopped=False):
        """Trusted supervisor operation only after verifying old processes stopped."""
        if confirmed_stopped is not True:
            raise ValueError("Verify prior workers are stopped before recovery")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            active = [r[0] for r in db.execute("SELECT DISTINCT conversation_id FROM turns WHERE status='RUNNING'")]
            for conv in active:
                db.execute("UPDATE conversations SET status='NEEDS_ATTENTION' WHERE id=?", (conv,))
                db.execute("UPDATE turns SET status='NEEDS_ATTENTION' WHERE conversation_id=? AND status IN ('QUEUED','RUNNING')", (conv,))
                db.execute("INSERT INTO events(kind,subject,at) VALUES ('worker_interrupted',?,?)", (conv, now()))
            sending = [r[0] for r in db.execute("SELECT id FROM outbox WHERE state='SENDING'")]
            for ident in sending:
                db.execute("UPDATE outbox SET state='UNKNOWN' WHERE id=?", (ident,))
                db.execute("INSERT INTO events(kind,subject,at) VALUES ('delivery_uncertain',?,?)", (ident, now()))
            return {"conversations_quarantined": len(active), "uncertain_deliveries": len(sending)}
