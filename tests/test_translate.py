"""M3 exit-criteria tests: the three spec
asserts as named tests, the forced-failure fallback lane, the default-OFF
config flag, and the degenerate-output guard. Contract-level tests run on a
stub engine so they pass offline (CI has no gitignored weights); the real
CTranslate2 engine is exercised by the tests that skip without
models/opus_mt_mul_en_ct2/ (run scripts/fetch_m3_weights.py locally)."""

import gzip
import json
import os
import sys
from pathlib import Path

import pytest

from regcompass.contracts import GLOSS_LABEL, CanonicalText, Gloss, PipelineConfig
from regcompass.map import ConfigError
from regcompass.translate import (
    LlmEngine,
    OpusMtEngine,
    detect_language,
    draft_gloss,
    draft_glosses,
    looks_degenerate,
    make_engine,
    needs_gloss,
)

ROOT = Path(__file__).parent.parent
MODEL_DIR = ROOT / "models" / "opus_mt_mul_en_ct2"
M1_MY = Path(__file__).parent / "golden" / "m1" / "my_personal_data_protection_act_2010.json.gz"

CFG = PipelineConfig()

# The one reviewed Malay quote in the Round 1 corpus (Act A1472 s.6, three
# mapping rows share it): the named quality reference for the gloss lane.
MALAY_SAMPLE = (
    "menghendaki pemberi perkhidmatan komunikasi untuk memintas dan menyimpan "
    "komunikasi tertentu atau komunikasi sesuatu perihalan tertentu yang "
    "diterima atau dihantar, atau yang akan diterima atau dihantar oleh "
    "pemberi perkhidmatan komunikasi itu"
)


class StubEngine:
    """Deterministic offline engine for contract-level tests."""

    name = "stub"

    def __init__(self, reply: str | None = "the stub translation of the provision"):
        self.reply = reply
        self.calls = 0

    def translate(self, text: str) -> str:
        self.calls += 1
        if self.reply is None:
            raise RuntimeError("engine down")
        return self.reply


# ---------------------------------------------------------------------------
# the three spec exit asserts, as named tests
# ---------------------------------------------------------------------------


class TestSpecExitAsserts:
    def test_gloss_produced_for_a_malay_sample(self):
        """Spec assert 1 (contract level; the real-engine twin is below)."""
        g = draft_gloss("doc:c0001::7.2", MALAY_SAMPLE, StubEngine(), CFG, source_language="ms")
        assert g.english is not None and g.english.strip()
        assert g.source_language == "ms"
        assert g.uncertainty_flag is None

    def test_canonical_stream_untouched_by_glossing(self):
        """Spec assert 2: glossing chunks of a real golden stream changes no
        byte of the canonical text; the gloss lives in its own object."""
        with gzip.open(M1_MY, "rt", encoding="utf-8") as f:
            canonical = CanonicalText(**json.load(f))
        before = canonical.full_text
        items = [
            (f"chunk:{i}", before[start : start + 400])
            for i, start in enumerate(range(0, 2000, 400))
        ]
        glosses = draft_glosses(items, StubEngine(), CFG)
        assert canonical.full_text == before  # byte identity, same object
        assert len(glosses) == len(items)
        for g, (_, src) in zip(glosses, items):
            assert g.source_text == src  # provenance carries the EXACT source slice

    def test_gloss_labelled_non_authoritative_and_ai_generated(self):
        """Spec assert 3: the label is mechanical, not a convention. The
        contract cannot construct an authoritative or human gloss, and the
        human-facing rendering opens with the exact label the export battery
        enforces on the shipped Verbatim English column."""
        g = draft_gloss("k", MALAY_SAMPLE, StubEngine(), CFG)
        assert g.authority == "draft"
        assert g.ai_generated is True
        assert g.labelled_english is not None
        assert g.labelled_english.startswith(f"{GLOSS_LABEL} ")
        # the same opening-bracket rule export.py's battery enforces
        assert g.labelled_english.startswith("[")
        with pytest.raises(Exception):
            Gloss(
                key="k", source_text="x", english="y", engine="e",
                authority="approved",  # no such authority exists on this contract
            )


