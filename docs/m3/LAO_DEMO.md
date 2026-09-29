# M3 demo: a Lao statute through the verifiable pipeline

Source: `Lao PDR-Law on Electronic Transaction (Amended) No. 31.pdf` (a 29-page SCAN in Lao script; zero text layer).

> Record of a July 2026 demo, kept as it was written. What it had to do by hand is
> now in the product: a Run picks the tesseract language data and the meaning-only
> Gate from the Document's own Language, and the chunker reads Lao article headings
> deterministically instead of asking a model for boundaries. The lane is tested in
> `tests/test_non_english_lane.py`.

Machine artifact: `lao_demo.json` in this directory. Regenerate with
`uv run python scripts/m3_demo.py lao` (needs the mapper key + local ollama).

## What each stage did

| Stage | Result |
|---|---|
| M2 OCR (Tesseract, vendored `lao` traineddata) | 53,671 chars, mean word confidence 0.841 |
| M4 chunk | 16 sections via the LLM boundary fallback (positions and labels only) |
| M5 gate, embedding tier (bge-m3, cross-lingual) | 13 chunks clear the English-calibrated cosine floor; top pair Part III: Electronic Documents -> indicator 6.2 at cosine 0.590 |
| M6-rule selection (30B) | byte-verified: True (attempt 1, mode exact) |

## The byte-verified snippet (Lao, the stream's own bytes)

> 4. ອາຍຸຂອງການເກັບຮັກສາເອກະສານທາງເອເລັກໂຕຣນິກແຕ່ລະປະເພດ ໃຫ້ປະຕິບັດຕາມກົດບນາຍ
> ແລະ ລະບຽບການທີ່ກ່ຽວຂ້ອງ.
> ມາດຕາ ຂຂ ການນໍາໃຊ້ເອກະສານທາງເອເລັກໂຕຣນິກເປັນຫຼັກຖານ

## Labelled glosses of that snippet

**llm** (`llm:openrouter/qwen/qwen3-30b-a3b-instruct-2507`):

> [AI translation, non-authoritative] 4. The retention period for each type of electronic document shall comply with applicable laws and regulations.
> 
> Article K Paragraph K Use of Electronic Documents as Evidence

**opus_mt** (`opus-mt-mul-en-ct2`):

> [AI translation, non-authoritative] 4. The age of maintaining the maintenance on electronic Internet based digital content depends upon how to behave in various specific procedures and arrangements related.... The fact that the standard license restrictions on use of electronic Internet based building using computers is principles fundamental

## Honest notes (what was and was NOT proven)

- PROVEN: the mechanical citation property survives a non-Latin, scanned,
  Round-2-style input end to end: the shipped snippet is a byte-for-byte
  substring of the canonical OCR stream (asserted in the run), selected by
  the model but never re-emitted by it.
- PROVEN: the OCR frontier is usable on Lao (mean word confidence 0.841); the M2 dictionary-hit proxy is English-tuned and inapplicable, so its
  manual-review flag stands (reported, not silenced).
- FINDING: gate_document crashes on a Lao corpus: the bm25 lexical tier tokenizes with the English legal vocabulary (zero tokens per Lao chunk, empty index); Round 2 non-Latin corpora need per-language vocabularies or an embedding-only gate mode.
- FINDING: on Lao OCR text the model re-emits quotes with normalized spacing, so strict emit-and-verify fails; locating the quote whitespace-collapsed and slicing the stream's own bytes preserves the byte-for-byte guarantee (Round 2: prefer point-and-slice selection for non-Latin scripts).
- FINDING: opus-mt-mul-en is unusable on Lao (see the
  gloss above); the Lao gloss lane must run on the
  `llm` engine config. Malay drafts are usable but rough (see
  `MALAY_COMPARISON.md`).
- NOT proven: mapping-decision quality on Lao (no ground truth exists for
  this document; the indicator here comes from the cross-lingual gate and
  a single selection, not a scored rubric); translation fidelity beyond
  spot inspection; chunk-boundary quality (the LLM fallback emitted
  plausible Part-level boundaries, unaudited against the source).
- The glosses are DRAFTS: non-authoritative, AI-generated, unshipped.
  Nothing from this demo enters the Round 1 submission artifacts.