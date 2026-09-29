"""M2 exit-criteria tests: OCR both scanned fixtures with quality proxies
computed and persisted, word/page slice invariants on the OCR stream, the
RapidOCR escalation lane, the manual-review flag lane, evidence pairs, and
CER < 5% against the hand-checked reference excerpts (transcribed from the
rendered pages, see tests/fixtures/ocr_reference/)."""

import shutil
from pathlib import Path

import pytest

# A base-tier install must SKIP the OCR lane cleanly, never error at the
# fixtures: the imports are live-extra and the binary is a brew/apt install.
pytest.importorskip("pytesseract", reason="M2 OCR tests need the `live` extra (pytesseract)")
pytest.importorskip("rapidocr", reason="M2 OCR tests need the `live` extra (rapidocr)")
if shutil.which("tesseract") is None:
    pytest.skip("tesseract binary not on PATH", allow_module_level=True)

from regcompass.contracts import PipelineConfig  # noqa: E402
from regcompass.ocr import cer_against_reference, dictionary_hit_rate, ocr_document  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
PK_SCANNED = FIXTURES / "sample_legislation/scanned/Pakistan_PECA.pdf"
IN_SCANNED = FIXTURES / "sample_legislation/scanned/India-Public_Procurement_order_2017.pdf"
PK_REFERENCE = FIXTURES / "ocr_reference/pakistan_peca_page1_excerpt.txt"
IN_REFERENCE = FIXTURES / "ocr_reference/india_procurement_page1_excerpt.txt"

CER_MAX = PipelineConfig().ocr_cer_max


@pytest.fixture(scope="module")
def pk(tmp_path_factory):
    evidence = tmp_path_factory.mktemp("evidence_pk")
    canonical = ocr_document(PK_SCANNED.read_bytes(), "doc_pk_sro", evidence_dir=evidence)
    return canonical, evidence


@pytest.fixture(scope="module")
def india(tmp_path_factory):
    evidence = tmp_path_factory.mktemp("evidence_in")
    canonical = ocr_document(IN_SCANNED.read_bytes(), "doc_in_procurement", evidence_dir=evidence)
    return canonical, evidence


class TestTesseractPrimary:
    def test_text_extracted_and_marked_ocr(self, pk):
        canonical, _ = pk
        assert canonical.ocr_applied is True
        assert canonical.extractor == "tesseract"
        assert "GAZETTE OF PAKISTAN" in canonical.full_text
        assert "Prevention of Electronic Crimes Act" in canonical.full_text

    def test_quality_proxies_computed_and_persisted(self, pk):
        canonical, _ = pk
        q = canonical.ocr_quality
        assert q is not None
        assert 0.5 <= q.mean_word_confidence <= 1.0
        assert 0.5 <= q.dictionary_hit_rate <= 1.0
        assert q.escalated_to_rapidocr is False
        assert q.manual_review is False

    def test_word_slice_invariant_on_ocr_stream(self, pk):
        canonical, _ = pk
        assert canonical.words
        for w in canonical.words:
            assert canonical.slice(w.char_start, w.char_end) == w.text

    def test_page_spans_cover_stream(self, pk):
        canonical, _ = pk
        joined = "\n".join(canonical.slice(p.char_start, p.char_end) for p in canonical.pages)
        assert joined == canonical.full_text
        assert len(canonical.pages) == 4  # the PECA S.R.O. fixture is 4 scanned pages

    def test_boxes_in_pdf_points(self, pk):
        """Rasterized at 300 DPI but boxes must land in PDF point space (an A4-ish
        page is ~595 x 842 pt), matching M1's coordinate system for the overlay."""
        canonical, _ = pk
        for w in canonical.words:
            assert 0 <= w.x0 < w.x1 <= 1000
            assert 0 <= w.y0 < w.y1 <= 1200


class TestCer:
    def test_cer_under_5_percent_pakistan(self, pk):
        canonical, _ = pk
        page1 = canonical.slice(canonical.pages[0].char_start, canonical.pages[0].char_end)
        cer = cer_against_reference(PK_REFERENCE.read_text(encoding="utf-8"), page1)
        assert cer < CER_MAX, f"CER {cer:.3%} >= {CER_MAX:.0%} on hand-checked excerpt"

    def test_cer_under_5_percent_india(self, india):
        canonical, _ = india
        page1 = canonical.slice(canonical.pages[0].char_start, canonical.pages[0].char_end)
        cer = cer_against_reference(IN_REFERENCE.read_text(encoding="utf-8"), page1)
        assert cer < CER_MAX, f"CER {cer:.3%} >= {CER_MAX:.0%} on hand-checked excerpt"

    def test_cer_metric_sane(self):
        assert cer_against_reference("abc", "xx abc yy") == 0.0
        assert cer_against_reference("abc", "xx abd yy") == pytest.approx(1 / 3)
        assert cer_against_reference("hello world", "hello   world") == 0.0  # whitespace collapsed
        with pytest.raises(ValueError):
            cer_against_reference("", "anything")


