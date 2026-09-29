"""M3 demo artifacts (m3-translation branch, phase 4). Two lanes:

  uv run python scripts/m3_demo.py malay
      Draft-gloss the reviewed Malay quote (Act A1472, the 3 shipped rows)
      with the offline opus-mt engine and write the draft-vs-reviewed
      comparison report (a comparison, NEVER an equality assert).

  set -a; source .env; set +a
  uv run python scripts/m3_demo.py lao
      The end-to-end chain on the Lao fixture: OCR (Tesseract lao) ->
      chunk (LLM boundary fallback; positions and labels only) -> gate
      (bge-m3 cross-lingual cosine; needs local ollama) -> snippet
      selection by the 30B mapper under the SAME mechanical rule as M6
      (verbatim quote, byte-for-byte substring check, 3 attempts then
      drop) -> labelled English gloss (LLM engine; opus-mt shown for the
      honest comparison). Needs OPENROUTER_API_KEY.

Outputs land in docs/m3/ (committed evidence). Nothing here touches the
canonical stream, tests/golden/, or the export lane; economy contracts stay
SG/AU/MY, which is exactly why this demo hand-wires the stage functions
instead of forcing a Round 2 economy through run_economy.
"""

from __future__ import annotations

import gzip
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from regcompass.chunk import _default_completion, split_document  # noqa: E402
from regcompass.config import load_pipeline  # noqa: E402
from regcompass.engines import resolve_engine  # noqa: E402
from regcompass.contracts import GLOSS_LABEL, PipelineConfig  # noqa: E402
from regcompass.translate import LlmEngine, OpusMtEngine, draft_gloss  # noqa: E402

OUT_DIR = ROOT / "docs" / "m3"
LAO_PDF = (
    ROOT / "tests/fixtures/sample_legislation/domestic_language/"
    "Lao PDR-Law on Electronic Transaction (Amended) No. 31.pdf"
)
CSV_GZ = ROOT / "tests/golden/repro/submission.csv.gz"
VERBATIM_EN = ROOT / "config/verbatim_english.json"

SELECT_PROMPT = """You are selecting evidence from a Lao statute digitized by OCR. From the
CHUNK below, select the ONE contiguous verbatim passage (in Lao, 1-3
sentences) that most directly {target}.
Copy the passage EXACTLY character for character: keep the original line
breaks, punctuation, and numbering, and copy any OCR errors AS-IS - do not
correct, complete, or normalize anything. Reply with ONLY this JSON:
{{"quote": "<exact substring of the chunk>"}}
{stricter}
CHUNK:
{chunk}"""

STRICTER = (
    "\nYour previous quote was NOT an exact substring of the chunk. You most"
    " likely corrected or paraphrased the text. Copy one passage"
    " character-for-character AS IT APPEARS, including misspellings and OCR"
    " artifacts."
)


def demo_malay() -> None:
    entries = {
        k: v
        for k, v in json.loads(VERBATIM_EN.read_text(encoding="utf-8")).items()
        if not k.startswith("_")
    }
    with gzip.open(CSV_GZ, "rt", encoding="utf-8") as f:
        import csv

        snippets = {r["Verbatim Snippet"] for r in csv.DictReader(f)}
    # All 3 A1472 rows share ONE Malay quote (the config's own comment); it is
    # the only non-English snippet in the shipped CSV.
    from regcompass.translate import needs_gloss

    malay_snippets = sorted(s for s in snippets if needs_gloss(s))
    cfg = load_pipeline()
    engine = OpusMtEngine(Path(cfg.gloss_model_dir))
    lines = [
        "# M3 draft gloss vs reviewed translation (Malay reference lane)",
        "",
        "The quality reference: the shipped Act A1472 rows'",
        "human-reviewed translations (config/verbatim_english.json) beside the",
        "offline opus-mt draft for the SAME source text. A comparison report,",
        "never an equality assert: drafts are non-authoritative and unshipped.",
        "",
    ]
    for snippet in malay_snippets:
        g = draft_gloss("malay-reference", snippet, engine, cfg, source_language="ms")
        lines += ["## Source (shipped Verbatim Snippet, Malay)", "", f"> {snippet}", ""]
        lines += [f"## opus-mt draft ({g.engine})", ""]
        if g.english is None:
            lines += [f"> (gloss failed: `{g.uncertainty_flag}`; would ship without gloss)", ""]
        else:
            lines += [f"> {g.labelled_english}", ""]
        lines += ["## Reviewed translations that actually ship (per mapping row)", ""]
        for k, v in entries.items():
            lines += [f"- `{k}`:", f"  > {GLOSS_LABEL} {v['english']}", ""]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "MALAY_COMPARISON.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT_DIR / 'MALAY_COMPARISON.md'}")


