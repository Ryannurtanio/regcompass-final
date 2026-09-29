-- RegCompass SQLite schema. 7 tables + crawl_manifest (M10)
-- + shortlist_windows (M11).
-- Embeddings are float32 BLOBs (C-order) with the dimension recorded per row, so the
-- documented PostgreSQL + pgvector swap is a Storage/VectorIndex implementation change,
-- not a schema redesign.
-- Timestamps are ISO-8601 UTC strings. JSON-valued columns are noted.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS documents (
    document_id        TEXT PRIMARY KEY,
    economy            TEXT NOT NULL,              -- internal code: SG | AU | MY
    source_url         TEXT,
    source_sha256      TEXT NOT NULL UNIQUE,       -- hash of the exact fetched bytes
    local_path         TEXT,                      -- relative to the data dir: <ECONOMY>/raw/<file>
    fetched_at         TEXT,
    extractor          TEXT,                       -- e.g. 'pdfplumber'
    extractor_version  TEXT,                       -- pinned; part of the ground-truth definition
    full_text          TEXT,                       -- THE canonical stream, produced once
    language           TEXT,
    ocr_applied        INTEGER NOT NULL DEFAULT 0, -- boolean
    ocr_quality_cer    REAL,                       -- MEASURED CER (needs a hand-checked reference); null in production
    mean_word_confidence   REAL,                   -- OCR quality proxy (M2); production stand-in for CER
    dictionary_hit_rate    REAL,                   -- OCR quality proxy (M2)
    cer_proxy_flag         INTEGER,                -- boolean: proxies below threshold = suspected CER >= 5%
    escalated_to_rapidocr  INTEGER,                -- boolean: tesseract escalated to the RapidOCR fallback
    manual_review          INTEGER,                -- boolean: still below a floor after escalation
    -- How this Document entered the Corpus: 'discovery' (a Portal fetch) or
    -- 'manual' (a reviewer added it by upload or by Source URL). A manual
    -- Document is exempted from the export's portal whitelist and says so in
    -- its Notes, so the weaker guarantee is never silent.
    source_kind        TEXT NOT NULL DEFAULT 'discovery',
    -- A secondary reference the Portal published beside this Document: the Lao
    -- Official Gazette offers an English rendering of some instruments, and its
    -- URL is recorded precisely because it is never the text a quote comes from.
    notes              TEXT,
    title              TEXT,                       -- mechanically derived at ingest (M11)
    n_pages            INTEGER,                    -- ingest page stats (M11 text-coverage gate)
    n_low_yield_pages  INTEGER,
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id       TEXT PRIMARY KEY,
    document_id    TEXT NOT NULL REFERENCES documents(document_id),
    char_start     INTEGER NOT NULL,
    char_end       INTEGER NOT NULL,
    section_label  TEXT NOT NULL,
    chunk_kind     TEXT NOT NULL DEFAULT 'section',  -- section|front_matter|toc|schedule|other
    page_start     INTEGER,
    page_end       INTEGER,
    embedding      BLOB,                              -- float32, C-order
    embedding_dim  INTEGER,
    embedding_dtype TEXT DEFAULT 'float32',
    created_at     TEXT NOT NULL,
    CHECK (char_end >= char_start)
);

-- A Mapping belongs to the Run that produced it. mapping_id keeps its exact
-- `chunk_id::indicator_id` spelling (the Round 1 goldens pin it), so the Run is
-- what makes a row unique: two Runs over one Economy now coexist instead of the
-- second silently overwriting the first, and reconciliation, export and the
-- audit view all read one Run at a time. Rows from a database written before
-- this column existed carry run_id 'legacy'.
CREATE TABLE IF NOT EXISTS mappings (
    run_id                     TEXT NOT NULL DEFAULT 'legacy',
    mapping_id                 TEXT NOT NULL,
    chunk_id                   TEXT NOT NULL REFERENCES chunks(chunk_id),
    document_id                TEXT NOT NULL REFERENCES documents(document_id),
    economy                    TEXT NOT NULL,
    indicator_id               TEXT NOT NULL,       -- canonical numeric: 6.1 .. 7.5
    indicator_name             TEXT NOT NULL,
    section                    TEXT NOT NULL,
    subsection                 TEXT,
    verbatim_quote             TEXT NOT NULL,
    page_number                INTEGER,
    impact                     TEXT,
    rdtii_score_contribution   REAL,                -- INVERSE direction: 0 = open, 1 = most restrictive
    insufficient_evidence      INTEGER NOT NULL DEFAULT 0,
    confidence                 REAL,                -- mechanical composite, never LLM self-reported
    verification_status        TEXT NOT NULL DEFAULT 'unverified',  -- unverified|passed|dropped
    discovery_tag              TEXT,                -- exactly 'NEW' or 'KNOWN', case-sensitive
    measure_type               TEXT,
    timeline_adopted           TEXT,
    timeline_entry_into_force  TEXT,
    timeline_last_amended      TEXT,
    timeline_repeal_status     TEXT,
    source_archived_url        TEXT,
    source_access_date         TEXT,
    uncertainty_flags          TEXT NOT NULL DEFAULT '[]',  -- JSON array of UncertaintyFlag
    controlling_evidence       INTEGER,             -- boolean; exactly one true per (economy, indicator) group
    novelty_scope              TEXT,                -- law|provision
    relationship_to_group      TEXT,                -- sole_source|complementary|superseded_by|supersedes|conflicting
    extraction_attempts        INTEGER NOT NULL DEFAULT 1,
    created_at                 TEXT NOT NULL,
    PRIMARY KEY (run_id, mapping_id),
    CHECK (discovery_tag IN ('NEW', 'KNOWN') OR discovery_tag IS NULL),
    CHECK (verification_status IN ('unverified', 'passed', 'dropped'))
);

