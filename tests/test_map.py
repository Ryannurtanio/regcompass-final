"""M6 map: the LLM SELECTS a verbatim quote per (chunk, indicator) pair; the code
anchors it byte-for-byte to the chunk slice or rejects it. Lanes under test:
ground-truth mapping, insufficient_evidence rather than a forced
mapping, preamble must-not-map, malformed output counts an attempt then drops,
paraphrase never survives, an Engine swap keeps the same prompt and schema.

The live lane needs OPENROUTER_API_KEY; it skips cleanly when unavailable. Run
it locally to verify the live mapping lane.
"""

from __future__ import annotations

import gzip
import json
import os
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import pytest

# A base-tier install (no `live` extra) must SKIP this module, not fail inside
# map_gated_chunk's lazy litellm import.
pytest.importorskip("litellm", reason="M6 map tests need the `live` extra (litellm)")

import regcompass.map as map_mod  # noqa: E402
from regcompass.config import CONFIG_DIR, load_indicators, load_models
from regcompass.contracts import Chunk, Engine, GatedChunk, PipelineConfig
from regcompass.engines import cost_usd_for, read_meter, resolve_engine, start_meter
from regcompass.map import (
    MAX_QUOTE_CHARS,
    MIN_QUOTE_CHARS,
    MapOutcome,
    _completion_kwargs,
    _response_format,
    anchor_quote,
    build_prompt,
    map_gated_chunk,
)

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_M4 = ROOT / "tests/golden/m4"
GOLDEN_M5 = ROOT / "tests/golden/m5"
# Prompt snapshots live OUTSIDE tests/golden: the goldens are the evidence
# chain (hashed by EVIDENCE.sha256), and a prompt wording change must not read
# as an evidence change.
SNAPSHOTS = ROOT / "tests/snapshots"

MY = "my_personal_data_protection_act_2010"
SG = "sg_telecommunications_act_1999"


from conftest import needs_paid  # noqa: E402 - money is opt-in; see conftest

needs_openrouter = pytest.mark.skipif(
    not os.environ.get("OPENROUTER_API_KEY"),
    reason="live OpenRouter eval needs OPENROUTER_API_KEY",
)


def ollama_engine(**overrides) -> Engine:
    """An Ollama-hosted Engine built inline. The registry ships none today
    (the local Engine B lane adds one), but the Ollama branch of the transport
    must stay wired and tested."""
    base = dict(
        name="local-inline",
        display_name="Inline Ollama Engine (test)",
        litellm_model="ollama_chat/qwen3:4b",
        provider="ollama",
        api_key_env=None,
        usd_per_million_input_tokens=0,
        usd_per_million_output_tokens=0,
        open_weights=True,
        structured_output="ollama_format",
        concurrency=1,
    )
    base.update(overrides)
    return Engine(**base)


# ---------------------------------------------------------------------------
# fixture loading: golden M5 passed pairs are M6's real inputs
# ---------------------------------------------------------------------------


@lru_cache(maxsize=None)
def _passed_pairs(slug: str) -> dict[tuple[str, str], GatedChunk]:
    with gzip.open(GOLDEN_M5 / f"{slug}.gated.json.gz", "rt", encoding="utf-8") as f:
        payload = json.load(f)
    out: dict[tuple[str, str], GatedChunk] = {}
    for row in payload["passed"]:
        g = GatedChunk.model_validate(row)
        out[(g.chunk.chunk_id.split(":")[1], g.indicator_id)] = g
    return out


def gated_pair(slug: str, cid: str, indicator: str) -> GatedChunk:
    return _passed_pairs(slug)[(cid, indicator)]


@lru_cache(maxsize=None)
def _m4_chunk(slug: str, cid: str) -> Chunk:
    with gzip.open(GOLDEN_M4 / f"{slug}.chunks.json.gz", "rt", encoding="utf-8") as f:
        for row in json.load(f)["chunks"]:
            if row["chunk_id"].endswith(f":{cid}"):
                return Chunk.model_validate(row)
    raise KeyError(f"{slug}:{cid}")


