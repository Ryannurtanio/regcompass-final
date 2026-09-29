"""U0 audit backend: bundle loading over real golden pipeline outputs, quote
location by the M7 byte discipline, highlight rectangles from real WordBox
data on BOTH lanes (pdfplumber born-digital and Tesseract OCR), the
accepted-only export gate, and the FastAPI surface including SPA serving.

Review Decisions live in the working database and are tested in
tests/test_reviews.py; what this file still owns of them is the gate itself
(TestReviewGate, which drives export_all directly) and the frozen bundle's
refusal to take one.

Fallback lanes encoded here: a quote spanning OCR text
highlights from Tesseract-derived boxes; a rejected record never reaches the
export; a stream with no word boxes degrades to highlight_available=False,
never an error; an economy whose records are ALL rejected still earns its
absence rows. The keyboard-only flow is a frontend lane verified by the
Playwright end-to-end check at VERIFY (it has no backend surface).
"""

from __future__ import annotations

import csv
import gzip
import itertools
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from regcompass.audit import (
    AuditBundle,
    HighlightRect,
    locate_quote,
    rects_for_span,
)
from regcompass.contracts import CanonicalText, MappingRecord, Review
from regcompass.export import ABSENCE_MARKER, export_all

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "golden"
FIXTURE_PDFS = ROOT / "tests" / "fixtures" / "sample_legislation"

SLUG_SG = "sg_telecommunications_act_1999"
SLUG_MY = "my_personal_data_protection_act_2010"
SLUG_AU = "au_C2026C00098VOL01"
SLUG_PK = "pk_sro_221_2017_peca_powers"  # scanned; canonical stream is Tesseract OCR

PDFS = {
    SLUG_SG: FIXTURE_PDFS / "born_digital" / "Telecommunications Act 1999.pdf",
    SLUG_MY: FIXTURE_PDFS / "born_digital" / "PERSONAL DATA PROTECTION ACT 2010.pdf",
    SLUG_AU: FIXTURE_PDFS / "born_digital" / "C2026C00098VOL01.pdf",
}
TITLES = {
    SLUG_SG: "Telecommunications Act 1999",
    SLUG_MY: "Personal Data Protection Act 2010",
    SLUG_AU: "Criminal Code Act 1995 (compilation, volume 1)",
}
ECONOMIES = {SLUG_SG: "SG", SLUG_MY: "MY", SLUG_AU: "AU"}

COVERAGE = {
    "SG": {"law": "Telecommunications Act 1999", "sections": 98, "pairs_gated": 136},
    "MY": {"law": "Personal Data Protection Act 2010", "sections": 146, "pairs_gated": 166},
    "AU": {"law": "Criminal Code Act 1995", "sections": 536, "pairs_gated": 145},
}


