-- PostgreSQL variant of schema.sql (the documented production swap; see
-- docs/POSTGRES.md and src/regcompass/pg.py). Differences from the SQLite
-- schema: SERIAL instead of AUTOINCREMENT, BYTEA instead of BLOB, boolean
-- columns as BOOLEAN, and chunk embeddings in a dedicated pgvector column
-- (the SQLite file keeps them as float32 BLOBs on the chunks table).
--
-- Requires: PostgreSQL 15+ with the pgvector extension (0.8.x).

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
    document_id        TEXT PRIMARY KEY,
    economy            TEXT NOT NULL,
    source_url         TEXT,
    source_sha256      TEXT NOT NULL UNIQUE,
    local_path         TEXT,                      -- relative to the data dir: <ECONOMY>/raw/<file>
    fetched_at         TEXT,
    extractor          TEXT,
    extractor_version  TEXT,
    full_text          TEXT,
    language           TEXT,
    ocr_applied        BOOLEAN NOT NULL DEFAULT FALSE,
    ocr_quality_cer    REAL,
    title              TEXT,
    n_pages            INTEGER,
    n_low_yield_pages  INTEGER,
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id       TEXT PRIMARY KEY,
    document_id    TEXT NOT NULL REFERENCES documents(document_id),
    char_start     INTEGER NOT NULL,
    char_end       INTEGER NOT NULL,
    section_label  TEXT NOT NULL,
    chunk_kind     TEXT NOT NULL DEFAULT 'section',
    page_start     INTEGER,
    page_end       INTEGER,
    created_at     TEXT NOT NULL,
    CHECK (char_end >= char_start)
);

-- bge-m3 is 1024-dimensional; adjust if the embedder tier changes.
CREATE TABLE IF NOT EXISTS chunk_embeddings (
    chunk_id   TEXT PRIMARY KEY REFERENCES chunks(chunk_id),
    embedding  vector(1024) NOT NULL
);

CREATE TABLE IF NOT EXISTS mappings (
    mapping_id                 TEXT PRIMARY KEY,
    chunk_id                   TEXT NOT NULL REFERENCES chunks(chunk_id),
    document_id                TEXT NOT NULL REFERENCES documents(document_id),
    economy                    TEXT NOT NULL,
    indicator_id               TEXT NOT NULL,
    indicator_name             TEXT NOT NULL,
    section                    TEXT NOT NULL,
    subsection                 TEXT,
    verbatim_quote             TEXT NOT NULL,
    page_number                INTEGER,
    impact                     TEXT,
    rdtii_score_contribution   REAL,
    insufficient_evidence      BOOLEAN NOT NULL DEFAULT FALSE,
    confidence                 REAL,
    verification_status        TEXT NOT NULL DEFAULT 'unverified',
    discovery_tag              TEXT,
    measure_type               TEXT,
    timeline_adopted           TEXT,
    timeline_entry_into_force  TEXT,
    timeline_last_amended      TEXT,
    timeline_repeal_status     TEXT,
    source_archived_url        TEXT,
    source_access_date         TEXT,
    uncertainty_flags          TEXT NOT NULL DEFAULT '[]',
    controlling_evidence       BOOLEAN,
    novelty_scope              TEXT,
    relationship_to_group      TEXT,
    extraction_attempts        INTEGER NOT NULL DEFAULT 1,
    created_at                 TEXT NOT NULL,
    CHECK (discovery_tag IN ('NEW', 'KNOWN') OR discovery_tag IS NULL),
    CHECK (verification_status IN ('unverified', 'passed', 'dropped'))
);

CREATE TABLE IF NOT EXISTS gate_scores (
    chunk_id      TEXT NOT NULL,
    indicator_id  TEXT NOT NULL,
    cosine_pillar REAL NOT NULL,
    PRIMARY KEY (chunk_id, indicator_id)
);

CREATE TABLE IF NOT EXISTS reviews (
    id             SERIAL PRIMARY KEY,
    mapping_id     TEXT NOT NULL,
    review_status  TEXT NOT NULL,
    reviewer_note  TEXT,
    reviewed_at    TEXT NOT NULL,
    CHECK (review_status IN ('accepted', 'rejected', 'flagged'))
);

CREATE TABLE IF NOT EXISTS audit_log (
    id           SERIAL PRIMARY KEY,
    stage        TEXT NOT NULL,
    input_hash   TEXT,
    output_hash  TEXT,
    method       TEXT NOT NULL,
    decision     TEXT NOT NULL,
    duration_ms  REAL NOT NULL,
    timestamp    TEXT NOT NULL
);

-- Run Records: the saved facts of one Run or one Discovery.
CREATE TABLE IF NOT EXISTS runs (
    run_id             TEXT PRIMARY KEY,
    kind               TEXT NOT NULL,
    economy            TEXT NOT NULL,
    pillars            TEXT NOT NULL DEFAULT '[]',
    indicators         TEXT,
    engine             TEXT,
    status             TEXT NOT NULL,
    started_at         TEXT NOT NULL,
    ended_at           TEXT,
    documents_fetched  INTEGER NOT NULL DEFAULT 0,
    prompt_tokens      INTEGER NOT NULL DEFAULT 0,
    completion_tokens  INTEGER NOT NULL DEFAULT 0,
    cost_usd           REAL NOT NULL DEFAULT 0.0,
    provider_cost_usd  REAL,
    error              TEXT,
    details            TEXT NOT NULL DEFAULT '{}',
    CHECK (kind IN ('run', 'discovery')),
    CHECK (status IN ('running', 'completed', 'failed'))
);

-- The extraction cache: one canonical stream per (Document bytes,
-- extraction logic and libraries, tesseract language data, OCR quality ladder).
-- A Run looks the key up before it reads a Document, so a scanned Act is OCR'd
-- once rather than on every pass.
CREATE TABLE IF NOT EXISTS extractions (
    extraction_key     TEXT PRIMARY KEY,
    source_sha256      TEXT NOT NULL,
    extractor_version  TEXT NOT NULL,
    ocr_languages      TEXT NOT NULL,
    ocr_policy_id      TEXT NOT NULL,
    source_format      TEXT NOT NULL,
    extractor          TEXT NOT NULL,
    ocr_applied        BOOLEAN NOT NULL DEFAULT FALSE,
    ocr_engine_version TEXT,
    tessdata_sha256    TEXT,
    n_chars            INTEGER NOT NULL,
    n_pages            INTEGER NOT NULL,
    n_low_yield_pages  INTEGER NOT NULL,
    canonical_json     BYTEA NOT NULL,          -- zlib-deflated CanonicalText JSON
    created_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_started ON runs (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_extractions_sha ON extractions(source_sha256);
CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_mappings_group_key ON mappings(economy, indicator_id);
CREATE INDEX IF NOT EXISTS idx_mappings_status ON mappings(verification_status);
CREATE INDEX IF NOT EXISTS idx_audit_stage ON audit_log(stage);

-- Optional ANN index once the corpus outgrows exact scans (~10^5+ vectors);
-- exact <=> ordering needs no index at Round-1 scale.
-- CREATE INDEX ON chunk_embeddings USING hnsw (embedding vector_cosine_ops);
