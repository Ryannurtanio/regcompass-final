"""Schema + storage seam tests (S0 exit criteria: schema applies to a fresh
SQLite file; audit logging and the embedding BLOB round-trip work)."""

import numpy as np
import pytest

from regcompass.observability import log_stage, sha256_hex
from regcompass.storage import Storage, VectorIndex, utc_now_iso

EXPECTED_TABLES = {
    "documents",
    "chunks",
    "mappings",
    "source_groups",
    "mapping_relationships",
    "reviews",
    "audit_log",
}


@pytest.fixture()
def storage(tmp_path):
    s = Storage(tmp_path / "test.db")
    s.apply_schema()
    yield s
    s.close()


def _insert_document(storage: Storage, document_id: str = "doc_001") -> None:
    storage.conn.execute(
        "INSERT INTO documents (document_id, economy, source_sha256, created_at) VALUES (?, ?, ?, ?)",
        (document_id, "SG", "a" * 64, utc_now_iso()),
    )


def _insert_chunk(storage: Storage, chunk_id: str, document_id: str = "doc_001") -> None:
    storage.conn.execute(
        "INSERT INTO chunks (chunk_id, document_id, char_start, char_end, section_label, created_at)"
        " VALUES (?, ?, 0, 10, 's. 1', ?)",
        (chunk_id, document_id, utc_now_iso()),
    )


def test_schema_applies_to_fresh_db(storage):
    assert EXPECTED_TABLES <= storage.table_names()


def test_schema_is_idempotent(storage):
    storage.apply_schema()
    assert EXPECTED_TABLES <= storage.table_names()


def test_foreign_keys_enforced(storage):
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        _insert_chunk(storage, "orphan_chunk", document_id="no_such_document")


def test_discovery_tag_check_constraint(storage):
    import sqlite3

    _insert_document(storage)
    _insert_chunk(storage, "chunk_001")
    with pytest.raises(sqlite3.IntegrityError):
        storage.conn.execute(
            "INSERT INTO mappings (mapping_id, chunk_id, document_id, economy, indicator_id,"
            " indicator_name, section, verbatim_quote, discovery_tag, created_at)"
            " VALUES ('m1', 'chunk_001', 'doc_001', 'SG', '7.2', 'x', 's. 1', 'q', 'new', ?)",
            (utc_now_iso(),),
        )


def test_upsert_document_thin_reupsert_preserves_unprovided_fields(storage):
    """INSERT OR REPLACE would delete-and-reinsert, blanking every column the
    re-upsert omits and firing FK deletes at chunk children; the UPSERT must
    only overwrite what the caller provided."""
    storage.upsert_document(
        "doc_001", "SG", "a" * 64,
        full_text="THE canonical stream", title="Telecommunications Act 1999", n_pages=42,
    )
    created = storage.conn.execute(
        "SELECT created_at FROM documents WHERE document_id = 'doc_001'"
    ).fetchone()["created_at"]
    _insert_chunk(storage, "chunk_001")
    storage.upsert_document("doc_001", "SG", "a" * 64)  # thin: id/economy/sha only
    row = storage.conn.execute("SELECT * FROM documents WHERE document_id = 'doc_001'").fetchone()
    assert row["full_text"] == "THE canonical stream"
    assert row["title"] == "Telecommunications Act 1999"
    assert row["n_pages"] == 42
    assert row["created_at"] == created
    n_chunks = storage.conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
    assert n_chunks == 1


def test_upsert_document_overwrites_only_provided_fields(storage):
    storage.upsert_document("doc_001", "SG", "a" * 64, title="Old Title", n_pages=42)
    storage.upsert_document("doc_001", "SG", "a" * 64, title="New Title")
    row = storage.conn.execute("SELECT * FROM documents WHERE document_id = 'doc_001'").fetchone()
    assert row["title"] == "New Title"
    assert row["n_pages"] == 42


