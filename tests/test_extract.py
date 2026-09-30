"""M1 exit-criteria tests: page/word slice invariants,
determinism, >=90% coverage vs the independent pypdfium2 baseline, the OCR
flag lane, the pypdfium2 fallback lane, and the HTML lane against committed
sso.agc.gov.sg bytes."""

import hashlib
from pathlib import Path

import pytest

from regcompass.extract import (
    OCR_TRIGGER_CHARS,
    ExtractionError,
    extract,
    extract_with_stats,
    sniff_format,
)

FIXTURES = Path(__file__).parent / "fixtures"
SG_TELECOM = FIXTURES / "sample_legislation/born_digital/Telecommunications Act 1999.pdf"
MY_PDPA = FIXTURES / "sample_legislation/born_digital/PERSONAL DATA PROTECTION ACT 2010.pdf"
AU_COMPILATION = FIXTURES / "sample_legislation/born_digital/C2026C00098VOL01.pdf"
NIUE_VOLUME = FIXTURES / "sample_legislation/consolidated_volume/Niue-Legislation Volume 1.pdf"
PK_SCANNED = FIXTURES / "sample_legislation/scanned/Pakistan_PECA.pdf"
SSO_HTML = FIXTURES / "html/sso_agc_gov_sg_Act_TA1999.html"


@pytest.fixture(scope="module")
def sg(request):
    raw = SG_TELECOM.read_bytes()
    return raw, extract_with_stats(raw, "pdf", "doc_sg_telecom")


@pytest.fixture(scope="module")
def my(request):
    raw = MY_PDPA.read_bytes()
    return raw, extract_with_stats(raw, "pdf", "doc_my_pdpa")


class TestDispatcher:
    def test_sniff_pdf(self):
        assert sniff_format(SG_TELECOM.read_bytes()) == "pdf"

    def test_sniff_html(self):
        assert sniff_format(SSO_HTML.read_bytes()) == "html"

    def test_mismatched_tag_rejected(self):
        with pytest.raises(ExtractionError, match="does not match"):
            extract(SG_TELECOM.read_bytes(), "html", "doc")

    def test_source_hash_is_of_exact_bytes(self, sg):
        raw, (canonical, _) = sg
        assert canonical.source_sha256 == hashlib.sha256(raw).hexdigest()


class TestPageSliceInvariant:
    def test_page_spans_slice_to_page_text(self, sg):
        _, (canonical, _) = sg
        # re-extract page texts independently to compare against slices
        import pdfplumber
        from io import BytesIO

        with pdfplumber.open(BytesIO(SG_TELECOM.read_bytes())) as pdf:
            for span, page in zip(canonical.pages, pdf.pages, strict=True):
                assert canonical.slice(span.char_start, span.char_end) == (page.extract_text() or "")

    def test_page_numbers_monotonic(self, sg):
        _, (canonical, _) = sg
        assert [p.page_number for p in canonical.pages] == list(range(1, len(canonical.pages) + 1))

    def test_spans_cover_stream_with_single_separators(self, sg):
        _, (canonical, _) = sg
        joined = "\n".join(canonical.slice(p.char_start, p.char_end) for p in canonical.pages)
        assert joined == canonical.full_text


class TestWordSliceInvariant:
    def test_every_word_slices_to_its_text(self, sg):
        _, (canonical, _) = sg
        assert canonical.words, "born-digital act must yield word boxes"
        for w in canonical.words:
            assert canonical.slice(w.char_start, w.char_end) == w.text

    @pytest.mark.parametrize("fixture_name", ["sg", "my"])
    def test_alignment_rate(self, fixture_name, request):
        _, (_, stats) = request.getfixturevalue(fixture_name)
        assert stats.alignment_rate >= 0.99, f"only {stats.alignment_rate:.2%} of words anchored"

    def test_word_boxes_have_positive_extent(self, sg):
        _, (canonical, _) = sg
        for w in canonical.words[:2000]:
            assert w.x1 > w.x0
            assert w.y1 > w.y0


class TestDeterminism:
    def test_two_runs_byte_identical_pdf(self):
        raw = MY_PDPA.read_bytes()
        a = extract(raw, "pdf", "doc_my_pdpa").model_dump_json()
        b = extract(raw, "pdf", "doc_my_pdpa").model_dump_json()
        assert a == b

    def test_two_runs_byte_identical_html(self):
        raw = SSO_HTML.read_bytes()
        a = extract(raw, "html", "doc_sso").model_dump_json()
        b = extract(raw, "html", "doc_sso").model_dump_json()
        assert a == b


