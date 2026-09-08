"""Durable same-audience fork coordination, owned by the single supervisor."""
import hashlib
import re
import uuid

from .registry import Unavailable, encode, now, visibility
from .session_fork import fork_completed_session, verify_completed_fork


async def recover_fork(supervisor, *, request_id, actor, channel_id, guild_id=None, authorize=None):
    """Adopt one verified existing candidate, without creating or rewriting files."""
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", request_id):
        raise ValueError("Invalid fork request ID")
    registry = supervisor.registry
    async with supervisor.lock:
        if supervisor.closed: raise Unavailable()
        if authorize is not None: await authorize()
        with registry.connect(readonly=True) as db:
            job = db.execute("SELECT * FROM session_forks WHERE request_id=? AND actor=?", (request_id, actor)).fetchone()
            if job is None or job["state"] not in {"COMPLETE", "NEEDS_ATTENTION"}: raise Unavailable()
            job = dict(job)
            source, _ = registry._authorized(db, job["source_id"], actor, channel_id, guild_id,
                statuses=("OPEN", "STOPPED", "NEEDS_ATTENTION"))
            registry._authorized(db, job["target_id"], actor, channel_id, guild_id,
                statuses=("OPEN",) if job["state"] == "COMPLETE" else ("NEEDS_ATTENTION",))
            if job["state"] == "COMPLETE": return job["target_id"]
            if db.execute("SELECT 1 FROM turns WHERE conversation_id IN (?,?) AND status='RUNNING'", (job["source_id"], job["target_id"])).fetchone():
                raise ValueError("Stop active work before fork recovery")
            entry = db.execute("SELECT pi_entry_id FROM turns WHERE id=? AND conversation_id=? AND status='ANSWERED'", (job["turn_id"], job["source_id"])).fetchone()
            if entry is None: raise Unavailable()
        await supervisor._retire(job["source_id"])
        await supervisor._retire(job["target_id"])
        base = registry.root / "sessions" / hashlib.sha256(source["space_id"].encode()).hexdigest()
        source_dir, target_dir = base / job["source_id"], base / job["target_id"]
        if any(path.resolve() != path for path in (base, source_dir, target_dir)):
            raise ValueError("Unsafe session partition")
        sources = list(source_dir.glob("*_" + source["pi_session_id"] + ".jsonl"))
        candidates = list(target_dir.glob("*.jsonl"))
        if len(sources) != 1 or len(candidates) != 1: raise ValueError("Exactly one source and candidate session required")
        result = verify_completed_fork(source=sources[0], candidate=candidates[0],
            session_id=source["pi_session_id"], entry_id=entry[0])
        if not candidates[0].name.endswith("_" + result["session_id"] + ".jsonl"):
            raise ValueError("Candidate session filename differs")
        if authorize is not None: await authorize()
        with registry.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            registry._authorized(db, job["source_id"], actor, channel_id, guild_id,
                statuses=("OPEN", "STOPPED", "NEEDS_ATTENTION"))
            registry._authorized(db, job["target_id"], actor, channel_id, guild_id, statuses=("NEEDS_ATTENTION",))
            if db.execute("SELECT state FROM session_forks WHERE request_id=?", (request_id,)).fetchone()[0] != "NEEDS_ATTENTION":
                raise Unavailable()
            db.execute("UPDATE conversations SET pi_session_id=?,status='OPEN' WHERE id=?", (result["session_id"], job["target_id"]))
            db.execute("UPDATE session_forks SET state='COMPLETE' WHERE request_id=?", (request_id,))
            db.execute("INSERT INTO events(kind,subject,at) VALUES ('fork_recovered',?,?)", (job["target_id"], now()))
        return job["target_id"]


