"""The Comparison: two Runs on one Economy and Pillar, one per Engine, laid
out Indicator by Indicator.

A reviewer who has run the same Economy and Pillar on Engine A and then on
Engine B asks one question of the result: did the two Engines land on the SAME
controlling provision? This module answers it and nothing else. It is pure:
every fact it needs (the two Run Records, each Run's Mappings, the Indicator
list, the Document titles, the Gate cosines) is handed in, and it opens no
database, reads no configuration and calls no model. The server does the I/O;
the command line and the tests reuse the same builder.

Three rules decide what a Comparison says:

  * WHICH Mapping stands for a side. M8 flags exactly one controlling Mapping
    per (Run, Indicator), so that is the one compared. A Run whose
    reconciliation has not run yet falls back to the highest Confidence
    Mapping, so the screen is never empty for a reason a reviewer cannot see.
  * WHAT counts as agreement. The Document AND the location reference
    (section plus subsection), compared with whitespace collapsed and case
    folded: section labels are repaired at export time and raw labels differ in
    spacing, so "S.  13" and "s. 13" are one provision, while "(1)" and "(2)"
    are two. Anything else the two sides chose is a disagreement.
  * WHAT a blank means. An Indicator only one Engine answered is marked
    only_a / only_b, never quietly dropped; one neither answered is neither.

Beside that Indicator view sits the provision view the organizers' Engine
Comparison sheet asks for: EVERY evidence provision either Run produced, paired
across the two Engines by provision identity, plus a per-Engine summary of each
pass. How a provision is matched is set out on ``PROVISION_MATCH_BASIS``.
"""

from __future__ import annotations

import io
import re
from collections import Counter
from typing import Callable, Iterable, Literal, Mapping, Sequence

from pydantic import BaseModel, Field

from regcompass.audit import SourceLink, build_source_link
from regcompass.contracts import MappingRecord, RunRecord
from regcompass.export import (
    FRAMEWORK_FAMILY_NAMES,
    FRAMEWORK_INDICATORS,
    article_section,
    confidence_score,
    framework_family_match,
    location_reference,
)
from regcompass.extract import format_for_extractor
from regcompass.labels import drafting_label

Agreement = Literal["agree", "disagree", "only_a", "only_b", "neither"]

#: Named in the payload so a reader never has to guess how "the same provision"
#: was decided.
MATCH_BASIS = (
    "Document plus location reference (section and subsection),"
    " whitespace-normalised and case-insensitive"
)

_WHITESPACE = re.compile(r"\s+")
_UNSAFE_IN_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


#: How two Mappings are decided to be one provision in the provision view.
PROVISION_MATCH_BASIS = (
    "Same Document and same section or article number (s. 26, s. 26(1) and"
    " Section 26 are one provision; the number is read from the section label,"
    " and a label with no number is matched on its whole text). Within one"
    " provision the two Engines' Mappings are paired most-alike first: same"
    " Indicator, citation and quoted words; then same Indicator and citation;"
    " then same Indicator; then same citation; then any remaining pair."
    " A citation is the number plus its bracketed subdivisions, spaces and"
    " case ignored (Section 26(1) and s. 26 (1) are one citation, s. 26(1) and"
    " s. 26 are two); quoted words are compared as word sequences, so spacing,"
    " case and punctuation are not a difference"
)

#: Start and end of a pass are shown as clock times where the live hour is
#: held, Bangkok (UTC+7, no daylight saving). Run Records keep UTC.
DISPLAY_UTC_OFFSET_HOURS = 7

_SECTION_NUMBER = re.compile(
    r"(?:\bs|\bss|\bsec|\bsections?|\barts?|\barticles?|\bpasal|\bregs?"
    r"|\bregulations?|\brules?|\bclauses?|\bcl)\.?\s*(\d+[A-Za-z]{0,3})",
    re.I,
)
_CN_ARTICLE = re.compile(r"第\s*([0-9一二三四五六七八九十百千零〇两]+)\s*条")
_LEADING_NUMBER = re.compile(r"^\s*(\d+[A-Za-z]{0,3})\b")
_WORD = re.compile(r"\w+")
_SUBDIVISION = re.compile(r"\([^)]*\)")


class ComparisonMismatch(ValueError):
    """The two Runs are not a Comparison: a Comparison is one Economy and one
    Pillar set seen by two Engines, so a pair that disagrees on either is
    refused with both values named rather than silently compared."""


# ---------------------------------------------------------------------------
# the payload
# ---------------------------------------------------------------------------


class ComparisonSide(BaseModel):
    """What one Run chose for one Indicator: the controlling Mapping, with the
    Document's title resolved so the screen and the file both read as prose."""

    mapping_id: str
    document_id: str
    document_title: str
    section: str
    subsection: str | None = None
    page_number: int | None = None
    verbatim_quote: str
    confidence: float | None = None
    controlling_evidence: bool = False
    # Filled from the Review Decision store when there is one; a Comparison
    # built before any review is a Comparison with no Decisions, not an error.
    review_status: str | None = None
    # Where this side's Mapping came from, so a Comparison row is followable to
    # the source exactly as an audit row is. Same rule, same function.
    source_url: str | None = None
    location_reference: str = ""
    source_link: SourceLink | None = None
    # "html" when the Document has no pages, so page_number is not one.
    format: Literal["pdf", "html"] = "pdf"


class ComparisonRow(BaseModel):
    """One Indicator, both sides, and the agreement mark between them."""

    indicator_id: str
    indicator_name: str
    agreement: Agreement
    a: ComparisonSide | None = None
    b: ComparisonSide | None = None


