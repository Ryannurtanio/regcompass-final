"""M1 - Extract: raw fetched bytes -> CanonicalText.

The output full_text IS the canonical stream: produced exactly once per document,
here and nowhere else. Everything downstream (chunking, mapping, the M7 substring
check) slices this same string by character offsets. Nothing in this module may
re-flow, re-wrap, or clean up text after the stream is built.

"Exactly once" is now literal rather than aspirational. The bottom of this module
is the extraction cache: a stream is stored under everything that decides what it
says (the Document's bytes, the extraction logic, the tesseract language data,
the OCR quality ladder for its script), so the next Run reads it back instead of
running tesseract over the same scanned Act a fourth time. Reproducibility is
the reason the key is that wide: if any part of it differs the Document is read
again, so a reused stream is always the stream this Run would have produced.

Engines:
- pdfplumber (primary, PDF): page text + word boxes for the audit highlight overlay.
- pypdfium2 (alternate, PDF): fast lane for 500+ page statutes and the fallback when
  pdfplumber fails on an odd/encrypted file. Text only, no word boxes.
- bs4/lxml (HTML): extracts from the EXACT fetched bytes; no browser, no DOM
  serialization, no print-to-PDF re-flow.

Low-yield pages (< OCR_TRIGGER_CHARS chars) are FLAGGED for M2, never OCR'd here.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from importlib import metadata
from io import BytesIO
from typing import TYPE_CHECKING, Literal

from regcompass.contracts import CanonicalText, PageSpan, PipelineConfig, WordBox

if TYPE_CHECKING:  # the cache talks to Storage; the module never imports it
    from regcompass.languages import OcrPolicy
    from regcompass.storage import Storage

FormatTag = Literal["pdf", "html"]
PdfEngine = Literal["pdfplumber", "pypdfium2"]

#: The extractor name the HTML lane records on its CanonicalText. It is the one
#: engine here that is not reading a PDF, which is how a stored Document's
#: format is read back later (format_for_extractor).
HTML_EXTRACTOR = "bs4-lxml"

# Per page; below this the page is flagged for M2. Single-sourced from the
# PipelineConfig default so the two copies can never drift.
OCR_TRIGGER_CHARS: int = PipelineConfig.model_fields["ocr_trigger_chars_per_page"].default
PAGE_SEPARATOR = "\n"  # single separator between page texts in the canonical stream

# Space-squash retry lane: some PDF producers (SSO's Arbortext consolidations)
# set inter-word gaps below pdfplumber's default x_tolerance of 3, so words fuse
# ("Atransferormayvoluntarily..."). A stream whose squash fraction exceeds the
# threshold is re-extracted once with the tight tolerance; the retry is kept
# only if it measures strictly cleaner, so well-behaved documents can never be
# altered by this lane. Deterministic: same bytes -> same lane -> same stream.
SQUASH_RUN_RE = re.compile(r"[A-Za-z,;()]{30,}")
SQUASH_FRAC_MAX = 0.02
TIGHT_X_TOLERANCE = 1.5


# Economies whose gazette sets its acts in two columns: the Jornal da República
# (Timor-Leste). Read line by line, such a page interleaves the columns and every
# sentence mixes two articles, so for these Economies a page whose words leave a
# clear gutter is read left column first, then right. Every other Economy's
# stream is untouched. Their stored streams need no key of their own: the OCR
# language string in the extraction key (por+eng) is already Timor-Leste's alone.
TWO_COLUMN_ECONOMIES = frozenset({"TL"})
# A page is two columns when a vertical line in the middle 30 percent of its
# width crosses at most this share of its words (running heads span the page),
# with at least COLUMN_MIN_SIDE of the words on each side of it.
COLUMN_MIN_WORDS = 40
COLUMN_MAX_CROSSING = 0.05
COLUMN_MIN_SIDE = 0.25


def reads_columns(economy: str | None) -> bool:
    """Whether this Economy's PDF pages are read column by column."""
    return (economy or "").upper() in TWO_COLUMN_ECONOMIES


