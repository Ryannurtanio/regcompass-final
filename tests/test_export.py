"""M9 export: the graded 13-column contract plus the gate battery that must be
green before anything ships. Lanes: exact column
names and order; NEW/KNOWN case-sensitive at PROVISION level via the committed
known-matrix; zero-score absence rows carry "No provision found" with earned-0
coverage evidence; mechanical confidence composite (never LLM-reported);
INVERSE score direction (0 = open, 1 = restrictive - asserted); verbatim
recheck at emission; portal whitelist + liveness; supersession check (the MAS
Cyber Hygiene Notice 2019 cancellation regression); no template example-row
content; consolidated CSV + supplementary JSON.
"""

from __future__ import annotations

import csv
import gzip
import json
import shutil
from pathlib import Path

import pytest

from regcompass.config import load_corpus, load_crosswalk, load_known_matrix, load_portals
from regcompass.contracts import MappingRecord, Timeline
from regcompass.export import (
    COLUMNS,
    EXTRA_COLUMNS,
    SCORE_IF_PRESENT,
    ExportGateError,
    article_section,
    build_absence_rows,
    build_row,
    confidence_score,
    derive_scores,
    discovery_tag,
    export_all,
    looks_non_english,
    norm_law,
    pointer_gate,
    run_gate_battery,
)

ROOT = Path(__file__).resolve().parents[1]

SLUGS = [
    "sg_telecommunications_act_1999",
    "my_personal_data_protection_act_2010",
    "au_C2026C00098VOL01",
]


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def mk_rec(
    section: str = "Part X s. 129",
    subsection: str | None = "(1)",
    indicator: str = "6.4",
    economy: str = "MY",
    document_id: str = "doc_my_personal_data_protection_act_2010",
    quote: str = "A data user shall not transfer any personal data of a data subject",
    attempts: int = 1,
    controlling: bool = True,
    repeal_status: str | None = None,
) -> MappingRecord:
    return MappingRecord(
        mapping_id=f"{document_id}:c0130::{indicator}",
        document_id=document_id,
        chunk_id=f"{document_id}:c0130",
        economy=economy,  # type: ignore[arg-type]
        indicator_id=indicator,  # type: ignore[arg-type]
        indicator_name="Conditional flow regimes",
        section=section,
        subsection=subsection,
        verbatim_quote=quote,
        page_number=52,
        impact="Prohibits transfer outside Malaysia unless the destination is whitelisted.",
        verification_status="passed",
        relationship_to_group="complementary",
        controlling_evidence=controlling,
        timeline=Timeline(repeal_status=repeal_status),
        extraction_attempts=attempts,
    )


def gate_lookup_for(rec: MappingRecord, cosine: float = 0.62) -> dict:
    return {(rec.chunk_id, rec.indicator_id): cosine}


def load_golden_m8(slug: str) -> list[MappingRecord]:
    with gzip.open(ROOT / f"tests/golden/m8/{slug}.reconciled.json.gz", "rt", encoding="utf-8") as f:
        return [MappingRecord.model_validate(r) for r in json.load(f)["records"]]


def chunk_texts(slug: str) -> dict[str, str]:
    with gzip.open(ROOT / f"tests/golden/m5/{slug}.gated.json.gz", "rt", encoding="utf-8") as f:
        payload = json.load(f)
    return {r["chunk"]["chunk_id"]: r["chunk"]["text"] for r in payload["passed"]}


def gate_cosines(slug: str) -> dict:
    with gzip.open(ROOT / f"tests/golden/m5/{slug}.gated.json.gz", "rt", encoding="utf-8") as f:
        payload = json.load(f)
    return {
        (r["chunk"]["chunk_id"], r["indicator_id"]): r["cosine_pillar"]
        for r in payload["passed"]
    }


COVERAGE = {
    "SG": {"law": "Telecommunications Act 1999", "sections": 98, "pairs_gated": 136},
    "MY": {"law": "Personal Data Protection Act 2010", "sections": 146, "pairs_gated": 166},
    "AU": {"law": "Criminal Code Act 1995", "sections": 536, "pairs_gated": 145},
}


# ---------------------------------------------------------------------------
# the 13 columns
# ---------------------------------------------------------------------------


class TestColumns:
    def test_exact_names_and_order(self):
        assert COLUMNS == (
            "Economy",
            "Law Name",
            "Law Number / Ref",
            "Last Amended",
            "Indicator ID",
            "Article / Section",
            "Discovery Tag",
            "Location Reference",
            "Verbatim Snippet",
            "Mapping Rationale",
            "Source URL",
            "Confidence",
            "Notes",
        )

    def test_extra_columns_are_appended_never_inserted(self):
        assert not set(EXTRA_COLUMNS) & set(COLUMNS)


class TestArticleSection:
    def test_section_and_subsection_concatenate(self):
        assert article_section(mk_rec()) == "Part X s. 129(1)"

    def test_no_subsection_emits_bare_section(self):
        assert article_section(mk_rec(subsection=None)) == "Part X s. 129"

    def test_subsection_restating_the_section_number_is_deduplicated(self):
        # SG dot style: section "Part 1 s. 2" + subsection "2.(1)" rendered
        # naively duplicates the number ("s. 22.(1)").
        rec = mk_rec(section="Part 1 s. 2", subsection="2.(1)")
        assert article_section(rec) == "Part 1 s. 2(1)"

    def test_subsection_with_dash_variant_is_deduplicated(self):
        rec = mk_rec(section="s. 55C", subsection="55C.—(2)")
        assert article_section(rec) == "s. 55C(2)"

    def test_plain_subsection_is_untouched(self):
        rec = mk_rec(section="Part IIIC s. 38", subsection="(1)")
        assert article_section(rec) == "Part IIIC s. 38(1)"


# ---------------------------------------------------------------------------
# mechanical confidence
# ---------------------------------------------------------------------------


class TestConfidence:
    def test_range_and_rounding(self):
        c = confidence_score(cosine=0.62, quote_len=180, n_indicators=1, attempts=1)
        assert 0.0 <= c <= 1.0
        assert c == round(c, 2)

    def test_monotonic_in_similarity_and_quote_length(self):
        base = confidence_score(0.55, 120, 1, 1)
        assert confidence_score(0.70, 120, 1, 1) > base
        assert confidence_score(0.55, 240, 1, 1) > base

    def test_retry_and_multi_indicator_penalties(self):
        clean = confidence_score(0.62, 180, 1, 1)
        assert confidence_score(0.62, 180, 1, 2) < clean
        assert confidence_score(0.62, 180, 1, 3) < confidence_score(0.62, 180, 1, 2)
        assert confidence_score(0.62, 180, 4, 1) < clean

    def test_bands(self):
        strong = confidence_score(0.72, 300, 1, 1)
        weak = confidence_score(0.46, 40, 5, 3)
        assert strong >= 0.70
        assert weak < 0.70


