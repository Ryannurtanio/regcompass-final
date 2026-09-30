"""What a Document's Language decides: the OCR language data and the Gate tier.

The organizer records one Language per Document from a fixed list
(`contracts.ORGANIZER_LANGUAGES`). Two mechanical choices hang off it and
nothing else in the pipeline branches on language:

  * **OCR**: which vendored traineddata files tesseract loads. Every non-English
    string keeps `eng` as a secondary language because official gazettes carry
    English headers, act numbers and Latin digits beside the national script.
  * **The Gate's keyword tier**: `gate.legal_tokens` is an English tokenizer
    (`[a-z][a-z0-9-]+`) with an English stopword list, and the Indicator keyword
    vocabularies are English phrases. Against a Lao or Thai page it produces no
    tokens at all, and against an Indonesian page it produces tokens that score
    zero. Since the Gate merges its two tiers with AND, leaving the keyword tier
    switched on for such a Document excludes every chunk and the Run yields
    nothing. Those Documents are shortlisted by meaning alone.

Malay and Portuguese both collapse to "Other" on the organizer's list, so the
tesseract map cannot key on the Language alone: it takes the Economy as a
second key. "Other" in Malaysia means Malay, which is why MY keeps the
`eng+msa` string it has always used, and Malaysia stays on the English keyword
path because its acts are English-bodied with Malay provisions beside them.
"Other" in Timor-Leste means Portuguese (Tetum beside it is written in the same
alphabet): it is read as `por+eng`, and its Documents are shortlisted by
meaning alone, since the English vocabulary scores a Portuguese text zero.

Script-specific tokenizers and keyword translation stay out of scope (the Q18
decision); this module only decides which existing lane a Document takes.
"""

from __future__ import annotations

from dataclasses import dataclass

# The tesseract language string per organizer Language, for the traineddata
# vendored under vendor/tessdata. A Language absent from this map has no
# vendored data and falls back to DEFAULT_TESSERACT with a note; adding one is
# a traineddata file, a row here, and a row in the README artifact table.
TESSERACT_BY_LANGUAGE: dict[str, str] = {
    "English": "eng",
    "Bahasa Indonesia": "ind+eng",
    "Thai": "tha+eng",
    "Lao": "lao+eng",
    "Russian": "rus+eng",
    "Chinese": "chi_sim+eng",
    "Vietnamese": "vie+eng",
    "Kazakh": "kaz+eng",
    "Mongolian": "mon+eng",
    "Hindi": "hin+eng",
}

DEFAULT_TESSERACT = "eng"

# Malaysia's own string, unchanged since M11: its acts are English with Malay
# provisions, and the repro stream is pinned to these exact bytes.
MALAYSIA_ECONOMY = "MY"
MALAYSIA_TESSERACT = "eng+msa"
_MALAYSIA_DEFAULT_LANGUAGES = (None, "English", "Other")

# Timor-Leste's own string: its laws are Portuguese, which the organizer's
# list has no word for, so "Other" there is Portuguese.
TIMOR_LESTE_ECONOMY = "TL"
TIMOR_LESTE_TESSERACT = "por+eng"
_TIMOR_LESTE_DEFAULT_LANGUAGES = (None, "Other")

# Languages written in a script of their own, outside the Latin alphabet. A
# text layer of one of these that is nearly all Latin letters is not the
# Document's text (shortlist.garbage_text_layer).
NON_LATIN_SCRIPT_LANGUAGES = frozenset(
    {"Thai", "Lao", "Chinese", "Hindi", "Kazakh", "Russian", "Mongolian"}
)

# Languages written in the Latin alphabet. `ocr.dictionary_hit_rate` counts hits
# against an English legal wordlist, so it only means something for these.
LATIN_SCRIPT_LANGUAGES = frozenset({"English", "Bahasa Indonesia", "Vietnamese", "Other"})

# Languages whose script the OCR escalation can actually read. `RapidOCR()` with
# its default models covers Latin text and Chinese; it has no Lao, Thai,
# Devanagari or Cyrillic model, and re-running one of those through it can only
# make the stream worse. This is a SEPARATE question from the dictionary proxy:
# a Chinese scan has no English dictionary hit rate, but when tesseract reads
# it with low confidence RapidOCR's Chinese model is a genuine second reader.
RAPIDOCR_LANGUAGES = LATIN_SCRIPT_LANGUAGES | {"Chinese"}

