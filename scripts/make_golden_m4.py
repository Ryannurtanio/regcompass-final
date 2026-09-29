"""Generate tests/golden/m4/ from the golden M1/M2 canonical streams: the real
chunk partitions the deterministic splitter produces, plus its report. The
degraded-lane document (pk S.R.O.) needs the live LLM boundary fallback and is
added by rerunning this script with --with-fallback once OPENROUTER_API_KEY
works (requires a valid OPENROUTER_API_KEY).

Run: uv run python scripts/make_golden_m4.py [--with-fallback]
"""

import gzip
import json
import sys
from dataclasses import asdict
from pathlib import Path

from regcompass.chunk import _default_completion, split_document
from regcompass.contracts import CanonicalText

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests/golden"
OUT = GOLDEN / "m4"

DETERMINISTIC = [
    ("sg_telecommunications_act_1999", "m1/sg_telecommunications_act_1999.json.gz"),
    ("sso_agc_gov_sg_Act_TA1999", "m1/sso_agc_gov_sg_Act_TA1999.json.gz"),
    ("my_personal_data_protection_act_2010", "m1/my_personal_data_protection_act_2010.json.gz"),
    ("au_C2026C00098VOL01", "m1/au_C2026C00098VOL01.json.gz"),
    ("niue_legislation_volume_1", "m1/niue_legislation_volume_1.json.gz"),
]
FALLBACK = [
    ("pk_sro_221_2017_peca_powers", "m2/pk_sro_221_2017_peca_powers.json.gz"),
]


def load(rel: str) -> CanonicalText:
    with gzip.open(GOLDEN / rel, "rt", encoding="utf-8") as f:
        return CanonicalText.model_validate_json(f.read())


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with_fallback = "--with-fallback" in sys.argv
    jobs = DETERMINISTIC + (FALLBACK if with_fallback else [])
    for slug, rel in jobs:
        canonical = load(rel)
        fn = _default_completion if with_fallback and (slug, rel) in FALLBACK else None
        chunks, report = split_document(canonical, completion_fn=fn)
        payload = {
            "report": asdict(report),
            "chunks": [c.model_dump() for c in chunks],
        }
        out = OUT / f"{slug}.chunks.json.gz"
        with gzip.open(out, "wt", encoding="utf-8", compresslevel=9) as f:
            json.dump(payload, f)
        kinds: dict[str, int] = {}
        for c in chunks:
            kinds[c.chunk_kind] = kinds.get(c.chunk_kind, 0) + 1
        print(
            f"{slug}: style={report.style} chunks={report.n_chunks} "
            f"sections={report.n_sections} kinds={kinds} "
            f"fallback={report.fallback_used} -> {out.name}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
