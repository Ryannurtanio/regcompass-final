"""The Evidence Export as the organizers' own workbook.

The final-round submission is not a file of ours that happens to have the right
columns: it is OUTPUT_TEMPLATE_FINAL_ROUND.xlsx, opened, filled and saved. The
template carries a formula column the Coverage Matrix counts from, four
advisory validations, an autofilter and six other sheets, and every one of them
is the secretariat's, not ours. So this module only ever does three things to
it: clear the two example rows' values, write our rows into A9:N109, and save.

Two quirks of the organizers' file drive the design.

1. The entry area is rows 9 to 109 - 101 rows - and the autofilter, the
   O-column formula fill and every Coverage Matrix COUNTIFS are hard-bounded to
   row 109. A 102nd row would be silently uncounted, so the cap is structural.
2. The Instructions say to DELETE example rows 7 and 8. Deleting them with
   openpyxl would shift the rows below without translating any formula, which
   breaks O9:O109, the autofilter range and the matrix bounds. The values are
   cleared instead and the export says so.

Indicator IDs are written as text cells. Entered as numbers, 4.01 collapses to
4.1 and 12.10 to 12.1, and those are different Indicators.
"""

from __future__ import annotations

from copy import copy
from dataclasses import dataclass
from pathlib import Path

from .config import CONFIG_DIR

TEMPLATE_NAME = "OUTPUT_TEMPLATE_FINAL_ROUND.xlsx"
SHEET = "Output Data"
FIRST_ROW = 9
LAST_ROW = 109
ROW_CAP = LAST_ROW - FIRST_ROW + 1  # 101
EXAMPLE_ROWS = (7, 8)
# The organizers' A..N, in their order. Column O is their Pillar formula and is
# never written. A test pins this against the template's own header row AND
# against export.COLUMNS + the Language column, so the two can never drift.
WORKBOOK_COLUMNS = (
    "Economy",
    "Law Name",
    "Law Number / Ref",
    "Last Amended",
    "Indicator ID",
    "Article / Section",
    "Discovery Tag",
    "Location Reference",
    "Verbatim Snippet",
    "Mapping Rationale",
    "Source URL",
    "Confidence",
    "Notes",
    "Language of Source",
)
INDICATOR_COLUMN = WORKBOOK_COLUMNS.index("Indicator ID") + 1
CONFIDENCE_COLUMN = WORKBOOK_COLUMNS.index("Confidence") + 1
TEXT_FORMAT = "@"

EXAMPLE_ROWS_NOTE = (
    "template example rows 7 and 8 emptied, not deleted: deleting them would"
    " shift the organizers' Pillar formula, autofilter range and Coverage"
    " Matrix bounds, which openpyxl does not translate"
)


class WorkbookError(RuntimeError):
    """The template is missing or is not the organizers' workbook."""


@dataclass
class WorkbookResult:
    path: Path
    n_rows: int
    rows_cut: int
    example_rows_cleared: bool
    note: str


def template_path(config_dir=CONFIG_DIR) -> Path:
    """The vendored copy of the organizers' template. config/organizer/README.md
    records its SHA-256 and a test pins the copy against it."""
    return Path(config_dir) / "organizer" / TEMPLATE_NAME


def write_workbook(
    template: Path,
    out_path: Path,
    rows: list[dict],
    *,
    cap: int = ROW_CAP,
) -> WorkbookResult:
    """Fill the organizers' Output Data sheet with `rows` and save to out_path.

    `rows` are the export's own rows, already battery-green and already capped
    by export.select_rows; the cap here is the structural backstop, so the file
    can never run past row 109 whatever the caller did. Every value is written
    as the export wrote it to the CSV, except Confidence, which becomes a real
    number for the template's 0-to-1 decimal rule."""
    from openpyxl import load_workbook

    template = Path(template)
    if not template.exists():
        raise WorkbookError(
            f"the organizers' template is missing at {template}:"
            f" the Evidence Export cannot be written without it"
        )
    cap = max(0, min(cap, ROW_CAP))
    shipped = rows[:cap]
    rows_cut = len(rows) - len(shipped)

    wb = load_workbook(template)
    if SHEET not in wb.sheetnames:
        raise WorkbookError(f"{template} has no '{SHEET}' sheet: not the organizers' template")
    ws = wb[SHEET]

    # The example rows go value by value; the row itself, its height and every
    # formula below it stay exactly where the organizers put them.
    for row in EXAMPLE_ROWS:
        for col in range(1, ws.max_column + 1):
            ws.cell(row=row, column=col).value = None

    for offset, row in enumerate(shipped):
        r = FIRST_ROW + offset
        for col, header in enumerate(WORKBOOK_COLUMNS, start=1):
            value = row.get(header, "")
            cell = ws.cell(row=r, column=col)
            if col == CONFIDENCE_COLUMN:
                text = str(value).strip()
                cell.value = float(text) if text else None
                continue
            text = "" if value is None else str(value)
            cell.value = text or None
            if col == INDICATOR_COLUMN:
                # text, never a number: 4.01 and 12.4.1 must survive intact
                cell.number_format = TEXT_FORMAT

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    return WorkbookResult(
        path=out_path,
        n_rows=len(shipped),
        rows_cut=rows_cut,
        example_rows_cleared=True,
        note=EXAMPLE_ROWS_NOTE,
    )


