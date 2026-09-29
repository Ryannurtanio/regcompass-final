"""Generate tests/golden/m2/ from the scanned fixtures: the real OCR CanonicalText
(with MEASURED CER against the hand-checked reference excerpts persisted in
ocr_quality_cer) plus the judge-facing evidence pairs (page PNG + extracted text).

Run: uv run python scripts/make_golden_m2.py
"""

import gzip
import sys
from pathlib import Path

from regcompass.ocr import cer_against_reference, ocr_document

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures"
GOLDEN = ROOT / "tests/golden/m2"

JOBS = [
    (
        "pk_sro_221_2017_peca_powers",
        "sample_legislation/scanned/Pakistan_PECA.pdf",
        "ocr_reference/pakistan_peca_page1_excerpt.txt",
    ),
    (
        "in_public_procurement_om_2017",
        "sample_legislation/scanned/India-Public_Procurement_order_2017.pdf",
        "ocr_reference/india_procurement_page1_excerpt.txt",
    ),
]


def main() -> int:
    for slug, rel, ref_rel in JOBS:
        raw = (FIXTURES / rel).read_bytes()
        evidence = GOLDEN / "evidence" / slug
        canonical = ocr_document(raw, f"doc_{slug}", evidence_dir=evidence)
        page1 = canonical.slice(canonical.pages[0].char_start, canonical.pages[0].char_end)
        cer = cer_against_reference((FIXTURES / ref_rel).read_text(encoding="utf-8"), page1)
        canonical = canonical.model_copy(
            update={"ocr_quality": canonical.ocr_quality.model_copy(update={"ocr_quality_cer": round(cer, 4)})}
        )
        out = GOLDEN / f"{slug}.json.gz"
        with gzip.open(out, "wt", encoding="utf-8", compresslevel=9) as f:
            f.write(canonical.model_dump_json())
        q = canonical.ocr_quality
        print(
            f"{slug}: {len(canonical.pages)} pages, {len(canonical.full_text):,} chars, "
            f"{len(canonical.words):,} words | conf {q.mean_word_confidence:.2f}, "
            f"dict {q.dictionary_hit_rate:.2f}, CER(page1 ref) {cer:.3%}, "
            f"escalated={q.escalated_to_rapidocr}, manual={q.manual_review} | "
            f"evidence: {len(list(evidence.glob('*.png')))} pairs -> {out.name}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
