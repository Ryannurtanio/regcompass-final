"""Quote-anchored section-label repair.

The shipped export carried labels with the chunk boundary's granularity, and
the chunker misses two heading species: 3+ letter alpha suffixes (27KBA) under
the num_title profile, and sections inserted by amendment-act schedules (43C
inside an "Add:" block). The repair re-derives the label at the quote's exact
character position without moving any chunk boundary, so no chunk-keyed
checkpoint is invalidated.
"""

from pathlib import Path

from regcompass.chunk import SectionLabelIndex, repair_section_labels
from regcompass.contracts import MappingRecord, Timeline
from regcompass.extract import extract


def mk_rec(
    quote: str,
    section: str,
    document_id: str = "doc_au_test",
    chunk_id: str = "doc_au_test:c0003",
    indicator: str = "7.5",
) -> MappingRecord:
    return MappingRecord(
        mapping_id=f"{chunk_id}::{indicator}",
        document_id=document_id,
        chunk_id=chunk_id,
        economy="AU",
        indicator_id=indicator,  # type: ignore[arg-type]
        indicator_name="Requirements to allow Government access to personal data",
        section=section,
        subsection="(1)",
        verbatim_quote=quote,
        page_number=12,
        impact="Warrant application requires prior consent.",
        verification_status="passed",
        timeline=Timeline(),
        extraction_attempts=1,
    )


# An AU-style body where 27KBA (3-letter suffix) is invisible to the chunker's
# num_title profile but present in the stream.
AU_SUFFIX_TEXT = "\n".join(
    [
        "Part 2 Data disruption warrants",
        "27KB Application for a data disruption warrant",
        "A law enforcement officer of the Australian Federal Police may apply",
        "for a data disruption warrant in respect of relevant data.",
        "27KBA Endorsement of application by the chief officer",
        "An application under section 27KB must not be made unless the chief",
        "officer has endorsed the making of the application in writing.",
        "27KC Determining the application",
        "An eligible Judge may issue the warrant if satisfied of the matters.",
    ]
)

# An amendment act: the schedule item grid ("24 Section 36", "25 At the end")
# is what the chunker sees; the INSERTED section 43C exists only inside the
# quoted "Add:" block.
AU_AMEND_TEXT = "\n".join(
    [
        "Schedule 2 Account takeover warrants",
        "24 Section 36",
        "Repeal the section, substitute:",
        "36 Application of this Part",
        "This Part applies in relation to a relevant offence.",
        "25 At the end of Part 3",
        "Add:",
        "43C Protection of persons assisting under warrant",
        "A person who assists an officer under an account takeover warrant is",
        "not subject to any civil liability in respect of that assistance.",
        "43E Concealment of access",
        "An officer may do anything reasonably necessary to conceal the fact",
        "that anything has been done under the warrant.",
    ]
)

# An SG-style page where the running page header ("NN Cybersecurity Act 2018")
# repeats with a DIFFERENT page number each time: exact-line furniture cannot
# see it, the repeated-title kill must.
SG_FURNITURE_TEXT = "\n".join(
    [
        "29A. Preservation of secrecy",
        "(1) A person who obtains information in the exercise of a power must",
        "not disclose the information except with lawful authority.",
        "81 Cybersecurity Act 2018 2020 Ed.",
        "(2) The obligation continues after the person ceases to hold office.",
        "82 Cybersecurity Act 2018 2020 Ed.",
        "(3) Contravention is an offence punishable on conviction.",
        "83 Cybersecurity Act 2018 2020 Ed.",
        "(4) Further page.",
        "84 Cybersecurity Act 2018 2020 Ed.",
        "(5) Further page.",
        "85 Cybersecurity Act 2018 2020 Ed.",
        "(6) Further page.",
        "30. Next section heading",
        "(1) Something else entirely.",
    ]
)


