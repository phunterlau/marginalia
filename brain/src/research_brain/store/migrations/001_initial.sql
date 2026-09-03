PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_assets (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    uri TEXT,
    local_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    content_type TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(uri, sha256)
);

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    canonical_url TEXT NOT NULL UNIQUE,
    title TEXT,
    authors_json TEXT NOT NULL DEFAULT '[]',
    external_ids_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_versions (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id),
    version_label TEXT,
    source_asset_id TEXT NOT NULL REFERENCES source_assets(id),
    parser_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(document_id, source_asset_id, parser_version)
);

CREATE TABLE IF NOT EXISTS document_blocks (
    id TEXT PRIMARY KEY,
    document_version_id TEXT NOT NULL REFERENCES document_versions(id),
    block_type TEXT NOT NULL,
    section_path TEXT,
    page INTEGER,
    ordinal INTEGER NOT NULL,
    raw_text TEXT NOT NULL,
    normalized_text TEXT NOT NULL,
    raw_latex TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(document_version_id, ordinal)
);

CREATE TABLE IF NOT EXISTS research_objects (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT,
    body TEXT NOT NULL,
    structured_json TEXT NOT NULL DEFAULT '{}',
    origin TEXT NOT NULL,
    review_state TEXT NOT NULL,
    confidence REAL CHECK(confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_links (
    object_id TEXT NOT NULL REFERENCES research_objects(id),
    block_id TEXT NOT NULL REFERENCES document_blocks(id),
    relation TEXT NOT NULL,
    confidence REAL CHECK(confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    PRIMARY KEY (object_id, block_id, relation)
);

CREATE TABLE IF NOT EXISTS links (
    source_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    target_id TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    origin TEXT NOT NULL,
    review_state TEXT NOT NULL,
    confidence REAL CHECK(confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    created_at TEXT NOT NULL,
    PRIMARY KEY (source_id, relation, target_id)
);

CREATE TABLE IF NOT EXISTS representations (
    id TEXT PRIMARY KEY,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    representation_type TEXT NOT NULL,
    model_or_parser TEXT,
    version TEXT,
    text_value TEXT,
    vector_blob BLOB,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(object_type, object_id, representation_type, model_or_parser, version)
);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    actor TEXT,
    object_id TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_versions_document ON document_versions(document_id);
CREATE INDEX IF NOT EXISTS idx_blocks_version ON document_blocks(document_version_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_objects_kind ON research_objects(kind);
CREATE INDEX IF NOT EXISTS idx_evidence_block ON evidence_links(block_id);
CREATE INDEX IF NOT EXISTS idx_events_object ON events(object_id, created_at);

CREATE VIRTUAL TABLE IF NOT EXISTS block_fts USING fts5(
    block_id UNINDEXED,
    document_version_id UNINDEXED,
    block_type UNINDEXED,
    section_path,
    body,
    tokenize = 'unicode61'
);

CREATE VIRTUAL TABLE IF NOT EXISTS object_fts USING fts5(
    object_id UNINDEXED,
    kind UNINDEXED,
    title,
    body,
    structured,
    tokenize = 'unicode61'
);