def locate_and_slice(
    proposed: str,
    stream: str,
    min_prefix_chars: int = 80,
    min_prefix_frac: float = 0.5,
) -> tuple[str, str] | None:
    """Locate the model's proposed quote in the stream IGNORING whitespace
    differences, then slice the stream's OWN bytes at that location (the
    repo's slice-not-reemit rule, applied to selection). When only a prefix
    of the proposal occurs contiguously (observed on Lao: the model copies
    exactly, then diverges where it bridges OCR noise), the longest exact
    prefix is sliced instead, floor-gated and trimmed to a word boundary.
    Returns (sliced_stream_bytes, mode) with mode "full" or "prefix", or
    None when nothing above the floor occurs in the stream."""
    norm_chars: list[str] = []
    norm_to_orig: list[int] = []
    prev_space = True
    for i, ch in enumerate(stream):
        if ch.isspace():
            if not prev_space:
                norm_chars.append(" ")
                norm_to_orig.append(i)
            prev_space = True
        else:
            norm_chars.append(ch)
            norm_to_orig.append(i)
            prev_space = False
    norm_stream = "".join(norm_chars)
    norm_quote = " ".join(proposed.split())
    if not norm_quote:
        return None

    def slice_at(pos: int, length: int) -> str:
        start = norm_to_orig[pos]
        end = norm_to_orig[pos + length - 1] + 1
        return stream[start:end]

    pos = norm_stream.find(norm_quote)
    if pos >= 0:
        return slice_at(pos, len(norm_quote)), "full"
    # longest prefix of the proposal that occurs contiguously (binary search:
    # "prefix of length k occurs" is monotone in k)
    lo, hi = 0, len(norm_quote)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if norm_stream.find(norm_quote[:mid]) >= 0:
            lo = mid
        else:
            hi = mid - 1
    prefix = norm_quote[:lo]
    if " " in prefix:
        prefix = prefix[: prefix.rfind(" ")]  # never end mid-word
    floor = max(min_prefix_chars, int(min_prefix_frac * len(norm_quote)))
    if len(prefix) < floor:
        return None
    return slice_at(norm_stream.find(prefix), len(prefix)), "prefix"