class TestSectionLabelIndex:
    def test_three_letter_suffix_heading_is_seen(self):
        idx = SectionLabelIndex(AU_SUFFIX_TEXT)
        pos = AU_SUFFIX_TEXT.index("must not be made unless")
        assert idx.label_at(pos) == "Part 2 s. 27KBA"

    def test_two_letter_neighbour_still_wins_its_own_body(self):
        idx = SectionLabelIndex(AU_SUFFIX_TEXT)
        pos = AU_SUFFIX_TEXT.index("may apply")
        assert idx.label_at(pos) == "Part 2 s. 27KB"

    def test_inserted_section_beats_the_schedule_item_grid(self):
        idx = SectionLabelIndex(AU_AMEND_TEXT)
        pos = AU_AMEND_TEXT.index("not subject to any civil liability")
        assert idx.label_at(pos) == "s. 43C"
        pos = AU_AMEND_TEXT.index("reasonably necessary to conceal")
        assert idx.label_at(pos) == "s. 43E"

    def test_sg_page_headers_cannot_shadow_the_dot_style_headings(self):
        idx = SectionLabelIndex(SG_FURNITURE_TEXT)
        # SG's dominant pattern is the "N." dot style; the num_title-shaped
        # page headers ("83 Cybersecurity Act 2018 2020 Ed.") are a different
        # pattern entirely, so they are neither candidates nor ambiguity: the
        # quote resolves to its true section
        pos = SG_FURNITURE_TEXT.index("Contravention is an offence")
        assert idx.label_at(pos) == "s. 29A"

    def test_au_page_headers_sharing_the_dominant_pattern_force_a_keep(self):
        # AU compilations: page headers ARE num_title-shaped ("72 Online
        # Safety Act 2021 No. 78, 2021"), repeat with a varying page number,
        # and can sit between a heading and a quote. The repeated-title kill
        # removes them from candidates and the ambiguity rule must then
        # refuse rather than answer with an earlier heading.
        blocks = []
        for pg in (70, 71, 72, 73, 74):
            blocks.append(
                "\n".join(
                    [
                        f"{pg} Online Safety Act 2021 No. 78, 2021",
                        f"{pg - 22} Duty about section {pg}",
                        "A provider must comply with the duty imposed by this",
                        "section in relation to the removal notice given.",
                    ]
                )
            )
        text = "\n".join(blocks)
        idx = SectionLabelIndex(text)
        # directly under its own unique heading: answered
        pos = text.index("A provider must comply", text.index("50 Duty"))
        assert idx.label_at(pos) == "s. 50"
        # header line itself is not a heading: a position just after a header
        # but before the next real heading is ambiguous -> refuse
        hdr = text.index("73 Online Safety Act 2021")
        assert idx.label_at(hdr + 10) is None

    def test_no_preceding_heading_returns_none(self):
        idx = SectionLabelIndex("preamble text with no headings\nmore prose")
        assert idx.label_at(10) is None

    def test_parallel_structure_repeated_titles_refuse_not_misattribute(self):
        # TIA-style parallel divisions repeat the same clause title with
        # different numbers; the repeated-title kill removes all of them, and
        # the ambiguity rule must then REFUSE (keep the chunk label) instead
        # of answering with the previous surviving heading (the off-by-one
        # class observed on the real corpus).
        blocks = []
        for n in (34, 44, 54, 64, 74):
            blocks.append(
                "\n".join(
                    [
                        f"{n} Form of application",
                        "An application must be in the approved form and must set out",
                        "the grounds on which the application is made.",
                        f"{n + 1} Issue of order for enforcement agency number {n}",
                        "The issuing authority may issue the order if satisfied.",
                    ]
                )
            )
        text = "\n".join(blocks)
        idx = SectionLabelIndex(text)
        # under a killed repeated title: refuse
        pos = text.index("the grounds on which", text.index("54 Form"))
        assert idx.label_at(pos) is None
        # under a unique title: answer
        pos2 = text.index("may issue the order", text.index("55 Issue"))
        assert idx.label_at(pos2) == "s. 55"

    def test_quote_capturing_its_own_heading_labels_that_heading(self):
        # MY amendment acts print a marginal note ABOVE the section heading;
        # a quote that starts at the note begins a line before "3." and the
        # nearest-preceding scan alone would land on s. 2. The quote span
        # containing the "3." heading must win.
        text = "\n".join(
            [
                "2. The principal Act is amended in paragraph 5(1)(b) by",
                "substituting for the word \"persons\" the word \"person\".",
                "amendment of section 6",
                "3. Subsection 6(1) of the principal Act is amended by inserting",
                "after the words \"an offence\" the words \"or is about to commit\".",
            ]
        )
        idx = SectionLabelIndex(text)
        start = text.index("amendment of section 6")
        end = text.index("about to commit") + 20
        assert idx.label_at(start) == "s. 2"  # preceding scan alone: wrong
        assert idx.label_at(start, end=end) == "s. 3"  # span rule: right

    def test_schedule_item_instruction_lines_are_never_headings(self):
        idx = SectionLabelIndex(AU_AMEND_TEXT)
        # a quote inside item 24's substituted text, BEFORE any inserted
        # section heading: nearest pattern-shaped line is the item line
        # "24 Section 36" - ambiguous, refuse
        pos = AU_AMEND_TEXT.index("Repeal the section, substitute:")
        assert idx.label_at(pos) is None


