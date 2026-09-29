"""M9 - export: the graded 13-column submission contract + the gate battery.

Everything here is mechanical. Rows are assembled from verified, reconciled
records plus config (corpus metadata, crosswalk, known matrix, portals); the
confidence column is a documented composite of model-independent signals; the
Discovery Tag comes from the committed provision-level KNOWN matrix; derived
scores follow the INVERSE RDTII direction (0 = open, 1 = restrictive - for
7.1/7.2 the PRESENCE of a framework is openness). The gate battery runs before
anything is written: a red battery ships nothing.

One mechanical narrowing happens before the battery: duplicate provision rows
(two accepted Mappings on one Economy's one Indicator citing one provision of
one law) are collapsed to the highest-Confidence row and counted in the export
report. The battery's duplicate check is unchanged and still refuses any pair
that reaches it.
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Callable
from urllib.parse import urlparse

from . import __version__
from .config import (
    CONFIG_DIR,
    load_corpus,
    load_crosswalk,
    load_default_pillars,
    load_indicators,
    load_known_matrix,
    load_models,
    load_portals,
    load_review_drops,
)
from .classify import (
    ProvisionClassification,
    ScoreCell,
    apply_classifications,
    derive_scores_v2,
)
from .chunk import SectionLabelIndex, quote_is_above_its_chunks_heading
from .contracts import (
    FIXTURE_SOURCE_KIND,
    GLOSS_LABEL,
    GLOSS_UNAVAILABLE,
    CorpusDoc,
    DocumentMapping,
    Engine,
    MappingRecord,
    PortalConfig,
    Review,
    organizer_language,
)
from .engines import resolve_engine
from .extract import FormatTag, format_for_extractor
from .languages import non_latin_share
from .workbook import ROW_CAP, template_path, write_workbook

COLUMNS = (
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
)
# Sanctioned enrichment, APPENDED after the 13, never inserted.
# "Verbatim English" is the appended-translation lane: a snippet stays in its
# source language, and a non-English snippet REQUIRES this appended translation
# (the battery enforces it). The text is a Gloss drafted by the Run's own Engine
# and stored in the database, or the committed config/verbatim_english.json
# where the database is silent; nothing is generated at export time. It carries
# the non-authoritative AI label unless a NAMED person approved it in review.
VERBATIM_ENGLISH_COLUMN = "Verbatim English"
# The final round's one new organizer column (Output Data column N, REQUIRED,
# criterion C1c). It is appended AFTER the 13, at position 14, so the Round 1
# positional contract on the first thirteen is untouched; the header is the
# organizers' own spelling, so their file and ours read the same.
LANGUAGE_COLUMN = "Language of Source"
EXTRA_COLUMNS = (
    LANGUAGE_COLUMN,
    "Novelty Scope",
    "Controlling Evidence",
    "Relationship To Group",
    VERBATIM_ENGLISH_COLUMN,
)

REQUIRED = (
    "Economy",
    "Law Name",
    "Indicator ID",
    "Article / Section",
    "Discovery Tag",
    "Verbatim Snippet",
    "Source URL",
)

ABSENCE_MARKER = "No provision found"
RATIONALE_MAX = 300

# INVERSE score direction: 0 = open, 1 = restrictive. For 7.1 and
# 7.2 the indicator is the LACK of a framework, so verified presence of the
# framework scores 0 (open); for every restriction indicator presence scores 1.
#
# Keyed off classify.SCORED_INDICATORS: the RDTII 2.1 Guide gives a scoring
# rubric to the nine Pillar 6 and 7 Indicators only. Every other Indicator is on
# the MAPPINGS-ONLY path: its Mappings export with their verbatim quotes and it
# gets no score cell and no absence row. A test asserts this table's keys are
# exactly the scored registry, so the two can never drift.
SCORE_IF_PRESENT = {
    "6.1": 1.0,
    "6.2": 1.0,
    "6.3": 1.0,
    "6.4": 1.0,
    "7.1": 0.0,
    "7.2": 0.0,
    "7.3": 1.0,
    "7.4": 1.0,
    "7.5": 1.0,
}

# Content of the template's example rows (OUTPUT_TEMPLATE_31MAY.xlsx R7-R9,
# read 5 Jul 2026): shipping any of it means template rows were not deleted.
# Example-row leak detection keys on the example rows' SNIPPET text only.
# The template's AU example cites a real law (C2004A02124 IS the TIA 1979 on
# the register), so a URL/register-ID marker would reject every legitimate
# mapping of that act once it entered the corpus - found on the full-corpus
# run, where the TIA is a Round 1 Database law.
TEMPLATE_EXAMPLE_MARKERS = (
    "An organisation shall not transfer personal data to a count",
    "An organisation shall not collect, use or disclose personal",
    "Period for keeping information and documents",
)

# Rubric-derived (M12) scores per indicator: {0, 0.5, 1} for the graded cells,
# binary for 6.3/7.3/7.5, plus the reserved 0.25 DPIA-only lane on 7.4 only
# (per the RDTII 2.1 Guide: Pillars 6/7 expected value set is {0, 0.25, 0.5, 1}).
V2_ALLOWED_SCORES = {
    "6.1": (0.0, 0.5, 1.0),
    "6.2": (0.0, 0.5, 1.0),
    "6.3": (0.0, 1.0),
    "6.4": (0.0, 0.5, 1.0),
    "7.1": (0.0, 0.5, 1.0),
    "7.2": (0.0, 0.5, 1.0),
    "7.3": (0.0, 1.0),
    "7.4": (0.0, 0.25, 0.5, 1.0),
    "7.5": (0.0, 1.0),
}

_REPEALED_RE = re.compile(r"repeal|cancel|replac|revok", re.I)

# Pure English function words (never legal content words, never Malay
# loanwords like "data" or "akta"): an English legal sentence of 8+ words
# without ANY of them is practically impossible, so zero hits = suspected
# non-English. Deliberately conservative: false alarms would block exports.
_ENGLISH_FUNCTION_WORDS = frozenset(
    "the of to and in is are was were be been or shall may must not for by"
    " with under on at as this that these those an it its which where when"
    " if any no such other than from has have".split()
)
_WORD_RE = re.compile(r"[a-z']+")
# Below this many characters a non-Latin text is a stray glyph or a bare
# number, not a quote; the script probe stays silent there.
_NON_LATIN_MIN_CHARS = 8


def looks_non_english(text: str) -> bool:
    """Conservative non-English detector for the battery.

    Two probes, in order. The SCRIPT probe first: a quote written mostly
    outside the Latin alphabet is non-English whatever its handful of Latin
    tokens say. Without it the word probe below counts zero words in pure
    Chinese, Lao or Thai, reads that as insufficient signal, and passes the row
    as English, so the gloss requirement never fires on exactly the rows that
    need one.

    Then the WORD probe for Latin-script text: fewer than 8 words is
    insufficient signal and passes as English; 8+ words with zero English
    function-word hits is flagged. Except for a LIST: a run of short items
    between commas ("digital audio, digital video, cell phones, digital fax
    machines") has no function words in any language, so their absence there
    says nothing, and it is read as insufficient signal too. A sentence is
    still judged whole, and a list whose items carry sentences is not a list
    of names."""
    if len(text.strip()) >= _NON_LATIN_MIN_CHARS and non_latin_share(text) > 0.5:
        return True
    words = _WORD_RE.findall(text.lower())
    if len(words) < 8:
        return False
    if _is_enumeration(text):
        return False
    return not any(w in _ENGLISH_FUNCTION_WORDS for w in words)


# A list item: at most this many words. Names of things run to two or three
# ("digital fax machines", "cell phones"); a clause rarely stops there, and
# holding the line at three keeps a comma-rich sentence from passing as a list.
_LIST_ITEM_MAX_WORDS = 3


def _is_enumeration(text: str) -> bool:
    """Three or more comma- or semicolon-separated items, each a short run of
    words: a list of names rather than a sentence."""
    items = [i for i in re.split(r"[,;]", text) if _WORD_RE.search(i.lower())]
    return len(items) >= 3 and all(
        len(_WORD_RE.findall(i.lower())) <= _LIST_ITEM_MAX_WORDS for i in items
    )


class ExportGateError(RuntimeError):
    def __init__(self, failures: list[str]):
        super().__init__("export gate battery red:\n" + "\n".join(f"- {f}" for f in failures))
        self.failures = failures


@dataclass
class SyntheticDoc:
    """A CorpusDoc for a document that is NOT in config/corpus.yaml (a live
    crawl catch, or a user-supplied map-pdf input). The curated corpus stays
    authoritative: synthetic docs only fill gaps so the export never KeyErrors
    on an off-corpus document_id. The two honesty flags drive Notes and the
    battery: law_name_mechanical stamps that the law name was derived from the
    document title (crawl lane); allow_any_host both stamps the user-supplied
    note AND exempts the row from the portal whitelist (map-pdf --allow-any-host).
    Neither lane is used by the Round 1 submission artifacts."""

    corpus_doc: CorpusDoc
    law_name_mechanical: bool = False
    allow_any_host: bool = False
    # The Document entered the Corpus through "Add document" rather than
    # Discovery. It travels with allow_any_host whenever its host is off the
    # whitelist, because a reviewer vouching for a URL is exactly what the
    # exemption means here; on a whitelisted host no exemption is needed and
    # the off-whitelist note would be false.
    manual_added: bool = False


LAW_NAME_MECHANICAL_NOTE = (
    "law name mechanically derived from the document title (off-corpus document:"
    " a live crawl catch, not a curated corpus entry)"
)
# The same fact for a Document a reviewer added: its title was derived the same
# way, but it was never caught by a crawl. MANUAL_ADD_NOTE says how it arrived.
LAW_NAME_MECHANICAL_MANUAL_NOTE = (
    "law name mechanically derived from the document title (off-corpus document:"
    " added by a reviewer, not a curated corpus entry)"
)
ALLOW_ANY_HOST_NOTE = (
    "user-supplied document, host not on the Round 1 portal whitelist"
)
MANUAL_ADD_NOTE = (
    "manually added by the reviewer (source kind manual): the Source URL was"
    " supplied by hand and is not an automated Portal catch"
)
FIXTURE_ADD_NOTE = "fixture legislation seeded from the install, demo only"


def synthetic_notes(
    doc: SyntheticDoc | None,
    source_kind: str | None = None,
    document_note: str | None = None,
) -> list[str]:
    """The honest disclosures a document owes its rows. THE one composer: a
    provision row and an absence row built on the same document carry the same
    sentences, which is what lets the battery exempt both from the portal
    whitelist on the same evidence.

    `doc` covers the off-corpus lanes, which are the ones that need a stand-in
    CorpusDoc at all. `source_kind` covers a fact that is true of CURATED
    documents too: a Corpus filled by `regcompass seed` holds the legislation
    bundled with the install rather than what the Portal served on the day of
    the Run, and the reader of a submission file has no other way to learn
    that. It adds a note and nothing else, because a seeded document keeps its
    official Source URL and is checked against the whitelist like any other.

    `document_note` is a secondary reference Discovery recorded beside the
    Document itself (the Lao Official Gazette's English rendering of an
    instrument). It comes LAST, after every disclosure, because the disclosures
    are about the weight of this row's evidence and the reference is an extra a
    reader may follow; a Document without one adds nothing."""
    notes: list[str] = []
    if doc is not None:
        if doc.law_name_mechanical:
            notes.append(
                LAW_NAME_MECHANICAL_MANUAL_NOTE if doc.manual_added else LAW_NAME_MECHANICAL_NOTE
            )
        if doc.allow_any_host:
            notes.append(ALLOW_ANY_HOST_NOTE)
        if doc.manual_added:
            notes.append(MANUAL_ADD_NOTE)
    if source_kind == FIXTURE_SOURCE_KIND:
        notes.append(FIXTURE_ADD_NOTE)
    if document_note:
        notes.append(document_note)
    return notes


# ---------------------------------------------------------------------------
# law-name normalization + Discovery Tag (provision level)
# ---------------------------------------------------------------------------


def norm_law(name: str) -> str:
    """Normalized law-name key: lowercase, parentheticals and punctuation
    stripped, whitespace collapsed. Containment on these keys = law match.
    (scripts/extract_known_matrix.py uses the same function.)"""
    name = re.sub(r"\([^)]*\)", " ", name)
    name = re.sub(r"[^a-z0-9 ]", " ", name.lower())
    return re.sub(r"\s+", " ", name).strip()


_SECTION_BASE_RE = re.compile(r"(?:\bs\.?|\bsections?|\barts?\.?|\barticles?)\s*(\d+[A-Za-z]{0,3})", re.I)


def _section_base(section_label: str) -> str | None:
    m = _SECTION_BASE_RE.search(section_label)
    return m.group(1).lower() if m else None


def _ref_base(ref: str) -> str:
    m = re.match(r"(\d+[A-Za-z]{0,3})", ref)
    return m.group(1).lower() if m else ref.lower()


def _law_matches(key: str, entry_key: str) -> bool:
    return bool(key) and bool(entry_key) and (key in entry_key or entry_key in key)


def discovery_tag(record: MappingRecord, law_name: str, matrix: dict) -> tuple[str, str | None]:
    """PROVISION-level NEW/KNOWN. KNOWN when the
    Round 1 Database cites this law for this indicator AND either cites this
    provision's section or is an article-less general reference (law-level
    match governs that row). Otherwise NEW, with novelty_scope 'provision' if
    the law itself is in the database or the Legal Inventory, else 'law'."""
    key = norm_law(law_name)
    sec = _section_base(record.section)
    for e in matrix["database"].get(record.economy, {}).get(record.indicator_id, []):
        if _law_matches(key, e["law_key"]):
            if not e["sections"]:
                return "KNOWN", None
            if sec and sec in {_ref_base(s) for s in e["sections"]}:
                return "KNOWN", None
    law_known = any(
        _law_matches(key, e["law_key"])
        for entries in matrix["database"].get(record.economy, {}).values()
        for e in entries
    ) or any(_law_matches(key, k) for k in matrix["inventory_law_keys"].get(record.economy, []))
    return "NEW", "provision" if law_known else "law"


# ---------------------------------------------------------------------------
# mechanical confidence (never LLM self-reported)
# ---------------------------------------------------------------------------


#: Our rule of thumb, NOT a calibrated probability: a row scoring below this
#: gets a human eye before it ships. The README's calibration paragraph states
#: the same number in words, a test holds the two together, and the Review
#: queue counts against it. One definition, so the rule cannot drift.
REVIEW_CONFIDENCE_THRESHOLD = 0.60


def confidence_parts(cosine: float, quote_len: int, n_indicators: int, attempts: int) -> list[dict]:
    """The four model-independent signals behind confidence_score, one dict
    each: signal key, plain label, raw input, weight, the signal's 0-1 score
    and its contribution (weight x score). Weights are documented constants,
    not tuned to please: similarity 0.45 (rescaled from the calibrated cosine
    band 0.35-0.75), quote length 0.25 (saturates at 240 chars: longer =
    safer), multi-indicator penalty 0.15 (a chunk claimed by many indicators
    is less specific), retry penalty 0.15 (a record that needed stricter
    retries is less trustworthy). confidence_score is the rounded sum of the
    contributions, so a breakdown shown beside a Confidence always adds up."""
    sim = max(0.0, min(1.0, (cosine - 0.35) / 0.40))
    qlen = min(1.0, quote_len / 240)
    multi = 1.0 if n_indicators <= 1 else max(0.4, 1.0 - 0.15 * (n_indicators - 1))
    retry = {1: 1.0, 2: 0.6}.get(attempts, 0.3)
    return [
        {"signal": signal, "label": label, "raw": raw, "weight": weight,
         "score": score, "contribution": weight * score}
        for signal, label, raw, weight, score in (
            ("similarity", "Meaning match", cosine, 0.45, sim),
            ("quote_length", "Quote length", quote_len, 0.25, qlen),
            ("specificity", "Specificity", n_indicators, 0.15, multi),
            ("attempts", "Proof attempts", attempts, 0.15, retry),
        )
    ]


def confidence_score(cosine: float, quote_len: int, n_indicators: int, attempts: int) -> float:
    """Composite of the four signals confidence_parts lays out: the rounded
    sum of their contributions, added in the same order as always so the
    number is byte-for-byte what it was before the parts existed."""
    return confidence_from_parts(confidence_parts(cosine, quote_len, n_indicators, attempts))


def confidence_from_parts(parts: list[dict]) -> float:
    """The Confidence a list of confidence_parts adds up to."""
    total = 0.0
    for part in parts:
        total += part["contribution"]
    return round(total, 2)


# ---------------------------------------------------------------------------
# row assembly
# ---------------------------------------------------------------------------


_SECTION_TAIL_NUM_RE = re.compile(r"\bs\.\s*(\S+)\s*$")


def article_section(record: MappingRecord) -> str:
    if not record.subsection:
        return record.section
    # SG dot-style extractions can restate the section number inside the
    # subsection field ("s. 2" + "2.(1)"); rendering both duplicates the
    # number ("s. 22.(1)"). Strip the restated prefix, keeping the rest.
    sub = record.subsection
    m = _SECTION_TAIL_NUM_RE.search(record.section or "")
    if m and sub.startswith(m.group(1) + "."):
        sub = sub[len(m.group(1)) + 1 :].lstrip("—–-")
    return f"{record.section}{sub}" if sub else record.section


def location_reference(record: MappingRecord, format_tag: FormatTag = "pdf") -> str:
    """The Evidence Export's Location Reference for one Mapping: where in the
    Document the quote sits, in the one spelling this project writes. THE
    source of that spelling: the export writes it into the workbook and the
    audit view's "Open source" link parses the page back out of it, so the
    two can never drift into different formats.

    An HTML Document has no pages (extraction records the whole page as page
    1), so its reference is the section path instead, which is the other form
    the organizers' column names ("PDF page number, or HTML anchor / section
    path")."""
    if format_tag == "html":
        path = article_section(record)
        return f"HTML: {path}" if path else ""
    return f"PDF: page {record.page_number}" if record.page_number else ""