class ComparisonRun(BaseModel):
    """One side's header: the Run Record as it was saved, plus the two things a
    reviewer reads it for that are not columns on the record (the Engine's
    display name and the wall time between start and end)."""

    record: RunRecord
    engine_display_name: str | None = None
    duration_s: float | None = None


FoundBy = Literal["Engine A only", "Engine B only", "Both"]
Differs = Literal["Yes", "No", "n/a"]


class ProvisionRow(BaseModel):
    """One row of the organizers' provision-by-provision table: a provision
    one or both Engines cited, the organizers' dropdown values for it, and a
    difference line written by rule, never by a model. The a_/b_ fields are
    the evidence each side's cell was decided from."""

    law_name: str
    article_section: str
    indicator_id: str
    found_by: FoundBy
    indicator_differs: Differs
    citation_differs: Differs
    quoted_words_differ: Differs
    difference: str
    document_id: str
    a_mapping_id: str | None = None
    b_mapping_id: str | None = None
    a_indicator_id: str | None = None
    b_indicator_id: str | None = None
    a_article_section: str | None = None
    b_article_section: str | None = None
    a_quote: str | None = None
    b_quote: str | None = None


class EngineSummary(BaseModel):
    """One Engine's pass as block 1 of the organizers' sheet reads it. A pass is
    the Run plus any Discovery on the same Economy that ran after the previous
    Run there and before this one: on the live day Engine A's pass starts by
    fetching, and Engine B's must fetch nothing, so its count is whatever the
    records between the two Runs say, normally 0."""

    engine: str | None = None
    provider_model: str = ""
    run_id: str
    discovery_run_ids: list[str] = Field(default_factory=list)
    started_at: str
    ended_at: str | None = None
    start_hhmm: str = ""
    end_hhmm: str = ""
    elapsed_minutes: float | None = None
    documents_fetched: int = 0
    cost_usd: float = 0.0
    # "provider" when the provider reported what it billed, else "metered":
    # our own count at the Engine's declared prices.
    cost_basis: Literal["provider", "metered"] = "metered"


class Comparison(BaseModel):
    """Everything the Comparison screen renders and the download carries."""

    economy: str
    pillars: list[int] = Field(default_factory=list)
    match_basis: str = MATCH_BASIS
    run_a: ComparisonRun
    run_b: ComparisonRun
    n_indicators: int = 0
    n_agree: int = 0
    n_disagree: int = 0
    n_only_a: int = 0
    n_only_b: int = 0
    n_neither: int = 0
    # Set when the pairing had to reach for something the reader should know
    # about, such as an Engine outside the two declared ones.
    note: str | None = None
    rows: list[ComparisonRow] = Field(default_factory=list)
    # the organizers' Engine Comparison sheet: per-pass summary and every
    # provision either Engine cited
    provision_match_basis: str = PROVISION_MATCH_BASIS
    pass_a: EngineSummary | None = None
    pass_b: EngineSummary | None = None
    n_provisions: int = 0
    n_found_by_a_only: int = 0
    n_found_by_b_only: int = 0
    n_found_by_both: int = 0
    provisions: list[ProvisionRow] = Field(default_factory=list)
    # 7.1 and 7.2 go into the evidence file once per Economy, and only on a law
    # of the right family; this names each side that has no such law, so the
    # file's missing row is explained where the two Engines are compared.
    framework_note: str | None = None


# ---------------------------------------------------------------------------
# the rules
# ---------------------------------------------------------------------------


def normalise(value: str | None) -> str:
    """A location reference as the match sees it: inner whitespace collapsed,
    ends stripped, case folded. None and "" are the same absent subsection."""
    return _WHITESPACE.sub(" ", value or "").strip().casefold()


def location_key(record: MappingRecord) -> tuple[str, str, str]:
    """The identity a match is decided on: Document plus location reference."""
    return (record.document_id, normalise(record.section), normalise(record.subsection))