# The Languages whose Documents keep the Gate's English keyword tier. Everything
# else on the organizer's list goes meaning-only, so a Language added to the
# list later defaults to the lane that cannot silently return nothing.
KEYWORD_TIER_LANGUAGES = frozenset({"English", "Other"})

# Share of a Document's letters that may be non-Latin before the keyword tier is
# dropped whatever the Language says. A page of English act text quoting a few
# words of national script stays under it; a scanned Lao statute is over 0.9.
NON_LATIN_SHARE_MAX = 0.2

# Deterministic cap on the script test: the leading characters of the stream are
# enough to tell a Lao statute from an English one, and a 200k-character scan of
# a 200-page act on every Document is waste.
_SCRIPT_SAMPLE_CHARS = 200_000


def tesseract_languages(language: str | None, economy: str | None = None) -> str:
    """The `-l` string for tesseract for one Document.

    `language` is the Document's Language as the organizer spells it (None when
    a Document predates the column). `economy` disambiguates "Other"."""
    if economy == MALAYSIA_ECONOMY and language in _MALAYSIA_DEFAULT_LANGUAGES:
        return MALAYSIA_TESSERACT
    if economy == TIMOR_LESTE_ECONOMY and language in _TIMOR_LESTE_DEFAULT_LANGUAGES:
        return TIMOR_LESTE_TESSERACT
    if language is None:
        return DEFAULT_TESSERACT
    return TESSERACT_BY_LANGUAGE.get(language, DEFAULT_TESSERACT)


def tesseract_language_note(language: str | None, economy: str | None = None) -> str | None:
    """A one-line reason when a Language has no vendored traineddata and OCR
    falls back to English, or None when the mapping is exact. The caller puts it
    on the Run's progress stream so a fallback is never silent."""
    if language is None or language in TESSERACT_BY_LANGUAGE:
        return None
    if economy == MALAYSIA_ECONOMY and language in _MALAYSIA_DEFAULT_LANGUAGES:
        return None
    if economy == TIMOR_LESTE_ECONOMY and language in _TIMOR_LESTE_DEFAULT_LANGUAGES:
        return None
    return (
        f"Language {language!r} has no vendored tesseract data;"
        f" OCR falls back to {DEFAULT_TESSERACT}"
    )


def is_latin_script_language(language: str | None) -> bool:
    """Whether the English dictionary-hit proxy means anything for this
    Language. None (Language not recorded) is treated as Latin, which is what
    every Document did before the column was threaded through."""
    return language is None or language in LATIN_SCRIPT_LANGUAGES


def is_english_language(language: str | None) -> bool:
    """Whether this Document's recorded Language IS English, on the organizers'
    spelling plus the two ISO codes a database row can carry. None is not an
    answer either way: a Document with no Language recorded makes no claim, and
    the caller decides from the text instead of assuming English and skipping
    the Gloss a non-English quote needs."""
    return (language or "").strip().lower() in {"english", "en", "eng"}


def has_vendored_tessdata(language: str | None, economy: str | None = None) -> bool:
    """Whether tesseract will read this Document with data for its own script,
    rather than falling back to English because nothing was vendored for it."""
    return tesseract_language_note(language, economy) is None


@dataclass(frozen=True)
class OcrPolicy:
    """What the OCR quality ladder may do for one Document's script. The three
    answers travel together because they are one decision made three ways, and
    they are NOT the same answer: a Chinese scan skips the English dictionary
    proxy yet still escalates to RapidOCR (which reads Chinese), and only a
    Language with no vendored traineddata is flagged for a human outright."""

    dictionary_proxy: bool  # the English wordlist hit rate is meaningful
    rapidocr_escalation: bool  # RapidOCR has a model for this script
    manual_review_reason: str | None  # set = force manual_review, with the why


def ocr_policy(language: str | None, economy: str | None = None) -> OcrPolicy:
    """The OCR quality ladder for one Document's Language."""
    reason = None
    if not has_vendored_tessdata(language, economy):
        reason = (
            f"no tesseract language data is vendored for Language {language!r},"
            f" so the page was read as {DEFAULT_TESSERACT}: the text is not"
            " trustworthy without a human check"
        )
    return OcrPolicy(
        dictionary_proxy=is_latin_script_language(language),
        rapidocr_escalation=language is None or language in RAPIDOCR_LANGUAGES,
        manual_review_reason=reason,
    )