_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")


def last_amended_year(value: str | None) -> str:
    """The Last Amended column as the template asks for it: "Year of most
    recent amendment. Leave blank if not amended." (Output Data, D5). The
    stored value may carry a month ('June 2026', the corpus.yaml convention);
    the column shows its year alone, and a value with no year shows nothing."""
    years = _YEAR_RE.findall(value or "")
    return years[-1] if years else ""


def _rationale(impact: str | None) -> str:
    if not impact:
        return ""
    if len(impact) <= RATIONALE_MAX:
        return impact
    return impact[: RATIONALE_MAX - 1].rsplit(" ", 1)[0] + "…"


def reviewer_override_note(original: str, corrected: str, reviewer: str | None) -> str:
    """The Notes disclosure a corrected row carries, so the override is visible
    in the workbook itself and not only in the supplementary record."""
    who = (reviewer or "").strip() or "an unnamed reviewer"
    return f"Reviewer override: Engine proposed {original}; corrected to {corrected} by {who}"


def apply_correction(record: MappingRecord, correction: Review, names: dict[str, str]) -> MappingRecord:
    """The record as it ships once a reviewer corrected its Indicator: under
    the corrected Indicator, with the reviewer's reason as its rationale. The
    stored Mapping is never touched; this copy exists only inside the export."""
    corrected = correction.corrected_indicator_id
    return record.model_copy(
        update={
            "indicator_id": corrected,
            "indicator_name": names.get(corrected, record.indicator_name),
            "impact": (correction.comment or "").strip(),
        }
    )


@lru_cache(maxsize=None)
def _portals(config_dir_str: str) -> dict[str, PortalConfig]:
    return load_portals(Path(config_dir_str))


@lru_cache(maxsize=None)
def _verbatim_english(config_dir_str: str) -> dict[str, str]:
    """The committed English translations for non-English verbatim snippets,
    keyed by mapping_id. A translation is separate from the quote,
    non-authoritative, and AI-labelled; the label is composed HERE so an
    unlabelled translation cannot ship out of this lane.

    This file is the FALLBACK. A Run stores its own Glosses in the database,
    keyed by (Run, Mapping), and those win: see verbatim_english_for."""
    path = Path(config_dir_str) / "verbatim_english.json"
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {
        k: f"{GLOSS_LABEL} {v['english']}"
        for k, v in raw.items()
        if not k.startswith("_")
    }


def gloss_is_reviewed(reviewed_by: str | None) -> bool:
    """Whether a Gloss may ship WITHOUT its label: only when a named person
    approved the text.

    THE shared predicate. The writer below composes the cell with it and the
    gate battery judges the cell with it, so the two can never disagree about
    what a shippable unlabelled rendering is. This replaces the older rule that
    only a translation committed to config/verbatim_english.json could ship: the
    mechanical guarantee is unchanged in kind (no unlabelled AI translation
    without human authority), only the place that authority is recorded moved
    from a config file to a named review in the database."""
    return bool((reviewed_by or "").strip())


