"""Local, explicitly registered storage spaces. No global object resolution.

This registry is trusted local administration, not a network authentication API.
Transport adapters must authenticate principals before requesting a scope.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Iterator

from .brain import Brain
from .store.sqlite import SQLiteStore


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", value):
        raise ValueError("Invalid identifier")
    return value


@dataclass(frozen=True)
class ContextScope:
    principal: str
    audience: str
    conversation_id: str
    writable_space: str
    read_spaces: tuple[str, ...]
    policy_version: int

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


class SpaceRegistry:
    VERSION = 1

    def __init__(self, root: str | Path, *, create: bool = False):
        self.root = Path(root).expanduser().resolve()
        self.path = self.root / "spaces.sqlite3"
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
            with self.connect() as db:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS registry_meta (
                        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                        schema_version INTEGER NOT NULL, policy_version INTEGER NOT NULL);
                    INSERT OR IGNORE INTO registry_meta VALUES (1,1,1);
                    CREATE TABLE IF NOT EXISTS spaces (
                        id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('personal','shared')),
                        owner TEXT NOT NULL, root TEXT NOT NULL UNIQUE, label TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS memberships (
                        space_id TEXT NOT NULL REFERENCES spaces(id), principal TEXT NOT NULL,
                        role TEXT NOT NULL CHECK(role IN ('member','maintainer')),
                        PRIMARY KEY(space_id,principal));
                """)
        with self.connect(readonly=True) as db:
            row = db.execute("SELECT schema_version FROM registry_meta WHERE singleton=1").fetchone()
            if row is None or row[0] != self.VERSION:
                raise ValueError("Incompatible space registry")

    @contextmanager
    def connect(self, *, readonly: bool = False) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(f"{self.path.as_uri()}?mode={'ro' if readonly else 'rwc'}", uri=True, timeout=5)
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

    def register(self, space_id: str, *, kind: str, owner: str, root: str | Path,
                 label: str | None = None) -> dict:
        identifier(space_id)
        identifier(owner)
        if kind not in {"personal", "shared"}:
            raise ValueError("Space kind must be personal or shared")
        target = Path(root).expanduser().resolve(strict=True)
        SQLiteStore(target / "brain.sqlite3", initialize=False)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # Nested roots could expose another space's assets or session files.
            for row in db.execute("SELECT root FROM spaces"):
                other = Path(row[0])
                if target == other or target in other.parents or other in target.parents:
                    raise ValueError("Space roots must be distinct and non-overlapping")
            db.execute("INSERT INTO spaces VALUES (?,?,?,?,?)",
                       (space_id, kind, owner, str(target), label or space_id))
            db.execute("INSERT INTO memberships VALUES (?,?, 'maintainer')", (space_id, owner))
            db.execute("UPDATE registry_meta SET policy_version=policy_version+1")
        return self.get(space_id)

    def get(self, space_id: str) -> dict:
        identifier(space_id)
        with self.connect(readonly=True) as db:
            row = db.execute("SELECT * FROM spaces WHERE id=?", (space_id,)).fetchone()
        if row is None:
            raise LookupError("Space unavailable")
        return dict(row)

    def list(self) -> list[dict]:
        """Administrative listing; never expose this directly to a bot user."""
        with self.connect(readonly=True) as db:
            return [dict(row) for row in db.execute("SELECT * FROM spaces ORDER BY id")]

    def set_membership(self, space_id: str, principal: str, role: str | None) -> None:
        identifier(principal)
        space = self.get(space_id)
        if space["kind"] != "shared" or principal == space["owner"]:
            raise ValueError("Personal membership and space ownership cannot be changed")
        if role not in {None, "member", "maintainer"}:
            raise ValueError("Invalid membership role")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if role is None:
                db.execute("DELETE FROM memberships WHERE space_id=? AND principal=?", (space_id, principal))
            else:
                db.execute("INSERT INTO memberships VALUES (?,?,?) ON CONFLICT(space_id,principal) DO UPDATE SET role=excluded.role",
                           (space_id, principal, role))
            db.execute("UPDATE registry_meta SET policy_version=policy_version+1")

    def scope(self, principal: str, *, conversation_id: str, writable_space: str,
              read_spaces: tuple[str, ...] = ()) -> ContextScope:
        identifier(principal)
        identifier(conversation_id)
        destination = self.get(writable_space)
        reads = tuple(sorted(set((writable_space, *read_spaces))))
        with self.connect(readonly=True) as db:
            version = db.execute("SELECT policy_version FROM registry_meta").fetchone()[0]
        scope = ContextScope(principal, f"{destination['kind']}:{writable_space}",
                             conversation_id, writable_space, reads, version)
        self.validate(scope)
        return scope

    def validate(self, scope: ContextScope, *, space_id: str | None = None,
                 maintainer: bool = False) -> None:
        def deny():
            raise PermissionError("Context unavailable or authorization changed")
        with self.connect(readonly=True) as db:
            db.execute("BEGIN")  # One consistent policy/membership snapshot.
            version = db.execute("SELECT policy_version FROM registry_meta").fetchone()[0]
            if version != scope.policy_version or scope.writable_space not in scope.read_spaces:
                deny()
            destination = db.execute("SELECT * FROM spaces WHERE id=?", (scope.writable_space,)).fetchone()
            if destination is None or scope.audience != f"{destination['kind']}:{destination['id']}":
                deny()
            for read in scope.read_spaces:
                row = db.execute("SELECT s.*, m.role FROM spaces s JOIN memberships m ON m.space_id=s.id WHERE s.id=? AND m.principal=?",
                                 (read, scope.principal)).fetchone()
                if row is None:
                    deny()
                if row["kind"] == "personal" and (destination["kind"] == "shared" or row["owner"] != scope.principal):
                    deny()
                if maintainer and read == scope.writable_space and row["role"] != "maintainer":
                    deny()
            if space_id is not None and space_id not in scope.read_spaces:
                deny()

    def open(self, space_id: str) -> Brain:
        """Trusted local CLI open. Network callers must use scoped operations."""
        return Brain(self.get(space_id)["root"], initialize=False)

    def review_card(self, scope: ContextScope, object_id: str, *, review_state: str,
                    expected_version: str, note: str | None = None, actor: str = "user"):
        """Maintainer-only review in the writable space, never a Pi read tool.

        Hold the policy writer lock through the Brain transaction so membership
        revocation cannot race a scientific review across the separate databases.
        """
        if review_state not in {"ACCEPTED", "DISPUTED", "REJECTED"}:
            raise ValueError("Invalid review decision")
        if not isinstance(expected_version, str) or not 1 <= len(expected_version) <= 100:
            raise ValueError("A current review version is required")
        if note is not None and (not isinstance(note, str) or len(note) > 4000 or "\x00" in note):
            raise ValueError("Invalid review note")
        if review_state != "ACCEPTED" and not (note or "").strip():
            raise ValueError("A note is required for dispute or rejection")
        with self.connect() as policy:
            policy.execute("BEGIN IMMEDIATE")
            self.validate(scope, space_id=scope.writable_space, maintainer=True)
            brain = self.open(scope.writable_space)
            record = brain.get_research_object(object_id)
            if record is None or record["kind"] not in {"method_card", "math_card"} or not record["evidence"]:
                raise LookupError("Evidence-backed card unavailable")
            result = brain.review_research_object(object_id, review_state=review_state,
                expected_version=expected_version, note=note, actor=actor)
            return {"space_id": scope.writable_space, "object_id": result.id,
                    "review_state": result.review_state, "updated_at": result.updated_at,
                    "origin": result.origin}

    def read(self, scope: ContextScope, space_id: str, operation: str, *args, **kwargs):
        """A bounded read-only entry point; rechecks revocation before returning.

        Does not return a Brain handle or allow caller-selected paths/provider calls.
        Arms must additionally bind the supplied scope to its authenticated session.
        """
        allowed = {"get_document", "get_evidence", "get_research_object", "recall", "search",
                   "read_object_field", "read_evidence_field", "paper_overview", "paper_cards", "search_discussions", "discussion_excerpt", "frontier_view"}
        if operation not in allowed or kwargs.get("semantic_live"):
            raise PermissionError("Operation unavailable")
        self.validate(scope, space_id=space_id)
        result = getattr(self.open(space_id), operation)(*args, **kwargs)
        self.validate(scope, space_id=space_id)
        return {"space_id": space_id, "result": result}

    def record_discussion(self, scope: ContextScope, record: dict):
        """Trusted transport projection only; not exposed through read operations."""
        if record.get("author") != scope.principal or record.get("conversation_id") != scope.conversation_id:
            raise PermissionError("Discussion attribution does not match scope")
        with self.connect() as policy:
            policy.execute("BEGIN IMMEDIATE")
            self.validate(scope)
            if (record.get("guild_id") is None) != scope.audience.startswith("personal:"):
                raise PermissionError("Discussion audience mismatch")
            return self.open(scope.writable_space).record_discussion(record)

    def save_discussion_excerpt(self, scope: ContextScope, record_id: str, *, revision: int,
                                start: int, end: int, expected_digest: str, actor: str):
        with self.connect() as policy:
            policy.execute("BEGIN IMMEDIATE")
            self.validate(scope, space_id=scope.writable_space, maintainer=True)
            result = self.open(scope.writable_space).save_discussion_excerpt(record_id,
                revision=revision, start=start, end=end, expected_digest=expected_digest, actor=actor)
            return {"space_id": scope.writable_space, **result}