class TestCoverage:
    """>=90% text coverage, measured against the OTHER engine (pypdfium2 is an
    independent PDFium-based extractor, our stand-in for the pdftotext baseline)."""

    @pytest.mark.parametrize("path,doc_id", [
        (SG_TELECOM, "doc_sg_telecom"),
        (MY_PDPA, "doc_my_pdpa"),
    ])
    def test_born_digital_coverage(self, path, doc_id):
        raw = path.read_bytes()
        primary = extract(raw, "pdf", doc_id, engine="pdfplumber")
        baseline = extract(raw, "pdf", doc_id, engine="pypdfium2")
        assert len(baseline.full_text) > 1000, "baseline extraction failed; coverage test is void"
        ratio = len(primary.full_text) / len(baseline.full_text)
        assert ratio >= 0.90, f"coverage {ratio:.2%} vs pypdfium2 baseline"

    @pytest.mark.slow
    def test_large_compilation_coverage(self):
        raw = AU_COMPILATION.read_bytes()
        primary = extract(raw, "pdf", "doc_au_comp", engine="pdfplumber")
        baseline = extract(raw, "pdf", "doc_au_comp", engine="pypdfium2")
        ratio = len(primary.full_text) / len(baseline.full_text)
        assert ratio >= 0.90, f"coverage {ratio:.2%} vs pypdfium2 baseline"


class TestOcrFlagLane:
    """Scanned pages are FLAGGED (< 50 chars/page), never OCR'd in M1."""

    def test_scanned_doc_flagged_not_ocrd(self):
        raw = PK_SCANNED.read_bytes()
        canonical = extract(raw, "pdf", "doc_pk_sro")
        n_pages = len(canonical.pages)
        assert len(canonical.low_yield_pages) >= n_pages * 0.8, (
            f"scanned doc: only {len(canonical.low_yield_pages)}/{n_pages} pages flagged"
        )
        assert canonical.ocr_applied is False
        assert canonical.ocr_quality is None

    def test_born_digital_mostly_unflagged(self, sg):
        _, (canonical, _) = sg
        assert len(canonical.low_yield_pages) <= len(canonical.pages) * 0.1


class TestFallbackLane:
    def test_pdfplumber_failure_falls_back_to_pypdfium2(self, monkeypatch):
        import pdfplumber

        def explode(*args, **kwargs):
            raise ValueError("simulated odd/encrypted PDF failure")

        monkeypatch.setattr(pdfplumber, "open", explode)
        canonical = extract(SG_TELECOM.read_bytes(), "pdf", "doc_sg_telecom")
        assert canonical.extractor == "pypdfium2"
        assert len(canonical.full_text) > 1000

    def test_explicit_pypdfium2_engine(self):
        canonical = extract(MY_PDPA.read_bytes(), "pdf", "doc_my_pdpa", engine="pypdfium2")
        assert canonical.extractor == "pypdfium2"
        assert "PERSONAL DATA PROTECTION" in canonical.full_text.upper()


class TestHtmlLane:
    def test_extracts_act_text_from_exact_fetched_bytes(self):
        raw = SSO_HTML.read_bytes()
        canonical = extract(raw, "html", "doc_sso_ta1999")
        assert canonical.extractor == "bs4-lxml"
        assert "Telecommunications Act 1999" in canonical.full_text
        assert "Short title" in canonical.full_text
        assert canonical.source_sha256 == hashlib.sha256(raw).hexdigest()

    def test_no_markup_in_stream(self):
        canonical = extract(SSO_HTML.read_bytes(), "html", "doc_sso_ta1999")
        for fragment in ("<div", "<span", "</", "<script"):
            assert fragment not in canonical.full_text

    def test_single_page_span_covers_stream(self):
        canonical = extract(SSO_HTML.read_bytes(), "html", "doc_sso_ta1999")
        assert len(canonical.pages) == 1
        span = canonical.pages[0]
        assert canonical.slice(span.char_start, span.char_end) == canonical.full_text


# ---------------------------------------------------------------------------
# Space-squash retry lane: PDF producers that set inter-word gaps below
# pdfplumber's default x_tolerance of 3 (SSO's Arbortext 2020-Ed consolidations
# measured ~2pt) fuse words into 30+ char runs. The lane detects the fusion
# mechanically and re-extracts once with the tight tolerance, keeping the
# retry only when it measures cleaner.
# ---------------------------------------------------------------------------


def _mk_pdf(content: bytes) -> bytes:
    """Minimal valid one-page PDF around a content stream (no dependencies)."""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref_pos = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (
        len(objs) + 1,
        xref_pos,
    )
    return bytes(out)


def _kerned_pdf(gap_thousandths: int) -> bytes:
    """Words separated by kerned gaps of gap_thousandths/1000 * 10pt."""
    gap = b" %d " % gap_thousandths
    words = gap.join(
        (b"(Consolidated)", b"(banking)", b"(provisions)", b"(concerning)", b"(disclosure)")
    )
    return _mk_pdf(b"BT /F1 10 Tf 72 700 Td [" + words + b"] TJ ET")


