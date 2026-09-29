"""Stage B/C: the end-to-end crawl lane (`regcompass e2e`), the map-pdf
registration lane, the OCR-proxy persistence gap, the drops command, and the
export's synthetic-corpus fallback for off-corpus documents.

Everything here runs OFFLINE and makes NO live calls: the crawl is driven by a
fake fetcher returning a committed fixture PDF, and the map/verify stages run on
the fake Engine (a verbatim quote parsed out of the prompt passes the
byte-for-byte check without Ollama).
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from regcompass.cli import app
from regcompass.contracts import (
    CanonicalText,
    CrawlFamily,
    CrawlSeedsEconomy,
    OcrQuality,
    PageSpan,
)
from regcompass.crawl import FetchResult, RateLimiter
from regcompass.engines import (
    _INDICATOR_RE,
    _PROVISION_RE,
    fake_completion,
    fake_embed,
    resolve_engine,
)
from regcompass.export import ALLOW_ANY_HOST_NOTE, LAW_NAME_MECHANICAL_NOTE, SyntheticDoc
from regcompass.pipeline import export_from_db, run_document, run_e2e
from regcompass.storage import Storage

from test_pipeline import ROOT  # noqa: E402

FAKE_ENGINE = resolve_engine("fake")

TELECOM = ROOT / "tests/fixtures/sample_legislation/born_digital/Telecommunications Act 1999.pdf"


def single_pass_completion():
    """A completion that passes EXACTLY ONE (chunk, indicator) pair with a real
    verbatim quote and returns no_evidence for the rest. The demo fake maps every
    chunk to a section's first line, which over a whole statute yields many rows
    sharing an (indicator, section) - a fixture artifact the real selective model
    never produces. Pinning one pass keeps the export deterministic and dupe-free
    while still exercising the full crawl -> map -> verify -> reconcile -> export
    stitch and the off-corpus synthetic-corpus fallback."""
    state = {"passed": False}

    def completion(prompt: str, strict: bool) -> str:
        ind = _INDICATOR_RE.search(prompt)
        prov = _PROVISION_RE.search(prompt)
        if ind is None or prov is None:
            return "not json at all"  # M8 group prompt -> hierarchy ladder
        if state["passed"]:
            return json.dumps({"maps_to_indicator": False, "verbatim_quote": ""})
        lines = [ln.strip() for ln in prov.group(1).splitlines() if len(ln.strip()) >= 40]
        if not lines:
            return json.dumps({"maps_to_indicator": False, "verbatim_quote": ""})
        state["passed"] = True
        return json.dumps({
            "maps_to_indicator": True,
            "verbatim_quote": lines[0],
            "impact": "The provision regulates the matter described in the quote.",
        })

    return completion


class NullLimiter(RateLimiter):
    def __init__(self):
        super().__init__(0.0, clock=lambda: 0.0, sleep=lambda s: None)


def fixture_fetcher(pdf: Path):
    """A crawl fetcher that returns a committed fixture PDF's exact bytes for
    any URL: the crawl machinery runs unchanged, but no network is touched."""
    body = pdf.read_bytes()

    def fetch(url: str) -> FetchResult:
        return FetchResult(
            url=url, final_url=url, http_status=200, content=body,
            content_type="application/pdf", method="httpx",
        )

    return fetch


# An SG seed whose act code is NOT in config/corpus.yaml, so the crawled
# document_id is off-corpus and the export must synthesize a CorpusDoc for it.
OFFCORPUS_SEEDS = CrawlSeedsEconomy(
    rate_limit_seconds=0.01,
    families={"data_protection": CrawlFamily(acts=["TESTACT9999"])},
)


def _e2e(tmp_path, **kwargs):
    storage = Storage(tmp_path / "e2e.db")
    storage.apply_schema()
    report = run_e2e(
        storage, "SG", (7,), FAKE_ENGINE,
        data_dir=tmp_path / "data", outdir=tmp_path / "out",
        max_documents=1, completion_fn=single_pass_completion(), embed_fn=fake_embed,
        fetcher=fixture_fetcher(TELECOM), limiter=NullLimiter(),
        seeds=OFFCORPUS_SEEDS,
        **kwargs,
    )
    return storage, report


class TestE2ELane:
    def test_crawl_to_export_end_to_end_no_manual_step(self, tmp_path):
        storage, report = _e2e(tmp_path)
        assert report.crawl_fetched == 1
        assert report.documents_mapped == ["doc_sg_sso_agc_gov_sg_Act_TESTACT9999"]
        assert report.run.n_passed > 0, "the verbatim-quote lane must pass"
        assert report.export is not None
        assert report.export.csv_path.exists()
        assert (tmp_path / "out" / "submission.json").exists()

    def test_offcorpus_document_gets_a_synthetic_corpus_row(self, tmp_path):
        """The crawled doc_id is absent from corpus.yaml: the export must NOT
        KeyError; it synthesizes a CorpusDoc (Source URL from the crawl record,
        so the SG portal whitelist stays satisfied) and flags the mechanically
        derived law name in Notes."""
        _, report = _e2e(tmp_path)
        with report.export.csv_path.open(encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        crawled = [
            r for r in rows
            if r["Source URL"].startswith("https://sso.agc.gov.sg/Act/TESTACT9999")
        ]
        assert crawled, "the crawled document's rows must be in the submission"
        assert all(LAW_NAME_MECHANICAL_NOTE in r["Notes"] for r in crawled)
        # host is a real SG portal host, so it is NOT flagged user-supplied
        assert all(ALLOW_ANY_HOST_NOTE not in r["Notes"] for r in crawled)

    def test_source_url_and_title_persisted_on_the_documents_row(self, tmp_path):
        storage, _ = _e2e(tmp_path)
        row = storage.conn.execute(
            "SELECT source_url, title FROM documents"
            " WHERE document_id = 'doc_sg_sso_agc_gov_sg_Act_TESTACT9999'"
        ).fetchone()
        assert row["source_url"] == "https://sso.agc.gov.sg/Act/TESTACT9999?ViewType=Pdf"
        assert row["title"]  # a mechanically derived, non-empty law name

    def test_progress_and_pair_callbacks_fire(self, tmp_path):
        lines: list[str] = []
        pairs: list[tuple[str, int, int]] = []
        _e2e(
            tmp_path,
            progress=lines.append,
            pair_progress=lambda d, i, n: pairs.append((d, i, n)),
        )
        joined = "\n".join(lines)
        assert any(l.startswith("M10 crawl |") for l in lines), "crawl narration"
        assert any(l.startswith("M1 extract |") for l in lines), "extract narration"
        assert any(l.startswith("M9 export |") for l in lines), "export narration"
        assert "battery GREEN" in joined
        assert pairs, "the M6/M7 per-pair progress callback must fire"
        assert pairs[-1][1] == pairs[-1][2], "the bar must reach total"

    def test_a_skipped_stage_says_so_instead_of_going_missing(self, tmp_path):
        """A gap in the stage numbers reads as a failure to anyone watching.

        This lane is a born-digital English Document, so two stages have
        nothing to do: no OCR is needed and there is nothing to translate.
        Both must say so in their own words, under their own stage name, or
        the log jumps M1 to M4 and looks like something broke."""
        lines: list[str] = []
        _e2e(tmp_path, progress=lines.append)

        skipped_ocr = [l for l in lines if l.startswith("M2 ocr |") and "skipped" in l]
        assert skipped_ocr, "M2 must say it was skipped, not vanish"
        assert "no OCR needed" in skipped_ocr[0], skipped_ocr[0]

        skipped_gloss = [
            l for l in lines if l.startswith("M3 gloss |") and "skipped" in l
        ]
        assert skipped_gloss, "M3 must say it was skipped, not vanish"
        assert "English" in skipped_gloss[0], skipped_gloss[0]

        # Every stage line still leads with its number AND its plain name, so
        # the log reads as steps rather than as codes.
        stages = {
            l.split(" |")[0] for l in lines if " | " in l and l[:1] == "M"
        }
        assert {"M1 extract", "M2 ocr", "M3 gloss", "M4 chunk", "M5 gate"} <= stages

    def test_no_fetch_is_a_readable_error_not_a_keyerror(self, tmp_path):
        storage = Storage(tmp_path / "e2e.db")
        storage.apply_schema()

        def dead(url: str) -> FetchResult:
            return FetchResult(url, url, 403, b"", "text/html", "curl_cffi")

        with pytest.raises(RuntimeError, match="fetched no usable"):
            run_e2e(
                storage, "SG", (7,), FAKE_ENGINE,
                data_dir=tmp_path / "data", outdir=tmp_path / "out",
                max_documents=1, completion_fn=fake_completion, embed_fn=fake_embed,
                fetcher=dead, limiter=NullLimiter(), seeds=OFFCORPUS_SEEDS,
            )


class TestMapPdfRegistration:
    def _register(self, tmp_path, **kwargs):
        storage = Storage(tmp_path / "m.db")
        storage.apply_schema()
        run_document(
            storage, "doc_sg_user_abc123", TELECOM, "SG", (7,), FAKE_ENGINE, "run_one",
            completion_fn=single_pass_completion(), embed_fn=fake_embed,
            source_url="https://example.org/my.pdf", title="My Custom Law 2026",
            filename_hint="my.pdf", **kwargs,
        )
        return storage

    def test_run_document_stores_source_url_and_explicit_title(self, tmp_path):
        storage = self._register(tmp_path)
        row = storage.conn.execute(
            "SELECT source_url, title FROM documents WHERE document_id = 'doc_sg_user_abc123'"
        ).fetchone()
        assert row["source_url"] == "https://example.org/my.pdf"
        assert row["title"] == "My Custom Law 2026"  # explicit title wins over derivation

    def test_export_allow_any_host_exempts_whitelist_and_stamps_notes(self, tmp_path):
        storage = self._register(tmp_path)
        from regcompass.contracts import CorpusDoc

        override = {
            "doc_sg_user_abc123": SyntheticDoc(
                CorpusDoc(
                    economy="SG", law_name="My Custom Law 2026",
                    source_url="https://example.org/my.pdf", url_is_direct=True,
                ),
                law_name_mechanical=False, allow_any_host=True,
            )
        }
        result = export_from_db(storage, tmp_path / "out", synthetic_docs=override)
        assert result.battery_failures == []
        with result.csv_path.open(encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        user_rows = [r for r in rows if r["Source URL"] == "https://example.org/my.pdf"]
        assert user_rows
        assert all(ALLOW_ANY_HOST_NOTE in r["Notes"] for r in user_rows)
        # user-supplied law name is NOT flagged mechanical
        assert all(LAW_NAME_MECHANICAL_NOTE not in r["Notes"] for r in user_rows)

    def test_offwhitelist_host_without_allow_any_host_fails_the_battery(self, tmp_path):
        storage = self._register(tmp_path)
        from regcompass.contracts import CorpusDoc

        override = {
            "doc_sg_user_abc123": SyntheticDoc(
                CorpusDoc(
                    economy="SG", law_name="My Custom Law 2026",
                    source_url="https://example.org/my.pdf", url_is_direct=True,
                ),
                law_name_mechanical=False, allow_any_host=False,
            )
        }
        with pytest.raises(Exception, match="portal whitelist"):
            export_from_db(storage, tmp_path / "out", synthetic_docs=override)


class TestOcrProxyPersistence:
    def test_run_document_threads_ocr_proxies_into_the_documents_row(self, tmp_path, monkeypatch):
        """Stage A's deferred gap: a scanned document entering via map-pdf / e2e
        must populate the 5 OCR proxy columns. run_document reads the proxies off
        canonical.ocr_quality and threads them through upsert_document."""
        import regcompass.ocr as ocr_mod
        import regcompass.shortlist as shortlist_mod

        raw = TELECOM.read_bytes()

        def fake_ocr(raw_bytes, doc_id, **kwargs):
            return CanonicalText(
                document_id=doc_id,
                source_sha256="a" * 64,
                extractor="tesseract+rapidocr",
                extractor_version="5.3.0+3.9.1",
                full_text="Section 5. A licensee shall protect personal data it holds under this Act. " * 40,
                pages=[PageSpan(page_number=1, char_start=0, char_end=100)],
                ocr_applied=True,
                ocr_quality=OcrQuality(
                    mean_word_confidence=0.72,
                    dictionary_hit_rate=0.61,
                    cer_proxy_flag=True,
                    escalated_to_rapidocr=True,
                    manual_review=False,
                ),
            )

        # force the OCR branch and swap in the fake engine (no tesseract needed)
        monkeypatch.setattr("regcompass.pipeline.should_ocr", lambda c: True)
        monkeypatch.setattr(ocr_mod, "ocr_document", fake_ocr)

        storage = Storage(tmp_path / "ocr.db")
        storage.apply_schema()
        run_document(
            storage, "doc_sg_user_scan", TELECOM, "SG", (7,), FAKE_ENGINE, "run_one",
            completion_fn=fake_completion, embed_fn=fake_embed,
        )
        meta = storage.document_meta()["doc_sg_user_scan"]
        assert meta["ocr_applied"] is True
        assert meta["mean_word_confidence"] == pytest.approx(0.72)
        assert meta["dictionary_hit_rate"] == pytest.approx(0.61)
        assert meta["escalated_to_rapidocr"] is True
        assert meta["cer_proxy_flag"] is True
        assert meta["manual_review"] is False


class TestDropsCommand:
    def test_drops_lists_dropped_insufficient_and_review_lanes(self, tmp_path):
        from regcompass.contracts import MappingRecord

        storage = Storage(tmp_path / "d.db")
        storage.apply_schema()
        storage.upsert_document("doc_sg_x", "SG", "b" * 64, full_text="x" * 100)
        storage.conn.execute(
            "INSERT INTO chunks (chunk_id, document_id, char_start, char_end,"
            " section_label, chunk_kind, created_at) VALUES"
            " ('doc_sg_x:0', 'doc_sg_x', 0, 10, 's. 1', 'section', '')"
        )
        storage.conn.commit()
        dropped = MappingRecord(
            mapping_id="m_drop", document_id="doc_sg_x", chunk_id="doc_sg_x:0",
            economy="SG", indicator_id="7.3", indicator_name="X", section="s. 1",
            verbatim_quote="q", verification_status="dropped",
        )
        insufficient = MappingRecord(
            mapping_id="m_ins", document_id="doc_sg_x", chunk_id="doc_sg_x:0",
            economy="SG", indicator_id="7.1", indicator_name="Y", section="s. 1",
            verbatim_quote="No provision found", insufficient_evidence=True,
        )
        storage.upsert_mappings([dropped, insufficient], run_id="run_one")
        storage.close()

        result = CliRunner().invoke(app, ["drops", "--db", str(tmp_path / "d.db")])
        assert result.exit_code == 0, result.output
        assert "Dropped mappings (verification_status='dropped'): 1" in result.output
        assert "doc_sg_x" in result.output and "7.3" in result.output
        assert "Insufficient-evidence records" in result.output
