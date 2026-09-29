"""Build vendor/wordlist_legal_en.txt from the M1 golden outputs.

The OCR dictionary-hit-rate proxy needs an English wordlist. Instead of vendoring
a third-party dictionary (license provenance), we derive one from our own
born-digital canonical streams (SG Telecom Act, MY PDPA, AU Criminal Code vol 1,
Niue volume, SSO HTML): ~1.5M words of real statutory English. Self-contained,
deterministic, legal-domain-specific.

Run: uv run python scripts/make_wordlist.py
"""

import gzip
import re
from collections import Counter
from pathlib import Path

from regcompass.contracts import CanonicalText

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_M1 = ROOT / "tests/golden/m1"
OUT = ROOT / "vendor/wordlist_legal_en.txt"

WORD_RE = re.compile(r"[a-z]{3,}")
MIN_FREQ = 2


def main() -> None:
    counts: Counter[str] = Counter()
    for path in sorted(GOLDEN_M1.glob("*.json.gz")):
        c = CanonicalText.model_validate_json(gzip.open(path, "rt", encoding="utf-8").read())
        counts.update(WORD_RE.findall(c.full_text.lower()))
    words = sorted(w for w, n in counts.items() if n >= MIN_FREQ)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(words) + "\n", encoding="utf-8")
    print(f"{len(words)} words -> {OUT.relative_to(ROOT)} ({OUT.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