def column_gutter(width: float, words: list[dict]) -> float | None:
    """The x of the gutter between two columns of words, or None when the page
    is not laid out in two columns. Deterministic: the line crossing the fewest
    words wins, the one nearest the middle breaking a tie."""
    n = len(words)
    if n < COLUMN_MIN_WORDS:
        return None
    best: tuple[tuple[int, float], float] | None = None
    for x in range(int(width * 0.35), int(width * 0.65) + 1):
        crossing = sum(1 for w in words if w["x0"] < x < w["x1"])
        key = (crossing, abs(x - width / 2))
        if best is None or key < best[0]:
            best = (key, float(x))
    assert best is not None
    (crossing, _), x = best
    left = sum(1 for w in words if w["x1"] <= x)
    right = sum(1 for w in words if w["x0"] >= x)
    if crossing > max(3, COLUMN_MAX_CROSSING * n):
        return None
    if left < COLUMN_MIN_SIDE * n or right < COLUMN_MIN_SIDE * n:
        return None
    return x


def squash_fraction(text: str) -> float:
    """Fraction of characters sitting in >= 30-char runs with no space: the
    mechanical detector for fused inter-word gaps."""
    if not text:
        return 0.0
    return sum(len(m) for m in SQUASH_RUN_RE.findall(text)) / len(text)


@dataclass
class ExtractionStats:
    """Alignment bookkeeping (not part of the contract): how many word boxes
    could be anchored into the page text by forward search."""

    words_total: int = 0
    words_aligned: int = 0
    squash_retry: bool = False  # tight-tolerance lane fired and won

    @property
    def alignment_rate(self) -> float:
        return self.words_aligned / self.words_total if self.words_total else 1.0


class ExtractionError(Exception):
    pass


def format_for_extractor(extractor: str | None) -> FormatTag:
    """Which lane produced a Document already in the Corpus, read back off the
    extractor name ingest recorded on it. sniff_format answers the same
    question from raw bytes; this answers it from the stored row, which is all
    the audit view has to hand."""
    return "html" if (extractor or "").strip() == HTML_EXTRACTOR else "pdf"


def sniff_format(raw: bytes) -> FormatTag:
    """PDF files must carry %PDF- in the first 1024 bytes (per spec some tools
    prepend junk); anything else we treat as HTML."""
    return "pdf" if b"%PDF-" in raw[:1024] else "html"


def extract(
    raw: bytes,
    format_tag: FormatTag,
    document_id: str,
    engine: PdfEngine = "pdfplumber",
) -> CanonicalText:
    canonical, _ = extract_with_stats(raw, format_tag, document_id, engine)
    return canonical


def extract_with_stats(
    raw: bytes,
    format_tag: FormatTag,
    document_id: str,
    engine: PdfEngine = "pdfplumber",
    *,
    columns: bool = False,
) -> tuple[CanonicalText, ExtractionStats]:
    """columns=True reads a two-column page column by column (reads_columns);
    the alternate lane has no word positions and reads as before."""
    sniffed = sniff_format(raw)
    if sniffed != format_tag:
        raise ExtractionError(f"format tag '{format_tag}' does not match sniffed content '{sniffed}'")
    if format_tag == "html":
        return _extract_html(raw, document_id)
    if engine == "pdfplumber":
        try:
            return _extract_pdfplumber(raw, document_id, columns=columns)
        except ExtractionError:
            raise
        except Exception:
            # Odd/encrypted PDF that pdfplumber chokes on: alternate lane.
            return _extract_pypdfium2(raw, document_id)
    return _extract_pypdfium2(raw, document_id)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _low_yield(page_texts: list[str]) -> list[int]:
    return [i + 1 for i, t in enumerate(page_texts) if len(t.strip()) < OCR_TRIGGER_CHARS]


def _build_spans(page_texts: list[str]) -> tuple[str, list[PageSpan]]:
    """Join page texts with PAGE_SEPARATOR and record each page's char range.
    Slicing full_text by a page's range returns that page's text exactly."""
    spans: list[PageSpan] = []
    cursor = 0
    parts: list[str] = []
    for i, text in enumerate(page_texts):
        if i > 0:
            cursor += len(PAGE_SEPARATOR)
        spans.append(PageSpan(page_number=i + 1, char_start=cursor, char_end=cursor + len(text)))
        parts.append(text)
        cursor += len(text)
    return PAGE_SEPARATOR.join(parts), spans


