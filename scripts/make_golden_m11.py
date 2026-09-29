"""Run the real M11 shortlist over the crawled corpus (data/, from
scripts/make_golden_m10.py) and snapshot the outputs to tests/golden/m11/:
one ranked CSV per (economy, pillar) plus report.json with the ingest,
embedding and ranking evidence the exit-gate tests assert against.

Resumable end to end: ingested documents (by source sha256) and embedded
documents (by existing windows) are skipped on re-runs, so only ranking and
the report are recomputed.

Needs: the crawled corpus on disk, a running ollama server with bge-m3.
Eval numbers (recall@10, capped precision@5) are computed when
config/round1_corpus_map.json exists (scripts/make_corpus_map.py, reviewed).

Run: uv run python scripts/make_corpus_map.py   # after first ingest
     uv run python scripts/make_golden_m11.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from regcompass.config import load_known_matrix  # noqa: E402
from regcompass.shortlist import (  # noqa: E402
    embed_economy,
    ingest_economy,
    precision_at_k,
    rank_economy,
    recall_at_k,
)
from regcompass.storage import Storage  # noqa: E402

DATA = ROOT / "data"
DB = DATA / "regcompass.db"
OUT = ROOT / "tests/golden/m11"
CORPUS_MAP = ROOT / "config/round1_corpus_map.json"

ECONOMIES = ("SG", "AU", "MY")
PILLARS = (6, 7)


def relevant_sets(economy: str, pillar: int, cmap: dict, matrix: dict) -> list[set[str]]:
    law_keys: list[str] = []
    for ind, laws in matrix[economy].items():
        if int(ind.split(".")[0]) != pillar:
            continue
        for law in laws:
            if law["law_key"] not in law_keys:
                law_keys.append(law["law_key"])
    sets = []
    for key in law_keys:
        entry = cmap[economy][key]
        if entry["status"] in ("in_corpus", "consolidated"):
            sets.append(set(entry["document_ids"]))
    return sets


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    storage = Storage(DB)
    storage.apply_schema()
    cmap = None
    if CORPUS_MAP.exists():
        cmap = {
            k: v
            for k, v in json.loads(CORPUS_MAP.read_text(encoding="utf-8")).items()
            if not k.startswith("_")
        }
    matrix = load_known_matrix()["database"]

    report: dict = {"economies": {}}
    for economy in ECONOMIES:
        eco_report: dict = {}
        t0 = time.monotonic()
        results, excluded = ingest_economy(
            storage, DATA, economy, evidence_root=DATA / "ocr_evidence"
        )
        eco_report["ingest_duration_s"] = round(time.monotonic() - t0, 1)
        eco_report["ingested_new"] = len(results)
        eco_report["excluded"] = excluded
        for r in results:
            print(
                f"  {r.document_id}: '{r.title}' pages={r.n_pages}"
                f" low_yield={r.n_low_yield_pages} ocr={r.ocr_applied}"
            )

        t0 = time.monotonic()
        embedded = embed_economy(storage, economy)
        eco_report["embed_duration_s"] = round(time.monotonic() - t0, 1)
        eco_report["embedded_new"] = embedded

        docs = storage.documents_for_economy(economy)
        eco_report["n_documents"] = len(docs)
        total_pages = sum(d["n_pages"] or 0 for d in docs)
        low_yield = sum(d["n_low_yield_pages"] or 0 for d in docs)
        eco_report["text_coverage"] = (
            round(1.0 - low_yield / total_pages, 4) if total_pages else 0.0
        )
        eco_report["ocr_documents"] = [
            d["document_id"] for d in docs if d["ocr_applied"]
        ]

        rank_total = eco_report["embed_duration_s"]
        eco_report["pillars"] = {}
        for pillar in PILLARS:
            rows, prep = rank_economy(storage, economy, pillar, out_dir=OUT)
            rank_total += prep.duration_s
            pillar_report = {
                "csv": Path(prep.csv_path).name,
                "n_rows": len(rows),
                "duration_s": round(prep.duration_s, 2),
                "notes": prep.notes,
            }
            if cmap is not None:
                sets = relevant_sets(economy, pillar, cmap, matrix)
                ranked_ids = [r.document_id for r in rows]
                relevant = set().union(*sets) if sets else set()
                pillar_report["eval"] = {
                    "n_gt_laws_in_corpus": len(sets),
                    "recall_at_10": recall_at_k(ranked_ids, sets, 10),
                    "precision_at_5_capped": precision_at_k(ranked_ids, relevant, 5),
                }
            eco_report["pillars"][str(pillar)] = pillar_report
            print(f"== {economy} pillar {pillar}: {len(rows)} rows"
                  f" ({pillar_report.get('eval')})")
        # the latency gate covers the shortlist run on the ingested corpus:
        # embedding (cold on first run) + both pillar rankings
        eco_report["rank_duration_s"] = round(rank_total, 1)
        report["economies"][economy] = eco_report

    report["corpus_map_used"] = cmap is not None
    (OUT / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"wrote {OUT}/report.json")
    if cmap is None:
        print("NOTE: config/round1_corpus_map.json missing; eval skipped."
              " Run scripts/make_corpus_map.py, review, then re-run this script.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