-- The three tables that point AT a Mapping carry its run_id too: a foreign key
-- has to name the whole composite key, and a reconciliation group or a review
-- only ever means something inside one Run.
CREATE TABLE IF NOT EXISTS source_groups (
    group_id                  TEXT PRIMARY KEY,
    run_id                    TEXT NOT NULL DEFAULT 'legacy',
    economy                   TEXT NOT NULL,
    indicator_id              TEXT NOT NULL,
    authoritative_mapping_id  TEXT NOT NULL,
    reconciliation_notes      TEXT,
    created_at                TEXT NOT NULL,
    UNIQUE (run_id, economy, indicator_id),
    FOREIGN KEY (run_id, authoritative_mapping_id) REFERENCES mappings(run_id, mapping_id)
);

CREATE TABLE IF NOT EXISTS mapping_relationships (
    run_id        TEXT NOT NULL DEFAULT 'legacy',
    mapping_id    TEXT NOT NULL,
    group_id      TEXT NOT NULL REFERENCES source_groups(group_id),
    relationship  TEXT NOT NULL,   -- sole_source|complementary|superseded_by|supersedes|conflicting
    PRIMARY KEY (run_id, mapping_id, group_id),
    FOREIGN KEY (run_id, mapping_id) REFERENCES mappings(run_id, mapping_id)
);

-- ONE Review Decision per Mapping per Run: the key is (run_id, mapping_id), so
-- a reviewer who changes their mind REPLACES the decision in place instead of
-- stacking a second row whose precedence a reader has to work out. review_id
-- stays as the decision's own identifier (the API returns it) but no longer
-- decides identity.
CREATE TABLE IF NOT EXISTS reviews (
    review_id      TEXT NOT NULL,
    run_id         TEXT NOT NULL DEFAULT 'legacy',
    mapping_id     TEXT NOT NULL,
    review_status  TEXT NOT NULL,   -- accepted|rejected|flagged|corrected; only accepted enters the final export
    reviewer       TEXT,
    reviewed_at    TEXT NOT NULL,   -- ISO-8601 UTC, the time of the LATEST decision
    comment        TEXT,            -- the reviewer's note; for a correction, its required reason
    -- A correction's Indicator: the one the reviewer says this Mapping belongs
    -- under. The Mapping row keeps the Engine's own; this is the override.
    corrected_indicator_id TEXT,
    PRIMARY KEY (run_id, mapping_id),
    CHECK (review_status IN ('accepted', 'rejected', 'flagged', 'corrected')),
    FOREIGN KEY (run_id, mapping_id) REFERENCES mappings(run_id, mapping_id)
);

-- EVERY Review Decision ever written, oldest first. `reviews` holds the one
-- that stands; this keeps the ones it replaced, so changing a decision never
-- loses the earlier one. Rows are only ever inserted.
CREATE TABLE IF NOT EXISTS review_history (
    history_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                 TEXT NOT NULL,
    mapping_id             TEXT NOT NULL,
    review_status          TEXT NOT NULL,
    corrected_indicator_id TEXT,
    reviewer               TEXT,
    decided_at             TEXT NOT NULL,   -- ISO-8601 UTC
    comment                TEXT,
    FOREIGN KEY (run_id, mapping_id) REFERENCES mappings(run_id, mapping_id)
);
CREATE INDEX IF NOT EXISTS idx_review_history_mapping
    ON review_history(run_id, mapping_id);