# ---------------------------------------------------------------------------
# the Engine Comparison sheet (sheet 4 of the same template)
# ---------------------------------------------------------------------------
#
# Filled the same way as Output Data: the organizers' sheet is opened, its
# entry cells are written and nothing of theirs moves, so the two dropdowns
# (E17:E56, F17:H56) and the three COUNTIF counters (D58:D60) survive. The
# file carries this sheet only: the live hour's Engine Comparison is handed in
# on its own and copied into the evidence workbook, and the other sheets would
# be empty templates a reader could mistake for ours.
#
# The entry area is rows 17 to 56, 40 provisions. When there are more, the
# rest go on a continuation sheet laid out like block 2 with the same two
# dropdowns, row 57 says so, and the three counters are extended to read that
# sheet too, so a count is never silently short.

ENGINE_SHEET = "Engine Comparison"
ENGINE_CONTINUATION_SHEET = "Engine Comparison (cont.)"
ENGINE_FIRST_ROW = 17
ENGINE_LAST_ROW = 56
ENGINE_ROW_CAP = ENGINE_LAST_ROW - ENGINE_FIRST_ROW + 1  # 40
ENGINE_EXAMPLE_ROW = 16
ENGINE_NOTE_ROW = 57
ENGINE_COUNTER_ROWS = {58: "Engine A only", 59: "Engine B only", 60: "Both"}
ENGINE_COUNTER_COLUMN = "D"
# Block 1's field labels (A5:A10) and block 2's headers (A15:I15), verbatim.
ENGINE_SUMMARY_FIELDS = (
    "Provider and model name",
    "Start time (hh:mm)",
    "End time (hh:mm)",
    "Elapsed (minutes)",
    "Documents fetched during this pass",
    "Cost of this pass (US$)",
)
ENGINE_SUMMARY_FIRST_ROW = 5
ENGINE_SUMMARY_HEADERS = ("Field", "Engine A — first pass", "Engine B — second pass")
ENGINE_PROVISION_HEADERS = (
    "#",
    "Law Name",
    "Article / Section",
    "Indicator ID",
    "Found by",
    "Indicator differs?",
    "Citation differs?",
    "Quoted words differ?",
    "How they differ — one line",
)
ENGINE_HEADER_ROW = 15
FOUND_BY_VALUES = ("Engine A only", "Engine B only", "Both")
DIFFERS_VALUES = ("Yes", "No", "n/a")
CONTINUATION_HEADER_ROW = 3
CONTINUATION_FIRST_ROW = 4

ENGINE_EXAMPLE_ROW_NOTE = (
    "template example row 16 emptied: it describes a Thai statute, and a"
    " reader could take it for one of our provisions; the counters never read it"
)

