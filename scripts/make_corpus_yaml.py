"""Regenerate config/corpus.yaml from the crawl manifest + documents table
(the post-M10/M11 generator the fixture-era file header promised).

Sources, in precedence order, for each crawled document's law_name:
1. config/round1_corpus_map.json inverted (the reviewed DB-law -> document_ids
   curation): the ESCAP Database law name, with any "(Act NNN)" parenthetical
   moved into law_number_ref. Documents shared by a principal and an amendment
   entry take the principal (shortest name without "(Amendment)").
2. OVERRIDES below for titles the M11 deriver mangled (verified against the
   document's opening text).
3. The M11-derived documents.title, smart-title-cased.

last_amended: derived MECHANICALLY per economy, emitted as 'Month YYYY',
never guessed (blank when no pattern matches). AU: the compilation date from
the register URL path segment (…/C2004A03712/2026-06-04/…), falling back to
the document's own 'Compilation date:' front-matter line. SG/MY: the
document's own front matter via shortlist.derive_last_amended (SG SSO
version-in-force / amendments-up-to lines; MY gazette/assent print dates).

Ordering: build_absence_rows (M9) prefers the corpus entry matching the
coverage_stats law name; when the stats describe a multi-document search it
falls back to the economy's LAST corpus.yaml entry, so each economy's primary
data-protection law is written last (the sensible reference basis).

Run: uv run python scripts/make_corpus_yaml.py
"""

import json
import re
import sys
from pathlib import Path

import yaml

from regcompass.shortlist import derive_last_amended
from regcompass.storage import Storage

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"
DB = ROOT / "data/regcompass.db"

# Each economy's absence-row reference law (written last, see module docstring).
REFERENCE_DOC = {
    "SG": "doc_sg_sso_agc_gov_sg_Act_PDPA2012",
    "AU": "doc_au_C2026C00227",
    "MY": "doc_my_Act_709_ori",
}

# Verified against the document's opening text where the M11 title deriver
# dropped or mangled lines.
OVERRIDES = {
    "doc_my_Act_A1422": "Criminal Procedure Code (Amendment) Act 2010 (Amendment) Act 2012",
    "doc_my_ACT_593_(2)": "Criminal Procedure Code",
}

_PAREN_ACT = re.compile(r"\s*\((Act\s+[A-Z]?\d+)\)")
_AU_URL_DATE = re.compile(r"/(\d{4}-\d{2}-\d{2})/")
_SMALL_WORDS = {"and", "of", "the", "for", "to", "in", "on", "no"}
MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def smart_title(raw: str) -> str:
    """Title-case an all-caps or OCR-mangled act title, act-name style."""
    words = raw.split()
    out = []
    for i, w in enumerate(words):
        core = w.strip("()")
        if core.lower() in _SMALL_WORDS and i != 0:
            fixed = w.lower()
        elif re.fullmatch(r"[A-Za-z]+", core):
            fixed = w[: w.index(core[0])] + core.capitalize() + w[w.index(core[0]) + len(core):]
        else:
            fixed = w  # numbers, act codes: leave alone
        out.append(fixed)
    return " ".join(out)


def invert_corpus_map() -> dict[str, str]:
    """document_id -> Database law name; principals beat their amendments."""
    m = json.loads((CONFIG / "round1_corpus_map.json").read_text(encoding="utf-8"))
    best: dict[str, str] = {}
    for eco, laws in m.items():
        if eco.startswith("_"):
            continue
        for entry in laws.values():
            for doc_id in entry.get("document_ids") or []:
                name = entry["law"]
                cur = best.get(doc_id)
                if cur is None:
                    best[doc_id] = name
                    continue
                # Prefer the principal: no "(Amendment)", then shortest.
                def rank(n: str) -> tuple[int, int]:
                    return ("(amendment)" in n.lower(), len(n))
                if rank(name) < rank(cur):
                    best[doc_id] = name
    return best


