"""M5 exit-criteria tests: the two-tier gate (BGE-M3 pillar cosine, then bm25s
indicator vocabulary) over the real golden M4 chunks.

Recall ground truth comes from the ESCAP Round 1 Database xlsx (verified
against the cells directly, 5 Jul 2026): sections cited for our fixture acts
in Pillars 6-7 that exist in the base-act fixtures. Every cited section must
SURVIVE the gate for its indicator; excluded chunks are logged as GatedChunk
rows with gate_decision="excluded", never silently dropped.

Live-embedding tests need a local ollama server with bge-m3 and are skipped
when it is unreachable (CI has no ollama); the decision logic, exclusion-log,
and failure lanes run everywhere via an injected embed_fn."""

import gzip
import json
from pathlib import Path

import numpy as np
import pytest

from regcompass.contracts import Chunk, GatedChunk, PipelineConfig
from regcompass.gate import (
    GateReport,
    configured_pillars,
    embed_ollama,
    gate_document,
    indicators_for,
    legal_tokens,
    load_indicator_vocab,
    load_pillar_texts,
)
from regcompass.storage import Storage, VectorIndex

GOLDEN = Path(__file__).parent / "golden"

# These fixtures and their recall ground truth are Pillar 6 and 7 material, so
# every gate_document call here names those Pillars, exactly as a Run does.
PILLARS_67 = (6, 7)

# In-fixture recall ground truth (ESCAP Round 1 Database, AU/MY/SG sheets).
# MY 12a(1)/12b and "Amendment of Section 5" are 2024 Amendment Bill sections
# absent from the 2010 base act; Codes of Practice and Criminal Procedure Code
# refs belong to other instruments. AU's Criminal Code is cited with no
# section (7.2), so AU exercises reduction + exclusion lanes only.
GROUND_TRUTH = {
    "my": {"6.1": ["s. 6", "s. 9", "s. 129"], "6.4": ["s. 6", "s. 9", "s. 129"],
           "7.5": ["s. 45", "s. 121"]},
    "sg": {"7.3": ["s. 5"]},
}


from conftest import needs_ollama  # noqa: E402 - shared OLLAMA_HOST-aware probe


def load_chunks(rel: str) -> list[Chunk]:
    with gzip.open(GOLDEN / rel, "rt", encoding="utf-8") as f:
        return [Chunk.model_validate(c) for c in json.load(f)["chunks"]]


@pytest.fixture(scope="module")
def my_chunks():
    return load_chunks("m4/my_personal_data_protection_act_2010.chunks.json.gz")


@pytest.fixture(scope="module")
def sg_chunks():
    return load_chunks("m4/sg_telecommunications_act_1999.chunks.json.gz")


@pytest.fixture(scope="module")
def au_chunks():
    return load_chunks("m4/au_C2026C00098VOL01.chunks.json.gz")


def survivors(gated: list[GatedChunk], indicator: str) -> dict[str, GatedChunk]:
    return {
        g.chunk.section_label: g
        for g in gated
        if g.indicator_id == indicator and g.gate_decision == "passed"
    }


def find(gated: list[GatedChunk], indicator: str, label_end: str) -> GatedChunk:
    hits = [
        g for g in gated
        if g.indicator_id == indicator
        and (g.chunk.section_label == label_end or g.chunk.section_label.endswith(label_end))
    ]
    assert hits, f"no gated row for {label_end!r} x {indicator}"
    return hits[0]


# ---------------------------------------------------------------------------
# tokenizer (statutory tokens preserved as single tokens)
# ---------------------------------------------------------------------------


class TestLegalTokens:
    def test_statutory_tokens_survive_as_units(self):
        toks = legal_tokens("Under Part IVA and s 15(2) of Cap. 88, the transfer is banned.")
        assert "partiva" in toks
        assert "s15(2)" in toks
        assert "cap88" in toks
        assert "transfer" in toks

    def test_section_word_and_dot_variants(self):
        assert "s129" in legal_tokens("Section 129 applies")
        assert "s129" in legal_tokens("see s. 129")
        assert "s45(2)" in legal_tokens("Section 45(2) exempts")

    def test_plain_words_lowercased_hyphens_kept(self):
        toks = legal_tokens("Cross-Border TRANSFER of data")
        assert "cross-border" in toks and "transfer" in toks


