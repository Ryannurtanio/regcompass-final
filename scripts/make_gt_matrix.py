"""Freeze the full 27-cell ground-truth comparison.

LOCAL DEV ONLY: reads the ESCAP Round 1 Database xlsx from the parent
Knowledge Base repo (read-only there) plus the repro export's
supplementary.json, and writes the committed artifact
tests/golden/repro/GT_MATRIX.md. The 8 headline GT spot checks were never the
full matrix; this records every cell, with every mismatch justified, so the
submission notes can cite one frozen document.

Aggregation is direction-dependent: the LACK
indicators 7.1/7.2 take the row MINIMUM (a framework row scoring 0 wins);
every other indicator takes the row MAXIMUM (any restrictive row drives the
cell). Continuation rows leave Indicator_ID blank and inherit from above.

Usage:
  uv run --with openpyxl python scripts/make_gt_matrix.py \
      "../Knowledge Base/Databases/ESCAP-RDTII-2.1_ Round 1 Database.xlsx"
"""

import json
import sys
from pathlib import Path

from regcompass.round1_database import ROUND1_DATABASE_SCORES

ROOT = Path(__file__).resolve().parents[1]
SHEET_TO_CODE = {"Australia": "AU", "Malaysia": "MY", "Singapore": "SG"}
IN_SCOPE = ("6.1", "6.2", "6.3", "6.4", "7.1", "7.2", "7.3", "7.4", "7.5")
MIN_INDICATORS = {"7.1", "7.2"}  # inverse LACK direction: a 0 row wins

# The Round 1 Database ground-truth transcription, as a cross-check: the xlsx
# aggregation must reproduce it exactly or this script fails loudly. It lives in
# the package so tests/test_gt_scores.py can import the SAME copy; a second
# transcription here would be free to drift.
GROUND_TRUTH_TRANSCRIPTION = ROUND1_DATABASE_SCORES

# Every mismatch MUST have a justification here; a mismatch without one (or a
# justification for a cell that now agrees) fails the freeze. Keep these
# argued from the Guide and the shipped rows, never tuned.
JUSTIFICATIONS = {
    ("AU", "6.1"): (
        "Database 0.5 rests on My Health Records Act 2012 s.77, scored through "
        "the personal-data-in-government-systems reading, which is an "
        "inference, not a stated framework rule. Our rubric "
        "holds the conservative reading: no My Health Records provision "
        "survived adversarial confirmation as a transfer ban, so the cell "
        "derives 0 (under-scoring on the conservative side). The My Health "
        "Records rows themselves are present in the export as evidence."
    ),
    ("AU", "6.2"): (
        "Database 0.5 rests on My Health Records Act 2012 s.77 graded through "
        "the specific-dataset 0.5 lane. Our cell derives 1.0 from the Privacy "
        "Act 1988 credit-reporting storage duty ('must store the information "
        "... in Australia'), classified local_storage covering personal data; "
        "the Guide scores a personal-data storage measure 1 whether horizontal "
        "or sectoral. The divergence is the lane "
        "judgment for credit reporting information: one named dataset (0.5) "
        "or personal data at large (1.0). Recorded as an open interpretation "
        "question rather than tuned to match."
    ),
    ("MY", "6.2"): (
        "Database 0.5 grades sectoral/specific storage rules. Our cell "
        "derives 1.0 from the Services Tax Act 2018 record-keeping "
        "localization, classified non-personal HORIZONTAL (all taxable "
        "persons). The Guide's 0.5 lane names 'a specific data set' "
        "explicitly, and tax/accounting records arguably belong there "
        "(specific_dataset scope would derive 0.5 and agree); the data_scope "
        "granularity call is the divergence. Recorded rather than tuned."
    ),
    ("AU", "7.2"): (
        "Database 0.5 grades the Security of Critical Infrastructure Act 2018 "
        "as not a dedicated cybersecurity framework. Our cell derives 0.0 "
        "from a Data Availability and Transparency Act 2022 breach-"
        "notification duty whose classification confirmed "
        "dedicated_framework + horizontal. This cell turns entirely on the "
        "dedicated-framework qualifier judgment; recorded as an open "
        "interpretation question."
    ),
}

IN_SAMPLE_CAVEAT = (
    "In-sample caveat (unchanged from the repro report): the iteration "
    "that produced the classification rubric was informed by knowing "
    "which headline cells disagreed, so this matrix is an in-sample result "
    "like every number in the repro record; on a held-out economy the "
    "conservative refusal rate may over-refuse."
)


