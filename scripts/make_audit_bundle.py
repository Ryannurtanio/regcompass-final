"""Assemble an audit-bundle manifest for the U0 review UI.

Two lanes:

- fixtures (default): the COMMITTED demo bundle at audit_bundle/manifest.json,
  pointing (relative paths) at the golden pipeline outputs and the canonical
  fixture PDFs already in the repo. This is what judges run:
      regcompass serve --bundle audit_bundle
- repro: a bundle over the full three-economy reproduction run in gitignored
  data/repro/ (dev machine only), for reviewing the real 966 records. Reuses
  the run's checkpoints in place; coverage stats aggregate the run reports.

Usage: uv run python scripts/make_audit_bundle.py [fixtures|repro]
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FIXTURE_DOCS = [
    {
        "slug": "sg_telecommunications_act_1999",
        "title": "Telecommunications Act 1999",
        "economy": "SG",
        "pdf": "../tests/fixtures/sample_legislation/born_digital/Telecommunications Act 1999.pdf",
    },
    {
        "slug": "my_personal_data_protection_act_2010",
        "title": "Personal Data Protection Act 2010",
        "economy": "MY",
        "pdf": "../tests/fixtures/sample_legislation/born_digital/PERSONAL DATA PROTECTION ACT 2010.pdf",
    },
    {
        "slug": "au_C2026C00098VOL01",
        "title": "Criminal Code Act 1995 (compilation, volume 1)",
        "economy": "AU",
        "pdf": "../tests/fixtures/sample_legislation/born_digital/C2026C00098VOL01.pdf",
    },
]

# The fixture run's coverage evidence (same numbers test_export.py asserts on).
FIXTURE_COVERAGE = {
    "SG": {"law": "Telecommunications Act 1999", "sections": 98, "pairs_gated": 136},
    "MY": {"law": "Personal Data Protection Act 2010", "sections": 146, "pairs_gated": 166},
    "AU": {"law": "Criminal Code Act 1995", "sections": 536, "pairs_gated": 145},
}


def corpus_source_urls() -> dict[str, str]:
    """document_id -> official Source URL, read from config/corpus.yaml, which
    is the same file the Evidence Export's Source URL column comes from. Read
    rather than restated here so a bundle row and an exported row can never
    point at different addresses."""
    import yaml

    corpus = yaml.safe_load((ROOT / "config" / "corpus.yaml").read_text(encoding="utf-8"))
    return {
        doc_id: entry["source_url"]
        for doc_id, entry in (corpus.get("documents") or {}).items()
        if entry.get("source_url")
    }


def make_fixtures() -> Path:
    out_dir = ROOT / "audit_bundle"
    out_dir.mkdir(exist_ok=True)
    source_urls = corpus_source_urls()
    docs = []
    for d in FIXTURE_DOCS:
        slug = d["slug"]
        document_id = f"doc_{slug}"
        entry = {
            "document_id": document_id,
            "title": d["title"],
            "economy": d["economy"],
            "pdf": d["pdf"],
            "source_url": source_urls[document_id],
            "canonical": f"../tests/golden/m1/{slug}.json.gz",
            "chunks": f"../tests/golden/m4/{slug}.chunks.json.gz",
            "records": f"../tests/golden/m8/{slug}.reconciled.json.gz",
            "gate": f"../tests/golden/m5/{slug}.gated.json.gz",
        }
        for key in ("pdf", "canonical", "chunks", "records", "gate"):
            assert (out_dir / entry[key]).exists(), f"missing {entry[key]}"
        docs.append(entry)
    manifest = {"documents": docs, "coverage_stats": FIXTURE_COVERAGE}
    path = out_dir / "manifest.json"
    path.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return path


def make_repro() -> Path:
    repro = ROOT / "data" / "repro"
    out_dir = repro / "audit_bundle"
    out_dir.mkdir(exist_ok=True)
    conn = sqlite3.connect(ROOT / "data" / "regcompass.db")
    conn.row_factory = sqlite3.Row
    db_rows = {
        r["document_id"]: r
        for r in conn.execute(
            "SELECT document_id, local_path, title, source_url FROM documents"
        )
    }
    docs = []
    coverage: dict[str, dict] = {}
    for eco in ("SG", "AU", "MY"):
        report = json.loads((repro / f"{eco}.report.json").read_text(encoding="utf-8"))
        per_doc = report["documents"]
        coverage[eco] = {
            "law": f"{len(per_doc)} {eco} acts (full round-1 corpus)",
            "sections": sum(d["chunks"] for d in per_doc.values()),
            "pairs_gated": sum(d["gate_passed"] for d in per_doc.values()),
        }
        for doc_id, stats in sorted(per_doc.items()):
            if not stats["m7"].get("passed"):
                continue  # nothing to review
            row = db_rows.get(doc_id)
            if row is None or not (ROOT / "data" / row["local_path"]).exists():
                print(f"  skipped {doc_id}: no local PDF")
                continue
            docs.append(
                {
                    "document_id": doc_id,
                    "title": row["title"] or doc_id.removeprefix("doc_"),
                    "economy": eco,
                    "pdf": str(ROOT / "data" / row["local_path"]),
                    "source_url": row["source_url"],
                    "canonical": str(repro / f"canonical/{doc_id}.json.gz"),
                    "chunks": str(repro / f"m4/{doc_id}.chunks.json.gz"),
                    "records": str(repro / f"m8/{eco}.reconciled.json.gz"),
                    "gate": str(repro / f"m5/{doc_id}.gated.json.gz"),
                }
            )
    manifest = {"documents": docs, "coverage_stats": coverage}
    path = out_dir / "manifest.json"
    path.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print(f"{len(docs)} documents")
    return path


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "fixtures"
    if mode == "fixtures":
        print(make_fixtures())
    elif mode == "repro":
        print(make_repro())
    else:
        sys.exit(f"unknown mode {mode!r}: expected fixtures or repro")
