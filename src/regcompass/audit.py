"""U0 audit backend: the models and pure logic behind the human review UI.

The audit UI is a thin reader over pipeline truth. Every highlight rectangle
is derived from the canonical stream's WordBox coordinates (pdfplumber points
for born-digital pages, Tesseract boxes converted to points for OCR pages;
both are top-left-origin PDF points, so they map onto a PDF.js viewport by a
single scale factor). The quote is located by the same byte-for-byte
discipline as M7: exact substring, anchored to its chunk's character range.
Nothing here re-emits text; rects_for_span only reads offsets.

Review gate: only review_status == "accepted" records enter the
final RDTII export. The gate itself lives in export.export_all (the reviews
parameter); this module supplies the review records and the API surface.
"""

from __future__ import annotations

import gzip
import json
import re
from collections import Counter
from pathlib import Path
from typing import Literal
from urllib.parse import quote as urlquote

from pydantic import BaseModel, ConfigDict, Field

from .contracts import (
    CanonicalText,
    Chunk,
    EconomyCode,
    GlossRecord,
    MappingRecord,
    Review,
    ReviewStatus,
    WordBox,
)
from .export import REVIEW_CONFIDENCE_THRESHOLD, confidence_score, location_reference
from .extract import format_for_extractor
from .paths import resolve_stored_path

QUOTE_PREVIEW_CHARS = 160
# Two words share a line when their vertical overlap is at least half the
# shorter word's height (legal PDFs have superscripts and footnote markers
# that would otherwise split every line into several rects).
LINE_OVERLAP_FRACTION = 0.5


# ---------------------------------------------------------------------------
# API models (the U0 backend I/O contract)
# ---------------------------------------------------------------------------


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HighlightRect(_Model):
    """One merged line-level rectangle, in PDF points, top-left origin."""

    page: int = Field(ge=1)
    x0: float
    y0: float
    x1: float
    y1: float


class DocumentSummary(_Model):
    document_id: str
    title: str
    economy: EconomyCode
    n_pages: int = Field(ge=0)
    ocr_applied: bool
    # How the Document entered the Corpus: 'discovery' or 'manual'. A frozen
    # bundle predates "Add document" entirely, so its Documents keep the
    # default, which is the truth about them.
    source_kind: str = "discovery"
    n_records: int = Field(ge=0)
    n_accepted: int = Field(ge=0)
    n_rejected: int = Field(ge=0)
    n_flagged: int = Field(ge=0)


class SourceLink(_Model):
    """Where "Open source" takes a reviewer for one Mapping row.

    kind says WHAT is at the other end: 'official' is the Document's own Portal
    address, 'local' is the copy this app is serving, which is all a Document
    with no http(s) Source URL of its own can honestly offer. The interface
    labels the two differently for exactly that reason."""

    href: str
    kind: Literal["official", "local"]
    # The PDF page the link opens at, when the Location Reference named one.
    page: int | None = None


#: The page inside the Evidence Export's Location Reference (export writes
#: "PDF: page 12" for a PDF and "HTML: <section path>" for an HTML page). Read,
#: never re-invented: export.location_reference owns the format and this is the
#: only reader of it.
_PAGE_IN_LOCATION = re.compile(r"page\s+(\d+)", re.IGNORECASE)
#: A section anchor carried in a Location Reference, for an HTML Document whose
#: Portal page addresses provisions by id. Nothing writes one today, so this is
#: a reader that finds nothing rather than a format we invented.
_ANCHOR_IN_LOCATION = re.compile(r"#([^\s#]+)")


def build_source_link(
    *,
    document_id: str,
    source_url: str | None,
    format_tag: str,
    location_reference: str | None,
    run_id: str | None = None,
) -> SourceLink:
    """THE rule for following one Mapping row to its source, in one place.

    An http(s) Source URL is the official one and opens as it stands, with the
    cited place appended when we know it and the URL does not already carry a
    fragment: `#page=N` for a PDF Document (ignored harmlessly by a Portal that
    answers with HTML), or the section anchor the Location Reference names for
    an HTML one. Anything else (no Source URL at all, or a local marker such as
    a file: path) is not a Portal address, so the link falls back to this app's
    own stored-file endpoint for that Document and says so with kind='local'."""
    page: int | None = None
    if format_tag == "pdf":
        found = _PAGE_IN_LOCATION.search(location_reference or "")
        if found:
            page = int(found.group(1))

    url = (source_url or "").strip()
    if url.lower().startswith(("http://", "https://")):
        href = url
        if "#" not in href:
            if page is not None:
                href = f"{href}#page={page}"
            elif format_tag == "html":
                anchor = _ANCHOR_IN_LOCATION.search(location_reference or "")
                if anchor:
                    href = f"{href}#{anchor.group(1)}"
        return SourceLink(href=href, kind="official", page=page)

    # The same endpoint the PDF pane reads, so the local copy a reviewer opens
    # in a tab is byte-for-byte the file the highlight was drawn on.
    href = f"/api/documents/{urlquote(document_id, safe='')}/pdf"
    if run_id:
        href = f"{href}?run_id={urlquote(run_id, safe='')}"
    if page is not None:
        href = f"{href}#page={page}"
    return SourceLink(href=href, kind="local", page=page)