def synthetic_gated(chunk: Chunk, indicator: str) -> GatedChunk:
    """A hand-paired GatedChunk for lanes the real gate did not produce."""
    return GatedChunk(
        chunk=chunk,
        indicator_id=indicator,  # type: ignore[arg-type]
        cosine_pillar=0.5,
        bm25_indicator=1.0,
        gate_decision="passed",
    )


# ---------------------------------------------------------------------------
# offline fixtures
# ---------------------------------------------------------------------------

LAW_TEXT = (
    "Section 12. Transfer of customer data abroad\n"
    "(1) A licensee must not transfer customer data to a place outside the\n"
    "economy unless the customer has given express consent in writing.\n"
    "(2) The Authority may exempt any class of transfers from subsection (1).\n"
)
GOOD_QUOTE = (
    "must not transfer customer data to a place outside the\n"
    "economy unless the customer has given express consent in writing"
)


def tiny_gated(indicator: str = "6.4") -> GatedChunk:
    chunk = Chunk(
        chunk_id="doc_test:c0001",
        document_id="doc_test",
        char_start=0,
        char_end=len(LAW_TEXT),
        text=LAW_TEXT,
        section_label="s. 12",
        chunk_kind="section",
        page_start=3,
    )
    return synthetic_gated(chunk, indicator)


def selection(maps: bool, quote: str = "", subsection=None, impact=None) -> str:
    return json.dumps(
        {
            "maps_to_indicator": maps,
            "verbatim_quote": quote,
            "subsection": subsection,
            "impact": impact,
        }
    )


def scripted(responses: list[str]):
    """Fake completion_fn replaying canned responses; records every call."""
    calls: list[tuple[str, bool]] = []

    def fn(prompt: str, strict: bool) -> str:
        calls.append((prompt, strict))
        return responses[min(len(calls) - 1, len(responses) - 1)]

    fn.calls = calls  # type: ignore[attr-defined]
    return fn


# ---------------------------------------------------------------------------
# config: indicator definitions
# ---------------------------------------------------------------------------


class TestIndicatorConfig:
    def test_every_pillar_is_present_with_official_names(self):
        """All 12 Pillars now, generated from the organizer's sheets. The nine
        Round 1 Indicators keep their hand-written name and definition byte for
        byte (the golden evidence files pin both)."""
        defs = load_indicators()
        assert {d.pillar for d in defs.values()} == set(range(1, 13))
        assert set(defs) >= {"6.1", "6.2", "6.3", "6.4", "7.1", "7.2", "7.3", "7.4", "7.5"}
        assert defs["6.1"].name == "Ban and local processing requirements"
        assert defs["7.5"].name == "Requirements to allow Government access to personal data"
        assert defs["6.1"].definition_derived is False
        for d in defs.values():
            assert len(d.definition) > 50

    def test_a_malformed_indicator_id_fails_at_load(self, tmp_path):
        raw = json.loads((CONFIG_DIR / "indicators.json").read_text())
        raw["P6-I1"] = raw["6.1"]
        (tmp_path / "indicators.json").write_text(json.dumps(raw))
        with pytest.raises(ValueError, match="P6-I1"):
            load_indicators(tmp_path)

    def test_an_indicator_under_the_wrong_pillar_fails_at_load(self, tmp_path):
        raw = json.loads((CONFIG_DIR / "indicators.json").read_text())
        raw["7.3"] = {**raw["7.3"], "pillar": 6}
        (tmp_path / "indicators.json").write_text(json.dumps(raw))
        with pytest.raises(ValueError, match="7.3"):
            load_indicators(tmp_path)


# ---------------------------------------------------------------------------
# prompt contract
# ---------------------------------------------------------------------------