def law_fields(doc_id: str, title: str | None, map_names: dict[str, str]) -> tuple[str, str | None]:
    """(law_name, law_number_ref) for one crawled document."""
    number = None
    if doc_id in map_names:
        name = map_names[doc_id]
        m = _PAREN_ACT.search(name)
        if m:
            number = m.group(1)
            name = _PAREN_ACT.sub("", name).strip()
    elif doc_id in OVERRIDES:
        name = OVERRIDES[doc_id]
    else:
        name = smart_title(title or doc_id)
    if number is None and doc_id.startswith("doc_my_"):
        # MY act numbers are 3-digit principals (709) or A-numbered amendments
        # (A1422); 4-digit tokens without the A prefix are years, never numbers.
        tokens = re.split(r"[_\-()]+", doc_id)
        a_nums = [t for t in tokens if re.fullmatch(r"A\d{4}", t)]
        principals = [t for t in tokens if re.fullmatch(r"\d{3}", t)]
        if a_nums:
            number = f"Act {a_nums[0]}"
        elif principals:
            number = f"Act {principals[0]}"
    return name, number


def main() -> int:
    existing = yaml.safe_load((CONFIG / "corpus.yaml").read_text(encoding="utf-8"))
    fixture_docs = {
        k: v for k, v in existing["documents"].items()
        if k in (
            "doc_sg_telecommunications_act_1999",
            "doc_my_personal_data_protection_act_2010",
            "doc_au_C2026C00098VOL01",
        )
    }
    map_names = invert_corpus_map()
    storage = Storage(DB)
    documents: dict[str, dict] = dict(fixture_docs)
    for eco in ("SG", "AU", "MY"):
        rows = [r for r in storage.documents_for_economy(eco)]
        ref = REFERENCE_DOC[eco]
        rows.sort(key=lambda r: (r["document_id"] == ref, r["document_id"]))
        for r in rows:
            doc_id = r["document_id"]
            name, number = law_fields(doc_id, r["title"], map_names)
            last_amended = None
            if eco == "AU":
                m = _AU_URL_DATE.search(r["source_url"] or "")
                if m:
                    y, mo, _d = m.group(1).split("-")
                    last_amended = f"{MONTH_NAMES[int(mo) - 1]} {y}"
            if last_amended is None:
                last_amended = derive_last_amended(r["full_text"] or "", eco)
            documents[doc_id] = {
                "economy": eco,
                "law_name": name,
                "law_number_ref": number,
                "last_amended": last_amended,
                "source_url": r["source_url"],
                "url_is_direct": True,
            }
    header = (
        "# Document-level metadata for the export (M9 columns 1-4 and 11).\n"
        "# GENERATED by scripts/make_corpus_yaml.py from the crawl manifest +\n"
        "# documents table + config/round1_corpus_map.json (law names). Do not\n"
        "# hand-edit crawled entries; fix the generator and re-run. The three\n"
        "# fixture-era entries are preserved verbatim at the top. Entry ORDER\n"
        "# matters as a fallback: build_absence_rows uses each economy's LAST\n"
        "# entry as the reference law when coverage_stats names no single corpus\n"
        "# law (each economy's primary data-protection law is written last).\n"
        "# last_amended ('Month YYYY', mechanical, never guessed): AU = the\n"
        "# compilation date (register URL, else the document's own front\n"
        "# matter); SG = the SSO version-in-force / amendments-up-to date;\n"
        "# MY = the gazette print's publication (or assent) date. Blank when\n"
        "# the document declares none.\n"
    )
    out = header + yaml.safe_dump({"documents": documents}, sort_keys=False, allow_unicode=True, width=100)
    (CONFIG / "corpus.yaml").write_text(out, encoding="utf-8")
    print(f"wrote {len(documents)} entries ({len(fixture_docs)} fixture + {len(documents) - len(fixture_docs)} crawled)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