def verbatim_english_for(
    mapping_id: str, glosses: dict | None, config_dir=CONFIG_DIR
) -> tuple[str, str | None]:
    """(Verbatim English cell, the reviewer's name) for one Mapping.

    The database Gloss of THIS Run answers first, and the committed
    config/verbatim_english.json answers only where the database is silent. That
    order is also what keeps a Gloss drafted in one Run off another Run's
    identically-named Mapping: the caller looks the Run up, this function never
    guesses. A Gloss whose drafting FAILED writes GLOSS_UNAVAILABLE: the row
    states that no rendering could be produced rather than going out blank,
    which the battery would read as a forgotten translation and which would
    take the whole Export red over one quote."""
    gloss = (glosses or {}).get(mapping_id)
    if gloss is not None:
        english = (gloss.english or "").strip()
        if not english:
            return GLOSS_UNAVAILABLE, None
        if gloss_is_reviewed(gloss.reviewed_by):
            return english, gloss.reviewed_by
        return f"{gloss.label} {english}", None
    return _verbatim_english(str(config_dir)).get(mapping_id, ""), None


LANGUAGE_UNKNOWN_NOTE = (
    "Language of Source not recorded for this document and the Economy's Portal"
    " expects more than one Language, so the organizers' 'Other' is written"
    " rather than a guess"
)


def resolve_language(raw: str | None, portal: PortalConfig) -> tuple[str, bool]:
    """(Language of Source, disclosed) for a row.

    Order: the Document's own recorded Language; else the Economy's Portal
    Language when the Portal expects exactly ONE, which is the same value
    Discovery stamps on every Document it ingests for that Economy; else
    "Other" with a Notes disclosure. Criterion C1c is scored off this column,
    so an Economy whose Portal expects two Languages never gets a coin-flip."""
    if raw:
        return organizer_language(raw), False
    if len(portal.languages) == 1:
        return organizer_language(portal.languages[0]), False
    return "Other", True


def build_row(
    record: MappingRecord,
    doc: CorpusDoc,
    crosswalk,
    matrix: dict,
    gate_cosine_lookup: dict,
    indicators_per_chunk: dict[str, int],
    config_dir=CONFIG_DIR,
    *,
    extra_notes: list[str] | None = None,
    allow_any_host: bool = False,
    language: str | None = None,
    glosses: dict | None = None,
    format_tag: FormatTag = "pdf",
    proposed_indicator_id: str | None = None,
    reviewer: str | None = None,
) -> dict:
    """One submission row (13 columns + sanctioned extras + hidden _keys used
    only by the gate battery and stripped before writing).

    extra_notes (off-corpus lanes only) append honest disclosures to Notes;
    allow_any_host records a hidden flag the battery reads to exempt the row
    from the portal whitelist (map-pdf --allow-any-host). Both default off, so
    the curated Round 1 lane is byte-for-byte unchanged.

    language (final round): the Document's recorded Language, resolved to the
    organizers' Language of Source list by resolve_language.

    glosses (final round): mapping_id -> the Gloss stored for THIS Run. It fills
    the Verbatim English column, labelled unless a named person approved the
    text; where it is silent the committed config/verbatim_english.json still
    answers.

    format_tag: which lane extracted the Document, which decides how the
    Location Reference is spelled (location_reference).

    proposed_indicator_id (a reviewer's correction): the Indicator the Engine
    proposed, when the record arrives already moved to the reviewer's one
    (apply_correction). Confidence is looked up under the proposed Indicator,
    because that is the pair the pipeline scored, and Notes disclose the
    override naming the reviewer."""
    portals = _portals(str(config_dir))
    tag, novelty = discovery_tag(record, doc.known_matrix_law_name or doc.law_name, matrix)
    scored_as = proposed_indicator_id or record.indicator_id
    cosine = float(gate_cosine_lookup.get((record.chunk_id, scored_as), 0.0))
    conf = confidence_score(
        cosine,
        len(record.verbatim_quote),
        indicators_per_chunk.get(record.chunk_id, 1),
        record.extraction_attempts,
    )
    notes: list[str] = []
    if record.subsection is None:
        notes.append("provision has no internal subsection numbering")
    if not doc.url_is_direct:
        notes.append("Source URL is the official portal root; direct act URL pending the crawl module")
    repeal = record.timeline.repeal_status
    if repeal and _REPEALED_RE.search(repeal) and not record.controlling_evidence:
        notes.append(f"repealed-but-recorded: {repeal}")
    # Government-access powers are never rubric-eligible under
    # Pillar 6 (7.5 is their home); the scoring layer already excludes them,
    # this note makes the demotion visible on the row itself.
    if record.indicator_id.startswith("6.") and record.measure_type == "government_access":
        notes.append(
            "government-access power: out of scope for Pillar 6 scoring (scored under 7.5 where eligible)"
        )
    # After the fit-filtered ladder, a controlling record whose
    # classification does not evidence its indicator exists ONLY when no group
    # member fit; say so on the row rather than presenting it as evidence.
    if (
        record.controlling_evidence
        and record.measure_type is not None
        and record.rdtii_score_contribution is None
    ):
        notes.append(
            "controlling by legal hierarchy only: no group member's classification evidences this indicator"
        )
    if extra_notes:
        notes.extend(extra_notes)
    language_of_source, language_disclosed = resolve_language(language, portals[record.economy])
    if language_disclosed:
        notes.append(LANGUAGE_UNKNOWN_NOTE)
    if proposed_indicator_id is not None:
        notes.append(
            reviewer_override_note(proposed_indicator_id, record.indicator_id, reviewer)
        )
    gloss_cell, gloss_reviewed_by = verbatim_english_for(record.mapping_id, glosses, config_dir)
    row = {
        "Economy": portals[record.economy].official_name,
        "Law Name": doc.law_name,
        "Law Number / Ref": doc.law_number_ref or "",
        "Last Amended": last_amended_year(doc.last_amended),
        "Indicator ID": crosswalk.schemes[crosswalk.emission_scheme][record.indicator_id],
        "Article / Section": article_section(record),
        "Discovery Tag": tag,
        "Location Reference": location_reference(record, format_tag),
        "Verbatim Snippet": record.verbatim_quote,
        "Mapping Rationale": _rationale(record.impact),
        "Source URL": doc.source_url,
        "Confidence": f"{conf:.2f}",
        "Notes": "; ".join(notes),
        LANGUAGE_COLUMN: language_of_source,
        "Novelty Scope": novelty or "",
        "Controlling Evidence": "true" if record.controlling_evidence else "false",
        "Relationship To Group": record.relationship_to_group or "",
        VERBATIM_ENGLISH_COLUMN: gloss_cell,
        "_chunk_id": record.chunk_id,
        "_document_id": record.document_id,
        "_mapping_id": record.mapping_id,
        # The name the battery checks the label rule against: an unlabelled
        # rendering ships only on a named person's authority.
        "_gloss_reviewed_by": gloss_reviewed_by,
        "_repeal_status": repeal or "",
        "_economy_code": record.economy,
        "_measure_type": record.measure_type or "",
        "_score_contribution": record.rdtii_score_contribution,
        "_allow_any_host": allow_any_host,
    }
    return row


def pillar_of(indicator_id: str) -> int:
    """The Pillar an Indicator id belongs to: the number BEFORE the first dot.
    A string prefix test would file 12.1 under Pillar 1."""
    return int(indicator_id.split(".")[0])


def run_pillars_of(
    records: list[MappingRecord],
    gate_cosine_lookup: dict | None = None,
    config_dir=CONFIG_DIR,
) -> tuple[int, ...]:
    """Which Pillars this Run actually covered, so absence rows are never
    claimed for an Indicator the Gate was never asked about.

    Two sources, because either alone has a hole: the records say what was
    mapped, and the Gate scores say what was SCREENED even when every record
    was rejected. With neither (a caller that passes no records and no gate
    lookup) the configured default Pillars stand in, which keeps the
    'a searched economy still earns its zeros' lane exactly as it was."""
    pillars = {pillar_of(r.indicator_id) for r in records}
    for key in gate_cosine_lookup or {}:
        pillars.add(pillar_of(key[1]))
    if not pillars:
        return load_default_pillars(config_dir)
    return tuple(sorted(pillars))


