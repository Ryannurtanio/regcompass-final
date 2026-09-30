"""M11 shortlist: rank the crawled corpus per (economy, pillar) for review order.

Input: the M10 crawl manifest + the exact bytes under data/<economy>/raw/.
Output: one ranked CSV per (economy, pillar) with the exact columns
`rank, document_id, title, source_url, relevance_score, matched_keywords`,
plus the extracted canonical text (documents.full_text) and per-document
window embeddings (shortlist_windows) persisted in SQLite.

Ranking is REVIEW ORDER, never exclusion: every ingested document appears in
the CSV; a document that cannot be ingested (unreadable bytes, sha mismatch)
is recorded in the exclusion log with a reason, never silently dropped.

Scoring reuses the M5 components at document level (calibrated on the real
corpus against the Round 1 Database):
- semantic (weight 0.45): BGE-M3 cosine between the pillar description and the
  document's fixed-size character windows (max over windows, so a 500-page
  statute is represented across its whole length, not just its front matter);
- lexical (weight 0.25): bm25s over the M5 legal tokenizer AT WINDOW LEVEL
  (every 4000-char window, 50% overlap, is one bm25 document), one query PER
  INDICATOR of the pillar; a document scores its best window, rank-normalized
  across the economy's documents, max over the pillar's indicators. Window
  scoring removes the sheer-size advantage a 1300-page tax act has over a
  concentrated localization provision;
- phrase (weight 0.3): curated vocabulary phrases CO-OCCURRING inside one
  4000-char window, per indicator, saturating at 3 distinct phrases; max over
  windows, then over the pillar's indicators. Window-level co-occurrence is
  the homonym filter a document-level bag of words cannot be: "conditions for
  transfer" in a share-transfer part of a tax act shares no window with data
  language, while a real transfer provision packs several vocabulary phrases
  into one section. A phrase present in more than half the economy's documents
  carries no discrimination ("outside Singapore" is in every SG act) and is
  not counted, mirroring bm25's idf at phrase level.
matched_keywords lists the phrases literally present anywhere in the pillar
vocabulary: the keyword prefilter as an ANNOTATION, since with ranking
required to keep every document, a prefilter may inform but never drop.
"""

from __future__ import annotations

import csv
import hashlib
import re
import time
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np

from regcompass.config import (
    CONFIG_DIR,
    load_keywords,
    load_pillar_description,
    load_pillars,
    load_portals,
)
from regcompass.contracts import (
    CanonicalText,
    PipelineConfig,
    ShortlistRow,
    manifest_address,
)
from regcompass.extract import (
    extract_with_stats,
    extraction_key,
    load_extraction,
    ocr_quality_columns,
    sniff_format,
    store_extraction,
)
from regcompass.gate import EmbedFn, embed_ollama, legal_tokens
from regcompass.languages import (
    NON_LATIN_SCRIPT_LANGUAGES,
    garbage_ocr_languages,
    non_latin_share,
    ocr_policy,
    tesseract_languages,
)
from regcompass.observability import log_stage
from regcompass.paths import resolve_stored_path, storable_local_path
from regcompass.storage import Storage

WINDOW_CHARS = 4000  # same embedding-input size the M5 gate uses per chunk
MAX_WINDOWS = 64  # cap per document; over the cap windows are evenly spaced
OCR_PAGE_FRACTION = 0.5  # more than half the pages low-yield -> scanned, OCR lane

CSV_COLUMNS = ("rank", "document_id", "title", "source_url", "relevance_score", "matched_keywords")


# ---------------------------------------------------------------------------
# reports
# ---------------------------------------------------------------------------


@dataclass
class IngestResult:
    document_id: str
    title: str
    ocr_applied: bool
    n_pages: int
    n_low_yield_pages: int
    n_chars: int

    @property
    def page_coverage(self) -> float:
        if self.n_pages == 0:
            return 0.0
        return 1.0 - self.n_low_yield_pages / self.n_pages


@dataclass
class ShortlistReport:
    economy: str
    pillar: int
    n_documents: int = 0
    csv_path: str = ""
    duration_s: float = 0.0
    excluded: list = field(default_factory=list)
    notes: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# titles
# ---------------------------------------------------------------------------