def test_stage_logging_writes_audit_row(storage):
    with log_stage(storage, stage="m1_extract", method="pdfplumber-0.11.10", input_data=b"raw") as rec:
        rec.output_data = "canonical text"
        rec.decision = "extracted"
    row = storage.conn.execute("SELECT * FROM audit_log").fetchone()
    assert row["stage"] == "m1_extract"
    assert row["input_hash"] == sha256_hex(b"raw")
    assert row["output_hash"] == sha256_hex("canonical text")
    assert row["decision"] == "extracted"
    assert row["duration_ms"] >= 0


def test_stage_logging_records_errors_and_reraises(storage):
    with pytest.raises(RuntimeError):
        with log_stage(storage, stage="m6_map", method="qwen3-30b"):
            raise RuntimeError("model exploded")
    row = storage.conn.execute("SELECT decision FROM audit_log").fetchone()
    assert row["decision"] == "error: RuntimeError"


def test_embedding_blob_round_trip(storage):
    _insert_document(storage)
    _insert_chunk(storage, "chunk_001")
    vector = np.arange(8, dtype=np.float32) / 8.0
    storage.store_embedding("chunk_001", vector)
    ids, matrix = storage.load_embeddings()
    assert ids == ["chunk_001"]
    assert matrix.dtype == np.float32
    np.testing.assert_array_equal(matrix[0], vector)


def test_load_embeddings_rejects_mixed_dtypes(storage):
    """embedding_dtype is recorded on write and must be honored on read; a
    store holding two different dtypes cannot be stacked into one matrix."""
    _insert_document(storage)
    for cid in ("chunk_001", "chunk_002"):
        _insert_chunk(storage, cid)
        storage.store_embedding(cid, np.ones(4, dtype=np.float32))
    storage.conn.execute(
        "UPDATE chunks SET embedding_dtype = 'float64' WHERE chunk_id = 'chunk_002'"
    )
    with pytest.raises(ValueError, match="mixed embedding dtypes"):
        storage.load_embeddings()


def test_vector_index_exact_cosine(storage):
    _insert_document(storage)
    rng = np.random.default_rng(42)
    vectors = {f"chunk_{i:03d}": rng.standard_normal(16).astype(np.float32) for i in range(20)}
    for chunk_id, v in vectors.items():
        _insert_chunk(storage, chunk_id)
        storage.store_embedding(chunk_id, v)
    ids, matrix = storage.load_embeddings()
    index = VectorIndex(ids, matrix)
    # querying with an exact stored vector must return it at rank 1, score ~1.0
    results = index.search(vectors["chunk_007"], top_k=3)
    assert results[0][0] == "chunk_007"
    assert results[0][1] == pytest.approx(1.0, abs=1e-5)


def test_vector_index_empty_store_returns_empty():
    index = VectorIndex([], np.empty((0, 0), dtype=np.float32))
    assert index.search(np.ones(4, dtype=np.float32)) == []


# ---------------------------------------------------------------------------
# P0: MappingRecord round-trip + gate scores + chunk-text slicing (the
# judge-path run/export wiring persists everything the M9 export needs)
# ---------------------------------------------------------------------------


def _golden_records():
    import gzip
    import json as _json
    from pathlib import Path

    from regcompass.contracts import MappingRecord

    p = Path(__file__).resolve().parents[0] / "golden/m8/sg_telecommunications_act_1999.reconciled.json.gz"
    with gzip.open(p, "rt", encoding="utf-8") as f:
        return [MappingRecord.model_validate(r) for r in _json.load(f)["records"]]


def test_mapping_records_round_trip_byte_faithful(storage):
    records = _golden_records()
    _insert_document(storage, records[0].document_id)
    for cid in {r.chunk_id for r in records}:
        _insert_chunk(storage, cid, records[0].document_id)
    storage.upsert_mappings(records, run_id="run_one")
    loaded = storage.load_mappings()
    assert len(loaded) == len(records)
    by_id = {r.mapping_id: r for r in records}
    for got in loaded:
        want = by_id[got.mapping_id]
        assert got.model_dump() == want.model_dump()


