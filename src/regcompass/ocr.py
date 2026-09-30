"""M2 - OCR: scanned documents -> CanonicalText (the OCR stream becomes canonical
for those documents).

Ladder: Tesseract (primary, vendored tessdata_best) -> RapidOCR (escalation when
quality proxies fail) -> manual_review flag (never silently passed). The page text
is BUILT from the same TSV/line results that provide the word boxes, so the
word-slice invariant holds by construction, exactly as in M1.

Rasterization: pypdfium2 at the pinned DPI (PipelineConfig.ocr_dpi). Box
coordinates are converted from render pixels to PDF points (72/dpi) so the U0
highlight overlay treats M1 and M2 words identically.

Evidence pairs: every OCR-processed page saves (rasterized input PNG, extracted
text) so judges can compute error rates themselves.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from regcompass.contracts import CanonicalText, OcrQuality, PageSpan, PipelineConfig, WordBox
from regcompass.languages import OcrPolicy

VENDOR_TESSDATA = Path(__file__).resolve().parents[2] / "vendor" / "tessdata"
WORDLIST_PATH = Path(__file__).resolve().parents[2] / "vendor" / "wordlist_legal_en.txt"

# Cache: a traineddata file is 8 to 15 MB and never changes under a running
# process, so each one is hashed at most once.
_TESSDATA_SHA: dict[str, str] = {}


def tessdata_fingerprint(languages: str) -> str:
    """The vendored traineddata behind one tesseract `-l` string, as a stable
    JSON map of language code to the sha256 of the file that was loaded.

    This is PROVENANCE, not identity: it says which language models read a
    scanned Act, so a stored stream can be traced to the exact files that
    produced it. It is deliberately not part of the extraction key - a shipped
    database must keep serving its streams inside an image whose vendor
    directory is a copy rather than the same bytes on the same disk. A code
    with no vendored file is recorded as 'missing', which is the truth about a
    Language that fell back to English."""
    out: dict[str, str] = {}
    for code in sorted(set(languages.split("+"))):
        if code in _TESSDATA_SHA:
            out[code] = _TESSDATA_SHA[code]
            continue
        path = VENDOR_TESSDATA / f"{code}.traineddata"
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            digest = "missing"
        _TESSDATA_SHA[code] = digest
        out[code] = digest
    return json.dumps(out, sort_keys=True)

_WORD_RE = re.compile(r"[a-z]{3,}")

# Why a Document's dictionary hit rate is None rather than 0.0. It is saved in
# the OCR evidence and shown beside the proxies a reviewer reads.
DICT_PROXY_SKIPPED_NOTE = (
    "dictionary hit rate not applicable: the wordlist is English and this"
    " Document was read as {languages}; the escalation and manual-review"
    " decisions rest on tesseract word confidence alone"
)

# The all-English ladder: every proxy applies and nothing is forced. It is what
# a caller that says nothing about the Language gets, so existing behaviour on
# the English Economies is unchanged.
DEFAULT_POLICY = OcrPolicy(
    dictionary_proxy=True, rapidocr_escalation=True, manual_review_reason=None
)


def ocr_quality_for(
    mean_word_confidence: float,
    dictionary_hit: float | None,
    config: PipelineConfig,
    policy: OcrPolicy,
    languages: str,
    escalated: bool,
) -> OcrQuality:
    """The quality verdict for one OCR'd Document, as a pure decision over the
    measured proxies. Split out of ocr_document so the verdict can be tested
    without a scan, a binary and a minute of rasterization.

    A Language with no vendored traineddata was read as English whatever it
    actually is, so its verdict is manual_review with the reason recorded: a
    confident-looking mean over misread glyphs is not evidence of anything."""
    manual_review = mean_word_confidence < config.ocr_manual_min_confidence or (
        dictionary_hit is not None and dictionary_hit < config.ocr_manual_min_dict_hit
    )
    if policy.manual_review_reason is not None:
        manual_review = True
    return OcrQuality(
        mean_word_confidence=max(0.0, min(1.0, mean_word_confidence)),
        dictionary_hit_rate=dictionary_hit,
        dictionary_hit_rate_note=(
            None if policy.dictionary_proxy else DICT_PROXY_SKIPPED_NOTE.format(languages=languages)
        ),
        ocr_quality_cer=None,  # measured CER is filled by eval runs with a hand-checked reference
        cer_proxy_flag=escalated or manual_review,
        escalated_to_rapidocr=escalated,
        manual_review=manual_review,
        manual_review_reason=policy.manual_review_reason,
    )


@dataclass
class _PageResult:
    text: str  # page text built from the engine's own tokens
    words: list[tuple[str, float, float, float, float, int]]  # text, x0, y0, x1, y1, local_char_start
    confidences: list[float]  # 0..1 per word/line


def _load_wordlist() -> frozenset[str]:
    return frozenset(WORDLIST_PATH.read_text(encoding="utf-8").split())


def dictionary_hit_rate(text: str, wordlist: frozenset[str]) -> float:
    tokens = _WORD_RE.findall(text.lower())
    if not tokens:
        return 0.0
    return sum(t in wordlist for t in tokens) / len(tokens)


def cer_against_reference(reference: str, hypothesis: str) -> float:
    """Character error rate of the best alignment of `reference` anywhere inside
    `hypothesis` (semi-global edit distance: free start/end in the hypothesis).
    Whitespace runs are collapsed in both, since line-wrap position is not an
    OCR error. Returns errors / len(reference)."""
    ref = " ".join(reference.split())
    hyp = " ".join(hypothesis.split())
    if not ref:
        raise ValueError("empty reference")
    # DP over hypothesis (columns, free start/end) vs reference (rows)
    prev = [0] * (len(hyp) + 1)  # free leading gap in hypothesis
    for i, rc in enumerate(ref, start=1):
        curr = [i] + [0] * len(hyp)
        for j, hc in enumerate(hyp, start=1):
            curr[j] = min(
                prev[j] + 1,  # delete from reference
                curr[j - 1] + 1,  # insert from hypothesis
                prev[j - 1] + (rc != hc),  # substitute / match
            )
        prev = curr
    return min(prev) / len(ref)


def _tesseract_page(img, languages: str) -> _PageResult:
    """Run Tesseract, build page text from the TSV word grid (grouped by block/
    paragraph/line, words joined by single spaces, lines by newlines)."""
    import pytesseract

    data = pytesseract.image_to_data(
        img,
        lang=languages,
        config=f'--tessdata-dir "{VENDOR_TESSDATA}"',
        output_type=pytesseract.Output.DICT,
    )
    lines: dict[tuple[int, int, int], list[int]] = {}
    for i, token in enumerate(data["text"]):
        if not token.strip() or data["conf"][i] == -1:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append(i)

    text_parts: list[str] = []
    words: list[tuple[str, float, float, float, float, int]] = []
    confidences: list[float] = []
    cursor = 0
    for key in sorted(lines):
        if text_parts:
            cursor += 1  # the "\n" joining lines
        line_tokens: list[str] = []
        for k, i in enumerate(lines[key]):
            token = data["text"][i].strip()
            if k > 0:
                cursor += 1  # the " " joining words
            x0, y0 = data["left"][i], data["top"][i]
            x1, y1 = x0 + data["width"][i], y0 + data["height"][i]
            words.append((token, x0, y0, x1, y1, cursor))
            confidences.append(float(data["conf"][i]) / 100.0)
            line_tokens.append(token)
            cursor += len(token)
        text_parts.append(" ".join(line_tokens))
    return _PageResult(text="\n".join(text_parts), words=words, confidences=confidences)


def _rapidocr_page(img, engine) -> _PageResult:
    """Run RapidOCR; page text is its detected lines joined by newlines, and each
    line becomes one box (line-level granularity is what PP-OCR provides)."""
    import numpy as np

    result = engine(np.array(img))
    text_parts: list[str] = []
    words: list[tuple[str, float, float, float, float, int]] = []
    confidences: list[float] = []
    cursor = 0
    if result.txts:
        for line_text, box, score in zip(result.txts, result.boxes, result.scores):
            line_text = str(line_text)
            if not line_text.strip():
                continue
            if text_parts:
                cursor += 1  # the "\n"
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            words.append((line_text, min(xs), min(ys), max(xs), max(ys), cursor))
            confidences.append(float(score))
            text_parts.append(line_text)
            cursor += len(line_text)
    return _PageResult(text="\n".join(text_parts), words=words, confidences=confidences)


def _assemble(
    raw: bytes,
    document_id: str,
    page_results: list[_PageResult],
    extractor: str,
    extractor_version: str,
    quality: OcrQuality,
    px_to_pt: float,
) -> CanonicalText:
    spans: list[PageSpan] = []
    words: list[WordBox] = []
    parts: list[str] = []
    cursor = 0
    for idx, pr in enumerate(page_results):
        if idx > 0:
            cursor += 1  # page separator "\n" (same convention as M1)
        spans.append(PageSpan(page_number=idx + 1, char_start=cursor, char_end=cursor + len(pr.text)))
        for token, x0, y0, x1, y1, local_start in pr.words:
            words.append(
                WordBox(
                    text=token,
                    page=idx + 1,
                    x0=x0 * px_to_pt,
                    y0=y0 * px_to_pt,
                    x1=x1 * px_to_pt,
                    y1=y1 * px_to_pt,
                    char_start=cursor + local_start,
                    char_end=cursor + local_start + len(token),
                )
            )
        parts.append(pr.text)
        cursor += len(pr.text)
    return CanonicalText(
        document_id=document_id,
        source_sha256=hashlib.sha256(raw).hexdigest(),
        extractor=extractor,
        extractor_version=extractor_version,
        full_text="\n".join(parts),
        pages=spans,
        words=words,
        low_yield_pages=[],
        ocr_applied=True,
        ocr_quality=quality,
    )


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def rasterize_iter(raw: bytes, dpi: int):
    """Pages of a PDF as PIL images at the pinned DPI, ONE AT A TIME. The M11
    corpus has 100+ MB scanned statutes (MY ACT 593: 206 pages); holding every
    300-DPI page in memory at once (~26 MB each) does not survive them."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(raw)
    try:
        for page in pdf:
            yield page.render(scale=dpi / 72).to_pil()
            page.close()
    finally:
        pdf.close()


