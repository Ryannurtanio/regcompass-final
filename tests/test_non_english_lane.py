"""The non-English lane end to end: OCR in the Document's Language, the
meaning-only Gate, and verified Mappings out of a Lao statute.

The threading tests (which tesseract string, which Gate rule) run everywhere on
a fake OCR function. The tests that actually read a scan need the tesseract
binary and the vendored traineddata, and skip cleanly without them, exactly as
tests/test_ocr.py does.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from regcompass.contracts import CanonicalText, OcrQuality, PageSpan, PipelineConfig
from regcompass.engines import fake_completion, fake_embed, resolve_engine
from regcompass.languages import ocr_policy
from regcompass.ocr import ocr_quality_for
from regcompass.pipeline import run_document
from regcompass.storage import Storage

ROOT = Path(__file__).resolve().parents[1]
LAO_FULL = (
    ROOT / "tests/fixtures/sample_legislation/domestic_language"
    / "Lao PDR-Law on Electronic Transaction (Amended) No. 31.pdf"
)
LAO_SLICE = ROOT / "tests/fixtures/derived/lao_electronic_transactions_p05_p06.pdf"
TELECOM = ROOT / "tests/fixtures/sample_legislation/born_digital/Telecommunications Act 1999.pdf"

FAKE_ENGINE = resolve_engine("fake")
PILLARS_67 = (6, 7)

needs_tesseract = pytest.mark.skipif(
    shutil.which("tesseract") is None, reason="tesseract binary not on PATH"
)


# ---------------------------------------------------------------------------
# which tesseract string a Document asks for, and who passes it
# ---------------------------------------------------------------------------


def spy_ocr(monkeypatch, text: str = "Section 5. A licensee shall keep records under this Act. " * 20):
    """Replace OCR with a recorder. run_document imports ocr_document inside
    the function, so patching the module attribute is what the real call sees
    (the same hook tests/test_e2e.py uses)."""
    import regcompass.ocr as ocr_mod

    seen: dict[str, object] = {}

    def fake_ocr(raw, doc_id, languages="eng", **kwargs):
        seen["languages"] = languages
        seen["policy"] = kwargs.get("policy")
        return CanonicalText(
            document_id=doc_id,
            source_sha256="a" * 64,
            extractor="tesseract",
            extractor_version="5.5.2",
            full_text=text,
            pages=[PageSpan(page_number=1, char_start=0, char_end=len(text))],
            ocr_applied=True,
            ocr_quality=OcrQuality(mean_word_confidence=0.8, dictionary_hit_rate=0.8),
        )

    monkeypatch.setattr("regcompass.pipeline.should_ocr", lambda c: True)
    monkeypatch.setattr(ocr_mod, "ocr_document", fake_ocr)
    return seen


class TestOcrLanguageComesFromTheDocument:
    @pytest.mark.parametrize(
        ("economy", "language", "expected", "latin"),
        [
            ("LA", "Lao", "lao+eng", False),
            ("ID", "Bahasa Indonesia", "ind+eng", True),
            ("TH", "Thai", "tha+eng", False),
            ("RU", "Russian", "rus+eng", False),
            ("SG", "English", "eng", True),
            ("AU", "English", "eng", True),
            ("MY", "English", "eng+msa", True),
            ("MY", "Other", "eng+msa", True),
            ("MY", None, "eng+msa", True),
        ],
    )
    def test_run_document_passes_the_derived_string(
        self, tmp_path, monkeypatch, economy, language, expected, latin
    ):
        seen = spy_ocr(monkeypatch)
        storage = Storage(tmp_path / "lang.db")
        storage.apply_schema()
        run_document(
            storage, f"doc_{economy.lower()}_x", TELECOM, economy, (7,), FAKE_ENGINE, "run_one",
            completion_fn=fake_completion, embed_fn=fake_embed, language=language,
        )
        assert seen["languages"] == expected
        assert seen["policy"].dictionary_proxy is latin

    def test_the_stage_row_records_the_language_string(self, tmp_path, monkeypatch):
        spy_ocr(monkeypatch)
        storage = Storage(tmp_path / "lang.db")
        storage.apply_schema()
        run_document(
            storage, "doc_la_x", LAO_SLICE, "LA", (7,), FAKE_ENGINE, "run_one",
            completion_fn=fake_completion, embed_fn=fake_embed, language="Lao",
        )
        methods = [
            r["method"] for r in storage.conn.execute(
                "SELECT method FROM audit_log WHERE stage = 'm2_ocr'"
            )
        ]
        assert methods == ["tesseract:lao+eng"]

    def test_ingest_economy_derives_it_too(self, tmp_path, monkeypatch):
        """Discovery's ingest path takes the same derivation, so a Corpus is
        OCR'd in its own Language before a Run ever opens it."""
        import hashlib
        import shutil as sh

        import regcompass.ocr as ocr_mod
        from regcompass.shortlist import ingest_economy

        assert ocr_mod is not None  # imported for the reader: spy_ocr patches it
        seen = spy_ocr(monkeypatch)
        monkeypatch.setattr("regcompass.shortlist.should_ocr", lambda c: True)

        storage = Storage(tmp_path / "ingest.db")
        storage.apply_schema()
        data_dir = tmp_path / "data"
        raw_dir = data_dir / "LA" / "raw"
        raw_dir.mkdir(parents=True)
        content = LAO_SLICE.read_bytes()
        sha = hashlib.sha256(content).hexdigest()
        local = raw_dir / f"{sha[:12]}_lao_etl.pdf"
        sh.copyfile(LAO_SLICE, local)
        url = "https://laoofficialgazette.gov.la/etl.pdf"
        storage.manifest_add_pending(
            url, "LA", source_family="electronic_transactions", filename_hint="lao_etl.pdf"
        )
        storage.manifest_mark_fetched(
            url, http_status=200, method="httpx", sha256=sha,
            content_type="application/pdf", size_bytes=len(content),
            local_path=str(local.relative_to(data_dir)),
        )

        results, excluded = ingest_economy(storage, data_dir, "LA", language="Lao")
        assert not excluded, excluded
        assert results
        assert seen["languages"] == "lao+eng"
        assert seen["policy"].dictionary_proxy is False



