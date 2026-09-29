"""Generate tests/golden/m1/ from the real fixtures (golden chaining).

Each golden file is the extractor's REAL CanonicalText output, gzipped JSON
(word boxes make the large statutes tens of MB raw). M4 develops against these.

Run: uv run python scripts/make_golden_m1.py
"""

import gzip
import sys
import time
from pathlib import Path

from regcompass.extract import extract_with_stats

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures"
GOLDEN = ROOT / "tests/golden/m1"

JOBS = [
    # (slug, relative fixture path, format tag)
    ("sg_telecommunications_act_1999", "sample_legislation/born_digital/Telecommunications Act 1999.pdf", "pdf"),
    ("my_personal_data_protection_act_2010", "sample_legislation/born_digital/PERSONAL DATA PROTECTION ACT 2010.pdf", "pdf"),
    ("au_C2026C00098VOL01", "sample_legislation/born_digital/C2026C00098VOL01.pdf", "pdf"),
    ("niue_legislation_volume_1", "sample_legislation/consolidated_volume/Niue-Legislation Volume 1.pdf", "pdf"),
    ("pk_peca_sros_scanned_flags_only", "sample_legislation/scanned/Pakistan_PECA.pdf", "pdf"),
    ("sso_agc_gov_sg_Act_TA1999", "html/sso_agc_gov_sg_Act_TA1999.html", "html"),
]


def main() -> int:
    GOLDEN.mkdir(parents=True, exist_ok=True)
    for slug, rel, tag in JOBS:
        raw = (FIXTURES / rel).read_bytes()
        t0 = time.perf_counter()
        canonical, stats = extract_with_stats(raw, tag, f"doc_{slug}")
        dt = time.perf_counter() - t0
        out = GOLDEN / f"{slug}.json.gz"
        with gzip.open(out, "wt", encoding="utf-8", compresslevel=9) as f:
            f.write(canonical.model_dump_json())
        print(
            f"{slug}: {len(canonical.pages)} pages, {len(canonical.full_text):,} chars, "
            f"{len(canonical.words):,} words (align {stats.alignment_rate:.2%}), "
            f"low-yield {len(canonical.low_yield_pages)}, engine {canonical.extractor} "
            f"{canonical.extractor_version}, {dt:.1f}s -> {out.name} "
            f"({out.stat().st_size / 1024:.0f} KB)"
        )
        # First-extract identification duty for the AU compilation (FIXTURES.md)
        if slug.startswith("au_"):
            first_page = canonical.slice(canonical.pages[0].char_start, canonical.pages[0].char_end)
            print("--- AU compilation first page (identify the act) ---")
            print("\n".join(first_page.split("\n")[:15]))
            print("---")
    return 0


if __name__ == "__main__":
    sys.exit(main())