class TestPrompt:
    def test_prompt_carries_indicator_chunk_and_anatomy_rules(self):
        defs = load_indicators()
        prompt = build_prompt(tiny_gated("6.4"), "SG", defs["6.4"], strict=False)
        assert "6.4" in prompt
        assert "Conditional flow regimes" in prompt
        assert defs["6.4"].definition[:60] in prompt
        assert LAW_TEXT in prompt  # the full provision, uncut
        assert "s. 12" in prompt
        # statute-anatomy rules
        low = prompt.lower()
        for marker in ("must", "shall", "preamble", "penalt", "includes", "schedule"):
            assert marker in low, f"anatomy rule marker '{marker}' missing"
        assert "maps_to_indicator" in prompt and "verbatim_quote" in prompt

    def test_strict_prompt_is_a_superset(self):
        defs = load_indicators()
        base = build_prompt(tiny_gated(), "SG", defs["6.4"], strict=False)
        strict = build_prompt(tiny_gated(), "SG", defs["6.4"], strict=True)
        assert strict.startswith(base)
        assert "character-for-character" in strict

    @pytest.mark.parametrize("indicator", ["6.4", "7.3"])
    def test_prompt_inlines_this_indicators_registry_criteria(self, indicator):
        """The scoring ladder the Engine is judged against is the registry's own
        text, not a paraphrase: an Indicator whose criteria change in
        config/indicators.json changes the prompt with it."""
        defs = load_indicators()
        ind_def = defs[indicator]
        prompt = build_prompt(tiny_gated(indicator), "MY", ind_def, strict=False)
        assert ind_def.criteria
        assert ind_def.criteria in prompt
        assert ind_def.exception and ind_def.exception in prompt
        # and no OTHER Indicator's ladder leaks in
        other = defs["6.1" if indicator != "6.1" else "7.3"]
        assert other.criteria and other.criteria not in prompt

    def test_prompt_states_partial_evidence_and_the_overlap_rule(self):
        defs = load_indicators()
        prompt = build_prompt(tiny_gated("6.4"), "MY", defs["6.4"], strict=False)
        assert "does NOT have to satisfy the indicator" in prompt
        assert "EVEN PARTIALLY" in prompt
        # the neighbouring-indicator paragraph is generic: it names no Indicator
        assert "NEIGHBOURING" in prompt
        assert "may legitimately be cited under more" in prompt

    def test_the_impact_sentence_is_asked_for_in_english(self):
        """A rationale mixing English with the source's own script reads as
        broken to a reviewer; a source term may only follow its English
        rendering, in quotation marks."""
        defs = load_indicators()
        prompt = build_prompt(tiny_gated("6.4"), "CN", defs["6.4"], strict=False)
        assert "Write it in English" in prompt
        assert "only in quotation marks after its English" in prompt

    def test_prompt_is_pinned_by_a_snapshot(self):
        """The prompt is the one input every Engine's measured score depends on,
        so it cannot drift silently. Regenerate ON PURPOSE with
        REGCOMPASS_UPDATE_SNAPSHOTS=1 and re-measure both Engines after."""
        defs = load_indicators()
        prompt = build_prompt(tiny_gated("6.4"), "SG", defs["6.4"], strict=False)
        snapshot = SNAPSHOTS / "map_prompt_6_4.txt"
        if os.environ.get("REGCOMPASS_UPDATE_SNAPSHOTS"):
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            snapshot.write_text(prompt, encoding="utf-8")
        assert snapshot.exists(), f"missing snapshot {snapshot}"
        assert prompt == snapshot.read_text(encoding="utf-8"), (
            "the mapping prompt changed; both Engines must be re-measured on the"
            " EVAL_SET before this snapshot is regenerated"
            " (REGCOMPASS_UPDATE_SNAPSHOTS=1)"
        )


# ---------------------------------------------------------------------------
# quote anchoring: the mechanical heart of M6
# ---------------------------------------------------------------------------


class TestAnchorQuote:
    def test_exact_substring_passes_through(self):
        assert anchor_quote(LAW_TEXT, GOOD_QUOTE) == GOOD_QUOTE

    def test_whitespace_mangled_quote_reanchors_to_exact_slice(self):
        mangled = " ".join(GOOD_QUOTE.split())
        assert mangled != GOOD_QUOTE
        anchored = anchor_quote(LAW_TEXT, mangled)
        assert anchored == GOOD_QUOTE
        assert anchored in LAW_TEXT

    def test_paraphrase_never_anchors(self):
        assert anchor_quote(LAW_TEXT, "licensees may not send customer data overseas") is None

    def test_single_character_change_never_anchors(self):
        assert anchor_quote(LAW_TEXT, GOOD_QUOTE.replace("express", "expressed")) is None

    def test_ambiguous_reanchor_is_rejected(self):
        # not an exact substring anywhere, and the whitespace-insensitive form
        # matches two different slices: refuse rather than guess
        text = "The fee\nis due.\nThe fee  is due.\n"
        assert "The fee is due." not in text
        assert anchor_quote(text, "The fee is due.") is None

    def test_empty_quote_rejected(self):
        assert anchor_quote(LAW_TEXT, "   ") is None


