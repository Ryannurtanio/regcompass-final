"""Generate tests/golden/m7/ from the golden M6 records: the full mechanical
verify pass. Clean records verify offline; records failing a check re-map live
on the 30B tier with mechanical feedback (needs OPENROUTER_API_KEY for those).

Per document: every VerifyOutcome (passed record, no_evidence record, or
dropped) plus a summary. Golden M7 records are the shipped, 100%-verified
dataset M8 reconciles.

Run: set -a; source .env; set +a; uv run python scripts/make_golden_m7.py
"""

import gzip
import json
import sys
from pathlib import Path

from regcompass.contracts import GatedChunk, MappingRecord
from regcompass.verify import verify_with_retry

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests/golden"
OUT = GOLDEN / "m7"

JOBS = [
    ("sg_telecommunications_act_1999", "SG"),
    ("my_personal_data_protection_act_2010", "MY"),
    ("au_C2026C00098VOL01", "AU"),
]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for slug, economy in JOBS:
        with gzip.open(GOLDEN / f"m5/{slug}.gated.json.gz", "rt", encoding="utf-8") as f:
            gated_by = {
                (r["chunk"]["chunk_id"], r["indicator_id"]): GatedChunk.model_validate(r)
                for r in json.load(f)["passed"]
            }
        with gzip.open(GOLDEN / f"m6/{slug}.mapped.json.gz", "rt", encoding="utf-8") as f:
            m6 = json.load(f)["outcomes"]

        outcomes = []
        retried = 0
        for o in m6:
            key = (o["chunk_id"], o["indicator_id"])
            if o["outcome"] == "dropped":
                outcomes.append({**{k: o[k] for k in ("chunk_id", "section_label", "indicator_id")},
                                 "outcome": "dropped", "attempts": o["attempts"],
                                 "failures": o["failures"], "record": None})
                continue
            record = MappingRecord.model_validate(o["record"])
            if o["outcome"] == "no_evidence":
                outcomes.append({**{k: o[k] for k in ("chunk_id", "section_label", "indicator_id")},
                                 "outcome": "no_evidence", "attempts": o["attempts"],
                                 "failures": [], "record": record.model_dump()})
                continue
            v = verify_with_retry(record, gated_by[key], economy)
            if v.attempts > record.extraction_attempts:
                retried += 1
                print(f"  retried {slug} {o['section_label']} x {o['indicator_id']}: "
                      f"{v.outcome} after {v.attempts} attempts")
            outcomes.append({**{k: o[k] for k in ("chunk_id", "section_label", "indicator_id")},
                             "outcome": v.outcome, "attempts": v.attempts,
                             "failures": v.failures, "record": v.record.model_dump()})

        counts: dict[str, int] = {}
        for o in outcomes:
            counts[o["outcome"]] = counts.get(o["outcome"], 0) + 1
        summary = {"economy": economy, "n_pairs": len(outcomes), "counts": counts,
                   "verify_retried": retried}
        with gzip.open(OUT / f"{slug}.verified.json.gz", "wt", encoding="utf-8", compresslevel=9) as f:
            json.dump({"summary": summary, "outcomes": outcomes}, f)
        print(f"{slug}: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
