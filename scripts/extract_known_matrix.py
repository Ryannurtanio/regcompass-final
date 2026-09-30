"""Extract the provision-level KNOWN matrix and the Baseline Law List from the
ESCAP RDTII 2.1 Round 1 and Round 2 Databases (plus the law-level Legal
Inventory CSV), and write config/known_matrix.json and config/baseline_laws.json.

LOCAL DEV ONLY: the source files live in the parent Knowledge Base repo on the
dev machine and are read-only there; the OUTPUT json is committed so the judge
repo never depends on the parent path. Re-run only when ESCAP reissues a
database, then re-verify the pivotal cells.

Usage:
  uv run --with openpyxl python scripts/extract_known_matrix.py \
      "../Knowledge Base/Databases/ESCAP-RDTII-2.1_ Round 1 Database.xlsx" \
      "../Knowledge Base/Databases/ESCAP-RDTII-2.1_ Round 2 Database.xlsx" \
      "../Knowledge Base/Databases/Singapore, Malaysia, Australia, Legal Inventory.csv"
"""

import csv
import json
import re
import sys
from pathlib import Path

from regcompass.export import instrument_ident, norm_law, norm_url  # noqa: E402  (one normalizer)

# Database file -> sheet -> (Economy code, official name).
ROUND_1_SHEETS = {
    "Australia": ("AU", "Australia"),
    "Malaysia": ("MY", "Malaysia"),
    "Singapore": ("SG", "Singapore"),
}
ROUND_2_SHEETS = {
    "China": ("CN", "China"),
    "India": ("IN", "India"),
    "Indonesia": ("ID", "Indonesia"),
    "Lao PDR": ("LA", "Lao PDR"),
    "Mongolia": ("MN", "Mongolia"),
    "Russian Federation": ("RU", "Russian Federation"),
    "Thailand": ("TH", "Thailand"),
}
# Economies in the live-test pool that no 2025 baseline covers: their rows are
# NEW by definition. Recorded, never invented.
NO_BASELINE = ["KZ", "TL", "VN"]

ROUND_1_SHEET_CODES = [code for code, _ in ROUND_1_SHEETS.values()]

INDICATOR_RE = re.compile(r"^\d{1,2}\.\d{1,2}$")

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


URL_RE = re.compile(r"https?://[^\s;,<>\"']+")
_LAW_SPLIT_RE = re.compile(r"[;；]|\n\s*\n")
_TRAILING_PUNCT = " \t\n,.;:，。；：、"
_YEAR_RE = re.compile(r"(?<!\d)(1[89]\d\d|20\d\d)(?!\d)")
# Buddhist Era years ("B.E.2562", "พ.ศ. 2562") are 543 years ahead.
_BE_YEAR_RE = re.compile(r"(?:\bB\.?\s?E\.?|พ\.\s?ศ\.)\s*(2[45]\d\d)(?!\d)")
_BE_OFFSET = 543

# A title with none of these words, no digit and no non-Latin letters is an
# operator or company name ("UNITEL", "Lao Telecom (LaoTel)"), not a law.
_INSTRUMENT_WORD_RE = re.compile(
    r"\b(?:acts?|laws?|decrees?|regulations?|regulatory|rules?|orders?|measures?|codes?|"
    r"notifications?|decisions?|resolutions?|constitution|directives?|directions?|"
    r"ordinances?|announcements?|circulars?|instructions?|guidelines?|provisions?|"
    r"standards?|agreements?|bills?|statutes?|treaty|treaties|conventions?|polic(?:y|ies)|"
    r"plans?|strateg(?:y|ies)|specifications?|notices?|procedures?|requirements?|"
    r"conditions?|licen[cs]es?|charters?|protocols?|concept|framework|lists?|programmes?|"
    r"programs?|manuals?|handbooks?|masterplan|roadmap|guides?|arrangements?|schemes?|"
    r"forms?|papers?|reports?|evaluations?|v)\b",
    re.I,
)


