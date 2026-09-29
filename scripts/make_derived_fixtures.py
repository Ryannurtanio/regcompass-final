"""Cut the small DERIVED fixtures out of the canonical organizer files.

The canonical files under tests/fixtures/sample_legislation are read-only (see
tests/fixtures/FIXTURES.md): they are the organizer's bytes and nothing may
modify or rename them. Some of them are too slow to run in the fast suite,
though. The Lao statute is a 29-page scan with no text layer, so an end-to-end
test over the whole file is minutes of real OCR.

This script writes page slices of those files into tests/fixtures/derived/,
using pypdfium2 (already pinned for rasterization; no new dependency). The
slices are committed, so the suite never needs this script; it exists so the
derivation is reproducible and auditable.

    uv run --no-sync python scripts/make_derived_fixtures.py
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pypdfium2 as pdfium

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "tests/fixtures/sample_legislation"
DERIVED_DIR = ROOT / "tests/fixtures/derived"

# (source file, zero-based pages to keep, derived filename, why these pages)
SLICES: tuple[tuple[str, tuple[int, ...], str, str], ...] = (
    (
        "domestic_language/Lao PDR-Law on Electronic Transaction (Amended) No. 31.pdf",
        (4, 5),
        "lao_electronic_transactions_p05_p06.pdf",
        "printed pages 5 and 6: six consecutive articles of body text"
        " (mmadtaa 5 to 11), enough for the chunker's article_word profile and"
        " a real Gate shortlist, at 4% of the file's OCR cost",
    ),
)


def main() -> None:
    DERIVED_DIR.mkdir(parents=True, exist_ok=True)
    for rel, pages, name, why in SLICES:
        src = SOURCE_DIR / rel
        source = pdfium.PdfDocument(src.read_bytes())
        dest = pdfium.PdfDocument.new()
        dest.import_pages(source, list(pages))
        out = DERIVED_DIR / name
        dest.save(str(out))
        raw = out.read_bytes()
        print(f"{name}: {len(raw):,} B  sha256={hashlib.sha256(raw).hexdigest()}")
        print(f"  from {rel} pages {[p + 1 for p in pages]} ({why})")


if __name__ == "__main__":
    main()
