"""Glosses drafted by the selected Engine.

A Run that produces a Mapping whose Verbatim Quote is not English asks the
SELECTED Engine for an English Gloss in the same Run, stores it beside the
Mapping labelled and unreviewed, and counts its tokens in the Run Record. A
reviewer edits the text and signs it with their name, which makes it a Reviewed
Gloss; only then does the Evidence Export ship it without the label.

Everything here runs offline on the fake Engine: no network, no key, no paid
call. The Run tests read the same two-page Lao scan the non-English lane uses
and skip cleanly without the tesseract binary, exactly as
tests/test_non_english_lane.py does.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from regcompass.engines import resolve_engine

from regcompass.contracts import (
    GLOSS_LABEL,
    GLOSS_UNAVAILABLE,
    Chunk,
    GlossRecord,
    MappingRecord,
    PipelineConfig,
)
from regcompass.storage import Storage, utc_now_iso

ROOT = Path(__file__).resolve().parents[1]

DOC_ID = "doc_seeded_act"
QUOTES = [
    "The Authority may license any person to provide a telecommunication service.",
    "A licensee shall not discriminate between customers of the same class.",
]
FULL_TEXT = "\n\n".join(QUOTES)


def seed_run(storage: Storage, run_id: str = "run_a") -> list[str]:
    """One completed Run over one seeded Document, with two passed Mappings."""
    storage.upsert_document(
        DOC_ID, "SG", "sha_seeded", full_text=FULL_TEXT, title="Seeded Act 1999",
        n_pages=1, language="en",
    )
    storage.upsert_chunks(
        Chunk(
            chunk_id=f"{DOC_ID}:c{i}",
            document_id=DOC_ID,
            char_start=FULL_TEXT.index(quote),
            char_end=FULL_TEXT.index(quote) + len(quote),
            text=quote,
            section_label=f"s. {i + 1}",
            page_start=1,
            page_end=1,
        )
        for i, quote in enumerate(QUOTES)
    )
    storage.run_start(
        run_id=run_id, kind="run", economy="SG", pillars=[7], indicators=None,
        engine="fake", started_at=utc_now_iso(),
    )
    records = [
        MappingRecord(
            mapping_id=f"{DOC_ID}:c{i}::7.{i + 1}",
            document_id=DOC_ID,
            chunk_id=f"{DOC_ID}:c{i}",
            economy="SG",
            indicator_id=f"7.{i + 1}",
            indicator_name=f"Indicator 7.{i + 1}",
            section=f"s. {i + 1}",
            verbatim_quote=quote,
            page_number=1,
            verification_status="passed",
        )
        for i, quote in enumerate(QUOTES)
    ]
    storage.upsert_mappings(records, run_id=run_id)
    storage.run_finish(run_id, status="completed", ended_at=utc_now_iso())
    return [r.mapping_id for r in records]


@pytest.fixture()
def seeded(tmp_path):
    """(db path, run id, mapping ids) for one seeded Run."""
    db = tmp_path / "regcompass.db"
    storage = Storage(db)
    storage.apply_schema()
    ids = seed_run(storage)
    storage.close()
    return db, "run_a", ids


# ---------------------------------------------------------------------------
# the store: keyed (run_id, mapping_id), which is what closes the keying defect
# ---------------------------------------------------------------------------


class TestTheGlossStore:
    def test_a_drafted_gloss_reads_back_unreviewed_and_labelled(self, seeded):
        db, run_id, ids = seeded
        storage = Storage(db)
        stored = storage.gloss_set(
            run_id=run_id, mapping_id=ids[0], english="A licence may be granted.",
            engine="fake", source_language="lo",
        )
        assert isinstance(stored, GlossRecord)
        assert stored.reviewed is False
        assert stored.reviewed_by is None
        assert stored.label == GLOSS_LABEL
        read_back = storage.gloss_get(run_id, ids[0])
        assert read_back == stored
        storage.close()

    def test_two_runs_keep_their_own_glosses_for_the_same_mapping_id(self, seeded):
        """The Run-scoping keying defect: a bare mapping_id key would let Run A's
        Gloss attach to Run B's quote."""
        db, run_a, ids = seeded
        storage = Storage(db)
        seed_run_b_ids = _second_run(storage, "run_b")
        storage.gloss_set(run_id=run_a, mapping_id=ids[0], english="From Run A", engine="fake")
        storage.gloss_set(
            run_id="run_b", mapping_id=seed_run_b_ids[0], english="From Run B", engine="fake"
        )
        assert storage.gloss_get(run_a, ids[0]).english == "From Run A"
        assert storage.gloss_get("run_b", ids[0]).english == "From Run B"
        assert sorted(storage.glosses_for_run(run_a)) == [ids[0]]
        storage.close()

    def test_a_named_review_edits_the_text_and_marks_it_reviewed(self, seeded):
        db, run_id, ids = seeded
        storage = Storage(db)
        storage.gloss_set(run_id=run_id, mapping_id=ids[0], english="draft text", engine="fake")
        reviewed = storage.gloss_review(
            run_id=run_id, mapping_id=ids[0], english="checked text", reviewed_by="R. Nurtanio"
        )
        assert reviewed.reviewed is True
        assert reviewed.reviewed_by == "R. Nurtanio"
        assert reviewed.english == "checked text"
        assert reviewed.reviewed_at is not None
        assert reviewed.engine == "fake", "the drafting Engine stays on the record"
        storage.close()

    @pytest.mark.parametrize("name", ["", "   ", None])
    def test_an_unnamed_review_is_refused(self, seeded, name):
        """A Reviewed Gloss is one a NAMED person approved: without a name the
        export would ship an unlabelled AI translation on nobody's authority."""
        db, run_id, ids = seeded
        storage = Storage(db)
        storage.gloss_set(run_id=run_id, mapping_id=ids[0], english="draft", engine="fake")
        with pytest.raises(ValueError):
            storage.gloss_review(
                run_id=run_id, mapping_id=ids[0], english="edited", reviewed_by=name
            )
        assert storage.gloss_get(run_id, ids[0]).reviewed is False
        storage.close()

    def test_redrafting_replaces_the_row_in_place(self, seeded):
        db, run_id, ids = seeded
        storage = Storage(db)
        storage.gloss_set(run_id=run_id, mapping_id=ids[0], english="first", engine="fake")
        storage.gloss_set(run_id=run_id, mapping_id=ids[0], english="second", engine="fake")
        assert len(storage.glosses_for_run(run_id)) == 1
        assert storage.gloss_get(run_id, ids[0]).english == "second"
        storage.close()

    def test_a_failed_draft_is_recorded_with_its_uncertainty_flag(self, seeded):
        db, run_id, ids = seeded
        storage = Storage(db)
        stored = storage.gloss_set(
            run_id=run_id, mapping_id=ids[0], english=None, engine="fake",
            uncertainty_flag="no-clear-translation-equivalent",
        )
        assert stored.english is None
        assert stored.uncertainty_flag == "no-clear-translation-equivalent"
        storage.close()