# The organizers' own text for the sheet, used only when the template file is
# not available and the layout has to be built rather than filled.
_ENGINE_SHEET_TEXT = {
    "A1": "Finale morning — engine comparison  (criterion C5b, 4 points)",
    "A2": (
        "Completed during the live hour on 15 October and submitted with your"
        " evidence. Cover every provision either engine produced. The second"
        " engine re-reads documents already downloaded and fetches nothing new."
    ),
    "A3": "1 · Per-engine summary",
    "A4": ENGINE_SUMMARY_HEADERS[0],
    "B4": ENGINE_SUMMARY_HEADERS[1],
    "C4": ENGINE_SUMMARY_HEADERS[2],
    "A12": (
        "Engine B's 'documents fetched' must be 0. A steward checks this against"
        " the Run Record."
    ),
    "A14": "2 · Provision-by-provision comparison",
    "A58": "Found by Engine A only:",
    "A59": "Found by Engine B only:",
    "A60": "Found by both:",
    "A62": "3 · Which output would you hand to a ministry, and why?",
    "A68": (
        "One paragraph, in your own words. Two engines agreeing is a good result"
        " — say so, and say what it tells you. This is the part that carries the"
        " marks, because it is the part a menu cannot fake."
    ),
}
_ENGINE_SHEET_MERGES = ("A1:I1", "A2:I2", "A12:I12", "A63:I67", "A68:I68")


@dataclass
class EngineComparisonResult:
    n_rows: int
    n_on_sheet: int
    n_continued: int
    from_template: bool
    note: str


def _add_list_validation(ws, sqref: str, values) -> None:
    from openpyxl.worksheet.datavalidation import DataValidation

    dv = DataValidation(
        type="list", formula1='"' + ",".join(values) + '"', allow_blank=True
    )
    dv.add(sqref)
    ws.add_data_validation(dv)


def _build_engine_sheet(wb):
    """The organizers' Engine Comparison layout, built from their own text,
    for a machine where the template file is missing."""
    ws = wb.create_sheet(ENGINE_SHEET)
    for ref, text in _ENGINE_SHEET_TEXT.items():
        ws[ref] = text
    for offset, label in enumerate(ENGINE_SUMMARY_FIELDS):
        ws.cell(row=ENGINE_SUMMARY_FIRST_ROW + offset, column=1, value=label)
    for col, header in enumerate(ENGINE_PROVISION_HEADERS, start=1):
        ws.cell(row=ENGINE_HEADER_ROW, column=col, value=header)
    for row, value in ENGINE_COUNTER_ROWS.items():
        ws[f"{ENGINE_COUNTER_COLUMN}{row}"] = (
            f'=COUNTIF(E{ENGINE_FIRST_ROW}:E{ENGINE_LAST_ROW},"{value}")'
        )
    for merged in _ENGINE_SHEET_MERGES:
        ws.merge_cells(merged)
    for row in range(ENGINE_FIRST_ROW, ENGINE_LAST_ROW + 1):
        ws.cell(row=row, column=4).number_format = TEXT_FORMAT
    _add_list_validation(ws, f"E{ENGINE_FIRST_ROW}:E{ENGINE_LAST_ROW}", FOUND_BY_VALUES)
    _add_list_validation(ws, f"F{ENGINE_FIRST_ROW}:H{ENGINE_LAST_ROW}", DIFFERS_VALUES)
    ws.freeze_panes = f"A{ENGINE_FIRST_ROW}"
    for letter, width in (("A", 36.0), ("B", 30.0), ("D", 16.0), ("E", 15.0), ("I", 44.0)):
        ws.column_dimensions[letter].width = width
    return ws


def _provision_cells(number: int, row) -> list:
    return [
        number,
        row.law_name,
        row.article_section,
        row.indicator_id,
        row.found_by,
        row.indicator_differs,
        row.citation_differs,
        row.quoted_words_differ,
        row.difference,
    ]


def _write_provision(ws, r: int, cells: list, style_row: int | None = None) -> None:
    for col, value in enumerate(cells, start=1):
        cell = ws.cell(row=r, column=col)
        cell.value = value if value != "" else None
        if style_row is not None:
            source = ws.parent[ENGINE_SHEET].cell(row=style_row, column=col)
            if source.has_style:
                cell._style = copy(source._style)
        if col == 4:
            # text, never a number: 12.10 is not 12.1
            cell.number_format = TEXT_FORMAT


def _summary_values(summary) -> list:
    if summary is None:
        return [None] * len(ENGINE_SUMMARY_FIELDS)
    return [
        summary.provider_model or None,
        summary.start_hhmm or None,
        summary.end_hhmm or None,
        summary.elapsed_minutes,
        summary.documents_fetched,
        summary.cost_usd,
    ]