# ---------------------------------------------------------------------------
# Discovery Tag (provision level, case-sensitive)
# ---------------------------------------------------------------------------


class TestDiscoveryTag:
    matrix = load_known_matrix()

    def test_known_at_provision_level(self):
        tag, scope = discovery_tag(mk_rec(), "Personal Data Protection Act 2010", self.matrix)
        assert tag == "KNOWN"
        assert scope is None

    def test_known_law_level_when_escap_row_is_article_less(self):
        rec = mk_rec(
            section="Part 4 s. 30",
            indicator="7.3",
            economy="SG",
            document_id="doc_sg_telecommunications_act_1999",
        )
        tag, scope = discovery_tag(rec, "Telecommunications Act 1999", self.matrix)
        assert tag == "KNOWN"

    def test_known_law_level_via_article_less_row(self):
        # ESCAP's MY 7.3 row cites the PDPA with no sections: law-level governs
        rec = mk_rec(section="Part IX s. 125", indicator="7.3", economy="MY")
        tag, _ = discovery_tag(rec, "Personal Data Protection Act 2010", self.matrix)
        assert tag == "KNOWN"

    def test_new_provision_inside_a_known_law(self):
        # ESCAP's MY 7.2 rows cite only the Computer Crimes Act and the Cyber
        # Security Act; a PDPA provision on 7.2 is NEW at provision level
        rec = mk_rec(section="Part II s. 9", indicator="7.2", economy="MY")
        tag, scope = discovery_tag(rec, "Personal Data Protection Act 2010", self.matrix)
        assert tag == "NEW"
        assert scope == "provision"

    def test_new_law(self):
        rec = mk_rec(document_id="doc_my_totally_novel_act_2025", indicator="6.4")
        tag, scope = discovery_tag(rec, "Totally Novel Data Act 2025", self.matrix)
        assert tag == "NEW"
        assert scope == "law"

    def test_conjunction_cited_sibling_is_known(self):
        # ESCAP's SG 7.5 comment cites "(section 39 and 40)": s. 40 must be
        # KNOWN like its sibling s. 39 (the old parser dropped
        # every token after the first number).
        for sec in ("Part 4 s. 39", "Part 4 s. 40"):
            rec = mk_rec(
                section=sec,
                indicator="7.5",
                economy="SG",
                document_id="doc_sg_criminal_procedure_code_2010",
            )
            tag, _ = discovery_tag(rec, "Criminal Procedure Code 2010", self.matrix)
            assert tag == "KNOWN", sec

    def test_range_cited_section_is_known(self):
        # ESCAP's MY 7.2 comment cites "Sections 15-24": every interior
        # section is a database citation, not a discovery.
        rec = mk_rec(
            section="Part IV s. 24",
            indicator="7.2",
            economy="MY",
            document_id="doc_my_Act_854",
        )
        tag, _ = discovery_tag(rec, "Cyber Security Act 2024", self.matrix)
        assert tag == "KNOWN"

    def test_known_matrix_law_name_decouples_display_from_matching(self):
        # The MY PDPA amendment displays its assented Act
        # title, while KNOWN matching keeps the pre-assent Bill title the
        # ESCAP database cites. A naive rename flips these rows to NEW.
        corpus = load_corpus()
        doc = corpus["doc_my_Act_A1727"]
        assert doc.law_name == "Personal Data Protection (Amendment) Act 2024"
        assert doc.known_matrix_law_name == "Personal Data Protection (Amendment) Bill 2024"
        for sec, ind in (("s. 9", "6.4"), ("s. 5", "7.1")):
            rec = mk_rec(
                section=sec, indicator=ind, economy="MY", document_id="doc_my_Act_A1727"
            )
            row = build_row(
                rec,
                doc,
                load_crosswalk(),
                self.matrix,
                gate_lookup_for(rec),
                {rec.chunk_id: 1},
            )
            assert row["Law Name"] == "Personal Data Protection (Amendment) Act 2024"
            assert row["Discovery Tag"] == "KNOWN", (sec, ind)
        # the Act title alone (no override) would NOT match: the decoupling
        # is load-bearing, not decorative
        tag, _ = discovery_tag(
            mk_rec(section="s. 9", indicator="6.4", economy="MY"),
            "Personal Data Protection (Amendment) Act 2024",
            self.matrix,
        )
        assert tag == "NEW"

    def test_norm_law_matching_survives_act_number_parentheticals(self):
        assert norm_law("Personal Data Protection Act (Act 709) 2010") == norm_law(
            "Personal  Data Protection Act 2010"
        )


# ---------------------------------------------------------------------------
# score derivation: INVERSE direction (0 = open, 1 = restrictive)
# ---------------------------------------------------------------------------


class TestScores:
    def test_inverse_direction_is_asserted_in_the_map(self):
        # presence of a data-protection / cybersecurity FRAMEWORK is openness
        assert SCORE_IF_PRESENT["7.1"] == 0.0
        assert SCORE_IF_PRESENT["7.2"] == 0.0
        # presence of a restriction scores restrictive
        for ind in ("6.1", "6.2", "6.3", "6.4", "7.3", "7.4", "7.5"):
            assert SCORE_IF_PRESENT[ind] == 1.0

    def test_derive_scores_my_ground_truth_spots(self):
        records = load_golden_m8("my_personal_data_protection_act_2010")
        scores = derive_scores(records)
        assert scores[("MY", "6.1")] == 0.0  # ESCAP: no ban exists
        assert scores[("MY", "6.4")] == 1.0  # ESCAP: conditional regime exists
        assert scores[("MY", "7.1")] == 0.0  # ESCAP: comprehensive framework exists

    def test_all_derived_scores_are_allowed_values(self):
        for slug in SLUGS:
            for (econ, ind), s in derive_scores(load_golden_m8(slug)).items():
                assert s in (0.0, 0.5, 1.0)
                if ind == "7.3":
                    assert s in (0.0, 1.0)  # binary per methodology sheet


# ---------------------------------------------------------------------------
# absence rows (a 0 must be EARNED)
# ---------------------------------------------------------------------------


