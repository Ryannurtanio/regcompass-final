"""Generate config/round1_corpus_map.json: the reviewed mapping from every law
the ESCAP Round 1 Database cites (config/known_matrix.json, economies SG/AU/MY,
pillars 6-7) to the crawled corpus documents, so the M11 recall/precision gates
have a mechanical ground truth.

Statuses:
- in_corpus:    the instrument itself was crawled; document_ids lists every
                corpus document that IS that instrument (multi-volume acts map
                to all their volumes).
- consolidated: the instrument is an amendment whose provisions live inside a
                crawled consolidation; document_ids points there.
- not_available: the instrument class is not hosted on the whitelisted statute
                portal (codes of practice, guidelines, licences, strategies)
                or the portal's own index lacks it; reason says which.

Matching is mechanical (act-number tokens, then normalized-name containment
against ingested document titles); the OVERRIDES table below is the reviewed
curation for everything mechanical matching cannot decide, each entry with its
reason. Regenerate after any corpus change: uv run python scripts/make_corpus_map.py

The output is COMMITTED and validated by tests/test_shortlist.py::TestCorpusMap.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from regcompass.config import load_known_matrix  # noqa: E402
from regcompass.storage import Storage  # noqa: E402

DB = ROOT / "data/regcompass.db"
OUT = ROOT / "config/round1_corpus_map.json"

# Reviewed curation (5 Jul 2026). Key: (economy, law_key as it appears in
# known_matrix.json). Every entry states why it cannot be a plain title match.
OVERRIDES: dict[tuple[str, str], dict] = {
    # -- SG: instruments not hosted on sso.agc.gov.sg -----------------------
    # (keys are the law_key values exactly as known_matrix.json spells them)
    ("SG", "personal data protection act 2020"): {
        # 'Personal Data Protection (Amendment) Act 2020'
        "status": "consolidated",
        "match_titles": ["PERSONAL DATA PROTECTION ACT 2012"],
        "reason": "amendment consolidated into the current SSO revision of the PDPA 2012;"
        " SSO serves consolidations only",
    },
    ("SG", "licence to provide facilities based operations granted by the info communications media development authority to singapore telecommunications limited under section 5 of the telecommunications act"): {
        "status": "not_available",
        "reason": "IMDA licence document, not hosted on the SSO statute portal (whitelist)",
    },
    ("SG", "specific terms and conditions for ip telephony services"): {
        "status": "not_available",
        "reason": "IMDA licence terms, not hosted on the SSO statute portal (whitelist)",
    },
    ("SG", "guide to data protection impact assessments"): {
        "status": "not_available",
        "reason": "PDPC guidance document, not hosted on the SSO statute portal (whitelist)",
    },
    ("SG", "advisory guidelines on the pdpa for children s personal data in the digital environment"): {
        "status": "not_available",
        "reason": "PDPC advisory guidelines, not hosted on the SSO statute portal (whitelist)",
    },
    # -- AU: non-register instruments ---------------------------------------
    ("AU", "2023 2030 australian cyber security strategy"): {
        "status": "not_available",
        "reason": "government strategy paper, not an instrument on the Federal Register",
    },
    ("AU", "privacy impact assessment 2020"): {
        "status": "not_available",
        "reason": "OAIC guidance document, not an instrument on the Federal Register",
    },
    # -- MY: instruments not on lom.agc.gov.my ------------------------------
    ("MY", "personal data protection code of practice for banking sector and financial institutions 2017"): {
        "status": "not_available",
        "reason": "PDP Commissioner code of practice, not hosted on the lom statute portal",
    },
    ("MY", "personal data protection code of practice for licensees under the communications and multimedia of 2017"): {
        "status": "not_available",
        "reason": "PDP Commissioner code of practice, not hosted on the lom statute portal",
    },
    ("MY", "personal data protection standard 2015"): {
        "status": "not_available",
        "reason": "PDP Commissioner standard, not hosted on the lom statute portal",
    },
    ("MY", "criminal procedure code 2018"): {
        "status": "in_corpus",
        "match_titles": ["ACT 593 (2)", "CRIMINAL PROCEDURE CODE (AMENDMENT) (NO. 2) ACT 2012"],
        "reason": "the portal's principal Act 593 PDF is an OUTDATED pre-2012 revision"
        " (no s.116B-116C computerized-data access provisions, verified 5 Jul 2026);"
        " the cited lawful-access text entered the corpus via amendment A1431, so"
        " both documents represent the citation",
    },
    ("MY", "services tax act 2018"): {
        "status": "in_corpus",
        "match_titles": ["SERVICE TAX ACT 2018"],
        "reason": "the portal's official BI title is 'SERVICE TAX ACT 2018' (singular);"
        " the ESCAP database prints the plural (docs/PORTALS.md naming trap)",
    },
    ("MY", "income tax act 1967"): {
        "status": "not_available",
        "reason": "principal Act 53 has no entry with a PDF in the lom search index"
        " (verified 5 Jul 2026; only amendment acts surface)",
    },
}

_PAREN_ACT = re.compile(r"\((?:act\s*)?(a?\d{2,4})\)", re.I)
_NOISE = re.compile(r"[^a-z0-9 ]+")


def norm(s: str) -> str:
    s = " ".join(s.split()).casefold()
    s = _PAREN_ACT.sub(" ", s)
    s = s.replace("bill", "act")  # the DB styles A1727 a Bill; the gazetted doc is an Act
    return " ".join(_NOISE.sub(" ", s).split())


_YEAR = re.compile(r"(?:1[89]|20)\d\d")


def act_tokens(s: str) -> set[str]:
    """Act-number tokens like 709 / a1727; years are NOT act numbers."""
    return {
        m.casefold() for m in _PAREN_ACT.findall(" ".join(s.split()))
        if not _YEAR.fullmatch(m)
    }


def main() -> int:
    matrix = load_known_matrix()["database"]
    storage = Storage(DB)
    docs = {
        eco: storage.documents_for_economy(eco) for eco in matrix
    }

    result: dict = {
        "_comment": (
            "Round 1 Database law -> corpus document mapping (M11 eval ground truth). "
            "Generated by scripts/make_corpus_map.py from config/known_matrix.json + the "
            "ingested documents table; the not_available/consolidated entries are the "
            "reviewed curation in the script's OVERRIDES table. Regenerate after any "
            "corpus change and re-review."
        )
    }
    unresolved = []
    for eco, inds in sorted(matrix.items()):
        entries: dict = {}
        for ind, laws in sorted(inds.items()):
            for law in laws:
                key = law["law_key"]
                if key in entries:
                    continue
                override = OVERRIDES.get((eco, key))
                if override and override["status"] == "not_available":
                    entries[key] = {
                        "law": " ".join(law["law"].split()),
                        "status": "not_available",
                        "document_ids": [],
                        "reason": override["reason"],
                    }
                    continue
                targets = norm(law["law"]) if not override else None
                match_titles = override["match_titles"] if override else None
                matched = []
                law_acts = act_tokens(law["law"])
                for row in docs[eco]:
                    title = row["title"] or ""
                    if match_titles is not None:
                        if title in match_titles:
                            matched.append(row["document_id"])
                        continue
                    title_n = norm(title)
                    # act-number token match (MY A1727, Act 709 style)
                    title_acts = act_tokens(title) | {
                        t for t in title_n.split()
                        if re.fullmatch(r"a?\d{3,4}", t) and not _YEAR.fullmatch(t)
                    }
                    if law_acts and law_acts & title_acts:
                        matched.append(row["document_id"])
                        continue
                    # normalized containment either way
                    if title_n and (title_n in targets or targets in title_n):
                        matched.append(row["document_id"])
                if matched:
                    entries[key] = {
                        "law": " ".join(law["law"].split()),
                        "status": override["status"] if override else "in_corpus",
                        "document_ids": sorted(matched),
                        "reason": override["reason"] if override else None,
                    }
                else:
                    entries[key] = {
                        "law": " ".join(law["law"].split()),
                        "status": "UNRESOLVED",
                        "document_ids": [],
                        "reason": None,
                    }
                    unresolved.append((eco, key))
        result[eco] = entries

    OUT.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    n_laws = sum(len(v) for k, v in result.items() if not k.startswith("_"))
    print(f"wrote {OUT} ({n_laws} laws)")
    if unresolved:
        print("UNRESOLVED (review needed):")
        for eco, key in unresolved:
            print(f"  {eco}: {key}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