def write_engine_comparison(
    template: Path | None,
    out,
    comparison,
) -> EngineComparisonResult:
    """Fill the organizers' Engine Comparison sheet from a Comparison and save
    it to ``out`` (a path or a binary file object).

    Block 1 takes each pass's summary (B5:C10), block 2 every provision row
    from row 17, block 3 (A63, the team's own paragraph) is never written.
    ``template`` may be None or missing, in which case the same layout is
    built from the organizers' text."""
    from openpyxl import Workbook, load_workbook

    from_template = template is not None and Path(template).exists()
    if from_template:
        wb = load_workbook(template)
        if ENGINE_SHEET not in wb.sheetnames:
            raise WorkbookError(
                f"{template} has no '{ENGINE_SHEET}' sheet: not the organizers' template"
            )
        for name in list(wb.sheetnames):
            if name != ENGINE_SHEET:
                del wb[name]
        ws = wb[ENGINE_SHEET]
    else:
        wb = Workbook()
        del wb[wb.active.title]
        ws = _build_engine_sheet(wb)
    wb.active = wb.sheetnames.index(ENGINE_SHEET)

    for offset, (a, b) in enumerate(
        zip(_summary_values(comparison.pass_a), _summary_values(comparison.pass_b))
    ):
        r = ENGINE_SUMMARY_FIRST_ROW + offset
        ws.cell(row=r, column=2).value = a
        ws.cell(row=r, column=3).value = b

    for col in range(1, len(ENGINE_PROVISION_HEADERS) + 1):
        ws.cell(row=ENGINE_EXAMPLE_ROW, column=col).value = None

    rows = list(comparison.provisions)
    on_sheet, continued = rows[:ENGINE_ROW_CAP], rows[ENGINE_ROW_CAP:]
    for offset, row in enumerate(on_sheet):
        _write_provision(ws, ENGINE_FIRST_ROW + offset, _provision_cells(offset + 1, row))

    note = ENGINE_EXAMPLE_ROW_NOTE
    if continued:
        extra = wb.create_sheet(ENGINE_CONTINUATION_SHEET)
        last = CONTINUATION_FIRST_ROW + len(continued) - 1
        extra["A1"] = (
            f"{ENGINE_SHEET}, block 2 continued: provisions {ENGINE_ROW_CAP + 1}"
            f" to {len(rows)}"
        )
        for col, header in enumerate(ENGINE_PROVISION_HEADERS, start=1):
            cell = extra.cell(row=CONTINUATION_HEADER_ROW, column=col, value=header)
            source = ws.cell(row=ENGINE_HEADER_ROW, column=col)
            if source.has_style:
                cell._style = copy(source._style)
        for offset, row in enumerate(continued):
            number = ENGINE_ROW_CAP + offset + 1
            _write_provision(
                extra, CONTINUATION_FIRST_ROW + offset, _provision_cells(number, row),
                style_row=ENGINE_FIRST_ROW,
            )
        _add_list_validation(extra, f"E{CONTINUATION_FIRST_ROW}:E{last}", FOUND_BY_VALUES)
        _add_list_validation(extra, f"F{CONTINUATION_FIRST_ROW}:H{last}", DIFFERS_VALUES)
        for letter, dim in ws.column_dimensions.items():
            extra.column_dimensions[letter].width = dim.width
        extra.freeze_panes = f"A{CONTINUATION_FIRST_ROW}"
        quoted = f"'{ENGINE_CONTINUATION_SHEET}'"
        for r, value in ENGINE_COUNTER_ROWS.items():
            ws[f"{ENGINE_COUNTER_COLUMN}{r}"] = (
                f'=COUNTIF(E{ENGINE_FIRST_ROW}:E{ENGINE_LAST_ROW},"{value}")'
                f'+COUNTIF({quoted}!E{CONTINUATION_FIRST_ROW}:E{last},"{value}")'
            )
        ws[f"A{ENGINE_NOTE_ROW}"] = (
            f"{len(rows)} provisions: {ENGINE_ROW_CAP} here, {len(continued)} more on"
            f" sheet {quoted}. The counters below include them."
        )
        note += (
            f"; {len(continued)} provisions past row {ENGINE_LAST_ROW} continue on"
            f" {quoted} and the counters D58:D60 were extended to read them"
        )

    if isinstance(out, (str, Path)):
        Path(out).parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return EngineComparisonResult(
        n_rows=len(rows),
        n_on_sheet=len(on_sheet),
        n_continued=len(continued),
        from_template=from_template,
        note=note,
    )
