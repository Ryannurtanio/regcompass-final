# docs - deep-dive references

Longer-form documents that back up specific claims in the root `README.md`. Nothing here is
required to run the pipeline; each stands alone.

For the architecture (Discovery, the Run, and the module behind each step), start with the
**Architecture Overview** section of the root `README.md`; the documents below go deeper on
single parts of it.

- `AUDIT_UI.md` - screenshot walkthrough of the audit UI (module U0): how a reviewer checks
  each verified mapping against the highlighted source PDF and how Accept / Reject / Flag
  gates the final export. Screenshots live in `img/u0/`.
- `WEB_DEMO.md` - the judge-facing single-page web UI over the pipeline: launch a run,
  watch the stages stream with the verification funnel and run trace, browse the read-only
  database, inspect the exports. Includes the endpoints and the Docker notes.
- `PORTALS.md` - everything the M10 crawl learned about the three legislation portals
  (SG SSO lazy-load trap, AU Federal Register volumes, the MY `lom` vs `lor` host fact),
  recorded so a future adapter never rediscovers it. Evidence trail:
  `tests/golden/m10/manifest.json`.
- `DEPLOY_VPS.md` - hosting one copy on a rented Ubuntu server: the
  `docker-compose.vps.yml` override (login, Caddy for HTTPS, nothing else published),
  loading the prepared database, changing the password, backups and updates.
- `POSTGRES.md` - the production storage swap story: when SQLite + numpy stops being the
  right call, `src/regcompass/pg.py` moves embeddings to PostgreSQL + pgvector (executed
  live, 5/5 parity). Includes the pg8000-not-psycopg license rationale and the no-Ollama
  GBNF fallback.
- `schema_postgres.sql` - the full PostgreSQL DDL variant of the shipped root
  `schema.sql`, consumed by `POSTGRES.md`.
- `m3/` - the M3 translation-gloss demo evidence (Round 2
  preparation): `LAO_DEMO.md` (a scanned Lao statute through the verifiable pipeline
  end to end, byte-verified snippet + labelled glosses + honest notes), `lao_demo.json`
  (the machine artifact), `MALAY_COMPARISON.md` (opus-mt draft vs the reviewed
  translations that actually ship), and `MULTILANG_NOTE.md` (the bilingual Indian
  gazette fixture: corrupted Devanagari text layer + the needs_gloss non-Latin fix).
  Regenerate with `scripts/m3_demo.py`.
- `img/` - screenshots referenced by the walkthroughs.