_HTML_TITLE = re.compile(rb"<title[^>]*>(.*?)</title>", re.I | re.S)
_SG_SUFFIX = " - Singapore Statutes Online"
# A statute heading line: an instrument word somewhere, a 4-digit year at the
# end, only name-like characters, plausible length. Covers "Privacy Act 1988",
# "Telecommunications Regulations 2021", "PERSONAL DATA PROTECTION ACT 2010",
# "AKTA KESELAMATAN SIBER 2024", "Criminal Procedure Code 2010".
_LINE_SHAPE = re.compile(r"^[A-Za-z][A-Za-z0-9 ,()'’./-]{5,119}$")
_INSTRUMENT = re.compile(r"\b(?:acts?|regulations?|code|akta|kanun|ordinance)\b", re.I)
_YEAR_END = re.compile(r"\b(?:1[89]\d\d|20\d\d)$")
_TITLE_SCAN_LINES = 60


# Jurisdiction banner lines printed above the act name on official covers;
# never part of the title and never joined into one.
_BANNERS = frozenset(
    {"the statutes of the republic of singapore", "laws of malaysia", "undang-undang malaysia"}
)


def _is_heading(text: str) -> str | None:
    text = re.sub(r"\s+", " ", text).strip()
    if text.lower().startswith("the "):
        text = text[4:]
    if text.count("(") != text.count(")"):
        return None  # a wrapped fragment, not a complete title
    if not (_LINE_SHAPE.match(text) and _INSTRUMENT.search(text) and _YEAR_END.search(text)):
        return None
    # a title needs a content word beyond the instrument word and numbers:
    # a bare "ACT 2012" is the wrapped tail of a name, not a name
    content = [
        t for t in re.sub(r"[^A-Za-z0-9 ]+", " ", text).split()
        if not t.isdigit() and not _INSTRUMENT.fullmatch(t)
    ]
    return text if content else None


def _find_heading(lines: list[str]) -> str | None:
    """Statute titles wrap across up to 3 lines on AU register cover pages
    ("Surveillance Legislation Amendment" / "(Identify and Disrupt) Act 2021",
    or the year alone on its own line). Join 1-3 consecutive lines and test
    the joined text. A non-last constituent may not end with a digit: that
    keeps short refs ("Act 709") and register noise ("No. 98, 2021",
    "Authorised Version C2021A00098") out of joins while allowing genuine
    wrapped fragments, which end mid-phrase."""
    cleaned = [re.sub(r"\s+", " ", ln).strip() for ln in lines]
    for i in range(len(cleaned)):
        for j in (1, 2, 3):
            parts = cleaned[i : i + j]
            if len(parts) < j or any(not p for p in parts):
                break
            if any(p.casefold() in _BANNERS for p in parts):
                continue
            if j > 1 and any(p[-1].isdigit() or p[-1] in ".:;" for p in parts[:-1]):
                continue  # short refs ("Act 709"), register noise, full sentences
            if j > 1 and len(parts[0].split()) < 2:
                continue  # one-word lines ("WARTA") are headers, not wrapped fragments
            if j > 1 and _is_heading(parts[-1]):
                continue  # the last line stands alone ("PERSONAL DATA PROTECTION ACT
                # 2010" under "LAWS OF MALAYSIA"): prefer it at its own start index
            found = _is_heading(" ".join(parts))
            if found:
                return found
    return None


def derive_title(raw: bytes, format_tag: str, full_text: str, filename_hint: str | None) -> str:
    """Mechanical title derivation: HTML <title> tag (SG portal suffix
    stripped), else the first statute-heading line of the extracted text,
    else the filename hint stem. Never empty, never model-generated."""
    if format_tag == "html":
        m = _HTML_TITLE.search(raw)
        if m:
            title = re.sub(r"\s+", " ", m.group(1).decode("utf-8", errors="replace")).strip()
            if title.endswith(_SG_SUFFIX):
                title = title[: -len(_SG_SUFFIX)]
            if title:
                return title
    found = _find_heading(full_text.split("\n")[:_TITLE_SCAN_LINES])
    if found:
        return found
    stem = (filename_hint or "document").rsplit(".", 1)[0]
    return re.sub(r"[_\s]+", " ", stem).strip() or "document"


# ---------------------------------------------------------------------------
# last-amended derivation (front-matter dates, never guessed)
# ---------------------------------------------------------------------------

_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
_TEXT_DATE = r"(\d{1,2})\s+(" + "|".join(_MONTHS) + r")\s+(\d{4})"
_FRONT_MATTER_CHARS = 3000

# SG: SSO consolidation front matter. The informal-consolidation line names
# the date the CURRENT version came into force (dd/mm/yyyy, the SSO print
# convention); the revised-edition line is the older fallback.
_SG_IN_FORCE = re.compile(r"version\s+in\s+force\s+from\s+(\d{1,2})/(\d{1,2})/(\d{4})", re.I)
_SG_AMENDMENTS_UP_TO = re.compile(r"amendments\s+up\s+to\s+and\s+including\s+" + _TEXT_DATE, re.I)

