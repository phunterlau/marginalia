PRAGMA foreign_keys = ON;

ALTER TABLE source_assets ADD COLUMN requested_uri TEXT;
ALTER TABLE source_assets ADD COLUMN retrieved_at TEXT;
ALTER TABLE source_assets ADD COLUMN license_uri TEXT;

ALTER TABLE document_versions ADD COLUMN resolution_state TEXT NOT NULL DEFAULT 'resolved';

CREATE TABLE document_compilations (
    id TEXT PRIMARY KEY,
    document_version_id TEXT NOT NULL REFERENCES document_versions(id),
    parser_name TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    config_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    diagnostics_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(document_version_id, parser_name, parser_version, config_digest)
);

ALTER TABLE document_blocks ADD COLUMN compilation_id TEXT REFERENCES document_compilations(id);
ALTER TABLE document_blocks ADD COLUMN source_member TEXT;
ALTER TABLE document_blocks ADD COLUMN line_start INTEGER;
ALTER TABLE document_blocks ADD COLUMN line_end INTEGER;
ALTER TABLE document_blocks ADD COLUMN char_start INTEGER;
ALTER TABLE document_blocks ADD COLUMN char_end INTEGER;
ALTER TABLE document_blocks ADD COLUMN raw_sha256 TEXT;

INSERT INTO document_compilations(
    id, document_version_id, parser_name, parser_version, config_digest,
    status, diagnostics_json, created_at
)
SELECT 'comp_' || substr(hex(randomblob(16)), 1, 24), id, 'structural', parser_version,
       'legacy', 'complete', '{"migrated":true}', created_at
FROM document_versions;

UPDATE document_blocks
SET compilation_id = (
    SELECT c.id FROM document_compilations c
    WHERE c.document_version_id = document_blocks.document_version_id
    ORDER BY c.created_at LIMIT 1
);

CREATE TABLE generation_runs (
    id TEXT PRIMARY KEY,
    task TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    reasoning_effort TEXT,
    prompt_version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    input_block_ids_json TEXT NOT NULL DEFAULT '[]',
    request_json TEXT NOT NULL DEFAULT '{}',
    response_id TEXT,
    raw_output_json TEXT,
    status TEXT NOT NULL,
    error_json TEXT,
    usage_json TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE generation_attempts (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES generation_runs(id),
    attempt_number INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    outcome TEXT NOT NULL,
    status_code INTEGER,
    error_json TEXT,
    usage_json TEXT,
    UNIQUE(run_id, attempt_number)
);

ALTER TABLE research_objects ADD COLUMN extraction_run_id TEXT REFERENCES generation_runs(id);

ALTER TABLE representations ADD COLUMN dimension INTEGER;
ALTER TABLE representations ADD COLUMN input_digest TEXT;

CREATE INDEX idx_compilations_version ON document_compilations(document_version_id, created_at);
CREATE INDEX idx_blocks_compilation ON document_blocks(compilation_id, ordinal);
CREATE INDEX idx_generation_runs_digest ON generation_runs(input_digest, status);
CREATE INDEX idx_generation_attempts_run ON generation_attempts(run_id, attempt_number);