def build_absence_rows(
    records: list[MappingRecord],
    corpus: dict[str, CorpusDoc],
    crosswalk,
    coverage_stats: dict[str, dict],
    config_dir=CONFIG_DIR,
    run_pillars: tuple[int, ...] | None = None,
    languages: dict[str, str | None] | None = None,
    synthetic_docs: dict[str, SyntheticDoc] | None = None,
    source_kinds: dict[str, str | None] | None = None,
    withheld: dict[tuple[str, str], int] | None = None,
) -> list[dict]:
    """A zero must be EARNED: every (economy, indicator) with no verified
    provision gets a 'No provision found' row whose Notes record what was
    searched and cite the closest general law as the reference basis.

    Scoped to the Run's Pillars. A Pillar 12 Run must not emit nine Pillar 6
    and 7 rows saying every candidate was screened and found wanting: the Gate
    never queried those Indicators, so the claim would be false.

    synthetic_docs: an absence row whose reference-basis document is off-corpus
    carries that document's own disclosures and its whitelist exemption. An
    Economy whose Portal has no verified host yet (Lao PDR) reaches the export
    only through manually added Documents, and its provision rows were already
    exempt; without this its earned zeros, built on the SAME document and the
    SAME Source URL, failed the battery and no export was possible at all.

    source_kinds: the same reasoning for a fact that is true of curated
    documents too. An earned zero built on a seeded ('fixture') Document rests
    on the legislation bundled with the install, so it carries the same
    disclosure its Economy's provision rows carry.

    withheld: (economy, indicator) -> how many VERIFIED Mappings the review
    gate kept out. For those Indicators the row is still a zero, but its Notes
    say why: evidence was found and nobody accepted it, which is not the same
    fact as a search that found nothing and must not be written as one."""
    portals = _portals(str(config_dir))
    covered = run_pillars if run_pillars is not None else run_pillars_of(records, None, config_dir)
    scored_here = [i for i in SCORE_IF_PRESENT if pillar_of(i) in covered]
    matrix = load_known_matrix(config_dir)
    indicators = load_indicators(config_dir)
    present: dict[str, set[str]] = {}
    for r in records:
        present.setdefault(r.economy, set()).add(r.indicator_id)
    docs_by_econ = {doc.economy: doc for doc in corpus.values()}
    # last-wins document_id per economy, matching docs_by_econ's fallback pick
    ids_by_econ = {doc.economy: doc_id for doc_id, doc in corpus.items()}
    # Which Economies read a SEEDED Corpus. Decided per Economy, not off the
    # reference-basis document: that document is whichever corpus.yaml entry
    # carries the law name the coverage stats cite, and it is often not in this
    # database at all. An earned zero rests on the Documents the Run actually
    # searched, so those are what it discloses. Every one of them must be a
    # fixture, so a genuine Corpus that happens to include one seeded Document
    # is never labelled a demonstration.
    kinds_by_econ: dict[str, set[str | None]] = {}
    for seen_id, kind in (source_kinds or {}).items():
        doc = corpus.get(seen_id)
        if doc is not None:
            kinds_by_econ.setdefault(doc.economy, set()).add(kind)
    fixture_economies = {
        econ for econ, kinds in kinds_by_econ.items() if kinds == {FIXTURE_SOURCE_KIND}
    }
    rows = []
    # Iterate every economy that was SEARCHED (coverage_stats), not just those
    # with surviving records: under the U0 review gate an economy whose records
    # were all rejected still earned its zeros. Economies with records but no
    # stats entry keep the old behavior via the union.
    economies = sorted(set(coverage_stats) | set(present))
    for econ in economies:
        present.setdefault(econ, set())
        # An economy can carry records but no stats entry (older callers); its
        # Notes then cite the reference-basis law without the chunk/gate counts.
        stats = coverage_stats.get(econ)
        law = stats["law"] if stats else docs_by_econ[econ].law_name
        # The reference-basis law is named by coverage_stats; a corpus entry
        # with that exact law_name carries the row's Law Name columns. When
        # stats describe a multi-document search (no single matching entry),
        # the economy's last corpus.yaml entry is the reference basis.
        doc_id = next(
            (k for k, d in corpus.items() if d.economy == econ and d.law_name == law),
            ids_by_econ[econ],
        )
        doc = corpus[doc_id]
        language_of_source, language_disclosed = resolve_language(
            (languages or {}).get(doc_id), portals[econ]
        )
        synthetic = (synthetic_docs or {}).get(doc_id)
        extra_notes = synthetic_notes(
            synthetic,
            FIXTURE_SOURCE_KIND if econ in fixture_economies else None,
        )
        for ind in scored_here:
            if ind in present[econ]:
                continue
            tag = "KNOWN" if matrix["database"].get(econ, {}).get(ind) else "NEW"
            searched = (
                f"Searched {law}: {stats['sections']} sections chunked, "
                f"{stats['pairs_gated']} gate-passed (section x indicator) candidates "
                f"screened by the mapper; "
                if stats
                else f"Searched {law}; "
            )
            n_withheld = (withheld or {}).get((econ, ind), 0)
            outcome = (
                f"{n_withheld} verified Mapping{'' if n_withheld == 1 else 's'}"
                " for this indicator "
                f"{'was' if n_withheld == 1 else 'were'} found but not accepted"
                " in review, so none enters this export. "
                if n_withheld
                else "every candidate for this indicator returned "
                "insufficient evidence or none passed the gate. "
            )
            notes = (
                f"{ABSENCE_MARKER} for indicator {ind} ({indicators[ind].name}). "
                f"{searched}{outcome}"
                f"Reference basis: {law}."
            )
            if language_disclosed:
                notes = f"{notes} {LANGUAGE_UNKNOWN_NOTE}."
            if extra_notes:
                notes = "; ".join([notes, *extra_notes])
            rows.append(
                {
                    "Economy": portals[econ].official_name,
                    "Law Name": doc.law_name,
                    "Law Number / Ref": doc.law_number_ref or "",
                    "Last Amended": last_amended_year(doc.last_amended),
                    "Indicator ID": crosswalk.schemes[crosswalk.emission_scheme][ind],
                    "Article / Section": ABSENCE_MARKER,
                    "Discovery Tag": tag,
                    "Location Reference": "",
                    "Verbatim Snippet": ABSENCE_MARKER,
                    "Mapping Rationale": "",
                    "Source URL": doc.source_url,
                    "Confidence": "",
                    "Notes": notes,
                    LANGUAGE_COLUMN: language_of_source,
                    "Novelty Scope": "",
                    "Controlling Evidence": "",
                    "Relationship To Group": "",
                    VERBATIM_ENGLISH_COLUMN: "",
                    "_chunk_id": "",
                    "_document_id": doc_id,
                    "_repeal_status": "",
                    "_economy_code": econ,
                    "_allow_any_host": bool(synthetic and synthetic.allow_any_host),
                }
            )
    return rows


# ---------------------------------------------------------------------------
# derived scores (supplementary JSON; validated by the battery)
# ---------------------------------------------------------------------------


def derive_scores(records: list[MappingRecord]) -> dict[tuple[str, str], float]:
    """Presence-based derivation with the INVERSE direction map: per economy in
    the record set, every in-scope indicator gets the presence score when a
    verified provision exists, else the complementary absence score.

    6.1/6.4 boundary rule (RDTII 2.1 Guide): if a provision's transfer language
    is ALSO verified as a conditional flow regime (6.4), that provision is the
    conditional reading, not a ban; 6.1 presence therefore requires ban
    evidence from a provision NOT simultaneously claimed by 6.4."""
    passed = [r for r in records if r.verification_status == "passed"]
    present: dict[str, set[str]] = {}
    chunks_64: dict[str, set[str]] = {}
    for r in passed:
        if r.indicator_id == "6.4":
            chunks_64.setdefault(r.economy, set()).add(r.chunk_id)
    for r in passed:
        if r.indicator_id == "6.1" and r.chunk_id in chunks_64.get(r.economy, set()):
            continue
        present.setdefault(r.economy, set()).add(r.indicator_id)
    scores: dict[tuple[str, str], float] = {}
    for econ, inds in present.items():
        for ind, if_present in SCORE_IF_PRESENT.items():
            scores[(econ, ind)] = if_present if ind in inds else 1.0 - if_present
    return scores


# ---------------------------------------------------------------------------
# duplicate provision rows: collapsed before the battery, guarded by it
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CollapsedProvision:
    """One (Economy, Indicator, provision) key that more than one accepted
    Mapping landed on, and what the export did about it: which Mapping's row
    was written and which Mappings' rows it stood in for. Nothing is deleted
    and no Review Decision is touched; the dropped Mappings keep their rows in
    the per-document working JSONs and their decisions in the database."""

    economy: str
    indicator_id: str
    law_name: str
    article_section: str
    kept_mapping_id: str
    dropped_mapping_ids: tuple[str, ...]


def duplicate_key(row: dict) -> tuple:
    """The identity of a provision row: one Economy's one Indicator citing one
    provision of one law. THE shared key - the battery refuses a second row on
    it and the collapse below makes sure a second row never reaches the
    battery, so the two can never disagree about what a duplicate is.

    Law Name is part of it on purpose: two acts of one Economy can both have a
    section 4, and those are two provisions, not one."""
    return (row["Economy"], row["Indicator ID"], row["Law Name"], row["Article / Section"])


def _collapse_rank(row: dict) -> tuple:
    """Sort key deciding which of several rows on one provision survives:
    Controlling Evidence first, then Confidence, then the earlier Mapping id.

    Controlling Evidence outranks Confidence because the two answer different
    questions. Confidence is a mechanical composite of anchoring signals;
    Controlling Evidence is the reconciler's finding that THIS instrument
    governs the provision, and dropping the only row carrying it would leave
    the Economy's Indicator evidenced by a subordinate instrument."""
    return (
        0 if row.get("Controlling Evidence") == "true" else 1,
        -_confidence_rank(row),
        str(row.get("_mapping_id", "")),
    )


def collapse_duplicate_provisions(
    rows: list[dict],
) -> tuple[list[dict], list[CollapsedProvision]]:
    """One row per (Economy, Indicator, law, provision), plus the record of
    what was collapsed.

    A Run reaches this honestly: the Gate shortlists two neighbouring chunks of
    one Document, the Engine quotes a passage in each, and the quote-anchored
    label repair re-derives both labels to the same nearest heading. Two
    accepted Mappings then describe the same provision under the same
    Indicator, and the organizers' sheet has one row for it.

    The row kept is a Controlling Evidence row if the group has one, then the
    highest Confidence, then the earlier Mapping id, so the choice is
    deterministic and two exports of one database agree byte for byte.
    Controlling Evidence leads on purpose: it is the reconciler's answer about
    which instrument governs, and a subordinate row with a better mechanical
    Confidence must never stand in for it. Absence rows are never collapsed:
    'No provision found' is not a provision, and one Economy's Indicator
    carries exactly one of them by construction.

    Callers pass provision rows only; the battery still runs after this and
    still refuses a duplicate pair, which is what makes this a narrowing of
    what ships rather than a weakening of the guard."""
    order: list[tuple] = []
    groups: dict[tuple, list[dict]] = {}
    passthrough: list[dict] = []
    for row in rows:
        if row["Article / Section"] == ABSENCE_MARKER:
            passthrough.append(row)
            continue
        key = duplicate_key(row)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)

    kept_rows: list[dict] = []
    collapsed: list[CollapsedProvision] = []
    for key in order:
        group = groups[key]
        if len(group) == 1:
            kept_rows.append(group[0])
            continue
        ranked = sorted(group, key=_collapse_rank)
        keeper = ranked[0]
        kept_rows.append(keeper)
        collapsed.append(
            CollapsedProvision(
                economy=key[0],
                indicator_id=key[1],
                law_name=key[2],
                article_section=key[3],
                kept_mapping_id=str(keeper.get("_mapping_id", "")),
                dropped_mapping_ids=tuple(
                    str(r.get("_mapping_id", "")) for r in ranked[1:]
                ),
            )
        )
    return kept_rows + passthrough, collapsed


# ---------------------------------------------------------------------------
# the gate battery (green or nothing ships)
# ---------------------------------------------------------------------------


