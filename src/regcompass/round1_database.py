"""The ESCAP RDTII 2.1 Round 1 Database scores, transcribed by hand.

3 economies x 9 in-scope indicators = 27 cells, copied from the organizers'
"ESCAP-RDTII-2.1_ Round 1 Database.xlsx". That workbook is not in this repo (it
lives in the read-only Knowledge Base beside it), so this transcription is the
only copy of those numbers the code can see.

It is reference data for COMPARISON ONLY. Nothing in the pipeline reads it: no
stage scores against it, no export cites it, and it never influences a mapping.
Two consumers use it, both after the fact:

- scripts/make_gt_matrix.py (local dev only) cross-checks its xlsx aggregation
  against this dict and dies loudly if the two disagree, then freezes
  tests/golden/repro/GT_MATRIX.md.
- tests/test_gt_scores.py recomputes the published 23/27 agreement offline from
  this dict and the shipped tests/golden/repro/supplementary.json, so the
  headline number in README.md, GT_MATRIX.md and REPORT.md cannot drift.

Scores are on the RDTII scale: 0.0, 0.5 or 1.0.
"""

from __future__ import annotations

ROUND1_DATABASE_SCORES: dict[str, dict[str, float]] = {
    "AU": {"6.1": 0.5, "6.2": 0.5, "6.3": 0.0, "6.4": 1.0,
           "7.1": 0.0, "7.2": 0.5, "7.3": 1.0, "7.4": 0.0, "7.5": 1.0},
    "MY": {"6.1": 0.0, "6.2": 0.5, "6.3": 0.0, "6.4": 1.0,
           "7.1": 0.0, "7.2": 0.0, "7.3": 1.0, "7.4": 1.0, "7.5": 1.0},
    "SG": {"6.1": 0.0, "6.2": 0.5, "6.3": 0.0, "6.4": 1.0,
           "7.1": 0.0, "7.2": 0.0, "7.3": 1.0, "7.4": 1.0, "7.5": 1.0},
}