# ---------------------------------------------------------------------------
# decision logic + exclusion log, offline via injected embeddings
# ---------------------------------------------------------------------------


def fake_embed_factory(hot_markers: set[str]):
    """Deterministic embeddings: the pillar description texts (recognised by
    identity) and any chunk containing a hot marker substring share an axis;
    every other text is orthogonal to the pillars."""
    pillar_texts = set(load_pillar_texts(PILLARS_67).values())

    def fake_embed(texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), 8), dtype=np.float32)
        for i, t in enumerate(texts):
            if t in pillar_texts or any(m in t for m in hot_markers):
                out[i, 0] = 1.0
            else:
                out[i, 1] = 1.0
        return out

    return fake_embed


def tiny_chunks() -> list[Chunk]:
    texts = {
        "s. 1": "The transfer of personal data outside the territory is prohibited "
                "without consent. Cross-border transfer requires adequacy.",
        "s. 2": "The Minister may appoint officers to sit on the tribunal bench.",
        "front matter": "AN ACT relating to data.",
    }
    chunks, off = [], 0
    stream = "".join(texts.values())
    for i, (label, t) in enumerate(texts.items()):
        chunks.append(Chunk(
            chunk_id=f"doc_tiny:c{i:04d}", document_id="doc_tiny",
            char_start=off, char_end=off + len(t), text=t,
            section_label=label,
            chunk_kind="section" if label.startswith("s.") else "front_matter",
        ))
        off += len(t)
    assert stream  # silence linters
    return chunks


class TestDecisionLogic:
    def test_every_section_x_indicator_pair_is_logged(self):
        chunks = tiny_chunks()
        gated, report = gate_document(chunks, pillars=PILLARS_67, embed_fn=fake_embed_factory({"transfer of personal data", "tribunal"}))
        # 2 section chunks x 9 indicators, passed or excluded, nothing dropped
        assert len(gated) == 2 * 9
        assert {g.gate_decision for g in gated} <= {"passed", "excluded"}
        assert report.n_candidates == 2
        assert report.skipped_non_section == 1  # front matter, logged in report

    def test_below_cosine_threshold_is_visible_not_dropped(self):
        """The known-relevant-chunk-below-threshold lane: the row EXISTS and
        says excluded; it does not vanish."""
        chunks = tiny_chunks()
        gated, _ = gate_document(chunks, pillars=PILLARS_67, embed_fn=fake_embed_factory(set()))  # nothing hot
        row = find(gated, "6.1", "s. 1")
        assert row.gate_decision == "excluded"
        assert row.cosine_pillar <= 0.01

    def test_passed_requires_both_tiers(self):
        chunks = tiny_chunks()
        gated, _ = gate_document(chunks, pillars=PILLARS_67, embed_fn=fake_embed_factory({"transfer of personal data", "tribunal"}))
        # s. 1 talks about cross-border transfer -> bm25 6.1 hit + hot cosine
        assert find(gated, "6.1", "s. 1").gate_decision == "passed"
        # s. 2 is hot on cosine but has no 6.1 vocabulary -> excluded by tier 2
        assert find(gated, "6.1", "s. 2").gate_decision == "excluded"

    def test_scores_recorded_on_every_row(self):
        chunks = tiny_chunks()
        gated, _ = gate_document(chunks, pillars=PILLARS_67, embed_fn=fake_embed_factory({"transfer of personal data"}))
        for g in gated:
            assert -1.0 <= g.cosine_pillar <= 1.0
            assert g.bm25_indicator >= 0.0

    def test_deterministic(self):
        chunks = tiny_chunks()
        fn = fake_embed_factory({"transfer of personal data"})
        a, _ = gate_document(chunks, pillars=PILLARS_67, embed_fn=fn)
        b, _ = gate_document(chunks, pillars=PILLARS_67, embed_fn=fn)
        assert [g.model_dump() for g in a] == [g.model_dump() for g in b]