#: What the stored bytes ARE, by the file's own extension. The local-copy link
#: opens them in a browser tab, and a tab shown the wrong type renders a broken
#: document rather than the evidence.
_MEDIA_BY_SUFFIX = {
    ".pdf": "application/pdf",
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ),
    ".txt": "text/plain; charset=utf-8",
    ".xml": "application/xml",
}
_MEDIA_BY_FORMAT = {"pdf": "application/pdf", "html": "text/html; charset=utf-8"}


def media_type_for_stored_file(path: Path | str, extractor: str | None = None) -> str:
    """The media type to serve one Document's stored bytes as.

    The file's own extension answers first, because it is what the fetch or the
    upload actually saved. An unfamiliar extension falls back to the lane that
    read the file (the extractor ingest recorded). Neither known means we do
    not claim a type at all: octet-stream is the honest answer, and a browser
    offers to save the file rather than mis-rendering it."""
    suffix = Path(path).suffix.lower()
    if suffix in _MEDIA_BY_SUFFIX:
        return _MEDIA_BY_SUFFIX[suffix]
    if extractor:
        return _MEDIA_BY_FORMAT[format_for_extractor(extractor)]
    return "application/octet-stream"


#: What the audit view's left pane can do with a Document's stored file. Only a
#: PDF can be drawn as pages; anything else (a web page, another upload, a file
#: no longer on disk) is shown as its text instead.
SourceFormat = Literal["pdf", "html", "other", "missing"]


def source_format_for(path: Path | None, extractor: str | None = None) -> SourceFormat:
    """Read off the same media type the stored-file endpoint serves, so the
    pane and the endpoint never disagree about what the bytes are."""
    if path is None or not Path(path).is_file():
        return "missing"
    media = media_type_for_stored_file(path, extractor)
    if media == "application/pdf":
        return "pdf"
    if media.startswith("text/html"):
        return "html"
    return "other"


class RecordSummary(_Model):
    mapping_id: str
    indicator_id: str
    indicator_name: str
    section: str
    subsection: str | None
    page_number: int | None
    quote_preview: str
    confidence: float | None
    controlling_evidence: bool
    review_status: ReviewStatus | None
    review_note: str | None = None
    # Where this row came from, as the Evidence Export states it: the
    # Document's Source URL and the row's Location Reference, plus the link
    # the interface's "Open source" control follows.
    source_url: str | None = None
    location_reference: str = ""
    source_link: SourceLink | None = None
    # The lane that read the Document. An HTML quote is stored as page 1 of a
    # page that has no pages, so a screen reads this before calling it one.
    format: Literal["pdf", "html"] = "pdf"


class QueueRecord(RecordSummary):
    """One row of the Review queue. A queue spans every Document the Run
    mapped, so the row has to name the Document it came from: the Documents
    table has that in its own row, and the queue has no such parent."""

    document_id: str
    document_title: str


class ReviewQueue(_Model):
    """The Run's Mappings as one ordered list, with the counts the screen
    states above it ("14 of 60 below 0.60, 9 unreviewed").

    The three counts describe the WHOLE Run, never the filtered view, so the
    line reads the same with the filter on and off. records is what the filter
    and the order left."""

    run_id: str | None
    threshold: float
    total: int
    below_threshold: int
    unreviewed: int
    records: list[QueueRecord]


class RecordDetail(_Model):
    record: MappingRecord
    section_label: str | None
    highlights: list[HighlightRect]
    highlight_available: bool
    quote_char_start: int | None
    quote_char_end: int | None
    review: Review | None
    # The English Gloss of this Verbatim Quote, where the Run drafted one. The
    # quote itself is never replaced: the pane shows the original first and the
    # Gloss beside it, with its label until a named person approves the text.
    gloss: GlossRecord | None = None
    # What the stored file is, and, when it is not a PDF, the Piece's text for
    # the pane to show in place of a page.
    source_format: SourceFormat = "pdf"
    source_text: str | None = None


