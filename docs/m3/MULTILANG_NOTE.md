# M3 note: the multilanguage fixture (INDIA-~1.PDF)

This note covers the one multilanguage fixture (INDIA-~1.PDF) not exercised
by any test, script, or artifact elsewhere. Exercised 10 Jul 2026.
Companion evidence to `LAO_DEMO.md`.

## What the file actually is

A born-digital 4-page Gazette of India, Extraordinary notification (Ministry
of Finance, 16 April 2024): the Foreign Exchange Management (Non-debt
Instruments) (Third Amendment) Rules, 2024, amending space-sector FDI caps.
Hindi (Devanagari) and English interleaved on every page. M1 pdfplumber
extracts 8,342 chars across 4 pages; `should_ocr` is False, so the M2 OCR
lane never fires; this file does not exercise per-page script handling in M2.

## Finding 1: the born-digital Devanagari text layer is corrupted

The embedded Devanagari font is non-Unicode: extraction yields systematic
glyph substitution in the Hindi half (for example the stream reads
"जििेिी मुद्रा प्रबंध अजधजनयम" where the page displays
"विदेशी मुद्रा प्रबंध अधिनियम", the Foreign Exchange Management Act). The
interleaved ENGLISH text extracts cleanly. This is the classic Indian-gazette
encoding problem: for Round 2, born-digital is NOT automatically
trustworthy - a non-Latin text layer needs a fidelity check (or a forced OCR
lane) before it becomes a canonical stream.

## Finding 2 (fixed + pinned): needs_gloss missed non-Latin scripts

`needs_gloss` delegated to the export battery's `looks_non_english`, whose
under-8-Latin-words rule deliberately passes pure non-Latin text as
insufficient signal. Right for the Round 1 shipping gate it guards (the
corpus's only non-English is Latin-script Malay); wrong for the drafting
lane: a pure Devanagari or Lao snippet returned False, so the gloss CLI
would have skipped exactly the snippets that most need drafts. Fixed in
translate.py as SUPERSET semantics (battery result OR 8+ non-Latin letters);
the export battery itself is untouched. Pinned by
`test_needs_gloss_flags_non_latin_scripts` and the superset test.

## The M3 lane on a Hindi snippet (rule 1 of the notification)

- `needs_gloss`: True (after the fix). `detect_language`: "und" - Devanagari
  is outside the deliberate script map, conservative by design, not a bug.
- opus-mt draft: fluent-looking garbage ("The complex name of these GNOMEs
  will be the date on which GGI Finance Management..."). Garbage in, garbage
  out: the corrupted glyphs destroy it. Recorded as shipped-draft evidence of
  why drafts are human-gated; the degeneracy guard is a repetition check, not
  a fidelity check, and fidelity is the reviewer's job by design.
- LLM alternate (30B) draft on the SAME corrupted snippet: "These rules shall
  be known as the Foreign Exchange Management (Non-Debt Instruments) (Third
  Amendment) Rules, 2024. (2) These shall come into force on the date of
  their publication in the Official Gazette." - it decodes the glyph
  substitution from context, matching the gazette's own English half.

## What was and was NOT proven

- PROVEN: the M1 lane handles the file (born-digital, no OCR); the gloss lane
  now flags non-Latin snippets; the labelled-draft and honest-failure
  behaviors both surfaced on real input.
- PROVEN (comparative): on encoding-corrupted Devanagari the `llm` engine
  recovers meaning, opus-mt does not - consistent with the Lao verdict:
  non-Latin lanes should default to `gloss_engine: llm`.
- NOT proven: extraction fidelity for well-encoded Devanagari (this file has
  no clean Hindi text layer to compare against); per-page script detection
  (not built; nothing needed it); any mapping-quality claim for India (out of
  scope, not a Round 1 economy).
- Nothing from this note enters the Round 1 submission artifacts.

Reproduce: the snippet run is 10 lines against `translate.py`; no saved
machine artifact was needed beyond this note.