# ---------------------------------------------------------------------------
# the OCR quality verdict, decided without a scan
# ---------------------------------------------------------------------------


class TestOcrQualityDecision:
    """ocr_quality_for is the whole verdict, so the three questions a script
    raises can be checked without a binary, a scan or a minute of OCR."""

    CFG = PipelineConfig()

    def test_a_language_with_no_vendored_data_is_always_manual_review(self):
        """"Other" outside Malaysia (Tetum, Portuguese, Khmer) has no vendored
        traineddata, so its pages are read as English whatever they say. A
        high mean confidence over misread glyphs must not pass as a clean read."""
        q = ocr_quality_for(0.97, None, self.CFG, ocr_policy("Other", "TL"), "eng", False)
        assert q.manual_review is True
        assert q.manual_review_reason is not None
        assert "Other" in q.manual_review_reason
        assert "eng" in q.manual_review_reason

    @pytest.mark.parametrize("language", ["Chinese", "Hindi", "Kazakh", "Mongolian", "Vietnamese"])
    def test_every_live_test_script_is_read_with_its_own_data(self, language):
        """Each of these is vendored now, so a good read is not forced to
        review; the measured proxies still decide."""
        policy = ocr_policy(language, "XX")
        assert policy.manual_review_reason is None
        q = ocr_quality_for(0.90, None, self.CFG, policy, "xxx+eng", False)
        assert q.manual_review_reason is None

    def test_chinese_still_escalates_to_rapidocr(self):
        """The escalation is a SEPARATE question from the English dictionary:
        RapidOCR reads Chinese, so a page tesseract reads with low confidence
        has a second reader."""
        policy = ocr_policy("Chinese", "CN")
        assert policy.rapidocr_escalation is True
        assert policy.dictionary_proxy is False

    @pytest.mark.parametrize("language", ["Lao", "Thai", "Russian", "Hindi", "Kazakh", "Mongolian"])
    def test_scripts_rapidocr_cannot_read_do_not_escalate(self, language):
        assert ocr_policy(language, "XX").rapidocr_escalation is False

    def test_a_vendored_non_latin_language_is_not_forced_to_review(self):
        policy = ocr_policy("Lao", "LA")
        assert policy.manual_review_reason is None
        q = ocr_quality_for(0.82, None, self.CFG, policy, "lao+eng", False)
        assert q.manual_review is False

    def test_low_confidence_still_flags_a_non_latin_document(self):
        q = ocr_quality_for(0.10, None, self.CFG, ocr_policy("Lao", "LA"), "lao+eng", False)
        assert q.manual_review is True
        assert q.manual_review_reason is None  # measured, not forced

    def test_the_english_verdict_is_unchanged(self):
        q = ocr_quality_for(0.94, 0.90, self.CFG, ocr_policy("English", "SG"), "eng", False)
        assert q.manual_review is False
        assert q.manual_review_reason is None
        assert q.dictionary_hit_rate == 0.90
        assert q.dictionary_hit_rate_note is None
        assert q.cer_proxy_flag is False