def _second_run(storage: Storage, run_id: str) -> list[str]:
    """A second Run over the SAME Document, producing the same mapping ids."""
    storage.run_start(
        run_id=run_id, kind="run", economy="SG", pillars=[7], indicators=None,
        engine="fake", started_at=utc_now_iso(),
    )
    records = [
        MappingRecord(
            mapping_id=f"{DOC_ID}:c{i}::7.{i + 1}",
            document_id=DOC_ID,
            chunk_id=f"{DOC_ID}:c{i}",
            economy="SG",
            indicator_id=f"7.{i + 1}",
            indicator_name=f"Indicator 7.{i + 1}",
            section=f"s. {i + 1}",
            verbatim_quote=quote,
            page_number=1,
            verification_status="passed",
        )
        for i, quote in enumerate(QUOTES)
    ]
    storage.upsert_mappings(records, run_id=run_id)
    storage.run_finish(run_id, status="completed", ended_at=utc_now_iso())
    return [r.mapping_id for r in records]


# ---------------------------------------------------------------------------
# the drafting lane: the SELECTED Engine's transport, so the meter sees it
# ---------------------------------------------------------------------------

LAO_QUOTE = "ມາດຕາ 5 ຫຼັກການກ່ຽວກັບວຽກງານທຸລະກໍາທາງເອເລັກໂຕຣນິກ ໃຫ້ປະຕິບັດຕາມຫຼັກການ ດັ່ງນີ້."


