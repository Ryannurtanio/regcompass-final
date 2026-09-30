"""Rows built on a Document a reviewer added by URL read right.

China's three statutes are HTML pages on the regulator's host, entered through
"Add document" by their Source URL. What is pinned here:

  * an HTML Document's Location Reference names the format truthfully (the
    section path the organizers' column asks for), never "PDF: page 1";
  * Law Name, Law Number / Ref and Last Amended for a Document outside
    corpus.yaml come from config/law_metadata.yaml, keyed by document id, and
    nowhere else;
  * Last Amended is a year, as the template's column guidance asks ("Year of
    most recent amendment. Leave blank if not amended."), for every Economy;
  * a Document added by URL is not called a live crawl catch, and a Document
    on a whitelisted host is not called off-whitelist.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from regcompass.compare import compare_runs
from regcompass.config import load_crosswalk, load_known_matrix, load_law_metadata, load_portals
from regcompass.contracts import CorpusDoc, MappingRecord, RunRecord
from regcompass.export import (
    ALLOW_ANY_HOST_NOTE,
    LAW_NAME_MECHANICAL_NOTE,
    LAW_NAME_MECHANICAL_PREPARED_NOTE,
    MANUAL_ADD_NOTE,
    build_row,
    last_amended_year,
    location_reference,
    run_gate_battery,
    synthetic_notes,
)
from regcompass.pipeline import synthesize_offcorpus_docs
from regcompass.storage import utc_now_iso

PIPL = "doc_cn_c_1631050028355286"
DSL = "doc_cn_c_1624994566919140"
CSL = "doc_cn_c_1768735112911946"
PIPL_URL = "https://www.cac.gov.cn/2021-08/20/c_1631050028355286.htm"

CN_META = {
    PIPL: {
        "economy": "CN",
        "title": "Personal Information Protection Law of the People's Republic of China",
        "source_url": PIPL_URL,
        "source_kind": "manual",
        "extractor": "bs4-lxml",
    },
    DSL: {
        "economy": "CN",
        "title": "Data Security Law of the People's Republic of China",
        "source_url": "https://www.cac.gov.cn/2021-06/11/c_1624994566919140.htm",
        "source_kind": "manual",
        "extractor": "bs4-lxml",
    },
    CSL: {
        "economy": "CN",
        "title": "Cybersecurity Law of the People's Republic of China",
        "source_url": "https://www.cac.gov.cn/2025-12/29/c_1768735112911946.htm",
        "source_kind": "manual",
        "extractor": "bs4-lxml",
    },
}


def _cn_record(section: str = "Chapter 3 s. 38", page: int = 1) -> MappingRecord:
    return MappingRecord(
        mapping_id=f"{PIPL}:c0040::6.1",
        document_id=PIPL,
        chunk_id=f"{PIPL}:c0040",
        economy="CN",
        indicator_id="6.1",
        indicator_name="Indicator 6.1",
        section=section,
        verbatim_quote="Personal information handlers shall meet one of the following conditions",
        page_number=page,
        verification_status="passed",
        controlling_evidence=True,
    )


# ---------------------------------------------------------------------------
# Location Reference
# ---------------------------------------------------------------------------


class TestLocationReferenceNamesTheFormat:
    def test_a_pdf_row_keeps_its_page(self):
        assert location_reference(_cn_record(page=7)) == "PDF: page 7"
        assert location_reference(_cn_record(page=7), "pdf") == "PDF: page 7"

    def test_an_html_row_names_its_section_path_and_no_page(self):
        """An HTML page has no pages: extraction records the whole page as
        page 1, so a page number would point at nothing."""
        ref = location_reference(_cn_record(), "html")
        assert ref == "HTML: Chapter 3 Art. 38"
        assert "PDF" not in ref and "page" not in ref

    def test_the_export_row_reads_the_format_it_is_given(self):
        rec = _cn_record()
        doc = CorpusDoc(economy="CN", law_name="PIPL", source_url=PIPL_URL)
        row = build_row(
            rec, doc, load_crosswalk(), load_known_matrix(),
            {(rec.chunk_id, rec.indicator_id): 0.6}, {rec.chunk_id: 1},
            format_tag="html",
        )
        assert row["Location Reference"] == "HTML: Chapter 3 Art. 38"

    def test_the_comparison_screen_agrees(self):
        run = lambda rid: RunRecord(  # noqa: E731
            run_id=rid, kind="run", economy="CN", pillars=[6], indicators=None,
            engine="engine_a", status="completed", started_at=utc_now_iso(),
            ended_at=utc_now_iso(),
        )
        comparison = compare_runs(
            run("run_a"), run("run_b"), [_cn_record()], [], {"6.1": "Indicator 6.1"},
            document_sources={PIPL: {"source_url": PIPL_URL, "extractor": "bs4-lxml"}},
        )
        side = comparison.rows[0].a
        assert side.location_reference == "HTML: Chapter 3 Art. 38"
        assert side.source_link.href == PIPL_URL


# ---------------------------------------------------------------------------
# Law Number / Ref and Last Amended
# ---------------------------------------------------------------------------


class TestLawMetadataForDocumentsOutsideTheCorpusFile:
    def test_the_committed_file_carries_the_three_chinese_statutes(self):
        meta = load_law_metadata()
        assert meta[PIPL].law_number_ref == "Order of the President No. 91 (2021)"
        assert meta[PIPL].last_amended is None
        assert meta[DSL].law_number_ref == "Order of the President No. 84 (2021)"
        assert meta[DSL].last_amended is None
        assert meta[CSL].law_number_ref == "Order of the President No. 53 (2016)"
        assert meta[CSL].last_amended == "2025"

    def test_indonesia_and_india_carry_their_full_names(self):
        meta = load_law_metadata()
        assert meta["doc_id_UU_Nomor_27_Tahun_2022"].law_name == (
            "Undang-Undang Republik Indonesia Nomor 27 Tahun 2022"
            " tentang Pelindungan Data Pribadi"
        )
        assert meta["doc_id_UU_Nomor_27_Tahun_2022"].law_number_ref == "Law No. 27/2022"
        assert meta["doc_id_UU_Nomor_11_Tahun_2008"].last_amended == "2024"
        assert meta["doc_in_a2023-22"].law_name == "Digital Personal Data Protection Act, 2023"
        assert meta["doc_in_2009-10"].law_name == "Information Technology (Amendment) Act, 2008"

    def test_a_missing_file_means_no_entries(self, tmp_path: Path):
        assert load_law_metadata(tmp_path) == {}

    def test_the_synthesizer_fills_the_columns_from_it(self):
        docs = synthesize_offcorpus_docs(CN_META)
        assert docs[CSL].corpus_doc.law_number_ref == "Order of the President No. 53 (2016)"
        assert docs[CSL].corpus_doc.last_amended == "2025"
        assert docs[PIPL].corpus_doc.last_amended is None

    def test_a_recorded_law_name_replaces_the_derived_title(self):
        docs = synthesize_offcorpus_docs(
            {
                "doc_id_UU_Nomor_27_Tahun_2022": {
                    "economy": "ID", "title": "UU Nomor 27 Tahun 2022",
                    "source_url": "https://peraturan.bpk.go.id/Download/224884/x.pdf",
                    "source_kind": "discovery",
                },
            }
        )
        synthetic = docs["doc_id_UU_Nomor_27_Tahun_2022"]
        assert synthetic.corpus_doc.law_name.startswith("Undang-Undang Republik Indonesia")
        # A person recorded that name, so it is no longer derived from the title.
        assert synthetic.law_name_mechanical is False
        assert LAW_NAME_MECHANICAL_NOTE not in synthetic_notes(synthetic, "discovery")

    def test_a_document_with_no_entry_stays_blank(self):
        docs = synthesize_offcorpus_docs(
            {
                "doc_id_uu_27_2022": {
                    "economy": "ID", "title": "Undang-Undang 27 Tahun 2022",
                    "source_url": "https://peraturan.go.id/files/uu27-2022.pdf",
                    "source_kind": "discovery",
                },
            }
        )
        doc = docs["doc_id_uu_27_2022"].corpus_doc
        assert (doc.law_number_ref, doc.last_amended) == (None, None)


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------


class TestNotesForADocumentAddedByUrl:
    def test_it_is_not_called_a_live_crawl_catch(self):
        notes = synthetic_notes(synthesize_offcorpus_docs(CN_META)[PIPL], "manual")
        assert not any("live crawl catch" in n for n in notes)
        assert LAW_NAME_MECHANICAL_NOTE not in notes
        # the law name comes from config/law_metadata.yaml, so no mechanical-name note
        assert not any("law name mechanically derived" in n for n in notes)
        assert MANUAL_ADD_NOTE in notes

    def test_a_whitelisted_host_is_not_called_off_whitelist(self):
        synthetic = synthesize_offcorpus_docs(CN_META)[PIPL]
        assert synthetic.manual_added is True
        assert synthetic.allow_any_host is False
        assert ALLOW_ANY_HOST_NOTE not in synthetic_notes(synthetic, "manual")

    def test_the_row_passes_the_battery_on_the_whitelist_alone(self):
        rec = _cn_record()
        synthetic = synthesize_offcorpus_docs(CN_META)[PIPL]
        row = build_row(
            rec, synthetic.corpus_doc, load_crosswalk(), load_known_matrix(),
            {(rec.chunk_id, rec.indicator_id): 0.6}, {rec.chunk_id: 1},
            extra_notes=synthetic_notes(synthetic, "manual"),
            allow_any_host=synthetic.allow_any_host,
            format_tag="html",
        )
        texts = {rec.chunk_id: "x " + rec.verbatim_quote + " y"}
        assert run_gate_battery([row], texts, load_portals(), lambda url: True) == []
        assert row["Law Number / Ref"] == "Order of the President No. 91 (2021)"
        assert row["Last Amended"] == ""

    def test_a_manual_document_off_the_whitelist_keeps_both_disclosures(self):
        synthetic = synthesize_offcorpus_docs(
            {
                "doc_my_added_by_hand": {
                    "economy": "MY", "title": "Added By Hand Act 2026",
                    "source_url": "https://example.org/by-hand.pdf",
                    "source_kind": "manual",
                },
            }
        )["doc_my_added_by_hand"]
        assert synthetic.allow_any_host is True
        notes = synthetic_notes(synthetic, "manual")
        assert ALLOW_ANY_HOST_NOTE in notes and MANUAL_ADD_NOTE in notes

    @pytest.mark.parametrize("kind", ["discovery", None])
    def test_a_discovered_document_keeps_the_crawl_wording(self, kind):
        synthetic = synthesize_offcorpus_docs(
            {
                "doc_in_dpdp_2023": {
                    "economy": "IN", "title": "Digital Personal Data Protection Act 2023",
                    "source_url": "https://indiacode.gov.in/dpdp.pdf",
                    "source_kind": kind,
                },
            }
        )["doc_in_dpdp_2023"]
        assert synthetic_notes(synthetic, kind)[0] == LAW_NAME_MECHANICAL_NOTE


class TestAPreparedDocumentIsNotALiveCatch:
    """A Document Discovery fetched while the Corpus was prepared, days before
    the Run, was not caught live, and its Notes must not say it was."""

    META = {
        "doc_la_instruction_0144": {
            "economy": "LA", "title": "Instruction No. 0144",
            "source_url": "https://laoofficialgazette.gov.la/kcfinder/upload/files/0144.pdf",
            "source_kind": "discovery",
            "fetched_at": "2026-09-22T18:58:33.258235+00:00",
        },
    }

    def test_fetched_days_before_the_run_it_says_prepared(self):
        synthetic = synthesize_offcorpus_docs(
            self.META, run_started_at="2026-09-29T09:26:08Z"
        )["doc_la_instruction_0144"]
        notes = synthetic_notes(synthetic, "discovery")
        assert notes[0] == LAW_NAME_MECHANICAL_PREPARED_NOTE
        assert not any("live crawl catch" in n for n in notes)

    def test_fetched_in_the_same_pass_it_stays_a_live_catch(self):
        synthetic = synthesize_offcorpus_docs(
            self.META, run_started_at="2026-09-22T19:05:00Z"
        )["doc_la_instruction_0144"]
        assert synthetic_notes(synthetic, "discovery")[0] == LAW_NAME_MECHANICAL_NOTE


# ---------------------------------------------------------------------------
# Last Amended is a year
# ---------------------------------------------------------------------------


class TestLastAmendedIsAYear:
    @pytest.mark.parametrize(
        "stored, shown",
        [
            ("June 2026", "2026"),
            ("December 2021", "2021"),
            ("2010", "2010"),
            ("2025", "2025"),
            (None, ""),
            ("", ""),
            ("no date here", ""),
        ],
    )
    def test_the_column_shows_the_year_alone(self, stored, shown):
        assert last_amended_year(stored) == shown

    def test_a_curated_row_renders_its_month_as_a_year(self):
        rec = _cn_record()
        doc = CorpusDoc(
            economy="CN", law_name="PIPL", source_url=PIPL_URL, last_amended="June 2026"
        )
        row = build_row(
            rec, doc, load_crosswalk(), load_known_matrix(),
            {(rec.chunk_id, rec.indicator_id): 0.6}, {rec.chunk_id: 1},
        )
        assert row["Last Amended"] == "2026"
