# scripts - generators and run infrastructure

Nothing here is imported by `src/regcompass`; these are operator tools. Each script's
docstring states its exact run command and requirements (some need `.env` sourced for the
OpenRouter key, some need live ollama, m10 crawls live portals).

## Golden generators (one per module; output goes to `tests/golden/<mN>/`)

`make_golden_m1.py` (extraction streams), `make_golden_m2.py` (OCR + page evidence),
`make_golden_m4.py` (chunks), `make_golden_m5.py` (gate, live bge-m3),
`make_golden_m6.py` (mapper, live 30B), `make_golden_m7.py` (verify),
`make_golden_m8.py` (reconcile), `make_golden_m9.py` (export),
`make_golden_m10.py` (live corpus crawl), `make_golden_m11.py` (shortlist eval).

See `tests/golden/README.md` for what each output is and who consumes it. The evidence-only
outputs (m9, m10, and everything under repro/m12/u0) are checksum-pinned: after a legitimate
regeneration, refresh `tests/golden/EVIDENCE.sha256` (command in
`tests/test_evidence_goldens.py`).

## Full-corpus run infrastructure

- `run_repro.py` - the three-economy reproduction driver, five subcommands run in
  order: `run` (per-economy M1..M7 over the full corpus), `classify` (M12 measure-nature
  classification),
  `reconfirm` (re-asks the adversarial confirmations for one measure nature after a claim's
  text has been edited; cheap, checkpoint-shape-preserving), `finish` (M8 reconcile with the
  fit-filtered controlling ladder + M9 export + GT spot checks), and `export`
  (ZERO-model-call regeneration from the frozen
  m8 checkpoints: section-label repair + controlling-pointer gate + M9; use this for any
  export-side fix instead of the paid `finish`). Resume-safe: per-document checkpoints and
  per-pair jsonl live in gitignored `data/repro/`. Produces the evidence in
  `tests/golden/repro/` and `m12/`.
- `pack_prepared_data.py` - packs the prepared-data archive a release attaches: a pruned
  copy of the working database (the six Economies, completed Runs on the declared Engines,
  no Review Decisions) plus the source bytes its Documents reference, with a `.sha256` and a
  manifest. Reads the database and data folder only. `regcompass load-data` unpacks it; see
  `docs/RELEASE_DATA.md`.
- `rehearse_discovery.py` - rehearses Discovery for every Economy in the live-test pool (ID,
  IN, LA, CN, TH, MN, VN, RU, KZ, TL) and every Pillar with no Engine call. Each cell (one Economy,
  one Pillar) runs Discovery by Pillar in a fresh data folder, reads each Document, splits it
  into sections and runs the Gate, then stops where the Engine would first be called; a guard
  removes every Engine key from the process and refuses any completion. One Economy at a time
  by default, OCR held to one thread. Asks the live Portals politely, and needs a local ollama
  for the Gate. Resumable: each finished cell is one line in `<out>/results/<Economy>.jsonl`.
  Flags: `--out` (working folder, required), `--table` (write the markdown table there),
  `--economies` and `--pillars` (comma lists, default all), `--cap` (Document cap per cell,
  default 12), `--parallel` (Economies at once, default 1), `--redo-failed`, `--render-only`
  (rewrite the table without running), `--previous` (an earlier `--out`, summarised beside
  this pass), `--notes` (a markdown file placed above the tables).
- `make_audit_bundle.py` - builds an audit-UI bundle: `fixtures` mode writes the committed
  demo manifest at `audit_bundle/`, `repro` mode writes to `data/repro/audit_bundle/`.
- `u0_e2e.py` - Playwright end-to-end test of the audit UI; source of `tests/golden/u0/`. Also
  drives the Export download links, the Confidence number, the kept progress log, the Runs
  table width, the Comparison defaults and the Run named in the Evidence header.
- `pdf_render_check.py` - Playwright check that a SCANNED law renders in the audit view. It
  reads the pixels the browser painted, because a page the viewer could not decode is a blank
  white rectangle with the right DOM around it. Covers a born-digital act in the same run.

## Config and vendor generators

- `make_corpus_yaml.py` - regenerates `config/corpus.yaml` from the crawled corpus DB
  (fixture entries preserved verbatim; Last Amended derived mechanically, never guessed).
- `make_corpus_map.py` - regenerates `config/round1_corpus_map.json` (the reviewed
  Database-law to document mapping).
- `extract_known_matrix.py` - regenerates `config/known_matrix.json` and
  `config/baseline_laws.json` from the RDTII Round 1 and Round 2 Databases and the Legal Inventory (operator-supplied local input, not included in this repository; judges never need to run this).
- `make_wordlist.py` - regenerates `vendor/wordlist_legal_en.txt` from the m1 golden
  streams.
- `reverify_mappings.py` - re-checks the stored Mappings of a COPY of a database with the
  current word-for-word rules, read-only, and prints per Economy and Engine which earlier drops
  pass now and whether anything that passed now fails. No Engine call.
- `fetch_m3_weights.py` - fetches the M3 translation-gloss model
  (Helsinki-NLP/opus-mt-mul-en at a pinned HF revision, per-file SHA-256s verified) and
  converts it to CTranslate2 int8 at `models/opus_mt_mul_en_ct2/` (gitignored, never
  committed). The conversion runs in an ephemeral uvx env, so torch never enters the
  project venv. Only the optional M3 gloss lane (default OFF) needs it; judges never
  run this.
- `m3_demo.py` - the M3 demo artifacts in `docs/m3/`: `malay` (opus-mt draft vs the
  reviewed A1472 translations), `lao` (the end-to-end chain on the scanned Lao fixture:
  OCR lao -> LLM-fallback chunking -> cross-lingual gate -> byte-verified selection ->
  labelled gloss; needs the mapper key + local ollama), `render` (re-render the Lao md
  from the saved JSON, no model calls).

## Evidence freezers

- `make_gt_matrix.py` - freezes the full 27-cell ground-truth comparison to
  `tests/golden/repro/GT_MATRIX.md` from the repro export's supplementary.json plus the
  ESCAP Round 1 Database xlsx (operator-supplied local input, not included in this repository; judges never need to run
  this). Needs openpyxl, which is not a project dependency:
  `uv run --with openpyxl python scripts/make_gt_matrix.py "<path to Round 1 Database xlsx>"`.
  A legitimate regeneration must come out byte-identical unless the export changed; refresh
  `tests/golden/EVIDENCE.sha256` last.

## One-off converters

- `convert_run_log.py` - rebuilds a recorded event file for a Run made before the app
  recorded events, from its command-line log plus its Run Record (read-only from the
  working database), and checks every count against the record. Used once, for the
  Singapore P7 Engine A Run, whose result is `tests/fixtures/run_events/`. `--install`
  also writes the file beside the database, so the Runs list offers 'Watch again'.