class TestTheSelectedEngineDraftsTheGloss:
    ITEMS = [("doc:c0::6.1", LAO_QUOTE), ("doc:c1::7.2", LAO_QUOTE + " ສອງ")]

    def test_one_call_covers_every_mapping_of_the_document(self):
        from regcompass.engines import read_meter, start_meter
        from regcompass.translate import draft_document_glosses

        start_meter()
        glosses = draft_document_glosses(
            self.ITEMS, resolve_engine("fake"), PipelineConfig(), source_language="lo"
        )
        meter = read_meter()
        assert [g.key for g in glosses] == [k for k, _ in self.ITEMS]
        assert all(g.english for g in glosses), "the fake Engine drafts offline"
        assert all(g.source_language == "lo" for g in glosses)
        assert meter.calls == 1, "one call per Document, not one per Mapping"
        assert meter.prompt_tokens > 0 and meter.completion_tokens > 0

    def test_the_answer_is_keyed_by_mapping_id_not_by_position(self):
        from regcompass.translate import draft_document_glosses

        glosses = draft_document_glosses(
            self.ITEMS, resolve_engine("fake"), PipelineConfig()
        )
        by_key = {g.key: g.english for g in glosses}
        assert by_key["doc:c0::6.1"] != by_key["doc:c1::7.2"]
        for key, english in by_key.items():
            assert key in english, "the scripted answer names the Mapping it glosses"

    def test_a_mapping_the_engine_skipped_takes_the_fallback_lane(self):
        from regcompass.translate import draft_document_glosses

        def half_answer(prompt: str, strict: bool) -> str:
            return json.dumps(
                {"glosses": [{"mapping_id": "doc:c0::6.1", "english": "The first rule applies."}]}
            )

        glosses = draft_document_glosses(
            self.ITEMS, resolve_engine("fake"), PipelineConfig(), completion_fn=half_answer
        )
        assert glosses[0].english == "The first rule applies."
        assert glosses[1].english is None
        assert glosses[1].uncertainty_flag == "no-clear-translation-equivalent"

    def test_a_broken_answer_never_raises(self):
        from regcompass.translate import draft_document_glosses

        glosses = draft_document_glosses(
            self.ITEMS, resolve_engine("fake"), PipelineConfig(),
            completion_fn=lambda prompt, strict: "not json at all",
        )
        assert [g.english for g in glosses] == [None, None]
        assert all(g.uncertainty_flag for g in glosses)

    def test_an_untranslated_echo_is_a_failure_not_a_gloss(self):
        """The guard that already protects the offline lane: an answer that is
        still in the source script is not a Gloss."""
        from regcompass.translate import draft_document_glosses

        echo = json.dumps({"glosses": [{"mapping_id": "doc:c0::6.1", "english": LAO_QUOTE}]})
        glosses = draft_document_glosses(
            self.ITEMS[:1], resolve_engine("fake"), PipelineConfig(),
            completion_fn=lambda prompt, strict: echo,
        )
        assert glosses[0].english is None


# ---------------------------------------------------------------------------
# the Run: a non-English Document is glossed, an English one is not
# ---------------------------------------------------------------------------

TELECOM = ROOT / "tests/fixtures/sample_legislation/born_digital/Telecommunications Act 1999.pdf"
# The two pages sliced from the canonical 29-page Lao scan, the same derived
# fixture the non-English lane runs on (scripts/make_derived_fixtures.py).
LAO_SLICE = ROOT / "tests/fixtures/derived/lao_electronic_transactions_p05_p06.pdf"

needs_tesseract = pytest.mark.skipif(
    shutil.which("tesseract") is None, reason="tesseract binary not on PATH"
)


def run_one_document(tmp_path, *, pdf, language, economy):
    """One Document through a whole Run Record, on the fake Engine, with every
    model call recorded. Returns (storage, run_id, calls)."""
    from regcompass.engines import fake_completion, fake_embed
    from regcompass.pipeline import RunReport, record_run, run_document

    calls: list[tuple[str, str]] = []

    def spy(prompt: str, strict: bool) -> str:
        answer = fake_completion(prompt, strict)
        calls.append((prompt, answer))
        return answer

    engine = resolve_engine("fake")
    storage = Storage(tmp_path / "run.db")
    storage.apply_schema()
    report = RunReport(economy=economy, engine=engine.name)
    with record_run(
        storage, report, economy=economy, pillars=(6, 7), engine=engine
    ) as run_id:
        run_document(
            storage, "doc_x_1", pdf, economy, (6, 7), engine, run_id,
            completion_fn=spy, embed_fn=fake_embed, language=language, report=report,
        )
    return storage, run_id, calls


def lao_run(tmp_path):
    """The Lao statute slice, OCR'd in Lao and mapped by the fake Engine."""
    return run_one_document(tmp_path, pdf=LAO_SLICE, language="Lao", economy="LA")