# ---------------------------------------------------------------------------
# fallback lane (gloss failure never blocks the pipeline)
# ---------------------------------------------------------------------------


class TestFallbackLane:
    def test_engine_failure_ships_without_gloss_and_never_raises(self):
        items = [("a", MALAY_SAMPLE), ("b", MALAY_SAMPLE)]
        glosses = draft_glosses(items, StubEngine(reply=None), CFG)
        assert len(glosses) == 2  # the list never shrinks
        for g in glosses:
            assert g.english is None
            assert g.labelled_english is None
            assert g.uncertainty_flag == "no-clear-translation-equivalent"

    def test_empty_output_takes_the_fallback_lane(self):
        g = draft_gloss("k", MALAY_SAMPLE, StubEngine(reply="   "), CFG)
        assert g.english is None
        assert g.uncertainty_flag == "no-clear-translation-equivalent"

    def test_degenerate_repetition_takes_the_fallback_lane(self):
        """Observed opus-mt failure mode on Lao: the decoder loops one
        phrase; mechanically flagged, never shipped."""
        loop = "31st edition " * 30
        g = draft_gloss("k", MALAY_SAMPLE, StubEngine(reply=loop), CFG)
        assert g.english is None
        assert g.uncertainty_flag == "no-clear-translation-equivalent"

    def test_non_english_echo_takes_the_fallback_lane(self):
        """An engine echoing the source (or transliterating) is not a gloss."""
        echo = "menghendaki pemberi perkhidmatan komunikasi untuk memintas dan menyimpan komunikasi"
        g = draft_gloss("k", MALAY_SAMPLE, StubEngine(reply=echo), CFG)
        assert g.english is None

    def test_a_gloss_without_english_must_carry_the_flag(self):
        with pytest.raises(Exception):
            Gloss(key="k", source_text="x", english=None, engine="e", uncertainty_flag=None)

    def test_config_error_is_not_swallowed(self):
        """A missing key/model is the operator's problem: it must escape the
        per-item fallback (same fail-fast rule as the transport ladders)."""

        class Misconfigured:
            name = "bad"

            def translate(self, text: str) -> str:
                raise ConfigError("the gloss LLM needs OPENROUTER_API_KEY (in .env); not set")

        with pytest.raises(ConfigError):
            draft_gloss("k", MALAY_SAMPLE, Misconfigured(), CFG)


# ---------------------------------------------------------------------------
# config flag + engine selection
# ---------------------------------------------------------------------------


