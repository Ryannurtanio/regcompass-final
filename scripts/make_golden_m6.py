"""Generate tests/golden/m6/ from the golden M5 gated chunks: real 30B mapper
output for every gate-passed (chunk, indicator) pair of the three fixture acts.
Requires OPENROUTER_API_KEY (source .env).

Per document: every MapOutcome (mapped record, no_evidence record, or dropped
with its failure log) plus a summary. Golden M6 records are M7's input fixtures.

Run: set -a; source .env; set +a; uv run python scripts/make_golden_m6.py
"""

import gzip
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from regcompass.contracts import GatedChunk
from regcompass.map import map_gated_chunk

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests/golden"
OUT = GOLDEN / "m6"

JOBS = [
    ("sg_telecommunications_act_1999", "SG"),
    ("my_personal_data_protection_act_2010", "MY"),
    ("au_C2026C00098VOL01", "AU"),
]
WORKERS = 8


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for slug, economy in JOBS:
        with gzip.open(GOLDEN / f"m5/{slug}.gated.json.gz", "rt", encoding="utf-8") as f:
            passed = [GatedChunk.model_validate(r) for r in json.load(f)["passed"]]

        def _one(g: GatedChunk) -> dict:
            out = map_gated_chunk(g, economy)
            return {
                "chunk_id": g.chunk.chunk_id,
                "section_label": g.chunk.section_label,
                "indicator_id": g.indicator_id,
                "outcome": out.outcome,
                "attempts": out.attempts,
                "failures": out.failures,
                "record": out.record.model_dump() if out.record else None,
            }

        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            outcomes = list(pool.map(_one, passed))

        counts: dict[str, int] = {}
        for o in outcomes:
            counts[o["outcome"]] = counts.get(o["outcome"], 0) + 1
        retried = sum(1 for o in outcomes if o["attempts"] > 1)
        summary = {
            "economy": economy,
            "model_tier": "mapper",
            "n_pairs": len(outcomes),
            "counts": counts,
            "retried": retried,
        }
        with gzip.open(OUT / f"{slug}.mapped.json.gz", "wt", encoding="utf-8", compresslevel=9) as f:
            json.dump({"summary": summary, "outcomes": outcomes}, f)
        print(f"{slug}: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
