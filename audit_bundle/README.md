# audit_bundle - committed demo bundle for the audit UI

`manifest.json` is the fixture-scale demo bundle consumed by
`regcompass audit --bundle audit_bundle` (document list, records, and highlight coords
derived from the golden fixture runs). It is committed so the audit UI works from a plain
clone with no pipeline run.

Regenerate with `uv run python scripts/make_audit_bundle.py fixtures`. The full-corpus
variant (`repro` mode) writes to gitignored `data/repro/audit_bundle/` instead.
Screenshot walkthrough of the UI: `docs/AUDIT_UI.md`.
