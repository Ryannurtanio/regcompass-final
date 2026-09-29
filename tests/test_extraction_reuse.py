"""A Run reads a Document's text once and reuses it.

Extraction (and OCR above all) is the most expensive non-Engine work a Run
does: on the Malaysia Corpus 1,808 seconds of a 1,823-second Run were tesseract
reading seven scanned Acts for the fourth time. The text those Acts produce
depends on exactly four things - the Document's bytes, the extraction logic, the
tesseract language data it was read with, and the OCR quality ladder for its
script - so a stream stored under all four is the stream this Run would have
produced, and reusing it changes nothing about the answer.

These tests hold that claim from both ends: the reused Run produces a
byte-identical Evidence Export, and a change to ANY key part re-reads the
Document rather than serving a stale stream.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from regcompass import extract as extract_mod
from regcompass import ocr as ocr_mod
from regcompass import pipeline as pipeline_mod
from regcompass import shortlist as shortlist_mod
from regcompass.contracts import CanonicalText, PageSpan, PipelineConfig
from regcompass.engines import fake_completion, fake_embed, resolve_engine
from regcompass.extract import (
    EXTRACTOR_VERSION,
    extraction_key,
    load_extraction,
    store_extraction,
)
from regcompass.languages import ocr_policy, tesseract_languages
from regcompass.pipeline import export_from_db, run_economy
from regcompass.shortlist import ingest_economy
from regcompass.storage import Storage

from corpus_fixtures import ROOT, seed_corpus  # noqa: E402
from test_shortlist import add_fetched_doc  # noqa: E402

FAKE = resolve_engine("fake")
ECONOMY = "SG"
PILLARS = (7,)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def build_corpus(root):
    """One seeded Corpus, the way Discovery builds one."""
    storage = Storage(root / "regcompass.db")
    storage.apply_schema()
    doc_ids = seed_corpus(storage, root / "data", ECONOMY)
    return storage, root / "data", doc_ids


def forget_extractions(storage: Storage) -> None:
    """Empty the cache, which is what every database looked like before this
    cache: the Run reads the bytes itself."""
    storage.conn.execute("DELETE FROM extractions")
    storage.conn.commit()


def do_run(storage, data_dir):
    return run_economy(
        storage, ECONOMY, pillars=PILLARS, engine=FAKE,
        completion_fn=fake_completion, embed_fn=fake_embed, data_dir=data_dir,
    )


def extraction_methods(storage: Storage, stage: str = "m1_extract") -> list[str]:
    return [
        r["method"]
        for r in storage.conn.execute(
            "SELECT method FROM audit_log WHERE stage = ? ORDER BY id", (stage,)
        ).fetchall()
    ]


def run_record_details(storage: Storage, run_id: str) -> dict:
    """One Run Record's details column, decoded."""
    row = storage.conn.execute(
        "SELECT details FROM runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    return json.loads(row["details"])


def run_extraction_details(storage: Storage, run_id: str) -> dict:
    """What the Run Record says about each Document's text: reused or extracted."""
    return run_record_details(storage, run_id)["extraction"]


# ---------------------------------------------------------------------------


class TestTheKeyCarriesEverythingThatChangesTheText:
    def test_the_same_four_parts_give_the_same_key(self):
        a = extraction_key("a" * 64, ocr_languages="eng", policy=ocr_policy("English"))
        b = extraction_key("a" * 64, ocr_languages="eng", policy=ocr_policy("English"))
        assert a.digest == b.digest

    def test_different_bytes_give_a_different_key(self):
        a = extraction_key("a" * 64, ocr_languages="eng", policy=ocr_policy("English"))
        b = extraction_key("b" * 64, ocr_languages="eng", policy=ocr_policy("English"))
        assert a.digest != b.digest

    def test_different_ocr_languages_give_a_different_key(self):
        a = extraction_key("a" * 64, ocr_languages="eng", policy=ocr_policy("English"))
        b = extraction_key("a" * 64, ocr_languages="tha", policy=ocr_policy("English"))
        assert a.digest != b.digest

    def test_a_different_ocr_policy_gives_a_different_key(self):
        english = ocr_policy("English")
        chinese = ocr_policy("Chinese")
        assert english != chinese
        a = extraction_key("a" * 64, ocr_languages="eng", policy=english)
        b = extraction_key("a" * 64, ocr_languages="eng", policy=chinese)
        assert a.digest != b.digest

    def test_a_changed_rasterization_dpi_gives_a_different_key(self):
        english = ocr_policy("English")
        a = extraction_key("a" * 64, ocr_languages="eng", policy=english)
        b = extraction_key(
            "a" * 64, ocr_languages="eng", policy=english,
            config=PipelineConfig(ocr_dpi=PipelineConfig().ocr_dpi + 50),
        )
        assert a.digest != b.digest
        assert "dpi=" in a.ocr_policy_id

    def test_a_changed_escalation_floor_gives_a_different_key(self):
        english = ocr_policy("English")
        base = PipelineConfig()
        a = extraction_key("a" * 64, ocr_languages="eng", policy=english, config=base)
        b = extraction_key(
            "a" * 64, ocr_languages="eng", policy=english,
            config=PipelineConfig(ocr_escalation_min_confidence=base.ocr_escalation_min_confidence / 2),
        )
        c = extraction_key(
            "a" * 64, ocr_languages="eng", policy=english,
            config=PipelineConfig(ocr_escalation_min_dict_hit=base.ocr_escalation_min_dict_hit / 2),
        )
        assert len({a.digest, b.digest, c.digest}) == 3

    def test_no_config_means_the_defaults_the_engine_would_use(self):
        english = ocr_policy("English")
        a = extraction_key("a" * 64, ocr_languages="eng", policy=english)
        b = extraction_key(
            "a" * 64, ocr_languages="eng", policy=english, config=PipelineConfig()
        )
        assert a.digest == b.digest

    def test_bumping_the_version_constant_gives_a_different_key(self, monkeypatch):
        before = extraction_key("a" * 64, ocr_languages="eng", policy=ocr_policy("English"))
        monkeypatch.setattr(extract_mod, "EXTRACTOR_VERSION", f"{EXTRACTOR_VERSION}-test")
        after = extraction_key("a" * 64, ocr_languages="eng", policy=ocr_policy("English"))
        assert before.digest != after.digest

    def test_the_stack_version_names_the_libraries_that_produce_the_text(self):
        version = extract_mod.extraction_stack_version()
        assert version.startswith(f"{EXTRACTOR_VERSION}+")
        assert "pdfplumber" in version


@pytest.fixture(scope="module")
def two_runs(tmp_path_factory):
    """One Corpus with an empty cache, run twice: the before-and-after of this
    cache inside a single database."""
    root = tmp_path_factory.mktemp("reuse")
    storage, data_dir, doc_ids = build_corpus(root)
    forget_extractions(storage)  # a Corpus as it looked before the cache existed
    first = do_run(storage, data_dir)
    second = do_run(storage, data_dir)
    return storage, root, doc_ids, first, second


class TestASecondRunReusesTheStoredText:
    def test_the_first_run_extracts_and_stores(self, two_runs):
        storage, _, doc_ids, first, _ = two_runs
        assert run_extraction_details(storage, first.run_id) == {
            d: "extracted" for d in doc_ids
        }
        stored = storage.conn.execute("SELECT COUNT(*) AS n FROM extractions").fetchone()
        assert stored["n"] == len(doc_ids)

    def test_the_second_run_reuses_it(self, two_runs):
        storage, _, doc_ids, _, second = two_runs
        assert run_extraction_details(storage, second.run_id) == {
            d: "reused" for d in doc_ids
        }

    def test_the_run_record_carries_the_key_it_looked_up(self, two_runs):
        storage, _, doc_ids, first, second = two_runs
        extracted = run_record_details(storage, first.run_id)["extraction_keys"]
        reused = run_record_details(storage, second.run_id)["extraction_keys"]
        assert set(extracted) == set(doc_ids)
        assert extracted == reused  # the same stream, so the same key
        assert all(len(k) == 64 for k in reused.values())
        methods = extraction_methods(storage)
        assert f"reuse:{reused[doc_ids[0]]}" in methods

    def test_the_audit_trail_still_has_one_m1_row_per_document_and_names_the_key(
        self, two_runs
    ):
        storage, _, doc_ids, _, _ = two_runs
        methods = extraction_methods(storage)
        # One m1_extract row per Document per Run, reused or not (the ingest
        # logs its own m11_ingest row and is not counted here).
        assert len(methods) == 2 * len(doc_ids)
        reuse = [m for m in methods if m.startswith("reuse:")]
        assert len(reuse) == len(doc_ids)
        assert all(len(m.split("reuse:")[1]) == 64 for m in reuse)

    def test_both_runs_export_the_same_bytes(self, two_runs):
        storage, root, _, first, second = two_runs
        a = export_from_db(storage, root / "out_first", run_id=first.run_id)
        b = export_from_db(storage, root / "out_second", run_id=second.run_id)
        assert a.csv_path.read_bytes() == b.csv_path.read_bytes()
        assert a.battery_failures == b.battery_failures

    def test_the_second_run_produced_the_same_chunks(self, two_runs):
        storage, _, _, first, second = two_runs
        assert first.n_chunks == second.n_chunks
        assert first.n_pairs_gated == second.n_pairs_gated
        assert first.n_passed == second.n_passed


class TestAChangedKeyPartForcesReExtraction:
    def test_a_bumped_version_constant_re_extracts(self, tmp_path, monkeypatch):
        storage, data_dir, doc_ids = build_corpus(tmp_path)
        forget_extractions(storage)
        do_run(storage, data_dir)
        monkeypatch.setattr(extract_mod, "EXTRACTOR_VERSION", f"{EXTRACTOR_VERSION}-test")
        second = do_run(storage, data_dir)
        assert set(run_extraction_details(storage, second.run_id).values()) == {"extracted"}
        rows = storage.conn.execute("SELECT COUNT(*) AS n FROM extractions").fetchone()
        assert rows["n"] == 2 * len(doc_ids)  # both versions kept, neither served wrong

    def test_a_changed_document_language_re_extracts(self, tmp_path):
        storage, data_dir, doc_ids = build_corpus(tmp_path)
        forget_extractions(storage)
        do_run(storage, data_dir)
        # The Language picks the tesseract data and the OCR ladder, so a
        # corrected Language is a different reading of the same bytes.
        storage.conn.execute("UPDATE documents SET language = 'Chinese'")
        storage.conn.commit()
        second = do_run(storage, data_dir)
        assert set(run_extraction_details(storage, second.run_id).values()) == {"extracted"}

    def test_changed_bytes_re_extract(self, tmp_path):
        storage, data_dir, doc_ids = build_corpus(tmp_path)
        key = extraction_key(
            "0" * 64, ocr_languages="eng", policy=ocr_policy("English")
        )
        assert load_extraction(storage, key, doc_ids[0]) is None

    def test_a_stored_stream_is_returned_under_the_asking_documents_id(self, tmp_path):
        storage, _, _ = build_corpus(tmp_path)
        key = extraction_key("f" * 64, ocr_languages="eng", policy=ocr_policy("English"))
        canonical = CanonicalText(
            document_id="written_as",
            source_sha256="f" * 64,
            extractor="pdfplumber",
            extractor_version="0.0.0",
            full_text="one two three",
            pages=[PageSpan(page_number=1, char_start=0, char_end=13)],
        )
        store_extraction(storage, key, canonical, source_format="pdf")
        back = load_extraction(storage, key, "read_as")
        assert back is not None
        assert back.document_id == "read_as"
        assert back.full_text == canonical.full_text
        assert back.pages == canonical.pages


class TestTheOcrLaneIsReusedToo:
    """The stage that costs the half hour: a reused OCR keeps its own audit row
    so the trail of a reused Run has the same shape as the trail of a fresh one."""

    @pytest.fixture()
    def scanned_run(self, tmp_path, monkeypatch):
        storage, data_dir, doc_ids = build_corpus(tmp_path)
        forget_extractions(storage)

        def fake_ocr(raw, document_id, languages="eng", policy=None, **kwargs):
            text = "Section 1. The scanned text of this Act, as OCR read it."
            return CanonicalText(
                document_id=document_id,
                source_sha256=hashlib.sha256(raw).hexdigest(),
                extractor="tesseract",
                extractor_version="5.5.2",
                full_text=text,
                pages=[PageSpan(page_number=1, char_start=0, char_end=len(text))],
                ocr_applied=True,
            )

        monkeypatch.setattr(ocr_mod, "ocr_document", fake_ocr)
        monkeypatch.setattr(pipeline_mod, "should_ocr", lambda canonical: True)
        first = do_run(storage, data_dir)
        second = do_run(storage, data_dir)
        return storage, doc_ids, first, second

    def test_the_first_run_ocrs_and_the_second_reuses(self, scanned_run):
        storage, doc_ids, first, second = scanned_run
        assert set(run_extraction_details(storage, first.run_id).values()) == {"extracted"}
        assert set(run_extraction_details(storage, second.run_id).values()) == {"reused"}

    def test_the_reused_run_still_has_one_m2_row_per_document(self, scanned_run):
        storage, doc_ids, _, _ = scanned_run
        methods = extraction_methods(storage, stage="m2_ocr")
        assert len(methods) == 2 * len(doc_ids)
        assert sum(1 for m in methods if m.startswith("reuse:")) == len(doc_ids)

    def test_the_reused_m2_row_keeps_the_same_decision(self, scanned_run):
        storage, _, _, _ = scanned_run
        decisions = [
            r["decision"]
            for r in storage.conn.execute(
                "SELECT decision FROM audit_log WHERE stage = 'm2_ocr' ORDER BY id"
            ).fetchall()
        ]
        assert len(set(decisions)) == 1
        assert decisions[0].startswith("ocr applied: ")


class TestTheOcrProvenanceIsRecordedButNeverKeyed:
    """Which tesseract read a scanned Act, and which language files it loaded,
    belong on the row. They stay OUT of the key so a database shipped inside an
    image keeps serving its streams there instead of OCRing everything again
    because the binary was rebuilt or the vendor directory was copied."""

    def test_an_ocr_row_names_the_engine_and_the_traineddata(
        self, tmp_path, monkeypatch
    ):
        storage, data_dir, _ = build_corpus(tmp_path)
        forget_extractions(storage)

        def fake_ocr(raw, document_id, languages="eng", policy=None, **kwargs):
            text = "Section 1. The scanned text, as OCR read it."
            return CanonicalText(
                document_id=document_id,
                source_sha256=hashlib.sha256(raw).hexdigest(),
                extractor="tesseract",
                extractor_version="5.5.2",
                full_text=text,
                pages=[PageSpan(page_number=1, char_start=0, char_end=len(text))],
                ocr_applied=True,
            )

        monkeypatch.setattr(ocr_mod, "ocr_document", fake_ocr)
        monkeypatch.setattr(pipeline_mod, "should_ocr", lambda canonical: True)
        do_run(storage, data_dir)
        row = storage.conn.execute(
            "SELECT ocr_engine_version, tessdata_sha256 FROM extractions"
        ).fetchone()
        assert row["ocr_engine_version"] == "5.5.2"
        fingerprint = json.loads(row["tessdata_sha256"])
        assert set(fingerprint) == {"eng"}
        assert len(fingerprint["eng"]) == 64

    def test_the_run_record_carries_the_same_provenance_either_way(
        self, tmp_path, monkeypatch
    ):
        storage, data_dir, doc_ids = build_corpus(tmp_path)
        forget_extractions(storage)

        def fake_ocr(raw, document_id, languages="eng", policy=None, **kwargs):
            text = "Section 1. The scanned text, as OCR read it."
            return CanonicalText(
                document_id=document_id,
                source_sha256=hashlib.sha256(raw).hexdigest(),
                extractor="tesseract",
                extractor_version="5.5.2",
                full_text=text,
                pages=[PageSpan(page_number=1, char_start=0, char_end=len(text))],
                ocr_applied=True,
            )

        monkeypatch.setattr(ocr_mod, "ocr_document", fake_ocr)
        monkeypatch.setattr(pipeline_mod, "should_ocr", lambda canonical: True)
        extracted = do_run(storage, data_dir)
        reused = do_run(storage, data_dir)
        first = run_record_details(storage, extracted.run_id)["extraction_provenance"]
        second = run_record_details(storage, reused.run_id)["extraction_provenance"]
        assert set(first) == set(doc_ids)
        assert first == second  # the reused Run reports what the read one did
        row = first[doc_ids[0]]
        assert row["ocr_engine_version"] == "5.5.2"
        assert set(json.loads(row["tessdata_sha256"])) == {"eng"}
        stored = storage.conn.execute(
            "SELECT ocr_engine_version, tessdata_sha256 FROM extractions"
        ).fetchone()
        assert row["ocr_engine_version"] == stored["ocr_engine_version"]
        assert row["tessdata_sha256"] == stored["tessdata_sha256"]

    def test_the_run_record_says_null_for_a_born_digital_document(self, tmp_path):
        storage, data_dir, doc_ids = build_corpus(tmp_path)
        report = do_run(storage, data_dir)
        provenance = run_record_details(storage, report.run_id)["extraction_provenance"]
        assert provenance == {d: None for d in doc_ids}

    def test_a_born_digital_row_claims_no_ocr_provenance(self, tmp_path):
        storage, data_dir, _ = build_corpus(tmp_path)
        row = storage.conn.execute(
            "SELECT ocr_applied, ocr_engine_version, tessdata_sha256 FROM extractions"
        ).fetchone()
        assert row["ocr_applied"] == 0
        assert row["ocr_engine_version"] is None
        assert row["tessdata_sha256"] is None

    def test_the_fingerprint_names_every_language_of_the_string(self):
        fingerprint = json.loads(ocr_mod.tessdata_fingerprint("eng+msa"))
        assert set(fingerprint) == {"eng", "msa"}
        assert all(len(v) == 64 for v in fingerprint.values())

    def test_an_unvendored_language_is_recorded_as_missing(self):
        fingerprint = json.loads(ocr_mod.tessdata_fingerprint("zho"))
        assert fingerprint == {"zho": "missing"}

    def test_changed_provenance_writes_the_same_row_rather_than_a_new_key(
        self, tmp_path, monkeypatch
    ):
        storage, _, _ = build_corpus(tmp_path)
        key = extraction_key("c" * 64, ocr_languages="eng", policy=ocr_policy("English"))
        text = "Section 1. A scanned provision."
        canonical = CanonicalText(
            document_id="doc",
            source_sha256="c" * 64,
            extractor="tesseract",
            extractor_version="5.5.2",
            full_text=text,
            pages=[PageSpan(page_number=1, char_start=0, char_end=len(text))],
            ocr_applied=True,
        )
        store_extraction(storage, key, canonical, source_format="pdf")
        monkeypatch.setattr(
            ocr_mod, "tessdata_fingerprint", lambda languages: '{"eng": "rebuilt"}'
        )
        store_extraction(storage, key, canonical, source_format="pdf")
        rows = storage.conn.execute(
            "SELECT tessdata_sha256 FROM extractions WHERE source_sha256 = ?",
            ("c" * 64,),
        ).fetchall()
        assert len(rows) == 1  # one key, whatever the provenance says
        assert rows[0]["tessdata_sha256"] == '{"eng": "rebuilt"}'
        assert load_extraction(storage, key, "doc") is not None

    def test_a_database_written_before_the_columns_gains_them(self, tmp_path):
        storage, data_dir, doc_ids = build_corpus(tmp_path)
        storage.conn.execute("DROP TABLE extractions")
        storage.conn.execute(
            "CREATE TABLE extractions (extraction_key TEXT PRIMARY KEY,"
            " source_sha256 TEXT NOT NULL, extractor_version TEXT NOT NULL,"
            " ocr_languages TEXT NOT NULL, ocr_policy_id TEXT NOT NULL,"
            " source_format TEXT NOT NULL, extractor TEXT NOT NULL,"
            " ocr_applied INTEGER NOT NULL DEFAULT 0, n_chars INTEGER NOT NULL,"
            " n_pages INTEGER NOT NULL, n_low_yield_pages INTEGER NOT NULL,"
            " canonical_json BLOB NOT NULL, created_at TEXT NOT NULL)"
        )
        storage.conn.commit()
        report = do_run(storage, data_dir)
        assert set(run_extraction_details(storage, report.run_id).values()) == {"extracted"}
        columns = {r["name"] for r in storage.conn.execute("PRAGMA table_info(extractions)")}
        assert {"ocr_engine_version", "tessdata_sha256"} <= columns
        rows = storage.conn.execute("SELECT COUNT(*) AS n FROM extractions").fetchone()
        assert rows["n"] == len(doc_ids)


class TestAnIngestAskedForOcrEvidenceAlwaysReadsTheDocument:
    """The OCR evidence pairs (a PNG and a text file per page) are written by
    the read itself, so an ingest that was asked for them must not be served a
    stored stream: the point of asking is the files, not the text."""

    def test_the_pairs_are_written_on_a_second_ingest_too(self, tmp_path):
        storage = Storage(tmp_path / "evidence.db")
        storage.apply_schema()
        data_dir = tmp_path / "data"
        scanned = (
            ROOT / "tests/fixtures/sample_legislation/scanned/Pakistan_PECA.pdf"
        ).read_bytes()
        url = add_fetched_doc(storage, data_dir, ECONOMY, "peca.pdf", scanned)
        assert url

        def ingest_into(evidence_root):
            results, excluded = ingest_economy(
                storage, data_dir, ECONOMY, evidence_root=evidence_root
            )
            assert excluded == [], excluded
            return results

        first_root = tmp_path / "evidence_first"
        first = ingest_into(first_root)
        assert len(first) == 1
        assert first[0].ocr_applied is True
        doc_id = first[0].document_id
        assert sorted(p.name for p in (first_root / doc_id).glob("page_*.png"))
        stored = storage.conn.execute("SELECT COUNT(*) AS n FROM extractions").fetchone()
        assert stored["n"] == 1  # the read was cached, as any other read is

        # The Document row is what makes ingest skip a Document, so removing it
        # is what an operator re-ingesting the same bytes looks like from here.
        storage.conn.execute("DELETE FROM documents WHERE document_id = ?", (doc_id,))
        storage.conn.commit()

        second_root = tmp_path / "evidence_second"
        second = ingest_into(second_root)
        assert len(second) == 1
        pngs = sorted(p.name for p in (second_root / doc_id).glob("page_*.png"))
        texts = sorted(p.name for p in (second_root / doc_id).glob("page_*.txt"))
        assert pngs == sorted(p.name for p in (first_root / doc_id).glob("page_*.png"))
        assert texts == sorted(p.name for p in (first_root / doc_id).glob("page_*.txt"))
        assert (second_root / doc_id / "quality.json").is_file()


class TestAPreTableDatabaseStillOpens:
    def test_a_database_without_the_table_runs_and_gains_it(self, tmp_path):
        storage, data_dir, doc_ids = build_corpus(tmp_path)
        storage.conn.execute("DROP TABLE extractions")
        storage.conn.commit()
        assert "extractions" not in storage.table_names()
        report = do_run(storage, data_dir)
        assert set(run_extraction_details(storage, report.run_id).values()) == {"extracted"}
        assert "extractions" in storage.table_names()
        rows = storage.conn.execute("SELECT COUNT(*) AS n FROM extractions").fetchone()
        assert rows["n"] == len(doc_ids)

    def test_a_read_against_a_missing_table_is_a_miss_not_an_error(self, tmp_path):
        storage = Storage(tmp_path / "bare.db")
        with pytest.raises(sqlite3.Error):
            storage.conn.execute("SELECT 1 FROM extractions")
        key = extraction_key("a" * 64, ocr_languages="eng", policy=ocr_policy("English"))
        assert load_extraction(storage, key, "doc") is None


class TestIngestStoresTheExtractionSoTheFirstRunIsFastToo:
    def test_seeding_the_corpus_stores_each_documents_text(self, tmp_path):
        storage, _, doc_ids = build_corpus(tmp_path)
        rows = storage.conn.execute(
            "SELECT source_sha256, n_chars FROM extractions"
        ).fetchall()
        assert len(rows) == len(doc_ids)
        assert all(r["n_chars"] > 0 for r in rows)

    def test_the_first_run_after_an_ingest_reuses_it(self, tmp_path):
        storage, data_dir, doc_ids = build_corpus(tmp_path)
        report = do_run(storage, data_dir)
        assert run_extraction_details(storage, report.run_id) == {
            d: "reused" for d in doc_ids
        }

    def test_the_ingest_key_is_the_key_the_run_asks_for(self, tmp_path):
        storage, _, _ = build_corpus(tmp_path)
        row = storage.corpus_documents(ECONOMY)[0]
        blob = sorted((tmp_path / "data" / ECONOMY / "raw").glob("*"))[0].read_bytes()
        key = extraction_key(
            hashlib.sha256(blob).hexdigest(),
            ocr_languages=tesseract_languages(row["language"], ECONOMY),
            policy=ocr_policy(row["language"], ECONOMY),
        )
        assert load_extraction(storage, key, row["document_id"]) is not None

    def test_a_failing_store_never_blocks_the_document(self, tmp_path, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("the cache is unwritable")

        monkeypatch.setattr(shortlist_mod, "store_extraction", boom)
        storage, data_dir, doc_ids = build_corpus(tmp_path)
        assert doc_ids
        assert len(storage.corpus_documents(ECONOMY)) == len(doc_ids)
        rows = storage.conn.execute("SELECT COUNT(*) AS n FROM extractions").fetchone()
        assert rows["n"] == 0
        # and the Run then reads the Documents itself, as it always could
        report = do_run(storage, data_dir)
        assert set(run_extraction_details(storage, report.run_id).values()) == {"extracted"}