class TestOllamaDownLane:
    def test_clear_failure_no_partial_results(self, monkeypatch):
        """Embedding service down -> one actionable error, nothing partial."""
        monkeypatch.setenv("OLLAMA_HOST", "http://localhost:1")  # dead port
        with pytest.raises(RuntimeError, match="ollama"):
            embed_ollama(["some text"])

    def test_gate_document_propagates_the_failure(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_HOST", "http://localhost:1")
        with pytest.raises(RuntimeError, match="ollama"):
            gate_document(tiny_chunks(), pillars=PILLARS_67)


class TestConfigDefaults:
    def test_gate_knobs(self):
        cfg = PipelineConfig()
        assert 0.0 < cfg.gate_pillar_cosine_min < 1.0
        assert cfg.gate_bm25_top_k >= 10


# ---------------------------------------------------------------------------
# the meaning-only lane (non-English Documents): cosine alone, same top-k cap
# ---------------------------------------------------------------------------


def lao_chunks(n: int = 30) -> list[Chunk]:
    """n section chunks of real Lao statute text. The keyword tier can produce
    no token at all from these, which is the whole reason the lane exists."""
    body = (
        "ໃນການເຄື່ອນໄຫວວຽກງານທຸລະກໍາທາງເອເລັກໂຕຣນິກ ໃຫ້ປະຕິບັດຕາມຫຼັກການ ດັ່ງນີ້: "
        "ຄວາມສະຫມັກໃຈ, ຄວາມສະເຫມີພາບ, ຄວາມເປັນອິດສະລະຂອງຄູ່ຮ່ວມທຸລະກໍາ, "
        "ປົກປ້ອງສິດ ແລະ ຜົນປະໂຫຍດອັນຊອບທໍາຂອງຄູ່ຮ່ວມທຸລະກໍາທາງເອເລັກໂຕຣນິກ."
    )
    chunks, off = [], 0
    for i in range(n):
        text = f"ມາດຕາ {i + 1} {body}"
        chunks.append(Chunk(
            chunk_id=f"doc_la_tiny:c{i:04d}", document_id="doc_la_tiny",
            char_start=off, char_end=off + len(text), text=text,
            section_label=f"ມາດຕາ {i + 1}", chunk_kind="section",
        ))
        off += len(text)
    return chunks


def bm25_spy(monkeypatch):
    """Make any use of the keyword tier an outright failure, so 'not consulted'
    is proved rather than inferred from the scores."""
    import bm25s

    def boom(*args, **kwargs):
        raise AssertionError("the bm25 keyword tier was built on a non-English Document")

    monkeypatch.setattr(bm25s, "BM25", boom)


class TestMeaningOnlyLane:
    def test_the_keyword_tier_is_never_built(self, monkeypatch):
        bm25_spy(monkeypatch)
        gated, report = gate_document(
            lao_chunks(), pillars=PILLARS_67,
            embed_fn=fake_embed_factory({"ມາດຕາ"}), keyword_tier=False,
        )
        assert gated
        assert report.gate_mode == "meaning_only"
        assert any("keyword tier skipped" in n for n in report.notes)

    def test_results_are_not_empty(self, monkeypatch):
        """The point of the lane: the same chunks under the two-tier rule pass
        NOTHING, because the English vocabulary cannot score Lao text."""
        bm25_spy(monkeypatch)
        gated, _ = gate_document(
            lao_chunks(), pillars=PILLARS_67,
            embed_fn=fake_embed_factory({"ມາດຕາ"}), keyword_tier=False,
        )
        assert [g for g in gated if g.gate_decision == "passed"]

    def test_the_two_tier_rule_cannot_run_on_this_corpus(self):
        """Why the flag exists at all. legal_tokens returns [] for every Lao
        chunk, and bm25s.index over all-empty token lists raises inside the
        library (observed 10 Jul 2026, recorded in scripts/m3_demo.py). Even
        without the crash the AND merge would exclude every chunk."""
        assert all(legal_tokens(c.text) == [] for c in lao_chunks())
        with pytest.raises(ValueError):
            gate_document(
                lao_chunks(), pillars=PILLARS_67, embed_fn=fake_embed_factory({"ມາດຕາ"}),
            )

    def test_bm25_column_is_zero_never_none(self, monkeypatch):
        bm25_spy(monkeypatch)
        gated, _ = gate_document(
            lao_chunks(), pillars=PILLARS_67,
            embed_fn=fake_embed_factory({"ມາດຕາ"}), keyword_tier=False,
        )
        assert all(g.bm25_indicator == 0.0 for g in gated)

    def test_the_top_k_cap_still_bounds_the_engine_calls(self, monkeypatch):
        """Every chunk clears the cosine floor here, so without a cap all 30
        would pass every Indicator and the Engine bill would follow."""
        bm25_spy(monkeypatch)
        cfg = PipelineConfig()
        gated, _ = gate_document(
            lao_chunks(30), config=cfg, pillars=PILLARS_67,
            embed_fn=fake_embed_factory({"ມາດຕາ"}), keyword_tier=False,
        )
        per_ind: dict[str, int] = {}
        for g in gated:
            if g.gate_decision == "passed":
                per_ind[g.indicator_id] = per_ind.get(g.indicator_id, 0) + 1
        assert per_ind
        assert all(n <= cfg.gate_bm25_top_k for n in per_ind.values()), per_ind

    def test_the_cosine_floor_still_excludes(self, monkeypatch):
        bm25_spy(monkeypatch)
        gated, _ = gate_document(
            lao_chunks(), pillars=PILLARS_67,
            embed_fn=fake_embed_factory(set()), keyword_tier=False,  # nothing hot
        )
        assert gated
        assert all(g.gate_decision == "excluded" for g in gated)

    def test_every_pair_is_still_logged(self, monkeypatch):
        bm25_spy(monkeypatch)
        gated, report = gate_document(
            lao_chunks(5), pillars=PILLARS_67,
            embed_fn=fake_embed_factory({"ມາດຕາ"}), keyword_tier=False,
        )
        assert len(gated) == 5 * 9
        assert report.n_candidates == 5

    def test_deterministic(self, monkeypatch):
        bm25_spy(monkeypatch)
        fn = fake_embed_factory({"ມາດຕາ"})
        a, _ = gate_document(lao_chunks(), pillars=PILLARS_67, embed_fn=fn, keyword_tier=False)
        b, _ = gate_document(lao_chunks(), pillars=PILLARS_67, embed_fn=fn, keyword_tier=False)
        assert [g.model_dump() for g in a] == [g.model_dump() for g in b]

    def test_the_english_path_is_untouched(self):
        """Same call as TestDecisionLogic makes, with the default flag: the
        keyword tier still excludes a chunk the cosine tier liked."""
        gated, report = gate_document(
            tiny_chunks(), pillars=PILLARS_67,
            embed_fn=fake_embed_factory({"transfer of personal data", "tribunal"}),
        )
        assert report.gate_mode == "two_tier"
        assert report.notes == []
        assert find(gated, "6.1", "s. 1").gate_decision == "passed"
        assert find(gated, "6.1", "s. 2").gate_decision == "excluded"


# ---------------------------------------------------------------------------
# live lanes: real BGE-M3 embeddings over the real golden M4 chunks
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def my_gated(my_chunks):
    return gate_document(my_chunks, pillars=PILLARS_67)


@needs_ollama
class TestRecallMalaysia:

    def test_ground_truth_sections_survive(self, my_gated):
        rows, _ = my_gated
        for indicator, labels in GROUND_TRUTH["my"].items():
            surv = survivors(rows, indicator)
            for want in labels:
                assert any(lab == want or lab.endswith(want) for lab in surv), (
                    f"ESCAP-cited {want} did not survive the {indicator} gate; "
                    f"survivors: {sorted(surv)}"
                )

    def test_reduction_ratio_in_band(self, my_gated):
        rows, report = my_gated
        cfg = PipelineConfig()
        per_ind = {}
        for g in rows:
            if g.gate_decision == "passed":
                per_ind[g.indicator_id] = per_ind.get(g.indicator_id, 0) + 1
        assert all(n <= cfg.gate_bm25_top_k for n in per_ind.values())
        assert report.n_candidates == 146
        assert 0 < report.reduction_ratio <= 0.25, f"reduction {report.reduction_ratio}"

    def test_exclusions_carry_scores(self, my_gated):
        rows, _ = my_gated
        excluded = [g for g in rows if g.gate_decision == "excluded"]
        assert excluded
        assert all(g.bm25_indicator >= 0 for g in excluded)


@needs_ollama
class TestRecallSingapore:
    def test_licensing_section_survives_7_3(self, sg_chunks):
        gated, _ = gate_document(sg_chunks, pillars=PILLARS_67)
        row = find(gated, "7.3", "s. 5")
        assert row.gate_decision == "passed", (
            "ESCAP cites TA s. 5 (licence under which retention conditions are "
            "imposed) for 7.3; it must survive the gate"
        )


@needs_ollama
class TestAustraliaReduction:
    def test_gate_scales_and_logs_on_536_sections(self, au_chunks):
        gated, report = gate_document(au_chunks, pillars=PILLARS_67)
        cfg = PipelineConfig()
        assert report.n_candidates == 536
        assert len(gated) == 536 * 9
        per_ind = {}
        for g in gated:
            if g.gate_decision == "passed":
                per_ind[g.indicator_id] = per_ind.get(g.indicator_id, 0) + 1
        assert all(n <= cfg.gate_bm25_top_k for n in per_ind.values())
        # a criminal code is mostly out-of-domain: strong reduction expected
        assert report.reduction_ratio < 0.05


@needs_ollama
class TestEmbeddingPersistence:
    def test_float32_blobs_roundtrip_and_search(self, my_chunks, tmp_path):
        db = tmp_path / "gate.sqlite"
        storage = Storage(db)
        storage.apply_schema()
        storage.upsert_document(
            my_chunks[0].document_id, economy="MY", source_sha256="0" * 64
        )
        storage.upsert_chunks(my_chunks)
        gated, report = gate_document(my_chunks, pillars=PILLARS_67, storage=storage)
        ids, mat = storage.load_embeddings()
        assert mat.dtype == np.float32
        assert mat.shape == (report.n_candidates, report.embed_dim)
        index = VectorIndex(ids, mat)
        # the cross-border section must be its own nearest neighbour
        target = next(c for c in my_chunks if c.section_label.endswith("s. 129"))
        row = ids.index(target.chunk_id)
        got = index.search(mat[row], top_k=1)
        assert got[0][0] == target.chunk_id


@needs_ollama
class TestLiveDeterminism:
    def test_two_runs_same_decisions(self, sg_chunks):
        a, _ = gate_document(sg_chunks, pillars=PILLARS_67)
        b, _ = gate_document(sg_chunks, pillars=PILLARS_67)
        assert [(g.chunk.chunk_id, g.indicator_id, g.gate_decision) for g in a] == [
            (g.chunk.chunk_id, g.indicator_id, g.gate_decision) for g in b
        ]


class TestVocabAndPillarLoaders:
    def test_the_runs_pillars_decide_which_vocabularies_load(self):
        """The loaders take the Run's Pillars: a one-Pillar Run must not read
        twelve vocabularies, and the Indicator set comes from the registry."""
        vocab = load_indicator_vocab(PILLARS_67)
        assert set(vocab) == set(indicators_for(PILLARS_67))
        assert set(vocab) == {"6.1", "6.2", "6.3", "6.4", "7.1", "7.2", "7.3", "7.4", "7.5"}
        assert all(v for v in vocab.values())
        assert set(load_indicator_vocab((12,))) == set(indicators_for((12,)))

    def test_every_configured_pillar_has_a_vocabulary_and_a_description(self):
        vocab = load_indicator_vocab()
        assert set(vocab) == set(indicators_for())
        texts = load_pillar_texts()
        assert set(texts) == set(configured_pillars())
        assert all(len(t) > 40 for t in texts.values())

    def test_pillar_texts(self):
        texts = load_pillar_texts(PILLARS_67)
        assert set(texts) == {6, 7}
        assert all(len(t) > 40 for t in texts.values())