class TestRepairSectionLabels:
    def test_mode_a_repair(self):
        rec = mk_rec(
            quote="An application under section 27KB must not be made unless the chief",
            section="Part 2 s. 27KB",
        )
        out, decisions = repair_section_labels([rec], AU_SUFFIX_TEXT)
        assert out[0].section == "Part 2 s. 27KBA"
        assert decisions[0]["outcome"] == "repaired"
        assert decisions[0]["old"] == "Part 2 s. 27KB"

    def test_mode_b_repair(self):
        rec = mk_rec(
            quote="not subject to any civil liability in respect of that assistance.",
            section="s. 25",
        )
        out, decisions = repair_section_labels([rec], AU_AMEND_TEXT)
        assert out[0].section == "s. 43C"
        assert decisions[0]["outcome"] == "repaired"

    def test_matching_number_keeps_the_original_label_verbatim(self):
        # same section number: the original label (with its part prefix) is
        # kept untouched, so clean rows cannot churn
        rec = mk_rec(
            quote="A law enforcement officer of the Australian Federal Police may apply",
            section="Part 2 s. 27KB",
        )
        out, decisions = repair_section_labels([rec], AU_SUFFIX_TEXT)
        assert out[0] is rec
        assert decisions[0]["outcome"] == "kept"

    def test_quote_not_in_stream_is_left_untouched(self):
        rec = mk_rec(quote="this text does not exist in the stream", section="s. 1")
        out, decisions = repair_section_labels([rec], AU_SUFFIX_TEXT)
        assert out[0] is rec
        assert decisions[0]["outcome"] == "not_found"

    def test_unlabelled_record_is_left_untouched(self):
        rec = mk_rec(
            quote="An application under section 27KB must not be made unless the chief",
            section="unstructured document (no reliable boundaries)",
        )
        out, decisions = repair_section_labels([rec], AU_SUFFIX_TEXT)
        assert out[0] is rec
        assert decisions[0]["outcome"] == "unlabelled"

    def test_chunk_offset_disambiguates_duplicate_quotes(self):
        # the same sentence appears twice (TOC echo + body); the chunk text
        # anchors the search to the right occurrence
        dup = AU_SUFFIX_TEXT + "\n27KD Relevant offences\n" + "A law enforcement officer of the Australian Federal Police may apply"
        chunk_text = "27KD Relevant offences\nA law enforcement officer of the Australian Federal Police may apply"
        rec = mk_rec(
            quote="A law enforcement officer of the Australian Federal Police may apply",
            section="Part 2 s. 27KB",
            chunk_id="doc_au_test:c0009",
        )
        out, decisions = repair_section_labels(
            [rec], dup, chunk_texts={"doc_au_test:c0009": chunk_text}
        )
        assert out[0].section == "Part 2 s. 27KD"
        assert decisions[0]["outcome"] == "repaired"

    def test_decision_trail_covers_every_record(self):
        recs = [
            mk_rec(quote="An eligible Judge may issue the warrant if satisfied of the matters.", section="Part 2 s. 27KC"),
            mk_rec(quote="missing quote", section="s. 2"),
        ]
        out, decisions = repair_section_labels(recs, AU_SUFFIX_TEXT)
        assert len(out) == len(decisions) == 2
        assert [d["outcome"] for d in decisions] == ["kept", "not_found"]