def duration_seconds(record: RunRecord) -> float | None:
    """Wall seconds between a Run Record's start and end, or None when it never
    ended (a running or crashed Run has no duration to report)."""
    from datetime import datetime

    if not record.started_at or not record.ended_at:
        return None
    try:
        start = datetime.fromisoformat(record.started_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(record.ended_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (end - start).total_seconds()


def is_evidence(record: MappingRecord) -> bool:
    """A Mapping that stands for a side. A dropped record failed the verbatim
    check and an insufficient-evidence record says the Engine found nothing:
    neither is what an Engine CHOSE, so neither may carry a side."""
    return record.verification_status != "dropped" and not record.insufficient_evidence


def resolve_confidence(
    record: MappingRecord,
    gate_cosines: Mapping[tuple[str, str], float] | None,
    per_chunk: Mapping[str, int] | None = None,
) -> float | None:
    """The Mapping's Confidence, falling back to the same mechanical composite
    the audit view computes from this Run's Gate cosines.

    mappings.confidence stays NULL until the composite is assigned, so a
    Comparison that read the column alone would show a blank beside an audit
    view showing a number, and a reviewer would reasonably read the blank as a
    weaker answer. Never a model's self-reported certainty."""
    if record.confidence is not None:
        return record.confidence
    if not gate_cosines:
        return None
    cosine = gate_cosines.get((record.chunk_id, record.indicator_id))
    if cosine is None:
        return None
    n_indicators = (per_chunk or {}).get(record.chunk_id, 1)
    return confidence_score(
        cosine, len(record.verbatim_quote), n_indicators, record.extraction_attempts
    )


def controlling_mapping(records: Sequence[MappingRecord]) -> MappingRecord | None:
    """The one Mapping that stands for this side: M8's controlling evidence,
    or the highest Confidence Mapping when reconciliation has not flagged one.
    mapping_id breaks a tie so the choice is the same on every call."""
    usable = [r for r in records if is_evidence(r)]
    if not usable:
        return None
    flagged = [r for r in usable if r.controlling_evidence]
    pool = flagged or usable
    return max(pool, key=lambda r: (r.confidence if r.confidence is not None else -1.0,
                                    r.mapping_id))


def _indicator_names(
    indicators: Sequence[str] | Mapping[str, str],
) -> dict[str, str]:
    """Accepts the Indicator list either as ids in registry order or as an
    ordered id -> name mapping, so a caller with the registry to hand passes
    the names and a caller without one still gets rows."""
    if isinstance(indicators, Mapping):
        return {str(k): str(v) for k, v in indicators.items()}
    return {str(i): "" for i in indicators}


def _side(
    record: MappingRecord | None,
    document_titles: Mapping[str, str] | None,
    gate_cosines: Mapping[tuple[str, str], float] | None,
    per_chunk: Mapping[str, int],
    reviews: Mapping[str, str] | None,
    document_sources: Mapping[str, Mapping[str, object]] | None = None,
) -> ComparisonSide | None:
    if record is None:
        return None
    titles = document_titles or {}
    source = (document_sources or {}).get(record.document_id) or {}
    source_url = source.get("source_url")
    source_url = str(source_url) if source_url else None
    extractor = source.get("extractor")
    format_tag = format_for_extractor(str(extractor) if extractor is not None else None)
    location = location_reference(record, format_tag)
    return ComparisonSide(
        mapping_id=record.mapping_id,
        document_id=record.document_id,
        document_title=titles.get(record.document_id) or record.document_id,
        section=record.section,
        subsection=record.subsection,
        page_number=record.page_number,
        verbatim_quote=record.verbatim_quote,
        confidence=resolve_confidence(record, gate_cosines, per_chunk),
        controlling_evidence=bool(record.controlling_evidence),
        review_status=(reviews or {}).get(record.mapping_id),
        source_url=source_url,
        format=format_tag,
        location_reference=location,
        source_link=build_source_link(
            document_id=record.document_id,
            source_url=source_url,
            format_tag=format_tag,
            location_reference=location,
        ),
    )


def _by_indicator(records: Iterable[MappingRecord]) -> dict[str, list[MappingRecord]]:
    out: dict[str, list[MappingRecord]] = {}
    for record in records:
        out.setdefault(record.indicator_id, []).append(record)
    return out


def agreement_of(
    a: MappingRecord | None, b: MappingRecord | None
) -> Agreement:
    """The mark between two sides of one Indicator."""
    if a is None and b is None:
        return "neither"
    if b is None:
        return "only_a"
    if a is None:
        return "only_b"
    return "agree" if location_key(a) == location_key(b) else "disagree"


def compare_runs(
    run_a: RunRecord,
    run_b: RunRecord,
    mappings_a: Sequence[MappingRecord],
    mappings_b: Sequence[MappingRecord],
    indicators: Sequence[str] | Mapping[str, str],
    *,
    engine_display_names: Mapping[str, str] | None = None,
    document_titles: Mapping[str, str] | None = None,
    # document_id -> the stored Document's own facts (source_url, extractor),
    # exactly as Storage.document_meta hands them over. Absent means no Portal
    # address is known and each side links to the app's stored copy.
    document_sources: Mapping[str, Mapping[str, object]] | None = None,
    gate_cosines: Mapping[tuple[str, str], float] | None = None,
    reviews_a: Mapping[str, str] | None = None,
    reviews_b: Mapping[str, str] | None = None,
    note: str | None = None,
    # Every Run Record on the Economy (Runs and Discoveries), which is where
    # each pass's Discoveries are found; absent means each pass is its Run.
    economy_records: Sequence[RunRecord] = (),
    # Engine key -> "provider / model" as the organizers' sheet names it.
    engine_models: Mapping[str, str] | None = None,
    # Whether a Mapping's law can carry its Economy's 7.1/7.2 row, asked the
    # way the export asks it (pipeline.framework_checker).
    framework_check: Callable[[MappingRecord], bool] | None = None,
) -> Comparison:
    """Pair two Runs Indicator by Indicator.

    ``indicators`` is the list to render, in order: either ids or an ordered
    id -> name mapping. An Indicator either side answered but the list does not
    name is appended rather than dropped, so a narrowing mismatch between the
    two Runs is visible instead of silently swallowing evidence.

    Raises ``ComparisonMismatch`` when the two Runs are not on the same Economy
    and the same Pillars."""
    if run_a.economy != run_b.economy:
        raise ComparisonMismatch(
            f"a Comparison is one Economy seen by two Engines, but Run"
            f" {run_a.run_id} is {run_a.economy} and Run {run_b.run_id} is"
            f" {run_b.economy}"
        )
    pillars_a, pillars_b = sorted(run_a.pillars), sorted(run_b.pillars)
    if pillars_a != pillars_b:
        raise ComparisonMismatch(
            f"a Comparison is one Pillar set seen by two Engines, but Run"
            f" {run_a.run_id} covers {pillars_a} and Run {run_b.run_id} covers"
            f" {pillars_b}"
        )

    names = _indicator_names(indicators)
    grouped_a, grouped_b = _by_indicator(mappings_a), _by_indicator(mappings_b)
    extra = sorted((set(grouped_a) | set(grouped_b)) - set(names))
    for indicator_id in extra:
        names[indicator_id] = ""

    # A chunk claimed by several Indicators is less specific, and the composite
    # says so. Counted per Run, which is per Document too: a chunk belongs to
    # exactly one Document.
    per_chunk_a = Counter(r.chunk_id for r in mappings_a)
    per_chunk_b = Counter(r.chunk_id for r in mappings_b)

    rows: list[ComparisonRow] = []
    counts: Counter[str] = Counter()
    for indicator_id, indicator_name in names.items():
        best_a = controlling_mapping(grouped_a.get(indicator_id, []))
        best_b = controlling_mapping(grouped_b.get(indicator_id, []))
        mark = agreement_of(best_a, best_b)
        counts[mark] += 1
        fallback = next(
            (r.indicator_name for r in (best_a, best_b) if r is not None), ""
        )
        rows.append(
            ComparisonRow(
                indicator_id=indicator_id,
                indicator_name=indicator_name or fallback or indicator_id,
                agreement=mark,
                a=_side(
                    best_a, document_titles, gate_cosines, per_chunk_a, reviews_a,
                    document_sources,
                ),
                b=_side(
                    best_b, document_titles, gate_cosines, per_chunk_b, reviews_b,
                    document_sources,
                ),
            )
        )

    provisions = compare_provisions(
        mappings_a, mappings_b, list(names), document_titles=document_titles
    )
    found_by = Counter(row.found_by for row in provisions)
    models = engine_models or {}

    displays = engine_display_names or {}
    return Comparison(
        economy=run_a.economy,
        pillars=pillars_a,
        run_a=ComparisonRun(
            record=run_a,
            engine_display_name=displays.get(run_a.engine or "", run_a.engine),
            duration_s=duration_seconds(run_a),
        ),
        run_b=ComparisonRun(
            record=run_b,
            engine_display_name=displays.get(run_b.engine or "", run_b.engine),
            duration_s=duration_seconds(run_b),
        ),
        n_indicators=len(rows),
        n_agree=counts["agree"],
        n_disagree=counts["disagree"],
        n_only_a=counts["only_a"],
        n_only_b=counts["only_b"],
        n_neither=counts["neither"],
        note=note,
        rows=rows,
        pass_a=engine_summary(
            run_a, economy_records, provider_model=models.get(run_a.engine or "", ""),
            discoveries=hour_discoveries(run_a, run_b, economy_records)
            if economy_records else None,
        ),
        pass_b=engine_summary(
            run_b, economy_records, provider_model=models.get(run_b.engine or "", ""),
            second_pass=True,
        ),
        n_provisions=len(provisions),
        n_found_by_a_only=found_by["Engine A only"],
        n_found_by_b_only=found_by["Engine B only"],
        n_found_by_both=found_by["Both"],
        provisions=provisions,
        framework_note=framework_note(
            names, (("Engine A", mappings_a), ("Engine B", mappings_b)), document_titles,
            framework_check,
        ),
    )


# ---------------------------------------------------------------------------
# the provision view: the organizers' Engine Comparison sheet
# ---------------------------------------------------------------------------


def provision_number(section: str | None) -> str:
    """The provision a section label names, as the match sees it: the section
    or article number ("s. 26(1)", "Section 26" and "Art. 26" all give "26";
    "第二十六条" gives its numeral), or the whole label normalised when it
    carries no number ("Schedule", "Preamble")."""
    text = section or ""
    for pattern in (_SECTION_NUMBER, _CN_ARTICLE, _LEADING_NUMBER):
        m = pattern.search(text)
        if m:
            return m.group(1).casefold()
    return normalise(text)


def citation_key(record: MappingRecord) -> str:
    """A citation as the Citation differs? column compares it: the provision
    number plus every bracketed subdivision of the exported Article / Section,
    spaces removed and case folded. So "Section 26(1)" and "s. 26 (1)" are one
    citation, and "s. 26(1)" and "s. 26" are two. A label with no number is
    compared on its whole text."""
    cited = article_section(record)
    number = provision_number(record.section)
    if number == normalise(record.section):
        return _WHITESPACE.sub("", cited).casefold()
    start = max(cited.casefold().find(number), 0)
    tail = "".join(_SUBDIVISION.findall(cited[start:]))
    return _WHITESPACE.sub("", f"{number}{tail}").casefold()


def quoted_words(text: str | None) -> tuple[str, ...]:
    """A quote as the Quoted words differ? column compares it: its words in
    order, case folded, with spacing and punctuation left out."""
    return tuple(_WORD.findall((text or "").casefold()))


def _pair_items(
    items_a: list[MappingRecord], items_b: list[MappingRecord]
) -> tuple[list[tuple[MappingRecord, MappingRecord]], list[MappingRecord], list[MappingRecord]]:
    """Pair two sides' Mappings of ONE provision, most-alike first, so the
    same answer on both sides is never split across two rows by a less alike
    pairing claiming it first. Deterministic: both lists arrive sorted."""
    stages = (
        lambda a, b: (a.indicator_id == b.indicator_id
                      and citation_key(a) == citation_key(b)
                      and quoted_words(a.verbatim_quote) == quoted_words(b.verbatim_quote)),
        lambda a, b: a.indicator_id == b.indicator_id and citation_key(a) == citation_key(b),
        lambda a, b: a.indicator_id == b.indicator_id,
        lambda a, b: citation_key(a) == citation_key(b),
        lambda a, b: True,
    )
    left, right = list(items_a), list(items_b)
    pairs: list[tuple[MappingRecord, MappingRecord]] = []
    for alike in stages:
        for a in list(left):
            match = next((b for b in right if alike(a, b)), None)
            if match is not None:
                pairs.append((a, match))
                left.remove(a)
                right.remove(match)
    return pairs, left, right


def _quote_difference(a: MappingRecord, b: MappingRecord) -> str:
    words_a, words_b = quoted_words(a.verbatim_quote), quoted_words(b.verbatim_quote)
    if words_a == words_b:
        return "same quoted words"
    joined_a, joined_b = " ".join(words_a), " ".join(words_b)
    if joined_b and joined_b in joined_a:
        return "Engine A quoted more of the same text"
    if joined_a and joined_a in joined_b:
        return "Engine B quoted more of the same text"
    shared = sum((Counter(words_a) & Counter(words_b)).values())
    longest = max(len(words_a), len(words_b)) or 1
    return (
        f"different quoted words ({round(100 * shared / longest)}% of words shared,"
        f" A {len(words_a)} words, B {len(words_b)} words)"
    )


def _both_difference(a: MappingRecord, b: MappingRecord) -> str:
    cite_a, cite_b = article_section(a), article_section(b)
    parts: list[str] = []
    if citation_key(a) != citation_key(b):
        line = f"Engine A cited {cite_a}, Engine B cited {cite_b}"
        key_a, key_b = citation_key(a), citation_key(b)
        if key_a.startswith(key_b):
            line += ", A is the more precise citation"
        elif key_b.startswith(key_a):
            line += ", B is the more precise citation"
        parts.append(line)
    if a.indicator_id != b.indicator_id:
        parts.append(f"Engine A mapped it to {a.indicator_id}, Engine B to {b.indicator_id}")
    quotes = _quote_difference(a, b)
    if not parts and quotes == "same quoted words":
        return "Same provision, Indicator and quoted words on both Engines"
    parts.append(quotes)
    return "; ".join(parts)


def _only_difference(
    record: MappingRecord,
    this_side: str,
    other_side: str,
    other_choice: MappingRecord | None,
    titles: Mapping[str, str],
) -> str:
    line = f"Only Engine {this_side} cited this provision"
    if other_choice is None:
        return f"{line}; Engine {other_side} found no evidence for {record.indicator_id}"
    where = article_section(other_choice)
    if other_choice.document_id != record.document_id:
        title = titles.get(other_choice.document_id) or other_choice.document_id
        where = f"{where} of {title}"
    return f"{line}; for {record.indicator_id} Engine {other_side} cited {where}"


def _indicator_order(indicator_ids: Sequence[str]) -> dict[str, int]:
    return {indicator_id: i for i, indicator_id in enumerate(indicator_ids)}


def compare_provisions(
    mappings_a: Sequence[MappingRecord],
    mappings_b: Sequence[MappingRecord],
    indicator_ids: Sequence[str],
    *,
    document_titles: Mapping[str, str] | None = None,
) -> list[ProvisionRow]:
    """Every evidence provision either Run produced, paired across the two
    Engines by provision identity (``PROVISION_MATCH_BASIS``), one row per
    pairing and one per Mapping left unpaired. Only the listed Indicators are
    read; a Mapping that fails ``is_evidence`` is not a provision cited.

    Two Mappings of one side with the same citation, Indicator and quoted
    words are one provision cited once (a Run can quote one passage from two
    overlapping chunks); anything else a side cited is a row of its own."""
    titles = document_titles or {}
    wanted = set(indicator_ids)
    order = _indicator_order(indicator_ids)

    def usable(records: Sequence[MappingRecord]) -> list[MappingRecord]:
        seen: set[tuple] = set()
        out: list[MappingRecord] = []
        ranked = sorted(
            (r for r in records if is_evidence(r) and r.indicator_id in wanted),
            key=lambda r: (not r.controlling_evidence, r.mapping_id),
        )
        for r in ranked:
            key = (r.document_id, citation_key(r), r.indicator_id, quoted_words(r.verbatim_quote))
            if key not in seen:
                seen.add(key)
                out.append(r)
        return out

    def grouped(records: list[MappingRecord]) -> dict[tuple[str, str], list[MappingRecord]]:
        out: dict[tuple[str, str], list[MappingRecord]] = {}
        for r in records:
            out.setdefault((r.document_id, provision_number(r.section)), []).append(r)
        for items in out.values():
            items.sort(key=lambda r: (order.get(r.indicator_id, len(order)),
                                      citation_key(r), r.mapping_id))
        return out

    evidence_a, evidence_b = usable(mappings_a), usable(mappings_b)
    by_provision_a, by_provision_b = grouped(evidence_a), grouped(evidence_b)
    choice_a = {i: controlling_mapping(rs) for i, rs in _by_indicator(evidence_a).items()}
    choice_b = {i: controlling_mapping(rs) for i, rs in _by_indicator(evidence_b).items()}

    rows: list[ProvisionRow] = []
    for key in set(by_provision_a) | set(by_provision_b):
        document_id = key[0]
        law_name = titles.get(document_id) or document_id
        pairs, only_a, only_b = _pair_items(
            by_provision_a.get(key, []), by_provision_b.get(key, [])
        )
        for a, b in pairs:
            same_citation = citation_key(a) == citation_key(b)
            same_section = normalise(a.section) == normalise(b.section)
            if same_citation:
                shown = article_section(a)
            elif same_section:
                # The provision both cited, in the Economy's drafting word.
                shown = drafting_label(a.section, a.economy)
            else:
                shown = f"{article_section(a)} / {article_section(b)}"
            same_indicator = a.indicator_id == b.indicator_id
            rows.append(ProvisionRow(
                law_name=law_name,
                article_section=shown,
                indicator_id=(a.indicator_id if same_indicator
                              else f"{a.indicator_id} / {b.indicator_id}"),
                found_by="Both",
                indicator_differs="No" if same_indicator else "Yes",
                citation_differs="No" if same_citation else "Yes",
                quoted_words_differ=(
                    "No" if quoted_words(a.verbatim_quote) == quoted_words(b.verbatim_quote)
                    else "Yes"
                ),
                difference=_both_difference(a, b),
                document_id=document_id,
                a_mapping_id=a.mapping_id, b_mapping_id=b.mapping_id,
                a_indicator_id=a.indicator_id, b_indicator_id=b.indicator_id,
                a_article_section=article_section(a), b_article_section=article_section(b),
                a_quote=a.verbatim_quote, b_quote=b.verbatim_quote,
            ))
        for record, side, other, choices in (
            *((r, "A", "B", choice_b) for r in only_a),
            *((r, "B", "A", choice_a) for r in only_b),
        ):
            is_a = side == "A"
            rows.append(ProvisionRow(
                law_name=law_name,
                article_section=article_section(record),
                indicator_id=record.indicator_id,
                found_by="Engine A only" if is_a else "Engine B only",
                indicator_differs="n/a",
                citation_differs="n/a",
                quoted_words_differ="n/a",
                difference=_only_difference(
                    record, side, other, choices.get(record.indicator_id), titles
                ),
                document_id=document_id,
                a_mapping_id=record.mapping_id if is_a else None,
                b_mapping_id=None if is_a else record.mapping_id,
                a_indicator_id=record.indicator_id if is_a else None,
                b_indicator_id=None if is_a else record.indicator_id,
                a_article_section=article_section(record) if is_a else None,
                b_article_section=None if is_a else article_section(record),
                a_quote=record.verbatim_quote if is_a else None,
                b_quote=None if is_a else record.verbatim_quote,
            ))

    def first_indicator(row: ProvisionRow) -> str:
        return row.a_indicator_id or row.b_indicator_id or ""

    def section_sort(row: ProvisionRow) -> tuple:
        number = provision_number(row.a_article_section or row.b_article_section)
        m = re.match(r"(\d+)(.*)", number)
        return (0, int(m.group(1)), m.group(2)) if m else (1, 0, number)

    rows.sort(key=lambda r: (
        order.get(first_indicator(r), len(order)), first_indicator(r),
        r.law_name, section_sort(r), r.article_section, r.found_by,
    ))
    return rows


def _parse_utc(value: str | None):
    from datetime import datetime

    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def clock_time(value: str | None, utc_offset_hours: int = DISPLAY_UTC_OFFSET_HOURS) -> str:
    """A Run Record timestamp as the sheet's hh:mm, at the given UTC offset."""
    from datetime import timedelta, timezone

    moment = _parse_utc(value)
    if moment is None:
        return ""
    return moment.astimezone(timezone(timedelta(hours=utc_offset_hours))).strftime("%H:%M")


def _window_start(run: RunRecord, economy_records: Sequence[RunRecord]):
    """Where this Run's pass begins: after the latest COMPLETED Run on another
    Engine, on the same Economy, that ended by the time this Run started. A
    stopped, failed or interrupted Run never closes a window, and neither does
    an earlier Run on the same Engine (a restart is the same pass)."""
    start = _parse_utc(run.started_at)
    boundary = None
    for other in economy_records:
        if (
            other.kind != "run" or other.run_id == run.run_id
            or other.economy != run.economy or other.status != "completed"
            or other.engine == run.engine
        ):
            continue
        ended = _parse_utc(other.ended_at)
        if ended is not None and start is not None and ended <= start and (
            boundary is None or ended > boundary
        ):
            boundary = ended
    return boundary


def _discoveries_between(economy: str, economy_records, begin, end) -> list[RunRecord]:
    found = []
    for record in economy_records:
        if record.kind != "discovery" or record.economy != economy:
            continue
        began, ended = _parse_utc(record.started_at), _parse_utc(record.ended_at)
        if began is None or ended is None or ended > end:
            continue
        if begin is not None and began < begin:
            continue
        found.append(record)
    return sorted(found, key=lambda r: r.started_at)


def pass_discoveries(
    run: RunRecord, economy_records: Sequence[RunRecord]
) -> list[RunRecord]:
    """The Discoveries that belong to this Run's pass: on the same Economy,
    ended by the time this Run started, and started after the pass window
    opened (_window_start). A Discovery that never ended is not attributed,
    because no one can say which pass it served."""
    start = _parse_utc(run.started_at)
    if start is None:
        return []
    return _discoveries_between(
        run.economy, economy_records, _window_start(run, economy_records), start
    )


def hour_discoveries(
    first: RunRecord, second: RunRecord | None, economy_records: Sequence[RunRecord]
) -> list[RunRecord]:
    """Every Discovery and add of the hour, all of it the first pass's: Runs
    never fetch, so whatever was fetched from the moment the first pass's
    window opened until the second Run started (or the first Run ended, when
    that is later) was fetched for the first pass. That includes an add made
    between the two Runs, and a Discovery made while both were running."""
    ends = [
        t for t in (
            _parse_utc(first.ended_at),
            _parse_utc(second.started_at) if second is not None else None,
            _parse_utc(first.started_at),
        ) if t is not None
    ]
    if not ends:
        return []
    return _discoveries_between(
        first.economy, economy_records, _window_start(first, economy_records), max(ends)
    )


def framework_note(
    indicator_ids: Iterable[str],
    sides: Sequence[tuple[str, Sequence[MappingRecord]]],
    document_titles: Mapping[str, str] | None,
    check: Callable[[MappingRecord], bool] | None = None,
) -> str | None:
    """One sentence per side and framework Indicator (7.1, 7.2) where that
    side has no Mapping on a law that can carry the row, which is exactly when
    its evidence file carries no row for it. None when every side has one.

    ``check`` is the export's own rule (pipeline.framework_checker); without
    it the Document titles are matched against the law families alone."""
    titles = document_titles or {}
    if check is None:
        def check(m: MappingRecord) -> bool:
            return framework_family_match(m.indicator_id, titles.get(m.document_id, m.document_id))

    parts = []
    for indicator_id in FRAMEWORK_INDICATORS:
        if indicator_id not in indicator_ids:
            continue
        family = FRAMEWORK_FAMILY_NAMES[indicator_id]
        for label, mappings in sides:
            if not any(
                m.indicator_id == indicator_id and is_evidence(m) and check(m)
                for m in mappings
            ):
                parts.append(
                    f"{label} has no {indicator_id} Mapping on a {family} law"
                    f" or a law the 2025 baseline cites for {indicator_id},"
                    f" so its evidence file has no {indicator_id} row"
                )
    return "; ".join(parts) + "." if parts else None


def engine_model_names(models) -> dict[str, str]:
    """Engine key -> "provider / model" as the organizers' sheets ask for it:
    the model id without the routing prefix the provider already names."""
    names = {}
    for name, engine in models.engines.items():
        model_id = engine.litellm_model
        prefix = f"{engine.provider}/"
        if model_id.startswith(prefix):
            model_id = model_id[len(prefix):]
        names[name] = f"{engine.provider} / {model_id}"
    return names


def engine_summary(
    run: RunRecord,
    economy_records: Sequence[RunRecord] = (),
    *,
    provider_model: str = "",
    utc_offset_hours: int = DISPLAY_UTC_OFFSET_HOURS,
    discoveries: Sequence[RunRecord] | None = None,
    second_pass: bool = False,
) -> EngineSummary:
    """Block 1 of the organizers' sheet for one pass, from the Run Records
    alone: the pass runs from its first Discovery's start (or the Run's) to
    the latest end among them; documents fetched and cost add the Run and its
    Discoveries. Cost is the provider's reported figure when it sent one.

    ``discoveries`` names the pass's Discoveries (hour_discoveries for the
    first pass); omitted, they are pass_discoveries. A ``second_pass`` has
    none: it re-reads what the first pass fetched, so its count is its Run's
    own, which is 0."""
    from datetime import timedelta, timezone

    if second_pass:
        discoveries = []
    elif discoveries is None:
        discoveries = pass_discoveries(run, economy_records)
    records = sorted([*discoveries, run], key=lambda r: r.started_at)
    started_at = records[0].started_at
    begin = _parse_utc(started_at)
    ends = [t for t in (_parse_utc(r.ended_at) for r in records) if t is not None]
    end = max(ends) if _parse_utc(run.ended_at) is not None and ends else None
    elapsed = None
    if begin is not None and end is not None:
        elapsed = round((end - begin).total_seconds() / 60, 1)
    use_provider = run.provider_cost_usd is not None
    cost = sum(
        (r.provider_cost_usd if use_provider and r.provider_cost_usd is not None
         else r.cost_usd)
        for r in records
    )
    return EngineSummary(
        engine=run.engine,
        provider_model=provider_model or (run.engine or ""),
        run_id=run.run_id,
        discovery_run_ids=[r.run_id for r in discoveries],
        started_at=started_at,
        ended_at=run.ended_at,
        start_hhmm=clock_time(started_at, utc_offset_hours),
        end_hhmm=(
            end.astimezone(timezone(timedelta(hours=utc_offset_hours))).strftime("%H:%M")
            if end is not None else ""
        ),
        elapsed_minutes=elapsed,
        documents_fetched=sum(r.documents_fetched for r in records),
        cost_usd=round(cost, 6),
        cost_basis="provider" if use_provider else "metered",
    )


# ---------------------------------------------------------------------------
# the hand-in file
# ---------------------------------------------------------------------------

#: The Run Record facts that head the CSV, as (row label, RunRecord attribute).
#: duration_s is derived, so it is handled beside them.
_HEADER_FIELDS = (
    ("run_id", "run_id"),
    ("economy", "economy"),
    ("engine_key", "engine"),
    ("status", "status"),
    ("started_at", "started_at"),
    ("ended_at", "ended_at"),
    ("documents_fetched", "documents_fetched"),
    ("prompt_tokens", "prompt_tokens"),
    ("completion_tokens", "completion_tokens"),
    ("cost_usd", "cost_usd"),
    ("provider_cost_usd", "provider_cost_usd"),
)

_SIDE_COLUMNS = (
    "mapping_id", "document_id", "document_title", "section", "subsection",
    "page_number", "confidence", "review_status", "verbatim_quote",
)

#: One row per Indicator, side A's columns then side B's.
COMPARISON_COLUMNS = (
    "indicator_id", "indicator_name", "agreement",
    *(f"a_{c}" for c in _SIDE_COLUMNS),
    *(f"b_{c}" for c in _SIDE_COLUMNS),
)


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def comparison_csv(comparison: Comparison) -> str:
    """The Comparison as one CSV: a leading block of the two Run Records, a
    blank row, then the table the screen shows, one row per Indicator.

    A steward opens this beside the evidence workbook, so it is a plain sheet
    with no comment syntax anywhere: the header block is ordinary rows whose
    first cell is the field name."""
    import csv as _csv

    buffer = io.StringIO(newline="")
    writer = _csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(["field", "run_a", "run_b"])
    writer.writerow([
        "engine",
        _cell(comparison.run_a.engine_display_name),
        _cell(comparison.run_b.engine_display_name),
    ])
    for label, attribute in _HEADER_FIELDS:
        writer.writerow([
            label,
            _cell(getattr(comparison.run_a.record, attribute)),
            _cell(getattr(comparison.run_b.record, attribute)),
        ])
    writer.writerow([
        "duration_s",
        _cell(comparison.run_a.duration_s),
        _cell(comparison.run_b.duration_s),
    ])
    pillars = " ".join(str(p) for p in comparison.pillars)
    writer.writerow(["pillars", pillars, pillars])
    writer.writerow(["match_basis", comparison.match_basis, ""])
    if comparison.note:
        writer.writerow(["note", comparison.note, ""])
    if comparison.framework_note:
        writer.writerow(["framework_note", comparison.framework_note, ""])
    writer.writerow([
        "agreement_counts",
        f"agree {comparison.n_agree}, disagree {comparison.n_disagree},"
        f" only_a {comparison.n_only_a}, only_b {comparison.n_only_b},"
        f" neither {comparison.n_neither}",
        "",
    ])
    writer.writerow([])

    writer.writerow(list(COMPARISON_COLUMNS))
    for row in comparison.rows:
        cells = [row.indicator_id, row.indicator_name, row.agreement]
        for side in (row.a, row.b):
            for column in _SIDE_COLUMNS:
                cells.append("" if side is None else _cell(getattr(side, column)))
        writer.writerow(cells)
    return buffer.getvalue()


def comparison_filename(comparison: Comparison, ext: str = "csv") -> str:
    """``comparison_<economy>_p<pillars>_<engineA>_vs_<engineB>.<ext>``, safe to
    put straight into a Content-Disposition header."""

    def safe(value: str | None, fallback: str) -> str:
        cleaned = _UNSAFE_IN_FILENAME.sub("-", (value or "").strip())
        return cleaned.strip("-") or fallback

    pillars = "-".join(str(p) for p in comparison.pillars) or "none"
    return (
        f"comparison_{safe(comparison.economy, 'economy')}_p{pillars}"
        f"_{safe(comparison.run_a.record.engine, comparison.run_a.record.run_id)}"
        f"_vs_{safe(comparison.run_b.record.engine, comparison.run_b.record.run_id)}"
        f".{safe(ext, 'csv')}"
    )


def engine_sheet_csv(comparison: Comparison) -> str:
    """The organizers' Engine Comparison sheet as one CSV, in the sheet's own
    words and order: block 1 (field, Engine A, Engine B), a blank row, block 2
    (every provision, numbered from 1), a blank row, the three counters, then
    the match basis and the Run ids so the file can be traced back. Block 3 is
    the team's own paragraph and is not in the file."""
    import csv as _csv

    from regcompass.workbook import (
        ENGINE_PROVISION_HEADERS,
        ENGINE_SUMMARY_FIELDS,
        ENGINE_SUMMARY_HEADERS,
    )

    def values(summary: EngineSummary | None) -> list[str]:
        if summary is None:
            return [""] * len(ENGINE_SUMMARY_FIELDS)
        return [
            summary.provider_model,
            summary.start_hhmm,
            summary.end_hhmm,
            _cell(summary.elapsed_minutes),
            _cell(summary.documents_fetched),
            _cell(summary.cost_usd),
        ]

    buffer = io.StringIO(newline="")
    writer = _csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(list(ENGINE_SUMMARY_HEADERS))
    for label, a, b in zip(
        ENGINE_SUMMARY_FIELDS, values(comparison.pass_a), values(comparison.pass_b)
    ):
        writer.writerow([label, a, b])
    writer.writerow([])
    writer.writerow(list(ENGINE_PROVISION_HEADERS))
    for number, row in enumerate(comparison.provisions, start=1):
        writer.writerow([
            number, row.law_name, row.article_section, row.indicator_id,
            row.found_by, row.indicator_differs, row.citation_differs,
            row.quoted_words_differ, row.difference,
        ])
    writer.writerow([])
    writer.writerow(["Found by Engine A only:", comparison.n_found_by_a_only])
    writer.writerow(["Found by Engine B only:", comparison.n_found_by_b_only])
    writer.writerow(["Found by both:", comparison.n_found_by_both])
    writer.writerow([])
    writer.writerow(["match_basis", comparison.provision_match_basis])
    for label, summary in (("run_a", comparison.pass_a), ("run_b", comparison.pass_b)):
        if summary is not None:
            trail = [summary.run_id, *summary.discovery_run_ids]
            writer.writerow([
                label, " ".join(trail), summary.started_at, summary.ended_at or "",
                f"cost basis {summary.cost_basis}",
            ])
    writer.writerow([
        "times",
        f"hh:mm at UTC+{DISPLAY_UTC_OFFSET_HOURS} (Bangkok); Run Records keep UTC",
    ])
    return buffer.getvalue()