class TestConfigFlag:
    def test_gloss_is_on_by_default(self):
        """The Run drafts a Gloss for every non-English Mapping, so the flag
        defaults ON; setting it False switches the whole lane off."""
        assert PipelineConfig().gloss_enabled is True
        assert PipelineConfig(gloss_enabled=False).gloss_enabled is False

    def test_llm_engine_without_key_fails_fast(self, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        from regcompass.engines import resolve_engine

        with pytest.raises(ConfigError):
            LlmEngine(resolve_engine("engine-b"))

    def test_a_key_typed_into_settings_enables_the_gloss_lane(self, monkeypatch):
        """The gloss lane reads keys through the same one reader every other
        Engine uses, so a key typed into the interface's Settings screen works
        here too. Reading os.environ directly would have made this lane the one
        place a Settings key did nothing, and a key that vanished between
        construction and the call would have been a KeyError instead of the
        clean ConfigError."""
        from regcompass import engines as engines_mod
        from regcompass.engines import resolve_engine

        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setattr(engines_mod, "_SESSION_KEYS", {}, raising=False)
        engine = resolve_engine("engine-b")
        with pytest.raises(ConfigError, match="Settings"):
            LlmEngine(engine)

        engines_mod.set_session_key("OPENROUTER_API_KEY", "sk-session-only-marker")
        gloss = LlmEngine(engine)  # constructed on the session key alone
        assert "OPENROUTER_API_KEY" not in os.environ, "the store is not the environment"

        # The key reaches the transport, and only the transport.
        sent: dict = {}

        # The batched gloss answer shape: the schema echoes each mapping_id.
        answer = json.dumps(
            {"glosses": [{"mapping_id": "gloss", "english": "translated"}]}
        )

        class _FakeLitellm:
            @staticmethod
            def completion(**kwargs):
                sent.update(kwargs)

                class _Choice:
                    message = type("M", (), {"content": answer})()

                return type("R", (), {"choices": [_Choice()]})()

        monkeypatch.setitem(sys.modules, "litellm", _FakeLitellm)
        assert gloss.translate("teks undang-undang") == "translated"
        assert sent["api_key"] == "sk-session-only-marker"

        # Forgetting it mid-session is a clean ConfigError, never a KeyError.
        engines_mod.forget_session_key("OPENROUTER_API_KEY")
        with pytest.raises(ConfigError, match="Settings"):
            gloss.translate("teks undang-undang")

    def test_make_engine_llm_needs_an_engine(self):
        cfg = PipelineConfig(gloss_engine="llm")
        with pytest.raises(ConfigError):
            make_engine(cfg, engine=None)

    def test_opus_mt_engine_missing_weights_points_at_the_fetch_script(self, tmp_path):
        with pytest.raises(ConfigError, match="fetch_m3_weights"):
            OpusMtEngine(tmp_path)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_detect_language_lao_and_und(self):
        assert detect_language("ມະຕິ ຂອງກອງປະຊຸມສະພາແຫ່ງຊາດ") == "lo"
        assert detect_language(MALAY_SAMPLE) == "und"  # Latin script is never guessed

    def test_needs_gloss_is_a_superset_of_the_export_battery(self):
        """Everything the battery flags is drafted (never disagree downward);
        the drafting lane additionally catches non-Latin scripts the battery's
        under-8-Latin-words rule passes as insufficient signal."""
        from regcompass.export import looks_non_english

        for text in (MALAY_SAMPLE, "The Minister may require a licensee to intercept."):
            if looks_non_english(text):
                assert needs_gloss(text)
            assert needs_gloss(text) == looks_non_english(text)  # Latin text: identical

    def test_needs_gloss_flags_non_latin_scripts(self):
        """Follow-up 1 regression (multilanguage fixture, 10 Jul 2026): pure
        Devanagari and Lao snippets returned False and the gloss CLI would
        have skipped them."""
        hindi = "केंद्रीय सरकार द्वारा जारी अधिसूचना के अनुसार यह प्रावधान लागू होता है"
        lao = "ມະຕິ ຂອງກອງປະຊຸມສະພາແຫ່ງຊາດ ກ່ຽວກັບການຮັບຮອງເອົາກົດຫນາຍ"
        assert needs_gloss(hindi)
        assert needs_gloss(lao)
        # under the 8-letter floor: a stray symbol or short token stays English
        assert not needs_gloss("Section 5 applies to the term deja-vu and one word: ค")

    def test_looks_degenerate_thresholds(self):
        assert looks_degenerate("standard " * 20, CFG.gloss_max_trigram_repeats)
        legal = (
            "A person must not disclose information or a document unless the "
            "disclosure is subject to conditions determined by the Minister."
        )
        assert not looks_degenerate(legal, CFG.gloss_max_trigram_repeats)

    def test_looks_degenerate_catches_the_stutter_pattern(self):
        """The A1472-fragment failure mode (10 Jul 2026): the same word doubled
        with a varying spacer defeats the trigram rule; the adjacent-pair rule
        must catch it."""
        stutter = " ".join(f"whatsoever whatsoever {w}" for w in "a b c d e f".split())
        assert looks_degenerate(stutter, CFG.gloss_max_trigram_repeats)
        # doubled words happen legitimately once or twice ("had had", names)
        assert not looks_degenerate(
            "The committee had had notice and the the record shows it.",
            CFG.gloss_max_trigram_repeats,
        )


# ---------------------------------------------------------------------------
# the CLI lane (phase 3): default-OFF gate + the draft-review artifact
# ---------------------------------------------------------------------------


class TestGlossCli:
    @staticmethod
    def _runner():
        from typer.testing import CliRunner

        from regcompass.cli import app

        return CliRunner(), app

    @staticmethod
    def _db_with_record(tmp_path: Path, economy: str, quote: str):
        from regcompass.contracts import Chunk, MappingRecord
        from regcompass.storage import Storage

        doc_id = f"doc_{economy.lower()}_x"
        rec = MappingRecord(
            mapping_id=f"{doc_id}:c0008::7.2",
            document_id=doc_id,
            chunk_id=f"{doc_id}:c0008",
            economy=economy,
            indicator_id="7.2",
            indicator_name="Interception obligations",
            section="s. 6",
            verbatim_quote=quote,
            verification_status="passed",
        )
        db = tmp_path / "work.db"
        storage = Storage(db)
        storage.apply_schema()
        storage.upsert_document(doc_id, economy, source_sha256=f"sha-{doc_id}")
        storage.upsert_chunks(
            [
                Chunk(
                    chunk_id=rec.chunk_id,
                    document_id=doc_id,
                    char_start=0,
                    char_end=len(quote),
                    text=quote,
                    section_label="s. 6",
                )
            ]
        )
        storage.upsert_mappings([rec], run_id="run_one")
        storage.close()
        return db

    def _db_with_malay_record(self, tmp_path: Path):
        return self._db_with_record(tmp_path, "MY", MALAY_SAMPLE)

    def test_gloss_refused_when_the_flag_is_off(self, tmp_path, monkeypatch):
        """A configuration that switches the lane off is obeyed, and says so,
        before the command touches anything."""
        import regcompass.cli as cli_mod

        monkeypatch.setattr(
            cli_mod, "load_pipeline", lambda *a, **k: PipelineConfig(gloss_enabled=False)
        )
        runner, app = self._runner()
        result = runner.invoke(app, ["gloss", "--db", str(tmp_path / "absent.db")])
        assert result.exit_code == 2
        assert "switched OFF" in result.output

    def test_gloss_enable_writes_the_review_artifact(self, tmp_path, monkeypatch):
        import hashlib

        import regcompass.translate as translate_mod

        monkeypatch.setattr(translate_mod, "make_engine", lambda cfg, engine=None: StubEngine())
        db = self._db_with_malay_record(tmp_path)
        db_bytes_before = hashlib.sha256(db.read_bytes()).hexdigest()
        out = tmp_path / "glosses"
        runner, app = self._runner()
        result = runner.invoke(app, ["gloss", "--db", str(db), "--out", str(out), "--enable"])
        assert result.exit_code == 0, result.output
        # the gloss lane is READ-ONLY on the working database: byte identity
        # (spec assert 2's enforceable form)
        assert hashlib.sha256(db.read_bytes()).hexdigest() == db_bytes_before
        payload = json.loads((out / "draft_glosses.json").read_text(encoding="utf-8"))
        assert len(payload["glosses"]) == 1
        g = payload["glosses"][0]
        assert g["key"] == "doc_my_x:c0008::7.2"
        assert g["source_language"] == "ms"  # economy metadata beats script detection
        assert g["authority"] == "draft" and g["ai_generated"] is True
        md = (out / "draft_glosses.md").read_text(encoding="utf-8")
        assert GLOSS_LABEL in md  # the label rides every human-facing draft
        assert "NON-AUTHORITATIVE" in md

    def test_gloss_fallback_records_are_visible_in_the_artifact(self, tmp_path, monkeypatch):
        import regcompass.translate as translate_mod

        monkeypatch.setattr(
            translate_mod, "make_engine", lambda cfg, engine=None: StubEngine(reply=None)
        )
        db = self._db_with_malay_record(tmp_path)
        out = tmp_path / "glosses"
        runner, app = self._runner()
        result = runner.invoke(app, ["gloss", "--db", str(db), "--out", str(out), "--enable"])
        assert result.exit_code == 0, result.output  # failure never blocks the lane
        payload = json.loads((out / "draft_glosses.json").read_text(encoding="utf-8"))
        g = payload["glosses"][0]
        assert g["english"] is None
        assert g["uncertainty_flag"] == "no-clear-translation-equivalent"
        assert "ships without gloss" in (out / "draft_glosses.md").read_text(encoding="utf-8")

    def test_gloss_english_only_db_needs_no_engine(self, tmp_path, monkeypatch):
        """An all-English corpus exits green without ever constructing an
        engine (Round 1 must not feel M3 even when the lane is invoked)."""
        import regcompass.translate as translate_mod

        def boom(cfg, engine=None):  # pragma: no cover - must never run
            raise AssertionError("engine constructed for an English-only db")

        monkeypatch.setattr(translate_mod, "make_engine", boom)
        db = self._db_with_record(
            tmp_path, "SG", "The Minister may by order prohibit the transfer of any data."
        )
        runner, app = self._runner()
        result = runner.invoke(app, ["gloss", "--db", str(db), "--enable"])
        assert result.exit_code == 0
        assert "no non-English snippets" in result.output


# ---------------------------------------------------------------------------
# goldens byte-identity regression
# ---------------------------------------------------------------------------


def test_goldens_byte_identity_regression():
    """M3 must leave every evidence golden byte-identical (hard guardrail 3).
    Re-verifies the pinned manifest here so the M3 suite fails on its own if
    this branch ever moves an evidence byte, independent of which other test
    files a run selects."""
    import hashlib

    # one source of truth for the pinned set: a second hardcoded tuple here
    # silently went stale the moment test_evidence_goldens.py pinned more dirs
    from test_evidence_goldens import EVIDENCE_DIRS

    golden = ROOT / "tests" / "golden"
    manifest = {}
    for line in (golden / "EVIDENCE.sha256").read_text(encoding="utf-8").strip().splitlines():
        digest, rel = line.split("  ", 1)
        manifest[rel] = digest
    actual = {}
    for d in EVIDENCE_DIRS:
        for p in sorted((golden / d).rglob("*")):
            if p.is_file():
                actual[p.relative_to(golden).as_posix()] = hashlib.sha256(
                    p.read_bytes()
                ).hexdigest()
    assert actual == manifest


# ---------------------------------------------------------------------------
# the real engine (skips without the fetched weights; run locally for VERIFY)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not (MODEL_DIR / "model.bin").exists(),
    reason="gloss weights not fetched (scripts/fetch_m3_weights.py)",
)
class TestRealOpusMtEngine:
    def test_gloss_produced_for_a_malay_sample_real_engine(self):
        """Spec assert 1 on the REAL engine: the reviewed A1472 Malay quote
        drafts to non-empty, non-degenerate English mentioning interception
        or communications (fidelity is judged by the phase 4 comparison
        artifact, not asserted here)."""
        engine = OpusMtEngine(MODEL_DIR)
        g = draft_gloss("doc:c0008::7.2", MALAY_SAMPLE, engine, CFG, source_language="ms")
        assert g.english is not None
        assert g.engine == "opus-mt-mul-en-ct2"
        assert any(w in g.english.lower() for w in ("intercept", "communication"))

    def test_real_engine_is_deterministic(self):
        engine = OpusMtEngine(MODEL_DIR)
        first = draft_gloss("k", MALAY_SAMPLE, engine, CFG)
        second = draft_gloss("k", MALAY_SAMPLE, engine, CFG)
        assert first.english == second.english

    def test_lao_repetition_collapse_is_caught_not_shipped(self):
        """Pinned as a regression: opus-mt on Lao
        degenerates, and the mechanical guard routes it to the fallback lane
        (which is exactly why the Lao demo uses the LLM alternate)."""
        engine = OpusMtEngine(MODEL_DIR)
        lao = "ມະຕິ ຂອງກອງປະຊຸມສະພາແຫ່ງຊາດ ກ່ຽວກັບການຮັບຮອງເອົາກົດຫນາຍວ່າດ້ວຍທຸລະກໍາທາງເອເລັກໂຕຣນິກ"
        g = draft_gloss("k", lao, engine, CFG)
        assert g.source_language == "lo"
        # either the guard catches the collapse (english=None + flag) or the
        # model produces real English; it must never ship labelled garbage
        if g.english is not None:
            assert not looks_degenerate(g.english, CFG.gloss_max_trigram_repeats)