def load_expected(xlsx_path: Path) -> dict[str, dict[str, float]]:
    import openpyxl

    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    expected: dict[str, dict[str, float]] = {}
    for sheet, code in SHEET_TO_CODE.items():
        rows_by_ind: dict[str, list[float]] = {}
        current = None
        for r in list(wb[sheet].iter_rows(min_col=1, max_col=3, values_only=True))[1:]:
            ind = str(r[1]).strip() if r[1] not in (None, "") else None
            if ind:
                current = ind
            if current not in IN_SCOPE:
                continue
            score = r[2]
            if score is None or str(score).strip() == "":
                continue
            rows_by_ind.setdefault(current, []).append(float(score))
        expected[code] = {
            ind: (min(vals) if ind in MIN_INDICATORS else max(vals))
            for ind, vals in rows_by_ind.items()
        }
    return expected


def main() -> int:
    xlsx_path = Path(sys.argv[1])
    expected = load_expected(xlsx_path)
    if expected != GROUND_TRUTH_TRANSCRIPTION:
        for eco in GROUND_TRUTH_TRANSCRIPTION:
            for ind in IN_SCOPE:
                want = GROUND_TRUTH_TRANSCRIPTION[eco].get(ind)
                got = expected.get(eco, {}).get(ind)
                if want != got:
                    print(f"DISAGREEMENT {eco} {ind}: xlsx-aggregated {got} vs transcription {want}")
        raise SystemExit("xlsx aggregation does not reproduce the ground-truth transcription; fix before freezing")

    supp = json.loads(
        (ROOT / "data/repro/export/supplementary.json").read_text(encoding="utf-8")
    )
    derived = supp["derived_scores"]
    details = supp.get("score_details", {})

    lines = [
        "# Full 27-cell ground-truth comparison (frozen artifact)",
        "",
        "Derived rubric scores (supplementary.json, `derive_scores_v2`) against the",
        "ESCAP Round 1 Database indicator-level ground truth, all 3 economies x all",
        "9 in-scope indicators. Generated by `scripts/make_gt_matrix.py` from the",
        "organizer-supplied Database xlsx, which is not redistributed in this",
        "repository (aggregation: row MIN for the LACK indicators",
        "7.1/7.2, row MAX otherwise; the aggregation is cross-checked against a",
        "hand-transcribed copy of the Database's indicator-level scores before",
        "anything is written). The 8",
        "headline spot checks in REPORT.md were never the full matrix; this is.",
        "",
        f"{IN_SAMPLE_CAVEAT}",
        "",
        "| Economy | Indicator | Database GT | Derived | Agreement | Basis (controlling provision) |",
        "|---------|-----------|------------:|--------:|-----------|-------------------------------|",
    ]
    n_agree = 0
    mismatches: list[tuple[str, str]] = []
    for eco in ("AU", "MY", "SG"):
        for ind in IN_SCOPE:
            want = GROUND_TRUTH_TRANSCRIPTION[eco][ind]
            got = derived[eco][ind]
            agree = want == got
            n_agree += agree
            if not agree:
                mismatches.append((eco, ind))
            d = details.get(eco, {}).get(ind, {})
            basis = str(d.get("basis", ""))
            controlling = d.get("controlling_mapping_id") or ""
            basis_cell = f"{basis}" + (f" ({controlling})" if controlling else "")
            lines.append(
                f"| {eco} | {ind} | {want} | {got} | "
                f"{'agree' if agree else 'MISMATCH'} | {basis_cell} |"
            )
    lines += ["", f"**Agreement: {n_agree}/27.**", ""]

    unjustified = [m for m in mismatches if m not in JUSTIFICATIONS]
    stale = [m for m in JUSTIFICATIONS if m not in mismatches]
    if unjustified:
        raise SystemExit(f"mismatches without justification: {unjustified}; write them before freezing")
    if stale:
        raise SystemExit(f"stale justifications for now-agreeing cells: {stale}; remove them")

    if mismatches:
        lines.append("## Every mismatch, justified")
        lines.append("")
        for eco, ind in mismatches:
            lines.append(f"### {eco} {ind}: Database {GROUND_TRUTH_TRANSCRIPTION[eco][ind]}, derived {derived[eco][ind]}")
            lines.append("")
            lines.append(JUSTIFICATIONS[(eco, ind)])
            lines.append("")

    out = ROOT / "tests/golden/repro/GT_MATRIX.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{n_agree}/27 agree; mismatches: {mismatches}")
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