def clean(cell) -> str:
    return str(cell or "").replace("\xa0", " ").strip()


def clean_title(part: str) -> str:
    return re.sub(r"\s+", " ", part).strip().rstrip(_TRAILING_PUNCT).strip()


def split_laws(acts_cell: str) -> list[str]:
    """One database cell often lists several laws, separated by `;`, `；` or a
    blank line. Each becomes its own title, whitespace collapsed and trailing
    punctuation stripped."""
    out = []
    for part in _LAW_SPLIT_RE.split(acts_cell):
        title = clean_title(part)
        if title and re.search(r"\w", title):
            out.append(title)
    return out


def is_legal_instrument(title: str) -> bool:
    """False only for a title that is clearly a company or operator name: no
    instrument word, no number or year, and no non-Latin letters (a native
    title is never second-guessed)."""
    if _INSTRUMENT_WORD_RE.search(title) or re.search(r"\d", title):
        return True
    return any(ch.isalpha() and not ch.isascii() for ch in title)


def extract_urls(cells) -> list[str]:
    """Every URL in the reference cells, in order, deduplicated, with tracking
    parameters (utm_*) dropped and trailing punctuation trimmed."""
    out: list[str] = []
    for cell in cells:
        for url in URL_RE.findall(clean(cell)):
            url = url.rstrip(".)]")
            url = re.sub(r"[?&]utm_[^&#]*", "", url)
            if url and url not in out:
                out.append(url)
    return out


def law_year(title: str, timeframe: str, single: bool = True) -> int | None:
    """The last year in the title (a Buddhist Era year converted), else, on a
    row citing only this law, the first year in the Timeframe; else None."""
    found = [(m.start(), int(m.group(1))) for m in _YEAR_RE.finditer(title)]
    found += [(m.start(), int(m.group(1)) - _BE_OFFSET) for m in _BE_YEAR_RE.finditer(title)]
    if found:
        return max(found)[1]
    if single:
        years = _YEAR_RE.findall(timeframe)
        return int(years[0]) if years else None
    return None


def pair_urls(laws: list[str], urls: list[str]) -> list[tuple[list[str], str]]:
    """Tie each law of one database row to its URLs. The database lists URLs
    per row, not per law: a single-law row owns all of them ("single"); a
    multi-law row with as many URLs as laws pairs them in order
    ("positional"); otherwise no URL is guessed ("none")."""
    if len(laws) == 1:
        return [(list(urls), "single" if urls else "none")]
    if len(urls) == len(laws):
        return [([u], "positional") for u in urls]
    return [([], "none") for _ in laws]


def reference_columns(header) -> range:
    """The References header plus the unnamed columns after it, up to the
    next named column (Note, Identify types of update, ...)."""
    start = next(i for i, h in enumerate(header) if clean(h).lower().startswith("references"))
    end = start + 1
    while end < len(header) and not clean(header[end]):
        end += 1
    return range(start, end)


def indicator_sort_key(ind: str) -> tuple[int, int]:
    pillar, number = ind.split(".")
    return int(pillar), int(number)


def read_sheet(ws) -> list[dict]:
    """Rows of one Economy sheet: indicator (inherited by continuation rows),
    law titles, comment, timeframe and reference URLs."""
    rows = list(ws.iter_rows(values_only=True))
    refs = reference_columns(rows[0])
    current = None
    out = []
    for r in rows[1:]:
        ind = clean(r[1]) if len(r) > 1 else ""
        if ind:
            current = ind if INDICATOR_RE.match(ind) else None
        if current is None:
            continue
        acts_cell = clean(r[3])
        if not acts_cell:
            continue
        out.append(
            {
                "indicator": current,
                "acts_cell": acts_cell,
                "comment": str(r[5] or "").replace("\xa0", " "),
                "timeframe": clean(r[6]),
                "urls": extract_urls(r[i] for i in refs if i < len(r)),
            }
        )
    return out