class GlossReviewRequest(_Model):
    """A Gloss approval as the interface posts it: the text the reviewer
    approved and the name they approved it under. reviewed_by is typed as
    optional so a blank name comes back as the 400 that explains itself rather
    than a bare validation error."""

    mapping_id: str
    english: str
    reviewed_by: str | None = None
    run_id: str | None = None


class ReviewRequest(_Model):
    """A Review Decision as the interface posts it. run_id names the Run whose
    Mapping is being judged; it is optional only so a single-Run server can
    leave it to the audit view's own scope, which is the same Run."""

    mapping_id: str
    review_status: ReviewStatus
    run_id: str | None = None
    reviewer: str | None = None
    comment: str | None = None  # the reviewer's note


class AcceptAllRequest(_Model):
    """Accept every verified Mapping of this Run that carries no decision yet.
    Decisions already made are left exactly as they are."""

    run_id: str | None = None


class ExportPreview(_Model):
    """What the Evidence Export would contain right now, counted off the
    database without writing a file. gated is False on the frozen bundle lane,
    which carries no Review Decisions at all."""

    run_id: str | None
    gated: bool
    n_verified: int = Field(ge=0)
    n_accepted: int = Field(ge=0)
    n_rejected: int = Field(ge=0)
    n_flagged: int = Field(ge=0)
    n_unreviewed: int = Field(ge=0)
    accepted_mapping_ids: list[str] = Field(default_factory=list)


class ExportFile(_Model):
    """One file an Evidence Export produced, named so a reviewer can download
    it. `name` is the bare file name the download endpoint takes, never a path:
    on the supported container path the output directory lives inside a named
    volume, and a reviewer reading a path there has no way to reach the file."""

    name: str
    # What this file IS, in the reviewer's words rather than its extension.
    label: str
    # The one a reviewer came for. Exactly one file carries this.
    primary: bool = False


class ExportSummary(_Model):
    n_records_total: int = Field(ge=0)
    n_accepted: int = Field(ge=0)
    n_rejected: int = Field(default=0, ge=0)
    n_flagged: int = Field(default=0, ge=0)
    n_unreviewed: int = Field(default=0, ge=0)
    n_rows: int = Field(ge=0)
    csv_path: str
    # The organizers' filled workbook, and the rows their 101-row entry area
    # could not hold. None only when the workbook was not written.
    xlsx_path: str | None = None
    rows_cut: int = Field(default=0, ge=0)
    # Duplicate provision rows folded into one before the battery judged them,
    # and the (Indicator, provision) keys involved, in one sentence.
    rows_collapsed: int = Field(default=0, ge=0)
    duplicate_collapse: str | None = None
    supplementary_path: str
    # Every file this Export wrote, workbook first, as download names. The
    # interface turns these into links; the paths above stay for the CLI and
    # for anyone reading the API directly.
    files: list[ExportFile] = Field(default_factory=list)


class AuditManifestDoc(_Model):
    """One document's file pointers. Paths are resolved relative to the
    manifest's directory unless absolute."""

    document_id: str
    title: str
    economy: EconomyCode
    pdf: str
    canonical: str
    chunks: str
    records: str
    gate: str | None = None  # m5 gated payload (cosines for the confidence composite)
    # The Document's official Portal address, so a bundle row can be followed
    # to the source and not only to the copy in the bundle. Absent on a
    # manifest written before this field existed, which reads as "not recorded".
    source_url: str | None = None


class AuditManifest(_Model):
    documents: list[AuditManifestDoc]
    # economy -> {law, sections, pairs_gated}; feeds build_absence_rows
    coverage_stats: dict[str, dict] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# pure logic: quote location + highlight rectangles
# ---------------------------------------------------------------------------


def locate_quote(
    full_text: str,
    quote: str,
    chunk_start: int | None = None,
    chunk_end: int | None = None,
) -> tuple[int, int] | None:
    """Absolute (char_start, char_end) of the verbatim quote in the canonical
    stream. The chunk range anchors the search (a quote can recur elsewhere in
    a long act); the whole stream is the degraded-input fallback. None only
    for text that is not byte-for-byte in the stream, which a verified record
    can never be."""
    if not quote:
        return None
    if chunk_start is not None and chunk_end is not None:
        idx = full_text.find(quote, chunk_start, chunk_end)
        if idx != -1:
            return idx, idx + len(quote)
    idx = full_text.find(quote)
    if idx == -1:
        return None
    return idx, idx + len(quote)