class TestEvidencePairs:
    def test_pairs_saved_per_page(self, pk):
        canonical, evidence = pk
        pngs = sorted(evidence.glob("page_*.png"))
        txts = sorted(evidence.glob("page_*.txt"))
        assert len(pngs) == len(txts) == len(canonical.pages)
        for png in pngs:
            assert png.stat().st_size > 10_000  # a real raster, not an empty stub
        # the text side of the pair is the page text from the SAME engine pass
        page1 = canonical.slice(canonical.pages[0].char_start, canonical.pages[0].char_end)
        assert txts[0].read_text(encoding="utf-8") == page1


@pytest.fixture(scope="module")
def escalated():
    return ocr_document(PK_SCANNED.read_bytes(), "doc_pk_sro", force_escalation=True)


class TestEscalationLane:
    """Force-run the escalation on a fixture even though
    Tesseract passes it."""

    def test_escalation_fires_and_is_recorded(self, escalated):
        assert escalated.extractor == "tesseract+rapidocr"
        assert escalated.ocr_quality.escalated_to_rapidocr is True
        assert escalated.ocr_quality.cer_proxy_flag is True

    def test_escalated_text_is_real(self, escalated):
        assert "GAZETTE OF PAKISTAN" in escalated.full_text
        assert "Prevention of Electronic Crimes Act" in escalated.full_text

    def test_escalated_stream_keeps_invariants(self, escalated):
        for w in escalated.words:
            assert escalated.slice(w.char_start, w.char_end) == w.text


class TestManualReviewLane:
    def test_impossible_floor_forces_manual_review_not_silent_pass(self):
        """A document that still fails the proxies AFTER escalation must carry
        manual_review=True (flagged, never silently passed)."""
        cfg = PipelineConfig(
            ocr_escalation_min_confidence=1.0,  # force the escalation gate to fail
            ocr_manual_min_confidence=1.0,  # and the post-escalation floor too
        )
        canonical = ocr_document(PK_SCANNED.read_bytes(), "doc_pk_sro", config=cfg)
        q = canonical.ocr_quality
        assert q.escalated_to_rapidocr is True
        assert q.manual_review is True
        assert q.cer_proxy_flag is True


class TestDictionaryProxy:
    def test_statutory_words_hit(self):
        from regcompass.ocr import _load_wordlist

        wl = _load_wordlist()
        for w in ("government", "section", "act", "person", "regulations"):
            assert w in wl
        assert dictionary_hit_rate("the government may by regulations prescribe", wl) >= 0.8

    def test_garbage_misses(self):
        from regcompass.ocr import _load_wordlist

        assert dictionary_hit_rate("xqz vbnk wrtpl zzgh qqrst", _load_wordlist()) == 0.0


class TestDeterminism:
    def test_two_tesseract_runs_identical(self, pk):
        canonical, _ = pk
        again = ocr_document(PK_SCANNED.read_bytes(), "doc_pk_sro")
        assert again.model_dump_json() == canonical.model_dump_json()


class TestRapidOcrModelPin:
    """P0: the escalation engine's ONNX models ship INSIDE the pinned
    rapidocr wheel (no runtime download, fully offline); their digests are
    part of the ground-truth definition like every extractor version."""

    PINNED = {
        "PP-OCRv6_det_small.onnx": "090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f",
        "PP-OCRv6_rec_small.onnx": "6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884",
        "ch_ppocr_mobile_v2.0_cls_mobile.onnx": "e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c",
    }

    def test_bundled_models_match_pinned_digests(self):
        import hashlib
        from importlib import resources

        models = resources.files("rapidocr") / "models"
        found = {f.name: f for f in models.iterdir() if f.name.endswith(".onnx")}
        assert set(found) == set(self.PINNED)
        for name, want in self.PINNED.items():
            got = hashlib.sha256(found[name].read_bytes()).hexdigest()
            assert got == want, f"{name}: {got}"