def _dedupe(urls: list[str]) -> tuple[list[str], int]:
    """URLs deduplicated by their normalized form (http and https, www. and
    a trailing slash are one URL); the first spelling is kept."""
    seen, out = set(), []
    for url in urls:
        key = norm_url(url) or url
        if key not in seen:
            seen.add(key)
            out.append(url)
    return out, len(urls) - len(out)


def build(
    sheets: dict[str, tuple[str, list[dict]]], legacy: frozenset[str] = frozenset(), stats=None
) -> tuple[dict, dict]:
    """(KNOWN matrix database, Baseline Law List economies) from parsed sheets:
    code -> (official name, rows). Economies in `legacy` keep the matrix's
    long-standing `;` split so the entries it already held never change.
    `stats`, if given, receives per-Economy counts of what each cleanup rule
    removed."""
    database: dict[str, dict[str, list[dict]]] = {}
    economies: dict[str, dict] = {}
    for code, (name, rows) in sheets.items():
        counts = {"not_instrument": 0, "url_duplicates": 0, "contested_positional_urls": 0}
        econ: dict[str, list[dict]] = {}
        laws: dict[str, dict] = {}
        for row_index, row in enumerate(rows):
            sections = parse_sections(row["comment"])
            if code in legacy:
                acts = [a.strip() for a in row["acts_cell"].split(";") if a.strip()]
            else:
                acts = split_laws(row["acts_cell"])
            for act in acts:
                if not is_legal_instrument(act):
                    continue
                econ.setdefault(row["indicator"], []).append(
                    {"law": act, "law_key": norm_law(act), "sections": sections}
                )
            titles = split_laws(row["acts_cell"])
            single = len(titles) == 1
            for title, (urls, pairing) in zip(titles, pair_urls(titles, row["urls"])):
                key = norm_law(title)
                if not key:
                    continue
                if not is_legal_instrument(title):
                    counts["not_instrument"] += 1
                    continue
                law = laws.setdefault(
                    key,
                    {
                        "law": title,
                        "law_key": key,
                        "year": None,
                        "indicators": [],
                        "_single": [],
                        "_positional": [],
                    },
                )
                if law["year"] is None:
                    law["year"] = law_year(title, row["timeframe"], single=single)
                if row["indicator"] not in law["indicators"]:
                    law["indicators"].append(row["indicator"])
                if pairing == "single":
                    law["_single"].extend(u for u in urls if u not in law["_single"])
                else:
                    law["_positional"].extend((row_index, u) for u in urls)
        # Positional pairing is a guess. When a URL it gives one law is also
        # claimed by another law of this Economy, the row's order is off, so
        # every positional URL from that row is dropped. Spellings of one
        # Indonesian instrument count as one law.
        def identity(law: dict) -> str:
            return instrument_ident(code, law["law"]) or law["law_key"]

        claims: dict[str, set[str]] = {}
        for law in laws.values():
            urls = law["_single"] + [u for _, u in law["_positional"]]
            for url in urls:
                claims.setdefault(norm_url(url) or url, set()).add(identity(law))
        suspect_rows = {
            row_index
            for law in laws.values()
            for row_index, url in law["_positional"]
            if claims[norm_url(url) or url] - {identity(law)}
        }
        entries = []
        for law in laws.values():
            single_urls, positional = law.pop("_single"), law.pop("_positional")
            kept = []
            for row_index, url in positional:
                if row_index in suspect_rows:
                    counts["contested_positional_urls"] += 1
                elif url not in kept:
                    kept.append(url)
            single_urls, dropped_single = _dedupe(single_urls)
            urls, dropped = _dedupe(single_urls + kept)
            counts["url_duplicates"] += dropped_single + dropped
            law["indicators"].sort(key=indicator_sort_key)
            law["urls"] = urls
            law["url_pairing"] = (
                "single" if single_urls else "positional" if urls else "none"
            )
            entries.append(law)
        database[code] = {k: econ[k] for k in sorted(econ, key=indicator_sort_key)}
        economies[code] = {"name": name, "laws": entries}
        if stats is not None:
            stats[code] = counts
    return database, economies


