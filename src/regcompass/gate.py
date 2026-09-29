"""M5 - the two-tier relevance gate between chunks and the mapper LLM.

Tier 1 (semantic): BGE-M3 embeddings via the local Ollama server; a chunk must
clear a cosine floor against its pillar's description. Tier 2 (lexical): bm25s
over a legal tokenizer that preserves statutory tokens ("s 15(2)", "Part IVA",
"Cap. 88") as single tokens; the chunk must rank in the indicator's top-k with
a positive score. Only chunks passing BOTH tiers reach the mapper.

Every (section chunk x indicator) pair is emitted as a GatedChunk row, passed
or excluded, so exclusions are always visible (multi-row indicators need every
contributing act; silent truncation is forbidden). Non-section chunks are not
candidates and are counted in the report.

The embedding service being down is a CLEAR failure (actionable RuntimeError),
never a partial result. Embeddings are persisted as float32 BLOBs when a
Storage is supplied (chunk rows must exist; store_embedding raises otherwise).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from .config import (
    CONFIG_DIR,
    indicator_ids,
    load_keywords,
    load_pillar_description,
    load_pillars,
)
from .contracts import Chunk, GatedChunk, PipelineConfig
from .storage import Storage

EmbedFn = Callable[[list[str]], np.ndarray]

EMBED_BATCH = 32
EMBED_MAX_CHARS = 4000  # deterministic embedding-input truncation; bm25 sees full text

# The two shortlisting rules, named on the GateReport and the m5_gate audit row.
TWO_TIER = "two_tier"
MEANING_ONLY = "meaning_only"
MEANING_ONLY_NOTE = (
    "keyword tier skipped: legal_tokens and its stopword list are English and"
    " the Indicator vocabularies are English phrases, so on this Document the"
    " bm25 tier can only score zero and the AND merge would exclude every"
    " chunk; the cosine ranking is capped at the same gate_bm25_top_k, so the"
    " Engine call budget is unchanged"
)


def configured_pillars(config_dir=CONFIG_DIR) -> tuple[int, ...]:
    return tuple(sorted(load_pillars(config_dir)))


def indicators_for(
    pillars: tuple[int, ...] | None = None, config_dir=CONFIG_DIR
) -> tuple[str, ...]:
    """The Indicators this Run gates against: those of the Run's Pillars that a
    statute can actually answer. None means every configured Pillar."""
    return indicator_ids(pillars, config_dir)


def load_indicator_vocab(
    pillars: tuple[int, ...] | None = None, config_dir=CONFIG_DIR
) -> dict[str, list[str]]:
    """The keyword vocabularies for the Run's Pillars only. Loading all 12 when
    a Run asked for one is waste, and a Pillar whose file is missing must fail
    loudly at the Gate rather than silently gate nothing."""
    vocab: dict[str, list[str]] = {}
    for pillar in pillars if pillars is not None else configured_pillars(config_dir):
        vocab.update(load_keywords(pillar, config_dir))
    return vocab


def load_pillar_texts(
    pillars: tuple[int, ...] | None = None, config_dir=CONFIG_DIR
) -> dict[int, str]:
    return {
        p: load_pillar_description(p, config_dir)
        for p in (pillars if pillars is not None else configured_pillars(config_dir))
    }


# ---------------------------------------------------------------------------
# legal tokenizer
# ---------------------------------------------------------------------------

_SPECIALS = re.compile(
    r"(?:\bs\.?|\bsection)\s+(\d+[a-z]{0,3})\s*(\(\d+[a-z]?\))?"
    r"|\bpart\s+([ivxlc]+[a-z]?|\d+(?:\.\d+)?[a-z]{0,2})\b"
    r"|\bcap\.?\s*(\d+)",
    re.I,
)
_WORD = re.compile(r"[a-z][a-z0-9-]+")


def _stopwords() -> frozenset:
    from bm25s.stopwords import STOPWORDS_EN

    return frozenset(STOPWORDS_EN)


_STOPWORDS = _stopwords()


def legal_tokens(text: str) -> list[str]:
    """Lowercase tokens with statutory references kept as single tokens:
    'Section 45(2)' -> 's45(2)', 'Part IVA' -> 'partiva', 'Cap. 88' -> 'cap88'.
    English stopwords are dropped so shared function words never score."""
    text = text.lower()
    out: list[str] = []

    def _sub(m: re.Match) -> str:
        if m.group(1):
            out.append(f"s{m.group(1)}{m.group(2) or ''}")
        elif m.group(3):
            out.append(f"part{m.group(3)}")
        elif m.group(4):
            out.append(f"cap{m.group(4)}")
        return " "

    rest = _SPECIALS.sub(_sub, text)
    out.extend(w for w in _WORD.findall(rest) if w not in _STOPWORDS)
    return out


# ---------------------------------------------------------------------------
# embeddings via the local Ollama server
# ---------------------------------------------------------------------------


def _ollama_base_url() -> str:
    base = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = f"http://{base}"
    return base


def _embedder_model() -> str:
    from .config import load_models

    litellm_name = load_models().embedder.litellm_model  # e.g. "ollama/bge-m3"
    return litellm_name.split("/", 1)[1] if "/" in litellm_name else litellm_name


def embed_ollama(texts: list[str], model: str | None = None, timeout: float = 300.0) -> np.ndarray:
    """Embed texts with the local Ollama server. Batched; any failure raises
    (a partial result is never returned)."""
    import httpx

    base = _ollama_base_url()
    model = model or _embedder_model()
    rows: list[np.ndarray] = []
    try:
        with httpx.Client(timeout=timeout) as client:
            for i in range(0, len(texts), EMBED_BATCH):
                batch = texts[i : i + EMBED_BATCH]
                resp = client.post(f"{base}/api/embed", json={"model": model, "input": batch})
                if resp.status_code != 200:
                    raise RuntimeError(
                        f"ollama embed failed (HTTP {resp.status_code}) at {base}: "
                        f"{resp.text[:300]}"
                    )
                embs = resp.json().get("embeddings")
                if not embs or len(embs) != len(batch):
                    raise RuntimeError(
                        f"ollama returned {len(embs or [])} embeddings for a "
                        f"batch of {len(batch)} at {base}"
                    )
                rows.append(np.asarray(embs, dtype=np.float32))
    except httpx.TransportError as e:
        raise RuntimeError(
            f"cannot reach the ollama server at {base}: is it running? "
            "Start it with `ollama serve` and make sure the embedding model is "
            "present (`ollama pull bge-m3`). No partial results were produced."
        ) from e
    return np.vstack(rows) if rows else np.empty((0, 0), dtype=np.float32)


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------


@dataclass
class GateReport:
    """What the gate did; the GatedChunk rows themselves are the exclusion log."""

    n_candidates: int = 0
    skipped_non_section: int = 0
    embed_dim: int = 0
    reduction_ratio: float = 0.0
    passed_per_indicator: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    # "two_tier" (cosine AND bm25) or "meaning_only" (cosine, capped by the same
    # top-k). The caller logs it on the m5_gate audit row, so a reviewer can see
    # from the record which rule shortlisted a Document.
    gate_mode: str = TWO_TIER


def _unit(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.clip(norms, 1e-12, None)


def gate_document(
    chunks: list[Chunk],
    config: PipelineConfig | None = None,
    embed_fn: EmbedFn | None = None,
    storage: Storage | None = None,
    config_dir=CONFIG_DIR,
    pillars: tuple[int, ...] | None = None,
    indicators: tuple[str, ...] | None = None,
    keyword_tier: bool = True,
) -> tuple[list[GatedChunk], GateReport]:
    """Score every section chunk against every Indicator of the Run's Pillars
    and emit one GatedChunk row per pair (passed or excluded, never dropped).

    pillars=None means every configured Pillar. Passing the Run's Pillars is
    what keeps a one-Pillar Run from embedding twelve Pillar descriptions and
    running twelve times the bm25 queries it needs.

    indicators narrows further, to the exact ids a Run asked for (the live test
    is one Pillar and two Indicators). The ids are checked upstream by
    config.narrow_indicators; here they simply replace the Pillar's full set, so
    no pair outside them is ever scored, emitted, or sent to the mapper.

    keyword_tier=False is the non-English lane (languages.keyword_tier_applies
    decides): the bm25 index is never built, bm25_indicator is written as 0.0 on
    every row, and a chunk passes on cosine alone if it clears
    gate_pillar_cosine_min AND ranks in the Pillar's top gate_bm25_top_k by
    cosine. The cap is what keeps an embedding-only rule from passing a whole
    statute and multiplying the Engine calls."""
    config = config or PipelineConfig()
    embed_fn = embed_fn or embed_ollama
    report = GateReport()

    candidates = [c for c in chunks if c.chunk_kind == "section"]
    report.n_candidates = len(candidates)
    report.skipped_non_section = len(chunks) - len(candidates)
    if not candidates:
        report.notes.append("no section chunks; nothing to gate")
        return [], report

    pillar_texts = load_pillar_texts(pillars, config_dir)
    vocab = load_indicator_vocab(pillars, config_dir)
    if indicators is None:
        indicators = indicators_for(pillars, config_dir)
    else:
        indicators = tuple(indicators)
    if not indicators:
        report.notes.append(f"no legislation-mapped indicators for pillars {pillars}")
        return [], report
    ordered_pillars = sorted(pillar_texts)

    # Tier 1: pillar cosine (embeddings computed once per chunk).
    pillar_vecs = _unit(
        np.asarray(embed_fn([pillar_texts[p] for p in ordered_pillars]), dtype=np.float32)
    )
    chunk_vecs = np.asarray(
        embed_fn([c.text[:EMBED_MAX_CHARS] for c in candidates]), dtype=np.float32
    )
    report.embed_dim = int(chunk_vecs.shape[1])
    unit_vecs = _unit(chunk_vecs)
    cosine = {p: unit_vecs @ pillar_vecs[i] for i, p in enumerate(ordered_pillars)}

    if storage is not None:
        for c, vec in zip(candidates, chunk_vecs):
            storage.store_embedding(c.chunk_id, vec)

    # Tier 2: bm25 score of every chunk for every indicator vocabulary.
    n = len(candidates)
    bm25_scores: dict[str, np.ndarray] = {}
    if keyword_tier:
        import bm25s

        retriever = bm25s.BM25()
        retriever.index([legal_tokens(c.text) for c in candidates], show_progress=False)
        for ind in indicators:
            query = [t for phrase in vocab[ind] for t in legal_tokens(phrase)]
            docs, scores = retriever.retrieve([query], k=n, show_progress=False)
            full = np.zeros(n, dtype=np.float32)
            full[docs[0]] = scores[0]
            bm25_scores[ind] = full
    else:
        report.gate_mode = MEANING_ONLY
        report.notes.append(MEANING_ONLY_NOTE)
        zeros = np.zeros(n, dtype=np.float32)
        bm25_scores = {ind: zeros for ind in indicators}

    gated: list[GatedChunk] = []
    passed_pairs = 0
    for ind in indicators:
        pillar = int(ind.split(".")[0])
        cos = cosine[pillar]
        scores = bm25_scores[ind]
        if keyword_tier:
            # rank cutoff: the k-th highest positive score for this indicator
            order = np.argsort(-scores)
        else:
            # same cutoff, ranked by cosine; stable so equal cosines keep
            # document order and two Runs shortlist the same chunks
            order = np.argsort(-cos, kind="stable")
        top_idx = set(order[: config.gate_bm25_top_k].tolist())
        for i, c in enumerate(candidates):
            passed = (
                float(cos[i]) >= config.gate_pillar_cosine_min
                and i in top_idx
                and (not keyword_tier or float(scores[i]) > 0.0)
            )
            passed_pairs += int(passed)
            gated.append(
                GatedChunk(
                    chunk=c,
                    indicator_id=ind,  # type: ignore[arg-type]
                    cosine_pillar=float(np.clip(cos[i], -1.0, 1.0)),
                    bm25_indicator=float(max(scores[i], 0.0)),
                    gate_decision="passed" if passed else "excluded",
                )
            )
        report.passed_per_indicator[ind] = int(
            sum(1 for g in gated if g.indicator_id == ind and g.gate_decision == "passed")
        )

    report.reduction_ratio = passed_pairs / (n * len(indicators))
    return gated, report