# The three controlling-pointer mechanisms. MY prints
# inserted sections with LOWERCASE suffixes ("230b."), invisible to the
# uppercase-only patterns; and the Part tracker latched onto wrapped
# cross-reference sentences ("Part IIIC of the Privacy Act 1988 ...") that
# start a line, mislabelling the Part prefix.

MY_LOWERCASE_INSERT_TEXT = "\n".join(
    [
        "87. Section 229 of the principal Act is amended by inserting",
        "the words after subsection (2).",
        "New Chapter 1a",
        "88. The principal Act is amended by inserting after Chapter 1 the",
        "following new chapter:",
        "“Chapter 1a",
        "Network Security",
        "Certifying agencies",
        "230a. The Commission may register certifying agencies",
        "for the purpose of certifying compliance with standards.",
        "Network security measures and requirements",
        "230b. (1) Where the Commission is satisfied that it is necessary",
        "to prevent or counter any network security threat, the Commission",
        "may direct a licensee to take such measures as may be specified.",
        "89. Section 232 of the principal Act is amended by deleting",
        "the words in subsection (1).",
    ]
)

AU_CROSSREF_PART_TEXT = "\n".join(
    [
        "Part 3.3—Data breach responsibilities",
        "37 Interaction with Part IIIC of the Privacy Act 1988",
        "This section sets out the relationship between this Act and",
        "Part IIIC of the Privacy Act 1988 (notification of eligible data",
        "breaches), as it applies to data scheme entities.",
        "38 Notify Commissioner of non-personal data breach",
        "A data scheme entity must notify the Commissioner, in an approved",
        "form (if any), of any data breach involving non-personal data.",
        "39 Remedial obligations",
        "The entity must take reasonable steps to mitigate the breach.",
    ]
)


class TestRound4Mechanisms:
    def test_my_lowercase_inserted_section_is_repaired(self):
        rec = mk_rec(
            quote=(
                "Where the Commission is satisfied that it is necessary\n"
                "to prevent or counter any network security threat, the Commission\n"
                "may direct a licensee to take such measures as may be specified."
            ),
            section="s. 88",
        )
        out, decisions = repair_section_labels([rec], MY_LOWERCASE_INSERT_TEXT)
        assert out[0].section == "s. 230b"
        assert decisions[0]["outcome"] == "repaired"

    def test_crossref_part_line_never_becomes_part_context(self):
        idx = SectionLabelIndex(AU_CROSSREF_PART_TEXT)
        pos = AU_CROSSREF_PART_TEXT.find("A data scheme entity must notify")
        assert idx.label_at(pos) == "Part 3.3 s. 38"

    def test_foreign_part_prefix_is_repaired_to_the_documents_own(self):
        rec = mk_rec(
            quote=(
                "A data scheme entity must notify the Commissioner, in an approved\n"
                "form (if any), of any data breach involving non-personal data."
            ),
            section="Part IIIC s. 38",
        )
        out, decisions = repair_section_labels([rec], AU_CROSSREF_PART_TEXT)
        assert out[0].section == "Part 3.3 s. 38"
        assert decisions[0]["outcome"] == "repaired"

    def test_part_prefix_kept_when_index_has_no_part_context(self):
        # AU_SUFFIX_TEXT's "Part 2 Data disruption warrants" is a real Part
        # heading; strip it to simulate a document with no Part headings at
        # all: absence of part context must never strip the model's prefix.
        text = AU_SUFFIX_TEXT.replace("Part 2 Data disruption warrants\n", "")
        rec = mk_rec(
            quote="An eligible Judge may issue the warrant if satisfied of the matters.",
            section="Part 9 s. 27KC",
        )
        out, decisions = repair_section_labels([rec], text)
        assert out[0].section == "Part 9 s. 27KC"
        assert decisions[0]["outcome"] == "kept"


# A quote taken from the text ABOVE a chunk's own section heading
# must not be relabelled to the section that ended before the chunk began.
# This is the real Malaysia shape (golden m4 chunk c0006 of the Personal Data
# Protection Act 2010): the chunk opens on the PART II heading and its Division
# line, and the section heading that owns those lines ("Section 5.") comes
# AFTER them, so the nearest-preceding scan lands on "Section 4.", a heading
# thousands of characters before the chunk even begins, in the previous Part.
# A chunk with no heading of its own is a different case and keeps the old
# behaviour: see test_a_continuation_chunk_still_takes_the_heading_above_it.