def _gz(path: Path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def write_manifest(tmp_path: Path, slugs: list[str]) -> Path:
    """A real-data manifest: every referenced file is a committed golden
    pipeline output or a canonical fixture PDF, pointed at in place."""
    docs = []
    for slug in slugs:
        docs.append(
            {
                "document_id": f"doc_{slug}",
                "title": TITLES[slug],
                "economy": ECONOMIES[slug],
                "pdf": str(PDFS[slug]),
                "canonical": str(GOLDEN / "m1" / f"{slug}.json.gz"),
                "chunks": str(GOLDEN / "m4" / f"{slug}.chunks.json.gz"),
                "records": str(GOLDEN / "m8" / f"{slug}.reconciled.json.gz"),
                "gate": str(GOLDEN / "m5" / f"{slug}.gated.json.gz"),
            }
        )
    manifest = {"documents": docs, "coverage_stats": COVERAGE}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def sg_bundle(tmp_path_factory) -> AuditBundle:
    return AuditBundle(write_manifest(tmp_path_factory.mktemp("sg"), [SLUG_SG]))


@pytest.fixture(scope="module")
def full_bundle(tmp_path_factory) -> AuditBundle:
    return AuditBundle(
        write_manifest(tmp_path_factory.mktemp("full"), [SLUG_SG, SLUG_MY, SLUG_AU])
    )


@pytest.fixture(scope="module")
def ocr_canonical() -> CanonicalText:
    return CanonicalText.model_validate(_gz(GOLDEN / "m2" / f"{SLUG_PK}.json.gz"))


_REVIEW_SEQ = itertools.count(1)


def mk_review(
    mapping_id: str, status: str, comment: str | None = None, run_id: str = "run_test"
) -> Review:
    return Review(
        review_id=f"rev_{next(_REVIEW_SEQ):06d}",
        run_id=run_id,
        mapping_id=mapping_id,
        review_status=status,  # type: ignore[arg-type]
        reviewer="tester",
        reviewed_at=datetime(2026, 7, 6, tzinfo=timezone.utc),
        comment=comment,
    )


# ---------------------------------------------------------------------------
# bundle loading over real goldens
# ---------------------------------------------------------------------------


class TestBundle:
    def test_loads_real_golden_documents(self, sg_bundle):
        doc = sg_bundle.docs[f"doc_{SLUG_SG}"]
        assert doc.canonical.extractor == "pdfplumber"
        assert len(doc.canonical.words) > 10_000
        assert doc.records, "the m8 golden must yield passed records"
        assert all(r.verification_status == "passed" for r in doc.records)

    def test_document_summary_counts(self, sg_bundle):
        [summary] = sg_bundle.document_summaries({})
        doc = sg_bundle.docs[f"doc_{SLUG_SG}"]
        assert summary.n_records == len(doc.records)
        assert summary.n_pages == len(doc.canonical.pages)
        assert summary.ocr_applied is False
        assert summary.n_accepted == summary.n_rejected == summary.n_flagged == 0

    def test_record_summaries_carry_review_state(self, sg_bundle):
        doc_id = f"doc_{SLUG_SG}"
        first = sg_bundle.docs[doc_id].records[0]
        reviews = {first.mapping_id: mk_review(first.mapping_id, "flagged")}
        summaries = sg_bundle.record_summaries(doc_id, reviews)
        by_id = {s.mapping_id: s for s in summaries}
        assert by_id[first.mapping_id].review_status == "flagged"
        others = [s for s in summaries if s.mapping_id != first.mapping_id]
        assert all(s.review_status is None for s in others)

    def test_export_inputs_cover_all_records(self, full_bundle):
        records = full_bundle.all_records()
        texts = full_bundle.chunk_text_lookup()
        cosines = full_bundle.gate_cosine_lookup()
        assert {r.chunk_id for r in records} <= set(texts)
        assert {(r.chunk_id, r.indicator_id) for r in records} <= set(cosines)


# ---------------------------------------------------------------------------
# quote location: the M7 byte discipline, re-checked for every golden record
# ---------------------------------------------------------------------------


class TestLocateQuote:
    def test_every_golden_record_locates_inside_its_chunk(self, full_bundle):
        for doc in full_bundle.docs.values():
            for rec in doc.records:
                chunk = doc.chunks[rec.chunk_id]
                span = locate_quote(
                    doc.canonical.full_text,
                    rec.verbatim_quote,
                    chunk.char_start,
                    chunk.char_end,
                )
                assert span is not None, rec.mapping_id
                qs, qe = span
                assert doc.canonical.full_text[qs:qe] == rec.verbatim_quote
                assert chunk.char_start <= qs and qe <= chunk.char_end, rec.mapping_id

    def test_falls_back_to_whole_stream_outside_chunk_range(self, sg_bundle):
        doc = sg_bundle.docs[f"doc_{SLUG_SG}"]
        rec = doc.records[0]
        span = locate_quote(doc.canonical.full_text, rec.verbatim_quote, 0, 5)
        assert span is not None
        qs, qe = span
        assert doc.canonical.full_text[qs:qe] == rec.verbatim_quote

    def test_text_not_in_stream_returns_none(self, sg_bundle):
        doc = sg_bundle.docs[f"doc_{SLUG_SG}"]
        assert locate_quote(doc.canonical.full_text, "utterly absent paraphrase") is None
        assert locate_quote(doc.canonical.full_text, "") is None


# ---------------------------------------------------------------------------
# highlight rectangles: pdfplumber lane + Tesseract OCR lane
# ---------------------------------------------------------------------------


def _assert_sane_rects(rects: list[HighlightRect], page_lo: int, page_hi: int):
    assert rects
    for r in rects:
        assert page_lo <= r.page <= page_hi
        assert r.x1 > r.x0 and r.y1 > r.y0
        # PDF points, top-left origin: A4/Letter legal PDFs stay under 1000pt.
        assert 0 <= r.x0 < 1000 and 0 <= r.y0 < 1200


class TestHighlights:
    def test_born_digital_record_highlights(self, sg_bundle):
        doc = sg_bundle.docs[f"doc_{SLUG_SG}"]
        for rec in doc.records:
            detail = doc.detail(rec, None)
            assert detail.highlight_available, rec.mapping_id
            chunk = doc.chunks[rec.chunk_id]
            _assert_sane_rects(detail.highlights, chunk.page_start, chunk.page_end)

    def test_rects_are_line_merged_not_per_word(self, sg_bundle):
        doc = sg_bundle.docs[f"doc_{SLUG_SG}"]
        rec = max(doc.records, key=lambda r: len(r.verbatim_quote))
        detail = doc.detail(rec, None)
        n_words_in_span = len(
            [
                w
                for w in doc.canonical.words
                if w.char_end > detail.quote_char_start and w.char_start < detail.quote_char_end
            ]
        )
        assert len(detail.highlights) < n_words_in_span / 2

    def test_ocr_stream_quote_highlights_from_tesseract_boxes(self, ocr_canonical):
        """Fallback lane: a quote spanning OCR text. The stream,
        word boxes, and quote text are all real Tesseract output from the
        scanned Pakistan_PECA.pdf fixture; the quote is a verbatim slice."""
        assert ocr_canonical.ocr_applied and ocr_canonical.extractor == "tesseract"
        mid_word = ocr_canonical.words[len(ocr_canonical.words) // 2]
        qs = mid_word.char_start
        qe = min(qs + 120, len(ocr_canonical.full_text))
        quote = ocr_canonical.full_text[qs:qe]
        span = locate_quote(ocr_canonical.full_text, quote, qs - 10, qe + 10)
        assert span == (qs, qe)
        rects = rects_for_span(ocr_canonical.words, qs, qe)
        _assert_sane_rects(rects, 1, len(ocr_canonical.pages))

    def test_no_word_boxes_degrades_not_errors(self, sg_bundle):
        """Degraded-input lane: a stream with no word geometry still serves
        the record, with highlight_available False."""
        doc = sg_bundle.docs[f"doc_{SLUG_SG}"]
        rec = doc.records[0]
        stripped = doc.canonical.model_copy(update={"words": []})
        original = doc.canonical
        doc.canonical = stripped
        try:
            detail = doc.detail(rec, None)
        finally:
            doc.canonical = original
        assert detail.highlight_available is False
        assert detail.highlights == []
        assert detail.quote_char_start is not None  # quote still located

    def test_rects_for_span_empty_when_nothing_overlaps(self, ocr_canonical):
        assert rects_for_span(ocr_canonical.words, 10**9, 10**9 + 5) == []


# ---------------------------------------------------------------------------
# the accepted-only review gate in the export (wired into M9)
# ---------------------------------------------------------------------------


class TestReviewGate:
    def _export(self, bundle: AuditBundle, tmp_path: Path, reviews: dict[str, str]):
        return export_all(
            tmp_path,
            bundle.all_records(),
            chunk_text_lookup=bundle.chunk_text_lookup(),
            gate_cosine_lookup=bundle.gate_cosine_lookup(),
            coverage_stats=COVERAGE,
            liveness_fn=lambda url: True,
            reviews=reviews,
        )

    def _rows(self, result):
        with result.csv_path.open(encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))

    def test_only_accepted_records_enter_the_export(self, full_bundle, tmp_path):
        records = full_bundle.all_records()
        accepted = {r.mapping_id: "accepted" for r in records[: len(records) // 2]}
        rejected_one = records[-1]
        reviews = dict(accepted)
        reviews[rejected_one.mapping_id] = "rejected"
        result = self._export(full_bundle, tmp_path, reviews)
        rows = self._rows(result)
        substantive = [r for r in rows if r["Article / Section"] != ABSENCE_MARKER]
        accepted_quotes = {
            r.verbatim_quote for r in records if reviews.get(r.mapping_id) == "accepted"
        }
        assert substantive, "accepted records must ship"
        for row in substantive:
            assert row["Verbatim Snippet"] in accepted_quotes
        assert rejected_one.verbatim_quote not in {r["Verbatim Snippet"] for r in substantive}

    def test_unreviewed_and_flagged_are_excluded_too(self, full_bundle, tmp_path):
        records = full_bundle.all_records()
        reviews = {records[0].mapping_id: "accepted", records[1].mapping_id: "flagged"}
        result = self._export(full_bundle, tmp_path, reviews)
        rows = self._rows(result)
        substantive = [r for r in rows if r["Article / Section"] != ABSENCE_MARKER]
        assert len(substantive) == 1
        assert substantive[0]["Verbatim Snippet"] == records[0].verbatim_quote

    def test_fully_rejected_economy_still_earns_absence_rows(self, full_bundle, tmp_path):
        records = full_bundle.all_records()
        reviews = {
            r.mapping_id: ("rejected" if r.economy == "SG" else "accepted") for r in records
        }
        result = self._export(full_bundle, tmp_path, reviews)
        rows = self._rows(result)
        sg_rows = [r for r in rows if r["Economy"] == "Singapore"]
        assert sg_rows, "a searched economy never vanishes from the export"
        assert all(r["Article / Section"] == ABSENCE_MARKER for r in sg_rows)
        assert all(r["Verbatim Snippet"] == ABSENCE_MARKER for r in sg_rows)

    def test_no_reviews_argument_means_no_gate(self, full_bundle, tmp_path):
        result = export_all(
            tmp_path,
            full_bundle.all_records(),
            chunk_text_lookup=full_bundle.chunk_text_lookup(),
            gate_cosine_lookup=full_bundle.gate_cosine_lookup(),
            coverage_stats=COVERAGE,
            liveness_fn=lambda url: True,
        )
        rows = self._rows(result)
        substantive = [r for r in rows if r["Article / Section"] != ABSENCE_MARKER]
        assert len(substantive) == len(full_bundle.all_records())
        supp = json.loads(result.supplementary_path.read_text(encoding="utf-8"))
        assert "review_gate" not in supp

    def test_supplementary_records_the_gate(self, full_bundle, tmp_path):
        records = full_bundle.all_records()
        reviews = {records[0].mapping_id: "accepted", records[1].mapping_id: "rejected"}
        result = self._export(full_bundle, tmp_path, reviews)
        supp = json.loads(result.supplementary_path.read_text(encoding="utf-8"))
        gate = supp["review_gate"]
        assert gate["n_verified"] == len(records)
        assert gate["n_accepted"] == 1
        assert gate["n_rejected"] == 1
        assert gate["n_unreviewed"] == len(records) - 2
        assert "accepted" in gate["rule"]


# ---------------------------------------------------------------------------
# the FastAPI surface
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path) -> TestClient:
    """The one server in bundle mode: the frozen-bundle lane the judge runs
    with `regcompass serve --bundle <dir>`, which needs no key and no Run."""
    from regcompass.server import create_app

    manifest = write_manifest(tmp_path, [SLUG_SG, SLUG_MY, SLUG_AU])
    app = create_app(
        db_path=tmp_path / "unused.db",
        out_dir=tmp_path / "out",
        data_dir=tmp_path / "data",
        bundle_manifest=manifest,
        ui_dir=None,
    )
    return TestClient(app)


class TestApi:
    def test_documents_listing(self, client):
        docs = client.get("/api/documents").json()
        assert {d["document_id"] for d in docs} == {
            f"doc_{SLUG_SG}",
            f"doc_{SLUG_MY}",
            f"doc_{SLUG_AU}",
        }
        assert all(d["n_records"] > 0 for d in docs)

    def test_bundle_records_carry_parts_that_add_up_to_their_confidence(self, client):
        recs = client.get(f"/api/documents/doc_{SLUG_SG}/records").json()
        scored = [r for r in recs if r["confidence"] is not None]
        assert scored
        explained = [r for r in scored if r["confidence_parts"] is not None]
        assert explained, "the bundle lane computes Confidence from Gate cosines"
        for r in explained:
            assert len(r["confidence_parts"]) == 4
            assert round(sum(p["contribution"] for p in r["confidence_parts"]), 2) == r["confidence"]

    def test_record_detail_has_highlights(self, client):
        recs = client.get(f"/api/documents/doc_{SLUG_SG}/records").json()
        detail = client.get(f"/api/records/{recs[0]['mapping_id']}").json()
        assert detail["highlight_available"] is True
        assert detail["highlights"]
        assert detail["record"]["verbatim_quote"]

    def test_pdf_served(self, client):
        r = client.get(f"/api/documents/doc_{SLUG_SG}/pdf")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/pdf"
        assert r.content[:5] == b"%PDF-"

    def test_a_review_decision_is_refused_on_this_lane(self, client):
        """The bundle is a reading room: a Review Decision belongs to a Run in
        the working database, and there is none here. The refusal says so
        rather than writing a decision nobody could act on."""
        recs = client.get(f"/api/documents/doc_{SLUG_SG}/records").json()
        refused = client.post(
            "/api/reviews",
            json={"mapping_id": recs[0]["mapping_id"], "review_status": "accepted"},
        )
        assert refused.status_code == 409
        assert "working database" in refused.json()["detail"]
        assert client.get("/api/reviews").json() == {"run_id": None, "reviews": []}
        docs = {d["document_id"]: d for d in client.get("/api/documents").json()}
        assert docs[f"doc_{SLUG_SG}"]["n_accepted"] == 0
        detail = client.get(f"/api/records/{recs[0]['mapping_id']}").json()
        assert detail["review"] is None

    def test_unknown_ids_404(self, client):
        assert client.get("/api/documents/doc_nope/records").status_code == 404
        assert client.get("/api/records/nope").status_code == 404

    def test_export_is_ungated_on_the_frozen_bundle(self, client, tmp_path):
        """The bundle carries no Review Decisions, so its export ships what
        Round 1 shipped. The preview says so: gated is False."""
        preview = client.get("/api/export/preview").json()
        assert preview["gated"] is False
        assert preview["n_verified"] > 0

        summary = client.post("/api/export").json()
        csv_rows = list(csv.DictReader(Path(summary["csv_path"]).open(encoding="utf-8-sig")))
        substantive = [r for r in csv_rows if r["Article / Section"] != ABSENCE_MARKER]
        assert len(substantive) == summary["n_records_total"]
        # n_rows must be a CSV-parsed count: verbatim quotes embed newlines
        assert summary["n_rows"] == len(csv_rows)


class TestSpaServing:
    def _client(self, tmp_path, with_ui: bool) -> TestClient:
        from regcompass.server import create_app

        manifest = write_manifest(tmp_path, [SLUG_SG])
        ui = None
        if with_ui:
            ui = tmp_path / "dist"
            (ui / "assets").mkdir(parents=True)
            (ui / "index.html").write_text("<!doctype html><title>RegCompass audit</title>")
            (ui / "assets" / "app.js").write_text("// bundle")
        app = create_app(
            db_path=tmp_path / "unused.db",
            out_dir=tmp_path / "out",
            data_dir=tmp_path / "data",
            bundle_manifest=manifest,
            ui_dir=ui,
        )
        return TestClient(app)

    def test_index_and_spa_fallback(self, tmp_path):
        c = self._client(tmp_path, with_ui=True)
        assert "RegCompass audit" in c.get("/").text
        # client-side route: falls back to index.html, NOT a 404
        assert "RegCompass audit" in c.get("/documents/doc_x").text

    def test_missing_asset_404s_and_api_not_shadowed(self, tmp_path):
        c = self._client(tmp_path, with_ui=True)
        assert c.get("/assets/app.js").status_code == 200
        assert c.get("/assets/typo.js").status_code == 404
        assert c.get("/api/documents").status_code == 200

    def test_api_only_mode_without_bundle_dir(self, tmp_path):
        c = self._client(tmp_path, with_ui=False)
        assert c.get("/api/documents").status_code == 200


class TestShippedNotices:
    """P0 license gate: the committed UI bundle redistributes IBM Plex under
    the OFL 1.1, which requires the copyright notice and full license text to
    travel WITH the fonts - inside ui_dist, which also puts them in the wheel."""

    def test_ui_dist_ships_third_party_notices(self):
        from importlib import resources

        p = resources.files("regcompass") / "ui_dist" / "THIRD_PARTY_NOTICES.md"
        text = p.read_text(encoding="utf-8")
        assert "SIL OPEN FONT LICENSE" in text
        assert "Copyright 2019 IBM Corp" in text
        assert "pdfjs-dist" in text and "React" in text

    def test_bundle_fonts_are_actually_present(self):
        from importlib import resources

        assets = resources.files("regcompass") / "ui_dist" / "assets"
        names = [f.name for f in assets.iterdir()]
        assert any("ibm-plex" in n and n.endswith(".woff2") for n in names)
