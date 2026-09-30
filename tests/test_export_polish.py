"""The exported row reads the way a legal reviewer expects: Articles for
civil-law statutes, the RDTII's own words in the rationale, and a flag on any
row that falls into one of the Indicator Reference's scoring traps."""

from regcompass.config import load_crosswalk, load_known_matrix
from regcompass.contracts import CorpusDoc, MappingRecord, Timeline
from regcompass.export import build_row, location_reference


def rec(economy="CN", section="Chapter 3 s. 40", subsection="(2)", indicator="6.1",
        impact="Requires operators to store data locally, a sector-specific restriction, matching rung 2.",
        quote="关键信息基础设施的运营者在中华人民共和国境内运营中收集和产生的个人信息和重要数据应当在境内存储。"):
    doc_id = f"doc_{economy.lower()}_law"
    return MappingRecord(
        mapping_id=f"{doc_id}:c0001::{indicator}",
        document_id=doc_id,
        chunk_id=f"{doc_id}:c0001",
        economy=economy,  # type: ignore[arg-type]
        indicator_id=indicator,  # type: ignore[arg-type]
        indicator_name="x",
        section=section,
        subsection=subsection,
        verbatim_quote=quote,
        page_number=1,
        impact=impact,
        verification_status="passed",
        relationship_to_group="complementary",
        controlling_evidence=True,
        timeline=Timeline(),
        extraction_attempts=1,
    )


def row_for(record, law_name="中华人民共和国网络安全法", format_tag="html", trap_check=True):
    doc = CorpusDoc(economy=record.economy, law_name=law_name, source_url="https://www.cac.gov.cn/x.htm")
    return build_row(
        record, doc, load_crosswalk(), load_known_matrix(),
        {(record.chunk_id, record.indicator_id): 0.6}, {record.chunk_id: 1},
        format_tag=format_tag, trap_check=trap_check,
    )


def test_chinese_article_label_in_the_article_and_location_columns():
    r = rec()
    row = row_for(r)
    assert row["Article / Section"] == "Chapter 3 Art. 40(2)"
    assert row["Location Reference"] == "HTML: Chapter 3 Art. 40(2)"
    assert location_reference(r, "html") == "HTML: Chapter 3 Art. 40(2)"


def test_indonesian_article_and_common_law_section():
    assert row_for(rec(economy="ID", section="s. 56", subsection="(2)"), "UU Nomor 27 Tahun 2022")[
        "Article / Section"
    ] == "Art. 56(2)"
    assert row_for(rec(economy="IN", section="s. 43A", subsection=None), "Information Technology Act, 2000")[
        "Article / Section"
    ] == "s. 43A"


def test_rationale_is_in_rdtii_words():
    row = row_for(rec())
    assert row["Mapping Rationale"] == (
        "Requires operators to store data locally, a sector-specific restriction,"
        " matching RDTII criterion 2 (score 0.5)."
    )


def test_a_trap_row_carries_the_flag_in_notes():
    row = row_for(rec(indicator="7.3", impact="Requires retention of logs.",
                      quote="网络运营者应当留存相关的网络日志。"))
    assert "Check before submitting: 7.3 needs a specified minimum duration" in row["Notes"]
    amending = row_for(rec(economy="ID", section="s. 26", indicator="6.4", impact="x", quote="x" * 20),
                       "UU Nomor 19 Tahun 2016")
    assert "cited from an amending act" in amending["Notes"]


def test_a_clean_row_has_no_flag():
    assert "Check before submitting" not in row_for(rec())["Notes"]


def test_the_round_1_lanes_carry_no_trap_flag():
    # the Round 1 exports are pinned byte for byte; the flags are opt-in
    row = row_for(rec(indicator="7.3", quote="网络运营者应当留存相关的网络日志。"), trap_check=False)
    assert "Check before submitting" not in row["Notes"]


def test_evidence_detail_sends_the_rationale_in_rdtii_words():
    from regcompass.audit import detail_for

    r = rec()
    detail = detail_for(
        r, full_text=r.verbatim_quote, words_fn=lambda a, b: [], chunk_start=None,
        chunk_end=None, section_label=None, review=None, source_format="html",
    )
    assert detail.record.impact == r.impact  # the stored text is the Engine's own
    assert detail.impact_display == (
        "Requires operators to store data locally, a sector-specific restriction,"
        " matching RDTII criterion 2 (score 0.5)."
    )