# MY: lom.agc.gov.my gazette prints. Layout and OCR noise vary (dot leaders,
# 'pubiication', date before or after the word Gazette, day/month/year split
# across lines), so the pattern anchors on the label and takes the first
# textual date within a short non-digit window after it.
_MY_GAZETTE = re.compile(r"pub\w{0,3}ication\s+in\s+the\D{0,60}?" + _TEXT_DATE, re.I | re.S)
_MY_ASSENT = re.compile(r"Royal\W{1,5}Assent\D{0,60}?" + _TEXT_DATE, re.I | re.S)

# AU: Federal Register compilations state their own compilation date.
_AU_COMPILATION = re.compile(r"Compilation\s+date:\s*" + _TEXT_DATE, re.I)


def derive_last_amended(full_text: str, economy: str) -> str | None:
    """Mechanical 'Last Amended' derivation from the document's OWN front
    matter, emitted as 'Month YYYY' (the phrasing the organizers' guidance
    rewards). The semantic is: the amendment state of the cited source
    document as it declares it. SG = the date the consolidated version in
    force was made / amendments-up-to date; MY = the gazette publication
    (or assent) date of the print we hold; AU = the compilation date.
    No pattern match = None, NEVER a guess."""
    head = full_text[:_FRONT_MATTER_CHARS]
    if economy == "SG":
        m = _SG_IN_FORCE.search(head)
        if m:
            month = int(m.group(2))
            if 1 <= month <= 12:
                return f"{_MONTHS[month - 1]} {m.group(3)}"
        m = _SG_AMENDMENTS_UP_TO.search(head)
        if m:
            return f"{m.group(2)} {m.group(3)}"
    elif economy == "MY":
        m = _MY_GAZETTE.search(head) or _MY_ASSENT.search(head)
        if m:
            return f"{m.group(2)} {m.group(3)}"
    elif economy == "AU":
        m = _AU_COMPILATION.search(head)
        if m:
            return f"{m.group(2)} {m.group(3)}"
    return None


# ---------------------------------------------------------------------------
# windows
# ---------------------------------------------------------------------------


