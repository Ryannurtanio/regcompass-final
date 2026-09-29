"""Generate tests/golden/m5/ from the golden M4 chunks: real BGE-M3 + bm25s
gate output for the three fixture acts. Requires the local ollama server
(`ollama serve`, `ollama pull bge-m3`).

Per document: the passed GatedChunk rows in full (M6's input), plus the
complete decision log (every chunk x indicator with scores, no text) so
exclusions stay auditable without duplicating megabytes of chunk text
(recover text via chunk_id from tests/golden/m4/).

Run: uv run python scripts/make_golden_m5.py
"""

import gzip
import json
import sys
from dataclasses import asdict
from pathlib import Path

from regcompass.contracts import Chunk
from regcompass.gate import gate_document

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests/golden"
OUT = GOLDEN / "m5"

JOBS = [
    "sg_telecommunications_act_1999",
    "my_personal_data_protection_act_2010",
    "au_C2026C00098VOL01",
]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for slug in JOBS:
        with gzip.open(GOLDEN / f"m4/{slug}.chunks.json.gz", "rt", encoding="utf-8") as f:
            chunks = [Chunk.model_validate(c) for c in json.load(f)["chunks"]]
        gated, report = gate_document(chunks)
        payload = {
            "report": asdict(report),
            "passed": [g.model_dump() for g in gated if g.gate_decision == "passed"],
            "decisions": [
                {
                    "chunk_id": g.chunk.chunk_id,
                    "section_label": g.chunk.section_label,
                    "indicator_id": g.indicator_id,
                    "cosine_pillar": round(g.cosine_pillar, 6),
                    "bm25_indicator": round(g.bm25_indicator, 6),
                    "gate_decision": g.gate_decision,
                }
                for g in gated
            ],
        }
        out = OUT / f"{slug}.gated.json.gz"
        with gzip.open(out, "wt", encoding="utf-8", compresslevel=9) as f:
            json.dump(payload, f)
        print(
            f"{slug}: candidates={report.n_candidates} "
            f"passed_per_indicator={report.passed_per_indicator} "
            f"reduction={report.reduction_ratio:.3f} -> {out.name}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