def _same_line(a: WordBox, b: WordBox) -> bool:
    overlap = min(a.y1, b.y1) - max(a.y0, b.y0)
    shorter = min(a.y1 - a.y0, b.y1 - b.y0)
    return overlap >= LINE_OVERLAP_FRACTION * shorter


def rects_for_span(words: list[WordBox], char_start: int, char_end: int) -> list[HighlightRect]:
    """Merged line-level rectangles for every word overlapping the character
    span. Empty when the stream carries no word boxes for the span (degraded
    lane: the UI shows the record without a highlight)."""
    hit = [w for w in words if w.char_end > char_start and w.char_start < char_end]
    if not hit:
        return []
    hit.sort(key=lambda w: (w.page, w.y0, w.x0))
    rects: list[HighlightRect] = []
    line: list[WordBox] = []
    for w in hit:
        if line and (w.page != line[0].page or not _same_line(line[-1], w)):
            rects.append(_merge_line(line))
            line = []
        line.append(w)
    rects.append(_merge_line(line))
    return rects


def _merge_line(line: list[WordBox]) -> HighlightRect:
    return HighlightRect(
        page=line[0].page,
        x0=min(w.x0 for w in line),
        y0=min(w.y0 for w in line),
        x1=max(w.x1 for w in line),
        y1=max(w.y1 for w in line),
    )


def detail_for(
    record: MappingRecord,
    *,
    full_text: str,
    words_fn,
    chunk_start: int | None,
    chunk_end: int | None,
    section_label: str | None,
    review: Review | None,
    gloss: GlossRecord | None = None,
    source_format: SourceFormat = "pdf",
) -> RecordDetail:
    """One record with its highlight geometry. THE resolver: the frozen bundle
    and the working database both come through here, so there is one quote
    search and one set of rectangles in the codebase rather than a copy per data
    source that can drift apart.

    words_fn(char_start, char_end) hands back the WordBoxes overlapping the
    located quote. A bundle has the whole word list in memory and ignores the
    span; the database turns it into a range scan, which is the difference
    between reading a handful of rows and 180,000 of them."""
    span = locate_quote(full_text, record.verbatim_quote, chunk_start, chunk_end)
    highlights: list[HighlightRect] = []
    if span is not None:
        highlights = rects_for_span(words_fn(*span), *span)
    source_text = None
    if source_format != "pdf":
        if chunk_start is not None and chunk_end is not None:
            source_text = full_text[chunk_start:chunk_end]
        elif span is not None:
            source_text = full_text[max(0, span[0] - 800) : span[1] + 800]
        else:
            source_text = record.verbatim_quote
    return RecordDetail(
        record=record,
        section_label=section_label,
        highlights=highlights,
        highlight_available=bool(highlights),
        quote_char_start=span[0] if span else None,
        quote_char_end=span[1] if span else None,
        review=review,
        gloss=gloss,
        source_format=source_format,
        source_text=source_text,
    )


# ---------------------------------------------------------------------------
# bundle loading
# ---------------------------------------------------------------------------


def _read_json(path: Path):
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    return json.loads(path.read_text(encoding="utf-8"))


def _load_records(payload) -> list[MappingRecord]:
    if isinstance(payload, dict):
        payload = payload["records"]  # m8 reconciled shape
    return [MappingRecord.model_validate(r) for r in payload]


class AuditDocument:
    """One document's loaded truth: canonical stream, chunks, verified records."""

    def __init__(self, entry: AuditManifestDoc, base: Path):
        self.entry = entry
        self.pdf_path = _resolve(entry.pdf, base)
        self.canonical: CanonicalText = CanonicalText.model_validate(
            _read_json(_resolve(entry.canonical, base))
        )
        chunks_payload = _read_json(_resolve(entry.chunks, base))
        if isinstance(chunks_payload, dict):
            chunks_payload = chunks_payload["chunks"]
        self.chunks: dict[str, Chunk] = {
            c["chunk_id"]: Chunk.model_validate(c) for c in chunks_payload
        }
        # The records file may hold a whole economy (repro m8 checkpoints are
        # per-economy); keep only this document's verified rows.
        self.records: list[MappingRecord] = [
            r
            for r in _load_records(_read_json(_resolve(entry.records, base)))
            if r.verification_status == "passed" and r.document_id == entry.document_id
        ]
        self.gate_cosines: dict[tuple[str, str], float] = {}
        if entry.gate is not None:
            gated = _read_json(_resolve(entry.gate, base))
            self.gate_cosines = {
                (r["chunk"]["chunk_id"], r["indicator_id"]): float(r["cosine_pillar"])
                for r in gated["passed"]
            }

    def detail(self, record: MappingRecord, review: Review | None) -> RecordDetail:
        chunk = self.chunks.get(record.chunk_id)
        return detail_for(
            record,
            full_text=self.canonical.full_text,
            words_fn=lambda _s, _e: self.canonical.words,
            chunk_start=chunk.char_start if chunk else None,
            chunk_end=chunk.char_end if chunk else None,
            section_label=chunk.section_label if chunk else None,
            review=review,
            source_format=source_format_for(
                self.pdf_path if self.pdf_path.exists() else None,
                self.canonical.extractor,
            ),
        )