def test_upsert_mappings_is_replace_not_duplicate(storage):
    records = _golden_records()[:3]
    _insert_document(storage, records[0].document_id)
    for cid in {r.chunk_id for r in records}:
        _insert_chunk(storage, cid, records[0].document_id)
    storage.upsert_mappings(records, run_id="run_one")
    # M8 pass: same ids, controlling flags now filled INSIDE the same Run
    updated = [r.model_copy(update={"controlling_evidence": True}) for r in records]
    storage.upsert_mappings(updated, run_id="run_one")
    loaded = storage.load_mappings()
    assert len(loaded) == 3
    assert all(r.controlling_evidence is True for r in loaded)


def test_load_mappings_filters(storage):
    records = _golden_records()[:4]
    _insert_document(storage, records[0].document_id)
    for cid in {r.chunk_id for r in records}:
        _insert_chunk(storage, cid, records[0].document_id)
    storage.upsert_mappings(records, run_id="run_one")
    assert storage.load_mappings(economy="SG")
    assert storage.load_mappings(economy="AU") == []
    passed = storage.load_mappings(verification_status="passed")
    assert all(r.verification_status == "passed" for r in passed)


def test_gate_scores_round_trip(storage):
    scores = {("doc_001:c0001", "6.1"): 0.62, ("doc_001:c0001", "7.5"): 0.44}
    storage.upsert_gate_scores(scores)
    assert storage.load_gate_scores() == scores
    # replace, not duplicate
    storage.upsert_gate_scores({("doc_001:c0001", "6.1"): 0.70})
    assert storage.load_gate_scores()[("doc_001:c0001", "6.1")] == 0.70


def _documents_columns(storage: Storage) -> set[str]:
    return {r["name"] for r in storage.conn.execute("PRAGMA table_info(documents)")}


def test_schema_carries_the_ocr_proxy_columns(storage):
    cols = _documents_columns(storage)
    assert {
        "mean_word_confidence",
        "dictionary_hit_rate",
        "cer_proxy_flag",
        "escalated_to_rapidocr",
        "manual_review",
    } <= cols


def test_ocr_proxy_migration_backfills_and_is_idempotent(tmp_path):
    """A DB created before the OCR proxy columns existed gets them back-filled
    by apply_schema (ALTER TABLE ADD COLUMN when missing), idempotently."""
    import sqlite3

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE documents (document_id TEXT PRIMARY KEY, economy TEXT NOT NULL,"
        " source_sha256 TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO documents (document_id, economy, source_sha256, created_at)"
        " VALUES ('doc_old', 'SG', ?, ?)",
        ("b" * 64, utc_now_iso()),
    )
    conn.commit()
    conn.close()

    s = Storage(db)
    s.apply_schema()
    s.apply_schema()  # idempotent: no duplicate-column error on the second pass
    assert "mean_word_confidence" in _documents_columns(s)
    # the pre-existing row survives with nulls in the new columns
    meta = s.document_meta()
    assert meta["doc_old"]["mean_word_confidence"] is None
    # source_kind is back-filled the same way, but NOT to null: a
    # row written before the column existed can only have come from Discovery,
    # so the default states that rather than leaving it unanswerable.
    assert "source_kind" in _documents_columns(s)
    assert meta["doc_old"]["source_kind"] == "discovery"
    # notes: the secondary reference a Portal published beside a
    # Document. Null on every row that predates the column, which is the truth
    # about them, and present on both tables so Discovery can carry it across.
    assert "notes" in _documents_columns(s)
    assert [r["name"] for r in s.conn.execute("PRAGMA table_info(crawl_manifest)")].count(
        "notes"
    ) == 1
    # title: the name a Portal's own listing gives a Document, carried from
    # the manifest row to the Corpus row; null on every row that predates it.
    assert [r["name"] for r in s.conn.execute("PRAGMA table_info(crawl_manifest)")].count(
        "title"
    ) == 1
    s.close()


