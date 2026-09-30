"""Deleting rows from an organizers' sheet the way a spreadsheet application
does.

openpyxl's delete_rows moves cell values and styles up and nothing else: every
formula keeps pointing at the old rows, and row heights, data validations,
conditional formats, the autofilter, merged ranges and frozen panes stay where
they were. The Output Data sheet's Pillar formulas, the Coverage Matrix
COUNTIFS and the four validations all address the entry rows, so deleting the
two example rows without the rest would silently break the counts. This
module moves all of them with the rows.
"""

from __future__ import annotations

import re

from openpyxl.formatting.formatting import ConditionalFormattingList
from openpyxl.formula.tokenizer import Token, Tokenizer
from openpyxl.worksheet.cell_range import CellRange, MultiCellRange

_CELL_RE = re.compile(r"^(\$?[A-Z]{1,3})(\$?)(\d+)$")


def _shift_row(row: int, first: int, count: int) -> int:
    return row - count if row >= first + count else row


def _shift_ref(ref: str, first: int, count: int) -> str:
    """One A1 reference or range, rows at or below the deleted block moved up."""
    parts = []
    for part in ref.split(":"):
        m = _CELL_RE.match(part)
        if m is None:  # a whole column (A:A) or something that is not a cell
            parts.append(part)
            continue
        col, dollar, row = m.groups()
        parts.append(f"{col}{dollar}{_shift_row(int(row), first, count)}")
    return ":".join(parts)


def _sheet_of(operand: str) -> tuple[str | None, str]:
    if "!" not in operand:
        return None, operand
    sheet, ref = operand.rsplit("!", 1)
    return sheet.strip("'").replace("''", "'"), ref


def shift_formula(formula: str, own_sheet: str, target: str, first: int, count: int) -> str:
    """`formula` (on sheet `own_sheet`) with every reference into sheet
    `target` moved as if rows first..first+count-1 of `target` were deleted."""
    if not isinstance(formula, str) or not formula.startswith("="):
        return formula
    tok = Tokenizer(formula)
    for t in tok.items:
        if t.type != Token.OPERAND or t.subtype != Token.RANGE:
            continue
        sheet, ref = _sheet_of(t.value)
        if (sheet or own_sheet) != target:
            continue
        prefix = t.value[: len(t.value) - len(ref)]
        t.value = prefix + _shift_ref(ref, first, count)
    return "=" + "".join(t.value for t in tok.items)


def _shift_sqref(sqref, first: int, count: int) -> MultiCellRange:
    return MultiCellRange(
        " ".join(_shift_ref(str(r.coord), first, count) for r in MultiCellRange(str(sqref)).ranges)
    )


def delete_rows(wb, sheet: str, first: int, count: int) -> None:
    """Delete rows first..first+count-1 of `sheet` in workbook `wb` and move
    everything that addresses the rows below them up by `count`."""
    ws = wb[sheet]
    heights = {r: dim.height for r, dim in list(ws.row_dimensions.items())}
    # merges above the deleted rows stay exactly as they are
    merged = [str(m) for m in ws.merged_cells.ranges if m.max_row >= first]
    for m in merged:
        ws.unmerge_cells(m)

    ws.delete_rows(first, count)

    # formulas, on this sheet and on every sheet that reads it
    for other in wb.worksheets:
        for row in other.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and cell.value.startswith("="):
                    cell.value = shift_formula(cell.value, other.title, sheet, first, count)

    # row heights: the rows below take their own heights up with them
    for r in range(first, max(heights, default=0) + 1):
        ws.row_dimensions[r].height = heights.get(r + count)

    for m in merged:
        rng = CellRange(m)
        if rng.max_row >= first and rng.min_row < first + count:
            continue  # a merge inside the deleted rows goes with them
        ws.merge_cells(_shift_ref(m, first, count))

    for dv in ws.data_validations.dataValidation:
        dv.sqref = _shift_sqref(dv.sqref, first, count)
        for attr in ("formula1", "formula2"):
            value = getattr(dv, attr)
            if value:
                setattr(dv, attr, shift_formula("=" + value, sheet, sheet, first, count)[1:])

    old = ws.conditional_formatting
    ws.conditional_formatting = ConditionalFormattingList()
    for cf in old:
        target = str(_shift_sqref(cf.sqref, first, count))
        for rule in cf.rules:
            rule.formula = [
                shift_formula("=" + f, sheet, sheet, first, count)[1:] for f in (rule.formula or [])
            ]
            ws.conditional_formatting.add(target, rule)

    if ws.auto_filter.ref:
        ws.auto_filter.ref = _shift_ref(ws.auto_filter.ref, first, count)
    if ws.freeze_panes:
        ws.freeze_panes = _shift_ref(ws.freeze_panes, first, count)
