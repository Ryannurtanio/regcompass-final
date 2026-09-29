# config - pipeline configuration and reviewed reference data

All files here are committed and judge-visible. Generated files say so; the rest are
hand-maintained. Loaded via `src/regcompass/config.py` unless noted.

| File | What it is | Consumer | Maintained by |
|---|---|---|---|
| `corpus.yaml` | Per-document corpus metadata: law name, source URL, Last Amended (derived mechanically from front matter, never guessed) | `export.py` | generated: `scripts/make_corpus_yaml.py` |
| `crawl_seeds.yaml` | M10 crawl seed list per economy | `crawl.py` | hand |
| `indicator_id_crosswalk.json` | Numeric indicator IDs (canonical, 6.1-7.5) to P-code aliases; export emits numeric by default | `config.py` | hand |
| `indicators.json` | The 9 RDTII Pillar 6/7 indicator definitions | `config.py`, mapper prompts | hand (from the framework docs) |
| `law_metadata.yaml` | Law Name (where the derived title is abbreviated or misread), Law Number / Ref and Last Amended year for Documents outside `corpus.yaml`, keyed by document id; each value read off the official text, blank when absent | `pipeline.py` via `config.load_law_metadata` | hand |
| `known_matrix.json` | KNOWN provision matrix behind the Discovery Tag (`NEW`/`KNOWN`) | `config.py` | generated: `scripts/extract_known_matrix.py` (local-only input) |
| `models.yaml` | The Engine registry (declared Engines, prices, key variable names) plus the shared embedder | `config.py`, `engines.py` | hand |
| `pillar_6_keywords.json`, `pillar_7_keywords.json` | Gate keyword vocabulary, loaded per pillar. SHARED with the M11 shortlist ranking: after any vocab edit, re-run the M5 live tests | `gate.py` via `config.load_keywords` | hand (evaluated in `tests/golden/m11/EVAL.md`) |
| `prepared_data.yaml` | Download address and SHA-256 of the prepared-data archive a release attaches (blank between releases) | `prepared_data.py` via `regcompass load-data` | hand, at release time (`docs/RELEASE_DATA.md`) |
| `portals.yaml` | Country portal adapter config (endpoints, URL shapes) | `crawl.py` | hand (quirks documented in `docs/PORTALS.md`) |
| `review_drops.json` | Human review-REJECTED records excluded at export (mapping_id, reason, reviewer, date); checkpoints keep the full record, `supplementary.json` discloses every applied drop. | `export.py` via `config.load_review_drops` | hand-reviewed per entry |
| `round1_corpus_map.json` | Reviewed mapping: UN Database law entries to crawled documents | `scripts/make_corpus_yaml.py`, `scripts/make_golden_m11.py`, `test_shortlist.py` | generated then human-reviewed: `scripts/make_corpus_map.py` |
| `verbatim_english.json` | REVIEWED English translations for non-English verbatim snippets; the FALLBACK lane, read only where the database holds no Gloss for that (Run, Mapping). The non-authoritative label is composed in `export.py` code, so a translation from this file can never ship unlabelled | `export.py` | hand-reviewed per entry |