def _resolve(path_str: str, base: Path) -> Path:
    p = Path(path_str)
    return p if p.is_absolute() else base / p


class AuditBundle:
    """The audit server's whole data source: a manifest of per-document
    pipeline outputs (canonical stream, chunks, verified records, optional
    gate cosines) plus the coverage stats the gated export needs."""

    def __init__(self, manifest_path: Path):
        self.manifest_path = Path(manifest_path)
        self.manifest = AuditManifest.model_validate(_read_json(self.manifest_path))
        base = self.manifest_path.parent
        self.docs: dict[str, AuditDocument] = {}
        self._record_index: dict[str, tuple[str, MappingRecord]] = {}
        for entry in self.manifest.documents:
            doc = AuditDocument(entry, base)
            self.docs[entry.document_id] = doc
            for rec in doc.records:
                self._record_index[rec.mapping_id] = (entry.document_id, rec)

    # -- listing ------------------------------------------------------------

    def document_summaries(self, reviews: dict[str, Review]) -> list[DocumentSummary]:
        out = []
        for entry in self.manifest.documents:
            doc = self.docs[entry.document_id]
            statuses = [
                reviews[r.mapping_id].review_status
                for r in doc.records
                if r.mapping_id in reviews
            ]
            out.append(
                DocumentSummary(
                    document_id=entry.document_id,
                    title=entry.title,
                    economy=entry.economy,
                    n_pages=len(doc.canonical.pages),
                    ocr_applied=doc.canonical.ocr_applied,
                    n_records=len(doc.records),
                    n_accepted=statuses.count("accepted"),
                    n_rejected=statuses.count("rejected"),
                    n_flagged=statuses.count("flagged"),
                )
            )
        return out

    def record_summaries(self, document_id: str, reviews: dict[str, Review]) -> list[RecordSummary]:
        doc = self.docs[document_id]
        per_chunk = Counter(r.chunk_id for r in doc.records)
        # The bundle records the format on the canonical stream it shipped
        # (the extractor that produced it), and the Source URL only when the
        # manifest builder put one there.
        source_url = doc.entry.source_url
        format_tag = format_for_extractor(doc.canonical.extractor)
        out = []
        for r in sorted(doc.records, key=lambda r: (r.indicator_id, r.mapping_id)):
            review = reviews.get(r.mapping_id)
            confidence = r.confidence
            if confidence is None and (r.chunk_id, r.indicator_id) in doc.gate_cosines:
                # The record carries None until M9 assigns the composite; the
                # audit view computes the SAME mechanical formula from the
                # bundle's gate cosines (never LLM-reported).
                confidence = confidence_score(
                    doc.gate_cosines[(r.chunk_id, r.indicator_id)],
                    len(r.verbatim_quote),
                    per_chunk[r.chunk_id],
                    r.extraction_attempts,
                )
            out.append(
                RecordSummary(
                    mapping_id=r.mapping_id,
                    indicator_id=r.indicator_id,
                    indicator_name=r.indicator_name,
                    section=r.section,
                    subsection=r.subsection,
                    page_number=r.page_number,
                    quote_preview=r.verbatim_quote[:QUOTE_PREVIEW_CHARS],
                    confidence=confidence,
                    controlling_evidence=r.controlling_evidence,
                    review_status=review.review_status if review else None,
                    review_note=review.comment if review else None,
                    source_url=source_url,
                    format=format_tag,
                    location_reference=location_reference(r, format_tag),
                    source_link=build_source_link(
                        document_id=document_id,
                        source_url=source_url,
                        format_tag=format_tag,
                        location_reference=location_reference(r, format_tag),
                    ),
                )
            )
        return out

    def has_record(self, mapping_id: str) -> bool:
        return mapping_id in self._record_index

    def has_document(self, document_id: str) -> bool:
        return document_id in self.docs

    def pdf_path(self, document_id: str) -> Path | None:
        """The source PDF on disk, or None when this document has none. Both
        audit sources answer this, so the server's PDF endpoint never asks
        which lane it is serving."""
        doc = self.docs.get(document_id)
        if doc is None or not doc.pdf_path.exists():
            return None
        return doc.pdf_path

    def media_type(self, document_id: str) -> str:
        """What pdf_path's file is, for the header that serves it. Both audit
        sources answer this too, so the endpoint stays lane-agnostic."""
        doc = self.docs.get(document_id)
        if doc is None:
            return "application/octet-stream"
        return media_type_for_stored_file(doc.pdf_path, doc.canonical.extractor)

    def record_detail(self, mapping_id: str, reviews: dict[str, Review]) -> RecordDetail | None:
        hit = self._record_index.get(mapping_id)
        if hit is None:
            return None
        document_id, record = hit
        return self.docs[document_id].detail(record, reviews.get(mapping_id))

    # -- export inputs --------------------------------------------------------

    def all_records(self) -> list[MappingRecord]:
        return [rec for doc in self.docs.values() for rec in doc.records]

    def chunk_text_lookup(self) -> dict[str, str]:
        return {
            chunk_id: chunk.text
            for doc in self.docs.values()
            for chunk_id, chunk in doc.chunks.items()
        }

    def gate_cosine_lookup(self) -> dict[tuple[str, str], float]:
        out: dict[tuple[str, str], float] = {}
        for doc in self.docs.values():
            out.update(doc.gate_cosines)
        return out