def test_document_meta_returns_pdf_path_and_ocr_proxies(storage):
    storage.upsert_document(
        "doc_ocr", "MY", "c" * 64,
        local_path="data/raw/MY/scanned.pdf",
        extractor="tesseract", extractor_version="5.3.4",
        ocr_applied=1, mean_word_confidence=0.9, dictionary_hit_rate=0.85,
        cer_proxy_flag=0, escalated_to_rapidocr=0, manual_review=0,
    )
    meta = storage.document_meta()["doc_ocr"]
    assert meta["local_path"] == "data/raw/MY/scanned.pdf"
    assert meta["extractor"] == "tesseract"
    assert meta["extractor_version"] == "5.3.4"
    assert meta["ocr_applied"] is True
    assert meta["mean_word_confidence"] == 0.9
    assert meta["cer_proxy_flag"] is False  # 0 -> False, never null
    assert meta["escalated_to_rapidocr"] is False


def test_document_meta_carries_economy_source_url_and_title(storage):
    """The export's off-corpus synthesis (crawl e2e, map-pdf) reads economy,
    source_url and title off document_meta to build a synthetic CorpusDoc."""
    storage.upsert_document(
        "doc_syn", "SG", "9" * 64,
        source_url="https://sso.agc.gov.sg/Act/XYZ?ViewType=Pdf",
        title="Some Act 2026",
    )
    meta = storage.document_meta()["doc_syn"]
    assert meta["economy"] == "SG"
    assert meta["source_url"] == "https://sso.agc.gov.sg/Act/XYZ?ViewType=Pdf"
    assert meta["title"] == "Some Act 2026"


def test_document_meta_born_digital_emits_nulls(storage):
    storage.upsert_document("doc_born", "SG", "d" * 64, extractor="pdfplumber")
    meta = storage.document_meta()["doc_born"]
    assert meta["ocr_applied"] is False  # column default 0
    assert meta["mean_word_confidence"] is None
    assert meta["cer_proxy_flag"] is None  # never populated -> null, not False


def test_document_meta_filters_by_document_ids(storage):
    storage.upsert_document("doc_a", "SG", "e" * 64)
    storage.upsert_document("doc_b", "AU", "f" * 64)
    meta = storage.document_meta(["doc_a"])
    assert set(meta) == {"doc_a"}


def test_audit_durations_sums_by_stage(storage):
    for stage, ms in (("m6_map", 100.0), ("m6_map", 50.0), ("m1_extract", 20.0)):
        with log_stage(storage, stage=stage, method="x"):
            pass
        storage.conn.execute(
            "UPDATE audit_log SET duration_ms = ? WHERE id = (SELECT MAX(id) FROM audit_log)",
            (ms,),
        )
    storage.conn.commit()
    durations = storage.audit_durations()
    assert durations["m6_map"] == 150.0
    assert durations["m1_extract"] == 20.0


def test_chunk_texts_slice_from_canonical_stream(storage):
    full = "PART 1\n1. First section text.\n2. Second section text.\n"
    storage.upsert_document(
        "doc_001", "SG", "sha_doc_001_full", full_text=full
    )
    storage.conn.execute(
        "INSERT INTO chunks (chunk_id, document_id, char_start, char_end,"
        " section_label, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("doc_001:c0001", "doc_001", 7, 29, "s. 1", utc_now_iso()),
    )
    storage.conn.commit()
    texts = storage.chunk_texts()
    assert texts["doc_001:c0001"] == full[7:29]


# ---------------------------------------------------------------------------
# The Corpus reader: which Documents a Run may read. The `runs` table and its
# Storage methods belong to the Run Record lane, covered in tests/test_run_records.py.
# ---------------------------------------------------------------------------