-- ONE Gloss per Mapping per Run: the English rendering of a non-English
-- Verbatim Quote, drafted by the Run's own Engine. The key is (run_id,
-- mapping_id) for the same reason reviews carry it: a Gloss drafted in Run A
-- must never attach to Run B's identically-named Mapping. reviewed_by names the
-- person who approved the text; without a name the export keeps the label on.
CREATE TABLE IF NOT EXISTS glosses (
    run_id            TEXT NOT NULL DEFAULT 'legacy',
    mapping_id        TEXT NOT NULL,
    english           TEXT,            -- NULL = drafting failure, see uncertainty_flag
    label             TEXT NOT NULL,   -- the mandatory AI-translation prefix
    reviewed          INTEGER NOT NULL DEFAULT 0,
    reviewed_by       TEXT,            -- the named person; empty is never a review
    reviewed_at       TEXT,            -- ISO-8601 UTC
    source_language   TEXT,
    engine            TEXT NOT NULL,   -- the Engine that drafted it
    uncertainty_flag  TEXT,
    drafted_at        TEXT NOT NULL,
    PRIMARY KEY (run_id, mapping_id),
    FOREIGN KEY (run_id, mapping_id) REFERENCES mappings(run_id, mapping_id)
);

-- M10 crawl manifest: one row per URL, the checkpoint/resume source of truth.
-- Bytes on disk (local_path) are the EXACT server response body; sha256 is the
-- dedupe key. A failed row is recorded, never retried forever (re-runs skip it).
CREATE TABLE IF NOT EXISTS crawl_manifest (
    url              TEXT PRIMARY KEY,
    economy          TEXT NOT NULL,               -- SG | AU | MY
    kind             TEXT NOT NULL DEFAULT 'document',  -- document | index
    source_family    TEXT,                        -- seed family, e.g. data_protection
    filename_hint    TEXT,
    notes            TEXT,                        -- secondary reference found beside the Document
    status           TEXT NOT NULL DEFAULT 'pending',   -- pending | fetched | failed
    http_status      INTEGER,
    method           TEXT,                        -- httpx | curl_cffi | playwright
    sha256           TEXT,
    content_type     TEXT,
    size_bytes       INTEGER,
    local_path       TEXT,                        -- relative to the data dir
    is_duplicate_of  TEXT,                        -- url of the first fetch with identical bytes
    attempts         INTEGER NOT NULL DEFAULT 0,
    error            TEXT,
    fetched_at       TEXT,
    created_at       TEXT NOT NULL,
    CHECK (status IN ('pending', 'fetched', 'failed'))
);

-- M11 shortlist: fixed-size character windows of a document's canonical stream,
-- each with its BGE-M3 embedding (float32 C-order BLOB). Document relevance for
-- a pillar is the max window cosine, so long statutes are represented across
-- their whole length, not just the front matter.
CREATE TABLE IF NOT EXISTS shortlist_windows (
    document_id    TEXT NOT NULL REFERENCES documents(document_id),
    window_index   INTEGER NOT NULL,
    char_start     INTEGER NOT NULL,
    char_end       INTEGER NOT NULL,
    embedding      BLOB NOT NULL,
    embedding_dim  INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (document_id, window_index),
    CHECK (char_end >= char_start)
);

-- U0 highlight geometry: one row per word of the canonical stream, written at
-- ingest beside documents.full_text. Without this the audit view can highlight
-- only records that came out of the frozen bundle, so a Run started from the
-- interface would show its quotes with no rectangle on the page.
--
-- Coordinates are PDF points, top-left origin (pdfplumber for born-digital
-- pages, Tesseract boxes converted to points for OCR pages), exactly as
-- CanonicalText.words carries them. The word's TEXT is not stored: it is
-- documents.full_text[char_start:char_end] by construction on both lanes, and
-- the slice-not-reemit rule that keeps chunks text-free applies here too.
-- WITHOUT ROWID with the character offset in the key makes the audit view's
-- "which words overlap this quote" a range scan over one document.
-- Measured on the committed fixtures: the 130-page Telecommunications Act 1999
-- is 40,739 words and 2.3 MB of rows; the 666-page Australian volume is 184,813
-- words and 10.6 MB. Well inside what a working SQLite file carries, so the
-- rows stay rows: a JSON blob per page would be about a third of the size but
-- would force the reader to decompress a whole act to draw one rectangle.
CREATE TABLE IF NOT EXISTS document_words (
    document_id  TEXT NOT NULL REFERENCES documents(document_id),
    char_start   INTEGER NOT NULL,
    char_end     INTEGER NOT NULL,
    idx          INTEGER NOT NULL,   -- position in CanonicalText.words
    page         INTEGER NOT NULL,
    x0           REAL NOT NULL,
    y0           REAL NOT NULL,
    x1           REAL NOT NULL,
    y1           REAL NOT NULL,
    PRIMARY KEY (document_id, char_start),
    CHECK (char_end >= char_start)
) WITHOUT ROWID;

