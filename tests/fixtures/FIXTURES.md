# Test fixtures

The 8 sample-legislation PDFs from the UN materials (the organizers' own extraction test
kit), copied verbatim from that kit. These are CANONICAL
COPIES: never modify them, never rename them (filenames are reproduced verbatim, including
the `INDIA-~1.PDF` 8.3 short name). Organized
here by the pipeline lane each one exercises.

`tests/golden/` starts empty: each module's real output on these fixtures, once VERIFIED,
is saved there, and the next module develops against those files (golden chaining).

## born_digital/ (M1 extract happy path)

- `Telecommunications Act 1999.pdf` - SINGAPORE (the country is a verified correction; the
  filename does not say it). Born-digital statute, "s. N" drafting style. Primary M1 and M4
  fixture.
- `PERSONAL DATA PROTECTION ACT 2010.pdf` - Malaysia PDPA (Act 709), born-digital. Ground
  truth: MY 6.1 = 0 and 7.4 = 1 rest on this act, so it is also the key M5/M6 recall
  fixture.
- `C2026C00098VOL01.pdf` - Australia, Federal Register compilation C2026C00098 volume 1
  (2.1 MB, large). Identified from its own front matter: **Criminal Code Act 1995
  (No. 12, 1995), Compilation No. 173, compilation date 14 March 2026**, volume 1 of 3
  (sections 1-5 + Schedule sections 1.1-261.3), registered 23/03/2026. 666 pages,
  1,184,609 extracted chars. The M1 speed lane (pypdfium2 alternate) and the AU
  Part/Division/section drafting style for M4.

## scanned/ (M2 OCR lane)

- `Pakistan_PECA.pdf` - scanned. MISLABEL ON RECORD: the content is S.R.O.s (statutory
  regulatory orders), NOT the PECA Act itself (verified on inspection).
  Treat as an OCR fixture, not a PECA source.
- `India-Public_Procurement_order_2017.pdf` - scanned order, 1.3 MB. The second OCR fixture
  and the RapidOCR escalation candidate.

## domestic_language/ (M2 Lao lane + M3 translation gloss)

- `Lao PDR-Law on Electronic Transaction (Amended) No. 31.pdf` - a SCAN in Lao script
  (verified: needs OCR then translation; it is not born-digital). Tesseract is the only
  stack option with Lao. Later-round preparation, not Round 1 critical.
  The whole 29-page file runs end to end in `tests/test_non_english_lane.py` behind the
  `slow` marker; the fast suite uses the two-page slice under `derived/` below.

## multilanguage/ (mixed-script pages)

- `INDIA-~1.PDF` - one legal file carrying multiple languages. What it
  actually is (exercised 10 Jul 2026): a BORN-DIGITAL 4-page Gazette
  of India notification (Foreign Exchange Management (Non-debt Instruments) (Third
  Amendment) Rules, 2024 - space-sector FDI caps), Hindi Devanagari and English interleaved
  on every page. M1 pdfplumber extracts it (8,342 chars, no OCR lane fires), BUT the
  embedded Devanagari font is non-Unicode: the Hindi half comes out systematically
  glyph-substituted while the English half is clean. Exercised M3 gloss labelling and
  found the needs_gloss non-Latin gap (fixed + pinned in tests/test_translate.py); the
  full record is docs/m3/MULTILANG_NOTE.md. Per-page script handling in M2 does not
  apply here: the file never reaches M2.

## consolidated_volume/ (structural stress test)

- `Niue-Legislation Volume 1.pdf` - many laws consolidated in one 5.3 MB volume. The M4
  chunker's stress test: per-law boundaries, front matter, and tables of contents must be
  labelled and accounted, not silently chunked into the first law.

## html/ (M1 HTML lane)

- `sso_agc_gov_sg_Act_TA1999.html` - the EXACT bytes (262,495; HTTP 200, text/html;
  charset=utf-8) of https://sso.agc.gov.sg/Act/TA1999 (Singapore Telecommunications Act
  1999 on SSO), fetched 5 Jul 2026 with a plain Chrome-UA curl (the cheap rung of the SG
  anti-bot ladder was sufficient). Extracted with the
  bs4/lxml path; NOT a canonical UN corpus file, so unlike the sample_legislation PDFs it
  may be re-fetched if SSO's markup changes, with the change noted here.
- `cac_pipl_articles_excerpt.htm` - 8,987 B, a trimmed excerpt of the Cyberspace
  Administration of China's page for the **Personal Information Protection Law**
  (www.cac.gov.cn, the regulator's public official text, as the China Corpus stores it,
  23 Sep 2026): the page title, the contents list and the paragraphs from chapter 1 to
  article 14, markup unchanged, the site's navigation and scripts cut away. The page prints
  each article number in bold, `<strong>第一条</strong>`, so the HTML lane puts the number
  on a line of its own; `tests/test_chunk.py` checks the chunker still finds every article.