def main() -> int:
    import openpyxl

    round1, round2, inventory_path = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    config = Path(__file__).resolve().parents[1] / "config"

    sheets: dict[str, tuple[str, list[dict]]] = {}
    for path, mapping in ((round1, ROUND_1_SHEETS), (round2, ROUND_2_SHEETS)):
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        for sheet, (code, name) in mapping.items():
            sheets[code] = (name, read_sheet(wb[sheet]))
    stats: dict[str, dict[str, int]] = {}
    database, economies = build(sheets, frozenset(ROUND_1_SHEET_CODES), stats)

    inventory: dict[str, list[str]] = {"AU": [], "MY": [], "SG": []}
    country_to_code = {"Australia": "AU", "Malaysia": "MY", "Singapore": "SG"}
    with inventory_path.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            code = country_to_code.get(row["country"].strip())
            if code:
                key = norm_law(row["Act.and.or.practice"])
                if key and key not in inventory[code]:
                    inventory[code].append(key)

    matrix = {
        "_comment": (
            "Provision-level KNOWN matrix (matching at provision granularity). "
            "Derived from the ESCAP RDTII 2.1 Round 1 Database (AU, MY, SG) and Round 2 "
            "Database (CN, IN, ID, LA, MN, RU, TH), all 12 Pillars (per economy/indicator: "
            "cited instruments + section references parsed from the comment prose; an "
            "entry with empty sections is an article-less general reference, where the "
            "law-level match governs) plus the Legal Inventory CSV (law-level normalized "
            "keys, seeding candidates only, never forcing KNOWN on a provision). "
            "no_baseline_economies: no 2025 baseline exists for them, so their rows are NEW. "
            "Generated by scripts/extract_known_matrix.py; regenerate only from a "
            "reissued database and re-verify pivotal cells."
        ),
        "database": database,
        "inventory_law_keys": inventory,
        "no_baseline_economies": NO_BASELINE,
    }
    baseline = {
        "_comment": (
            "Baseline Law List: every law the 2025 RDTII 2.1 baseline cites, per Economy, "
            "with the Indicators whose rows cite it and the reference URLs to try in order "
            "(URLs from rows citing only this law first). url_pairing: single = a row "
            "citing only this law gave the URLs; positional = a multi-law row with one URL "
            "per law, paired in order (a row whose pairing gives one law a URL another law "
            "claims is dropped whole); none = no URL could be tied to the law. Company and "
            "operator names are left out. Years are Gregorian (B.E. converted). Viet Nam and "
            "Kazakhstan are absent: no 2025 baseline exists for them. Generated by "
            "scripts/extract_known_matrix.py from the local Knowledge Base; do not edit by hand."
        ),
        "sources": {
            "round_1_database": {
                "file": round1.name,
                "sheets": {sheet: code for sheet, (code, _) in ROUND_1_SHEETS.items()},
            },
            "round_2_database": {
                "file": round2.name,
                "sheets": {sheet: code for sheet, (code, _) in ROUND_2_SHEETS.items()},
            },
        },
        "economies": economies,
    }
    for name, payload in (("known_matrix.json", matrix), ("baseline_laws.json", baseline)):
        (config / name).write_text(
            json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    for code, econ in database.items():
        laws = economies[code]["laws"]
        pillars = sorted({int(i.split(".")[0]) for i in econ})
        print(
            f"{code}: pillars={pillars} indicators={len(econ)} "
            f"entries={sum(len(v) for v in econ.values())} laws={len(laws)} "
            f"with_urls={sum(1 for law in laws if law['urls'])} "
            f"inventory_laws={len(inventory.get(code, []))} removed={stats[code]}"
        )
    print(f"-> {config / 'known_matrix.json'}\n-> {config / 'baseline_laws.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