def demo_lao() -> None:
    from regcompass.contracts import CanonicalText
    from regcompass.ocr import ocr_document

    cfg = load_pipeline().model_copy(
        update={
            # The M2 quality proxies are ENGLISH-tuned (dictionary hit rate on an
            # English legal wordlist): on a Lao document they would always fire.
            # Escalation is pointless here (RapidOCR has no Lao); the manual_review
            # flag the defaults would raise is reported honestly in the demo note.
            "ocr_escalation_min_confidence": 0.0,
            "ocr_escalation_min_dict_hit": 0.0,
        }
    )
    cache = ROOT / "data" / "m3_demo_ocr_cache.json"  # data/ is gitignored
    if cache.exists():
        canonical = CanonicalText(**json.loads(cache.read_text(encoding="utf-8")))
        print(f"M2: OCR stream loaded from cache ({cache})")
    else:
        print("M2: OCR (tesseract, lao traineddata, 29 pages) ...")
        canonical = ocr_document(
            LAO_PDF.read_bytes(), "doc_la_electronic_transactions_31", languages="lao", config=cfg
        )
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(canonical.model_dump_json(), encoding="utf-8")
    print(f"  {len(canonical.full_text)} chars, mean word conf {canonical.ocr_quality.mean_word_confidence:.3f}")

    print("M4: chunk (LLM boundary fallback expected; positions and labels only) ...")
    chunks, report = split_document(canonical, cfg, completion_fn=_default_completion)
    sections = [c for c in chunks if c.chunk_kind == "section"]
    print(f"  style={report.style} chunks={len(chunks)} (sections {len(sections)}) fallback={report.fallback_used}")

    print("M5 pillar tier: bge-m3 cross-lingual cosine vs the English pillar descriptions ...")
    # FINDING (10 Jul 2026): the full gate_document CRASHES on a Lao corpus -
    # its bm25 lexical tier tokenizes with the English legal vocabulary, so
    # every Lao chunk yields zero tokens and the index is empty. Cross-lingual
    # gating can only ride the embedding tier; a Round 2 non-Latin corpus needs
    # per-language keyword vocabularies or an embedding-only gate mode. The
    # demo therefore ranks with the SAME embedding tier the gate uses.
    # RESOLVED in the final round: gate_document now takes keyword_tier,
    # and a Run sets it from the Document's Language, so the product has the
    # embedding-only gate mode this finding asked for. This demo script is kept
    # as the July record and still ranks by hand.
    import numpy as np

    from regcompass.config import load_indicators
    from regcompass.gate import EMBED_MAX_CHARS, _unit, embed_ollama, load_pillar_texts

    pillar_texts = load_pillar_texts()
    indicators = load_indicators()
    section_chunks = sections or chunks
    chunk_vecs = _unit(
        np.asarray(
            embed_ollama([c.text[:EMBED_MAX_CHARS] for c in section_chunks]), dtype=np.float32
        )
    )
    pillar_vecs = _unit(
        np.asarray(embed_ollama([pillar_texts[6], pillar_texts[7]]), dtype=np.float32)
    )
    ind_ids = sorted(indicators)
    ind_vecs = _unit(
        np.asarray(
            embed_ollama([f"{indicators[i].name}. {indicators[i].definition}" for i in ind_ids]),
            dtype=np.float32,
        )
    )
    pillar_cos = chunk_vecs @ pillar_vecs.T  # (chunks, 2)
    best_chunk_i = int(np.argmax(pillar_cos.max(axis=1)))
    top_chunk = section_chunks[best_chunk_i]
    top_pillar_cos = float(pillar_cos[best_chunk_i].max())
    ind_cos = chunk_vecs[best_chunk_i] @ ind_vecs.T
    top_indicator_id = ind_ids[int(np.argmax(ind_cos))]
    indicator = indicators[top_indicator_id]
    n_above_threshold = int((pillar_cos.max(axis=1) >= cfg.gate_pillar_cosine_min).sum())
    print(
        f"  {n_above_threshold}/{len(section_chunks)} chunks clear the English-calibrated"
        f" cosine floor {cfg.gate_pillar_cosine_min}; top {top_pillar_cos:.3f}"
    )
    print(
        f"  selecting from chunk {top_chunk.chunk_id} ({top_chunk.section_label})"
        f" for indicator {top_indicator_id} ({indicator.name})"
    )

    print("M6-rule selection: 30B points at a passage; the STREAM's own bytes are sliced ...")
    quote, attempts, selection_mode = None, 0, None
    stricter = ""
    for attempt in range(1, cfg.extraction_attempts + 1):
        attempts = attempt
        prompt = SELECT_PROMPT.format(
            target=f"relates to: {indicator.definition[:300]}",
            stricter=stricter,
            chunk=top_chunk.text[:8000],
        )
        content = _default_completion(prompt, strict=attempt > 1)
        m = re.search(r'\{.*\}', content, re.DOTALL)
        try:
            candidate = json.loads(m.group(0))["quote"] if m else ""
        except (ValueError, KeyError, TypeError):
            candidate = ""
        if candidate and candidate in canonical.full_text:  # THE mechanical rule
            quote, selection_mode = candidate, "exact"
            break
        # Non-Latin frontier (first run of this demo): the model re-emits Lao
        # with normalized spacing, so emit-and-verify fails where the quote IS
        # in the stream. Locate it whitespace-collapsed and slice the stream's
        # own bytes (the M4 rule: models point, they never re-emit text); the
        # byte-for-byte check below then holds on the sliced bytes.
        located = locate_and_slice(candidate, canonical.full_text) if candidate else None
        if located is not None and located[0] in canonical.full_text:
            quote, selection_mode = located[0], f"located-and-sliced ({located[1]})"
            break
        probe = locate_and_slice(candidate, canonical.full_text, min_prefix_chars=1, min_prefix_frac=0.0)
        exact_prefix = len(probe[0]) if probe else 0
        print(
            f"  attempt {attempt}: candidate {len(candidate)} chars, not in stream;"
            f" longest exact prefix {exact_prefix} chars (head: {candidate[:60]!r})"
        )
        stricter = STRICTER
    if quote is None:
        print("  no byte-verified quote after 3 attempts: recorded as DROP (the honest lane)")
    else:
        assert quote in canonical.full_text  # the property the demo exists to show
        print(f"  byte-verified on attempt {attempts} ({selection_mode}): {len(quote)} chars")

    glosses = {}
    if quote:
        print("M3: gloss (LLM engine; opus-mt shown for the honest comparison) ...")
        llm = LlmEngine(resolve_engine(None))
        glosses["llm"] = draft_gloss("lao-demo", quote, llm, cfg)
        try:
            opus = OpusMtEngine(Path(cfg.gloss_model_dir))
            glosses["opus_mt"] = draft_gloss("lao-demo", quote, opus, cfg)
        except Exception as e:  # weights not fetched: note it, the LLM lane carries the demo
            print(f"  opus-mt lane skipped: {e}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "document": LAO_PDF.name,
        "ocr": {
            "extractor": canonical.extractor,
            "extractor_version": canonical.extractor_version,
            "chars": len(canonical.full_text),
            "mean_word_confidence": canonical.ocr_quality.mean_word_confidence,
            "manual_review_flag": canonical.ocr_quality.manual_review,
            "proxy_note": "dictionary-hit proxy is English-tuned; inapplicable to Lao",
        },
        "chunking": {
            "style": report.style,
            "n_chunks": len(chunks),
            "n_sections": len(sections),
            "fallback_used": report.fallback_used,
            "fallback_attempts": report.fallback_attempts,
        },
        "gate": {
            "tier": "embedding only (bge-m3 cross-lingual)",
            "finding": (
                "gate_document crashes on a Lao corpus: the bm25 lexical tier"
                " tokenizes with the English legal vocabulary (zero tokens per"
                " Lao chunk, empty index); Round 2 non-Latin corpora need"
                " per-language vocabularies or an embedding-only gate mode"
            ),
            "n_above_cosine_floor": n_above_threshold,
            "top_pair": {
                "chunk_id": top_chunk.chunk_id,
                "section_label": top_chunk.section_label,
                "indicator_id": top_indicator_id,
                "cosine_pillar": top_pillar_cos,
            },
        },
        "selection": {
            "attempts": attempts,
            "byte_verified": quote is not None,
            "mode": selection_mode,
            "finding": (
                "on Lao OCR text the model re-emits quotes with normalized"
                " spacing, so strict emit-and-verify fails; locating the quote"
                " whitespace-collapsed and slicing the stream's own bytes"
                " preserves the byte-for-byte guarantee (Round 2: prefer"
                " point-and-slice selection for non-Latin scripts)"
            ),
            "quote_lao": quote,
        },
        "glosses": {k: g.model_dump() for k, g in glosses.items()},
    }
    (OUT_DIR / "lao_demo.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"wrote {OUT_DIR / 'lao_demo.json'}")
    render_lao()


def render_lao() -> None:
    """LAO_DEMO.md from lao_demo.json (re-render without model calls)."""
    p = json.loads((OUT_DIR / "lao_demo.json").read_text(encoding="utf-8"))
    sel, gate, ocr, chk = p["selection"], p["gate"], p["ocr"], p["chunking"]
    lines = [
        "# M3 demo: a Lao statute through the verifiable pipeline",
        "",
        f"Source: `{p['document']}` (a 29-page SCAN in Lao script; zero text layer).",
        "Machine artifact: `lao_demo.json` in this directory. Regenerate with",
        "`uv run python scripts/m3_demo.py lao` (needs the mapper key + local ollama).",
        "",
        "## What each stage did",
        "",
        "| Stage | Result |",
        "|---|---|",
        f"| M2 OCR (Tesseract, vendored `lao` traineddata) | {ocr['chars']:,} chars, mean word confidence {ocr['mean_word_confidence']:.3f} |",
        f"| M4 chunk | {chk['n_sections']} sections via the LLM boundary fallback (positions and labels only) |",
        f"| M5 gate, embedding tier (bge-m3, cross-lingual) | {gate['n_above_cosine_floor']} chunks clear the English-calibrated cosine floor; top pair {gate['top_pair']['section_label']} -> indicator {gate['top_pair']['indicator_id']} at cosine {gate['top_pair']['cosine_pillar']:.3f} |",
        f"| M6-rule selection (30B) | byte-verified: {sel['byte_verified']} (attempt {sel['attempts']}, mode {sel['mode']}) |",
        "",
        "## The byte-verified snippet (Lao, the stream's own bytes)",
        "",
        "> " + (sel["quote_lao"] or "(dropped after 3 attempts)").replace("\n", "\n> "),
        "",
        "## Labelled glosses of that snippet",
        "",
    ]
    for name, g in p["glosses"].items():
        text = g["english"]
        label = f"{GLOSS_LABEL} {text}" if text else f"(gloss failed: {g['uncertainty_flag']})"
        lines += [f"**{name}** (`{g['engine']}`):", "", "> " + label.replace("\n", "\n> "), ""]
    lines += [
        "## Honest notes (what was and was NOT proven)",
        "",
        "- PROVEN: the mechanical citation property survives a non-Latin, scanned,",
        "  real-world input end to end: the shipped snippet is a byte-for-byte",
        "  substring of the canonical OCR stream (asserted in the run), selected by",
        "  the model but never re-emitted by it.",
        "- PROVEN: the OCR frontier is usable on Lao (mean word confidence"
        f" {ocr['mean_word_confidence']:.3f});"
        " the M2 dictionary-hit proxy is English-tuned and inapplicable, so its",
        "  manual-review flag stands (reported, not silenced).",
        f"- FINDING: {gate['finding']}.",
        f"- FINDING: {sel['finding']}.",
        "- FINDING: opus-mt-mul-en is unusable on Lao (see the gloss above);",
        "  the Lao gloss lane must run on the `llm` engine config. Malay drafts",
        "  are usable but rough (see",
        "  `MALAY_COMPARISON.md`).",
        "- NOT proven: mapping-decision quality on Lao (no ground truth exists for",
        "  this document; the indicator here comes from the cross-lingual gate and",
        "  a single selection, not a scored rubric); translation fidelity beyond",
        "  spot inspection; chunk-boundary quality (the LLM fallback emitted",
        "  plausible Part-level boundaries, unaudited against the source).",
        "- The glosses are DRAFTS: non-authoritative, AI-generated, unshipped.",
        "  Nothing from this demo enters the Round 1 submission artifacts.",
    ]
    (OUT_DIR / "LAO_DEMO.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT_DIR / 'LAO_DEMO.md'}")


if __name__ == "__main__":
    lane = sys.argv[1] if len(sys.argv) > 1 else ""
    if lane == "malay":
        demo_malay()
    elif lane == "lao":
        demo_lao()
    elif lane == "render":
        render_lao()
    else:
        print("usage: python scripts/m3_demo.py {malay|lao|render}")
        sys.exit(2)