class TestAbsenceRows:
    # coverage_stats declares which economies were SEARCHED: since the U0
    # review gate landed, every economy in it earns absence rows even with
    # zero surviving records, so these AU-record unit tests pass AU-only stats.
    AU_COVERAGE = {"AU": COVERAGE["AU"]}

    def test_absent_indicators_get_no_provision_found_rows(self):
        records = load_golden_m8("au_C2026C00098VOL01")
        present = {r.indicator_id for r in records}
        rows = build_absence_rows(records, load_corpus(), load_crosswalk(), self.AU_COVERAGE)
        absent = {r["Indicator ID"] for r in rows}
        assert absent == {i for i in SCORE_IF_PRESENT if i not in present}
        for row in rows:
            assert row["Article / Section"] == "No provision found"
            assert row["Verbatim Snippet"] == "No provision found"
            assert "Criminal Code Act 1995" in row["Notes"]  # reference basis
            assert "536 sections" in row["Notes"]  # search coverage evidence
            assert row["Law Name"] == "Criminal Code Act 1995"
            assert row["Discovery Tag"] in ("NEW", "KNOWN")

    def test_searched_economy_with_no_records_still_earns_absence_rows(self):
        """The U0 review-gate lane at the unit level: an economy listed in
        coverage_stats whose records were all excluded gets a full set of
        'No provision found' rows, never silence."""
        rows = build_absence_rows([], load_corpus(), load_crosswalk(), self.AU_COVERAGE)
        assert {r["Indicator ID"] for r in rows} == set(SCORE_IF_PRESENT)
        assert all(r["Economy"] == "Australia" for r in rows)

    def test_economy_with_records_but_no_stats_entry_does_not_crash(self):
        """The union comment promised this lane and line 293 crashed it with
        a KeyError: an economy with surviving
        records but no coverage_stats entry still earns its absence rows,
        with Notes citing the reference-basis law minus the search counts."""
        records = load_golden_m8("au_C2026C00098VOL01")
        present = {r.indicator_id for r in records}
        rows = build_absence_rows(records, load_corpus(), load_crosswalk(), {})
        assert {r["Indicator ID"] for r in rows} == {
            i for i in SCORE_IF_PRESENT if i not in present
        }
        for row in rows:
            assert row["Article / Section"] == "No provision found"
            assert "Reference basis:" in row["Notes"]
            assert "sections chunked" not in row["Notes"]  # no stats to cite

    def test_reference_basis_follows_coverage_stats_law(self):
        """The absence row's Law Name is the corpus doc named by
        coverage_stats, not whichever entry happens to be last in
        corpus.yaml."""
        records = load_golden_m8("au_C2026C00098VOL01")
        corpus = load_corpus()
        au_names = [d.law_name for d in corpus.values() if d.economy == "AU"]
        assert au_names[-1] != "Criminal Code Act 1995"  # order must not matter
        rows = build_absence_rows(records, corpus, load_crosswalk(), self.AU_COVERAGE)
        assert rows and all(r["Law Name"] == "Criminal Code Act 1995" for r in rows)

    def test_multi_document_stats_fall_back_to_last_corpus_entry(self):
        """A full-corpus run passes an aggregate stats law ('18 acts...') that
        matches no corpus entry; the economy's last corpus.yaml entry then
        carries the Law Name columns."""
        records = load_golden_m8("au_C2026C00098VOL01")
        corpus = load_corpus()
        stats = {"AU": {"law": "18 Australian acts", "sections": 9, "pairs_gated": 9}}
        rows = build_absence_rows(records, corpus, load_crosswalk(), stats)
        last_au = [d for d in corpus.values() if d.economy == "AU"][-1]
        assert rows and all(r["Law Name"] == last_au.law_name for r in rows)


# ---------------------------------------------------------------------------
# gate battery
# ---------------------------------------------------------------------------


def good_row_and_lookups():
    rec = mk_rec()
    corpus = load_corpus()
    row = build_row(
        rec,
        corpus[rec.document_id],
        load_crosswalk(),
        load_known_matrix(),
        gate_lookup_for(rec),
        {rec.chunk_id: 1},
    )
    texts = {rec.chunk_id: "PREFIX " + rec.verbatim_quote + " SUFFIX"}
    return row, texts