def run_gate_battery(
    rows: list[dict],
    chunk_text_by_id: dict[str, str],
    portals: dict[str, PortalConfig],
    liveness_fn: Callable[[str], bool],
) -> list[str]:
    failures: list[str] = []
    liveness_cache: dict[str, bool] = {}
    seen_keys: set[tuple] = set()
    for i, row in enumerate(rows, start=1):
        where = f"row {i} ({row.get('Indicator ID', '?')}, {row.get('Article / Section', '?')})"
        visible = [k for k in row if not k.startswith("_")]
        if tuple(visible[:13]) != COLUMNS:
            failures.append(f"{where}: column names/order violate the 13-column contract")
            continue
        if row["Discovery Tag"] not in ("NEW", "KNOWN"):
            failures.append(f"{where}: Discovery Tag must be exactly NEW or KNOWN (case-sensitive)")
        for col in REQUIRED:
            if not str(row[col]).strip():
                failures.append(f"{where}: REQUIRED column '{col}' is empty")
        is_absence = row["Article / Section"] == ABSENCE_MARKER
        if not is_absence:
            text = chunk_text_by_id.get(row["_chunk_id"])
            if text is None or row["Verbatim Snippet"] not in text:
                failures.append(f"{where}: verbatim recheck failed (snippet not in source chunk)")
        host = urlparse(row["Source URL"]).netloc
        allowed = portals[row["_economy_code"]].hosts
        if host not in allowed:
            # A user-supplied map-pdf row (--allow-any-host) is exempt from the
            # portal whitelist, but STILL needs a real host and stamps its Notes
            # disclosure. The curated Round 1 lane never sets this flag.
            if row.get("_allow_any_host") and host:
                if ALLOW_ANY_HOST_NOTE not in row["Notes"]:
                    failures.append(
                        f"{where}: off-whitelist host exempted but the Notes"
                        f" disclosure is missing"
                    )
            else:
                failures.append(f"{where}: Source URL host '{host}' not in the portal whitelist {allowed}")
        url = row["Source URL"]
        if url not in liveness_cache:
            liveness_cache[url] = bool(liveness_fn(url))
        if not liveness_cache[url]:
            failures.append(f"{where}: Source URL liveness check failed (dead or error) for {url}")
        repeal = row["_repeal_status"]
        if repeal and _REPEALED_RE.search(repeal):
            if row["Controlling Evidence"] == "true":
                failures.append(
                    f"{where}: a repealed/cancelled instrument cannot be controlling evidence ({repeal})"
                )
            elif "repealed-but-recorded" not in row["Notes"]:
                failures.append(f"{where}: repealed instrument recorded without the Notes flag")
        if any(m in row["Verbatim Snippet"] for m in TEMPLATE_EXAMPLE_MARKERS):
            failures.append(f"{where}: template example-row content detected; delete example rows")
        verbatim_english = str(row.get(VERBATIM_ENGLISH_COLUMN, "")).strip()
        if not is_absence and looks_non_english(row["Verbatim Snippet"]):
            if not verbatim_english:
                failures.append(
                    f"{where}: snippet looks non-English but the appended"
                    f" '{VERBATIM_ENGLISH_COLUMN}' column is missing/empty"
                )
        # The label rule, through the ONE predicate the writer used: a rendering
        # ships unlabelled only where a named person approved it (a Reviewed
        # Gloss). Everything else must open with the non-authoritative label.
        if (
            verbatim_english
            and not gloss_is_reviewed(row.get("_gloss_reviewed_by"))
            and not verbatim_english.startswith(GLOSS_LABEL)
        ):
            failures.append(
                f"{where}: '{VERBATIM_ENGLISH_COLUMN}' must open with its"
                f" non-authoritative label unless a named reviewer approved it"
            )
        if len(row["Mapping Rationale"]) > RATIONALE_MAX:
            failures.append(f"{where}: Mapping Rationale exceeds 300 chars")
        if str(row["Confidence"]).strip():
            c = float(row["Confidence"])
            if not 0.0 <= c <= 1.0:
                failures.append(f"{where}: Confidence {c} outside 0.00-1.00")
        # Controlling-instrument gate: a Controlling
        # Evidence=true row must EVIDENCE its indicator - its classification
        # yields a score contribution - unless the whole group had no fitting
        # member, which the row must then say out loud.
        if (
            row.get("Controlling Evidence") == "true"
            and row.get("_measure_type")
            and row.get("_score_contribution") is None
            and "controlling by legal hierarchy only" not in row["Notes"]
        ):
            failures.append(
                f"{where}: controlling row's classification"
                f" ({row['_measure_type']}) does not evidence its indicator"
                " and the row is not flagged as hierarchy-only"
            )
        # The final guard on provision identity. export_all collapses duplicate
        # provision rows BEFORE this runs and reports how many it collapsed, so
        # a failure here means a duplicate reached the battery by another route
        # and nothing ships. The guard is not removed by the collapse; it is
        # what proves the collapse worked.
        key = duplicate_key(row)
        if key in seen_keys and not is_absence:
            failures.append(f"{where}: duplicate provision-indicator row {key}")
        seen_keys.add(key)
    return failures


def live_url_ok(url: str) -> bool:
    """Opt-in liveness probe for the CLI export's --check-liveness flag (the
    judge path is offline by default; the repro script has its own)."""
    import httpx

    try:
        r = httpx.get(
            url,
            timeout=30.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
        )
        return r.status_code < 400
    except httpx.HTTPError:
        return False


def pointer_gate(
    records: list[MappingRecord],
    text_loader: Callable[[str], str],
    chunk_text_lookup: dict[str, str] | None = None,
) -> tuple[list[str], list[str]]:
    """Section-label battery over CONTROLLING rows: the section label a judge
    verifies first must name the document's OWN nearest heading at the
    quote's exact position (re-derived via SectionLabelIndex, the same
    machinery the label repair uses). Returns (failures, notes).

    Fail-closed zones are NOTES, not passes: a row whose quote cannot be
    located, or whose heading zone is ambiguous (label_at returns None -
    the known schedule-label class), is reported for eyeballing rather
    than silently accepted or silently failed. A quote sitting ABOVE its own
    chunk's heading is one of those zones: the nearest heading then names a
    section that ended before the chunk began, so it cannot judge the row's
    label. A chunk with no heading of its own is NOT such a zone, and a wrong
    label on one still fails. The predicate is shared with the repair
    (quote_is_above_its_chunks_heading in chunk.py) so the two cannot drift."""
    failures: list[str] = []
    notes: list[str] = []
    indexes: dict[str, SectionLabelIndex] = {}
    texts: dict[str, str] = {}
    for r in records:
        if not r.controlling_evidence or r.verification_status != "passed":
            continue
        doc = r.document_id
        if doc not in indexes:
            texts[doc] = text_loader(doc)
            indexes[doc] = SectionLabelIndex(texts[doc])
        t, idx = texts[doc], indexes[doc]
        chunk_span: tuple[int, int] | None = None
        if chunk_text_lookup and r.chunk_id in chunk_text_lookup:
            found = t.find(chunk_text_lookup[r.chunk_id])
            if found >= 0:
                chunk_span = (found, found + len(chunk_text_lookup[r.chunk_id]))
        base = chunk_span[0] if chunk_span is not None else 0
        qpos = t.find(r.verbatim_quote, base)
        if qpos < 0:
            qpos = t.find(r.verbatim_quote)
        if qpos < 0:
            notes.append(f"{r.mapping_id}: quote not located in canonical stream")
            continue
        derived, heading_pos = idx.label_at_with_pos(qpos, end=qpos + len(r.verbatim_quote))
        if derived is None:
            notes.append(
                f"{r.mapping_id}: ambiguous heading zone (fail-closed);"
                f" label kept as {r.section!r}"
            )
            continue
        if quote_is_above_its_chunks_heading(idx, chunk_span, qpos, heading_pos):
            notes.append(
                f"{r.mapping_id}: quote sits above its own chunk's heading, so"
                f" the nearest heading ({derived!r}) names an earlier section"
                f" (fail-closed); label kept as {r.section!r}"
            )
            continue
        cm = _SECTION_TAIL_NUM_RE.search(r.section or "")
        if cm is None:
            failures.append(
                f"{r.mapping_id}: controlling row carries no section number:"
                f" {r.section!r}"
            )
            continue
        dm = _SECTION_TAIL_NUM_RE.search(derived)
        if dm and dm.group(1) != cm.group(1):
            failures.append(
                f"{r.mapping_id}: label {r.section!r} does not name the nearest"
                f" heading at its quote ({derived!r})"
            )
            continue
        if derived.startswith("Part ") and (r.section or "") != derived:
            failures.append(
                f"{r.mapping_id}: Part prefix of {r.section!r} contradicts the"
                f" document's own heading context ({derived!r})"
            )
    return failures, notes


# ---------------------------------------------------------------------------
# consolidated submission.json (judge-recommended structured output)
# ---------------------------------------------------------------------------

# The emitted CER is an explicit null with this disclosure. A
# measured character error rate needs a hand-checked reference the production
# lane never has, so the honest signal is the confidence + dictionary proxy set.
OCR_QUALITY_NOTE = (
    "measured CER requires a hand-checked reference; production uses confidence"
    " + dictionary proxies"
)
_OCR_PROXY_FIELDS = (
    "mean_word_confidence",
    "dictionary_hit_rate",
    "cer_proxy_flag",
    "escalated_to_rapidocr",
    "manual_review",
)
RAW_CONTEXT_WINDOW = 300
# documents.local_path can be stored absolute (pipeline stores str(pdf_path));
# anchor on the first known corpus root so the emitted path is relative POSIX.
_SRC_ANCHORS = ("tests", "data", "raw", "corpus")


def _relative_source_path(local_path: str | None) -> str | None:
    """Normalize documents.local_path to a relative POSIX pointer. A path that
    is already relative (the repro DB stores 'AU/raw/x.pdf' relative to the data
    dir) passes through unchanged; only an absolute path (the judge lane stores
    str(pdf_path), which can be absolute) has its machine prefix stripped by
    anchoring on the first known corpus root."""
    if not local_path:
        return None
    p = PurePosixPath(str(local_path).replace("\\", "/"))
    if not p.is_absolute():
        return p.as_posix()
    parts = p.parts
    for anchor in _SRC_ANCHORS:
        if anchor in parts:
            return PurePosixPath(*parts[parts.index(anchor):]).as_posix()
    return p.as_posix().lstrip("/") or None


