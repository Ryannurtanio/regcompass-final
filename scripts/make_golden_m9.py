"""Generate tests/golden/m9/: the real export over the golden M8 dataset with
LIVE URL liveness checks (the emission-time gate). No LLM calls; needs network
for the three portal URLs only.

Run: uv run python scripts/make_golden_m9.py

Offline regeneration:

    uv run python scripts/make_golden_m9.py --liveness-from tests/golden/m9/submission.csv

Every Source URL in an existing golden is a URL that passed the liveness gate
when that golden was cut, so the recorded file IS the liveness result. Reusing
it regenerates the evidence for a column change without re-probing three
government portals, and without a network call deciding what the goldens say.
A URL absent from the named file is treated as dead, exactly as an offline
probe would have to.

The workbook is deliberately not written here: an xlsx is a zip whose entries
carry write timestamps, so it cannot be pinned by bytes the way the CSV and the
JSONs are (tests/test_workbook.py checks the workbook cell by cell instead).
"""

import argparse
import csv
import gzip
import json
import sys
from pathlib import Path

import httpx

from regcompass.contracts import MappingRecord
from regcompass.export import export_all

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests/golden"
OUT = GOLDEN / "m9"

SLUGS = [
    "sg_telecommunications_act_1999",
    "my_personal_data_protection_act_2010",
    "au_C2026C00098VOL01",
]

COVERAGE = {
    "SG": {"law": "Telecommunications Act 1999", "sections": 98, "pairs_gated": 136},
    "MY": {"law": "Personal Data Protection Act 2010", "sections": 146, "pairs_gated": 166},
    "AU": {"law": "Criminal Code Act 1995", "sections": 536, "pairs_gated": 145},
}


def live_liveness(url: str) -> bool:
    """A URL is live when it answers 2xx/3xx to a browser-ish GET."""
    try:
        r = httpx.get(
            url,
            timeout=30.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
        )
        print(f"  liveness {url} -> HTTP {r.status_code}")
        return r.status_code < 400
    except httpx.HTTPError as e:
        print(f"  liveness {url} -> {type(e).__name__}")
        return False


def recorded_liveness(path: Path):
    """A liveness function that answers from an existing golden export."""
    with path.open(encoding="utf-8-sig", newline="") as f:
        live = {row["Source URL"] for row in csv.DictReader(f) if row["Source URL"]}
    print(f"  liveness replayed from {path}: {len(live)} URL(s) recorded live")

    def check(url: str) -> bool:
        ok = url in live
        if not ok:
            print(f"  liveness {url} -> NOT in {path.name}; treated as dead")
        return ok

    return check


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--liveness-from",
        type=Path,
        metavar="CSV",
        help="reuse the liveness results recorded in an existing golden export"
        " instead of probing the portals (offline regeneration)",
    )
    args = parser.parse_args()
    liveness_fn = (
        recorded_liveness(args.liveness_from) if args.liveness_from else live_liveness
    )
    records, texts, cosines = [], {}, {}
    for slug in SLUGS:
        with gzip.open(GOLDEN / f"m8/{slug}.reconciled.json.gz", "rt", encoding="utf-8") as f:
            records.extend(MappingRecord.model_validate(r) for r in json.load(f)["records"])
        with gzip.open(GOLDEN / f"m5/{slug}.gated.json.gz", "rt", encoding="utf-8") as f:
            payload = json.load(f)
        for r in payload["passed"]:
            texts[r["chunk"]["chunk_id"]] = r["chunk"]["text"]
            cosines[(r["chunk"]["chunk_id"], r["indicator_id"])] = r["cosine_pillar"]

    result = export_all(
        OUT,
        records,
        chunk_text_lookup=texts,
        gate_cosine_lookup=cosines,
        coverage_stats=COVERAGE,
        liveness_fn=liveness_fn,
        write_xlsx=False,
    )
    supp = json.loads(result.supplementary_path.read_text(encoding="utf-8"))
    print(
        f"battery GREEN: {supp['n_rows']} rows "
        f"({supp['n_substantive']} substantive + {supp['n_absence']} absence), "
        f"{len(result.working_json_paths)} working JSONs -> {OUT}"
    )
    print("derived scores:", json.dumps(supp["derived_scores"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