def non_latin_share(text: str) -> float:
    """Share of the text's letters written outside the Latin alphabet.

    Punctuation, digits and whitespace are ignored: a statute is mostly
    numbering either way. Latin Extended (accents) and Latin Extended Additional
    (Vietnamese) count as Latin, so a Vietnamese Document is not mistaken for a
    non-Latin one by this test alone."""
    latin = non_latin = 0
    for ch in text[:_SCRIPT_SAMPLE_CHARS]:
        if not ch.isalpha():
            continue
        if ch.isascii() or "À" <= ch <= "ɏ" or "Ḁ" <= ch <= "ỿ":
            latin += 1
        else:
            non_latin += 1
    total = latin + non_latin
    return non_latin / total if total else 0.0


def keyword_tier_applies(
    language: str | None,
    text: str | None = None,
    non_latin_share_max: float = NON_LATIN_SHARE_MAX,
    *,
    economy: str | None = None,
) -> bool:
    """Whether the Gate's English keyword tier can read this Document.

    The Language decides first, the text second: a Document labelled English
    whose stream is Lao (a portal default that nobody corrected) still takes the
    meaning-only lane, because the label is a claim and the bytes are evidence.

    The test does not run the other way, and deliberately: an Indonesian
    Document is Latin-script and would pass a script test while the English
    keyword vocabulary still scores it zero. So Lao PDR's official English PDFs,
    carrying the Economy's default Language of Lao, take the meaning-only lane
    too. That is a trade, not a free win: the two rules rank by different
    things, so a chunk that ranks in an Indicator's bm25 top-k but outside the
    Pillar's cosine top-k passes the two-tier Gate and fails this one. The
    budget is identical either way (the same gate_bm25_top_k caps both), and
    meaning-only never returns nothing, which is what the two-tier rule does on
    a script it cannot tokenize. Timor-Leste's "Other" is Portuguese, so the
    Economy sends it meaning-only too."""
    if economy == TIMOR_LESTE_ECONOMY and language in _TIMOR_LESTE_DEFAULT_LANGUAGES:
        return False
    if language is not None and language not in KEYWORD_TIER_LANGUAGES:
        return False
    if text and non_latin_share(text) > non_latin_share_max:
        return False
    return True


# Scripts a text layer can still show when its words are garbage, and the
# Language each is read as. Cyrillic is written by three Languages on the
# organizers' list, so the Economy's own Languages decide which.
_SCRIPT_LANGUAGE = (
    ("฀", "๿", "Thai"),
    ("຀", "໿", "Lao"),
    ("ऀ", "ॿ", "Hindi"),
    ("一", "鿿", "Chinese"),
    ("Ѐ", "ӿ", "Russian"),
)
_CYRILLIC_LANGUAGES = ("Kazakh", "Mongolian", "Russian")
# Characters of one script a layer must show before it counts as present.
_SCRIPT_MIN_CHARS = 20


def garbage_ocr_languages(
    language: str | None,
    economy: str | None,
    text: str,
    economy_languages: tuple[str, ...] | list[str] = (),
) -> tuple[str, str | None]:
    """(tesseract `-l` string, the Language to read it as) for a PDF whose text
    layer was judged garbage. The layer's own Language may be wrong, and its
    words are gone, so OCR is given every script the Document could be in:
    what the layer still shows, the Document's Language, and the Languages its
    Economy publishes in (India's Hindi gazettes are filed as English by
    default). English stays last, as the secondary language every string
    carries. The Language returned is the first non-Latin one among them, and
    decides the OCR quality ladder (languages.ocr_policy)."""
    sample = text[:_SCRIPT_SAMPLE_CHARS]
    names: list[str] = []
    for lo, hi, name in _SCRIPT_LANGUAGE:
        if sum(1 for ch in sample if lo <= ch <= hi) >= _SCRIPT_MIN_CHARS:
            if name == "Russian":
                names.extend(
                    [n for n in economy_languages if n in _CYRILLIC_LANGUAGES] or ["Russian"]
                )
            else:
                names.append(name)
    names += [n for n in (language, *economy_languages) if n]
    codes: list[str] = []
    for name in names:
        for code in tesseract_languages(name, economy).split("+"):
            if code not in codes:
                codes.append(code)
    codes = [c for c in codes if c != "eng"] + ["eng"]
    reading = next((n for n in names if n in NON_LATIN_SCRIPT_LANGUAGES), language)
    return "+".join(codes), reading