MY_PART_BOUNDARY_TEXT = "\n".join(
    [
        "PART I - PRELIMINARY",
        "Section 1. Short title and commencement",
        "This Act may be cited as the Personal Data Protection Act 2010.",
        "Section 4. Interpretation",
        "In this Act, unless the context otherwise requires, the words below",
        "have the meanings assigned to them in this section.",
        "PART II - PERSONAL DATA PROTECTION",
        "Division 1 - Personal Data Protection Principles",
        "Section 5. Personal Data Protection Principles",
        "(1) The processing of personal data by a data user shall be in",
        "compliance with the Personal Data Protection Principles.",
        "Section 6. General Principle",
        "(1) A data user shall not process personal data about a data subject",
        "unless the data subject has given his consent to the processing.",
    ]
)

MY_C0006_TEXT = "\n".join(
    [
        "PART II - PERSONAL DATA PROTECTION",
        "Division 1 - Personal Data Protection Principles",
        "Section 5. Personal Data Protection Principles",
        "(1) The processing of personal data by a data user shall be in",
        "compliance with the Personal Data Protection Principles.",
    ]
)

MY_C0007_TEXT = "\n".join(
    [
        "Section 6. General Principle",
        "(1) A data user shall not process personal data about a data subject",
        "unless the data subject has given his consent to the processing.",
    ]
)

# What the fake Engine quotes out of chunk c0006: its first line of 40
# characters or more, which is the Division line ABOVE the chunk's own
# section heading.
MY_C0006_QUOTE = "Division 1 - Personal Data Protection Principles"


class TestNeverRelabelsBackwardsOutOfTheChunk:
    def test_the_index_reports_where_the_heading_it_chose_sits(self):
        idx = SectionLabelIndex(MY_PART_BOUNDARY_TEXT)
        pos = MY_PART_BOUNDARY_TEXT.index(MY_C0006_QUOTE)
        label, heading_pos = idx.label_at_with_pos(pos, end=pos + len(MY_C0006_QUOTE))
        assert label == "Part I s. 4"
        assert heading_pos == MY_PART_BOUNDARY_TEXT.index("Section 4. Interpretation")
        assert heading_pos < MY_PART_BOUNDARY_TEXT.index(MY_C0006_TEXT)

    def test_a_quote_above_its_chunks_heading_keeps_the_chunks_label(self):
        rec = mk_rec(
            quote=MY_C0006_QUOTE,
            section="Part II s. 5",
            document_id="doc_my_test",
            chunk_id="doc_my_test:c0006",
        )
        out, decisions = repair_section_labels(
            [rec],
            MY_PART_BOUNDARY_TEXT,
            chunk_texts={"doc_my_test:c0006": MY_C0006_TEXT},
        )
        assert out[0].section == "Part II s. 5"
        assert decisions[0]["outcome"] == "no_heading"
        assert decisions[0]["old"] == decisions[0]["new"] == "Part II s. 5"

    def test_a_quote_inside_its_own_section_is_still_repaired(self):
        rec = mk_rec(
            quote="unless the data subject has given his consent to the processing.",
            section="Part II s. 5",
            document_id="doc_my_test",
            chunk_id="doc_my_test:c0007",
        )
        out, decisions = repair_section_labels(
            [rec],
            MY_PART_BOUNDARY_TEXT,
            chunk_texts={"doc_my_test:c0007": MY_C0007_TEXT},
        )
        assert out[0].section == "Part II s. 6"
        assert decisions[0]["outcome"] == "repaired"

    def test_a_continuation_chunk_still_takes_the_heading_above_it(self):
        """A chunk cut in the middle of a section carries no heading of its
        own, so the nearest heading BEFORE it is the section the quote really
        belongs to. Declining there would leave a wrong carried label in place
        and blind the gate to it."""
        rec = mk_rec(
            quote="unless the data subject has given his consent to the processing.",
            section="Part II s. 5",
            document_id="doc_my_test",
            chunk_id="doc_my_test:c0008",
        )
        out, decisions = repair_section_labels(
            [rec],
            MY_PART_BOUNDARY_TEXT,
            chunk_texts={
                "doc_my_test:c0008": (
                    "unless the data subject has given his consent to the processing."
                )
            },
        )
        assert out[0].section == "Part II s. 6"
        assert decisions[0]["outcome"] == "repaired"

    def test_a_chunk_that_cannot_be_located_falls_back_to_the_plain_search(self):
        # Offset -1 says nothing about where the record sits, so the repair
        # behaves as it does with no chunk text at all and the export gate
        # judges the result, rather than the two disagreeing.
        rec = mk_rec(
            quote="unless the data subject has given his consent to the processing.",
            section="Part II s. 5",
            document_id="doc_my_test",
            chunk_id="doc_my_test:c0007",
        )
        out, decisions = repair_section_labels(
            [rec],
            MY_PART_BOUNDARY_TEXT,
            chunk_texts={"doc_my_test:c0007": "a chunk text that is not in this stream"},
        )
        assert out[0].section == "Part II s. 6"
        assert decisions[0]["outcome"] == "repaired"

    def test_the_part_prefix_branch_obeys_the_same_rule(self):
        # Same section number, foreign Part prefix: normally repaired to the
        # document's own Part (see TestRound4Mechanisms). Not from a quote in
        # the chunk's preamble, where the derived Part comes from the heading
        # of a section that ended before this chunk began.
        rec = mk_rec(
            quote=MY_C0006_QUOTE,
            section="Part IX s. 4",
            document_id="doc_my_test",
            chunk_id="doc_my_test:c0006",
        )
        out, decisions = repair_section_labels(
            [rec],
            MY_PART_BOUNDARY_TEXT,
            chunk_texts={"doc_my_test:c0006": MY_C0006_TEXT},
        )
        assert out[0].section == "Part IX s. 4"
        assert decisions[0]["outcome"] == "no_heading"