def _extract_pdfplumber(
    raw: bytes, document_id: str, *, columns: bool = False
) -> tuple[CanonicalText, ExtractionStats]:
    import pdfplumber

    stats = ExtractionStats()

    def _pull(tolerance: dict) -> tuple[list[str], list[list[dict]]]:
        texts: list[str] = []
        words: list[list[dict]] = []
        with pdfplumber.open(BytesIO(raw)) as pdf:
            for page in pdf.pages:
                page_words = page.extract_words(**tolerance)
                gutter = column_gutter(page.width, page_words) if columns else None
                if gutter is None:
                    texts.append(page.extract_text(**tolerance) or "")
                    words.append(page_words)
                    continue
                # Each character goes to the column its centre sits in, so a
                # running head across the gutter is split, never lost.
                sides = [
                    page.filter(lambda o, g=gutter, left=left: o.get("object_type") != "char"
                                or ((o["x0"] + o["x1"]) / 2 < g) == left)
                    for left in (True, False)
                ]
                side_texts = [side.extract_text(**tolerance) or "" for side in sides]
                texts.append("\n".join(t for t in side_texts if t))
                words.append([w for side in sides for w in side.extract_words(**tolerance)])
        return texts, words

    page_texts, page_words = _pull({})
    frac = squash_fraction("".join(page_texts))
    if frac > SQUASH_FRAC_MAX:
        tight_texts, tight_words = _pull({"x_tolerance": TIGHT_X_TOLERANCE})
        if squash_fraction("".join(tight_texts)) < frac:
            page_texts, page_words = tight_texts, tight_words
            stats.squash_retry = True

    full_text, spans = _build_spans(page_texts)

    # Anchor each word into its page text by forward search. extract_text and
    # extract_words share the same char-grouping rules and tolerances, so words
    # appear in page text in emission order; a moving cursor keeps the search
    # linear and prevents matching an earlier duplicate.
    words: list[WordBox] = []
    for span, text, raw_words in zip(spans, page_texts, page_words):
        cursor = 0
        for w in raw_words:
            stats.words_total += 1
            idx = text.find(w["text"], cursor)
            if idx == -1:
                continue  # unanchorable (ligature/space edge case): skipped, counted
            stats.words_aligned += 1
            words.append(
                WordBox(
                    text=w["text"],
                    page=span.page_number,
                    x0=w["x0"],
                    y0=w["top"],
                    x1=w["x1"],
                    y1=w["bottom"],
                    char_start=span.char_start + idx,
                    char_end=span.char_start + idx + len(w["text"]),
                )
            )
            cursor = idx + len(w["text"])

    canonical = CanonicalText(
        document_id=document_id,
        source_sha256=_sha256(raw),
        extractor="pdfplumber",
        extractor_version=metadata.version("pdfplumber"),
        full_text=full_text,
        pages=spans,
        words=words,
        low_yield_pages=_low_yield(page_texts),
    )
    return canonical, stats


def _extract_pypdfium2(raw: bytes, document_id: str) -> tuple[CanonicalText, ExtractionStats]:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(raw)
    try:
        page_texts: list[str] = []
        for page in pdf:
            textpage = page.get_textpage()
            page_texts.append(textpage.get_text_bounded() or "")
            textpage.close()
            page.close()
    finally:
        pdf.close()

    full_text, spans = _build_spans(page_texts)
    canonical = CanonicalText(
        document_id=document_id,
        source_sha256=_sha256(raw),
        extractor="pypdfium2",
        extractor_version=metadata.version("pypdfium2"),
        full_text=full_text,
        pages=spans,
        words=[],  # word boxes come from the primary engine or the OCR TSV lane
        low_yield_pages=_low_yield(page_texts),
    )
    return canonical, ExtractionStats()