class DatabaseAuditSource:
    """One Run's Mappings out of the WORKING database, with the same highlight
    geometry the frozen bundle lane produces.

    The bundle is a snapshot of a pipeline that already finished; this is what a
    reviewer sees for a Run they just started from the interface. Both answer
    the same question and both go through detail_for, so a record opened from
    either side resolves its quote to the same page and the same rectangles.

    run_id=None means every record in the database, which is what a pre-Run
    database (rows migrated as 'legacy') honestly holds.

    data_dir is the root a Corpus Document's stored bytes hang off. A row
    records its path relative to that root, and rows written before that was
    enforced hold an absolute one, so pdf_path accepts either spelling rather
    than making the caller guess which lane wrote the row."""

    def __init__(self, storage, run_id: str | None = None, data_dir: Path | str | None = None):
        self.storage = storage
        self.run_id = run_id
        self.data_dir = Path(data_dir) if data_dir is not None else None
        self._records_cache: list[MappingRecord] | None = None
        self._gate_cache: dict[tuple[str, str], float] | None = None

    # -- the Run's verified records -----------------------------------------

    def records(self) -> list[MappingRecord]:
        """Every verified Mapping of this Run. Only passed records: the audit
        view reviews evidence that survived M7, and a dropped row is not
        evidence. Loaded once per instance, and an instance is per request."""
        if self._records_cache is None:
            self._records_cache = self.storage.load_mappings(
                run_id=self.run_id, verification_status="passed"
            )
        return self._records_cache

    def _gate_cosines(self) -> dict[tuple[str, str], float]:
        if self._gate_cache is None:
            self._gate_cache = self.storage.load_gate_scores()
        return self._gate_cache

    def _record(self, mapping_id: str) -> MappingRecord | None:
        for rec in self.records():
            if rec.mapping_id == mapping_id:
                return rec
        return None

    def has_record(self, mapping_id: str) -> bool:
        return self._record(mapping_id) is not None

    def has_document(self, document_id: str) -> bool:
        return any(r.document_id == document_id for r in self.records())

    # -- listing -------------------------------------------------------------

    def document_summaries(self, reviews: dict[str, Review]) -> list[DocumentSummary]:
        """One row per Document this Run actually produced evidence for. A
        Document the Run never mapped is not listed: the audit view answers
        "what did this Run find", not "what is in the database"."""
        by_doc: dict[str, list[MappingRecord]] = {}
        for rec in self.records():
            by_doc.setdefault(rec.document_id, []).append(rec)
        meta = self.storage.document_meta(list(by_doc))
        out: list[DocumentSummary] = []
        for document_id in sorted(by_doc):
            recs = by_doc[document_id]
            row = meta.get(document_id, {})
            statuses = [
                reviews[r.mapping_id].review_status
                for r in recs
                if r.mapping_id in reviews
            ]
            out.append(
                DocumentSummary(
                    document_id=document_id,
                    title=row.get("title") or document_id,
                    economy=recs[0].economy,
                    n_pages=self._n_pages(document_id),
                    ocr_applied=bool(row.get("ocr_applied")),
                    source_kind=row.get("source_kind") or "discovery",
                    n_records=len(recs),
                    n_accepted=statuses.count("accepted"),
                    n_rejected=statuses.count("rejected"),
                    n_flagged=statuses.count("flagged"),
                )
            )
        return out

    def _n_pages(self, document_id: str) -> int:
        row = self.storage.conn.execute(
            "SELECT n_pages FROM documents WHERE document_id = ?", (document_id,)
        ).fetchone()
        return int(row["n_pages"] or 0) if row is not None else 0

    def record_summaries(
        self, document_id: str, reviews: dict[str, Review]
    ) -> list[RecordSummary]:
        recs = [r for r in self.records() if r.document_id == document_id]
        per_chunk = Counter(r.chunk_id for r in recs)
        cosines = self._gate_cosines()
        source_url, format_tag = self._source_of(document_id)
        out: list[RecordSummary] = []
        for r in sorted(recs, key=lambda r: (r.indicator_id, r.mapping_id)):
            review = reviews.get(r.mapping_id)
            confidence = r.confidence
            if confidence is None and (r.chunk_id, r.indicator_id) in cosines:
                # Same mechanical composite the bundle lane computes, from the
                # gate cosines this Run persisted. Never a model's own number.
                confidence = confidence_score(
                    cosines[(r.chunk_id, r.indicator_id)],
                    len(r.verbatim_quote),
                    per_chunk[r.chunk_id],
                    r.extraction_attempts,
                )
            out.append(
                RecordSummary(
                    mapping_id=r.mapping_id,
                    indicator_id=r.indicator_id,
                    indicator_name=r.indicator_name,
                    section=r.section,
                    subsection=r.subsection,
                    page_number=r.page_number,
                    quote_preview=r.verbatim_quote[:QUOTE_PREVIEW_CHARS],
                    confidence=confidence,
                    controlling_evidence=bool(r.controlling_evidence),
                    review_status=review.review_status if review else None,
                    review_note=review.comment if review else None,
                    source_url=source_url,
                    format=format_tag,
                    location_reference=location_reference(r, format_tag),
                    source_link=build_source_link(
                        document_id=document_id,
                        source_url=source_url,
                        format_tag=format_tag,
                        location_reference=location_reference(r, format_tag),
                        run_id=self.run_id,
                    ),
                )
            )
        return out

    def _source_of(self, document_id: str) -> tuple[str | None, str]:
        """This Document's Source URL as stored, and which lane extracted it.
        A row that predates either column, or a Document the database does not
        hold, reads as "no Portal address, PDF" - which is what the local
        fallback link is for."""
        row = self.storage.conn.execute(
            "SELECT source_url, extractor FROM documents WHERE document_id = ?",
            (document_id,),
        ).fetchone()
        if row is None:
            return None, "pdf"
        return row["source_url"], format_for_extractor(row["extractor"])

    def pdf_path(self, document_id: str) -> Path | None:
        """This Document's stored bytes. local_path is written relative to the
        data folder, and older rows hold an absolute path or one with a stray
        `data/` prefix, so the shared resolver is asked: it tries the row's own
        spelling first and then the Economy's raw folder, taking a candidate
        only when its bytes hash to the digest on the row."""
        row = self.storage.conn.execute(
            "SELECT local_path, economy, source_sha256 FROM documents"
            " WHERE document_id = ?",
            (document_id,),
        ).fetchone()
        raw = None if row is None else row["local_path"]
        if not raw:
            return None
        path = Path(raw)
        if path.is_file():
            return path
        if self.data_dir is None:
            return None
        found = resolve_stored_path(
            raw, self.data_dir,
            economy=row["economy"], sha256=row["source_sha256"],
        )
        return None if found is None else found.path

    def media_type(self, document_id: str) -> str:
        """What pdf_path's file is, for the header that serves it. An upload is
        not always a PDF: this Corpus takes HTML from a Portal that publishes
        it that way, and the reviewer's own file picker takes whatever they
        pick."""
        path = self.pdf_path(document_id)
        if path is None:
            return "application/octet-stream"
        row = self.storage.conn.execute(
            "SELECT extractor FROM documents WHERE document_id = ?", (document_id,)
        ).fetchone()
        return media_type_for_stored_file(path, None if row is None else row["extractor"])

    def source_format(self, document_id: str) -> SourceFormat:
        """What the audit view's left pane can draw for this Document."""
        row = self.storage.conn.execute(
            "SELECT extractor FROM documents WHERE document_id = ?", (document_id,)
        ).fetchone()
        return source_format_for(
            self.pdf_path(document_id), None if row is None else row["extractor"]
        )

    # -- export inputs --------------------------------------------------------

    def all_records(self) -> list[MappingRecord]:
        return list(self.records())

    def chunk_text_lookup(self) -> dict[str, str]:
        return self.storage.chunk_texts()

    def gate_cosine_lookup(self) -> dict[tuple[str, str], float]:
        return dict(self._gate_cosines())

    def record_detail(self, mapping_id: str, reviews: dict[str, Review]) -> RecordDetail | None:
        record = self._record(mapping_id)
        if record is None:
            return None
        chunk = self.storage.chunk_row(record.chunk_id)
        document_id = record.document_id
        return detail_for(
            record,
            full_text=self.storage.document_full_text(document_id),
            words_fn=lambda start, end: self.storage.words_for(document_id, start, end),
            chunk_start=chunk["char_start"] if chunk is not None else None,
            chunk_end=chunk["char_end"] if chunk is not None else None,
            section_label=chunk["section_label"] if chunk is not None else None,
            review=reviews.get(mapping_id),
            gloss=self.gloss_for(mapping_id),
            source_format=self.source_format(document_id),
        )

    def gloss_for(self, mapping_id: str) -> GlossRecord | None:
        """This Run's Gloss for one Mapping, or None. Looked up BY RUN: a Gloss
        another Run drafted for the same Mapping id is not this one's. When the
        view names no Run (a database from before Runs existed), the Mapping's
        own run_id places the lookup, exactly as a Review Decision's does. A
        database from before the glosses table reads as no Gloss, never as an
        error (Storage.gloss_get)."""
        run_id = self.run_id or self.storage.run_id_of_mapping(mapping_id)
        if not run_id:
            return None
        return self.storage.gloss_get(run_id, mapping_id)