def _raw_context(chunk_text: str | None, quote: str, window: int = RAW_CONTEXT_WINDOW) -> str | None:
    """+-window chars around the verbatim quote inside its chunk, clipped to the
    chunk bounds. The quote is a verbatim substring by contract; an absence row
    (no chunk) or a quote we cannot locate yields None rather than a guess."""
    if not chunk_text or not quote:
        return None
    pos = chunk_text.find(quote)
    if pos < 0:
        return None
    start = max(0, pos - window)
    end = min(len(chunk_text), pos + len(quote) + window)
    return chunk_text[start:end]


def _ocr_quality_block(meta: dict | None) -> dict:
    """Honest per-document OCR block: the confidence/dictionary proxies plus
    ocr_applied, an explicit null measured CER, and the disclosure note. Old
    DBs and offline exports carry no proxies, so every field falls back to
    null (a born-digital document also reports proxies null, ocr_applied false)."""
    meta = meta or {}
    block: dict = {"ocr_applied": meta.get("ocr_applied")}
    for f in _OCR_PROXY_FIELDS:
        block[f] = meta.get(f)
    block["ocr_quality_cer"] = None
    block["note"] = OCR_QUALITY_NOTE
    return block


def _run_metadata(
    crosswalk,
    document_meta: dict[str, dict],
    processing_time: dict | None,
    generated_at: str | None,
    config_dir,
    engine: Engine | None = None,
) -> dict:
    models = load_models(config_dir)
    engine = engine or resolve_engine(None, config_dir)
    extractors = sorted(
        {
            (m.get("extractor"), m.get("extractor_version"))
            for m in document_meta.values()
            if m.get("extractor")
        }
    )
    meta: dict = {"tool_version": __version__}
    if generated_at is not None:
        meta["generated_at"] = generated_at
    meta["indicator_scheme"] = crosswalk.emission_scheme
    meta["battery"] = "green"
    # EXACTLY what ran: the one selected Engine plus the shared embedder. The
    # registry is a menu, not a record; listing Engines that never answered
    # would misstate the run. `regcompass engines` lists the menu.
    meta["models"] = {
        "engine": {
            "name": engine.name,
            "display_name": engine.display_name,
            "litellm_model": engine.litellm_model,
        },
        "embedder": {
            "name": models.embedder.name,
            "litellm_model": models.embedder.litellm_model,
        },
    }
    meta["extractors"] = [{"engine": e, "version": v} for e, v in extractors]
    meta["processing_time"] = processing_time or {
        "granularity": "unavailable",
        "note": "run timing is recorded in audit_log; not attached in this export context",
    }
    return meta


def build_submission_json(
    rows: list[dict],
    crosswalk,
    chunk_text_lookup: dict[str, str],
    derived_scores: dict[str, dict[str, float]],
    *,
    document_meta: dict[str, dict] | None = None,
    processing_time: dict | None = None,
    generated_at: str | None = None,
    config_dir=CONFIG_DIR,
    engine: Engine | None = None,
) -> dict:
    """The consolidated judge-facing structured output. provisions[] are the
    battery-passed CSV rows regrouped per (economy, law), so the JSON is a
    faithful structured mirror of submission.csv; each provision adds a
    +-RAW_CONTEXT_WINDOW char raw_context window around its verbatim quote.
    Law-level OCR quality is the honest proxy block (measured CER stays null);
    run_metadata records the Engine that ran and the shared embedder, plus
    extractors, tool version, indicator scheme, and run-level processing time.
    engine defaults to the configured default Engine when the caller does not
    know which one ran. generated_at is injectable and omitted when None so
    goldens stay deterministic."""
    document_meta = document_meta or {}
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        groups.setdefault((row["Economy"], row["Law Name"]), []).append(row)

    by_economy: dict[str, list[dict]] = {}
    for (economy, law_name), law_rows in groups.items():
        # any row in a law group can carry the document id (substantive rows
        # always do); a pure-absence law keeps its reference-basis document.
        doc_id = next((r["_document_id"] for r in law_rows if r.get("_document_id")), "")
        dmeta = document_meta.get(doc_id)
        provisions = [
            {
                "indicator_id": r["Indicator ID"],
                "article_section": r["Article / Section"],
                "discovery_tag": r["Discovery Tag"],
                "location_reference": r["Location Reference"],
                "verbatim_snippet": r["Verbatim Snippet"],
                "verbatim_english": r.get(VERBATIM_ENGLISH_COLUMN, ""),
                "mapping_rationale": r["Mapping Rationale"],
                "confidence": r["Confidence"],
                "notes": r["Notes"],
                "chunk_id": r.get("_chunk_id", ""),
                "raw_context": _raw_context(
                    chunk_text_lookup.get(r.get("_chunk_id", "")), r["Verbatim Snippet"]
                ),
            }
            for r in law_rows
        ]
        by_economy.setdefault(economy, []).append(
            {
                "law_name": law_name,
                "law_number_ref": law_rows[0]["Law Number / Ref"] or None,
                "last_amended": law_rows[0]["Last Amended"] or None,
                "source_url": law_rows[0]["Source URL"],
                "source_pdf_path": _relative_source_path((dmeta or {}).get("local_path")),
                "ocr_quality": _ocr_quality_block(dmeta),
                "provisions": provisions,
            }
        )

    economies = [
        {"economy": eco, "laws": sorted(laws, key=lambda entry: entry["law_name"])}
        for eco, laws in sorted(by_economy.items())
    ]
    return {
        "run_metadata": _run_metadata(
            crosswalk, document_meta, processing_time, generated_at, config_dir, engine
        ),
        "economies": economies,
        "derived_scores": derived_scores,
    }


# ---------------------------------------------------------------------------
# the export
# ---------------------------------------------------------------------------


@dataclass
class ExportResult:
    csv_path: Path
    supplementary_path: Path
    submission_path: Path
    working_json_paths: list[Path]
    rows: list[dict]
    scores: dict
    battery_failures: list[str] = field(default_factory=list)
    # The organizers' filled workbook, and how many battery-green rows the
    # 101-row cap left out of both files.
    xlsx_path: Path | None = None
    rows_cut: int = 0
    # Duplicate provision rows folded into one before the battery judged them:
    # how many rows that removed, and the (Economy, Indicator, law, provision)
    # keys involved. Both are reported by the CLI and the API.
    rows_collapsed: int = 0
    collapsed: list[CollapsedProvision] = field(default_factory=list)
    # Rows whose Verbatim English says no rendering could be produced. Row by
    # row the Export already says so, but a reader scanning a green file has no
    # reason to open every cell, and a gloss lane that failed on ALL of them
    # would look exactly like a green Export. The count is what makes that
    # visible; the CLI and the Run narrate it.
    glosses_unavailable: int = 0


def duplicate_collapse_notice(result: "ExportResult") -> str | None:
    """The one sentence the CLI and the API say about collapsed duplicates, or
    None when nothing was collapsed. It names the count and every (Indicator,
    provision) key, because a row that stood in for another is a claim the
    reviewer is entitled to see."""
    if not getattr(result, "collapsed", None):
        return None
    keys = "; ".join(
        f"{c.indicator_id} {c.article_section} ({c.law_name})" for c in result.collapsed
    )
    return (
        f"duplicate provisions: {result.rows_collapsed} row(s) collapsed into the"
        f" highest-Confidence accepted Mapping, one row per provision -> {keys}"
    )


def _confidence_rank(row: dict) -> float:
    """Sort key for the cap: higher Confidence first. A blank Confidence ranks
    below every scored row, so a cut takes the rows that claim least first."""
    text = str(row.get("Confidence", "")).strip()
    return float(text) if text else -1.0


def _row_order(row: dict) -> tuple:
    return (row["Economy"], row["Indicator ID"], row["Article / Section"])


def select_rows(rows: list[dict], cap: int = ROW_CAP) -> tuple[list[dict], int]:
    """The provision rows that fit the organizers' entry area, and how many
    were cut.

    Output Data holds 101 rows (9 to 109) and every formula that counts them -
    the autofilter, the Pillar fill, the Coverage Matrix COUNTIFS - stops at
    109, so a 102nd row is not a long file, it is an uncounted one. Over the
    cap, Economies take turns: each contributes its next-best row by Confidence
    until the sheet is full, so a single deep Economy cannot crowd the others
    out of the Coverage Matrix. Ties break on (Economy, Indicator ID, Article /
    Section), the same order the file is written in, so the choice is
    deterministic and two exports of one database agree byte for byte.

    The caller passes provisions only: absence rows never reach the workbook,
    so they never spend a slot in it. The CSV keeps every absence row on top of
    what this returns."""
    if len(rows) <= cap:
        return rows, 0
    by_economy: dict[str, list[dict]] = {}
    for row in rows:
        by_economy.setdefault(row["Economy"], []).append(row)
    for queue in by_economy.values():
        queue.sort(key=lambda r: (-_confidence_rank(r),) + _row_order(r))
    kept: list[dict] = []
    economies = sorted(by_economy)
    depth = 0
    while len(kept) < cap and any(len(q) > depth for q in by_economy.values()):
        for economy in economies:
            queue = by_economy[economy]
            if depth < len(queue):
                kept.append(queue[depth])
                if len(kept) == cap:
                    break
        depth += 1
    kept.sort(key=_row_order)
    return kept, len(rows) - len(kept)