class TestGateBattery:
    portals = load_portals()

    def _run(self, rows, texts, liveness=lambda url: True):
        return run_gate_battery(rows, texts, self.portals, liveness)

    def test_green_on_a_well_formed_row(self):
        row, texts = good_row_and_lookups()
        assert self._run([row], texts) == []

    # Controlling-Evidence=true gate: a row whose classification
    # does not evidence its indicator is a fit-filter breach unless the row
    # says "controlling by legal hierarchy only" (the no-fit fallback).
    def test_controlling_row_must_evidence_its_indicator(self):
        rec = mk_rec().model_copy(
            update={"measure_type": "government_access", "rdtii_score_contribution": None}
        )
        corpus = load_corpus()
        row = build_row(
            rec, corpus[rec.document_id], load_crosswalk(), load_known_matrix(),
            gate_lookup_for(rec), {rec.chunk_id: 1},
        )
        texts = {rec.chunk_id: "PREFIX " + rec.verbatim_quote + " SUFFIX"}
        # build_row itself flags this state (hierarchy-only note), so the
        # battery is green; strip the note to simulate the breach
        assert "controlling by legal hierarchy only" in row["Notes"]
        assert self._run([row], texts) == []
        row["Notes"] = row["Notes"].replace(
            "controlling by legal hierarchy only: no group member's classification evidences this indicator",
            "",
        )
        assert any("does not evidence its indicator" in f for f in self._run([row], texts))

    def test_fitting_controlling_row_needs_no_flag(self):
        rec = mk_rec().model_copy(
            update={"measure_type": "conditional_transfer", "rdtii_score_contribution": 1.0}
        )
        corpus = load_corpus()
        row = build_row(
            rec, corpus[rec.document_id], load_crosswalk(), load_known_matrix(),
            gate_lookup_for(rec), {rec.chunk_id: 1},
        )
        texts = {rec.chunk_id: "PREFIX " + rec.verbatim_quote + " SUFFIX"}
        assert "controlling by legal hierarchy only" not in row["Notes"]
        assert self._run([row], texts) == []

    # A snippet stays in its source language, and a
    # non-English snippet REQUIRES the appended "Verbatim English" column.
    # The Malay sentence is synthetic test data in PDPA style, not a quote.
    MALAY = (
        "Seseorang tidak boleh memindahkan apa-apa data peribadi ke sesuatu "
        "tempat di luar negara melainkan jika ditetapkan oleh pihak berkuasa."
    )

    def test_non_english_snippet_without_english_column_fails(self):
        row, _ = good_row_and_lookups()
        row["Verbatim Snippet"] = self.MALAY
        texts = {row["_chunk_id"]: "PREFIX " + self.MALAY + " SUFFIX"}
        assert any("Verbatim English" in f for f in self._run([row], texts))

    def test_non_english_snippet_with_labelled_translation_passes(self):
        row, _ = good_row_and_lookups()
        row["Verbatim Snippet"] = self.MALAY
        row["Verbatim English"] = (
            "[AI translation, non-authoritative] A person must not transfer "
            "any personal data to a place outside the country unless "
            "prescribed by the relevant authority."
        )
        texts = {row["_chunk_id"]: "PREFIX " + self.MALAY + " SUFFIX"}
        assert self._run([row], texts) == []

    def test_unlabelled_translation_fails(self):
        # translations are non-authoritative and AI-labelled
        row, texts = good_row_and_lookups()
        row["Verbatim English"] = "An unlabelled translation."
        assert any("label" in f for f in self._run([row], texts))

    def test_translations_load_from_reviewed_config_with_label_composed(self):
        from regcompass.config import CONFIG_DIR
        from regcompass.export import _verbatim_english

        translations = _verbatim_english(str(CONFIG_DIR))
        # the one Malay provision in the Round 1 corpus: three rows, one quote
        assert len(translations) == 3
        for mapping_id, text in translations.items():
            assert mapping_id.startswith("doc_my_20141230_A1472_BI_Act_A1472:c0008::7.")
            assert text.startswith("[AI translation, non-authoritative] ")
            assert "intercept and retain" in text

    def test_non_english_detector_is_conservative(self):
        # every real English quote passes; short snippets are never flagged
        assert not looks_non_english(
            "A person shall not transfer any telecommunication licence "
            "without the prior written consent of the Authority."
        )
        assert not looks_non_english("Data peribadi dilindungi.")  # < 8 words
        assert looks_non_english(self.MALAY)

    def test_the_detector_flags_a_script_that_has_no_latin_words(self):
        """The function-word probe counts Latin words, so pure Chinese scored
        zero words and passed as English: the gloss requirement never fired on
        a Chinese row. A statute written outside the Latin alphabet is
        non-English whatever the probe can read."""
        chinese = (
            "第五条 处理个人信息应当遵循合法、正当、必要和诚信原则，"
            "不得通过误导、欺诈、胁迫等方式处理个人信息。"
        )
        lao = "ມາດຕາ 5 ຫຼັກການກ່ຽວກັບວຽກງານທຸລະກໍາທາງເອເລັກໂຕຣນິກ"
        thai = "มาตรา ๕ การประกอบธุรกิจบริการเกี่ยวกับธุรกรรมทางอิเล็กทรอนิกส์"
        for text in (chinese, lao, thai):
            assert looks_non_english(text)

    def test_a_latin_token_inside_a_chinese_quote_does_not_rescue_it(self):
        """A statute quoting an English term or an acronym is still Chinese;
        a handful of Latin tokens must not put it under the 8-word floor and
        back into the English lane."""
        assert looks_non_english(
            "第三条 在中华人民共和国境内处理自然人个人信息的活动 (Personal Information) 适用本法。"
        )

    def test_a_short_english_snippet_is_still_not_flagged(self):
        assert not looks_non_english("Section 5 applies.")
        assert not looks_non_english("2021")
        assert not looks_non_english("")

    def test_column_order_enforced(self):
        row, texts = good_row_and_lookups()
        broken = {k: row[k] for k in reversed(list(row))}
        assert any("column" in f for f in self._run([broken], texts))

    def test_discovery_tag_case_enforced(self):
        row, texts = good_row_and_lookups()
        row["Discovery Tag"] = "known"
        assert any("Discovery Tag" in f for f in self._run([row], texts))

    def test_required_fields_enforced(self):
        row, texts = good_row_and_lookups()
        row["Law Name"] = ""
        assert any("Law Name" in f for f in self._run([row], texts))

    def test_verbatim_recheck_catches_paraphrase(self):
        row, texts = good_row_and_lookups()
        row["Verbatim Snippet"] = "data users may not send data overseas"
        assert any("verbatim" in f.lower() for f in self._run([row], texts))

    def test_url_host_whitelist(self):
        row, texts = good_row_and_lookups()
        row["Source URL"] = "https://www.google.com/search?q=pdpa"
        assert any("whitelist" in f.lower() for f in self._run([row], texts))

    def test_url_liveness(self):
        row, texts = good_row_and_lookups()
        fails = self._run([row], texts, liveness=lambda url: False)
        assert any("liveness" in f.lower() or "dead" in f.lower() for f in fails)

    def test_supersession_regression_cancelled_notice_cannot_control(self):
        # MAS Cyber Hygiene Notice 2019 was cancelled 1 Jul 2022 (FSM-N16
        # replaced it): a repealed/cancelled instrument must never control
        rec = mk_rec(repeal_status="Cancelled 1 Jul 2022, replaced by FSM-N16")
        corpus = load_corpus()
        row = build_row(
            rec, corpus[rec.document_id], load_crosswalk(), load_known_matrix(),
            gate_lookup_for(rec), {rec.chunk_id: 1},
        )
        texts = {rec.chunk_id: rec.verbatim_quote}
        assert any("cancel" in f.lower() or "repeal" in f.lower() for f in self._run([row], texts))

    def test_repealed_but_recorded_non_controlling_passes_with_note(self):
        rec = mk_rec(controlling=False, repeal_status="Cancelled 1 Jul 2022")
        corpus = load_corpus()
        row = build_row(
            rec, corpus[rec.document_id], load_crosswalk(), load_known_matrix(),
            gate_lookup_for(rec), {rec.chunk_id: 1},
        )
        texts = {rec.chunk_id: rec.verbatim_quote}
        assert self._run([row], texts) == []
        assert "repealed-but-recorded" in row["Notes"]

    def test_pillar6_government_access_row_carries_the_scope_note(self):
        # a 6.x row classified government_access is out of
        # scope for Pillar 6 scoring; the demotion must be visible on the row
        rec = mk_rec(indicator="6.1").model_copy(update={"measure_type": "government_access"})
        corpus = load_corpus()
        row = build_row(
            rec, corpus[rec.document_id], load_crosswalk(), load_known_matrix(),
            gate_lookup_for(rec), {rec.chunk_id: 1},
        )
        assert "out of scope for Pillar 6 scoring" in row["Notes"]
        # 7.5 is the home of access powers: no note there
        rec75 = mk_rec(indicator="7.5").model_copy(update={"measure_type": "government_access"})
        row75 = build_row(
            rec75, corpus[rec75.document_id], load_crosswalk(), load_known_matrix(),
            gate_lookup_for(rec75), {rec75.chunk_id: 1},
        )
        assert "out of scope" not in row75["Notes"]

    def test_template_example_rows_are_rejected(self):
        row, texts = good_row_and_lookups()
        row["Verbatim Snippet"] = (
            '"An organisation shall not transfer personal data to a country or '
            'territory outside Singapore except in accordance..."'
        )
        texts = {mk_rec().chunk_id: row["Verbatim Snippet"]}
        assert any("template" in f.lower() for f in self._run([row], texts))

    def test_real_law_sharing_the_example_register_id_is_not_flagged(self):
        """The template's AU example row cites C2004A02124, which IS the real
        TIA 1979 register ID: a legitimate mapping of that act (same ID in its
        Source URL) must never be rejected as an example-row leak."""
        row, texts = good_row_and_lookups()
        row["Source URL"] = "https://www.legislation.gov.au/C2004A02124/2026-06-04/2026-06-04/text/original/pdf/2"
        row["_economy_code"] = "AU"
        failures = self._run([row], texts)
        assert not any("template" in f.lower() for f in failures)

    def test_rationale_length_cap(self):
        row, texts = good_row_and_lookups()
        row["Mapping Rationale"] = "x" * 301
        assert any("300" in f for f in self._run([row], texts))

    def test_confidence_never_blank_on_substantive_rows_and_in_range(self):
        row, texts = good_row_and_lookups()
        assert 0.0 <= float(row["Confidence"]) <= 1.0