def prepare(registry, source_id, turn_id, actor, *, channel_id, guild_id, request_id, name):
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", request_id):
        raise ValueError("Invalid fork request ID")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
        raise ValueError("Invalid conversation name")
    with registry.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        source, scope = registry._authorized(db, source_id, actor, channel_id, guild_id,
            statuses=("OPEN", "STOPPED", "NEEDS_ATTENTION"))
        old = db.execute("SELECT * FROM session_forks WHERE request_id=?", (request_id,)).fetchone()
        if old:
            if (old["source_id"], old["turn_id"], old["actor"]) != (source_id, turn_id, actor):
                raise Unavailable()
            prior_name = db.execute("SELECT name FROM conversations WHERE id=?", (old["target_id"],)).fetchone()[0]
            if prior_name != name.strip():
                raise ValueError("Duplicate fork request changed")
            return dict(old), False
        turn = db.execute("SELECT * FROM turns WHERE id=? AND conversation_id=? AND status='ANSWERED'", (turn_id, source_id)).fetchone()
        if turn is None or db.execute("SELECT 1 FROM turns WHERE conversation_id=? AND status='RUNNING'", (source_id,)).fetchone():
            raise Unavailable()
        target_id = "conv_" + uuid.uuid4().hex
        target_scope = registry.spaces.scope(scope.principal, conversation_id=target_id,
            writable_space=scope.writable_space, read_spaces=scope.read_spaces)
        db.execute("INSERT INTO conversations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            target_id, scope.principal, guild_id, channel_id, scope.writable_space,
            scope.audience, encode(scope.read_spaces), scope.policy_version, visibility(target_scope),
            str(uuid.uuid4()), name.strip(), "FORKING", now()))
        db.execute("INSERT INTO session_forks VALUES (?,?,?,?,?,'PREPARED',?)",
            (request_id, source_id, target_id, turn_id, actor, now()))
        db.execute("INSERT INTO events(kind,subject,at) VALUES ('fork_prepared',?,?)", (target_id, now()))
        return dict(db.execute("SELECT * FROM session_forks WHERE request_id=?", (request_id,)).fetchone()), True


async def fork_conversation(supervisor, *, source_id, turn_id, actor, channel_id,
                            request_id, name="Research fork", guild_id=None,
                            node, sdk_module, fork_factory=fork_completed_session,
                            authorize=None):
    """No destination space parameter: copying private history to shared is forbidden.

    Pending/failed requests require inspection, never automatic filesystem replay.
    Successful duplicate requests return the existing authorized destination.
    """
    registry = supervisor.registry
    async with supervisor.lock:
        if supervisor.closed:
            raise RuntimeError("Supervisor closed")
        if authorize is not None:
            await authorize()
        job, fresh = prepare(registry, source_id, turn_id, actor, channel_id=channel_id,
                             guild_id=guild_id, request_id=request_id, name=name)
        if not fresh:
            if job["state"] != "COMPLETE":
                raise ValueError("Fork requires local reconciliation")
            with registry.connect(readonly=True) as db:
                registry._authorized(db, job["target_id"], actor, channel_id, guild_id)
            return job["target_id"]
        try:
            await supervisor._retire(source_id)
            with registry.connect(readonly=True) as db:
                source, _ = registry._authorized(db, source_id, actor, channel_id, guild_id,
                    statuses=("OPEN", "STOPPED", "NEEDS_ATTENTION"))
                entry = db.execute("SELECT pi_entry_id FROM turns WHERE id=?", (turn_id,)).fetchone()[0]
            partition = hashlib.sha256(source["space_id"].encode()).hexdigest()
            base = registry.root / "sessions" / partition
            source_dir = base / source_id
            files = list(source_dir.glob("*_" + source["pi_session_id"] + ".jsonl"))
            if len(files) != 1 or files[0].resolve().parent != source_dir.resolve():
                raise ValueError("Exact source session unavailable")
            result = await fork_factory(node=node, sdk_module=sdk_module, source=files[0],
                destination=base / job["target_id"], session_id=source["pi_session_id"], entry_id=entry)
            if authorize is not None:
                await authorize()
            with registry.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                registry._authorized(db, job["target_id"], actor, channel_id, guild_id, statuses=("FORKING",))
                db.execute("UPDATE conversations SET pi_session_id=?,status='OPEN' WHERE id=?", (result["session_id"], job["target_id"]))
                db.execute("UPDATE session_forks SET state='COMPLETE' WHERE request_id=?", (request_id,))
                db.execute("INSERT INTO events(kind,subject,at) VALUES ('fork_completed',?,?)", (job["target_id"], now()))
            return job["target_id"]
        except BaseException:
            with registry.connect() as db:
                db.execute("UPDATE session_forks SET state='NEEDS_ATTENTION' WHERE request_id=?", (request_id,))
                db.execute("UPDATE conversations SET status='NEEDS_ATTENTION' WHERE id=?", (job["target_id"],))
                db.execute("INSERT INTO events(kind,subject,at) VALUES ('fork_uncertain',?,?)", (job["target_id"], now()))
            raise