# ---------------------------------------------------------------------------
# Article-word and Chinese statutes. The index used to know only the English
# heading shapes, so in an Indonesian act the numbered items of the
# definitions article ("2. Pelindungan Data Pribadi adalah ...") were the only
# "headings" it found, and every quote below them was relabelled "s. 2".
# ---------------------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures"
UU27_TEXT = (FIXTURES / "ocr_reference/uu27_2022_pasal1_19_excerpt.txt").read_text(encoding="utf-8")


def _cac_pipl_text() -> str:
    raw = (FIXTURES / "html/cac_pipl_articles_excerpt.htm").read_bytes()
    return extract(raw, "html", "doc_cn_cac_pipl").full_text


def _id_rec(quote: str, section: str) -> MappingRecord:
    return mk_rec(quote=quote, section=section, document_id="doc_id_uu27", chunk_id="doc_id_uu27:c0014")


class TestArticleWordStatutes:
    def test_a_correct_pasal_label_is_kept(self):
        out, decisions = repair_section_labels(
            [_id_rec("b. kepentingan proses penegakan hukum;", "s. 15")], UU27_TEXT
        )
        assert out[0].section == "s. 15"
        assert decisions[0]["outcome"] == "kept"

    def test_the_label_comes_from_the_nearest_pasal_heading(self):
        out, decisions = repair_section_labels(
            [_id_rec("Pemasangan alat pemroses atau pengolah data visual", "s. 16")], UU27_TEXT
        )
        assert out[0].section == "s. 17"
        assert decisions[0]["outcome"] == "repaired"

    def test_a_cross_reference_line_is_not_a_heading(self):
        """Below "Pasal 13 ayat (1) dan ayat (2) dikecualikan untuk:" the
        quote is still article 15, and the zone is not ambiguous."""
        out, decisions = repair_section_labels(
            [_id_rec("a. kepentingan pertahanan dan keamanan nasional;", "s. 13")], UU27_TEXT
        )
        assert out[0].section == "s. 15"
        assert decisions[0]["outcome"] == "repaired"

    def test_a_page_foot_catchword_is_not_a_heading(self):
        index = SectionLabelIndex(UU27_TEXT)
        pos = UU27_TEXT.index("Pasal 18. .") + 2
        assert index.label_at(pos) == "s. 17"