-- M5 gate cosines, keyed like the export's gate_cosine_lookup: the mechanical
-- confidence composite at M9 needs the pillar cosine for every passed
-- (chunk, indicator) pair. Written by the judge-path run, read at export.
CREATE TABLE IF NOT EXISTS gate_scores (
    chunk_id      TEXT NOT NULL,
    indicator_id  TEXT NOT NULL,
    cosine_pillar REAL NOT NULL,
    PRIMARY KEY (chunk_id, indicator_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    stage        TEXT NOT NULL,
    input_hash   TEXT,
    output_hash  TEXT,
    method       TEXT NOT NULL,
    decision     TEXT NOT NULL,
    duration_ms  REAL NOT NULL,
    timestamp    TEXT NOT NULL
);

-- Run Records: the saved facts of one Run or one Discovery. The
-- record of truth for what a run cost and fetched, so nothing is lost when the
-- server restarts. documents_fetched is always 0 on a Run (a Run reads the
-- Corpus and never fetches); Discovery writes its own count here.
CREATE TABLE IF NOT EXISTS runs (
    run_id             TEXT PRIMARY KEY,
    kind               TEXT NOT NULL CHECK (kind IN ('run', 'discovery')),
    economy            TEXT NOT NULL,
    pillars            TEXT NOT NULL DEFAULT '[]',
    indicators         TEXT,
    engine             TEXT,
    -- 'interrupted' is the fourth honest end: the process that opened the row
    -- died before it could close it (a restart, a kill), so nobody ever wrote
    -- the result. It is set by the sweep the server runs at start, because a
    -- start is the one moment at which nothing can still be running.
    status             TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed', 'interrupted')),
    started_at         TEXT NOT NULL,
    ended_at           TEXT,
    documents_fetched  INTEGER NOT NULL DEFAULT 0,
    prompt_tokens      INTEGER NOT NULL DEFAULT 0,
    completion_tokens  INTEGER NOT NULL DEFAULT 0,
    cost_usd           REAL NOT NULL DEFAULT 0.0,
    provider_cost_usd  REAL,
    error              TEXT,
    details            TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs (started_at DESC);

-- The extraction cache. One row per (Document bytes, extraction
-- logic and libraries, tesseract language data, OCR quality ladder): together
-- those decide every character of the canonical stream, so a row keyed on all
-- four IS the stream a Run would have produced from those bytes. A Run looks
-- the key up before it reads anything, which is what stops it running tesseract
-- over the same scanned Act on every pass. Change any key part and the key
-- moves, the Document is read again, and the old row is left alone rather than
-- overwritten: nothing is ever served under a key it was not produced for.
-- canonical_json is the CanonicalText contract model's own JSON, zlib-deflated;
-- it carries the page spans and the word boxes the audit highlight needs.
CREATE TABLE IF NOT EXISTS extractions (
    extraction_key     TEXT PRIMARY KEY,           -- sha256 over the four key parts
    source_sha256      TEXT NOT NULL,              -- the Document's exact bytes
    extractor_version  TEXT NOT NULL,              -- logic version + extractor libraries
    ocr_languages      TEXT NOT NULL,              -- the tesseract -l string
    ocr_policy_id      TEXT NOT NULL,              -- the OCR quality ladder for the script
    source_format      TEXT NOT NULL,              -- pdf | html, as the bytes sniffed
    extractor          TEXT NOT NULL,              -- what produced it, with its version
    ocr_applied        INTEGER NOT NULL DEFAULT 0, -- boolean
    -- Provenance of an OCR'd stream, never part of the key: which tesseract
    -- read it and which vendored traineddata files it loaded (a JSON map of
    -- language code to file sha256). Both are null for a born-digital stream.
    -- They stay out of the key on purpose, so a database shipped inside an
    -- image keeps serving its streams there rather than re-OCRing everything
    -- because the binary was rebuilt or the vendor directory was copied.
    ocr_engine_version TEXT,
    tessdata_sha256    TEXT,
    n_chars            INTEGER NOT NULL,
    n_pages            INTEGER NOT NULL,
    n_low_yield_pages  INTEGER NOT NULL,
    canonical_json     BLOB NOT NULL,              -- zlib-deflated CanonicalText JSON
    created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_extractions_sha ON extractions(source_sha256);

CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_mappings_group_key ON mappings(economy, indicator_id);
CREATE INDEX IF NOT EXISTS idx_mappings_status ON mappings(verification_status);
CREATE INDEX IF NOT EXISTS idx_mappings_run ON mappings(run_id);
-- (reviews needs no index on (run_id, mapping_id): that pair is its primary key.)
CREATE INDEX IF NOT EXISTS idx_reviews_run ON reviews(run_id);
CREATE INDEX IF NOT EXISTS idx_glosses_run ON glosses(run_id);
CREATE INDEX IF NOT EXISTS idx_audit_stage ON audit_log(stage);
CREATE INDEX IF NOT EXISTS idx_crawl_status ON crawl_manifest(economy, status);
CREATE INDEX IF NOT EXISTS idx_crawl_sha ON crawl_manifest(sha256);
