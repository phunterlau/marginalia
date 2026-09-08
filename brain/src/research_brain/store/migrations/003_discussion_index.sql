CREATE TABLE discussion_revisions (
    record_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    digest TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY(record_id, revision)
);
CREATE TABLE discussion_heads (
    record_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    FOREIGN KEY(record_id, revision) REFERENCES discussion_revisions(record_id, revision)
);
CREATE VIRTUAL TABLE discussion_fts USING fts5(
    record_id UNINDEXED, question, answer, tokenize='unicode61'
);
