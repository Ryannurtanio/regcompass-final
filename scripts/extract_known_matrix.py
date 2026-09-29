"""Extract the provision-level KNOWN matrix from the ESCAP Round 1 Database
xlsx plus the law-level Legal Inventory CSV, and write config/known_matrix.json.

LOCAL DEV ONLY: the source files live in the parent Knowledge Base repo on the
dev machine and are read-only there; the OUTPUT json is committed so the judge
repo never depends on the parent path. Re-run only when ESCAP reissues the
database, then re-verify the pivotal cells.

Usage:
  uv run --with openpyxl python scripts/extract_known_matrix.py \
      "../Knowledge Base/Databases/ESCAP-RDTII-2.1_ Round 1 Database.xlsx" \
      "../Knowledge Base/Databases/Singapore, Malaysia, Australia, Legal Inventory.csv"
"""

import csv
import json
import re
import sys
from pathlib import Path

SHEET_TO_CODE = {"Australia": "AU", "Malaysia": "MY", "Singapore": "SG"}
IN_SCOPE = {"6.1", "6.2", "6.3", "6.4", "7.1", "7.2", "7.3", "7.4", "7.5"}

# Section references in the database's prose comments: "Section 129", "s 45",
# "Sections 15-24", "(section 39 and 40)", "Article 26(2)", "Section 12a" etc.
# A keyword introduces a RUN of tokens joined by commas / "and" / "&" / range
# dashes; the old single-token capture dropped everything after the first
# number ("39 and 40" -> {39}, "15-24" -> {15}), which shipped false NEW tags.
_SECTION_TOKEN = r"\d+[A-Za-z]{0,3}(?:\(\d+[a-z]?\))?"
_SECTION_SEP = r"(?:\s*,\s*|\s*&\s*|\s+and\s+|\s*(?:-|–|—)\s*|\s+to\s+)"
SECTION_RUN_RE = re.compile(
    r"\b(?:sections?|s\.?|arts?\.?|articles?)\s+"
    rf"({_SECTION_TOKEN}(?:{_SECTION_SEP}{_SECTION_TOKEN})*)",
    re.I,
)
_ITEM_SEP_RE = re.compile(r"\s*,\s*|\s*&\s*|\s+and\s+", re.I)
_RANGE_RE = re.compile(r"^\s*(\d+)\s*(?:-|–|—|to)\s*(\d+)\s*$", re.I)
_RANGE_SEP_RE = re.compile(r"\s*(?:-|–|—)\s*|\s+to\s+", re.I)
_TOKEN_HEAD_RE = re.compile(rf"^{_SECTION_TOKEN}")

# Ranges wider than this are treated as citing only their endpoints: a huge
# span is more likely prose noise than a genuine enumerable citation.
_MAX_RANGE_SPAN = 60


def parse_sections(comment: str) -> list[str]:
    """All section tokens cited in a comment, with conjunction lists split and
    plain-integer ranges expanded ("Sections 15-24" -> 15..24). Letter-suffixed
    and subsection tokens (317ZH, 45(2)) are kept verbatim, never expanded."""
    out: set[str] = set()
    for m in SECTION_RUN_RE.finditer(comment):
        for part in (p.strip() for p in _ITEM_SEP_RE.split(m.group(1)) if p.strip()):
            rng = _RANGE_RE.match(part)
            if rng:
                lo, hi = int(rng.group(1)), int(rng.group(2))
                if 0 < hi - lo <= _MAX_RANGE_SPAN:
                    out.update(str(n) for n in range(lo, hi + 1))
                else:
                    out.update({rng.group(1), rng.group(2)})
            else:
                # Not a plain-integer range: a dash pair of letter-suffixed
                # tokens ("12A-12C") keeps both endpoints, never expands.
                for piece in _RANGE_SEP_RE.split(part):
                    tok = _TOKEN_HEAD_RE.match(piece.strip())
                    if tok:
                        out.add(tok.group(0))
    return sorted(out)


def norm_law(name: str) -> str:
    """Normalized law-name key: lowercase, parentheticals and punctuation
    stripped, whitespace collapsed. Containment on these keys is the law match."""
    name = re.sub(r"\([^)]*\)", " ", name)
    name = re.sub(r"[^a-z0-9 ]", " ", name.lower())
    return re.sub(r"\s+", " ", name).strip()


def main() -> int:
    import openpyxl

    xlsx_path, inventory_path = Path(sys.argv[1]), Path(sys.argv[2])
    out_path = Path(__file__).resolve().parents[1] / "config/known_matrix.json"

    matrix: dict[str, dict[str, list[dict]]] = {}
    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    for sheet, code in SHEET_TO_CODE.items():
        ws = wb[sheet]
        rows = list(ws.iter_rows(min_col=1, max_col=8, values_only=True))
        current = None
        econ: dict[str, list[dict]] = {}
        for r in rows[1:]:
            ind = str(r[1]).strip() if r[1] not in (None, "") else None
            if ind:
                current = ind
            if current not in IN_SCOPE:
                continue
            acts_cell = str(r[3] or "").replace("\xa0", " ").strip()
            comment = str(r[5] or "").replace("\xa0", " ")
            if not acts_cell:
                continue
            sections = parse_sections(comment)
            for act in [a.strip() for a in acts_cell.split(";") if a.strip()]:
                econ.setdefault(current, []).append(
                    {"law": act, "law_key": norm_law(act), "sections": sections}
                )
        matrix[code] = econ

    inventory: dict[str, list[str]] = {"AU": [], "MY": [], "SG": []}
    country_to_code = {"Australia": "AU", "Malaysia": "MY", "Singapore": "SG"}
    with inventory_path.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            code = country_to_code.get(row["country"].strip())
            if code:
                key = norm_law(row["Act.and.or.practice"])
                if key and key not in inventory[code]:
                    inventory[code].append(key)

    payload = {
        "_comment": (
            "Provision-level KNOWN matrix (matching at provision granularity). "
            "Derived from the ESCAP RDTII 2.1 Round 1 Database (per economy/indicator: "
            "cited instruments + section references parsed from the comment prose; an "
            "entry with empty sections is an article-less general reference, where the "
            "law-level match governs) plus the Legal Inventory CSV (law-level normalized "
            "keys, seeding candidates only, never forcing KNOWN on a provision). "
            "Generated by scripts/extract_known_matrix.py; regenerate only from a "
            "reissued database and re-verify pivotal cells."
        ),
        "database": matrix,
        "inventory_law_keys": inventory,
    }
    out_path.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    for code, econ in matrix.items():
        n_entries = sum(len(v) for v in econ.values())
        n_articleless = sum(1 for v in econ.values() for e in v if not e["sections"])
        print(
            f"{code}: indicators={sorted(econ)} entries={n_entries} "
            f"article-less={n_articleless} inventory_laws={len(inventory[code])}"
        )
    print(f"-> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