def gloss_calls(calls) -> list[tuple[str, str]]:
    return [(p, a) for p, a in calls if "<<<GLOSS " in p]


@needs_tesseract
class TestARunGlossesItsNonEnglishDocument:
    def test_every_mapping_carries_a_labelled_unreviewed_gloss(self, tmp_path):
        storage, run_id, calls = lao_run(tmp_path)
        passed = [
            r["mapping_id"] for r in storage.conn.execute(
                "SELECT mapping_id FROM mappings WHERE run_id = ?"
                " AND verification_status = 'passed'", (run_id,)
            )
        ]
        assert passed, "the Lao lane produced no verified Mapping to gloss"
        glosses = storage.glosses_for_run(run_id)
        assert sorted(glosses) == sorted(passed)
        for mapping_id, gloss in glosses.items():
            assert gloss.reviewed is False and gloss.reviewed_by is None
            assert gloss.label == GLOSS_LABEL
            assert gloss.english and mapping_id in gloss.english
            assert gloss.engine == "fake"
            assert gloss.source_language == "lo"
        assert len(gloss_calls(calls)) == 1, "one gloss call per Document"
        storage.close()

    def test_the_run_record_counts_the_gloss_tokens(self, tmp_path):
        """Recomputed with the fake Engine's own scripted rule (four characters
        to a token) over EVERY call the Run made, gloss call included."""
        storage, run_id, calls = lao_run(tmp_path)
        expected_prompt = sum(len(p) // 4 for p, _ in calls)
        expected_completion = sum(len(a) // 4 for _, a in calls)
        row = storage.conn.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        assert row["prompt_tokens"] == expected_prompt
        assert row["completion_tokens"] == expected_completion
        gloss_prompt_tokens = sum(len(p) // 4 for p, _ in gloss_calls(calls))
        assert gloss_prompt_tokens > 0
        assert row["prompt_tokens"] > expected_prompt - gloss_prompt_tokens
        storage.close()

    def test_the_run_record_details_carry_the_gloss_time(self, tmp_path):
        storage, run_id, _ = lao_run(tmp_path)
        details = json.loads(
            storage.conn.execute(
                "SELECT details FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()["details"]
        )
        assert details["n_glossed"] == len(storage.glosses_for_run(run_id))
        assert details["gloss_ms"] >= 0.0
        storage.close()


class TestAnEnglishDocumentIsNeverGlossed:
    def test_no_gloss_call_and_no_gloss_row(self, tmp_path):
        storage, run_id, calls = run_one_document(
            tmp_path, pdf=TELECOM, language="English", economy="AU"
        )
        assert storage.conn.execute(
            "SELECT COUNT(*) AS n FROM mappings WHERE run_id = ?"
            " AND verification_status = 'passed'", (run_id,)
        ).fetchone()["n"] > 0
        assert gloss_calls(calls) == [], "the gloss transport was called on an English Document"
        assert storage.glosses_for_run(run_id) == {}
        details = json.loads(
            storage.conn.execute(
                "SELECT details FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()["details"]
        )
        assert details["n_glossed"] == 0
        storage.close()


HINDI_QUOTE = "केंद्रीय सरकार द्वारा जारी अधिसूचना के अनुसार यह प्रावधान लागू होता है"


def gloss_one_document(tmp_path, quotes: list[str], *, language: str):
    """gloss_document over one seeded Document whose passed Mappings carry these
    quotes, on the fake Engine with every model call recorded. Returns
    (storage, mapping ids, calls, glossed count)."""
    from regcompass.engines import fake_completion
    from regcompass.pipeline import RunReport, gloss_document

    calls: list[tuple[str, str]] = []

    def spy(prompt: str, strict: bool) -> str:
        answer = fake_completion(prompt, strict)
        calls.append((prompt, answer))
        return answer

    storage = Storage(tmp_path / "gloss.db")
    storage.apply_schema()
    full_text = "\n\n".join(quotes)
    storage.upsert_document(
        "doc_in_x", "IN", "sha_in_x", full_text=full_text, title="Seeded Act 2023",
        n_pages=1, language=language,
    )
    storage.upsert_chunks(
        Chunk(
            chunk_id=f"doc_in_x:c{i}",
            document_id="doc_in_x",
            char_start=full_text.index(quote),
            char_end=full_text.index(quote) + len(quote),
            text=quote,
            section_label=f"s. {i + 1}",
            page_start=1,
            page_end=1,
        )
        for i, quote in enumerate(quotes)
    )
    storage.run_start(
        run_id="run_a", kind="run", economy="IN", pillars=[6], indicators=None,
        engine="fake", started_at=utc_now_iso(),
    )
    records = [
        MappingRecord(
            mapping_id=f"doc_in_x:c{i}::6.1",
            document_id="doc_in_x",
            chunk_id=f"doc_in_x:c{i}",
            economy="IN",
            indicator_id="6.1",
            indicator_name="Indicator 6.1",
            section=f"s. {i + 1}",
            verbatim_quote=quote,
            page_number=1,
            verification_status="passed",
        )
        for i, quote in enumerate(quotes)
    ]
    storage.upsert_mappings(records, run_id="run_a")
    engine = resolve_engine("fake")
    n = gloss_document(
        storage, records, run_id="run_a", doc_id="doc_in_x", engine=engine,
        language=language, config=PipelineConfig(), completion_fn=spy,
        report=RunReport(economy="IN", engine=engine.name),
    )
    return storage, [r.mapping_id for r in records], calls, n


class TestAnEnglishRecordedDocumentStillGlossesItsNonEnglishQuotes:
    """A Document recorded as English can still carry non-English text (an
    India Act published in Hindi under the portal's English default). The
    recorded Language does not excuse a quote the export would require a
    translation for."""

    def test_a_devanagari_quote_is_glossed(self, tmp_path):
        storage, (hindi_id,), calls, n = gloss_one_document(
            tmp_path, [HINDI_QUOTE], language="English"
        )
        assert n == 1
        assert len(gloss_calls(calls)) == 1
        stored = storage.glosses_for_run("run_a")
        assert set(stored) == {hindi_id}
        assert stored[hindi_id].english
        storage.close()

    def test_a_mixed_document_glosses_only_the_non_english_quote(self, tmp_path):
        storage, (english_id, hindi_id), calls, n = gloss_one_document(
            tmp_path, [QUOTES[0], HINDI_QUOTE], language="English"
        )
        assert n == 1
        (prompt, _), = gloss_calls(calls)
        assert f"<<<GLOSS {hindi_id}\n" in prompt
        assert english_id not in prompt
        assert set(storage.glosses_for_run("run_a")) == {hindi_id}
        storage.close()

    def test_an_all_english_quote_set_makes_no_gloss_call(self, tmp_path):
        storage, _, calls, n = gloss_one_document(tmp_path, QUOTES, language="English")
        assert n == 0
        assert gloss_calls(calls) == []
        assert storage.glosses_for_run("run_a") == {}
        storage.close()


# ---------------------------------------------------------------------------
# the Evidence Export: labelled unless a named person approved the text
# ---------------------------------------------------------------------------

# The one Malay provision in the Round 1 corpus: three rows share its quote, and
# config/verbatim_english.json carries their reviewed translation.
MALAY_MAPPING_ID = "doc_my_20141230_A1472_BI_Act_A1472:c0008::7.2"


def gloss_record(mapping_id: str, english: str, *, reviewed_by: str | None = None):
    from datetime import datetime, timezone

    return GlossRecord(
        run_id="run_a",
        mapping_id=mapping_id,
        english=english,
        reviewed=bool(reviewed_by),
        reviewed_by=reviewed_by,
        reviewed_at=datetime.now(timezone.utc) if reviewed_by else None,
        source_language="ms",
        engine="fake",
        drafted_at=datetime.now(timezone.utc),
    )


class TestTheExportReadsTheDatabaseFirst:
    def test_a_stored_gloss_beats_the_reviewed_config_file(self):
        from regcompass.config import CONFIG_DIR
        from regcompass.export import verbatim_english_for

        stored = gloss_record(MALAY_MAPPING_ID, "the stored rendering")
        cell, reviewed_by = verbatim_english_for(MALAY_MAPPING_ID, {MALAY_MAPPING_ID: stored}, CONFIG_DIR)
        assert cell == f"{GLOSS_LABEL} the stored rendering"
        assert reviewed_by is None

    def test_the_config_file_answers_where_the_database_is_silent(self):
        from regcompass.config import CONFIG_DIR
        from regcompass.export import verbatim_english_for

        cell, reviewed_by = verbatim_english_for(MALAY_MAPPING_ID, {}, CONFIG_DIR)
        assert cell.startswith(GLOSS_LABEL)
        assert "intercept and retain" in cell
        assert reviewed_by is None, "the config lane keeps its label, as it always did"

    def test_a_reviewed_gloss_drops_the_label(self):
        from regcompass.config import CONFIG_DIR
        from regcompass.export import verbatim_english_for

        stored = gloss_record(MALAY_MAPPING_ID, "the approved rendering", reviewed_by="R. Nurtanio")
        cell, reviewed_by = verbatim_english_for(MALAY_MAPPING_ID, {MALAY_MAPPING_ID: stored}, CONFIG_DIR)
        assert cell == "the approved rendering"
        assert reviewed_by == "R. Nurtanio"

    def _failed(self):
        from datetime import datetime, timezone

        return GlossRecord(
            run_id="run_a", mapping_id="doc:c0::7.1", english=None, engine="fake",
            uncertainty_flag="no-clear-translation-equivalent",
            drafted_at=datetime.now(timezone.utc),
        )

    def test_a_failed_gloss_says_so_instead_of_leaving_the_column_empty(self):
        """An empty cell is unshippable: the battery reads it as a missing
        translation and the whole Export goes red, so a Run whose gloss lane
        failed on one quote produced no Export at all. The row states that no
        rendering is available instead, which is the honest answer and the one
        a reader can act on."""
        from regcompass.config import CONFIG_DIR
        from regcompass.export import verbatim_english_for

        cell, reviewed_by = verbatim_english_for(
            "doc:c0::7.1", {"doc:c0::7.1": self._failed()}, CONFIG_DIR
        )
        assert cell == GLOSS_UNAVAILABLE
        assert cell.startswith(GLOSS_LABEL), "the cell still carries its label"
        assert "not available" in cell
        assert reviewed_by is None


class TestTheBatteryJudgesTheLabelByAuthority:
    MALAY = (
        "Seseorang tidak boleh memindahkan apa-apa data peribadi ke sesuatu "
        "tempat di luar negara melainkan jika ditetapkan oleh pihak berkuasa."
    )

    def _row(self):
        from test_export import good_row_and_lookups

        row, _ = good_row_and_lookups()
        row["Verbatim Snippet"] = self.MALAY
        texts = {row["_chunk_id"]: "PREFIX " + self.MALAY + " SUFFIX"}
        return row, texts

    def _battery(self, row, texts):
        from regcompass.config import load_portals
        from regcompass.export import run_gate_battery

        return run_gate_battery([row], texts, load_portals(), lambda url: True)

    def test_an_unreviewed_gloss_must_carry_the_label(self):
        row, texts = self._row()
        row["Verbatim English"] = "No person shall transfer personal data abroad."
        row["_gloss_reviewed_by"] = None
        assert any("label" in f for f in self._battery(row, texts))

    def test_a_reviewed_gloss_may_ship_without_it(self):
        row, texts = self._row()
        row["Verbatim English"] = "No person shall transfer personal data abroad."
        row["_gloss_reviewed_by"] = "R. Nurtanio"
        assert self._battery(row, texts) == []

    def test_a_non_english_snippet_still_needs_some_english(self):
        row, texts = self._row()
        row["Verbatim English"] = ""
        row["_gloss_reviewed_by"] = "R. Nurtanio"
        assert any("missing/empty" in f for f in self._battery(row, texts))

    def test_the_no_rendering_statement_passes(self):
        """The statement the writer produces when drafting failed has to clear
        the same gate a rendering clears, or the writer has only moved the red
        light somewhere else."""
        row, texts = self._row()
        row["Verbatim English"] = GLOSS_UNAVAILABLE
        row["_gloss_reviewed_by"] = None
        assert self._battery(row, texts) == []

    def test_an_unlabelled_value_with_no_named_reviewer_still_fails(self):
        """The rule the spec replaced said only human-reviewed renderings may
        ship. The mechanical guarantee that replaces it: unlabelled ONLY on a
        named person's authority."""
        row, texts = self._row()
        row["Verbatim English"] = "An unlabelled translation."
        row["_gloss_reviewed_by"] = "   "
        assert any("label" in f for f in self._battery(row, texts))


class TestTheExportedFileCarriesTheGloss:
    """The whole export, over the committed golden dataset, offline."""

    def _inputs(self):
        from test_export import COVERAGE, SLUGS, chunk_texts, gate_cosines, load_golden_m8

        records, texts, cosines = [], {}, {}
        for slug in SLUGS:
            records.extend(load_golden_m8(slug))
            texts.update(chunk_texts(slug))
            cosines.update(gate_cosines(slug))
        return records, texts, cosines, COVERAGE

    def _export(self, tmp_path, glosses=None):
        from regcompass.export import export_all

        records, texts, cosines, coverage = self._inputs()
        result = export_all(
            tmp_path, records, chunk_text_lookup=texts, gate_cosine_lookup=cosines,
            coverage_stats=coverage, liveness_fn=lambda url: True, glosses=glosses,
            write_xlsx=False,
        )
        return result

    def _first_mapping_id(self):
        records, _, _, _ = self._inputs()
        return next(r.mapping_id for r in records if r.verification_status == "passed")

    def _row(self, result, mapping_id):
        return next(r for r in result.rows if r.get("_mapping_id") == mapping_id)

    def test_a_reviewed_gloss_ships_unlabelled_and_the_snippet_is_untouched(self, tmp_path):
        mapping_id = self._first_mapping_id()
        stored = gloss_record(
            mapping_id, "The approved English rendering.", reviewed_by="R. Nurtanio"
        )
        plain = self._export(tmp_path / "plain")
        result = self._export(tmp_path / "reviewed", glosses={mapping_id: stored})
        row = self._row(result, mapping_id)
        assert row["Verbatim English"] == "The approved English rendering."
        assert row["Verbatim Snippet"] == self._row(plain, mapping_id)["Verbatim Snippet"]
        assert result.battery_failures == []

    def test_an_unreviewed_gloss_ships_labelled(self, tmp_path):
        mapping_id = self._first_mapping_id()
        stored = gloss_record(mapping_id, "A drafted English rendering.")
        result = self._export(tmp_path, glosses={mapping_id: stored})
        row = self._row(result, mapping_id)
        assert row["Verbatim English"] == f"{GLOSS_LABEL} A drafted English rendering."
        assert result.battery_failures == []

    def test_the_csv_carries_the_same_two_values(self, tmp_path):
        import csv as csv_mod

        mapping_id = self._first_mapping_id()
        result = self._export(
            tmp_path, glosses={mapping_id: gloss_record(mapping_id, "A drafted rendering.")}
        )
        with result.csv_path.open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv_mod.DictReader(f))
        assert any(
            r["Verbatim English"] == f"{GLOSS_LABEL} A drafted rendering." for r in rows
        )


# ---------------------------------------------------------------------------
# the API: a reviewer edits the text, signs it, and it survives a restart
# ---------------------------------------------------------------------------


def _app(db, tmp_path, **kwargs):
    from regcompass.server import create_app

    return create_app(
        db_path=db, out_dir=tmp_path / "out", data_dir=tmp_path / "data",
        ui_dir=None, **kwargs,
    )


def _client(db, tmp_path):
    from fastapi.testclient import TestClient

    return TestClient(_app(db, tmp_path))


@pytest.fixture()
def drafted(seeded):
    """The seeded Run with an unreviewed Gloss on its first Mapping."""
    db, run_id, ids = seeded
    storage = Storage(db)
    storage.gloss_set(
        run_id=run_id, mapping_id=ids[0], english="a drafted rendering",
        engine="fake", source_language="ms",
    )
    storage.close()
    return db, run_id, ids


class TestTheReviewApi:
    def test_a_named_review_persists_across_a_restart(self, drafted, tmp_path):
        db, run_id, ids = drafted
        client = _client(db, tmp_path)
        posted = client.post(
            "/api/glosses/review",
            json={
                "run_id": run_id, "mapping_id": ids[0],
                "english": "the rendering a person checked",
                "reviewed_by": "R. Nurtanio",
            },
        )
        assert posted.status_code == 200, posted.text
        body = posted.json()
        assert body["reviewed"] is True
        assert body["reviewed_by"] == "R. Nurtanio"
        assert body["english"] == "the rendering a person checked"

        # The restart: a NEW app over the SAME database file.
        del client
        restarted = _client(db, tmp_path)
        listed = restarted.get("/api/glosses", params={"run_id": run_id}).json()
        assert listed["run_id"] == run_id
        assert [(g["mapping_id"], g["english"], g["reviewed_by"]) for g in listed["glosses"]] == [
            (ids[0], "the rendering a person checked", "R. Nurtanio")
        ]

    @pytest.mark.parametrize("name", ["", "   "])
    def test_an_unnamed_review_is_a_400(self, drafted, tmp_path, name):
        db, run_id, ids = drafted
        client = _client(db, tmp_path)
        posted = client.post(
            "/api/glosses/review",
            json={
                "run_id": run_id, "mapping_id": ids[0], "english": "edited",
                "reviewed_by": name,
            },
        )
        assert posted.status_code == 400
        assert "name" in posted.json()["detail"].lower()
        storage = Storage(db)
        assert storage.gloss_get(run_id, ids[0]).reviewed is False
        storage.close()

    def test_an_unknown_mapping_is_a_404(self, drafted, tmp_path):
        db, run_id, _ = drafted
        client = _client(db, tmp_path)
        posted = client.post(
            "/api/glosses/review",
            json={
                "run_id": run_id, "mapping_id": "no_such_mapping",
                "english": "edited", "reviewed_by": "R. Nurtanio",
            },
        )
        assert posted.status_code == 404

    def test_the_record_pane_sees_the_original_quote_and_the_gloss(self, drafted, tmp_path):
        db, run_id, ids = drafted
        client = _client(db, tmp_path)
        detail = client.get(f"/api/records/{ids[0]}", params={"run_id": run_id}).json()
        assert detail["record"]["verbatim_quote"] == QUOTES[0]
        assert detail["gloss"]["english"] == "a drafted rendering"
        assert detail["gloss"]["reviewed"] is False
        assert detail["gloss"]["label"] == GLOSS_LABEL

    def test_a_record_with_no_gloss_reports_none(self, drafted, tmp_path):
        db, run_id, ids = drafted
        client = _client(db, tmp_path)
        detail = client.get(f"/api/records/{ids[1]}", params={"run_id": run_id}).json()
        assert detail["gloss"] is None


# ---------------------------------------------------------------------------
# a working database made before the glosses table existed
# ---------------------------------------------------------------------------


@pytest.fixture()
def pre_gloss_db(seeded):
    """The seeded Run in a database with NO glosses table: what an operator
    reusing a working database from an earlier build actually points at."""
    db, run_id, ids = seeded
    storage = Storage(db)
    storage.conn.execute("DROP TABLE glosses")
    storage.conn.commit()
    storage.close()
    return db, run_id, ids


class TestADatabaseFromBeforeTheGlossTable:
    def test_the_store_reads_it_as_no_glosses(self, pre_gloss_db):
        db, run_id, ids = pre_gloss_db
        storage = Storage(db)
        assert "glosses" not in storage.table_names()
        assert storage.glosses_for_run(run_id) == {}
        assert storage.gloss_get(run_id, ids[0]) is None
        storage.close()

    def test_the_record_detail_still_opens(self, pre_gloss_db, tmp_path):
        db, run_id, ids = pre_gloss_db
        client = _client(db, tmp_path)
        r = client.get(f"/api/records/{ids[0]}", params={"run_id": run_id})
        assert r.status_code == 200, r.text
        assert r.json()["gloss"] is None

    def test_listing_glosses_is_empty_not_an_error(self, pre_gloss_db, tmp_path):
        db, run_id, _ = pre_gloss_db
        client = _client(db, tmp_path)
        r = client.get("/api/glosses", params={"run_id": run_id})
        assert r.status_code == 200, r.text
        assert r.json()["glosses"] == []

    def test_the_export_never_fails_on_the_missing_table(self, pre_gloss_db, tmp_path):
        """The export may still refuse this hand-seeded Run on its own merits
        (the battery judges it), but never because a table is absent."""
        db, run_id, _ = pre_gloss_db
        client = _client(db, tmp_path)
        r = client.post("/api/export", params={"run_id": run_id})
        assert r.status_code != 500, r.text
        assert "no such table" not in r.text

    def test_approving_a_gloss_creates_the_table(self, pre_gloss_db, tmp_path):
        """The write lane applies the schema the way a Run start does, so a
        reviewer on a reused database can still approve a rendering."""
        db, run_id, ids = pre_gloss_db
        client = _client(db, tmp_path)
        r = client.post(
            "/api/glosses/review",
            json={
                "run_id": run_id, "mapping_id": ids[0],
                "english": "an approved rendering", "reviewed_by": "R. Nurtanio",
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["reviewed_by"] == "R. Nurtanio"
        storage = Storage(db)
        assert storage.gloss_get(run_id, ids[0]).english == "an approved rendering"
        storage.close()


def test_the_run_path_glosses_by_default():
    """The offline command was the only gloss lane and was OFF by default; the
    Run now drafts Glosses itself, so the flag defaults ON."""
    assert PipelineConfig().gloss_enabled is True
