"""Versioned SQLite ledger and lexical index."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import importlib.resources
import json
from pathlib import Path
import re
import sqlite3
import struct
from typing import Any, Iterator, Sequence

from ..ids import stable_id
from ..models import ParsedBlock, ResearchObject, RetrievalFiltersV1, SearchHit, SearchHitV2


ORIGINS = {
    "SOURCE_EXPLICIT", "SOURCE_IMPLIED", "AGENT_EXTRACTED", "AGENT_INTERPRETED",
    "AGENT_PROPOSED", "USER_STATED", "USER_ACCEPTED", "EXPERIMENT_OBSERVED",
    "EXTERNALLY_VERIFIED", "SYSTEM_DERIVED",
}
REVIEW_STATES = {"UNREVIEWED", "ACCEPTED", "REJECTED", "DISPUTED", "SUPERSEDED", "DEPRECATED", "INVALIDATED"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class SQLiteStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def migrate(self) -> None:
        migration_root = importlib.resources.files("research_brain.store").joinpath("migrations")
        with self.connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
            applied = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
            for migration in sorted(migration_root.iterdir(), key=lambda item: item.name):
                if migration.suffix != ".sql":
                    continue
                version = int(migration.name.split("_", 1)[0])
                if version in applied:
                    continue
                connection.executescript(migration.read_text(encoding="utf-8"))
                connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)", (version, utc_now()))

    def add_source_asset(self, *, asset_id: str, kind: str, uri: str, local_path: str, sha256: str, content_type: str | None) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO source_assets(id, kind, uri, local_path, sha256, content_type, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (asset_id, kind, uri, local_path, sha256, content_type, utc_now()),
            )

    def add_document(self, *, document_id: str, canonical_url: str, title: str | None, external_ids: dict[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO documents(id, canonical_url, title, authors_json, external_ids_json, created_at) VALUES (?, ?, ?, '[]', ?, ?)",
                (document_id, canonical_url, title, json.dumps(external_ids, sort_keys=True), utc_now()),
            )
            if title:
                connection.execute("UPDATE documents SET title = COALESCE(title, ?) WHERE id = ?", (title, document_id))

    def add_document_version(
        self,
        *,
        version_id: str,
        document_id: str,
        version_label: str | None,
        source_asset_id: str,
        parser_version: str,
        blocks: Sequence[ParsedBlock],
    ) -> bool:
        with self.connect() as connection:
            exists = connection.execute("SELECT 1 FROM document_versions WHERE id = ?", (version_id,)).fetchone()
            if exists:
                return False
            connection.execute(
                "INSERT INTO document_versions(id, document_id, version_label, source_asset_id, parser_version, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (version_id, document_id, version_label, source_asset_id, parser_version, utc_now()),
            )
            for ordinal, block in enumerate(blocks):
                block_id = stable_id("block", version_id, str(ordinal), block.block_type, block.raw_text)
                metadata = json.dumps(block.metadata or {}, sort_keys=True)
                connection.execute(
                    """INSERT INTO document_blocks(
                        id, document_version_id, block_type, section_path, page, ordinal,
                        raw_text, normalized_text, raw_latex, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (block_id, version_id, block.block_type, block.section_path, block.page, ordinal,
                     block.raw_text, block.normalized_text, block.raw_latex, metadata),
                )
                connection.execute(
                    "INSERT INTO block_fts(block_id, document_version_id, block_type, section_path, body) VALUES (?, ?, ?, ?, ?)",
                    (block_id, version_id, block.block_type, block.section_path or "", block.normalized_text),
                )
            self._append_event(connection, "paper_ingested", document_id, {"document_version_id": version_id, "block_count": len(blocks)}, "system")
        return True

    def ingest_compilation(
        self,
        *,
        asset: dict[str, Any],
        document: dict[str, Any],
        version: dict[str, Any],
        compilation: dict[str, Any],
        blocks: Sequence[ParsedBlock],
    ) -> tuple[bool, bool]:
        """Insert one fully parsed source as a single database transaction."""
        with self.connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO source_assets(
                    id, kind, uri, local_path, sha256, content_type, created_at,
                    requested_uri, retrieved_at, license_uri
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (asset["id"], asset["kind"], asset["uri"], asset["local_path"], asset["sha256"],
                 asset.get("content_type"), asset["created_at"], asset.get("requested_uri"),
                 asset.get("retrieved_at"), asset.get("license_uri")),
            )
            connection.execute(
                """INSERT OR IGNORE INTO documents(
                    id, canonical_url, title, authors_json, external_ids_json, created_at
                ) VALUES (?, ?, ?, '[]', ?, ?)""",
                (document["id"], document["canonical_url"], document.get("title"),
                 json.dumps(document.get("external_ids", {}), sort_keys=True), document["created_at"]),
            )
            if document.get("title"):
                connection.execute("UPDATE documents SET title = COALESCE(title, ?) WHERE id = ?",
                                   (document["title"], document["id"]))
            created_version = connection.execute("SELECT 1 FROM document_versions WHERE id = ?", (version["id"],)).fetchone() is None
            if created_version:
                connection.execute(
                    """INSERT INTO document_versions(
                        id, document_id, version_label, source_asset_id, parser_version,
                        created_at, resolution_state
                    ) VALUES (?, ?, ?, ?, 'source', ?, ?)""",
                    (version["id"], document["id"], version.get("version_label"), asset["id"],
                     version["created_at"], version.get("resolution_state", "resolved")),
                )
            created_compilation = connection.execute("SELECT 1 FROM document_compilations WHERE id = ?", (compilation["id"],)).fetchone() is None
            if created_compilation:
                connection.execute(
                    """INSERT INTO document_compilations(
                        id, document_version_id, parser_name, parser_version, config_digest,
                        status, diagnostics_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'complete', ?, ?)""",
                    (compilation["id"], version["id"], compilation["parser_name"],
                     compilation["parser_version"], compilation["config_digest"],
                     json.dumps(compilation.get("diagnostics", {}), sort_keys=True), compilation["created_at"]),
                )
                # Migration 001 scoped ordinals to source versions. Keep compatibility
                # by allocating a disjoint range; ordering within a compilation remains exact.
                base_ordinal = connection.execute(
                    "SELECT COALESCE(MAX(ordinal) + 1, 0) FROM document_blocks WHERE document_version_id=?",
                    (version["id"],),
                ).fetchone()[0]
                for local_ordinal, block in enumerate(blocks):
                    ordinal = base_ordinal + local_ordinal
                    meta = block.metadata or {}
                    member = meta.get("source_member")
                    raw_sha = meta.get("raw_sha256") or __import__("hashlib").sha256(block.raw_text.encode()).hexdigest()
                    block_id = stable_id(
                        "block", compilation["id"], str(member or ""), str(meta.get("char_start", ordinal)),
                        str(meta.get("char_end", ordinal)), raw_sha,
                    )
                    connection.execute(
                        """INSERT INTO document_blocks(
                            id, document_version_id, block_type, section_path, page, ordinal,
                            raw_text, normalized_text, raw_latex, metadata_json, compilation_id,
                            source_member, line_start, line_end, char_start, char_end, raw_sha256
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (block_id, version["id"], block.block_type, block.section_path, block.page, ordinal,
                         block.raw_text, block.normalized_text, block.raw_latex, json.dumps(meta, sort_keys=True),
                         compilation["id"], member, meta.get("line_start"), meta.get("line_end"),
                         meta.get("char_start"), meta.get("char_end"), raw_sha),
                    )
                    connection.execute(
                        "INSERT INTO block_fts(block_id, document_version_id, block_type, section_path, body) VALUES (?, ?, ?, ?, ?)",
                        (block_id, version["id"], block.block_type, block.section_path or "", block.normalized_text),
                    )
                self._append_event(connection, "paper_compiled", document["id"], {
                    "document_version_id": version["id"], "compilation_id": compilation["id"],
                    "block_count": len(blocks), "parser_version": compilation["parser_version"],
                }, "system")
            return created_version, created_compilation

    def get_document(self, document_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
            if not row:
                return None
            result = dict(row)
            result["authors"] = json.loads(result.pop("authors_json"))
            result["external_ids"] = json.loads(result.pop("external_ids_json"))
            result["versions"] = [dict(item) for item in connection.execute(
                "SELECT * FROM document_versions WHERE document_id = ? ORDER BY created_at", (document_id,)
            )]
            for version in result["versions"]:
                version["compilations"] = [dict(item) for item in connection.execute(
                    "SELECT * FROM document_compilations WHERE document_version_id = ? ORDER BY created_at", (version["id"],)
                )]
            return result

    def get_evidence(self, block_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT b.*, d.id AS document_id, d.title AS document_title, d.canonical_url,
                          v.version_label, v.resolution_state,
                          a.uri AS source_uri, a.requested_uri, a.sha256 AS source_sha256,
                          a.local_path AS source_path, a.license_uri
                   FROM document_blocks b
                   JOIN document_versions v ON v.id = b.document_version_id
                   JOIN documents d ON d.id = v.document_id
                   JOIN source_assets a ON a.id = v.source_asset_id
                   WHERE b.id = ?""",
                (block_id,),
            ).fetchone()
            if not row:
                return None
            result = dict(row)
            result["metadata"] = json.loads(result.pop("metadata_json"))
            return result

    def get_blocks(self, document_id: str, *, compilation_id: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as connection:
            if compilation_id is None:
                row = connection.execute(
                    """SELECT c.id FROM document_compilations c
                       JOIN document_versions v ON v.id = c.document_version_id
                       WHERE v.document_id = ? AND c.status = 'complete'
                       ORDER BY v.created_at DESC, c.created_at DESC LIMIT 1""", (document_id,),
                ).fetchone()
                if not row:
                    raise LookupError(f"No compilation found for document: {document_id}")
                compilation_id = row["id"]
            rows = connection.execute(
                """SELECT b.*, v.version_label FROM document_blocks b
                   JOIN document_versions v ON v.id = b.document_version_id
                   WHERE b.compilation_id = ? ORDER BY b.ordinal""", (compilation_id,),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["metadata"] = json.loads(item.pop("metadata_json"))
                result.append(item)
            return result

    def get_object_record(self, object_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM research_objects WHERE id = ?", (object_id,)).fetchone()
            if not row:
                return None
            result = dict(row)
            result["structured"] = json.loads(result.pop("structured_json"))
            result["evidence"] = []
            for link in connection.execute(
                """SELECT e.relation, e.confidence, b.id AS block_id, b.block_type, b.raw_text,
                          b.raw_latex, b.source_member, b.line_start, b.line_end,
                          v.version_label, d.id AS document_id, d.title AS document_title
                   FROM evidence_links e JOIN document_blocks b ON b.id=e.block_id
                   JOIN document_versions v ON v.id=b.document_version_id
                   JOIN documents d ON d.id=v.document_id WHERE e.object_id=?
                   ORDER BY b.ordinal""", (object_id,),
            ):
                result["evidence"].append(dict(link))
            return result

    def review_object(self, object_id: str, *, review_state: str, note: str | None = None,
                      actor: str = "user") -> ResearchObject:
        if review_state not in REVIEW_STATES - {"UNREVIEWED"}:
            raise ValueError(f"Invalid review decision: {review_state}")
        with self.connect() as connection:
            row = connection.execute("SELECT review_state FROM research_objects WHERE id=?", (object_id,)).fetchone()
            if not row:
                raise LookupError(f"Research object not found: {object_id}")
            now = utc_now()
            connection.execute("UPDATE research_objects SET review_state=?, updated_at=? WHERE id=?",
                               (review_state, now, object_id))
            self._append_event(connection, "object_reviewed", object_id,
                               {"from": row["review_state"], "to": review_state, "note": note}, actor)
        return self.get_object(object_id)  # type: ignore[return-value]

    def begin_generation(self, *, run_id: str, task: str, provider: str, model: str,
                         reasoning_effort: str | None, prompt_version: str, schema_version: str,
                         input_digest: str, block_ids: Sequence[str], request: dict[str, Any],
                         force: bool = False) -> tuple[str, bool]:
        with self.connect() as connection:
            if not force:
                cached = connection.execute(
                    """SELECT id FROM generation_runs WHERE task=? AND model=? AND reasoning_effort IS ?
                       AND prompt_version=? AND schema_version=? AND input_digest=? AND status='complete'""",
                    (task, model, reasoning_effort, prompt_version, schema_version, input_digest),
                ).fetchone()
                if cached:
                    return cached["id"], False
            connection.execute(
                """INSERT INTO generation_runs(id, task, provider, model, reasoning_effort,
                   prompt_version, schema_version, input_digest, input_block_ids_json,
                   request_json, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?)""",
                (run_id, task, provider, model, reasoning_effort, prompt_version, schema_version,
                 input_digest, json.dumps(list(block_ids)), json.dumps(request, sort_keys=True), utc_now()),
            )
            return run_id, True

    def record_attempt(self, *, run_id: str, number: int, started_at: str, outcome: str,
                       status_code: int | None = None, error: dict[str, Any] | None = None,
                       usage: dict[str, Any] | None = None) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO generation_attempts(id, run_id, attempt_number, started_at,
                   completed_at, outcome, status_code, error_json, usage_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (stable_id("attempt", run_id, str(number)), run_id, number, started_at, utc_now(), outcome,
                 status_code, json.dumps(error, sort_keys=True) if error else None,
                 json.dumps(usage, sort_keys=True) if usage else None),
            )

    def finish_generation(self, run_id: str, *, status: str, response_id: str | None = None,
                          output: Any = None, usage: dict[str, Any] | None = None,
                          error: dict[str, Any] | None = None) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE generation_runs SET response_id=?, raw_output_json=?, status=?, error_json=?,
                   usage_json=?, completed_at=? WHERE id=?""",
                (response_id, json.dumps(output, sort_keys=True) if output is not None else None, status,
                 json.dumps(error, sort_keys=True) if error else None,
                 json.dumps(usage, sort_keys=True) if usage else None, utc_now(), run_id),
            )

    def create_extracted_objects(self, objects: Sequence[dict[str, Any]], *, run_id: str) -> list[ResearchObject]:
        now = utc_now()
        ids: list[str] = []
        with self.connect() as connection:
            for index, item in enumerate(objects):
                object_id = stable_id("obj", run_id, str(index), item["kind"], item["title"], item["body"])
                ids.append(object_id)
                structured_json = json.dumps(item["structured"], sort_keys=True)
                connection.execute(
                    """INSERT INTO research_objects(id, kind, title, body, structured_json, origin,
                       review_state, confidence, created_at, updated_at, extraction_run_id)
                       VALUES (?, ?, ?, ?, ?, 'AGENT_EXTRACTED', 'UNREVIEWED', NULL, ?, ?, ?)""",
                    (object_id, item["kind"], item["title"], item["body"], structured_json, now, now, run_id),
                )
                connection.execute("INSERT INTO object_fts(object_id, kind, title, body, structured) VALUES (?, ?, ?, ?, ?)",
                                   (object_id, item["kind"], item["title"], item["body"], structured_json))
                for block_id, relation in item["evidence"]:
                    connection.execute("INSERT INTO evidence_links(object_id, block_id, relation) VALUES (?, ?, ?)",
                                       (object_id, block_id, relation))
                self._append_event(connection, "object_created", object_id,
                                   {"kind": item["kind"], "origin": "AGENT_EXTRACTED",
                                    "review_state": "UNREVIEWED", "extraction_run_id": run_id}, "openai")
        return [self.get_object(item) for item in ids]  # type: ignore[misc]

    def embedding_targets(self, document_id: str | None = None) -> list[dict[str, str]]:
        targets: list[dict[str, str]] = []
        with self.connect() as connection:
            block_sql = """SELECT b.id, b.block_type, b.section_path, b.normalized_text, b.raw_latex
                           FROM document_blocks b JOIN document_versions v ON v.id=b.document_version_id
                           WHERE b.compilation_id = (
                               SELECT c.id FROM document_compilations c
                               WHERE c.document_version_id=v.id AND c.status='complete'
                               ORDER BY c.created_at DESC LIMIT 1
                           )"""
            params: tuple[Any, ...] = ()
            if document_id:
                block_sql += " AND v.document_id=?"
                params = (document_id,)
            for row in connection.execute(block_sql, params):
                if row["block_type"] not in {"paragraph", "equation"}:
                    continue
                text_value = row["normalized_text"]
                if row["block_type"] == "equation":
                    text_value = f"Section: {row['section_path'] or ''}\nEquation: {row['raw_latex'] or text_value}"
                targets.append({"object_type": "document_block", "object_id": row["id"],
                                "representation_type": "semantic", "text": text_value})
            object_sql = "SELECT id, title, body, structured_json FROM research_objects"
            for row in connection.execute(object_sql):
                targets.append({"object_type": "research_object", "object_id": row["id"],
                                "representation_type": "semantic",
                                "text": f"{row['title'] or ''}\n{row['body']}\n{row['structured_json']}"})
        return targets

    def unembedded_targets(self, targets: Sequence[dict[str, str]], *, model: str, version: str) -> list[dict[str, str]]:
        with self.connect() as connection:
            existing = {(row["object_type"], row["object_id"], row["representation_type"])
                        for row in connection.execute(
                            "SELECT object_type, object_id, representation_type FROM representations WHERE model_or_parser=? AND version=?",
                            (model, version))}
        return [item for item in targets
                if (item["object_type"], item["object_id"], item["representation_type"]) not in existing]

    def save_embeddings(self, records: Sequence[dict[str, Any]], *, model: str, version: str) -> int:
        count = 0
        with self.connect() as connection:
            for record in records:
                vector = record["vector"]
                blob = struct.pack(f"<{len(vector)}f", *vector)
                representation_id = stable_id("repr", record["object_type"], record["object_id"],
                                              record["representation_type"], model, version, record["input_digest"])
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO representations(id, object_type, object_id,
                       representation_type, model_or_parser, version, text_value, vector_blob,
                       metadata_json, created_at, dimension, input_digest)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?, ?)""",
                    (representation_id, record["object_type"], record["object_id"],
                     record["representation_type"], model, version, record["text"], blob,
                     utc_now(), len(vector), record["input_digest"]),
                )
                count += cursor.rowcount
        return count

    def semantic_vectors(self, *, model: str) -> list[tuple[str, str, list[float]]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT r.object_type, r.object_id, r.vector_blob, r.dimension
                   FROM representations r
                   LEFT JOIN document_blocks b ON r.object_type='document_block' AND b.id=r.object_id
                   LEFT JOIN document_versions v ON v.id=b.document_version_id
                   WHERE r.representation_type='semantic' AND r.model_or_parser=? AND r.vector_blob IS NOT NULL
                     AND (r.object_type!='document_block' OR b.compilation_id = (
                         SELECT c.id FROM document_compilations c
                         WHERE c.document_version_id=v.id AND c.status='complete'
                         ORDER BY c.created_at DESC LIMIT 1
                     ))""",
                (model,),
            ).fetchall()
            return [(row["object_type"], row["object_id"],
                     list(struct.unpack(f"<{row['dimension']}f", row["vector_blob"]))) for row in rows]

    def object_ids_for_kinds(self, kinds: Sequence[str]) -> set[str]:
        if not kinds:
            return set()
        with self.connect() as connection:
            return {row[0] for row in connection.execute(
                f"SELECT id FROM research_objects WHERE kind IN ({','.join('?' for _ in kinds)})", tuple(kinds))}

    def create_object(
        self,
        *,
        kind: str,
        body: str,
        title: str | None = None,
        structured: dict[str, Any] | None = None,
        origin: str = "USER_STATED",
        review_state: str = "ACCEPTED",
        confidence: float | None = None,
        object_id: str | None = None,
        evidence: Sequence[tuple[str, str, float | None]] = (),
        actor: str = "user",
    ) -> ResearchObject:
        if origin not in ORIGINS:
            raise ValueError(f"Unknown origin: {origin}")
        if review_state not in REVIEW_STATES:
            raise ValueError(f"Unknown review state: {review_state}")
        if confidence is not None and not 0 <= confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        structured = structured or {}
        now = utc_now()
        object_id = object_id or stable_id("obj", kind, now, title or "", body)
        structured_json = json.dumps(structured, sort_keys=True)
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO research_objects(id, kind, title, body, structured_json, origin, review_state, confidence, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (object_id, kind, title, body, structured_json, origin, review_state, confidence, now, now),
            )
            connection.execute(
                "INSERT INTO object_fts(object_id, kind, title, body, structured) VALUES (?, ?, ?, ?, ?)",
                (object_id, kind, title or "", body, structured_json),
            )
            for block_id, relation, link_confidence in evidence:
                connection.execute(
                    "INSERT INTO evidence_links(object_id, block_id, relation, confidence) VALUES (?, ?, ?, ?)",
                    (object_id, block_id, relation, link_confidence),
                )
            self._append_event(connection, "object_created", object_id, {"kind": kind, "origin": origin, "review_state": review_state}, actor)
        return self.get_object(object_id)  # type: ignore[return-value]

    def get_object(self, object_id: str) -> ResearchObject | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM research_objects WHERE id = ?", (object_id,)).fetchone()
            if not row:
                return None
            return ResearchObject(
                id=row["id"], kind=row["kind"], title=row["title"], body=row["body"],
                structured=json.loads(row["structured_json"]), origin=row["origin"],
                review_state=row["review_state"], confidence=row["confidence"],
                created_at=row["created_at"], updated_at=row["updated_at"],
            )

    def create_link(
        self,
        *,
        source_id: str,
        relation: str,
        target_id: str,
        metadata: dict[str, Any] | None = None,
        origin: str = "USER_STATED",
        review_state: str = "ACCEPTED",
        confidence: float | None = None,
        actor: str = "user",
    ) -> dict[str, Any]:
        if origin not in ORIGINS:
            raise ValueError(f"Unknown origin: {origin}")
        if review_state not in REVIEW_STATES:
            raise ValueError(f"Unknown review state: {review_state}")
        if confidence is not None and not 0 <= confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        metadata = metadata or {}
        metadata_json = json.dumps(metadata, sort_keys=True)
        now = utc_now()
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM links WHERE source_id=? AND relation=? AND target_id=?",
                (source_id, relation, target_id),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO links(source_id, relation, target_id, metadata_json, origin,
                                         review_state, confidence, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (source_id, relation, target_id, metadata_json, origin, review_state, confidence, now),
                )
                self._append_event(
                    connection, "link_created", source_id,
                    {"relation": relation, "target_id": target_id, "metadata": metadata,
                     "origin": origin, "review_state": review_state, "confidence": confidence},
                    actor,
                )
        links = self.get_links(source_id, direction="outgoing", relations=[relation])
        return next(link for link in links if link["target_id"] == target_id)

    def get_links(
        self,
        object_id: str,
        *,
        direction: str = "both",
        relations: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        if direction not in {"incoming", "outgoing", "both"}:
            raise ValueError(f"Unknown link direction: {direction}")
        direction_clause = {
            "incoming": "target_id = ?",
            "outgoing": "source_id = ?",
            "both": "(source_id = ? OR target_id = ?)",
        }[direction]
        params: list[Any] = [object_id] if direction != "both" else [object_id, object_id]
        sql = f"SELECT * FROM links WHERE {direction_clause}"
        if relations:
            sql += f" AND relation IN ({','.join('?' for _ in relations)})"
            params.extend(relations)
        sql += " ORDER BY created_at, source_id, relation, target_id"
        with self.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json"))
            item["direction"] = "outgoing" if item["source_id"] == object_id else "incoming"
            result.append(item)
        return result

    def list_object_records(
        self,
        *,
        kinds: Sequence[str] | None = None,
        thread_id: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT id, structured_json FROM research_objects"
        params: list[Any] = []
        if kinds:
            sql += f" WHERE kind IN ({','.join('?' for _ in kinds)})"
            params.extend(kinds)
        sql += " ORDER BY created_at DESC, id"
        with self.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            structured = json.loads(row["structured_json"])
            if thread_id is not None and structured.get("thread_id") != thread_id:
                continue
            record = self.get_object_record(row["id"])
            if record is not None:
                result.append(record)
        return result

    def update_object_structured(
        self,
        object_id: str,
        *,
        structured: dict[str, Any],
        event_type: str,
        changes: dict[str, Any],
        body: str | None = None,
        actor: str = "user",
    ) -> ResearchObject:
        now = utc_now()
        structured_json = json.dumps(structured, sort_keys=True)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT kind, title, body, structured_json FROM research_objects WHERE id = ?",
                (object_id,),
            ).fetchone()
            if not row:
                raise LookupError(f"Research object not found: {object_id}")
            before = json.loads(row["structured_json"])
            updated_body = row["body"] if body is None else body
            connection.execute(
                "UPDATE research_objects SET body = ?, structured_json = ?, updated_at = ? WHERE id = ?",
                (updated_body, structured_json, now, object_id),
            )
            connection.execute("DELETE FROM object_fts WHERE object_id = ?", (object_id,))
            connection.execute(
                "INSERT INTO object_fts(object_id, kind, title, body, structured) VALUES (?, ?, ?, ?, ?)",
                (object_id, row["kind"], row["title"] or "", updated_body, structured_json),
            )
            self._append_event(
                connection,
                event_type,
                object_id,
                {"changes": changes, "before": before, "after": structured},
                actor,
            )
        return self.get_object(object_id)  # type: ignore[return-value]

    @staticmethod
    def _fts_query(query: str) -> str:
        tokens = re.findall(r"[\w-]+", query, flags=re.UNICODE)
        if not tokens:
            raise ValueError("Search query must contain at least one searchable token")
        return " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)

    def search(self, query: str, *, kinds: Sequence[str] | None = None, limit: int = 10) -> list[SearchHit]:
        fts_query = self._fts_query(query)
        hits: list[SearchHit] = []
        with self.connect() as connection:
            object_sql = """SELECT object_id, kind, title, body, bm25(object_fts, 0, 0, 3, 1, 0.5) AS rank
                            FROM object_fts WHERE object_fts MATCH ?"""
            params: list[Any] = [fts_query]
            if kinds:
                object_sql += f" AND kind IN ({','.join('?' for _ in kinds)})"
                params.extend(kinds)
            object_sql += " ORDER BY rank LIMIT ?"
            params.append(limit)
            for row in connection.execute(object_sql, params):
                hits.append(SearchHit("research_object", row["object_id"], row["title"] or None, row["body"], row["kind"], row["rank"]))

            if not kinds:
                for row in connection.execute(
                    """SELECT f.block_id, f.document_version_id, f.block_type, f.section_path, f.body,
                              bm25(block_fts, 0, 0, 0, 2, 1) AS rank
                       FROM block_fts f
                       JOIN document_blocks b ON b.id=f.block_id
                       JOIN document_versions v ON v.id=b.document_version_id
                       WHERE block_fts MATCH ? AND b.compilation_id = (
                           SELECT c.id FROM document_compilations c
                           WHERE c.document_version_id=v.id AND c.status='complete'
                           ORDER BY c.created_at DESC, c.id DESC LIMIT 1
                       )
                       ORDER BY rank LIMIT ?""",
                    (fts_query, limit),
                ):
                    hits.append(SearchHit("document_block", row["block_id"], row["section_path"] or None,
                                          row["body"], row["block_type"], row["rank"], row["document_version_id"]))
        return sorted(hits, key=lambda hit: hit.rank)[:limit]

    def history(self, object_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM events WHERE object_id = ? ORDER BY created_at, id", (object_id,))
            result = []
            for row in rows:
                item = dict(row)
                item["payload"] = json.loads(item.pop("payload_json"))
                result.append(item)
            return result

    def _append_event(self, connection: sqlite3.Connection, event_type: str, object_id: str | None, payload: dict[str, Any], actor: str | None) -> str:
        now = utc_now()
        event_id = stable_id("event", event_type, object_id or "", now)
        connection.execute(
            "INSERT INTO events(id, event_type, actor, object_id, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (event_id, event_type, actor, object_id, json.dumps(payload, sort_keys=True), now),
        )
        return event_id