## derived/ (page slices cut from the canonical files)

Not organizer bytes: page slices this project cuts out of the canonical files above so the
fast suite can run a lane the whole file makes too slow. Regenerate with
`uv run --no-sync python scripts/make_derived_fixtures.py` (pypdfium2, already pinned); the
canonical sources are only ever read. The regeneration is reproducible in CONTENT, not in
bytes: pdfium stamps a fresh document id on every save, so a rerun produces the same pages
with a different sha256. The committed file is the fixture; do not pin its digest.

- `lao_electronic_transactions_p05_p06.pdf` - 80,641 B, printed pages 5 and 6 of the Lao
  statute above, carrying six consecutive articles of body text. The non-English lane runs
  on it end to end in seconds: OCR with `lao+eng`, the chunker's `article_word` profile,
  the meaning-only Gate, the fake Engine, byte-for-byte verification.

## ocr_reference/ (verbatim OCR excerpts)

Short text excerpts of what the OCR ladder actually read, kept beside the scans so a test
can assert against real output rather than a hand-typed approximation. Verbatim: never
tidied, never re-spelled, OCR noise and all.

- `pakistan_peca_page1_excerpt.txt` and `india_procurement_page1_excerpt.txt` - page-1
  excerpts of the two scanned fixtures above, read by `tests/test_ocr.py`.
- `pipl_cn_chapter1_excerpt.txt` - 3,192 B, Chinese. Chapter 1 and the opening of chapter 2
  of the **Personal Information Protection Law of the People's Republic of China**, from a
  scanned copy supplied by a teammate (image-only, 7 pages; the scan itself is NOT in this
  repository). Read by the project's own OCR ladder on **22 Sep 2026**: tesseract fell
  back, RapidOCR escalated on its Chinese model, 7,942 characters out of the whole file.
  This excerpt is the head of that stream, copied without a single edit, which
  is why articles 4, 11 and 14 are missing: the scan lost their heading lines and the OCR
  read them as `热采`, `校带护和护全采一` and `采`. It carries eleven article headings
  (1, 2, 3, 5, 6, 7, 8, 9, 10, 12, 13), two chapter headings and one section heading, so
  the `article_zh` chunking profile, the Chinese numeral ordering and the chapter hierarchy
  are all exercised on real text. `tests/test_chunk.py` splits it and
  `tests/test_non_english_lane.py` runs it upload to export.
- `uu27_2022_pasal1_19_excerpt.txt` - 10,775 B, Bahasa Indonesia. Articles 1 to 19 of
  **Undang-Undang Nomor 27 Tahun 2022** (Personal Data Protection), copied without an edit
  from the canonical stream of the official PDF's text layer (pdfplumber; the layer's own
  misreadings such as `Pasal I`, `1O` and `(l)` kept). It carries the shapes that are not
  headings: a sentence wrapped before a cross-reference (`Pasal 13 ayat (1) ...`), the
  page-foot catchword `Pasal 18. .`, and the numbered definitions of article 1.
  `tests/test_chunk.py` and `tests/test_section_label_repair.py` read it.

## run_events/ (a recorded Run to watch again)

- `run_20260923T041241Z_a07249.jsonl` - 193 events, the Singapore Pillar 7 Run on Engine A
  (10 Documents, 2,072 Pieces, 870 Candidates, 148 Mappings, 5 groups) in the event-file
  format a Run started from the app writes. That Run predates event files, so
  `scripts/convert_run_log.py` rebuilt it from its command-line log and Run Record. The
  Gate's scores per Candidate were never recorded (no Candidate events), and the times
  between Steps are estimated inside the Run's real start and end; the first event says so.
- `SG-P7-engine-a.log` - that Run's log, local paths replaced by `<repo>`.
- `run_20260923T041241Z_a07249.record.json` - its Run Record, plus the Corpus's titles and
  pages and each Document's stored Mappings, read from the working database.
  `tests/test_convert_run_log.py`, `tests/test_run_replay.py` and the UI's
  `replayControl.test.ts` read them.

## What is NOT here

The Round 1 working corpus (Singapore / Australia / Malaysia acts from the official portals)
is CRAWLED by M10, not fixtured: these 8 files prove extraction/OCR/chunking mechanics, and
the ESCAP Round 1 Database (the organizers' indicator-level ground truth, not distributed
in this repository) proves mapping correctness against the real corpus; the frozen
comparison lives in `tests/golden/repro/GT_MATRIX.md`.