# ---------------------------------------------------------------------------
# the Review queue
# ---------------------------------------------------------------------------


#: The orders the queue offers. "confidence" is the queue proper: the rows the
#: calibration rule says to hand-check come first. "record" is the order the
#: Documents table reads in, kept so the same list can be read either way.
QUEUE_SORTS = ("confidence", "record")


def _queue_order(row: QueueRecord) -> tuple[float, str]:
    """Confidence ascending, ties by Mapping id so the order is stable between
    two reads. A row with no Confidence sorts FIRST: nothing is known about it,
    which is the strongest reason to put a person in front of it."""
    return (-1.0 if row.confidence is None else row.confidence, row.mapping_id)


def build_review_queue(
    source,
    reviews: dict[str, Review],
    *,
    sort: str = "confidence",
    unreviewed_only: bool = False,
    threshold: float = REVIEW_CONFIDENCE_THRESHOLD,
) -> ReviewQueue:
    """Every Mapping of one Run as a single ordered list, with the counts the
    screen states above it.

    Built from the same two readers the Documents table uses, so a queue row
    and a table row are the same row: whatever the audit view can open from one
    it can open from the other. Works on both lanes for that reason, the
    working database and the frozen bundle, without either knowing about it.
    """
    if sort not in QUEUE_SORTS:
        raise ValueError(f"unknown sort {sort!r}")
    rows: list[QueueRecord] = []
    for doc in source.document_summaries(reviews):
        for rec in source.record_summaries(doc.document_id, reviews):
            rows.append(
                QueueRecord(
                    **rec.model_dump(),
                    document_id=doc.document_id,
                    document_title=doc.title,
                )
            )
    # Counted over the whole Run before anything is filtered away: the count
    # line has to say how much work there is, not how much is on screen.
    below = sum(1 for r in rows if r.confidence is None or r.confidence < threshold)
    unreviewed = sum(1 for r in rows if r.review_status is None)
    total = len(rows)
    if sort == "confidence":
        rows.sort(key=_queue_order)
    else:
        rows.sort(key=lambda r: (r.document_id, r.indicator_id, r.mapping_id))
    if unreviewed_only:
        rows = [r for r in rows if r.review_status is None]
    return ReviewQueue(
        run_id=getattr(source, "run_id", None),
        threshold=threshold,
        total=total,
        below_threshold=below,
        unreviewed=unreviewed,
        records=rows,
    )