def _extract_html(raw: bytes, document_id: str) -> tuple[CanonicalText, ExtractionStats]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(raw, "lxml")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    text = soup.get_text("\n")
    # Deterministic whitespace shaping AT STREAM CREATION (this is the one place
    # allowed to shape text, because the result IS the canonical stream): strip
    # trailing space per line, collapse runs of blank lines to one.
    lines = [ln.rstrip() for ln in text.split("\n")]
    shaped: list[str] = []
    for ln in lines:
        if ln == "" and shaped and shaped[-1] == "":
            continue
        shaped.append(ln)
    while shaped and shaped[0] == "":
        shaped.pop(0)
    while shaped and shaped[-1] == "":
        shaped.pop()
    full_text = "\n".join(shaped)

    canonical = CanonicalText(
        document_id=document_id,
        source_sha256=_sha256(raw),
        extractor=HTML_EXTRACTOR,
        extractor_version=f"{metadata.version('beautifulsoup4')}+{metadata.version('lxml')}",
        full_text=full_text,
        pages=[PageSpan(page_number=1, char_start=0, char_end=len(full_text))],
        words=[],
        low_yield_pages=[1] if len(full_text.strip()) < OCR_TRIGGER_CHARS else [],
    )
    return canonical, ExtractionStats()


# ---------------------------------------------------------------------------
# The extraction cache: read a Document once, reuse the stream
# ---------------------------------------------------------------------------

# Bump this when the extraction or OCR LOGIC changes in a way that changes the
# stream: a new whitespace rule, a different page separator, a changed OCR
# ladder. Every stored stream produced by the old logic then stops matching and
# is read again rather than served stale. The installed versions of the
# libraries that actually produce the text ride along in the key beside it
# (extraction_stack_version), so a pdfplumber or lxml upgrade has the same
# effect without anyone having to remember this constant.
EXTRACTOR_VERSION = "1"

# The libraries whose output IS the stream. tesseract is deliberately absent:
# asking it for its version means running the binary, and the OCR lane is
# reached only for a Document whose text layer is unusable.
_EXTRACTION_LIBRARIES = ("pdfplumber", "pypdfium2", "beautifulsoup4", "lxml")


def extraction_stack_version() -> str:
    """The logic version plus the installed versions of the extraction
    libraries, as one string. Read through the module global every time so a
    test can bump the constant and watch the key move."""
    parts = [EXTRACTOR_VERSION]
    for name in _EXTRACTION_LIBRARIES:
        try:
            parts.append(f"{name}{metadata.version(name)}")
        except Exception:  # noqa: BLE001 - an absent library is a fact, not a failure
            parts.append(f"{name}-absent")
    return "+".join(parts)


def ocr_policy_id(
    policy: "OcrPolicy | None", config: PipelineConfig | None = None
) -> str:
    """One stable string for how a Document would be OCR'd: the quality ladder
    its script gets, and the three configured knobs that decide the rest.

    The ladder's three answers travel together (regcompass.languages.ocr_policy)
    and each of them can change what is stored: the dictionary proxy and the
    RapidOCR escalation change the text, and the forced-manual-review reason
    changes the quality block recorded beside it. The config knobs change the
    text just as directly: the DPI decides what tesseract is shown, and the two
    escalation floors decide whether RapidOCR reads the page instead. All of
    them belong in the key, because changing any of them means the stored
    stream is no longer the stream a Run would produce.

    None for either argument is the default, spelled out rather than left blank
    so the column reads the same either way."""
    cfg = config or PipelineConfig()
    ladder = (
        "dict=1,rapidocr=1,manual="
        if policy is None
        else (
            f"dict={int(policy.dictionary_proxy)},"
            f"rapidocr={int(policy.rapidocr_escalation)},"
            f"manual={policy.manual_review_reason or ''}"
        )
    )
    return (
        f"{ladder},dpi={cfg.ocr_dpi},"
        f"min_conf={cfg.ocr_escalation_min_confidence},"
        f"min_dict_hit={cfg.ocr_escalation_min_dict_hit}"
    )


@dataclass(frozen=True)
class ExtractionKey:
    """Everything that decides what a Document's canonical stream says.

    `digest` is the primary key of the stored stream. The four parts are kept
    beside it because they are what a person reading the table needs: a row
    that was not reused is explained by whichever part moved."""

    source_sha256: str
    extractor_version: str
    ocr_languages: str
    ocr_policy_id: str

    @property
    def digest(self) -> str:
        joined = "\x1f".join(
            (
                self.source_sha256,
                self.extractor_version,
                self.ocr_languages,
                self.ocr_policy_id,
            )
        )
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def extraction_key(
    source_sha256: str,
    *,
    ocr_languages: str,
    policy: "OcrPolicy | None" = None,
    config: PipelineConfig | None = None,
) -> ExtractionKey:
    """The key these exact bytes, read this exact way, are stored under.

    config is the PipelineConfig the reading lane would hand the OCR engine, so
    the key moves when an operator changes the rasterization DPI or either
    escalation floor in config/pipeline.yaml."""
    return ExtractionKey(
        source_sha256=source_sha256,
        extractor_version=extraction_stack_version(),
        ocr_languages=ocr_languages,
        ocr_policy_id=ocr_policy_id(policy, config),
    )