class TestSquashRetryLane:
    def test_squash_fraction_detector(self):
        from regcompass.extract import squash_fraction

        assert squash_fraction("") == 0.0
        assert squash_fraction("normal spaced prose here") == 0.0
        fused = "Atransferormayvoluntarilytransferthewholebusiness"
        assert squash_fraction(fused) == 1.0
        assert 0.4 < squash_fraction(fused + " " + "short words " * 4) < 0.8

    def test_tight_gaps_fuse_by_default_and_lane_unfuses(self):
        """-200/1000 * 10pt = 2pt gaps: under the default tolerance (3) the
        words fuse; the retry lane must fire and produce spaced text."""
        raw = _kerned_pdf(-200)
        canonical, stats = extract_with_stats(raw, "pdf", "doc_test_squash")
        assert stats.squash_retry is True
        assert "Consolidated banking provisions concerning disclosure" in canonical.full_text
        for w in canonical.words:
            assert canonical.full_text[w.char_start:w.char_end] == w.text

    def test_clean_pdf_never_enters_the_lane(self):
        """-600/1000 * 10pt = 6pt gaps: spaced under the default tolerance,
        so the stream must be produced by the pinned default settings."""
        raw = _kerned_pdf(-600)
        canonical, stats = extract_with_stats(raw, "pdf", "doc_test_clean")
        assert stats.squash_retry is False
        assert "Consolidated banking" in canonical.full_text

    def test_fixture_streams_untouched(self, sg, my):
        """The lane must never fire on the well-behaved fixture corpus: their
        golden streams define downstream ground truth."""
        assert sg[1][1].squash_retry is False
        assert my[1][1].squash_retry is False


# ---------------------------------------------------------------------------
# Two-column pages (Timor-Leste): the Jornal da República sets its acts in two
# columns. Read line by line, a page interleaves them and every sentence mixes
# two articles; for an Economy whose gazette prints columns, a page whose words
# leave a clear gutter is read left column first, then right.
# ---------------------------------------------------------------------------

LEFT = [
    "Artigo 1", "Objeto", "O presente diploma estabelece o regime",
    "juridico geral do comercio eletronico", "e das assinaturas eletronicas.",
    "Artigo 2", "Definicoes", "Para efeitos do presente diploma",
    "considera-se prestador de servicos", "qualquer pessoa singular ou coletiva.",
]
RIGHT = [
    "Artigo 3", "Ambito", "O presente diploma aplica-se aos", "prestadores estabelecidos em",
    "territorio nacional e no estrangeiro.", "Artigo 4", "Principios",
    "A atividade de comercio eletronico", "rege-se pelos principios da", "liberdade e da boa-fe.",
]


def _two_column_pdf() -> bytes:
    rows = []
    for i, (left, right) in enumerate(zip(LEFT, RIGHT)):
        y = 700 - 14 * i
        rows.append(b"BT /F1 9 Tf 72 %d Td (%s) Tj ET" % (y, left.encode()))
        rows.append(b"BT /F1 9 Tf 320 %d Td (%s) Tj ET" % (y, right.encode()))
    return _mk_pdf(b"\n".join(rows))


def _one_column_pdf() -> bytes:
    rows = [
        b"BT /F1 9 Tf 72 %d Td (%s) Tj ET" % (700 - 14 * i, (a + " " + b).encode())
        for i, (a, b) in enumerate(zip(LEFT, RIGHT))
    ]
    return _mk_pdf(b"\n".join(rows))


class TestTwoColumnPages:
    def test_line_by_line_interleaves_the_columns(self):
        text = extract(_two_column_pdf(), "pdf", "doc_test_cols").full_text
        assert "Artigo 1" in text and "Artigo 3" in text
        assert text.index("Artigo 3") < text.index("Artigo 2")

    def test_a_column_economy_reads_each_column_whole(self):
        canonical, _ = extract_with_stats(_two_column_pdf(), "pdf", "doc_test_cols", columns=True)
        text = canonical.full_text
        assert "\n".join(LEFT) in text and "\n".join(RIGHT) in text
        assert text.index("Artigo 2") < text.index("Artigo 3")
        for w in canonical.words:
            assert text[w.char_start:w.char_end] == w.text
        assert len(canonical.words) == len(extract(_two_column_pdf(), "pdf", "x").words)

    def test_a_one_column_page_is_read_as_before(self):
        raw = _one_column_pdf()
        plain = extract(raw, "pdf", "doc_test_one").full_text
        assert extract_with_stats(raw, "pdf", "doc_test_one", columns=True)[0].full_text == plain

    def test_only_timor_leste_reads_columns(self):
        from regcompass.extract import reads_columns

        assert reads_columns("TL")
        for economy in ("AU", "SG", "MY", "ID", "LA", "VN", "KZ", None):
            assert not reads_columns(economy)
