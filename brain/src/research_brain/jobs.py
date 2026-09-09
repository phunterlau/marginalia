"""Durable, explicitly approved absorption jobs for one registered Brain space.

The operations database is separate from the canonical corpus. Each task is
idempotent in Brain; checkpoints are written only after canonical completion.
No cross-database atomicity is assumed. Unknown outcomes never auto-replay.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Callable, Iterator
from urllib.parse import urlparse
import uuid

from .brain import Brain
from .embeddings import EmbeddingIndexer
from . import embeddings
from . import extraction
from .ingest import arxiv_identity
from .spaces import identifier
from .store.sqlite import utc_now


def encoded(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(encoded(value).encode()).hexdigest()


@dataclass(frozen=True)
class SpendingLimits:
    max_calls: int = 32
    max_reserved_tokens: int = 2_000_000
    max_output_tokens: int = 16_384

    def __post_init__(self):
        if type(self.max_calls) is not int or not 1 <= self.max_calls <= 1000:
            raise ValueError("max_calls must be 1..1000")
        if type(self.max_reserved_tokens) is not int or not 1 <= self.max_reserved_tokens <= 100_000_000:
            raise ValueError("max_reserved_tokens must be 1..100000000")
        if self.max_output_tokens != 16_384:
            raise ValueError("Extraction currently requires the versioned 16384 output cap")


class JobStopped(RuntimeError):
    pass


class BudgetExceeded(JobStopped):
    pass


class AbsorptionJobs:
    def __init__(self, brain: Brain, space_id: str, *, create: bool = False,
                 authorization_context: dict | None = None, authorization_check: Callable | None = None):
        self.brain = brain
        self.space_id = identifier(space_id)
        self.authorization_context = json.loads(json.dumps(authorization_context)) if authorization_context is not None else None
        self.authorization_check = authorization_check
        self.path = brain.root / "jobs.sqlite3"
        if create:
            with self.connect(create=True) as db:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS meta (version INTEGER NOT NULL, space_id TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS jobs (
                        id TEXT PRIMARY KEY, plan_digest TEXT NOT NULL UNIQUE, plan_json TEXT NOT NULL,
                        status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                        attempt INTEGER NOT NULL DEFAULT 0, claimed_by TEXT,
                        cancel_requested INTEGER NOT NULL DEFAULT 0,
                        calls_reserved INTEGER NOT NULL DEFAULT 0, tokens_reserved INTEGER NOT NULL DEFAULT 0,
                        error_json TEXT);
                    CREATE TABLE IF NOT EXISTS steps (
                        job_id TEXT NOT NULL REFERENCES jobs(id), task TEXT NOT NULL, run_id TEXT,
                        result_json TEXT, PRIMARY KEY(job_id,task));
                    CREATE TABLE IF NOT EXISTS events (
                        id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
                        kind TEXT NOT NULL, actor TEXT NOT NULL, payload_json TEXT NOT NULL, at TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS calls (
                        id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
                        task TEXT NOT NULL, attempt INTEGER NOT NULL, request_digest TEXT NOT NULL,
                        reserved_tokens INTEGER NOT NULL, status TEXT NOT NULL,
                        dispatched_at TEXT NOT NULL, completed_at TEXT, response_json TEXT, error_json TEXT);
                """)
                db.execute("BEGIN IMMEDIATE")
                if not db.execute("SELECT 1 FROM meta").fetchone():
                    db.execute("INSERT INTO meta VALUES (1,?)", (space_id,))
        with self.connect() as db:
            rows = db.execute("SELECT * FROM meta").fetchall()
            if len(rows) != 1 or tuple(rows[0]) != (1, space_id):
                raise ValueError("Incompatible or wrong-space job database")

    @contextmanager
    def connect(self, *, create: bool = False) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(f"{self.path.as_uri()}?mode={'rwc' if create else 'rw'}", uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
        if create:
            db.execute("PRAGMA journal_mode=WAL")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _event(self, db, job_id, kind, *, actor="local", payload=None):
        db.execute("INSERT INTO events(job_id,kind,actor,payload_json,at) VALUES (?,?,?,?,?)",
                   (job_id, kind, actor, encoded(payload or {}), utc_now()))

    def plan(self, document_id: str, compilation_id: str, limits: SpendingLimits) -> dict:
        if os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/") != "https://api.openai.com/v1":
            raise ValueError("Absorption supports the direct OpenAI endpoint only")
        blocks = self.brain.store.get_blocks(document_id, compilation_id=compilation_id)
        if not blocks:
            raise LookupError("Compilation unavailable")
        with self.brain.store.connect() as db:
            source = db.execute("SELECT v.version_label,v.resolution_state,a.sha256,a.license_uri,c.parser_version,c.diagnostics_json FROM document_compilations c JOIN document_versions v ON v.id=c.document_version_id JOIN source_assets a ON a.id=v.source_asset_id WHERE c.id=? AND v.document_id=?", (compilation_id, document_id)).fetchone()
        if not source or not source["version_label"] or source["resolution_state"] != "resolved":
            raise ValueError("Paid work requires a resolved, pinned source revision")
        model = os.getenv("RESEARCH_EXTRACT_MODEL", "gpt-5.6-luna")
        effort = os.getenv("RESEARCH_REASONING_EFFORT", "medium")
        tasks = ["methods"] + (["math"] if any(b["block_type"] == "equation" for b in blocks) else [])
        previews = [asdict(self.brain.extractor.extract(task, document_id, compilation_id=compilation_id).plan) for task in tasks]
        source_targets = self.brain.store.embedding_targets(document_id, compilation_id=compilation_id, object_ids=[])
        plan = {
            "version": "absorption-v1", "space_id": self.space_id,
            "document_id": document_id, "compilation_id": compilation_id,
            "source": dict(source), "block_digest": digest(blocks),
            "tasks": tasks + ["embeddings"], "previews": previews,
            "provider": "openai", "extract_model": model, "reasoning_effort": effort,
            "embed_model": os.getenv("RESEARCH_EMBED_MODEL", "text-embedding-3-small"),
            "embedding_version": embeddings.EMBEDDING_VERSION,
            "prompt_version": extraction.PROMPT_VERSION, "store": False,
            "extraction_contract_digest": digest([extraction.METHOD_EXTRACTION_SCHEMA, extraction.MATH_EXTRACTION_SCHEMA,
                                                   extraction.METHOD_INSTRUCTIONS, extraction.MATH_INSTRUCTIONS]),
            "limits": asdict(limits), "source_embedding_records": len(source_targets),
            "embedding_selection": "pinned compilation and cards returned by these extraction tasks",
            "embedding_calls_note": "Source batches plus generated-card batches; every call shares the same hard budget.",
            "quality": {"blocks": len(blocks), "equations": sum(b["block_type"] == "equation" for b in blocks),
                        "unknown_license": source["license_uri"] is None,
                        "parser": source["parser_version"], "diagnostics": json.loads(source["diagnostics_json"])},
        }

        if self.authorization_context is not None:
            plan["authorization_context"] = self.authorization_context
            self._authorize(plan)
        return plan

    def _authorize(self, plan):
        context = plan.get("authorization_context")
        if context is not None:
            if self.authorization_check is None:
                raise JobStopped("Scoped job requires an authorization-aware worker")
            try:
                self.authorization_check(context)
            except Exception:
                raise JobStopped("Job authorization changed; refusing dispatch") from None

    def absorb(self, url: str, *, limits: SpendingLimits | None = None) -> dict:
        identity = arxiv_identity(url)
        if identity is None or urlparse(url).hostname not in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
            raise ValueError("absorb requires an arXiv URL")
        source = self.brain.ingest(url)
        return self.enqueue(source.document_id, source.compilation_id, limits=limits)

    def enqueue(self, document_id: str, compilation_id: str, *, limits: SpendingLimits | None = None) -> dict:
        plan = self.plan(document_id, compilation_id, limits or SpendingLimits())
        plan_digest = digest(plan)
        job_id = "job_" + plan_digest[:32]
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM jobs WHERE plan_digest=?", (plan_digest,)).fetchone():
                now = utc_now()
                db.execute("INSERT INTO jobs(id,plan_digest,plan_json,status,created_at,updated_at) VALUES (?,?,?,'WAITING_APPROVAL',?,?)",
                           (job_id, plan_digest, encoded(plan), now, now))
                for task in plan["tasks"]:
                    db.execute("INSERT INTO steps(job_id,task) VALUES (?,?)", (job_id, task))
                self._event(db, job_id, "source_ready")
        return self.show(job_id)

    def _get(self, db, job_id):
        identifier(job_id)
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise LookupError("Job unavailable")
        return dict(row)

    def show(self, job_id: str) -> dict:
        with self.connect() as db:
            job = self._get(db, job_id)
            job["plan"] = json.loads(job.pop("plan_json"))
            job["steps"] = [dict(r) for r in db.execute("SELECT task,run_id,result_json FROM steps WHERE job_id=? ORDER BY task", (job_id,))]
            job["events"] = [dict(r) for r in db.execute("SELECT kind,actor,payload_json,at FROM events WHERE job_id=? ORDER BY id", (job_id,))]
            job["calls"] = [dict(r) for r in db.execute("SELECT id,task,attempt,request_digest,reserved_tokens,status,dispatched_at,completed_at,error_json FROM calls WHERE job_id=? ORDER BY dispatched_at,id", (job_id,))]
        job["space_id"] = self.space_id
        job["source_ready"] = True
        job["cards_ready_for_review"] = job["status"] == "COMPLETE"
        job["reviewed_memory"] = "Check current card review states; job completion never accepts cards."
        return job

    def list(self, *, limit: int = 50, offset: int = 0) -> list[dict]:
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("Invalid pagination")
        with self.connect() as db:
            return [{"space_id": self.space_id, **dict(r)} for r in db.execute(
                "SELECT id,status,created_at,updated_at,calls_reserved,tokens_reserved FROM jobs ORDER BY created_at,id LIMIT ? OFFSET ?", (limit, offset))]

    def approve(self, job_id: str, plan_digest: str, *, live: bool, actor: str = "local") -> dict:
        if not live:
            raise ValueError("Approval requires --live")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            job = self._get(db, job_id)
            if job["plan_digest"] != plan_digest or digest(json.loads(job["plan_json"])) != plan_digest:
                raise ValueError("Stale or invalid plan digest")
            self._authorize(json.loads(job["plan_json"]))
            if job["status"] != "WAITING_APPROVAL":
                raise ValueError("Job is not waiting for approval")
            db.execute("UPDATE jobs SET status='QUEUED',updated_at=? WHERE id=?", (utc_now(), job_id))
            self._event(db, job_id, "approved", actor=actor, payload={"plan_digest": plan_digest})
        return self.show(job_id)

    def cancel(self, job_id: str) -> dict:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            job = self._get(db, job_id)
            if job["status"] not in {"COMPLETE", "CANCELLED"}:
                db.execute("UPDATE jobs SET cancel_requested=1,status=CASE WHEN status='RUNNING' THEN status ELSE 'CANCELLED' END,updated_at=? WHERE id=?", (utc_now(), job_id))
                self._event(db, job_id, "cancel_requested")
        return self.show(job_id)

    @contextmanager
    def worker_lock(self):
        # The OS lock, not an old PID/file, proves a worker is currently active.
        with (self.brain.root / "worker.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise JobStopped("A worker is active in this space") from exc
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def retry(self, job_id: str, *, acknowledge_uncertain: bool = False) -> dict:
        with self.worker_lock():
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                job = self._get(db, job_id)
                if job["status"] not in {"FAILED", "CANCELLED", "NEEDS_ATTENTION", "RUNNING"}:
                    raise ValueError("Job is not retryable")
                if job["status"] in {"RUNNING", "NEEDS_ATTENTION"} and not acknowledge_uncertain:
                    raise ValueError("Uncertain work requires --ack-uncertain; prior calls may have been charged")
                for row in db.execute("SELECT run_id FROM steps WHERE job_id=? AND result_json IS NULL AND run_id IS NOT NULL", (job_id,)):
                    with self.brain.store.connect() as canonical:
                        canonical.execute("UPDATE generation_runs SET status='failed',error_json=?,completed_at=? WHERE id=? AND status='running'",
                                          (encoded({"reconciliation": "explicit local retry"}), utc_now(), row[0]))
                db.execute("UPDATE jobs SET status='WAITING_APPROVAL',cancel_requested=0,claimed_by=NULL,error_json=NULL,updated_at=? WHERE id=?", (utc_now(), job_id))
                self._event(db, job_id, "retry_requested", payload={"acknowledge_uncertain": acknowledge_uncertain})
                if acknowledge_uncertain:
                    db.execute("UPDATE calls SET status='ACKNOWLEDGED_UNCERTAIN' WHERE job_id=? AND status IN ('DISPATCHED','UNCERTAIN')", (job_id,))
        return self.show(job_id)

    def _dispatch(self, job_id: str, claimant: str, task: str, request: Any, output_cap: int) -> str:
        # Byte-level upper allowance for text tokenization plus fixed protocol
        # overhead. Reservations are never refunded, including failed calls.
        tokens = len(json.dumps(request, ensure_ascii=True).encode()) + 4096 + output_cap
        call_id = "call_" + uuid.uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            job = self._get(db, job_id)
            if job["status"] != "RUNNING" or job["claimed_by"] != claimant or job["cancel_requested"]:
                raise JobStopped("Job cancelled or claim unavailable")
            self._authorize(json.loads(job["plan_json"]))
            limits = json.loads(job["plan_json"])["limits"]
            if job["calls_reserved"] + 1 > limits["max_calls"] or job["tokens_reserved"] + tokens > limits["max_reserved_tokens"]:
                raise BudgetExceeded("Approved call/token ceiling exhausted; no request dispatched")
            db.execute("UPDATE jobs SET calls_reserved=calls_reserved+1,tokens_reserved=tokens_reserved+?,updated_at=? WHERE id=?", (tokens, utc_now(), job_id))
            db.execute("INSERT INTO calls(id,job_id,task,attempt,request_digest,reserved_tokens,status,dispatched_at) VALUES (?,?,?,?,?,?,'DISPATCHED',?)",
                       (call_id, job_id, task, job["attempt"], digest(request), tokens, utc_now()))
        return call_id

    def _call(self, job_id, claimant, task, request, output_cap, invoke):
        # Reuse only returned extraction responses from an earlier explicit
        # attempt of this exact immutable job. Extraction validates them again;
        # a transport return alone is not scientific/schema acceptance.
        if task in {"methods", "math"}:
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                job = self._get(db, job_id)
                if job["status"] != "RUNNING" or job["claimed_by"] != claimant or job["cancel_requested"]:
                    raise JobStopped("Job cancelled or claim unavailable")
                self._authorize(json.loads(job["plan_json"]))
                cached = db.execute(
                    "SELECT id,response_json FROM calls WHERE job_id=? AND task=? "
                    "AND request_digest=? AND attempt<? AND status='RETURNED' "
                    "ORDER BY dispatched_at DESC,id DESC LIMIT 1",
                    (job_id, task, digest(request), job["attempt"]),
                ).fetchone()
                if cached:
                    response = json.loads(cached["response_json"])
                    response["usage"] = {}  # Original usage remains on its paid call.
                    response["cached_call_id"] = cached["id"]
                    self._event(db, job_id, "response_reused", payload={
                        "task": task, "call_id": cached["id"], "attempt": job["attempt"],
                    })
                    return response
        call_id = self._dispatch(job_id, claimant, task, request, output_cap)
        try:
            response = invoke()
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            returned = getattr(exc, "response_payload", None)
            # Unknown transport failures might have reached the provider.
            with self.connect() as db:
                db.execute("UPDATE calls SET status=?,error_json=?,response_json=?,completed_at=? WHERE id=?",
                           ("RETURNED_INVALID" if returned is not None else ("ERROR" if isinstance(status, int) else "UNCERTAIN"),
                            encoded({"type": type(exc).__name__}), encoded(returned) if returned is not None else None, utc_now(), call_id))
            raise
        with self.connect() as db:
            db.execute("UPDATE calls SET status='RETURNED',response_json=?,completed_at=? WHERE id=?", (encoded(response), utc_now(), call_id))
        return response

    def work_once(self, *, extraction_factory: Callable | None = None, embedding_factory: Callable | None = None,
                  target_job_id: str | None = None) -> dict:
        if target_job_id is not None:
            identifier(target_job_id)
        with self.worker_lock():
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                for row in db.execute("SELECT id FROM jobs WHERE status='RUNNING'").fetchall():
                    db.execute("UPDATE jobs SET status='NEEDS_ATTENTION',updated_at=? WHERE id=?", (utc_now(), row[0]))
                    self._event(db, row[0], "interrupted_worker")
                queued = db.execute("SELECT id FROM jobs WHERE status='QUEUED' AND (? IS NULL OR id=?) ORDER BY created_at,id LIMIT 1", (target_job_id, target_job_id)).fetchone()
                if not queued:
                    return {"space_id": self.space_id, "status": "IDLE"}
                job_id = queued[0]
                claimant = uuid.uuid4().hex
                db.execute("UPDATE jobs SET status='RUNNING',attempt=attempt+1,claimed_by=?,updated_at=? WHERE id=?", (claimant, utc_now(), job_id))
                self._event(db, job_id, "claimed")
            job = self.show(job_id)
            plan = job["plan"]
            try:
                self._authorize(plan)
                if digest(plan) != job["plan_digest"] or plan["space_id"] != self.space_id or plan["prompt_version"] != extraction.PROMPT_VERSION:
                    raise JobStopped("Plan configuration changed; new approval required")
                if plan.get("embedding_version") != embeddings.EMBEDDING_VERSION:
                    raise JobStopped("Embedding representation changed; new approval required")
                if os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/") != "https://api.openai.com/v1":
                    raise JobStopped("Provider endpoint changed; refusing dispatch")
                if plan["extraction_contract_digest"] != digest([extraction.METHOD_EXTRACTION_SCHEMA, extraction.MATH_EXTRACTION_SCHEMA,
                                                                extraction.METHOD_INSTRUCTIONS, extraction.MATH_INSTRUCTIONS]):
                    raise JobStopped("Extraction schema or instructions changed; new approval required")
                blocks = self.brain.store.get_blocks(plan["document_id"], compilation_id=plan["compilation_id"])
                if digest(blocks) != plan["block_digest"]:
                    raise JobStopped("Approved compilation changed")
                self._execute(job_id, claimant, plan, extraction_factory, embedding_factory)
                self._authorize(plan)
                with self.connect() as db:
                    db.execute("UPDATE jobs SET status=CASE WHEN cancel_requested THEN 'CANCELLED' ELSE 'COMPLETE' END,updated_at=? WHERE id=?", (utc_now(), job_id))
                    self._event(db, job_id, "processing_finished")
            except Exception as exc:
                with self.connect() as db:
                    uncertain = db.execute("SELECT 1 FROM calls WHERE job_id=? AND status IN ('DISPATCHED','UNCERTAIN')", (job_id,)).fetchone()
                    cancelled = self._get(db, job_id)["cancel_requested"]
                    status = "NEEDS_ATTENTION" if uncertain else ("CANCELLED" if cancelled else "FAILED")
                    db.execute("UPDATE jobs SET status=?,error_json=?,updated_at=? WHERE id=?",
                               (status, encoded({"type": type(exc).__name__, "message": str(exc) if isinstance(exc, JobStopped) else "Task failed; inspect local generation ledger"}), utc_now(), job_id))
                    self._event(db, job_id, "processing_stopped", payload={"status": status})
            return self.show(job_id)

    def _execute(self, job_id, claimant, plan, extraction_factory, embedding_factory):
        card_ids = []
        for task in plan["tasks"]:
            self._authorize(plan)
            with self.connect() as db:
                if self._get(db, job_id)["cancel_requested"]:
                    raise JobStopped("Job cancelled")
                step = db.execute("SELECT * FROM steps WHERE job_id=? AND task=?", (job_id, task)).fetchone()
            if step["result_json"]:
                result = json.loads(step["result_json"])
                card_ids.extend(result.get("object_ids", []))
                continue
            def run_callback(run_id):
                with self.connect() as db:
                    db.execute("UPDATE steps SET run_id=? WHERE job_id=? AND task=?", (run_id, job_id, task))
            jobs = self
            if task != "embeddings":
                def factory(**config):
                    if extraction_factory:
                        provider = extraction_factory(**config)
                    else:
                        from .openai_provider import OpenAIResponsesProvider
                        provider = OpenAIResponsesProvider(**config)
                    class Guarded:
                        def extract(self, **request):
                            return jobs._call(job_id, claimant, task, request, plan["limits"]["max_output_tokens"], lambda: provider.extract(**request))
                    return Guarded()
                result = asdict(extraction.Extractor(self.brain.store, factory).extract(
                    task, plan["document_id"], compilation_id=plan["compilation_id"], live=True,
                    model=plan["extract_model"], reasoning_effort=plan["reasoning_effort"], run_callback=run_callback))
                card_ids.extend(result["object_ids"])
            else:
                def factory(**config):
                    if embedding_factory:
                        provider = embedding_factory(**config)
                    else:
                        from .openai_provider import OpenAIEmbeddingProvider
                        provider = OpenAIEmbeddingProvider(**config)
                    class Guarded:
                        def embed(self, texts):
                            def invoke():
                                vectors = provider.embed(texts)
                                return {"vectors": vectors, **getattr(provider, "last_metadata", {})}
                            response = jobs._call(job_id, claimant, task, texts, 0, invoke)
                            self.last_metadata = {k: v for k, v in response.items() if k != "vectors"}
                            return response["vectors"]
                    return Guarded()
                result = asdict(EmbeddingIndexer(self.brain.store, factory).index(
                    plan["document_id"], live=True, model=plan["embed_model"],
                    compilation_id=plan["compilation_id"], object_ids=card_ids, run_callback=run_callback))
            with self.connect() as db:
                db.execute("UPDATE steps SET result_json=? WHERE job_id=? AND task=?", (encoded(result), job_id, task))
                self._event(db, job_id, "task_complete", payload={"task": task, "run_id": result["run_id"]})