def window_spans(
    n_chars: int, window: int = WINDOW_CHARS, max_windows: int = MAX_WINDOWS
) -> list[tuple[int, int]]:
    """Deterministic character windows over a stream of n_chars. Consecutive
    and complete when they fit in max_windows; otherwise exactly max_windows
    windows of `window` chars with evenly spaced starts (coverage sampling)."""
    if n_chars <= 0:
        return []
    if n_chars <= window:
        return [(0, n_chars)]
    n_full = -(-n_chars // window)  # ceil
    if n_full <= max_windows:
        return [(i * window, min((i + 1) * window, n_chars)) for i in range(n_full)]
    last_start = n_chars - window
    starts = sorted({round(i * last_start / (max_windows - 1)) for i in range(max_windows)})
    return [(s, s + window) for s in starts]


# ---------------------------------------------------------------------------
# ingest: manifest bytes -> documents rows (canonical text + title)
# ---------------------------------------------------------------------------


def should_ocr(canonical: CanonicalText) -> bool:
    """Scanned-document decision: a majority of low-yield pages means the M1
    text lane got nothing usable and the M2 OCR stream becomes canonical."""
    if not canonical.pages:
        return False
    return len(canonical.low_yield_pages) / len(canonical.pages) > OCR_PAGE_FRACTION


# A text layer that is present but is not the Document's text. Each threshold is
# set against the stored Corpus (101 Documents, 29 Sep 2026), so that every
# stream a reviewer can read passes and the known bad ones do not:
#
# * unmapped glyphs ("(cid:N)", private-use code points, U+FFFD) over a fifth
#   of the visible text. The worst readable stream carries 6% (Malaysia's Act
#   854, a copyright page of (cid:N) glyphs).
# * letters under 40% of the visible text: the glyphs were dropped and only the
#   numbering and punctuation survive. The Hindi gazette doc_in_H202344 is at
#   21%; the lowest readable stream is 70% (an Australian volume of tables).
# * a Language written in its own script whose text layer is under half that
#   script AND reads as no English (an English legal-word hit rate under 0.2):
#   a legacy national font mapped onto Latin letters. Lao Decree 296 is at 0%
#   and 0.01; every other stored non-Latin stream is over 94% its script, and
#   the lowest English stream hits 0.67, so an official English text filed
#   under the Economy's Language is left alone.
# * Thai or Lao whose SARA AM outnumbers SARA AA: a broken font map that reads
#   every "า" as "ำ" (the official Thai PDPA PDF). The ordinary vowel is
#   one of the commonest letters of both scripts; the stored Lao streams carry
#   52,700 of it and none of the other.
# * one line repeated at least 200 times and more than 10 times a page: a
#   watermark printed over the text (the same Thai PDF, about 2,000 times). A
#   running header repeats once or twice a page; the most in the Corpus is 3.
UNMAPPED_SHARE_MAX = 0.2
LETTER_SHARE_MIN = 0.4
OWN_SCRIPT_SHARE_MIN = 0.5
ENGLISH_HIT_RATE_MIN = 0.2
WATERMARK_MIN_REPEATS = 200
WATERMARK_PER_PAGE = 10
# Below this many visible characters there is nothing to judge; the low-yield
# rule decides such a Document.
_GARBAGE_MIN_CHARS = 200
_GARBAGE_SAMPLE_CHARS = 200_000
_CID_RE = re.compile(r"\(cid:\d+\)")
# (the ordinary vowel, the one a broken font map puts in its place)
_SARA_AA_AM = {"Thai": ("\u0e32", "\u0e33"), "Lao": ("\u0eb2", "\u0eb3")}


@lru_cache(maxsize=1)
def _english_wordlist() -> frozenset[str]:
    from regcompass.ocr import _load_wordlist

    return _load_wordlist()


def garbage_text_layer(canonical: CanonicalText, language: str | None) -> str | None:
    """Why this PDF's text layer is not its text (so the OCR lane reads the
    page images instead), or None when it reads as text. The thresholds and the
    Documents they were set against are listed above."""
    from regcompass.ocr import dictionary_hit_rate

    text = canonical.full_text[:_GARBAGE_SAMPLE_CHARS]
    visible = sum(1 for ch in text if not ch.isspace())
    if visible < _GARBAGE_MIN_CHARS:
        return None
    unmapped = sum(len(m) for m in _CID_RE.findall(text)) + sum(
        1 for ch in text if "\ue000" <= ch <= "\uf8ff" or ch == "\ufffd"
    )
    if unmapped / visible > UNMAPPED_SHARE_MAX:
        return f"{unmapped / visible:.0%} of the text layer is unmapped glyphs"
    letters = sum(1 for ch in text if ch.isalpha() or unicodedata.category(ch).startswith("M"))
    if letters / visible < LETTER_SHARE_MIN:
        return f"only {letters / visible:.0%} of the text layer is letters: the words were dropped"
    if (
        language in NON_LATIN_SCRIPT_LANGUAGES
        and non_latin_share(text) < OWN_SCRIPT_SHARE_MIN
        and dictionary_hit_rate(text, _english_wordlist()) < ENGLISH_HIT_RATE_MIN
    ):
        return f"the text layer is not in {language} script: a legacy font mapped onto Latin letters"
    if language in _SARA_AA_AM:
        aa, am = _SARA_AA_AM[language]
        if text.count(am) > text.count(aa):
            return f"the text layer reads the {language} vowel {aa} as {am}: a broken font map"
    counts: dict[str, int] = {}
    for line in text.split("\n"):
        line = line.strip()
        if sum(ch.isalpha() for ch in line) >= 3:
            counts[line] = counts.get(line, 0) + 1
    if counts:
        repeats = max(counts.values())
        if repeats >= WATERMARK_MIN_REPEATS and repeats > WATERMARK_PER_PAGE * max(1, len(canonical.pages)):
            return f"one line repeats {repeats} times over the text layer: a watermark"
    return None


_SHORT_DIGEST = re.compile(r"[0-9a-f]{12}")


def readable_stem(row) -> str:
    """The part of a manifest row's filename hint an id can be read back from.
    Empty when the name carries no ASCII name characters at all, which is the
    ordinary case for a file named entirely in Lao or Chinese script."""
    hint = row["filename_hint"] or Path(row["local_path"]).name
    stem = hint.rsplit(".", 1)[0]
    return re.sub(r"[^A-Za-z0-9()-]+", "_", stem).strip("_")


def _listed(row) -> str | None:
    """The Portal listing's own name on a manifest row, where it has one."""
    return row["title"] if "title" in row.keys() else None


def document_id_for(row) -> str:
    """Stable, human-readable id from the manifest row's filename hint."""
    return f"doc_{row['economy'].lower()}_{readable_stem(row) or 'document'}"


def _is_digest_variant(document_id: str, plain: str) -> bool:
    """Is this id the digest-suffixed form of `plain`?"""
    return document_id.startswith(f"{plain}_") and bool(
        _SHORT_DIGEST.fullmatch(document_id[len(plain) + 1 :])
    )


def resolve_document_id(storage: Storage, row) -> str:
    """The id this manifest row's bytes take in the Corpus.

    The readable id stands wherever it can, because the AU, MY and SG Corpus
    and the goldens are keyed on it. It cannot stand in two cases, and both
    then put the bytes' own digest in the id, the way an uploaded file with no
    usable name is already named:

    - the name sanitises to nothing, so every file named in that script would
      derive one id per Economy and each ingest would write over the last;
    - the readable id is already held by a row carrying different bytes.

    A re-fetch under `--refresh` is neither. It replaces the bytes published at
    one address, so the Document at that address keeps the id it already has
    and its single row. A Discovery manifest key IS the address, and Discovery
    files one row per address, so an address that already has exactly one
    Document of this name identifies that Document. An upload's key carries a
    digest beside the address precisely because one landing page publishes many
    statutes, so it is never read as a refresh.
    """
    plain = document_id_for(row)
    key = row["url"]
    if key and manifest_address(key) == key:
        published_here = [
            r["document_id"]
            for r in storage.conn.execute(
                "SELECT document_id FROM documents WHERE economy = ? AND source_url = ?",
                (row["economy"], key),
            ).fetchall()
            if r["document_id"] == plain or _is_digest_variant(r["document_id"], plain)
        ]
        if len(published_here) == 1:
            return published_here[0]
    suffixed = f"{plain}_{row['sha256'][:12]}"
    if not readable_stem(row):
        return suffixed
    held = storage.conn.execute(
        "SELECT source_sha256 FROM documents WHERE document_id = ?", (plain,)
    ).fetchone()
    if held is not None and held["source_sha256"] != row["sha256"]:
        return suffixed
    return plain


def ingest_economy(
    storage: Storage,
    data_dir: Path,
    economy: str,
    *,
    config: PipelineConfig | None = None,
    ocr_languages: str | None = None,
    evidence_root: Path | None = None,
    language: str | None = None,
) -> tuple[list[IngestResult], list[dict]]:
    """Extract every fetched, non-duplicate manifest document into the
    documents table. Resumable: a document whose source_sha256 already has a
    row is skipped. A document that cannot be ingested lands in the exclusion
    list with a reason and the run continues (never a silent drop).

    `language` is the Document Language stored on each row, which Discovery
    passes as the Portal's default Language (the first entry of its `languages`
    list). Per-document detection is out of scope: the Portal's own default is
    the honest answer, and a reviewer can correct one Document.

    That Language also picks the tesseract language data and switches off the
    English OCR proxies for a non-Latin script (regcompass.languages).
    `ocr_languages` overrides the derivation for a caller that knows better;
    None derives it, which is what Discovery and the tests want."""
    config = config or PipelineConfig()
    if ocr_languages is None:
        ocr_languages = tesseract_languages(language, economy)
    policy = ocr_policy(language, economy)
    results: list[IngestResult] = []
    excluded: list[dict] = []
    for row in storage.manifest_rows(economy=economy, status="fetched"):
        if row["is_duplicate_of"] is not None or row["kind"] != "document":
            continue  # index rows (e.g. SSO lazy-load TOC pages) are not corpus
        existing = storage.conn.execute(
            "SELECT document_id FROM documents WHERE source_sha256 = ?", (row["sha256"],)
        ).fetchone()
        if existing is not None:
            continue
        # The manifest records where the bytes are relative to the data
        # folder. A row written before that was enforced holds one machine's
        # absolute path, so the resolver is asked rather than the string
        # trusted, and what the Corpus row below records is the spelling that
        # travels, not the one the manifest happened to keep.
        found = resolve_stored_path(
            row["local_path"], data_dir, economy=economy, sha256=row["sha256"]
        )
        local = (
            Path(data_dir) / row["local_path"] if found is None else found.path
        )
        stored_path = storable_local_path(local, data_dir)
        try:
            raw = local.read_bytes()
            if hashlib.sha256(raw).hexdigest() != row["sha256"]:
                raise ValueError("bytes on disk do not match manifest sha256")
            doc_id = resolve_document_id(storage, row)
            fmt = sniff_format(raw)
            key = extraction_key(
                row["sha256"], ocr_languages=ocr_languages, policy=policy,
                config=config,
            )
            with log_stage(storage, stage="m11_ingest", method=f"{economy}:{fmt}",
                           input_data=raw) as rec:
                # The same read the next Run would do, and the Run will read it
                # back rather than repeat it. An ingest that finds
                # the stream already stored (a re-ingest, or the same bytes
                # under a second Corpus) does not repeat it either - unless it
                # was asked for OCR evidence pairs, which are written by the
                # read itself and are the whole point of asking.
                canonical = (
                    None if evidence_root is not None
                    else load_extraction(storage, key, doc_id)
                )
                if (
                    canonical is not None and fmt == "pdf" and not canonical.ocr_applied
                    and garbage_text_layer(canonical, language) is not None
                ):
                    canonical = None  # stored before garbage layers were recognised
                reused = canonical is not None
                if canonical is None:
                    canonical, _ = extract_with_stats(raw, fmt, doc_id)
                    scanned = fmt == "pdf" and should_ocr(canonical)
                    garbage = (
                        fmt == "pdf" and not scanned
                        and garbage_text_layer(canonical, language) is not None
                    )
                    if scanned or garbage:
                        from regcompass.ocr import ocr_document

                        evidence_dir = (
                            evidence_root / doc_id if evidence_root is not None else None
                        )
                        doc_languages, doc_policy = ocr_languages, policy
                        if garbage:
                            # every script the Document could be in (see
                            # languages.garbage_ocr_languages)
                            portal = load_portals().get(economy)
                            doc_languages, reading = garbage_ocr_languages(
                                language, economy, canonical.full_text,
                                portal.languages if portal is not None else (),
                            )
                            doc_policy = ocr_policy(reading, economy)
                        canonical = ocr_document(
                            raw, doc_id, languages=doc_languages, config=config,
                            evidence_dir=evidence_dir, policy=doc_policy,
                        )
                ocr_applied = canonical.ocr_applied
                # The Portal listing's own name wins over one read off the
                # file (a Lao Gazette PDF is named "05ສພຊ2021.pdf"), the way a
                # typed name wins on the add lane.
                title = _listed(row) or derive_title(
                    raw, fmt, canonical.full_text, row["filename_hint"]
                )
                rec.output_data = canonical.full_text
                cached = reused
                if not reused:
                    # Storing the stream must never be what keeps a Document out
                    # of the Corpus: the Document is the deliverable and the
                    # stored stream is an optimisation the Run can do without,
                    # so a failure here is recorded in the trail and dropped.
                    try:
                        store_extraction(storage, key, canonical, source_format=fmt)
                        cached = True
                    except Exception:  # noqa: BLE001 - see above
                        cached = False
                if reused:
                    rec.decision = "ingested:reuse"
                else:
                    rec.decision = (
                        f"ingested:{'ocr' if ocr_applied else 'text'}"
                        f"{'' if cached else ' (extraction not stored)'}"
                    )
            storage.clear_window_embeddings(doc_id)  # new bytes: stale embeddings must go
            storage.upsert_document(
                doc_id,
                economy,
                canonical.source_sha256,
                # The manifest key is not always the address. A Discovery key
                # is; an upload's key carries the content digest beside the
                # address (or is a local marker, for bytes with no address at
                # all), because one landing page can publish many statutes and
                # the manifest has to tell them apart. What goes in the Corpus
                # row is where the Document is published, and nothing else.
                source_url=manifest_address(row["url"]),
                local_path=stored_path,
                fetched_at=row["fetched_at"],
                extractor=canonical.extractor,
                extractor_version=canonical.extractor_version,
                full_text=canonical.full_text,
                ocr_applied=int(ocr_applied),
                title=title,
                n_pages=len(canonical.pages),
                n_low_yield_pages=len(canonical.low_yield_pages),
                **({} if language is None else {"language": language}),
                # A secondary reference Discovery recorded beside this Document
                # (the Lao Portal's English rendering) travels from the manifest
                # row to the Corpus row here. Absent notes write nothing, so a
                # re-ingest never blanks a note another lane set.
                **({} if row["notes"] is None else {"notes": row["notes"]}),
                # The OCR verdict travels with the stream that carries it, on
                # the reuse lane as well as the fresh read: the stored stream
                # is the contract model's own JSON, so a reused scan knows its
                # own confidence and whether RapidOCR was reached for.
                **ocr_quality_columns(canonical),
            )
            results.append(
                IngestResult(
                    document_id=doc_id,
                    title=title,
                    ocr_applied=ocr_applied,
                    n_pages=len(canonical.pages),
                    n_low_yield_pages=len(canonical.low_yield_pages),
                    n_chars=len(canonical.full_text),
                )
            )
        except Exception as exc:  # noqa: BLE001 - each document is independent
            excluded.append(
                {
                    "url": row["url"],
                    "local_path": row["local_path"],
                    "reason": f"{type(exc).__name__}: {exc}"[:300],
                }
            )
    return results, excluded


# ---------------------------------------------------------------------------
# embeddings: document windows -> shortlist_windows
# ---------------------------------------------------------------------------


def embed_economy(
    storage: Storage, economy: str, embed_fn: EmbedFn | None = None, *, force: bool = False
) -> int:
    """Embed the windows of every document that has none yet (resumable).
    Returns the number of documents embedded in this call."""
    embed_fn = embed_fn or embed_ollama
    done = 0
    for doc in storage.documents_for_economy(economy):
        if not force:
            spans, _ = storage.load_window_embeddings(doc["document_id"])
            if spans:
                continue
        text = doc["full_text"] or ""
        spans = window_spans(len(text))
        if not spans:
            continue
        matrix = np.asarray(embed_fn([text[s:e] for s, e in spans]), dtype=np.float32)
        storage.store_window_embeddings(doc["document_id"], spans, matrix)
        done += 1
    return done


# ---------------------------------------------------------------------------
# ranking
# ---------------------------------------------------------------------------


def pillar_vocab(pillar: int, config_dir=CONFIG_DIR) -> list[str]:
    """The Pillar's full Indicator vocabulary, deduplicated, order preserved.
    Any configured Pillar works; an unconfigured one fails with the list."""
    configured = tuple(sorted(load_pillars(config_dir)))
    if pillar not in configured:
        raise ValueError(f"pillar must be one of {configured}, got {pillar!r}")
    seen: list[str] = []
    for phrases in load_keywords(pillar, config_dir).values():
        for phrase in phrases:
            if phrase not in seen:
                seen.append(phrase)
    return seen


def _unit_rows(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.clip(norms, 1e-12, None)


def rank_economy(
    storage: Storage,
    economy: str,
    pillar: int,
    *,
    embed_fn: EmbedFn | None = None,
    config_dir=CONFIG_DIR,
    out_dir: Path,
) -> tuple[list[ShortlistRow], ShortlistReport]:
    """Rank every ingested document of an economy for one pillar and write the
    CSV. Every document appears exactly once; an economy with no documents
    produces an explicit header-only CSV plus a note, never a crash."""
    import bm25s

    embed_fn = embed_fn or embed_ollama
    t0 = time.perf_counter()
    official = load_portals(config_dir)[economy].official_name.lower().replace(" ", "_")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"shortlist_{official}_pillar{pillar}.csv"
    report = ShortlistReport(economy=economy, pillar=pillar, csv_path=str(csv_path))

    docs = storage.documents_for_economy(economy)
    report.n_documents = len(docs)
    if not docs:
        report.notes.append(f"no documents ingested for {economy}; empty shortlist emitted")
        _write_csv(csv_path, [])
        report.duration_s = time.perf_counter() - t0
        return [], report

    indicator_vocab = {
        ind: phrases
        for ind, phrases in load_keywords(pillar, config_dir).items()
    }
    vocab = pillar_vocab(pillar, config_dir)
    pillar_text = load_pillar_description(pillar, config_dir)
    pillar_vec = _unit_rows(np.asarray(embed_fn([pillar_text]), dtype=np.float32))[0]

    # semantic tier: max window cosine per document (window spans reused by
    # the phrase tier below)
    cosines: list[float] = []
    doc_spans: list[list[tuple[int, int]]] = []
    for doc in docs:
        spans, matrix = storage.load_window_embeddings(doc["document_id"])
        doc_spans.append(spans)
        if not spans:
            cosines.append(0.0)
            report.notes.append(f"{doc['document_id']}: no window embeddings; cosine 0")
            continue
        cosines.append(float(np.max(_unit_rows(matrix) @ pillar_vec)))

    # lexical tier: window-level bm25 (a document is as relevant as its best
    # window), one focused query per indicator, doc-best rank-normalized
    # across the corpus, max over the pillar's indicators
    n = len(docs)
    texts = [doc["full_text"] or "" for doc in docs]
    step = WINDOW_CHARS // 2
    all_windows: list[str] = []
    window_owner: list[int] = []
    for i, text in enumerate(texts):
        for k in range(0, max(len(text), 1), step):
            all_windows.append(text[k : k + WINDOW_CHARS])
            window_owner.append(i)
    owner = np.asarray(window_owner)
    retriever = bm25s.BM25()
    retriever.index([legal_tokens(w) for w in all_windows], show_progress=False)
    lex = np.zeros(n, dtype=np.float32)
    for phrases in indicator_vocab.values():
        query = [tok for phrase in phrases for tok in legal_tokens(phrase)]
        idx, scores = retriever.retrieve([query], k=len(all_windows), show_progress=False)
        full = np.zeros(len(all_windows), dtype=np.float32)
        full[idx[0]] = scores[0]
        doc_best = np.zeros(n, dtype=np.float32)
        for i in range(n):
            mine = full[owner == i]
            doc_best[i] = float(np.max(mine)) if mine.size else 0.0
        for i in range(n):
            if doc_best[i] > 0.0:
                rank_norm = 1.0 - float(np.sum(doc_best > doc_best[i])) / n
                lex[i] = max(lex[i], rank_norm)

    # phrase-level idf: how many documents each vocabulary phrase occurs in
    texts_cf = [t.casefold() for t in texts]
    doc_freq = {p: sum(1 for t in texts_cf if p.casefold() in t) for p in vocab}
    df_cap = max(1, n // 2)

    scored = []
    for i, doc in enumerate(docs):
        text_cf = texts_cf[i]
        matched = [p for p in vocab if p.casefold() in text_cf]
        discriminating = {p for p in matched if doc_freq[p] <= df_cap}
        # co-occurrence inside a single window, per indicator, best window
        # wins. Scans ALL windows with 50% overlap (cheap string work, unlike
        # the sampled embedding windows): a provision must never be missed
        # because it fell between sampled windows or on a window boundary.
        phrase = 0.0
        if discriminating:
            step = WINDOW_CHARS // 2
            windows_cf = [
                text_cf[k : k + WINDOW_CHARS] for k in range(0, max(len(text_cf), 1), step)
            ]
            for phrases in indicator_vocab.values():
                rare = [p.casefold() for p in phrases if p in discriminating]
                if not rare:
                    continue
                best = max(sum(1 for p in rare if p in w) for w in windows_cf)
                phrase = max(phrase, min(1.0, best / 3.0))
        score = round(
            0.45 * max(cosines[i], 0.0) + 0.25 * float(lex[i]) + 0.3 * phrase, 6
        )
        scored.append((score, doc, matched))

    scored.sort(key=lambda t: (-t[0], t[1]["document_id"]))
    rows = [
        ShortlistRow(
            rank=n,
            document_id=doc["document_id"],
            title=doc["title"] or doc["document_id"],
            source_url=doc["source_url"] or "",
            relevance_score=score,
            matched_keywords=matched,
        )
        for n, (score, doc, matched) in enumerate(scored, start=1)
    ]
    _write_csv(csv_path, rows)
    with log_stage(storage, stage="m11_rank", method=f"{economy}:pillar{pillar}",
                   input_data=str(len(docs))) as rec:
        rec.output_data = csv_path.read_bytes()
        rec.decision = f"ranked {len(rows)} documents"
    report.duration_s = time.perf_counter() - t0
    return rows, report


def _write_csv(path: Path, rows: list[ShortlistRow]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_COLUMNS)
        for r in rows:
            writer.writerow(
                [r.rank, r.document_id, r.title, r.source_url,
                 f"{r.relevance_score:.6f}", "; ".join(r.matched_keywords)]
            )


# ---------------------------------------------------------------------------
# evaluation helpers (used by the exit-gate tests and the golden report)
# ---------------------------------------------------------------------------


def recall_at_k(ranked_ids: list[str], relevant_sets: list[set[str]], k: int) -> float | None:
    """Fraction of ground-truth laws with at least one of their documents in
    the top max(k, R) ranks, R the number of distinct relevant documents.
    None when there is nothing to recall (vacuous).

    The widening to R is structural, not a relaxation: the Round 1 Database
    cites 11 distinct pillar-7 laws for Australia, and 11 laws cannot occupy
    10 rank slots, so plain recall@10 is unreachable for a pillar that dense
    no matter how good the ranking is. With k = max(k, R) a perfect ranking
    always scores 1.0 and a sloppy one still fails."""
    if not relevant_sets:
        return None
    n_relevant_docs = len(set().union(*relevant_sets))
    top = set(ranked_ids[: max(k, n_relevant_docs)])
    return sum(1 for s in relevant_sets if s & top) / len(relevant_sets)


def precision_at_k(ranked_ids: list[str], relevant_docs: set[str], k: int) -> float | None:
    """Precision over the top j = min(k, R) positions, R the number of
    relevant documents in the corpus. Capping at R makes a perfect ranking
    score 1.0 even when fewer than k relevant documents exist (with a corpus
    this size, plain precision@5 would be bounded below 0.8 by R alone)."""
    if not relevant_docs:
        return None
    j = min(k, len(relevant_docs), len(ranked_ids))
    if j == 0:
        return None
    top = ranked_ids[:j]
    return sum(1 for d in top if d in relevant_docs) / j