def rasterize(raw: bytes, dpi: int) -> list:
    """All pages of a PDF as PIL images at the pinned DPI (small documents)."""
    return list(rasterize_iter(raw, dpi))


def ocr_document(
    raw: bytes,
    document_id: str,
    languages: str = "eng",
    config: PipelineConfig | None = None,
    evidence_dir: Path | None = None,
    force_escalation: bool = False,
    policy: OcrPolicy | None = None,
) -> CanonicalText:
    """OCR a scanned PDF end to end: Tesseract -> (quality gate) -> RapidOCR ->
    (quality gate) -> manual_review flag. Saves evidence pairs when evidence_dir
    is given.

    policy is the Document's script ladder (languages.ocr_policy); None means
    the all-English one, so nothing changes for a caller that does not care.
    It answers three separate questions, and they do not answer together:

      * the dictionary hit rate is counted against an ENGLISH legal wordlist, so
        on any other script it would read 0.0 and be taken for catastrophic OCR.
        It is recorded as None with its reason instead.
      * the RapidOCR escalation stays on for any script RapidOCR can read (Latin
        and Chinese). A Chinese scan tesseract reads with low confidence
        SHOULD escalate; a Lao or Thai one cannot benefit and is left alone.
      * a Language with no vendored traineddata is flagged manual_review with
        the reason, because it was read as English whatever it says."""
    import pytesseract

    cfg = config or PipelineConfig()
    pol = policy or DEFAULT_POLICY
    wordlist = _load_wordlist()
    px_to_pt = 72.0 / cfg.ocr_dpi

    tess_version = str(pytesseract.get_tesseract_version())
    # Streamed page-at-a-time OCR: evidence PNGs are saved as pages go by
    # (rasterization is deterministic, so the PNG is engine-independent);
    # evidence text files are written at the end from the FINAL engine's text.
    if evidence_dir is not None:
        evidence_dir.mkdir(parents=True, exist_ok=True)
    page_results = []
    for idx, img in enumerate(rasterize_iter(raw, cfg.ocr_dpi), start=1):
        page_results.append(_tesseract_page(img, languages))
        if evidence_dir is not None:
            img.save(evidence_dir / f"page_{idx:03d}.png")
    full_text = "\n".join(pr.text for pr in page_results)
    mean_conf = _mean([c for pr in page_results for c in pr.confidences])
    dict_hit = dictionary_hit_rate(full_text, wordlist) if pol.dictionary_proxy else None
    extractor, version = "tesseract", tess_version

    escalate = force_escalation or (
        pol.rapidocr_escalation
        and (
            mean_conf < cfg.ocr_escalation_min_confidence
            or (dict_hit is not None and dict_hit < cfg.ocr_escalation_min_dict_hit)
        )
    )
    escalated = False
    if escalate:
        from rapidocr import RapidOCR

        engine = RapidOCR()
        page_results = [
            _rapidocr_page(img, engine) for img in rasterize_iter(raw, cfg.ocr_dpi)
        ]
        full_text = "\n".join(pr.text for pr in page_results)
        mean_conf = _mean([c for pr in page_results for c in pr.confidences])
        dict_hit = dictionary_hit_rate(full_text, wordlist) if pol.dictionary_proxy else None
        extractor = "tesseract+rapidocr"
        version = f"{tess_version}+{metadata.version('rapidocr')}"
        escalated = True

    quality = ocr_quality_for(mean_conf, dict_hit, cfg, pol, languages, escalated)
    canonical = _assemble(raw, document_id, page_results, extractor, version, quality, px_to_pt)

    if evidence_dir is not None:
        for idx, pr in enumerate(page_results, start=1):
            (evidence_dir / f"page_{idx:03d}.txt").write_text(pr.text, encoding="utf-8")
        # The proxies beside the pages they were measured on, so a judge reading
        # the evidence pair sees WHY a proxy is null instead of guessing.
        (evidence_dir / "quality.json").write_text(
            json.dumps(
                {
                    "languages": languages,
                    "dictionary_proxy": pol.dictionary_proxy,
                    "rapidocr_escalation": pol.rapidocr_escalation,
                }
                | quality.model_dump(),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    return canonical