# ---------------------------------------------------------------------------
# the Gate rule a Run picks for a Document
# ---------------------------------------------------------------------------


class TestGateRuleComesFromTheDocument:
    def _run(self, tmp_path, monkeypatch, language, text, bm25_must_fail):
        import bm25s

        if bm25_must_fail:
            def boom(*a, **k):
                raise AssertionError("the keyword tier was built on a non-English Document")

            monkeypatch.setattr(bm25s, "BM25", boom)
        spy_ocr(monkeypatch, text=text)
        storage = Storage(tmp_path / "gate.db")
        storage.apply_schema()
        run_document(
            storage, "doc_x_1", TELECOM, "LA", PILLARS_67, FAKE_ENGINE, "run_one",
            completion_fn=fake_completion, embed_fn=fake_embed, language=language,
        )
        return [
            r["method"] for r in storage.conn.execute(
                "SELECT method FROM audit_log WHERE stage = 'm5_gate'"
            )
        ]

    def test_a_lao_document_never_builds_the_keyword_index(self, tmp_path, monkeypatch):
        lao = "ມາດຕາ 5 ຫຼັກການກ່ຽວກັບວຽກງານທຸລະກໍາທາງເອເລັກໂຕຣນິກ ໃຫ້ປະຕິບັດຕາມຫຼັກການ ດັ່ງນີ້. " * 20
        assert self._run(tmp_path, monkeypatch, "Lao", lao, True) == ["bge-m3"]

    def test_a_chinese_document_never_builds_the_keyword_index(self, tmp_path, monkeypatch):
        """China is one of the six chosen Economies. bm25s cannot tokenize a
        script that does not separate words, so the keyword tier must never be
        built for it: Chinese is absent from KEYWORD_TIER_LANGUAGES, and the
        script test in the Gate says the same thing from the bytes."""
        chinese = "第五条 处理个人信息应当遵循合法、正当、必要和诚信原则，不得通过误导、欺诈、胁迫等方式处理个人信息。" * 20
        assert self._run(tmp_path, monkeypatch, "Chinese", chinese, True) == ["bge-m3"]

    def test_an_english_document_keeps_both_tiers(self, tmp_path, monkeypatch):
        english = "Section 5. A licensee shall keep records of personal data under this Act. " * 20
        assert self._run(tmp_path, monkeypatch, "English", english, False) == ["bge-m3+bm25"]

    def test_a_document_with_no_language_keeps_both_tiers(self, tmp_path, monkeypatch):
        english = "Section 5. A licensee shall keep records of personal data under this Act. " * 20
        assert self._run(tmp_path, monkeypatch, None, english, False) == ["bge-m3+bm25"]


# ---------------------------------------------------------------------------
# the real thing: a scanned Lao statute to verified Mappings
# ---------------------------------------------------------------------------


def lao_run(storage_path: Path, pdf: Path) -> tuple[Storage, list]:
    storage = Storage(storage_path)
    storage.apply_schema()
    records = run_document(
        storage, "doc_la_etl", pdf, "LA", PILLARS_67, FAKE_ENGINE, "run_lao",
        completion_fn=fake_completion, embed_fn=fake_embed, language="Lao",
        source_url="https://laoofficialgazette.gov.la/etl.pdf",
        title="Law on Electronic Transaction (Amended) No. 31",
    )
    return storage, records


def assert_lao_mappings(storage: Storage, records: list) -> None:
    full_text = storage.conn.execute(
        "SELECT full_text FROM documents WHERE document_id = 'doc_la_etl'"
    ).fetchone()["full_text"]
    passed = [r for r in records if r.verification_status == "passed"]
    assert passed, "the Lao lane produced no verified Mapping"
    for record in passed:
        assert record.verbatim_quote in full_text, (
            "a verified quote must be byte-for-byte in the OCR stream"
        )
    # the quotes are Lao, not the English headers OCR also picks up
    from regcompass.languages import non_latin_share

    assert any(non_latin_share(r.verbatim_quote) > 0.5 for r in passed)


