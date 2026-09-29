"""The page a Mapping cites is the page its Verbatim Quote sits on.

A Piece (chunk) can run over several PDF pages. The Mapping used to carry the
page its Piece STARTS on, so a quote from the third page of a long section sent
the reader (the audit label, the Evidence Export's "PDF: page N" and the Open
source link's #page=N) one or more pages early. What is pinned here:

  * the pure rule: the page holding the quote's first visible character, found
    from the quote's position inside the Piece plus the Piece's own offset in
    the Document's stored text;
  * the fallback, named: with no page table, or a quote that cannot be placed,
    the Piece's first page is kept (exactly what was stored before);
  * the Map step stores the quote's page, including after a Prove retry;
  * the repair over an existing database: moves only page_number, is
    idempotent, has a dry run, and touches no other field;
  * the export and the Open source link read the corrected page.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from regcompass.audit import build_source_link
from regcompass.chunk import quote_page
from regcompass.contracts import CanonicalText, Chunk, GatedChunk, MappingRecord, PageSpan
from regcompass.export import location_reference
from regcompass.extract import PAGE_SEPARATOR, extraction_key, store_extraction
from regcompass.storage import Storage

# ---------------------------------------------------------------------------
# a four-page Document whose second Piece runs from page 2 to page 4
# ---------------------------------------------------------------------------

PAGE_TEXTS = [
    "Section 1. Short title\nThis Act is the Data Act.",
    "Section 2. Transfer of data\n(1) A licensee must keep a register of transfers.",
    "(2) A licensee must not transfer customer data abroad without consent.",
    "(3) The Authority may exempt any class of transfers.\n(4) A person who",
    # page 5 is only here so the Piece ends before the stream does
    "Section 3. Offences\nA licensee who fails commits an offence.",
]


def _stream(texts: list[str]) -> tuple[str, list[PageSpan]]:
    spans: list[PageSpan] = []
    cursor = 0
    for i, text in enumerate(texts):
        if i:
            cursor += len(PAGE_SEPARATOR)
        spans.append(PageSpan(page_number=i + 1, char_start=cursor, char_end=cursor + len(text)))
        cursor += len(text)
    return PAGE_SEPARATOR.join(texts), spans


FULL_TEXT, PAGES = _stream(PAGE_TEXTS)
DOC_ID = "doc_sg_pages"
SHA = "a" * 64
CANONICAL = CanonicalText(
    document_id=DOC_ID,
    source_sha256=SHA,
    extractor="pdfplumber",
    extractor_version="0.0-test",
    full_text=FULL_TEXT,
    pages=PAGES,
)

PIECE_START = PAGES[1].char_start
PIECE_END = PAGES[4].char_start  # the Piece covers pages 2, 3 and 4
PIECE = Chunk.from_stream(
    CANONICAL,
    chunk_id=f"{DOC_ID}:c0001",
    char_start=PIECE_START,
    char_end=PIECE_END,
    section_label="s. 2",
    page_start=2,
    page_end=4,
)

Q_PAGE_2 = "A licensee must keep a register of transfers."
Q_PAGE_3 = "A licensee must not transfer customer data abroad without consent."
# starts on page 3 and runs over the break onto page 4
Q_SPANS_3_4 = "without consent.\n(3) The Authority may exempt"


def _page(quote: str, fallback: int | None = 2, pages=PAGES) -> int | None:
    return quote_page(pages, PIECE.text, PIECE.char_start, quote, fallback)


class TestQuotePageRule:
    def test_a_quote_on_a_later_page_than_its_piece_gets_the_later_page(self):
        assert _page(Q_PAGE_3) == 3

    def test_a_quote_on_the_pieces_first_page_is_unchanged(self):
        assert _page(Q_PAGE_2) == 2

    def test_a_quote_over_a_page_break_takes_the_page_it_starts_on(self):
        assert Q_SPANS_3_4 in PIECE.text
        assert _page(Q_SPANS_3_4) == 3

    def test_leading_whitespace_on_the_page_break_does_not_pull_the_page_back(self):
        # the separator newline at the end of page 3 belongs to no page; the
        # quote's first visible character is on page 4
        quote = "\n(3) The Authority may exempt"
        assert quote in PIECE.text
        assert _page(quote) == 4

    def test_a_quote_twice_in_one_piece_takes_its_first_occurrence(self):
        """The same first occurrence the audit view highlights (locate_quote)."""
        texts = [
            "Section 9. Records\n(1) A licensee must keep records.",
            "(2) Again: A licensee must keep records.",
        ]
        full, pages = _stream(texts)
        quote = "A licensee must keep records."
        assert full.count(quote) == 2
        assert quote_page(pages, full, 0, quote, 1) == 1
        # a Piece that starts after the first occurrence finds the second
        second_start = pages[1].char_start
        assert quote_page(pages, full[second_start:], second_start, quote, 2) == 2

    def test_a_piece_starting_partway_down_a_page(self):
        """The Piece's own offset, not its page's start, anchors the lookup."""
        texts = [
            "Front matter line.\nSection 4. Access\n(1) An officer may",
            "require production of any document.\n(2) An officer may seize data.",
        ]
        full, pages = _stream(texts)
        start = full.index("Section 4.")
        assert start > pages[0].char_start
        piece = full[start:]
        assert quote_page(pages, piece, start, "(1) An officer may", 1) == 1
        assert quote_page(pages, piece, start, "An officer may seize data.", 1) == 2

    def test_fallback_with_no_page_table_is_the_pieces_first_page(self):
        assert _page(Q_PAGE_3, pages=[]) == 2

    def test_fallback_when_the_quote_is_not_in_the_piece(self):
        assert _page("text that is nowhere in this Piece") == 2

    def test_fallback_for_an_empty_quote(self):
        # a no-evidence record carries an empty quote and keeps page_start
        assert _page("") == 2
        assert _page("", fallback=None) is None


# ---------------------------------------------------------------------------
# the Map step stores the quote's page
# ---------------------------------------------------------------------------


def _gated(indicator: str = "6.4") -> GatedChunk:
    return GatedChunk(
        chunk=PIECE,
        indicator_id=indicator,  # type: ignore[arg-type]
        cosine_pillar=0.5,
        bm25_indicator=1.0,
        gate_decision="passed",
    )


def _answer(quote: str, subsection=None) -> str:
    import json

    return json.dumps(
        {
            "maps_to_indicator": True,
            "verbatim_quote": quote,
            "subsection": subsection,
            "impact": "Consent required before transfer.",
        }
    )


def _scripted(responses: list[str]):
    calls: list[str] = []

    def fn(prompt: str, strict: bool) -> str:
        calls.append(prompt)
        return responses[min(len(calls) - 1, len(responses) - 1)]

    return fn


class TestMapStoresTheQuotesPage:
    def test_a_mapping_carries_the_page_its_quote_is_on(self):
        pytest.importorskip("litellm")
        from regcompass.map import map_gated_chunk

        out = map_gated_chunk(
            _gated(), "SG", completion_fn=_scripted([_answer(Q_PAGE_3)]), pages=PAGES
        )
        assert out.record is not None
        assert out.record.page_number == 3

    def test_without_a_page_table_the_mapping_keeps_the_pieces_first_page(self):
        """The named fallback: a caller that has no page table (the bundle
        lanes, an old checkpoint) stores what it always stored."""
        pytest.importorskip("litellm")
        from regcompass.map import map_gated_chunk

        out = map_gated_chunk(_gated(), "SG", completion_fn=_scripted([_answer(Q_PAGE_3)]))
        assert out.record is not None
        assert out.record.page_number == 2

    def test_a_prove_retry_stores_the_page_of_the_quote_it_kept(self):
        """The first answer cites a subsection the Piece does not carry, so
        Prove re-maps; the kept quote is on page 3, the first one was on 2."""
        pytest.importorskip("litellm")
        from regcompass.map import map_gated_chunk
        from regcompass.verify import verify_with_retry

        fn = _scripted([_answer(Q_PAGE_2, "(9)"), _answer(Q_PAGE_3, "(2)")])
        first = map_gated_chunk(_gated(), "SG", completion_fn=fn, pages=PAGES)
        assert first.record is not None and first.record.page_number == 2
        out = verify_with_retry(
            first.record, _gated(), "SG", completion_fn=fn, canonical=CANONICAL
        )
        assert out.outcome == "passed"
        assert out.record.verbatim_quote == Q_PAGE_3
        assert out.record.page_number == 3

    def test_the_run_path_hands_the_documents_page_table_to_map(self, tmp_path):
        pytest.importorskip("litellm")
        from regcompass.contracts import PipelineConfig
        from regcompass.engines import resolve_engine
        from regcompass.pipeline import RunReport, map_and_verify_pairs

        storage = Storage(tmp_path / "run.db")
        storage.apply_schema()
        engine = resolve_engine("fake")
        records = map_and_verify_pairs(
            storage,
            [_gated()],
            doc_id=DOC_ID,
            economy="SG",
            engine=engine,
            config=PipelineConfig(),
            report=RunReport(economy="SG", engine=engine.name),
            completion_fn=_scripted([_answer(Q_PAGE_3)]),
            canonical=CANONICAL,
        )
        assert [r.page_number for r in records] == [3]
        assert records[0].verification_status == "passed"


# ---------------------------------------------------------------------------
# the export and the Open source link read the corrected page
# ---------------------------------------------------------------------------


def _record(quote: str, page: int | None, indicator: str = "6.4") -> MappingRecord:
    return MappingRecord(
        mapping_id=f"{PIECE.chunk_id}::{indicator}",
        document_id=DOC_ID,
        chunk_id=PIECE.chunk_id,
        economy="SG",
        indicator_id=indicator,  # type: ignore[arg-type]
        indicator_name="Conditional flow regimes",
        section="s. 2",
        subsection="(2)",
        verbatim_quote=quote,
        page_number=page,
        impact="Consent required before transfer.",
        verification_status="passed",
        confidence=0.81,
        controlling_evidence=True,
    )


class TestExportAndOpenSourceShowTheCorrectedPage:
    def test_a_mapped_record_exports_and_links_to_the_quotes_page(self):
        pytest.importorskip("litellm")
        from regcompass.map import map_gated_chunk

        out = map_gated_chunk(
            _gated(), "SG", completion_fn=_scripted([_answer(Q_PAGE_3)]), pages=PAGES
        )
        assert out.record is not None
        ref = location_reference(out.record)
        assert ref == "PDF: page 3"
        link = build_source_link(
            document_id=DOC_ID,
            source_url="https://sso.agc.gov.sg/Act/DA2099",
            format_tag="pdf",
            location_reference=ref,
        )
        assert link.page == 3
        assert link.href == "https://sso.agc.gov.sg/Act/DA2099#page=3"


# ---------------------------------------------------------------------------
# the repair over an existing database
# ---------------------------------------------------------------------------

RUN = "run_pages_test"
OTHER_RUN = "run_pages_other"


def _seed(db: Path, *, with_extraction: bool = True) -> Storage:
    storage = Storage(db)
    storage.apply_schema()
    storage.upsert_document(
        DOC_ID, "SG", SHA, full_text=FULL_TEXT, extractor="pdfplumber",
        extractor_version="0.0-test", n_pages=len(PAGES),
    )
    storage.upsert_chunks([PIECE])
    if with_extraction:
        key = extraction_key(SHA, ocr_languages="eng")
        store_extraction(storage, key, CANONICAL, source_format="pdf")
    # stored the old way: every row carries its Piece's first page
    storage.upsert_mappings(
        [
            _record(Q_PAGE_3, 2, "6.4"),
            _record(Q_PAGE_2, 2, "7.1"),
            _record(Q_SPANS_3_4, 2, "7.5"),
        ],
        run_id=RUN,
    )
    storage.upsert_mappings([_record(Q_PAGE_3, 2, "6.4")], run_id=OTHER_RUN)
    # a no-evidence row: no quote, nothing to place, never touched
    no_evidence = MappingRecord(
        mapping_id=f"{PIECE.chunk_id}::6.1",
        document_id=DOC_ID,
        chunk_id=PIECE.chunk_id,
        economy="SG",
        indicator_id="6.1",
        indicator_name="Indicator 6.1",
        section="s. 2",
        verbatim_quote="",
        page_number=2,
        insufficient_evidence=True,
    )
    storage.upsert_mappings([no_evidence], run_id=RUN)
    return storage


def _rows(db: Path) -> dict[tuple[str, str], dict]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM mappings").fetchall()
    conn.close()
    out = {}
    for r in rows:
        d = dict(r)
        out[(d["run_id"], d["mapping_id"])] = d
    return out


class TestRepairPageNumbers:
    def test_the_storage_reads_the_documents_page_table(self, tmp_path):
        storage = _seed(tmp_path / "db.sqlite")
        assert storage.document_pages(DOC_ID) == PAGES

    def test_no_stored_stream_means_no_page_table(self, tmp_path):
        storage = _seed(tmp_path / "db.sqlite", with_extraction=False)
        assert storage.document_pages(DOC_ID) == []

    def test_a_stored_stream_of_different_text_is_not_this_documents(self, tmp_path):
        """The same bytes read by an older extractor can sit beside the current
        stream; only the stream whose text IS the Document's text has offsets
        that mean anything for its Pieces."""
        storage = _seed(tmp_path / "db.sqlite", with_extraction=False)
        other = CANONICAL.model_copy(update={"full_text": FULL_TEXT + " (old reading)"})
        store_extraction(storage, extraction_key(SHA, ocr_languages="eng"), other, source_format="pdf")
        assert storage.document_pages(DOC_ID) == []

    def test_dry_run_counts_and_changes_nothing(self, tmp_path):
        from regcompass.pipeline import repair_page_numbers

        db = tmp_path / "db.sqlite"
        storage = _seed(db)
        before = _rows(db)
        report = repair_page_numbers(storage, dry_run=True)
        assert (report.examined, report.moved, report.undetermined, report.no_quote) == (
            4, 3, 0, 1,
        )
        assert _rows(db) == before

    def test_repair_moves_only_page_number_and_is_idempotent(self, tmp_path):
        from regcompass.pipeline import repair_page_numbers

        db = tmp_path / "db.sqlite"
        storage = _seed(db)
        before = _rows(db)
        report = repair_page_numbers(storage)
        assert report.moved == 3
        assert sorted((m.mapping_id, m.old, m.new) for m in report.moves if m.run_id == RUN) == [
            (f"{PIECE.chunk_id}::6.4", 2, 3),
            (f"{PIECE.chunk_id}::7.5", 2, 3),
        ]
        after = _rows(db)
        assert after.keys() == before.keys()
        for key, row in after.items():
            changed = {c for c in row if row[c] != before[key][c]}
            assert changed <= {"page_number"}, (key, changed)
        assert after[(RUN, f"{PIECE.chunk_id}::6.4")]["page_number"] == 3
        assert after[(RUN, f"{PIECE.chunk_id}::7.1")]["page_number"] == 2
        assert after[(OTHER_RUN, f"{PIECE.chunk_id}::6.4")]["page_number"] == 3
        again = repair_page_numbers(storage)
        assert again.moved == 0
        assert _rows(db) == after

    def test_one_run_can_be_repaired_alone(self, tmp_path):
        from regcompass.pipeline import repair_page_numbers

        db = tmp_path / "db.sqlite"
        storage = _seed(db)
        report = repair_page_numbers(storage, run_id=OTHER_RUN)
        assert (report.examined, report.moved) == (1, 1)
        rows = _rows(db)
        assert rows[(RUN, f"{PIECE.chunk_id}::6.4")]["page_number"] == 2
        assert rows[(OTHER_RUN, f"{PIECE.chunk_id}::6.4")]["page_number"] == 3

    def test_a_mapping_that_cannot_be_placed_is_left_as_it_was(self, tmp_path):
        from regcompass.pipeline import repair_page_numbers

        db = tmp_path / "db.sqlite"
        storage = _seed(db, with_extraction=False)
        before = _rows(db)
        report = repair_page_numbers(storage)
        assert (report.moved, report.undetermined, report.no_quote) == (0, 4, 1)
        assert _rows(db) == before

    def test_the_repaired_row_exports_and_links_to_the_quotes_page(self, tmp_path):
        from regcompass.pipeline import repair_page_numbers

        storage = _seed(tmp_path / "db.sqlite")
        repair_page_numbers(storage)
        record = next(
            r for r in storage.load_mappings(run_id=RUN)
            if r.mapping_id.endswith("::6.4")
        )
        ref = location_reference(record)
        assert ref == "PDF: page 3"
        link = build_source_link(
            document_id=DOC_ID, source_url=None, format_tag="pdf", location_reference=ref,
        )
        assert link.href == f"/api/documents/{DOC_ID}/pdf#page=3"


class TestRepairCommand:
    def test_dry_run_then_real_run_then_nothing_left(self, tmp_path):
        from regcompass.cli import app

        db = tmp_path / "db.sqlite"
        _seed(db).close()
        runner = CliRunner()

        dry = runner.invoke(app, ["repair-pages", "--db", str(db), "--dry-run"])
        assert dry.exit_code == 0, dry.output
        assert "3 of 4 Mappings would move" in dry.output
        assert f"{PIECE.chunk_id}::6.4" in dry.output
        assert _rows(db)[(RUN, f"{PIECE.chunk_id}::6.4")]["page_number"] == 2

        real = runner.invoke(app, ["repair-pages", "--db", str(db)])
        assert real.exit_code == 0, real.output
        assert "3 of 4 Mappings moved" in real.output
        assert _rows(db)[(RUN, f"{PIECE.chunk_id}::6.4")]["page_number"] == 3

        again = runner.invoke(app, ["repair-pages", "--db", str(db)])
        assert again.exit_code == 0, again.output
        assert "0 of 4 Mappings moved" in again.output

    def test_one_run_by_id(self, tmp_path):
        from regcompass.cli import app

        db = tmp_path / "db.sqlite"
        _seed(db).close()
        out = CliRunner().invoke(app, ["repair-pages", "--db", str(db), "--run", OTHER_RUN])
        assert out.exit_code == 0, out.output
        assert "1 of 1 Mappings moved" in out.output

    def test_an_unknown_run_is_a_clear_error(self, tmp_path):
        from regcompass.cli import app

        db = tmp_path / "db.sqlite"
        _seed(db).close()
        for extra in ([], ["--dry-run"]):
            out = CliRunner().invoke(
                app, ["repair-pages", "--db", str(db), "--run", "run_nope", *extra]
            )
            assert out.exit_code == 2, out.output
            assert "Run run_nope not found" in out.output

    def test_dry_run_leaves_the_file_byte_for_byte(self, tmp_path):
        """No schema pass, no journal-mode switch, no write of any kind."""
        from regcompass.cli import app

        db = tmp_path / "db.sqlite"
        _seed(db).close()
        conn = sqlite3.connect(db)
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.close()
        before = db.read_bytes()
        out = CliRunner().invoke(app, ["repair-pages", "--db", str(db), "--dry-run"])
        assert out.exit_code == 0, out.output
        assert "3 of 4 Mappings would move" in out.output
        assert db.read_bytes() == before
        assert not (tmp_path / "db.sqlite-wal").exists()

    def test_a_missing_database_is_a_clear_error(self, tmp_path):
        from regcompass.cli import app

        out = CliRunner().invoke(app, ["repair-pages", "--db", str(tmp_path / "none.db")])
        assert out.exit_code == 2
        assert "no working database" in out.output