# ---------------------------------------------------------------------------
# selection lanes (offline, scripted completions)
# ---------------------------------------------------------------------------


class TestSelectionLanes:
    def test_valid_selection_maps(self):
        fn = scripted([selection(True, GOOD_QUOTE, "(1)", "Consent required before transfer.")])
        out = map_gated_chunk(tiny_gated("6.4"), "SG", completion_fn=fn)
        assert out.outcome == "mapped" and out.attempts == 1
        r = out.record
        assert r is not None
        assert r.indicator_id == "6.4"
        assert r.indicator_name == "Conditional flow regimes"
        assert r.section == "s. 12"
        assert r.subsection == "(1)"
        assert r.page_number == 3
        assert r.economy == "SG"
        assert r.chunk_id == "doc_test:c0001"
        assert r.verbatim_quote in LAW_TEXT  # byte-for-byte, always
        assert r.insufficient_evidence is False
        assert r.verification_status == "unverified"
        assert r.confidence is None  # mechanical, never model-reported
        assert r.extraction_attempts == 1
        assert r.mapping_id == "doc_test:c0001::6.4"

    def test_no_evidence_lane(self):
        fn = scripted([selection(False)])
        out = map_gated_chunk(tiny_gated("7.3"), "SG", completion_fn=fn)
        assert out.outcome == "no_evidence" and out.attempts == 1
        assert out.record is not None
        assert out.record.insufficient_evidence is True
        assert out.record.verbatim_quote == ""

    def test_garbage_then_valid_counts_attempts_and_escalates(self):
        fn = scripted(["not json at all", selection(True, GOOD_QUOTE, "(1)", "x")])
        out = map_gated_chunk(tiny_gated(), "SG", completion_fn=fn)
        assert out.outcome == "mapped" and out.attempts == 2
        assert out.record is not None and out.record.extraction_attempts == 2
        assert [s for _, s in fn.calls] == [False, True]  # stricter prompt on retry

    def test_extra_key_is_malformed(self):
        bad = json.dumps(
            {
                "maps_to_indicator": True,
                "verbatim_quote": GOOD_QUOTE,
                "subsection": "(1)",
                "impact": "x",
                "confidence": 0.95,  # the model never self-reports confidence
            }
        )
        fn = scripted([bad, selection(True, GOOD_QUOTE, "(1)", "x")])
        out = map_gated_chunk(tiny_gated(), "SG", completion_fn=fn)
        assert out.attempts == 2 and out.outcome == "mapped"

    def test_all_malformed_drops_after_max_attempts(self):
        fn = scripted(["{", "[]", "still not it"])
        out = map_gated_chunk(tiny_gated(), "SG", completion_fn=fn)
        assert out.outcome == "dropped"
        assert out.record is None
        assert out.attempts == PipelineConfig().extraction_attempts == 3
        assert len(fn.calls) == 3
        assert len(out.failures) == 3

    def test_paraphrased_quote_is_malformed_and_drops(self):
        fn = scripted([selection(True, "licensees may not send data overseas", "(1)", "x")])
        out = map_gated_chunk(tiny_gated(), "SG", completion_fn=fn)
        assert out.outcome == "dropped" and out.attempts == 3
        assert any("anchor" in f for f in out.failures)

    def test_whitespace_mangled_quote_ships_the_exact_slice(self):
        mangled = " ".join(GOOD_QUOTE.split())
        fn = scripted([selection(True, mangled, "(1)", "x")])
        out = map_gated_chunk(tiny_gated(), "SG", completion_fn=fn)
        assert out.outcome == "mapped" and out.attempts == 1
        assert out.record is not None
        assert out.record.verbatim_quote == GOOD_QUOTE
        assert out.record.verbatim_quote in LAW_TEXT

    def test_maps_true_with_empty_quote_is_malformed(self):
        fn = scripted([selection(True, ""), selection(False)])
        out = map_gated_chunk(tiny_gated(), "SG", completion_fn=fn)
        assert out.attempts == 2 and out.outcome == "no_evidence"

    def test_trivially_short_quote_is_malformed(self):
        fn = scripted([selection(True, LAW_TEXT[: MIN_QUOTE_CHARS - 5], "(1)", "x")])
        out = map_gated_chunk(tiny_gated(), "SG", completion_fn=fn)
        assert out.outcome == "dropped"

    def test_overlong_quote_is_malformed(self):
        fn = scripted([selection(True, "x" * (MAX_QUOTE_CHARS + 1), None, "x")])
        out = map_gated_chunk(tiny_gated(), "SG", completion_fn=fn)
        assert out.outcome == "dropped"

    def test_excluded_pair_is_refused(self):
        g = tiny_gated().model_copy(update={"gate_decision": "excluded"})
        with pytest.raises(ValueError, match="excluded"):
            map_gated_chunk(g, "SG", completion_fn=scripted([selection(False)]))

    def test_fenced_json_is_accepted(self):
        fenced = f"```json\n{selection(True, GOOD_QUOTE, '(1)', 'x')}\n```"
        out = map_gated_chunk(tiny_gated(), "SG", completion_fn=scripted([fenced]))
        assert out.outcome == "mapped" and out.attempts == 1