@needs_tesseract
class TestLaoStatuteEndToEnd:
    def test_two_pages_reach_verified_mappings(self, tmp_path):
        """The fast lane: two pages sliced from the canonical 29-page scan
        (tests/fixtures/derived, cut by scripts/make_derived_fixtures.py).
        OCR in Lao, chunked on the article_word profile, shortlisted by meaning
        alone, mapped by the fake Engine, verified byte for byte."""
        storage, records = lao_run(tmp_path / "lao.db", LAO_SLICE)
        assert_lao_mappings(storage, records)

    def test_the_ocr_quality_proxies_report_the_skipped_dictionary(self, tmp_path):
        from regcompass.ocr import ocr_document

        canonical = ocr_document(
            LAO_SLICE.read_bytes(), "doc_la_etl", languages="lao+eng",
            policy=ocr_policy("Lao", "LA"),
        )
        q = canonical.ocr_quality
        assert q is not None
        assert q.dictionary_hit_rate is None, "the English proxy must not read as 0.0"
        assert q.dictionary_hit_rate_note and "English" in q.dictionary_hit_rate_note
        assert q.escalated_to_rapidocr is False, "RapidOCR has no Lao model"
        assert q.mean_word_confidence > 0.5
        assert q.manual_review is False, "confidence alone decides on a non-Latin script"

    def test_the_evidence_pair_carries_the_reason(self, tmp_path):
        import json

        from regcompass.ocr import ocr_document

        evidence = tmp_path / "evidence"
        ocr_document(
            LAO_SLICE.read_bytes(), "doc_la_etl", languages="lao+eng",
            policy=ocr_policy("Lao", "LA"), evidence_dir=evidence,
        )
        saved = json.loads((evidence / "quality.json").read_text(encoding="utf-8"))
        assert saved["languages"] == "lao+eng"
        assert saved["dictionary_proxy"] is False
        assert saved["rapidocr_escalation"] is False
        assert saved["dictionary_hit_rate"] is None
        assert "English" in saved["dictionary_hit_rate_note"]

    @pytest.mark.slow
    def test_the_whole_29_page_statute(self, tmp_path):
        storage, records = lao_run(tmp_path / "lao_full.db", LAO_FULL)
        assert_lao_mappings(storage, records)


# ---------------------------------------------------------------------------
# China: a hand-uploaded statute to a submission file
#
# A Chinese scan arrives the way a judge would supply one - uploaded by hand,
# with its Source URL and law name typed in. The whole lane runs here on the OCR excerpt of the
# team's Personal Information Protection Law scan (verbatim, see
# tests/fixtures/FIXTURES.md) so it needs no binary and no scan of its own:
# chunk on the Chinese article profile, the meaning-only Gate, the fake Engine,
# byte-for-byte verification, a labelled Gloss, and the 13-column export.
# ---------------------------------------------------------------------------

CN_EXCERPT = ROOT / "tests/fixtures/ocr_reference/pipl_cn_chapter1_excerpt.txt"
CN_DOC_ID = "doc_cn_user_pipl"
CN_SOURCE_URL = "https://www.npc.gov.cn/npc/c2/c30834/202108/t20210820_313088.html"
CN_LAW_NAME = "Personal Information Protection Law of the People's Republic of China"


def cn_override():
    """The reviewer-supplied law name and Source URL a hand-uploaded Document
    carries."""
    from regcompass.contracts import CorpusDoc
    from regcompass.export import SyntheticDoc

    return {
        CN_DOC_ID: SyntheticDoc(
            CorpusDoc(
                economy="CN", law_name=CN_LAW_NAME,
                source_url=CN_SOURCE_URL, url_is_direct=True,
            ),
            law_name_mechanical=False, allow_any_host=True, manual_added=True,
        )
    }


