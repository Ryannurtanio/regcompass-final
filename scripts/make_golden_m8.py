"""Generate tests/golden/m8/ from the golden M7 verified records: reconcile all
(economy, indicator) groups live on the 30B tier (singles short-circuit with no
model call). Requires OPENROUTER_API_KEY for the multi-record groups.

Output: one file per economy with the ReconciledGroups and the updated records
(relationship_to_group, controlling_evidence, uncertainty flags). This is M9's
input dataset.

Run: set -a; source .env; set +a; uv run python scripts/make_golden_m8.py
"""

import gzip
import json
import sys
from pathlib import Path

from regcompass.contracts import MappingRecord
from regcompass.reconcile import reconcile_records

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests/golden"
OUT = GOLDEN / "m8"

JOBS = [
    ("sg_telecommunications_act_1999", "SG"),
    ("my_personal_data_protection_act_2010", "MY"),
    ("au_C2026C00098VOL01", "AU"),
]

LAW_NAMES = {
    "doc_sg_telecommunications_act_1999": "Telecommunications Act 1999",
    "doc_my_personal_data_protection_act_2010": "Personal Data Protection Act 2010",
    "doc_au_C2026C00098VOL01": "Criminal Code Act 1995",
}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for slug, economy in JOBS:
        with gzip.open(GOLDEN / f"m7/{slug}.verified.json.gz", "rt", encoding="utf-8") as f:
            passed = [
                MappingRecord.model_validate(o["record"])
                for o in json.load(f)["outcomes"]
                if o["outcome"] == "passed"
            ]
        groups, updated = reconcile_records(passed, LAW_NAMES)
        n_multi = sum(1 for g in groups if len(g.mapping_ids) > 1)
        overrides = sum(
            1 for g in groups if "override" in (g.reconciliation_notes or "").lower()
        )
        fallbacks = sum(
            1 for g in groups if "fallback" in (g.reconciliation_notes or "").lower()
        )
        conflicts = sum(
            1
            for g in groups
            if any(rel == "conflicting" for rel in g.relationships.values())
        )
        summary = {
            "economy": economy,
            "n_records": len(updated),
            "n_groups": len(groups),
            "multi_groups": n_multi,
            "hierarchy_overrides": overrides,
            "ladder_fallbacks": fallbacks,
            "groups_with_conflicts": conflicts,
        }
        payload = {
            "summary": summary,
            "groups": [g.model_dump() for g in groups],
            "records": [r.model_dump() for r in updated],
        }
        with gzip.open(OUT / f"{slug}.reconciled.json.gz", "wt", encoding="utf-8", compresslevel=9) as f:
            json.dump(payload, f)
        print(f"{slug}: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