# ---------------------------------------------------------------------------
# tier plumbing: same prompt + same schema on both tiers, retries pinned 0
# ---------------------------------------------------------------------------


class TestEnginePlumbing:
    def test_response_format_is_strict_closed_schema(self):
        rf = _response_format()
        assert rf["type"] == "json_schema"
        js = rf["json_schema"]
        assert js["strict"] is True
        schema = js["schema"]
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == {
            "maps_to_indicator",
            "verbatim_quote",
            "subsection",
            "impact",
        }

    def test_openrouter_engine_kwargs(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-never-used-offline")
        kw = _completion_kwargs(load_models().engine("engine-b"))
        assert kw["model"].startswith("openrouter/")
        assert kw["num_retries"] == 0
        assert kw["temperature"] == 0
        assert kw["timeout"] == 180  # stalled-stream ceiling; the transport ladder retries loudly
        assert kw["api_key"] == "test-key-never-used-offline"
        assert kw["response_format"] == _response_format()

    def test_ollama_engine_kwargs_use_ollama_chat_and_host(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_HOST", "http://example.test:12345")
        kw = _completion_kwargs(ollama_engine())
        assert kw["model"].startswith("ollama_chat/")
        assert kw["api_base"] == "http://example.test:12345"
        assert kw["num_retries"] == 0
        assert kw["response_format"] == _response_format()
        assert "api_key" not in kw
        # Ollama's server default is 4,096 tokens; long statute chunks
        # overflow it (live run, exceed_context_size_error)
        assert kw["num_ctx"] == 16384
        # local inference is legitimately slow on 18k-char sections; the
        # 180s stalled-stream ceiling is a REMOTE concern
        assert kw["timeout"] == 600
        # runaway-generation bound (live run: a local quote loop filled the
        # whole 16k context); maps to Ollama num_predict
        assert kw["max_tokens"] == 4096

    def test_openrouter_engine_gets_no_num_ctx(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-never-used-offline")
        assert "num_ctx" not in _completion_kwargs(load_models().engine("engine-b"))

    def test_missing_engine_key_raises(self, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
            _completion_kwargs(load_models().engine("engine-b"))

    def test_an_engine_without_structured_output_is_refused(self):
        with pytest.raises(ValueError, match="structured output"):
            _completion_kwargs(ollama_engine(structured_output="none"))

    def test_engine_swap_keeps_the_prompt_identical(self):
        defs = load_indicators()
        base = build_prompt(tiny_gated(), "SG", defs["6.4"], strict=False)
        # the prompt builder takes no Engine input at all: swap is config-only
        import inspect

        assert "engine" not in inspect.signature(build_prompt).parameters
        # and no model/transport vocabulary leaks into the words either
        for token in ("30b", "4b", "qwen", "openrouter", "ollama", "litellm"):
            assert token not in base.lower()
        # while the inputs that SHOULD change the prompt do change it
        assert build_prompt(tiny_gated(), "SG", defs["6.4"], strict=True) != base
        assert build_prompt(tiny_gated(), "SG", defs["6.4"], strict=False, hint="x") != base

    def test_mock_transport_end_to_end(self, monkeypatch):
        """litellm mock_response exercises the production call path offline."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-never-used-offline")
        monkeypatch.setattr(
            map_mod, "_LITELLM_MOCK_RESPONSE", selection(True, GOOD_QUOTE, "(1)", "x")
        )
        out = map_gated_chunk(tiny_gated(), "SG")
        assert out.outcome == "mapped"
        assert out.record is not None and out.record.verbatim_quote in LAW_TEXT


# ---------------------------------------------------------------------------
# live evaluation (the module exit gate; skips without key/server)
# ---------------------------------------------------------------------------

# Per-pair ground truth for the mapper, derived from the ESCAP Round 1 Database
# and RE-VERIFIED against the xlsx cells directly (Malaysia R38/R45, Singapore
# R42). The mapper's GT is TEXT evidence: whether the
# provision's own text evidences the indicator. That differs from M5's recall GT
# (every ESCAP-cited section must merely REACH the mapper) in three pairs:
# - MY s.6 / s.9 x 6.1: ESCAP scores MY 6.1 = 0 (no ban exists; the whitelist
#   regime is classified 6.4). Zero-score rows emit "No provision found", so
#   the correct mapper output here is insufficient evidence.
# - SG s.5 x 7.3: ESCAP's retention quote lives in the licence instrument
#   GRANTED UNDER s.5, not in the Act text. Known indirect-evidence limitation.
# expect=True: must map with an in-chunk quote. hard=True: negative
# lanes (preamble, irrelevant) that must individually behave, not just average.
EVAL_SET: list[tuple[str, str, str, str, bool, bool, str]] = [
    (MY, "MY", "c0007", "6.1", False, False, "s. 6 consent principle: no ban evidence (MY 6.1 = 0)"),
    (MY, "MY", "c0010", "6.1", False, False, "s. 9 security principle: no ban evidence (MY 6.1 = 0)"),
    (MY, "MY", "c0130", "6.1", True, False, "s. 129 transfer prohibition language (ESCAP 6.1 row analyses it)"),
    (MY, "MY", "c0007", "6.4", True, False, "s. 6 consent condition"),
    (MY, "MY", "c0010", "6.4", True, False, "s. 9 security principle (ESCAP cites for 6.4)"),
    (MY, "MY", "c0130", "6.4", True, False, "s. 129 conditional transfer regime"),
    (MY, "MY", "c0046", "7.5", True, False, "s. 45 crime-purpose exemption"),
    (MY, "MY", "c0122", "7.5", True, False, "s. 121 production powers"),
    (SG, "SG", "c0006", "7.3", False, False, "s. 5: retention duty is in the licence, not the Act text"),
    (MY, "MY", "c0002", "7.4", False, True, "s. 1 short title: preamble must-not-map"),
    (SG, "SG", "c0023", "6.1", False, True, "s. 22 tree removal: irrelevant chunk"),
]


def _eval_pair(slug, cid, indicator) -> GatedChunk:
    if (cid, indicator) in _passed_pairs(slug):
        return gated_pair(slug, cid, indicator)
    return synthetic_gated(_m4_chunk(slug, cid), indicator)


PAID_RECORD_DIR = ROOT / "tests/.paid"


def _write_paid_record(engine_name: str, engine, payload: dict) -> Path:
    """One measured Engine, one JSON file. The gate is the only place these
    numbers are produced, so it has to leave them behind: a paid run that prints
    nothing cannot be the source of the figures the README publishes, and
    nobody is going to re-spend the money to check a claim. Gitignored: the
    file records a run on a machine with a key, not a repository fact."""
    PAID_RECORD_DIR.mkdir(parents=True, exist_ok=True)
    path = PAID_RECORD_DIR / f"{engine_name}.json"
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    return path


def declared_engines() -> list[str]:
    """Every Engine the registry declares except the offline fake one. Read from
    config/models.yaml, never hard-coded: the accuracy gate must follow an Engine
    swap automatically, because the engine declaration in the submission is
    immutable after 30 Sep and may not be written on an unmeasured Engine."""
    return [name for name, e in load_models().engines.items() if e.provider != "fake"]


@pytest.mark.slow
@pytest.mark.paid
@needs_paid
@needs_openrouter
class TestLiveEvalSet:
    """The ground-truth gate, run on EVERY declared Engine (the default one
    included, under its own name). Before 16 Sep 2026 it ran on the default
    Engine alone, so Engine A had never been measured at all."""

    @pytest.mark.parametrize("engine_name", declared_engines())
    def test_gt_eval_accuracy_and_quote_in_chunk(self, engine_name):
        engine = resolve_engine(engine_name)
        results = []
        unanchored: list[str] = []
        hard_violations: list[str] = []
        # The meter is the production one (regcompass.engines): the same
        # counting the Run Record uses, so the figures here and the figures a
        # run reports come from one mechanism.
        start_meter()
        for slug, econ, cid, ind, expect, hard, why in EVAL_SET:
            g = _eval_pair(slug, cid, ind)
            out = map_gated_chunk(g, econ, engine=engine)  # type: ignore[arg-type]
            got = out.outcome == "mapped"
            ok = got == expect and out.outcome != "dropped"
            if out.record is not None and out.record.verbatim_quote:
                # collected, not raised: the whole set is paid for either way,
                # so the run records every pair before it fails on one
                if out.record.verbatim_quote not in g.chunk.text:
                    unanchored.append(why)
            # negative lanes are hard requirements, not accuracy fodder
            if hard and out.outcome != "no_evidence":
                hard_violations.append(f"{why}: got {out.outcome}")
            results.append((why, expect, out.outcome, ok))
        meter = read_meter()
        n_ok = sum(1 for *_, ok in results if ok)
        acc = n_ok / len(results)
        cost = cost_usd_for(engine, meter.prompt_tokens, meter.completion_tokens)
        path = _write_paid_record(
            engine_name,
            engine,
            {
                "engine": engine_name,
                "display_name": engine.display_name,
                "model": engine.litellm_model,
                "measured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "threshold": 0.9,
                "accuracy": acc,
                "pairs": len(results),
                "pairs_passed": n_ok,
                "calls": meter.calls,
                "prompt_tokens": meter.prompt_tokens,
                "completion_tokens": meter.completion_tokens,
                "cost_usd": cost,
                "provider_cost_usd": meter.provider_cost_usd,
                "unanchored_quotes": unanchored,
                "hard_lane_violations": hard_violations,
                "rows": [
                    {"why": w, "expect": e, "outcome": o, "ok": ok}
                    for w, e, o, ok in results
                ],
            },
        )
        # one line per Engine, visible with -rA or -s; this is where the
        # README's published per-Engine numbers come from
        print(
            f"PAID GATE {engine_name} ({engine.litellm_model}): score {acc:.2f}"
            f" (bar 0.90), {n_ok}/{len(results)} pairs, tokens"
            f" {meter.prompt_tokens}/{meter.completion_tokens} over {meter.calls}"
            f" calls, cost USD {cost:.5f}, provider USD"
            f" {meter.provider_cost_usd}, recorded in {path.relative_to(ROOT)}"
        )
        assert not unanchored, f"{engine_name}: quote not in chunk: {unanchored}"
        assert not hard_violations, f"{engine_name}: {hard_violations}"
        detail = "\n".join(f"{'OK ' if ok else 'MISS'} exp={e} got={o} {w}" for w, e, o, ok in results)
        assert acc >= 0.9, (
            f"{engine_name} ({engine.display_name}) indicator accuracy"
            f" {acc:.2f} < 0.9\n{detail}"
        )


class TestFakeEngine:
    """The fake Engine runs the real M6 lane offline: no key, no network."""

    def test_fake_engine_maps_without_key_or_network(self, monkeypatch):
        from regcompass.engines import resolve_engine

        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        g = gated_pair(MY, "c0130", "6.4")
        out = map_gated_chunk(g, "MY", engine=resolve_engine("fake"))
        assert out.outcome != "dropped"
        assert out.record is not None
        if out.record.verbatim_quote:
            assert out.record.verbatim_quote in g.chunk.text
