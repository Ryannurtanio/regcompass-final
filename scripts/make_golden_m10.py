"""Run the real M10 crawl for all three economies (live, polite, resumable)
and snapshot the manifest to tests/golden/m10/manifest.json.

The crawled bytes live under data/<economy>/raw/ (gitignored until P0 decides
the shipped-corpus carve-out); the COMMITTED golden is the manifest snapshot:
every URL, status, sha256, size, method, and rate-limit evidence. Re-running
is safe: fetched rows are skipped by the manifest resume.

Run: uv run python scripts/make_golden_m10.py
"""

import json
import sys
import time
from pathlib import Path

from regcompass.config import load_crawl_seeds
from regcompass.crawl import crawl_economy
from regcompass.storage import Storage

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests/golden/m10"
DATA = ROOT / "data"
DB = DATA / "regcompass.db"


def main() -> int:
    DATA.mkdir(exist_ok=True)
    storage = Storage(DB)
    storage.apply_schema()
    seeds = load_crawl_seeds()

    reports = {}
    for economy in ("SG", "AU", "MY"):
        print(f"== {economy} (politeness floor {seeds[economy].rate_limit_seconds}s) ==")
        t0 = time.monotonic()
        report = crawl_economy(economy, storage, DATA, seeds[economy])
        dt = time.monotonic() - t0
        reports[economy] = {
            "discovered": report.discovered,
            "fetched": report.fetched,
            "deduplicated": report.deduplicated,
            "failed": report.failed,
            "skipped_resume": report.skipped_resume,
            "misses": report.misses,
            "duration_seconds": round(dt, 1),
        }
        print(f"  fetched={report.fetched} dedup={report.deduplicated}"
              f" failed={report.failed} resumed={report.skipped_resume} in {dt:.0f}s")
        for miss in report.misses:
            print(f"  miss: {miss}")

    rows = [dict(r) for r in storage.manifest_rows()]
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "manifest.json").write_text(
        json.dumps({"reports": reports, "manifest": rows}, indent=1), encoding="utf-8"
    )
    n_fetched = sum(1 for r in rows if r["status"] == "fetched")
    n_failed = sum(1 for r in rows if r["status"] == "failed")
    print(f"\nmanifest: {len(rows)} rows ({n_fetched} fetched, {n_failed} failed)"
          f" -> {OUT / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