class TestArticleNumberShapes:
    TEXT = (
        "Pasal 40\n"
        "Pemerintah berwenang melakukan pemutusan akses Informasi Elektronik.\n"
        "Pasal 4 1\n"
        "Penyelenggaraan Transaksi Elektronik dapat dilakukan dalam lingkup\n"
        "publik, sebagaimana dimaksud dalam Pasal 42 ayat (2), bukan\n"
        "Pasal 40.\n"
        "Penyelenggaraan dalam lingkup privat diatur tersendiri.\n"
    )

    def test_a_split_number_is_joined(self):
        rec = _id_rec("Penyelenggaraan Transaksi Elektronik dapat", "s. 40")
        out, _ = repair_section_labels([rec], self.TEXT)
        assert out[0].section == "s. 41"

    def test_a_misread_heading_keeps_its_number(self):
        text = self.TEXT + "Pasal2T\nPengendali Data Pribadi wajib melakukan pemrosesan.\n"
        rec = _id_rec("Pengendali Data Pribadi wajib melakukan", "s. 41")
        out, _ = repair_section_labels([rec], text)
        assert out[0].section == "s. 27"

    def test_a_sentence_end_on_a_cross_reference_is_not_a_heading(self):
        rec = _id_rec("Penyelenggaraan dalam lingkup privat", "s. 41")
        out, decisions = repair_section_labels([rec], self.TEXT)
        assert out[0].section == "s. 41"
        assert decisions[0]["outcome"] == "kept"


class TestElucidation:
    TEXT = (
        "Pasal 3\n"
        "Pelindungan Data Pribadi dilaksanakan berdasarkan asas pelindungan.\n"
        "Pasal 4\n"
        "Data Pribadi terdiri atas Data Pribadi yang bersifat spesifik.\n"
        "PENJEI,ASAN\n"
        "ATAS\n"
        "UNDANG-UNDANG REPUBUK INDONESI,A\n"
        "I UMUM\n"
        "Ferkembangan teknologi informasi dan komurikasi yang melaju.\n"
        "II.\n"
        "PASALDEMIPASAL\n"
        "Pasal 3\n"
        'Yang dimaksud dengan "asas pelindungan" adalah bahwa setiap\n'
        "pemrosesan Data Pribadi dilakukan dengan memberikan pelindungan.\n"
    )

    def test_a_note_on_an_article_is_labelled_as_the_elucidation(self):
        rec = _id_rec('Yang dimaksud dengan "asas pelindungan"', "s. 3")
        out, decisions = repair_section_labels([rec], self.TEXT)
        assert out[0].section == "Elucidation s. 3"
        assert decisions[0]["outcome"] == "repaired"

    def test_the_article_itself_keeps_its_plain_label(self):
        rec = _id_rec("Pelindungan Data Pribadi dilaksanakan", "s. 3")
        out, decisions = repair_section_labels([rec], self.TEXT)
        assert out[0].section == "s. 3"
        assert decisions[0]["outcome"] == "kept"

    def test_the_general_elucidation_is_an_ambiguous_zone(self):
        """No article heads the general part, and the nearest one above it
        (article 4 of the body) is not where its text comes from."""
        index = SectionLabelIndex(self.TEXT)
        assert index.label_at(self.TEXT.index("Ferkembangan")) is None


class TestChineseStatutes:
    def test_a_correct_article_label_is_kept(self):
        text = _cac_pipl_text()
        rec = _id_rec("自然人的个人信息受法律保护", "Chapter 1 s. 2")
        out, decisions = repair_section_labels([rec], text)
        assert out[0].section == "Chapter 1 s. 2"
        assert decisions[0]["outcome"] == "kept"

    def test_the_label_names_the_article_and_its_chapter(self):
        text = _cac_pipl_text()
        rec = _id_rec("取得个人的同意", "Chapter 1 s. 12")
        out, decisions = repair_section_labels([rec], text)
        assert out[0].section == "Chapter 2 s. 13"
        assert decisions[0]["outcome"] == "repaired"
