"""tests/golden/{m4,m5,m6,m7,m8,m9,m10,m12,repro,u0} are pinned by exact bytes.

m9, m10, m12, repro and u0 are EVIDENCE artifacts: no test consumes them as
inputs, so a silent regeneration would otherwise be caught by nothing. m4
through m8 are pipeline checkpoints that ARE test inputs, and pinning them
closes the matching hole: tests/test_verify.py reads them, so quietly
regenerating one could move what those tests assert instead of failing them.
The m6 fixture in particular carries the five pre-verify subsection offenders
that prove the M7 retry-and-drop escalation works; those bytes are evidence.

Regenerating ON PURPOSE means refreshing the manifest:

    cd <repo root>
    find tests/golden/m4 tests/golden/m5 tests/golden/m6 tests/golden/m7 \
         tests/golden/m8 tests/golden/m9 tests/golden/m10 tests/golden/m12 \
         tests/golden/repro tests/golden/u0 -type f | sort \
      | xargs shasum -a 256 | sed 's|tests/golden/||' > tests/golden/EVIDENCE.sha256
"""

import gzip
import hashlib
import json
import re
from pathlib import Path

GOLDEN = Path(__file__).parent / "golden"
ROOT = Path(__file__).parent.parent
EVIDENCE_DIRS = ("m4", "m5", "m6", "m7", "m8", "m9", "m10", "m12", "repro", "u0")


def test_evidence_goldens_match_manifest():
    lines = (GOLDEN / "EVIDENCE.sha256").read_text(encoding="utf-8").strip().splitlines()
    manifest = {}
    for line in lines:
        digest, rel = line.split("  ", 1)
        manifest[rel] = digest
    actual = {}
    for d in EVIDENCE_DIRS:
        for p in sorted((GOLDEN / d).rglob("*")):
            if p.is_file():
                rel = p.relative_to(GOLDEN).as_posix()
                actual[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    # dict compare reports adds, deletes, AND content drift in one assertion
    assert actual == manifest


def test_refusal_counts_reconcile_across_shipped_documents():
    """REPORT.md and supplementary.json ship side by side
    and once disagreed on the confirmation-refusal count (302 vs 301). Both
    are true over different denominators: 302 refused among ALL verified
    records (the m12 golden), 301 among the SHIPPED rows (the review-drops
    lane removed one refused record). This gate recomputes both from the
    committed artifacts so the two documents can never drift apart silently."""
    with gzip.open(GOLDEN / "m12/classifications.jsonl.gz", "rt", encoding="utf-8") as f:
        rows = [json.loads(line) for line in f]
    refused_ids = {
        r["mapping_id"] for r in rows if r["classification"].get("confirmed") is False
    }
    drops = json.loads(
        (ROOT / "config/review_drops.json").read_text(encoding="utf-8")
    )["drops"]
    dropped_refused = {d["mapping_id"] for d in drops} & refused_ids

    supp = json.loads((GOLDEN / "repro/supplementary.json").read_text(encoding="utf-8"))
    summary = supp["classification_summary"]
    assert summary["n_confirmation_refused"] == len(refused_ids) - len(dropped_refused)

    report = (GOLDEN / "repro/REPORT.md").read_text(encoding="utf-8")
    m = re.search(r"(\d+) score-eligible labels REFUSED", report)
    assert m, "REPORT.md must state the refusal headline"
    assert int(m.group(1)) == len(refused_ids)
    # and the denominator note must name the supplementary's number
    assert f"n_confirmation_refused={summary['n_confirmation_refused']}" in report