def export_all(
    outdir: Path,
    records: list[MappingRecord],
    chunk_text_lookup: dict[str, str],
    gate_cosine_lookup: dict,
    coverage_stats: dict[str, dict],
    liveness_fn: Callable[[str], bool],
    config_dir=CONFIG_DIR,
    classifications: dict[str, ProvisionClassification] | None = None,
    reviews: dict[str, str] | None = None,
    corrections: dict[str, Review] | None = None,
    document_meta: dict[str, dict] | None = None,
    processing_time: dict | None = None,
    generated_at: str | None = None,
    synthetic_docs: dict[str, SyntheticDoc] | None = None,
    engine: Engine | None = None,
    write_xlsx: bool = True,
    glosses: dict | None = None,
    run_pillars: tuple[int, ...] | None = None,
) -> ExportResult:
    """Assemble rows, run the battery, and only then write: one consolidated
    CSV, the per-document working JSONs, the supplementary JSON, and the
    consolidated submission.json (a structured mirror of the CSV plus the
    judge-recommended fields: source_pdf_path, per-document ocr_quality, run
    metadata, provisions grouped per law, and a +-300 char raw_context).

    Duplicate provision rows are collapsed BEFORE the battery judges them
    (collapse_duplicate_provisions): two accepted Mappings can describe the
    same provision under the same Indicator, and the organizers' sheet has one
    row for that. The count and the keys involved land in ExportResult and in
    supplementary.json's duplicate_collapse block, and the battery's duplicate
    guard still runs over what shipped.

    document_meta / processing_time / generated_at (optional): the DB-derived
    inputs to submission.json's run_metadata and per-law OCR block. Omitted on
    direct/offline calls (goldens), where the JSON is deterministic and its
    document fields fall back to nulls; generated_at is left out entirely when
    None so the golden stays byte-stable.

    engine (optional): the Engine that produced these records, named in
    run_metadata. export_from_db reads it off the run's audit rows; a direct
    call that does not know falls back to the configured default Engine.

    classifications (M12, optional): mapping_id -> ProvisionClassification.
    When given, records get measure_type / rdtii_score_contribution filled and
    the supplementary derived scores switch from the presence-based
    approximation to the classification rubric (derive_scores_v2), each cell
    naming its controlling provision. The 13-column contract is unchanged
    either way.

    write_xlsx (final round): also fill and save the organizers' workbook
    (submission.xlsx) from the same rows. The golden lane turns it off: an xlsx
    is a zip whose entries carry write timestamps, so it is not byte-pinnable
    evidence the way the CSV and the JSONs are.

    glosses (final round, optional): mapping_id -> the GlossRecord stored for
    the Run being exported. It fills the Verbatim English column, keeping the
    non-authoritative label unless a named person approved the text; the
    committed config/verbatim_english.json answers for Mappings it does not
    cover, which is what keeps the Round 1 evidence byte-for-byte stable.

    reviews (U0, optional): mapping_id -> review_status. When given, the human
    review gate applies: ONLY review_status == "accepted"
    records enter the export; rejected, flagged, and unreviewed records are
    all excluded. An indicator that loses every record earns its absence row
    like any other zero, and its Notes say the evidence was not accepted.

    corrections (optional): mapping_id -> the Review Decision of every
    CORRECTED Mapping. A corrected Mapping counts as accepted, but ships under
    the reviewer's Indicator with the reviewer's reason as its rationale and
    the override disclosed in Notes; its Confidence stays the pipeline's. The
    Indicator the Engine proposed gets nothing from it: it counts there as
    evidence found and not accepted. A Mapping marked corrected with no
    correction given is held back like any other unaccepted one.

    run_pillars (optional): the Pillars the Run itself was asked to search, off
    its Run Record. Given, absence rows are written for exactly those, so a Run
    with nothing accepted never claims a search of a Pillar it did not make.
    Omitted, the Pillars are read off the records and the Gate scores."""
    outdir = Path(outdir)
    corpus = load_corpus(config_dir)
    # Off-corpus documents (a live crawl catch, a map-pdf input) get a synthetic
    # CorpusDoc so the export never KeyErrors on their document_id. The curated
    # corpus stays authoritative: real entries override any synthetic one, so a
    # normal run (every doc in corpus.yaml) is byte-for-byte unchanged.
    synthetic_docs = synthetic_docs or {}
    if synthetic_docs:
        corpus = {**{k: s.corpus_doc for k, s in synthetic_docs.items()}, **corpus}
    crosswalk = load_crosswalk(config_dir)
    matrix = load_known_matrix(config_dir)
    portals = load_portals(config_dir)

    if classifications is not None:
        records = apply_classifications(records, classifications)
    passed = [r for r in records if r.verification_status == "passed"]
    review_gate: dict | None = None
    withheld: dict[tuple[str, str], int] = {}
    # mapping_id -> the Indicator the Engine proposed, for every record that
    # ships under a reviewer's corrected Indicator instead.
    proposed: dict[str, str] = {}
    reviewers: dict[str, str | None] = {}
    if reviews is not None:
        corrections = {
            m: c for m, c in (corrections or {}).items()
            if reviews.get(m) == "corrected" and c.corrected_indicator_id
        }
        for r in passed:
            # A corrected Mapping is evidence found but not accepted FOR THE
            # INDICATOR THE ENGINE PROPOSED, whatever it becomes elsewhere.
            if reviews.get(r.mapping_id) != "accepted":
                key = (r.economy, r.indicator_id)
                withheld[key] = withheld.get(key, 0) + 1
        statuses = [reviews.get(r.mapping_id) for r in passed]
        overrides = []
        for r in sorted(passed, key=lambda r: r.mapping_id):
            c = corrections.get(r.mapping_id)
            if c is None:
                continue
            overrides.append(
                {
                    "mapping_id": r.mapping_id,
                    "original_indicator_id": r.indicator_id,
                    "corrected_indicator_id": c.corrected_indicator_id,
                    "reviewer": c.reviewer,
                    "decided_at": c.model_dump(mode="json")["reviewed_at"],
                    "reason": (c.comment or "").strip(),
                }
            )
        review_gate = {
            "n_verified": len(passed),
            "n_accepted": statuses.count("accepted"),
            "n_corrected": statuses.count("corrected"),
            "n_rejected": statuses.count("rejected"),
            "n_flagged": statuses.count("flagged"),
            "n_unreviewed": statuses.count(None),
            "overrides": overrides,
        }
        names = (
            {k: d.name for k, d in load_indicators(config_dir).items()} if corrections else {}
        )
        shipped = []
        for r in passed:
            status = reviews.get(r.mapping_id)
            if status == "accepted":
                shipped.append(r)
            elif r.mapping_id in corrections:
                proposed[r.mapping_id] = r.indicator_id
                reviewers[r.mapping_id] = corrections[r.mapping_id].reviewer
                shipped.append(apply_correction(r, corrections[r.mapping_id], names))
        passed = shipped
    # Committed review-drop lane: named records a
    # human review rejected, with reasons, in config/review_drops.json.
    # Checkpoints stay untouched (the record and its failure-free trail
    # remain in the frozen evidence); the export excludes the row and the
    # supplementary discloses the exclusion. Entries matching no record are
    # ignored: the config is shared by fixture-scale and corpus-scale runs.
    drops = load_review_drops(config_dir)
    applied_drops = [
        {"mapping_id": r.mapping_id, "reason": drops[r.mapping_id]}
        for r in passed
        if r.mapping_id in drops
    ]
    if applied_drops:
        passed = [r for r in passed if r.mapping_id not in drops]
    per_chunk = Counter(r.chunk_id for r in passed)

    # How each Document's bytes reached the Corpus. A seeded ('fixture')
    # Document is a CURATED corpus entry, so it never becomes a SyntheticDoc
    # and the source kind is the only place the fact survives.
    source_kinds = {
        doc_id: (meta or {}).get("source_kind")
        for doc_id, meta in (document_meta or {}).items()
    }

    # A secondary reference the Portal published beside a Document, recorded by
    # Discovery on the Corpus row (documents.notes). Absent for almost every
    # Document, which is why it is read the same tolerant way as the rest.
    # It rides the PROVISION rows only. The other disclosures are true of every
    # row built on a Document, but this one names a second file for one
    # instrument, and an absence row's reference-basis document is picked per
    # Economy and is usually a different law; carrying it there would point a
    # reader at a translation of something they are not reading.
    document_notes = {
        doc_id: (meta or {}).get("notes")
        for doc_id, meta in (document_meta or {}).items()
    }

    def _synthetic_kwargs(document_id: str) -> dict:
        sd = synthetic_docs.get(document_id)
        notes = synthetic_notes(
            sd, source_kinds.get(document_id), document_notes.get(document_id)
        )
        if sd is None:
            # No stand-in document, so no whitelist exemption either: a seeded
            # Document keeps its official Source URL and is checked like any
            # other. Only the disclosure travels.
            return {"extra_notes": notes} if notes else {}
        return {"extra_notes": notes, "allow_any_host": sd.allow_any_host}

    # Each Document's recorded Language, the source of the Language of Source
    # column. Absent (a direct offline call, or a pre-migration database) the
    # row falls back to the Economy's Portal Language or discloses "Other".
    languages = {
        doc_id: (meta or {}).get("language") for doc_id, meta in (document_meta or {}).items()
    }
    # A Document nothing can stand in for: not in the curated corpus, and no
    # synthetic stand-in either, which happens when it has no official Source
    # URL to build one from. That used to surface as a bare KeyError on the
    # document id, which tells a reviewer nothing about what to do. It is a
    # gate failure like any other, and it names the fix, by the names the
    # screens use: the Document's title and the controls as labelled.
    unknown = sorted({r.document_id for r in passed if r.document_id not in corpus})
    if unknown:
        raise ExportGateError(
            [
                f"{((document_meta or {}).get(doc_id) or {}).get('title') or doc_id}:"
                " no Source URL recorded, so no shippable row can be built for"
                " it. On Start a Run, open Add document, find it in the Corpus"
                " list, press Set Source URL, then export again."
                for doc_id in unknown
            ]
        )
    # Which lane extracted each Document, read off its recorded extractor: an
    # HTML page is cited by section path, a PDF by page. Absent (a direct
    # offline call) every Document reads as a PDF, as it always did.
    formats = {
        doc_id: format_for_extractor((meta or {}).get("extractor"))
        for doc_id, meta in (document_meta or {}).items()
    }
    rows = [
        build_row(
            r, corpus[r.document_id], crosswalk, matrix, gate_cosine_lookup,
            per_chunk, config_dir, language=languages.get(r.document_id),
            glosses=glosses, format_tag=formats.get(r.document_id, "pdf"),
            proposed_indicator_id=proposed.get(r.mapping_id),
            reviewer=reviewers.get(r.mapping_id),
            **_synthetic_kwargs(r.document_id),
        )
        for r in passed
    ]
    # One row per provision before the battery sees them: two accepted
    # Mappings can describe the same provision under the same Indicator (two
    # neighbouring chunks whose repaired labels meet), and the organizers'
    # sheet has one row for that. The battery's duplicate guard still runs
    # after this.
    rows, collapsed = collapse_duplicate_provisions(rows)
    rows += build_absence_rows(
        passed, corpus, crosswalk, coverage_stats, config_dir,
        run_pillars=(
            tuple(sorted(run_pillars)) if run_pillars
            else run_pillars_of(records, gate_cosine_lookup, config_dir)
        ),
        languages=languages,
        synthetic_docs=synthetic_docs,
        source_kinds=source_kinds,
        withheld=withheld,
    )
    rows.sort(key=lambda r: (r["Economy"], r["Indicator ID"], r["Article / Section"]))

    scores = derive_scores(passed)
    score_cells: dict[tuple[str, str], ScoreCell] | None = None
    if classifications is not None:
        score_cells = derive_scores_v2(passed, classifications)
    failures = run_gate_battery(rows, chunk_text_lookup, portals, liveness_fn)
    failures += [
        f"derived score {s} for {econ} {ind} outside the allowed values"
        for (econ, ind), s in scores.items()
        if s not in (0.0, 0.5, 1.0) or (ind == "7.3" and s not in (0.0, 1.0))
    ]
    if score_cells is not None:
        failures += [
            f"rubric score {c.score} for {econ} {ind} outside the allowed values"
            for (econ, ind), c in score_cells.items()
            if c.score not in V2_ALLOWED_SCORES[ind]
        ]
    if failures:
        raise ExportGateError(failures)

    # The battery judges every assembled row; the cap then decides which of
    # those green rows fit the organizers' 101-row entry area. The budget is
    # spent on PROVISIONS alone, because only provisions go into Output Data:
    # counting absence rows against it would leave entry slots empty while
    # battery-green evidence was cut. The CSV then carries the chosen
    # provisions plus every absence row, its own earned-zero lineage.
    provision_rows = [r for r in rows if r["Article / Section"] != ABSENCE_MARKER]
    absence_rows = [r for r in rows if r["Article / Section"] == ABSENCE_MARKER]
    provision_rows, rows_cut = select_rows(provision_rows)
    rows = sorted(provision_rows + absence_rows, key=_row_order)

    outdir.mkdir(parents=True, exist_ok=True)
    csv_path = outdir / "submission.csv"
    header = list(COLUMNS) + list(EXTRA_COLUMNS)
    # utf-8-sig: the BOM makes Excel-by-double-click decode the verbatim
    # typography (em dashes, curly quotes in the quoted provisions) correctly
    # on Windows and non-UTF-8-locale machines; Python csv, pandas, and Excel
    # all strip it on read.
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for row in rows:
            writer.writerow([row[col] for col in header])

    # The organizers' own workbook holds the provision rows the CSV holds; the
    # CSV adds the absence rows. "No provision found" is our earned-zero
    # evidence and keeps its Round 1 place in the CSV, but Output Data is one
    # row per provision: an absence row there would spend an entry slot and,
    # through the Pillar formula, be counted by their Coverage Matrix as a
    # provision we cited.
    xlsx_path: Path | None = None
    if write_xlsx:
        xlsx_path = write_workbook(
            template_path(config_dir), outdir / "submission.xlsx", provision_rows
        ).path

    working_paths: list[Path] = []
    by_doc: dict[str, list[MappingRecord]] = {}
    for r in passed:
        by_doc.setdefault(r.document_id, []).append(r)
    for doc_id, recs in sorted(by_doc.items()):
        doc = corpus[doc_id]
        slug = doc_id.removeprefix("doc_")
        # One working file per (document, Pillar) for the Pillars this document
        # actually produced records for. The Pillar is the number BEFORE the
        # first dot: a string prefix test would file 12.1 under Pillar 1.
        by_pillar: dict[int, list[MappingRecord]] = {}
        for r in recs:
            by_pillar.setdefault(int(r.indicator_id.split(".")[0]), []).append(r)
        for pillar in sorted(by_pillar):
            pillar_recs = by_pillar[pillar]
            dm = DocumentMapping(
                economy=doc.economy,
                law_name=doc.law_name,
                law_number_ref=doc.law_number_ref,
                last_amended=doc.last_amended,
                document_id=doc_id,
                source_url=doc.source_url,
                mappings=pillar_recs,
            )
            p = outdir / f"mapping_{doc.economy.lower()}_{slug}_{pillar}.json"
            p.write_text(dm.model_dump_json(indent=1), encoding="utf-8")
            working_paths.append(p)

    nested: dict[str, dict[str, float]] = {}
    for (econ, ind), s in sorted(scores.items()):
        nested.setdefault(econ, {})[ind] = s
    supplementary: dict = {
        "derived_scores": nested,
        "score_direction": "inverse (0 = open, 1 = restrictive)",
        "score_method": (
            "presence-based: a verified controlling provision yields the "
            "SCORE_IF_PRESENT value for its indicator (7.1/7.2 invert: the "
            "presence of a framework is openness); absence yields the complement. "
            "Sectoral 0.5 granularity is a human-review refinement."
        ),
        "n_rows": len(rows),
        "n_substantive": sum(1 for r in rows if r["Article / Section"] != ABSENCE_MARKER),
        "n_absence": sum(1 for r in rows if r["Article / Section"] == ABSENCE_MARKER),
        "coverage_stats": coverage_stats,
        "battery": "green",
    }
    if rows_cut:
        supplementary["row_cap"] = {
            "cap": ROW_CAP,
            "rows_cut": rows_cut,
            "rule": (
                "the organizers' Output Data entry area is rows 9 to 109 and every"
                " formula that counts it stops at 109; over the cap, Economies take"
                " turns contributing their next-best row by Confidence"
            ),
        }
    if collapsed:
        supplementary["duplicate_collapse"] = {
            "rows_collapsed": sum(len(c.dropped_mapping_ids) for c in collapsed),
            "rule": (
                "one row per (Economy, Indicator, law, provision): where two"
                " accepted Mappings described the same provision under the same"
                " Indicator, the one with the highest Confidence was written"
                " (ties broken on the earlier Mapping id). No Review Decision"
                " was changed and the dropped Mappings keep their per-document"
                " working-JSON rows; the battery's duplicate guard still ran"
                " over what shipped"
            ),
            "collapsed": [
                {
                    "economy": c.economy,
                    "indicator_id": c.indicator_id,
                    "law_name": c.law_name,
                    "article_section": c.article_section,
                    "kept_mapping_id": c.kept_mapping_id,
                    "dropped_mapping_ids": list(c.dropped_mapping_ids),
                }
                for c in collapsed
            ],
        }
    if applied_drops:
        supplementary["review_drops"] = {
            "n_dropped": len(applied_drops),
            "dropped": applied_drops,
            "note": (
                "records excluded by the committed human-review drop lane "
                "(config/review_drops.json); checkpoints and audit trails "
                "keep the full record"
            ),
        }
    # Blank Last Amended is a WARNING, never a battery failure: the value is
    # mechanical (front matter / register URL) or recorded from the official
    # text (config/law_metadata.yaml), and a law never amended, or one whose
    # date nothing establishes, honestly ships blank.
    n_blank_last_amended = sum(1 for r in rows if not str(r["Last Amended"]).strip())
    if n_blank_last_amended:
        supplementary["warnings"] = {
            "last_amended_blank_rows": n_blank_last_amended,
            "note": (
                "Last Amended derives mechanically from the document's own "
                "front matter or register URL, or is recorded from the official "
                "text in config/law_metadata.yaml, and stays blank for a law "
                "never amended or when no date is established, never guessed."
            ),
        }
    if score_cells is not None:
        nested_v2: dict[str, dict[str, float]] = {}
        details: dict[str, dict[str, dict]] = {}
        for (econ, ind), c in sorted(score_cells.items()):
            nested_v2.setdefault(econ, {})[ind] = c.score
            details.setdefault(econ, {})[ind] = {
                "score": c.score,
                "controlling_mapping_id": c.controlling_mapping_id,
                "basis": c.basis,
            }
        classified = [r for r in passed if r.mapping_id in classifications]
        supplementary.update(
            {
                "derived_scores": nested_v2,
                "score_method": (
                    "classification rubric (M12): the model labels each verified "
                    "provision from a closed menu (measure nature, data scope, "
                    "application, government-data flag); scores derive in code from "
                    "the RDTII 2.1 Guide's per-indicator criteria "
                    "over the classified records. Labels are model "
                    "judgment; every cell names its controlling provision for "
                    "human review. Unclassified records never drive a score."
                ),
                "score_details": details,
                "presence_scores_v1": nested,
                "classification_summary": {
                    "n_passed": len(passed),
                    "n_classified": len(classified),
                    "n_unclassified": len(passed) - len(classified),
                    "n_not_data_measure": sum(
                        1 for r in classified if r.measure_type == "not_data_measure"
                    ),
                    "n_confirmation_refused": sum(
                        1 for r in classified if not classifications[r.mapping_id].confirmed
                    ),
                },
            }
        )
    if review_gate is not None:
        supplementary["review_gate"] = dict(
            review_gate,
            rule=(
                "only review_status == 'accepted' or 'corrected' records enter the"
                " export; a corrected record ships under the reviewer's Indicator"
                " with the reviewer's reason as its rationale, the override named"
                " in Notes and the pipeline's Confidence, and the Indicator the"
                " Engine proposed gets no evidence from it"
            ),
        )
    supplementary_path = outdir / "supplementary.json"
    supplementary_path.write_text(json.dumps(supplementary, indent=1), encoding="utf-8")

    # Consolidated submission.json: a structured mirror of the CSV (provisions
    # grouped per law) with raw_context, per-document OCR quality, and run
    # metadata. derived_scores mirror exactly what supplementary emits (v2 when
    # classifications drove the rubric, else the presence-based v1).
    submission = build_submission_json(
        rows,
        crosswalk,
        chunk_text_lookup,
        supplementary["derived_scores"],
        document_meta=document_meta,
        processing_time=processing_time,
        generated_at=generated_at,
        config_dir=config_dir,
        engine=engine,
    )
    submission_path = outdir / "submission.json"
    submission_path.write_text(
        json.dumps(submission, indent=1, ensure_ascii=False), encoding="utf-8"
    )
    if not (csv_path.exists() and supplementary_path.exists() and submission_path.exists()):
        raise ExportGateError(
            ["consolidated CSV + supplementary JSON + submission JSON must all exist"]
        )
    return ExportResult(
        csv_path=csv_path,
        supplementary_path=supplementary_path,
        submission_path=submission_path,
        working_json_paths=working_paths,
        rows=rows,
        scores=scores,
        battery_failures=[],
        xlsx_path=xlsx_path,
        rows_cut=rows_cut,
        rows_collapsed=sum(len(c.dropped_mapping_ids) for c in collapsed),
        collapsed=collapsed,
        glosses_unavailable=sum(
            1 for r in rows if str(r.get(VERBATIM_ENGLISH_COLUMN, "")) == GLOSS_UNAVAILABLE
        ),
    )