class TestCorpusDocuments:
    def test_only_documents_with_stored_content_are_in_the_corpus(self, storage):
        storage.upsert_document(
            "doc_sg_a", "SG", "sha_a", full_text="text", local_path="SG/raw/a.pdf",
            source_url="https://sso.agc.gov.sg/Act/A", language="English",
        )
        # a thin row (chunk attachment only): no text and no bytes on disk
        storage.upsert_document("doc_sg_thin", "SG", "sha_thin")
        storage.upsert_document(
            "doc_my_b", "MY", "sha_b", full_text="teks", local_path="MY/raw/b.pdf",
        )
        assert [r["document_id"] for r in storage.corpus_documents("SG")] == ["doc_sg_a"]
        assert [r["document_id"] for r in storage.corpus_documents("MY")] == ["doc_my_b"]
        assert storage.corpus_documents("AU") == []

    def test_the_language_and_source_url_come_back(self, storage):
        storage.upsert_document(
            "doc_sg_a", "SG", "sha_a", full_text="t", local_path="SG/raw/a.pdf",
            source_url="https://sso.agc.gov.sg/Act/A", language="English",
        )
        row = storage.corpus_documents("SG")[0]
        assert row["language"] == "English"
        assert row["source_url"] == "https://sso.agc.gov.sg/Act/A"

    def test_corpus_source_urls_answers_what_discovery_may_skip(self, storage):
        storage.upsert_document(
            "doc_sg_a", "SG", "sha_a", full_text="t",
            source_url="https://sso.agc.gov.sg/Act/A",
        )
        storage.upsert_document("doc_sg_b", "SG", "sha_b", full_text="t")  # no Source URL
        assert storage.corpus_source_urls("SG") == {"https://sso.agc.gov.sg/Act/A"}


# ---------------------------------------------------------------------------
# Several Runs at once: the pre-run job starts more than one `regcompass run`
# against one working database, so every connection the storage layer opens
# must let a second process write while the first one reads, and must wait
# its turn for the write lock rather than fail after SQLite's default 5 s.
# ---------------------------------------------------------------------------


class TestAConnectionIsReadyForSeveralProcesses:
    def test_the_journal_is_write_ahead(self, storage):
        mode = storage.conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"

    def test_a_writer_waits_at_least_a_minute_for_the_lock(self, storage):
        timeout_ms = storage.conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert timeout_ms >= 60_000

    def test_a_second_connection_gets_the_same_settings(self, tmp_path):
        first = Storage(tmp_path / "shared.db")
        first.apply_schema()
        second = Storage(tmp_path / "shared.db")
        try:
            assert second.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert second.conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 60_000
        finally:
            second.close()
            first.close()

    def test_closing_leaves_every_write_in_the_main_file(self, tmp_path):
        """The database is copied as one file (into an image, into a hand-in),
        so a close folds the write-ahead log back in and leaves no log behind
        that the copy would need."""
        import shutil
        import sqlite3

        path = tmp_path / "copied.db"
        s = Storage(path)
        s.apply_schema()
        _insert_document(s)
        s.conn.commit()
        s.close()
        assert not (tmp_path / "copied.db-wal").exists() or (
            tmp_path / "copied.db-wal"
        ).stat().st_size == 0
        shutil.copyfile(path, tmp_path / "alone.db")
        conn = sqlite3.connect(tmp_path / "alone.db")
        try:
            n = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        finally:
            conn.close()
        assert n == 1

    def test_a_read_only_connection_still_opens_a_write_ahead_database(self, tmp_path):
        """The server and the pre-run job read the database with mode=ro."""
        import sqlite3

        path = tmp_path / "ro.db"
        s = Storage(path)
        s.apply_schema()
        _insert_document(s)
        s.conn.commit()
        s.close()
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        finally:
            conn.close()

    def test_a_reader_does_not_block_a_writer_in_another_connection(self, tmp_path):
        path = tmp_path / "busy.db"
        writer = Storage(path)
        writer.apply_schema()
        reader = Storage(path)
        try:
            reader.conn.execute("BEGIN")
            reader.conn.execute("SELECT COUNT(*) FROM documents").fetchone()
            _insert_document(writer)
            writer.conn.commit()  # a rollback journal would wait here, then fail
            assert writer.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        finally:
            reader.conn.rollback()
            reader.close()
            writer.close()