def load_extraction(
    storage: "Storage", key: ExtractionKey, document_id: str
) -> CanonicalText | None:
    """The stored stream for this key, under the asking Document's id, or None.

    None is the answer to every kind of absence: no row, no table (a database
    written before the cache existed), or a stored payload this build can no longer
    read. All three mean the same thing to the caller, which is that it must
    read the Document itself, and none of them may take a Run down."""
    payload = storage.extraction_get(key.digest)
    if payload is None:
        return None
    try:
        canonical = CanonicalText.model_validate_json(payload)
    except Exception:  # noqa: BLE001 - an unreadable payload is a cache miss
        return None
    if canonical.document_id != document_id:
        # The same bytes can enter two Corpora under two ids. The stream is a
        # property of the bytes; the id is a property of the Corpus row.
        canonical = canonical.model_copy(update={"document_id": document_id})
    return canonical


def extraction_provenance(
    canonical: CanonicalText, key: ExtractionKey
) -> dict[str, str] | None:
    """Which OCR engine read this stream and which vendored traineddata files
    it loaded, or None for a stream no OCR touched.

    Derived from the stream and its key rather than read back from the row, so
    a reused Run and a fresh one report the same two facts by the same route.
    This is provenance, never identity: neither value is in the key."""
    if not canonical.ocr_applied:
        return None
    from regcompass.ocr import tessdata_fingerprint

    return {
        "ocr_engine_version": canonical.extractor_version,
        "tessdata_sha256": tessdata_fingerprint(key.ocr_languages),
    }


def ocr_quality_columns(canonical: CanonicalText) -> dict[str, object]:
    """The five OCR proxy columns of a documents row, for a stream OCR read.

    Empty for a born-digital stream, which is why the columns are null there
    and null is the truth. For an OCR'd one they are the verdict the ladder
    already computed, and every lane that writes a Corpus row must write them:
    the ingest wrote the extractor string alone, so a scan that escalated to
    RapidOCR came in reading `tesseract+rapidocr` beside a row that recorded no
    escalation and no confidence at all.
    """
    q = canonical.ocr_quality
    if q is None:
        return {}
    return {
        "mean_word_confidence": q.mean_word_confidence,
        "dictionary_hit_rate": q.dictionary_hit_rate,
        "ocr_quality_cer": q.ocr_quality_cer,
        "cer_proxy_flag": int(q.cer_proxy_flag),
        "escalated_to_rapidocr": int(q.escalated_to_rapidocr),
        "manual_review": int(q.manual_review),
    }


def store_extraction(
    storage: "Storage",
    key: ExtractionKey,
    canonical: CanonicalText,
    *,
    source_format: str,
) -> None:
    """Keep this stream for the next Run. Byte-for-byte what the extractor
    produced: the payload is the contract model's own JSON, so what comes back
    is what went in, page spans and word boxes included.

    A stream OCR read carries its provenance on the row: the tesseract that
    read it and the sha256 of every vendored traineddata file it loaded. Both
    are recorded, neither is keyed (see ocr.tessdata_fingerprint)."""
    provenance = extraction_provenance(canonical, key) or {}
    storage.extraction_store(
        key.digest,
        canonical.model_dump_json(),
        source_sha256=key.source_sha256,
        extractor_version=key.extractor_version,
        ocr_languages=key.ocr_languages,
        ocr_policy_id=key.ocr_policy_id,
        source_format=source_format,
        extractor=f"{canonical.extractor}-{canonical.extractor_version}",
        ocr_applied=canonical.ocr_applied,
        n_chars=len(canonical.full_text),
        n_pages=len(canonical.pages),
        n_low_yield_pages=len(canonical.low_yield_pages),
        ocr_engine_version=provenance.get("ocr_engine_version"),
        tessdata_sha256=provenance.get("tessdata_sha256"),
    )