def cn_run(tmp_path):
    """The upload lane, exactly as `map-pdf` and the UI drive it: one
    user-supplied file, the Economy's Language, one Pillar, two Indicators."""
    from regcompass.pipeline import export_from_db

    storage = Storage(tmp_path / "cn.db")
    storage.apply_schema()
    records = run_document(
        storage, CN_DOC_ID, CN_EXCERPT, "CN", (7,), FAKE_ENGINE, "run_cn",
        indicators=("7.2", "7.4"),
        completion_fn=fake_completion, embed_fn=fake_embed, language="Chinese",
        source_url=CN_SOURCE_URL, title=CN_LAW_NAME,
    )
    result = export_from_db(
        storage, tmp_path / "out", synthetic_docs=cn_override(), run_id="run_cn"
    )
    return storage, records, result


@pytest.fixture(scope="module")
def cn_result(tmp_path_factory):
    return cn_run(tmp_path_factory.mktemp("cn"))


class TestChineseStatuteEndToEnd:
    def test_the_statute_chunks_on_its_article_headings(self, cn_result):
        storage, _, _ = cn_result
        labels = [
            r["section_label"] for r in storage.conn.execute(
                "SELECT section_label FROM chunks WHERE document_id = ?"
                " ORDER BY char_start", (CN_DOC_ID,)
            )
        ]
        assert labels[0] == "Chapter 1 s. 1"
        assert "Chapter 2 s. 13" in labels
        assert len(labels) == 11, labels

    def test_every_verified_quote_is_chinese_and_byte_for_byte(self, cn_result):
        from regcompass.languages import non_latin_share

        storage, records, _ = cn_result
        full_text = storage.conn.execute(
            "SELECT full_text FROM documents WHERE document_id = ?", (CN_DOC_ID,)
        ).fetchone()["full_text"]
        passed = [r for r in records if r.verification_status == "passed"]
        assert passed, "the Chinese lane produced no verified Mapping"
        for record in passed:
            assert record.verbatim_quote in full_text
            assert non_latin_share(record.verbatim_quote) > 0.5

    def test_the_run_answered_only_the_two_requested_indicators(self, cn_result):
        _, records, _ = cn_result
        assert {r.indicator_id for r in records} == {"7.2", "7.4"}

    def test_the_export_is_green_and_reads_chinese(self, cn_result):
        import csv

        _, _, result = cn_result
        assert result.battery_failures == []
        with result.csv_path.open(encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        assert rows
        assert {r["Language of Source"] for r in rows} == {"Chinese"}
        # JC-1 as the final round flipped it: dotted numeric ids as text
        assert {r["Indicator ID"] for r in rows} <= {"7.1", "7.2", "7.3", "7.4", "7.5"}
        assert all(re.fullmatch(r"7\.\d", r["Indicator ID"]) for r in rows)

    def test_every_chinese_row_carries_a_labelled_english_rendering(self, cn_result):
        import csv

        from regcompass.contracts import GLOSS_LABEL, GLOSS_UNAVAILABLE

        _, _, result = cn_result
        with result.csv_path.open(encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        provision_rows = [r for r in rows if r["Verbatim Snippet"] != "No provision found"]
        assert provision_rows
        for row in provision_rows:
            cell = row["Verbatim English"]
            # Either a rendering or the statement that none could be produced;
            # both open with the label, and neither is ever blank.
            assert cell.startswith(GLOSS_LABEL), row["Indicator ID"]
            assert cell == GLOSS_UNAVAILABLE or cell[len(GLOSS_LABEL):].strip()

    def test_a_working_gloss_lane_counts_zero_missing_renderings(self, cn_result):
        _, _, result = cn_result
        assert result.glosses_unavailable == 0


class TestAFailedGlossLaneIsVisible:
    """A gloss lane that produced nothing for every quote still exports, and
    the rows say so one by one. Row by row is not enough: a reader scanning a
    green Export has no reason to open 24 cells, so the count is reported with
    the Export and narrated in the Run, where a total failure cannot be missed.
    """

    def _export_with_every_gloss_failed(self, tmp_path):
        from regcompass.pipeline import export_from_db

        storage, records, _ = cn_run(tmp_path)
        for record in records:
            if record.verification_status == "passed":
                storage.gloss_set(
                    run_id="run_cn", mapping_id=record.mapping_id, english=None,
                    engine="fake", source_language="zh",
                    uncertainty_flag="no-clear-translation-equivalent",
                )
        lines: list[str] = []
        result = export_from_db(
            storage, tmp_path / "out_failed", synthetic_docs=cn_override(),
            run_id="run_cn", progress=lines.append,
        )
        return result, lines

    def test_the_export_counts_the_rows_with_no_rendering(self, tmp_path):
        from regcompass.contracts import GLOSS_UNAVAILABLE

        result, _ = self._export_with_every_gloss_failed(tmp_path)
        assert result.battery_failures == []
        expected = sum(
            1 for r in result.rows if r.get("Verbatim English") == GLOSS_UNAVAILABLE
        )
        assert expected > 0, "the fixture should have produced provision rows"
        assert result.glosses_unavailable == expected

    def test_the_run_narrates_the_count(self, tmp_path):
        result, lines = self._export_with_every_gloss_failed(tmp_path)
        said = [ln for ln in lines if "no English rendering" in ln]
        assert said, lines
        assert str(result.glosses_unavailable) in said[0]


# ---------------------------------------------------------------------------
# a text layer that is not the Document's text goes to OCR
# ---------------------------------------------------------------------------

# The stored text layer of Lao PDR's Decree on Electronic Commerce No. 296: a
# legacy Lao font whose glyphs are mapped onto Latin letters.
LAO_LEGACY_FONT = (
    "iimijCSC;Jn1nsun\n"
    "rl\"l1Jf1iUl\"ljC9C;Jrllm;un CCJ.JlJ mlJ~ 2\"1EJ, rlilJCC';JrltJ:'jlJ ~1Jfo qi rl\"li.JtJ;Jrl\"l1J\n"
    "~fli CC;J::; ;;inf\\, lClEJUil~~9jU1ijC8C;Jll 'te1sun.\n"
    "JJinm 3 nil.J3:Vlt.Jioo,i1u\n"
    "1. ~ii, m.J\"lrnf):i tJn~u @D fltJn~u inr1,cDumu21EJ ~uii, @ muu;3mum,:imc;Sn\n"
) * 6


def garbage_layer_spy(monkeypatch):
    """The text layer comes back as the legacy-font garbage and OCR is a
    recorder; nothing forces the OCR decision, so the pipeline must make it."""
    import regcompass.pipeline as pipeline_mod

    def garbage_extract(raw, fmt, doc_id, engine="pdfplumber"):
        from regcompass.extract import ExtractionStats

        return CanonicalText(
            document_id=doc_id,
            source_sha256="b" * 64,
            extractor="pdfplumber",
            extractor_version="x",
            full_text=LAO_LEGACY_FONT,
            pages=[PageSpan(page_number=1, char_start=0, char_end=len(LAO_LEGACY_FONT))],
        ), ExtractionStats()

    seen = spy_ocr(monkeypatch)
    monkeypatch.setattr(pipeline_mod, "should_ocr", should_ocr_real())
    monkeypatch.setattr(pipeline_mod, "extract_with_stats", garbage_extract)
    return seen


def should_ocr_real():
    from regcompass.shortlist import should_ocr

    return should_ocr


class TestGarbageTextLayerGoesToOcr:
    def test_the_run_reads_a_legacy_font_pdf_with_ocr(self, tmp_path, monkeypatch):
        seen = garbage_layer_spy(monkeypatch)
        storage = Storage(tmp_path / "garbage.db")
        storage.apply_schema()
        lines: list[str] = []
        run_document(
            storage, "doc_la_decree_296", TELECOM, "LA", (7,), FAKE_ENGINE, "run_one",
            completion_fn=fake_completion, embed_fn=fake_embed, language="Lao",
            progress=lines.append,
        )
        assert seen["languages"] == "lao+eng"
        assert any("text layer unusable" in line and "Lao script" in line for line in lines)

    def test_the_same_layer_under_english_is_left_alone(self, tmp_path, monkeypatch):
        """Latin letters are what an English Document's layer should hold."""
        seen = garbage_layer_spy(monkeypatch)
        storage = Storage(tmp_path / "garbage.db")
        storage.apply_schema()
        run_document(
            storage, "doc_sg_x", TELECOM, "SG", (7,), FAKE_ENGINE, "run_one",
            completion_fn=fake_completion, embed_fn=fake_embed, language="English",
        )
        assert "languages" not in seen

    def test_a_stored_garbage_layer_is_read_again(self, tmp_path, monkeypatch):
        """A stream stored before garbage layers were recognised is not reused:
        the Document is read again and its OCR replaces it."""
        import hashlib

        from regcompass.extract import extraction_key, load_extraction, store_extraction
        from regcompass.languages import ocr_policy, tesseract_languages

        storage = Storage(tmp_path / "garbage.db")
        storage.apply_schema()
        raw = TELECOM.read_bytes()
        key = extraction_key(
            hashlib.sha256(raw).hexdigest(), ocr_languages=tesseract_languages("Lao", "LA"),
            policy=ocr_policy("Lao", "LA"), config=PipelineConfig(),
        )
        stale = CanonicalText(
            document_id="doc_la_decree_296", source_sha256=hashlib.sha256(raw).hexdigest(),
            extractor="pdfplumber", extractor_version="x", full_text=LAO_LEGACY_FONT,
            pages=[PageSpan(page_number=1, char_start=0, char_end=len(LAO_LEGACY_FONT))],
        )
        store_extraction(storage, key, stale, source_format="pdf")
        seen = garbage_layer_spy(monkeypatch)
        run_document(
            storage, "doc_la_decree_296", TELECOM, "LA", (7,), FAKE_ENGINE, "run_one",
            completion_fn=fake_completion, embed_fn=fake_embed, language="Lao",
        )
        assert seen["languages"] == "lao+eng"
        assert load_extraction(storage, key, "doc_la_decree_296").ocr_applied is True


# ---------------------------------------------------------------------------
# a Document with no headings at all still reaches the Gate, in passages
# ---------------------------------------------------------------------------

NOTICE = (
    "The Ministry reminds all service providers that personal information collected in the\n"
    "course of providing an online service shall be stored on servers located within the\n"
    "territory and shall not be transferred abroad without the approval of the Ministry.\n"
    "Providers shall notify the Ministry of any breach of personal data within seventy-two\n"
    "hours of its discovery, and shall keep a record of every transfer for five years.\n\n"
) * 8


class TestUnstructuredDocumentThroughTheRun:
    def test_passages_are_gated_mapped_and_cited(self, tmp_path):
        path = tmp_path / "notice.txt"
        path.write_text(NOTICE, encoding="utf-8")
        storage = Storage(tmp_path / "notice.db")
        storage.apply_schema()
        records = run_document(
            storage, "doc_xx_notice", path, "SG", (7,), FAKE_ENGINE, "run_notice",
            indicators=("7.2", "7.4"),
            completion_fn=fake_completion, embed_fn=fake_embed, language="English",
        )
        labels = [
            r["section_label"] for r in storage.conn.execute(
                "SELECT section_label FROM chunks WHERE document_id = ? ORDER BY char_start",
                ("doc_xx_notice",),
            )
        ]
        assert labels and all(re.fullmatch(r"Passage \d+", label) for label in labels)
        passed = [r for r in records if r.verification_status == "passed"]
        assert passed, "no passage reached the Engine"
        assert all(re.fullmatch(r"Passage \d+", r.section) for r in passed)


class TestGarbageLayerReadInItsEconomysScripts:
    def test_an_indian_gazette_filed_as_english_is_read_with_hindi(self, tmp_path, monkeypatch):
        """doc_in_H202344: a Hindi gazette whose Devanagari never reached the
        text layer, filed under English. OCR reads it with Hindi, which India
        publishes in, and not with English alone."""
        import regcompass.pipeline as pipeline_mod
        from regcompass.extract import ExtractionStats

        dropped = "] 417\n, ;\n1. (1) , ,\n(2) ,\n(i) ;\n(ii) ,\n418 [ 2\n(3) ,\n- -\n2. , ,\n" * 20

        def dropped_extract(raw, fmt, doc_id, engine="pdfplumber"):
            return CanonicalText(
                document_id=doc_id, source_sha256="b" * 64, extractor="pdfplumber",
                extractor_version="x", full_text=dropped,
                pages=[PageSpan(page_number=1, char_start=0, char_end=len(dropped))],
            ), ExtractionStats()

        seen = spy_ocr(monkeypatch)
        monkeypatch.setattr(pipeline_mod, "should_ocr", should_ocr_real())
        monkeypatch.setattr(pipeline_mod, "extract_with_stats", dropped_extract)
        storage = Storage(tmp_path / "in.db")
        storage.apply_schema()
        run_document(
            storage, "doc_in_h202344", TELECOM, "IN", (7,), FAKE_ENGINE, "run_one",
            completion_fn=fake_completion, embed_fn=fake_embed, language="English",
        )
        assert seen["languages"] == "hin+eng"
        assert seen["policy"].rapidocr_escalation is False