# ---------------------------------------------------------------------------
# the full export (integration over the real golden dataset, offline)
# ---------------------------------------------------------------------------


class TestExportAll:
    def _export(self, tmp_path):
        records, texts, cosines = [], {}, {}
        for slug in SLUGS:
            records.extend(load_golden_m8(slug))
            texts.update(chunk_texts(slug))
            cosines.update(gate_cosines(slug))
        return export_all(
            tmp_path,
            records,
            chunk_text_lookup=texts,
            gate_cosine_lookup=cosines,
            coverage_stats=COVERAGE,
            liveness_fn=lambda url: True,
        )

    def test_ships_csv_json_and_green_battery(self, tmp_path):
        result = self._export(tmp_path)
        assert result.battery_failures == []
        assert result.csv_path.exists()
        assert result.supplementary_path.exists()
        assert len(result.working_json_paths) >= 4  # per (document, pillar)
        with result.csv_path.open(encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
        assert tuple(rows[0][:13]) == COLUMNS
        assert tuple(rows[0][13:]) == EXTRA_COLUMNS
        n_substantive = sum(1 for r in rows[1:] if r[5] != "No provision found")
        n_absence = sum(1 for r in rows[1:] if r[5] == "No provision found")
        assert n_substantive == 93  # every verified record recorded
        assert n_absence == 27 - 24  # 3 economies x 9 indicators - covered groups
        supp = json.loads(result.supplementary_path.read_text(encoding="utf-8"))
        assert supp["derived_scores"]["MY"]["6.4"] == 1.0
        assert supp["derived_scores"]["MY"]["6.1"] == 0.0
        assert supp["derived_scores"]["MY"]["7.1"] == 0.0

    def test_csv_opens_with_a_bom_and_reads_back_clean(self, tmp_path):
        """Without a BOM, Excel-by-double-click decodes
        the file with the system codepage and garbles exactly the verbatim
        typography a judge must read. utf-8-sig fixes display; readers using
        utf-8-sig (csv/pandas/Excel) see identical content."""
        result = self._export(tmp_path)
        assert result.csv_path.read_bytes().startswith(b"\xef\xbb\xbf")
        with result.csv_path.open(encoding="utf-8-sig") as f:
            header = next(csv.reader(f))
        assert header[0] == COLUMNS[0]  # no BOM residue on the first cell

    def test_no_blank_last_amended_means_no_warning(self, tmp_path):
        # all three fixture corpus entries carry a last_amended date
        result = self._export(tmp_path)
        supp = json.loads(result.supplementary_path.read_text(encoding="utf-8"))
        assert "warnings" not in supp

    def test_blank_last_amended_counted_as_warning_not_failure(self, tmp_path, monkeypatch):
        """A document that declares no date ships blank (never guessed): the
        battery stays green and the supplementary counts the blanks."""
        import regcompass.export as export_mod

        real_load = export_mod.load_corpus

        def corpus_without_dates(config_dir=None):
            corpus = real_load(config_dir) if config_dir else real_load()
            return {
                k: v.model_copy(update={"last_amended": None}) for k, v in corpus.items()
            }

        monkeypatch.setattr(export_mod, "load_corpus", corpus_without_dates)
        result = self._export(tmp_path)
        assert result.battery_failures == []
        supp = json.loads(result.supplementary_path.read_text(encoding="utf-8"))
        assert supp["warnings"]["last_amended_blank_rows"] == supp["n_rows"]
        assert "never guessed" in supp["warnings"]["note"]

    def test_red_battery_ships_nothing(self, tmp_path):
        records = load_golden_m8("my_personal_data_protection_act_2010")
        with pytest.raises(ExportGateError):
            export_all(
                tmp_path,
                records,
                chunk_text_lookup={},  # verbatim recheck must fail
                gate_cosine_lookup={},
                coverage_stats=COVERAGE,
                liveness_fn=lambda url: True,
            )
        assert not (tmp_path / "submission.csv").exists()

    def test_working_json_is_contract_valid(self, tmp_path):
        from regcompass.contracts import DocumentMapping

        result = self._export(tmp_path)
        for p in result.working_json_paths:
            dm = DocumentMapping.model_validate_json(p.read_text(encoding="utf-8"))
            assert dm.mappings, p.name


class TestSubmissionJson:
    """Stage A: the consolidated submission.json (judge-recommended JSON fields)
    written from the shared export chokepoint alongside the CSV."""

    def _export(self, tmp_path, **kwargs):
        records, texts, cosines = [], {}, {}
        for slug in SLUGS:
            records.extend(load_golden_m8(slug))
            texts.update(chunk_texts(slug))
            cosines.update(gate_cosines(slug))
        return export_all(
            tmp_path,
            records,
            chunk_text_lookup=texts,
            gate_cosine_lookup=cosines,
            coverage_stats=COVERAGE,
            liveness_fn=lambda url: True,
            **kwargs,
        )

    def test_submission_json_is_written_and_shaped(self, tmp_path):
        result = self._export(tmp_path)
        assert result.submission_path.exists()
        assert result.submission_path.name == "submission.json"
        sub = json.loads(result.submission_path.read_text(encoding="utf-8"))
        assert set(sub) == {"run_metadata", "economies", "derived_scores"}
        rm = sub["run_metadata"]
        assert rm["tool_version"]
        assert rm["indicator_scheme"] == "numeric"
        assert rm["battery"] == "green"
        # exactly what ran: the selected Engine plus the shared embedder
        assert set(rm["models"]) == {"engine", "embedder"}
        for spec in rm["models"].values():
            assert spec["name"] and spec["litellm_model"]
        assert rm["models"]["engine"]["display_name"]
        assert "processing_time" in rm
        assert "extractors" in rm

    def test_provisions_mirror_the_csv_rows(self, tmp_path):
        result = self._export(tmp_path)
        sub = json.loads(result.submission_path.read_text(encoding="utf-8"))
        provisions = [p for e in sub["economies"] for law in e["laws"] for p in law["provisions"]]
        with result.csv_path.open(encoding="utf-8-sig") as f:
            data_rows = list(csv.reader(f))[1:]
        # one provision per CSV data row, grouped consistently
        assert len(provisions) == len(data_rows)
        supp = json.loads(result.supplementary_path.read_text(encoding="utf-8"))
        assert len(provisions) == supp["n_rows"]
        # every (indicator, article/section, snippet) triple lines up with a row
        prov_keys = sorted(
            (p["indicator_id"], p["article_section"], p["verbatim_snippet"]) for p in provisions
        )
        row_keys = sorted((r[4], r[5], r[8]) for r in data_rows)
        assert prov_keys == row_keys

    def test_derived_scores_mirror_supplementary(self, tmp_path):
        result = self._export(tmp_path)
        sub = json.loads(result.submission_path.read_text(encoding="utf-8"))
        supp = json.loads(result.supplementary_path.read_text(encoding="utf-8"))
        assert sub["derived_scores"] == supp["derived_scores"]

    def test_raw_context_is_a_bounded_window_around_the_quote(self, tmp_path):
        result = self._export(tmp_path)
        sub = json.loads(result.submission_path.read_text(encoding="utf-8"))
        saw_substantive = False
        for e in sub["economies"]:
            for law in e["laws"]:
                for p in law["provisions"]:
                    if p["verbatim_snippet"] == "No provision found":
                        assert p["raw_context"] is None  # absence rows carry no chunk
                        continue
                    rc = p["raw_context"]
                    assert rc is not None
                    saw_substantive = True
                    assert p["verbatim_snippet"] in rc  # quote sits inside its window
                    # +-300 around the quote, clipped to chunk bounds
                    assert len(rc) <= len(p["verbatim_snippet"]) + 2 * 300
        assert saw_substantive

    def test_ocr_quality_block_is_honest_nulls_without_a_db(self, tmp_path):
        result = self._export(tmp_path)
        sub = json.loads(result.submission_path.read_text(encoding="utf-8"))
        law = sub["economies"][0]["laws"][0]
        oq = law["ocr_quality"]
        assert oq["ocr_quality_cer"] is None  # measured CER is never guessed
        assert "hand-checked reference" in oq["note"]
        for field in (
            "mean_word_confidence",
            "dictionary_hit_rate",
            "cer_proxy_flag",
            "escalated_to_rapidocr",
            "manual_review",
        ):
            assert oq[field] is None
        assert law["source_pdf_path"] is None  # no document_meta passed

    def test_document_meta_flows_into_the_json(self, tmp_path):
        records = []
        for slug in SLUGS:
            records.extend(load_golden_m8(slug))
        doc_id = records[0].document_id
        document_meta = {
            doc_id: {
                "local_path": "/abs/machine/prefix/tests/fixtures/sample.pdf",
                "extractor": "pdfplumber",
                "extractor_version": "0.11.4",
                "ocr_applied": True,
                "mean_word_confidence": 0.91,
                "dictionary_hit_rate": 0.88,
                "cer_proxy_flag": False,
                "escalated_to_rapidocr": False,
                "manual_review": False,
            }
        }
        result = self._export(tmp_path, document_meta=document_meta)
        sub = json.loads(result.submission_path.read_text(encoding="utf-8"))
        # source_pdf_path normalized to a relative POSIX path anchored on tests/
        found = None
        for e in sub["economies"]:
            for law in e["laws"]:
                for p in law["provisions"]:
                    if p["chunk_id"].startswith(doc_id):
                        found = law
        assert found is not None
        assert found["source_pdf_path"] == "tests/fixtures/sample.pdf"
        assert found["ocr_quality"]["ocr_applied"] is True
        assert found["ocr_quality"]["mean_word_confidence"] == 0.91
        assert found["ocr_quality"]["ocr_quality_cer"] is None  # still null
        # extractors aggregate reaches run_metadata
        assert {"engine": "pdfplumber", "version": "0.11.4"} in sub["run_metadata"]["extractors"]

    def test_generated_at_is_injectable_and_omitted_for_determinism(self, tmp_path):
        default = self._export(tmp_path / "a")
        sub_default = json.loads(default.submission_path.read_text(encoding="utf-8"))
        assert "generated_at" not in sub_default["run_metadata"]  # deterministic golden mode
        stamped = self._export(tmp_path / "b", generated_at="2026-07-19T00:00:00+00:00")
        sub_stamped = json.loads(stamped.submission_path.read_text(encoding="utf-8"))
        assert sub_stamped["run_metadata"]["generated_at"] == "2026-07-19T00:00:00+00:00"
        # omitting generated_at yields byte-identical output across runs
        again = self._export(tmp_path / "c")
        assert (
            default.submission_path.read_bytes() == again.submission_path.read_bytes()
        )

    def test_submission_json_matches_committed_golden(self, tmp_path):
        result = self._export(tmp_path)
        golden = ROOT / "tests/golden/m9/submission.json"
        assert result.submission_path.read_bytes() == golden.read_bytes()


class TestExportWithClassifications:
    """M12 integration: classifications switch supplementary to the rubric
    method without touching the 13-column contract or the row set."""

    def _classify_all(self, records):
        from regcompass.classify import ELIGIBLE_NATURES, ProvisionClassification

        out = {}
        for r in records:
            if r.verification_status != "passed":
                continue
            nature = sorted(ELIGIBLE_NATURES[r.indicator_id])[0]
            out[r.mapping_id] = ProvisionClassification(
                measure_nature=nature,
                data_scope="personal",
                application="horizontal",
                government_data_only=False,
                minimum_period_specified=True if r.indicator_id == "7.3" else None,
                comprehensive_framework=True if r.indicator_id == "7.1" else None,
                dedicated_framework=True if r.indicator_id == "7.2" else None,
                judicial_authorization_required=False if r.indicator_id == "7.5" else None,
            )
        return out

    def _export(self, tmp_path, drop_one_classification=False):
        records, texts, cosines = [], {}, {}
        for slug in SLUGS:
            records.extend(load_golden_m8(slug))
            texts.update(chunk_texts(slug))
            cosines.update(gate_cosines(slug))
        classifications = self._classify_all(records)
        if drop_one_classification:
            classifications.pop(sorted(classifications)[0])
        result = export_all(
            tmp_path,
            records,
            chunk_text_lookup=texts,
            gate_cosine_lookup=cosines,
            coverage_stats=COVERAGE,
            liveness_fn=lambda url: True,
            classifications=classifications,
        )
        return result, records, classifications

    def test_rubric_method_with_details_and_unchanged_contract(self, tmp_path):
        result, records, classifications = self._export(tmp_path)
        with result.csv_path.open(encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
        assert tuple(rows[0][:13]) == COLUMNS  # contract untouched
        assert sum(1 for r in rows[1:] if r[5] != "No provision found") == 93
        supp = json.loads(result.supplementary_path.read_text(encoding="utf-8"))
        assert "classification rubric" in supp["score_method"]
        assert "presence_scores_v1" in supp
        summary = supp["classification_summary"]
        assert summary["n_classified"] == len(classifications)
        assert summary["n_unclassified"] == 0
        # every scored cell names its controlling provision or has none honestly
        for econ, cells in supp["score_details"].items():
            for ind, detail in cells.items():
                assert detail["score"] == supp["derived_scores"][econ][ind]
                if detail["controlling_mapping_id"] is not None:
                    assert detail["controlling_mapping_id"] in {r.mapping_id for r in records}

    def test_working_json_carries_measure_type(self, tmp_path):
        result, _, classifications = self._export(tmp_path)
        from regcompass.contracts import DocumentMapping

        seen = set()
        for p in result.working_json_paths:
            dm = DocumentMapping.model_validate_json(p.read_text(encoding="utf-8"))
            for m in dm.mappings:
                if m.mapping_id in classifications:
                    assert m.measure_type is not None
                    seen.add(m.mapping_id)
        assert seen

    def test_unclassified_record_is_counted_not_scored(self, tmp_path):
        result, _, classifications = self._export(tmp_path, drop_one_classification=True)
        supp = json.loads(result.supplementary_path.read_text(encoding="utf-8"))
        assert supp["classification_summary"]["n_unclassified"] == 1


# ---------------------------------------------------------------------------
# pointer gate + committed review-drop lane
# ---------------------------------------------------------------------------

POINTER_TEXT = "\n".join(
    [
        "Part 3.3—Data breach responsibilities",
        "37 Interaction with Part IIIC of the Privacy Act 1988",
        "This section sets out the relationship between this Act and",
        "Part IIIC of the Privacy Act 1988 (notification of eligible data",
        "breaches), as it applies to data scheme entities.",
        "38 Notify Commissioner of non-personal data breach",
        "A data scheme entity must notify the Commissioner of any breach",
        "involving non-personal data as soon as practicable.",
        "39 Remedial obligations",
        "The entity must take reasonable steps to mitigate the breach.",
    ]
)


def pointer_rec(section: str, quote: str, controlling: bool = True) -> MappingRecord:
    rec = mk_rec(section=section, quote=quote, controlling=controlling)
    return rec


class TestPointerGate:
    LOADER = staticmethod(lambda doc_id: POINTER_TEXT)

    def test_correct_label_passes(self):
        rec = pointer_rec(
            "Part 3.3 s. 38",
            "A data scheme entity must notify the Commissioner of any breach",
        )
        failures, notes = pointer_gate([rec], self.LOADER)
        assert failures == [] and notes == []

    def test_wrong_section_number_fails(self):
        rec = pointer_rec(
            "Part 3.3 s. 37",
            "A data scheme entity must notify the Commissioner of any breach",
        )
        failures, _ = pointer_gate([rec], self.LOADER)
        assert len(failures) == 1
        assert "does not name the nearest heading" in failures[0]

    def test_foreign_part_prefix_fails(self):
        rec = pointer_rec(
            "Part IIIC s. 38",
            "A data scheme entity must notify the Commissioner of any breach",
        )
        failures, _ = pointer_gate([rec], self.LOADER)
        assert len(failures) == 1
        assert "Part prefix" in failures[0]

    def test_non_controlling_rows_are_ignored(self):
        rec = pointer_rec(
            "Part IIIC s. 37",
            "A data scheme entity must notify the Commissioner of any breach",
            controlling=False,
        )
        failures, notes = pointer_gate([rec], self.LOADER)
        assert failures == [] and notes == []

    def test_unlocatable_quote_is_a_note_not_a_pass(self):
        rec = pointer_rec("Part 3.3 s. 38", "this sentence is not in the stream")
        failures, notes = pointer_gate([rec], self.LOADER)
        assert failures == []
        assert len(notes) == 1 and "not located" in notes[0]

    def test_ambiguous_zone_is_a_note_not_a_failure(self):
        # a stream with no headings at all: label_at fail-closes to None
        rec = pointer_rec("Part 3.3 s. 38", "only prose here about breaches")
        failures, notes = pointer_gate(
            [rec], lambda doc_id: "only prose here about breaches\nand more prose"
        )
        assert failures == []
        assert len(notes) == 1 and "fail-closed" in notes[0]


# A Part boundary: the chunk that opens Part II carries the Part line and a
# Division line ABOVE its own "Section 5." heading, so a quote taken from that
# preamble has the LAST section of Part I as its nearest preceding heading.
# The gate cannot judge such a row, but it must still judge a continuation
# chunk, which legitimately has no heading of its own.
GATE_PART_BOUNDARY_TEXT = "\n".join(
    [
        "PART I - PRELIMINARY",
        "Section 4. Interpretation",
        "In this Act, unless the context otherwise requires, the words below",
        "have the meanings assigned to them in this section.",
        "PART II - PERSONAL DATA PROTECTION",
        "Division 1 - Personal Data Protection Principles",
        "Section 5. Personal Data Protection Principles",
        "(1) The processing of personal data by a data user shall be in",
        "compliance with the Personal Data Protection Principles.",
        "(2) A data user who contravenes subsection (1) commits an offence.",
    ]
)

GATE_PREAMBLE_CHUNK = "\n".join(
    [
        "PART II - PERSONAL DATA PROTECTION",
        "Division 1 - Personal Data Protection Principles",
        "Section 5. Personal Data Protection Principles",
        "(1) The processing of personal data by a data user shall be in",
        "compliance with the Personal Data Protection Principles.",
    ]
)

GATE_CONTINUATION_CHUNK = "(2) A data user who contravenes subsection (1) commits an offence."


class TestPointerGateReadsTheChunk:
    LOADER = staticmethod(lambda doc_id: GATE_PART_BOUNDARY_TEXT)

    def test_a_quote_in_the_chunks_preamble_is_a_note(self):
        rec = pointer_rec(
            "Part II s. 5", "Division 1 - Personal Data Protection Principles"
        )
        failures, notes = pointer_gate(
            [rec], self.LOADER, {rec.chunk_id: GATE_PREAMBLE_CHUNK}
        )
        assert failures == []
        assert len(notes) == 1 and "above its own chunk's heading" in notes[0]

    def test_a_continuation_chunk_is_still_judged(self):
        """No heading inside the chunk, so the heading above it IS the row's
        section and a wrong carried label must fail, not be waved through."""
        rec = pointer_rec("Part I s. 4", GATE_CONTINUATION_CHUNK)
        failures, notes = pointer_gate(
            [rec], self.LOADER, {rec.chunk_id: GATE_CONTINUATION_CHUNK}
        )
        assert notes == []
        assert len(failures) == 1
        assert "does not name the nearest heading" in failures[0]

    def test_a_correct_continuation_label_passes(self):
        rec = pointer_rec("Part II s. 5", GATE_CONTINUATION_CHUNK)
        failures, notes = pointer_gate(
            [rec], self.LOADER, {rec.chunk_id: GATE_CONTINUATION_CHUNK}
        )
        assert failures == [] and notes == []


class TestReviewDrops:
    def _export(self, tmp_path, config_dir=None):
        records, texts, cosines = [], {}, {}
        for slug in SLUGS:
            records.extend(load_golden_m8(slug))
            texts.update(chunk_texts(slug))
            cosines.update(gate_cosines(slug))
        kwargs = {"config_dir": config_dir} if config_dir else {}
        return records, export_all(
            tmp_path / "out",
            records,
            chunk_text_lookup=texts,
            gate_cosine_lookup=cosines,
            coverage_stats=COVERAGE,
            liveness_fn=lambda url: True,
            **kwargs,
        )

    def _config_with_drop(self, tmp_path, mapping_id: str) -> Path:
        cfg = tmp_path / "config"
        shutil.copytree(ROOT / "config", cfg)
        (cfg / "review_drops.json").write_text(
            json.dumps(
                {
                    "drops": [
                        {
                            "mapping_id": mapping_id,
                            "reason": "rationale asserts scope the quote does not state",
                            "reviewer": "test",
                            "date": "2026-07-07",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        return cfg

    def _droppable(self, records) -> MappingRecord:
        # a NON-controlling record whose (economy, indicator) has siblings, so
        # the drop changes no absence row and no controlling selection
        by_group: dict[tuple, int] = {}
        for r in records:
            by_group[(r.economy, r.indicator_id)] = by_group.get((r.economy, r.indicator_id), 0) + 1
        for r in records:
            if not r.controlling_evidence and by_group[(r.economy, r.indicator_id)] > 1:
                return r
        raise AssertionError("no droppable record in goldens")

    def test_dropped_record_leaves_the_export_and_is_disclosed(self, tmp_path):
        records, baseline = self._export(tmp_path / "a")
        victim = self._droppable(records)
        cfg = self._config_with_drop(tmp_path, victim.mapping_id)
        _, result = self._export(tmp_path / "b", config_dir=cfg)
        assert result.battery_failures == []
        dropped_keys = {r["Verbatim Snippet"] for r in baseline.rows} - {
            r["Verbatim Snippet"] for r in result.rows
        }
        assert victim.verbatim_quote in "".join(dropped_keys) or len(result.rows) == len(baseline.rows) - 1
        supp = json.loads(result.supplementary_path.read_text(encoding="utf-8"))
        assert supp["review_drops"]["n_dropped"] == 1
        assert supp["review_drops"]["dropped"][0]["mapping_id"] == victim.mapping_id

    def test_unmatched_drop_entries_are_ignored(self, tmp_path):
        cfg = self._config_with_drop(tmp_path, "doc_nowhere:c9999::7.5")
        _, result = self._export(tmp_path / "c", config_dir=cfg)
        assert result.battery_failures == []
        supp = json.loads(result.supplementary_path.read_text(encoding="utf-8"))
        assert "review_drops" not in supp


# ---------------------------------------------------------------------------
# Manually added Documents
# ---------------------------------------------------------------------------


class TestManuallyAddedDocuments:
    """A Document a reviewer added by hand is exempt from the Portal whitelist,
    because the reviewer vouched for the URL and the Portal may have no
    verified host at all. That weakens the official-Portal guarantee, so every
    such row carries BOTH disclosures in Notes: the off-whitelist note and the
    one naming the Document as manually added."""

    portals = load_portals()

    def _manual_row(self, *, allow_any_host=True, notes=None):
        from regcompass.contracts import CorpusDoc
        from regcompass.export import ALLOW_ANY_HOST_NOTE, MANUAL_ADD_NOTE

        rec = mk_rec()
        doc = CorpusDoc(
            economy="MY", law_name="Added By Hand Act 2026",
            source_url="https://example.org/by-hand.pdf", url_is_direct=True,
        )
        row = build_row(
            rec, doc, load_crosswalk(), load_known_matrix(), gate_lookup_for(rec),
            {rec.chunk_id: 1},
            extra_notes=[ALLOW_ANY_HOST_NOTE, MANUAL_ADD_NOTE] if notes is None else notes,
            allow_any_host=allow_any_host,
        )
        return row, {rec.chunk_id: "PREFIX " + rec.verbatim_quote + " SUFFIX"}

    def test_a_manual_row_passes_the_battery_with_both_disclosures(self):
        from regcompass.export import ALLOW_ANY_HOST_NOTE, MANUAL_ADD_NOTE

        row, texts = self._manual_row()
        assert run_gate_battery([row], texts, self.portals, lambda url: True) == []
        assert ALLOW_ANY_HOST_NOTE in row["Notes"]
        assert MANUAL_ADD_NOTE in row["Notes"]

    def test_an_offwhitelist_row_without_the_exemption_still_fails(self):
        row, texts = self._manual_row(allow_any_host=False, notes=[])
        failures = run_gate_battery([row], texts, self.portals, lambda url: True)
        assert any("portal whitelist" in f for f in failures)

    def test_the_offcorpus_synthesizer_reads_the_source_kind(self):
        from regcompass.pipeline import synthesize_offcorpus_docs

        docs = synthesize_offcorpus_docs(
            {
                "doc_my_added_by_hand": {
                    "economy": "MY", "title": "Added By Hand Act 2026",
                    "source_url": "https://example.org/by-hand.pdf",
                    "source_kind": "manual",
                },
                "doc_my_crawled": {
                    "economy": "MY", "title": "Crawl Catch Act 2026",
                    "source_url": "https://lom.agc.gov.my/some.pdf",
                    "source_kind": "discovery",
                },
                "doc_my_pre_migration": {
                    "economy": "MY", "title": "Old Row Act 2026",
                    "source_url": "https://lom.agc.gov.my/old.pdf",
                },
            }
        )
        assert docs["doc_my_added_by_hand"].allow_any_host is True
        assert docs["doc_my_added_by_hand"].manual_added is True
        assert docs["doc_my_crawled"].allow_any_host is False
        assert docs["doc_my_crawled"].manual_added is False
        # A database written before the column existed reads None, which means
        # Discovery: it is the only way a row could have been written then.
        assert docs["doc_my_pre_migration"].manual_added is False
