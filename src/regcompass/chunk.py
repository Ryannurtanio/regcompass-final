"""M4 - chunk the canonical stream into section-level slices.

Primary: a deterministic structural splitter driven by per-drafting-style
profiles (SG "N." / "N.—(1)", SSO HTML "N." alone on a line, MY "Section N.",
AU/generic "N Title" and "N.N Title"). Chunk text is ALWAYS sliced from the
stream by character offsets via Chunk.from_stream, never re-emitted.

Fallback: when no profile finds credible structure (degraded OCR documents),
the LLM boundary fallback fires. The model sees line-numbered text and returns
line positions and labels ONLY; positions are converted to character offsets
mechanically and any text the model emits is discarded unread. On invalid
output it gets one stricter retry. When that fails too, or no model is wired
(the judged path), the document is split into numbered passages of about a
section's size ("Passage N"), marked unstructured in the report: coverage
never breaks and the Gate still reads every passage.

split_document only calls the LLM when a completion_fn is explicitly passed
(the CLI wires the LiteLLM one); the module never reaches the network by
default. llm_boundaries runs on the selected Engine (the configured default when
none is named) with num_retries=0 (the silent-retry trap).
"""

from __future__ import annotations

import json
import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from statistics import median
from typing import Callable, Sequence

from .config import CONFIG_DIR
from .contracts import (
    BoundaryProposal,
    CanonicalText,
    Chunk,
    Engine,
    PageSpan,
    PipelineConfig,
)
from .engines import make_completion, resolve_engine

# completion_fn contract: (prompt, strict) -> raw model content string
CompletionFn = Callable[[str, bool], str]

HEADING_LOOKBACK = 6  # max lines a section start extends backward over headings
CHUNK_KINDS = {"section", "front_matter", "toc", "schedule", "other"}

# Testing hook: when set, the default LiteLLM call runs offline against this
# canned content instead of the network (litellm mock_response).
_LITELLM_MOCK_RESPONSE: str | None = None

BACK_MATTER_MARKERS = (
    "LEGISLATIVE HISTORY",
    "COMPARATIVE TABLE",
    "ENDNOTES",
    "LIST OF AMENDMENTS",
    "LIST OF SECTIONS AMENDED",
)

TOC_HEAD_RE = re.compile(r"^\s*(table of contents|arrangement of sections|arrangement of acts)\s*$", re.I)
DOT_LEADER_RE = re.compile(r"\.{5,}\s*\d*\s*$")
PART_RE = re.compile(r"^\s{0,4}(?:PART|Part)\s+(\d+(?:\.\d+)?[A-Z]{0,2}|[IVXLC]+[A-Z]?)\b")
CHAPTER_RE = re.compile(r"^\s{0,4}(?:CHAPTER|Chapter)\s+(\d+[A-Z]?|[IVXLC]+)\b")
SCHEDULE_RE = re.compile(r"^\s{0,4}(?:THE\s+)?SCHEDULES?\b")
_TRAILING_PUNCT = (".", ";", ":", ",", "—", "–", "-")

# Chinese drafting: 第N章 is a chapter, 第N节 a section within one, 第N条 an
# article (the provision the Gate reads). The number is normally a Chinese
# numeral; the profile below also accepts digits, which some scans produce.
# The 8-glyph cap is what an article above a thousand needs: 第一千二百三十四条
# spells its number with seven.
_ZH_NUM_CLASS = "一二三四五六七八九十百千零〇0-9"
_ZH_NUM_LEN = "{1,8}"
CHAPTER_ZH_RE = re.compile(rf"^\s{{0,2}}第(?P<num>[{_ZH_NUM_CLASS}]{_ZH_NUM_LEN})章")
SUBSECTION_ZH_RE = re.compile(rf"^\s{{0,2}}第[{_ZH_NUM_CLASS}]{_ZH_NUM_LEN}节")
# CJK unified ideographs (U+4E00 to U+9FFF, written out because the file is
# full of them anyway). A line carrying one is judged by the Chinese heading
# forms above, never by the "the line is all upper case, so it is a heading"
# rule: Chinese has no letter case, so that rule says yes to every line.
_CJK_RE = re.compile(r"[一-鿿]")

# Indonesian statutes end with the official Elucidation: "PENJELASAN" over
# "ATAS" ("elucidation of"), a general part, then notes that head each one
# "Pasal N" exactly as the body heads the article itself. A quote from a note
# is not the article, so the notes carry their own label context. The word is
# matched loosely because the gazette text layers misread it ("PENJEI,ASAN").
_ELUCIDATION_HEAD_RE = re.compile(r"^\s{0,2}PENJ\S{4,8}\s*$")
ELUCIDATION = "Elucidation"


def _elucidation_line(lines: list[str]) -> int | None:
    """Line of the Elucidation heading, or None when the document has none."""
    for i, line in enumerate(lines):
        if _ELUCIDATION_HEAD_RE.match(line):
            following = next((l.strip() for l in lines[i + 1 : i + 3] if l.strip()), "")
            if following == "ATAS":
                return i
    return None


_ZH_DIGITS = {
    "〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_ZH_UNITS = {"十": 10, "百": 100, "千": 1000}


def zh_numeral_to_int(num: str) -> int | None:
    """A Chinese numeral as an integer, or None when the string is not one.

    Article numbers in a Chinese statute are written 第一条 ... 第七十四条, and
    the chunker orders provisions by number, so "十二" has to become 12 before
    anything can be sorted. Plain arabic digits pass through (some scans carry
    them), but a string MIXING the two spellings is rejected: reading "0一" as
    one number invents an article that is in no act, and the caller's sentinel
    key keeps it out of every run instead.

    Two spellings, told apart by whether a unit glyph is present:

      * MULTIPLICATIVE, the usual one, built from 十 (ten), 百 (hundred) and
        千 (thousand) with 零 as the filler: 十二 is 12, 一千零五 is 1005.
      * POSITIONAL, which some drafters use instead, writing the digits in
        order with 〇 for zero: 一〇 is 10 and 二〇二一 is 2021. Read
        multiplicatively those say 1 and 2, so the two readings cannot share a
        branch. A single glyph is never positional: both readings agree on it
        and the multiplicative one is the plain answer."""
    if not num:
        return None
    if num.isdigit():
        return int(num)
    if any(c not in _ZH_DIGITS and c not in _ZH_UNITS for c in num):
        return None
    if len(num) > 1 and not any(c in _ZH_UNITS for c in num):
        return int("".join(str(_ZH_DIGITS[c]) for c in num))
    total = section = digit = 0
    for ch in num:
        if ch in _ZH_DIGITS:
            digit = _ZH_DIGITS[ch]
            continue
        unit = _ZH_UNITS[ch]
        if unit >= 100:
            total += (digit or 1) * unit
            section = 0
        else:  # 十: a bare 十 means ten, so an absent digit reads as one
            section = (digit or 1) * 10
        digit = 0
    return total + section + digit


@dataclass(frozen=True)
class StyleProfile:
    """One drafting style. ambiguous profiles (patterns that plain prose or
    numbered lists can also match) must clear the median-length gate and the
    full chunk_min_run; unambiguous ones only need a run of min_run_override."""

    name: str
    pattern: re.Pattern
    ambiguous: bool
    min_run_override: int | None = None
    # apply _bare_title_is_noise to bare-number candidates (only meaningful for
    # heading-style profiles where the text after the number IS the title;
    # for SG "N. prose" the trailing text is body prose and may end with ".")
    title_noise_filter: bool = False
    # read candidates through _article_word_heading (the article-word styles,
    # where a sentence wrapped just before a cross-reference opens with the
    # same words)
    article_tail_filter: bool = False
    # an article-word style: where it finds a credible run it is the
    # document's structure, however many numbered items the ambiguous styles
    # also find (those are the clauses inside the articles)
    article: bool = False
    # an article word that an English act can also carry (_leads)
    english: bool = False


# The article word of a non-English drafting tradition followed by an arabic
# number: Lao "ມາດຕາ 5", Thai "มาตรา 5", Indonesian "Pasal 5", Russian
# "Статья 5", Vietnamese "Điều 5". "ນາດຕາ" is the Lao word as the OCR reads it
# with its first letter misread (it is not a word of its own), and the number
# may run straight into the title, "ມາດຕາ 4ການ...", because the Lao text
# layers drop the space. The second branch is the Indonesian heading
# as the gazette text layers misread it, "Pasal2T", "PasaJ22", "Pasal L4":
# only ALONE on its line (where the Indonesian style puts every heading) and
# only with at least one real digit, so the roman articles I and II of an
# amending act are never read as numbers. _article_word_heading reads it.
# A Russian article inserted by amendment, "Статья 8-1", is an article of its
# own (num_ru), not article 8 with a title that starts "-1". Portuguese
# "Artigo 5.º" (Timor-Leste, Tetum "Artigu") carries an ordinal mark after the
# number, which _article_word_heading reads (_PT_ORDINAL_RE).
ARTICLE_WORD_RE = re.compile(
    r"^\s{0,2}(?:"
    r"Статья\s+(?P<num_ru>\d{1,3}-\d{1,3})(?=\.|\s*$)"
    r"|(?:ມາດຕາ|ນາດຕາ|มาตรา|Pasal|Статья|Điều|Artigo|ARTIGO|Artigu|ARTIGU)"
    r"\s+(?P<num>\d{1,3}[A-Z]{0,2})(?![0-9A-Za-z])"
    r"|Pas[a4][l1IJ]\s*(?P<misread>(?=[0-9OTLlIt]*\d)[0-9OTLlIt]{1,3}[A-C]?)\s*$"
    r")"
)
# The Portuguese ordinal mark after an article number, as the Jornal da
# República prints it or its text layers read it: "5.º", "5º", "5.°", "5.o".
# "6.º-A" is an article inserted by amendment, numbered 6A.
_PT_ORDINAL_RE = re.compile(r"(?:\.?[º°]|\.o(?![A-Za-z]))(?:-(?P<letter>[A-Z])(?![A-Za-z]))?")
# What the text layers print for a digit: O for 0; I, l, L, t for 1; T for 7.
_MISREAD_DIGITS = str.maketrans("OTLlIt", "071111")
# Chinese puts its article word AROUND the number: "第十二条 ...". The number
# may stand alone on its line: the regulator's web pages print it in bold,
# <strong>第一条</strong>, and the HTML lane breaks the stream at every tag,
# so the article's body starts on the next line. The "title" group starts
# after 条, so the glyph is not read as a title that every article shares
# (a title repeated on five lines is page furniture and is thrown away).
ARTICLE_ZH_RE = re.compile(rf"^\s{{0,2}}第(?P<num>[{_ZH_NUM_CLASS}]{_ZH_NUM_LEN})条(?P<title>.*)")
# Mongolian and Kazakh put the number FIRST: "1 дүгээр зүйл.Хуулийн зорилт",
# "2 дугаар зүйл", "5-р зүйл" (and the rarer "Зүйл 5"); Kazakh "1-бап.
# Негізгі ұғымдар" or "1 бап", and "8-1-бап" for an article inserted by
# amendment. The word must end there: "8 дугаар зүйлд
# заасан" and "5-бабында" are the same words declined inside a sentence.
ARTICLE_NUM_FIRST_RE = re.compile(
    r"^\s{0,2}(?:"
    r"(?P<num>\d{1,3})\s*(?:-\s*р|д[үу]г(?:ээ|аа)р)\s+зүйл"
    r"|(?P<num_kz>\d{1,3}(?:-\d{1,3})?)\s*-?\s*бап"
    r"|Зүйл\s+(?P<num_word>\d{1,3})(?!\d)"
    r")(?![^\W\d_])(?P<title>.*)"
)
# English translations of civil-law statutes: "Article 1. Scope of
# regulation", "ARTICLE 2". Unlike the words above this one CAN occur in an
# English act, where a scheduled convention carries its own articles, so it
# only outranks the numbered styles on the share rule in _leads. "Article
# 8-1" is an article inserted by amendment, numbered on its own.
ARTICLE_EN_RE = re.compile(
    r"^\s{0,2}(?:Article|ARTICLE)\s+(?P<num>\d{1,3}(?:-\d{1,3}(?=\.|\s*$))?[A-Z]{0,2})(?![0-9A-Za-z-])"
)
_NUM_GROUPS = ("num", "num_ru", "num_kz", "num_word")


def _heading_title(line: str, m: re.Match) -> str:
    """The text after the number on a heading line ("" if none): where the
    pattern marks it, from its "title" group, else from the end of the number."""
    start = m.start("title") if "title" in m.re.groupindex else m.end("num")
    return line[start:].strip().lstrip(".").strip()


def _article_word_heading(line: str, m: re.Match) -> tuple[str, str] | None:
    """(number, title) of an article-word heading line, or None when the line
    is really prose or page furniture:

      * a sentence wrapped just before a cross-reference, "Pasal 13 ayat (1)
        dan ayat (2) dikecualikan untuk:". A heading's tail is empty (the
        Indonesian style) or a title, and a title never opens with a
        lower-case letter; Lao and Thai have no letter case, so their titled
        headings are never touched.
      * a list of cross-references that wrapped onto a new line, "Pasal 35,
        Pasal 36, Pasal 37, ...".
      * a sentence that ends on a cross-reference, "Pasal 40.", or the
        catchword a printer sets at the foot of a page to announce the next
        page's first line, "Pasal 18. .": the tail is punctuation and
        nothing else.

    A number the text layer split in two, "Pasal 4 1", is article 41, and a
    letter O inside a number is a zero ("Pasal 7O" is article 70). Where the
    article word follows the number (ARTICLE_NUM_FIRST_RE) the tail is what
    follows the word, its "title" group."""
    groups = m.re.groupindex
    if "misread" in groups and m.group("misread"):
        raw = m.group("misread")
        suffix = raw[-1] if raw[-1] in "ABC" else ""
        return raw[: len(raw) - len(suffix)].translate(_MISREAD_DIGITS) + suffix, ""
    name = next(g for g in _NUM_GROUPS if g in groups and m.group(g) is not None)
    num = m.group(name)
    while re.search(r"\dO", num):
        num = re.sub(r"(?<=\d)O", "0", num)
    if "title" in groups:
        tail = m.group("title").strip()
    else:
        tail = line[m.end(name):].strip()
        ordinal = _PT_ORDINAL_RE.match(tail)
        if ordinal:
            num += ordinal.group("letter") or ""
            tail = tail[ordinal.end():].strip()
        if num.isdigit() and re.fullmatch(r"\d{1,2}", tail) and len(num + tail) <= 3:
            return num + tail, ""
    if tail and not any(ch.isalnum() for ch in tail):
        return None
    title = tail.lstrip(".").strip()
    if title and (title[0].islower() or title[0] in ",;"):
        return None
    return num, title


PROFILES = (
    # "Section 12. Title" (Malaysia)
    StyleProfile("my_section_word", re.compile(r"^\s{0,2}Section\s+(?P<num>\d{1,3}[A-Z]{0,2})\.\s+\S"), False, 2),
    # The article word of a non-English drafting tradition (ARTICLE_WORD_RE).
    # Unambiguous: none of these words occurs in an English act, so the profile can never fire on one, and a
    # scanned non-English statute otherwise falls back to numbered passages and
    # its Mappings lose the article they come from.
    StyleProfile("article_word", ARTICLE_WORD_RE, False, 2, article_tail_filter=True, article=True),
    # Number-first article words (ARTICLE_NUM_FIRST_RE): Mongolian, Kazakh.
    StyleProfile(
        "article_num_first", ARTICLE_NUM_FIRST_RE, False, 2, article_tail_filter=True, article=True
    ),
    # "Article N" in an English translation (ARTICLE_EN_RE).
    StyleProfile(
        "article_en", ARTICLE_EN_RE, False, 2, article_tail_filter=True, article=True, english=True
    ),
    # The same idea for Chinese (ARTICLE_ZH_RE). Unambiguous for the same reason,
    # and its own profile because the number is a Chinese numeral, which only
    # _num_key knows how to order. Verified against the OCR of the team's
    # Personal Information Protection Law scan: 54 articles, 16 chapter lines.
    StyleProfile("article_zh", ARTICLE_ZH_RE, False, 2, article=True),
    # section number alone on its own line: "12." (SSO HTML rendering)
    StyleProfile("sso_num_alone", re.compile(r"^(?P<num>\d{1,3}[A-Z]{0,3})\.\s*$"), False, 2),
    # "12. Text" or "12.—(1) Text" (SG acts; OCR may degrade the dash)
    StyleProfile(
        "sg_num_dot",
        re.compile(r"^(?P<num>\d{1,3}[A-Z]{0,3})\.(?:[—–-]\(|\s+\S)"),
        True,
    ),
    # "12 Title" or "12.3 Title" heading lines (AU compilations, Niue, generic).
    # No "(" in the title-start class: wrapped lines quoting section numbers
    # look like '71.11 (causing harm to ...' and must not become candidates.
    StyleProfile(
        "num_title",
        re.compile(r"^(?P<num>\d{1,3}(?:\.\d{1,3})?[A-Z]{0,2})\s+[A-Z“\"]"),
        True,
        title_noise_filter=True,
    ),
)

_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def _bare_title_is_noise(title: str) -> bool:
    """Wrapped prose and table rows masquerading as bare-number headings:
    dates ('15 December 2001 to all other offences.'), percentage table rows
    ('1 Ethylene glycol dinitrate (EGDN) 0.2% by mass'), and sentence ends."""
    if title.startswith(_MONTHS):
        return True
    if "%" in title:
        return True
    if title.endswith(".") and not title.endswith("etc."):
        return True
    return False


@dataclass
class ChunkingReport:
    """What the splitter did and why; logged by the caller via log_stage."""

    style: str | None = None
    n_sections: int = 0
    n_chunks: int = 0
    coverage_chars: int = 0
    fallback_used: bool = False
    fallback_succeeded: bool = False
    fallback_attempts: int = 0
    # no structure was found and the document was split into numbered passages
    unstructured: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Candidate:
    line: int
    num_key: tuple
    has_dot_part: bool
    title: str  # text after the number on the heading line ("" if none)


def _num_key(num: str) -> tuple:
    """Order key for section numbers: '3' < '3A' < '4' < '4.1' < '4.2A'.

    A Chinese numeral becomes its integer here, which is what puts 第十条
    before 第十二条 instead of leaving the run of articles unordered."""
    # "8-1" (an article inserted by amendment) sits after 8 and before 9,
    # exactly where "8.1" does; the key keeps its hyphen for the label.
    m = re.fullmatch(r"(\d+)(?:([.-])(\d+))?([A-Z]{0,3})", num)
    if not m:
        zh = zh_numeral_to_int(num)
        if zh is not None:
            return (zh, -1, "")
        return (10**9, 10**9, num)  # defensive; the profiles produce no other shape
    major, sep, minor, suffix = m.groups()
    key = (int(major), int(minor) if minor is not None else -1, suffix or "")
    return key + ("-",) if sep == "-" else key


def _line_offsets(full_text: str) -> tuple[list[str], list[int]]:
    lines = full_text.split("\n")
    offsets, off = [], 0
    for line in lines:
        offsets.append(off)
        off += len(line) + 1
    return lines, offsets


def _toc_region(lines: list[str], profile: StyleProfile) -> tuple[int, int] | None:
    """(start_line, end_line_exclusive) of the contents block, or None.

    Dot-leader block first (AU-style printed contents); else an explicit
    contents marker line followed by entry-like lines (SG-style)."""
    dotted = [i for i, l in enumerate(lines) if DOT_LEADER_RE.search(l)]
    if len(dotted) >= 10:
        # first cluster: consecutive dotted lines with gaps <= 25 lines
        start = end = dotted[0]
        for i in dotted[1:]:
            if i - end > 25:
                break
            end = i
        if end - start >= 10:
            return (start, end + 1)
    for i, line in enumerate(lines):
        if TOC_HEAD_RE.match(line):
            entry = re.compile(r"^\s{0,4}(\d{1,3}[A-Z]{0,3}\s+\S|Part\s+\d|PART\s+\S|Division\s+\d|Long Title|SCHEDULE)")
            last = None
            for j in range(i + 1, len(lines)):
                if profile.pattern.match(lines[j]):
                    break
                if entry.match(lines[j]):
                    last = j
            if last is not None:
                return (i, last + 1)
            return None
    return None


def _candidates(lines: list[str], profile: StyleProfile, toc: tuple[int, int] | None) -> list[_Candidate]:
    out = []
    for i, line in enumerate(lines):
        if toc and toc[0] <= i < toc[1]:
            continue
        if DOT_LEADER_RE.search(line):
            continue
        m = profile.pattern.match(line)
        if not m:
            continue
        if profile.article_tail_filter:
            heading = _article_word_heading(line, m)
            if heading is None:
                continue
            num, title = heading
        else:
            num, title = m.group("num"), _heading_title(line, m)
        has_dot = "." in num
        if profile.title_noise_filter and not has_dot and _bare_title_is_noise(title):
            continue
        out.append(_Candidate(i, _num_key(num), has_dot, title))
    return out


CATCHWORD_MAX_LINES = 8  # a page foot plus the next page's running head


def _drop_catchwords(cands: list[_Candidate]) -> list[_Candidate]:
    """A heading repeated under the same number within a page break is a
    catchword (the foot of one page announcing the head of the next) followed
    by the heading itself: keep the second, which the body follows."""
    return [
        c
        for c, nxt in zip(cands, [*cands[1:], None])
        if nxt is None
        or nxt.num_key != c.num_key
        or nxt.line - c.line > CATCHWORD_MAX_LINES
    ]


def _kill_repeated_bare_titles(cands: list[_Candidate]) -> list[_Candidate]:
    """Running footers look like 'PAGE Title-of-the-Act' with the same title on
    hundreds of pages; kill bare-number candidates whose title repeats >= 5
    times. Dotted numbers are exempt (repeated titles like 'N.N Definitions'
    are legitimate)."""
    counts: dict[str, int] = {}
    for c in cands:
        if not c.has_dot_part and c.title:
            counts[c.title] = counts.get(c.title, 0) + 1
    return [c for c in cands if c.has_dot_part or not c.title or counts.get(c.title, 0) < 5]


def _increasing_runs(cands: list[_Candidate]) -> list[list[_Candidate]]:
    runs: list[list[_Candidate]] = []
    for c in cands:
        if runs and c.num_key > runs[-1][-1].num_key:
            runs[-1].append(c)
        else:
            runs.append([c])
    return runs


def _back_matter_start(lines: list[str], after_line: int) -> int | None:
    for i in range(after_line + 1, len(lines)):
        if lines[i].strip() in BACK_MATTER_MARKERS:
            return i
    return None


# The English article word leads only where its longest unbroken run of
# numbers is at least a third of the longest run the numbered styles find.
# Runs, not totals: a translated statute's clauses restart at 1 inside every
# article, so however many clauses it has, their longest run is one article's
# worth (8 clauses against 40 articles). An English act or volume that
# schedules conventions numbers its own sections in long runs against a few
# dozen convention articles (the Niue volume: 79 against 329).
ENGLISH_ARTICLE_SHARE = 3


def _longest_run(keys: Sequence[tuple]) -> int:
    """Length of the longest strictly increasing stretch of number keys."""
    best = run = 0
    prev = None
    for key in keys:
        run = run + 1 if prev is not None and key > prev else 1
        best, prev = max(best, run), key
    return best


def _leads(profile: StyleProfile, article_run: int, numbered_run: int) -> bool:
    """Whether an article-word style outranks the numbered styles here. The
    article words no English act carries always lead; the English one leads
    only on the run share above."""
    if not profile.article:
        return False
    return not profile.english or article_run * ENGLISH_ARTICLE_SHARE >= numbered_run


def _select_profile(
    lines: list[str], offsets: list[int], total: int, config: PipelineConfig
) -> tuple[StyleProfile, list[_Candidate], tuple[int, int] | None, int | None] | None:
    """Try every profile; return (profile, kept candidates, toc, back_matter_line)
    for the highest-scoring credible one, or None (fallback fires)."""
    credible: list[tuple[StyleProfile, list[_Candidate], tuple[int, int] | None, int | None]] = []
    for profile in PROFILES:
        toc = _toc_region(lines, profile)
        cands = _kill_repeated_bare_titles(_candidates(lines, profile, toc))
        if profile.article_tail_filter:
            cands = _drop_catchwords(cands)
        if not cands:
            continue
        back = _back_matter_start(lines, cands[0].line)
        if back is not None:
            cands = [c for c in cands if c.line < back]
        min_run = profile.min_run_override or config.chunk_min_run
        kept = [c for run in _increasing_runs(cands) if len(run) >= min_run for c in run]
        if not kept:
            continue
        if profile.ambiguous:
            end_char = offsets[back] if back is not None else total

            def _spans(cs: list[_Candidate]) -> list[int]:
                return [
                    (offsets[cs[i + 1].line] if i + 1 < len(cs) else end_char) - offsets[c.line]
                    for i, c in enumerate(cs)
                ]

            # Degenerate-run drop: a run whose OWN median span is tiny is an
            # arrangement-of-sections TOC or an index list (SSO whole-act PDFs
            # render both in the exact body "N. Title" shape), never body
            # structure. Body numbering restarts after such lists, so they
            # always form their own run.
            spans = _spans(kept)
            starts = {i for i, c in enumerate(kept) if i == 0 or c.num_key <= kept[i - 1].num_key}
            survivors: list[_Candidate] = []
            run_spans: list[int] = []
            run: list[_Candidate] = []
            for i, c in enumerate(kept):
                if i in starts and run:
                    if median(run_spans) >= config.chunk_min_median_chars:
                        survivors.extend(run)
                    run, run_spans = [], []
                run.append(c)
                run_spans.append(spans[i])
            if run and median(run_spans) >= config.chunk_min_median_chars:
                survivors.extend(run)
            if not survivors:
                continue
            if median(_spans(survivors)) < config.chunk_min_median_chars:
                continue
            kept = survivors
        credible.append((profile, kept, toc, back))
    if not credible:
        return None
    runs = {c[0].name: _longest_run([k.num_key for k in c[1]]) for c in credible}
    numbered_run = max((runs[c[0].name] for c in credible if not c[0].article), default=0)
    # max() keeps the first of equals, so PROFILES order breaks a tie as before
    return max(
        credible,
        key=lambda c: (_leads(c[0], runs[c[0].name], numbered_run), len(c[1])),
    )


def _furniture(lines: list[str]) -> set[str]:
    """Running headers/footers: exact line text repeating 5+ times in one
    document is page furniture, never a section heading."""
    counts: dict[str, int] = {}
    for line in lines:
        s = line.strip()
        if s:
            counts[s] = counts.get(s, 0) + 1
    return {s for s, n in counts.items() if n >= 5}


def _extend_heading(
    lines: list[str], start_line: int, floor: int, profile: StyleProfile, furniture: set[str]
) -> int:
    """Walk backward over marginal notes and Part/Division/Chapter headings so
    they travel WITH the section they introduce."""
    j = start_line
    while j - 1 >= floor and start_line - (j - 1) <= HEADING_LOOKBACK:
        prev = lines[j - 1].strip()
        if not prev or len(prev) > 100 or prev in furniture:
            break
        if profile.pattern.match(lines[j - 1]) or DOT_LEADER_RE.search(prev):
            break
        if _CJK_RE.search(prev):
            # Chinese has no letter case, so "prev == prev.upper()" below is
            # true of every Chinese line and the walk would swallow the tail of
            # the previous article. Decide on the actual heading forms instead:
            # only a chapter or a section heading travels with its article.
            # A chapter is the top of the hierarchy, so the walk ends on it:
            # the line above is the previous chapter's text, or the contents
            # list, whose chapter lines would otherwise ride along too.
            if SUBSECTION_ZH_RE.match(prev):
                j -= 1
                continue
            if CHAPTER_ZH_RE.match(prev):
                j -= 1
            break
        structural = bool(
            PART_RE.match(prev)
            or SCHEDULE_RE.match(prev)
            or re.match(r"^(Chapter|Division|Subdivision|Section)\s+\S", prev)
            or prev == prev.upper()
        )
        if not structural and prev.endswith(_TRAILING_PUNCT) and not prev.endswith("etc."):
            break
        j -= 1
    return j


def _page_at(pages: Sequence[PageSpan], char_pos: int) -> int:
    """The page holding one character of the stream. The separator between two
    pages belongs to no page and reads as the page before it."""
    starts = [p.char_start for p in pages]
    return pages[max(0, bisect_right(starts, char_pos) - 1)].page_number


def _page_span(canonical: CanonicalText, char_start: int, char_end: int) -> tuple[int | None, int | None]:
    if not canonical.pages:
        return None, None
    first = _page_at(canonical.pages, char_start)
    last = _page_at(canonical.pages, max(char_start, char_end - 1))
    return first, last


def quote_page(
    pages: Sequence[PageSpan],
    piece_text: str,
    piece_char_start: int,
    quote: str,
    fallback: int | None,
) -> int | None:
    """The page a Verbatim Quote starts on: the page of its first visible
    character, placed by where the quote sits inside its Piece plus where the
    Piece sits in the Document's stored text. A quote that runs over a page
    break takes the page it starts on.

    The quote is a byte-for-byte slice of the Piece (the Map step's anchoring
    and the Prove step's check both guarantee it), and the Piece is a slice of
    the stream at piece_char_start, so its first occurrence in the Piece is the
    same position the audit view highlights.

    fallback is returned when the position cannot be determined: no page table
    (a caller that has none), an empty quote (a no-evidence record), or a quote
    that is not in this Piece text. Callers pass the Piece's first page, which
    is what every Mapping carried before this rule existed."""
    if not pages or not quote.strip():
        return fallback
    offset = piece_text.find(quote)
    if offset < 0:
        return fallback
    lead = len(quote) - len(quote.lstrip())
    return _page_at(pages, piece_char_start + offset + lead)


def split_document(
    canonical: CanonicalText,
    config: PipelineConfig | None = None,
    completion_fn: CompletionFn | None = None,
) -> tuple[list[Chunk], ChunkingReport]:
    """Chunk one document. Deterministic splitter first; if no style profile
    finds credible structure, the LLM boundary fallback fires (only when
    completion_fn is provided), else the document is split into numbered
    passages ("Passage N"), marked unstructured in the report."""
    config = config or PipelineConfig()
    report = ChunkingReport()
    total = len(canonical.full_text)
    lines, offsets = _line_offsets(canonical.full_text)

    selected = _select_profile(lines, offsets, total, config)
    if selected is not None:
        profile, kept, toc, back = selected
        report.style = profile.name
        boundaries = _deterministic_boundaries(canonical, lines, offsets, profile, kept, toc, back)
    else:
        report.fallback_used = True
        proposals: list[BoundaryProposal] = []
        if completion_fn is not None:
            proposals, report.fallback_attempts = _llm_boundaries_with_stats(
                canonical, config, completion_fn
            )
        else:
            report.notes.append("no completion_fn provided; skipped LLM fallback")
        if proposals:
            report.fallback_succeeded = True
            report.style = "llm_boundary_fallback"
            boundaries = [(p.char_start, p.section_label, p.chunk_kind) for p in proposals]
        else:
            boundaries = _passage_boundaries(canonical.full_text)
            report.style = "passages"
            report.unstructured = True
            report.notes.append(
                f"no credible structure; split into {len(boundaries)} numbered passages"
            )

    chunks: list[Chunk] = []
    for i, (start, label, kind) in enumerate(boundaries):
        end = boundaries[i + 1][0] if i + 1 < len(boundaries) else total
        if end <= start:
            continue
        page_start, page_end = _page_span(canonical, start, end)
        chunks.append(
            Chunk.from_stream(
                canonical,
                chunk_id=f"{canonical.document_id}:c{len(chunks):04d}",
                char_start=start,
                char_end=end,
                section_label=label,
                chunk_kind=kind,  # type: ignore[arg-type]
                page_start=page_start,
                page_end=page_end,
            )
        )
    report.n_chunks = len(chunks)
    report.n_sections = sum(1 for c in chunks if c.chunk_kind == "section")
    report.coverage_chars = sum(c.char_end - c.char_start for c in chunks)
    # A real exception, not assert: `python -O` strips asserts, and a partition
    # break here is silent data loss (text no chunk covers is never searched).
    if report.coverage_chars != total:
        raise RuntimeError(
            f"chunker coverage broke: chunks cover {report.coverage_chars} of"
            f" {total} chars, not a partition"
        )
    return chunks, report


# The passage size: the median section chunk across the stored Corpus is about
# 900 characters (AU and SG acts 1,100 to 1,200, Malaysian 570, India 530), so
# a passage of about 1,000 reads to the Gate and the Engine like a section.
PASSAGE_CHARS = 1000
# Sentence ends a passage prefers to close on when the text has no blank lines
# (Latin, Chinese and Devanagari stops; Thai and Lao mark none, so their
# passages close on a line end).
_SENTENCE_END = (".", "。", ";", "；", ":", "!", "?", "।")


def _passage_boundaries(text: str) -> list[tuple[int, str, str]]:
    """(char_start, "Passage N", "section") boundaries splitting a text with no
    credible structure into passages of about PASSAGE_CHARS characters.

    Each passage ends at the break nearest to its target length within half a
    passage either way, preferring, in order: a blank line (a paragraph), a
    line that ends a sentence, any line end, a space. Only a text with none of
    those (one unbroken run) is cut at the target itself. The last passage
    takes whatever remains when that is under one and a half passages."""
    total = len(text)
    starts: list[int] = []
    pos = 0
    while pos < total:
        starts.append(pos)
        if total - pos <= PASSAGE_CHARS * 3 // 2:
            break
        pos = _passage_end(text, pos)
    return [(start, f"Passage {i}", "section") for i, start in enumerate(starts, start=1)]


def _passage_end(text: str, start: int) -> int:
    target = start + PASSAGE_CHARS
    lo, hi = start + PASSAGE_CHARS // 2, start + PASSAGE_CHARS * 3 // 2
    window = text[lo:hi]

    def nearest(offsets: list[int]) -> int | None:
        return min(offsets, key=lambda o: abs(o - target)) if offsets else None

    line_ends = [lo + i + 1 for i, ch in enumerate(window) if ch == "\n"]
    for ends in (
        [e for e in line_ends if text.startswith("\n", e)],  # blank line follows
        [e for e in line_ends if text[:e - 1].rstrip().endswith(_SENTENCE_END)],
        line_ends,
        [lo + i + 1 for i, ch in enumerate(window) if ch == " "],
    ):
        best = nearest(ends)
        if best is not None:
            return best
    return target


def _deterministic_boundaries(
    canonical: CanonicalText,
    lines: list[str],
    offsets: list[int],
    profile: StyleProfile,
    kept: list[_Candidate],
    toc: tuple[int, int] | None,
    back: int | None,
) -> list[tuple[int, str, str]]:
    """(char_start, label, kind) boundaries covering the whole stream."""
    boundaries: list[tuple[int, str, str]] = []
    toc_end_line = toc[1] if toc else 0
    furniture = _furniture(lines)

    # Part context for labels: track outside the TOC region only. A Chapter
    # heading RESETS the Part: AU Chapter 4 has no Parts, and without the reset
    # its sections would inherit the last Part of Chapter 2 (caught by the
    # M4 PDF spot-verification, s. 71.8/72.31). The value carries its own
    # heading word, because a Chinese statute is divided into 章 (chapters),
    # not Parts, and its labels have to say so.
    # The Elucidation counts only below the first article: above it, the
    # heading shape can only be something else.
    elucidation = _elucidation_line(lines)
    if elucidation is not None and elucidation <= kept[0].line:
        elucidation = None
    part_at_line: dict[int, str] = {}
    current_part = None
    for i, line in enumerate(lines):
        if toc and toc[0] <= i < toc[1]:
            continue
        if elucidation is not None and i >= elucidation:
            part_at_line[i] = ELUCIDATION
            continue
        zh = CHAPTER_ZH_RE.match(line)
        if zh:
            number = zh_numeral_to_int(zh.group("num"))
            current_part = f"Chapter {number}" if number is not None else None
            part_at_line[i] = current_part
            continue
        if CHAPTER_RE.match(line):
            current_part = None
        m = PART_RE.match(line)
        if m:
            current_part = f"Part {m.group(1)}"
        part_at_line[i] = current_part

    first_section_start = _extend_heading(lines, kept[0].line, toc_end_line, profile, furniture)
    if toc:
        if offsets[toc[0]] > 0:
            boundaries.append((0, "front matter", "front_matter"))
        boundaries.append((offsets[toc[0]], "table of contents", "toc"))
        if toc[1] < first_section_start:
            boundaries.append((offsets[toc[1]], "front matter (pre-body)", "front_matter"))
    elif first_section_start > 0:
        boundaries.append((0, "front matter", "front_matter"))

    prev_line = toc_end_line
    for c in kept:
        floor = max(prev_line, toc_end_line)
        if elucidation is not None and c.line > elucidation:
            floor = max(floor, elucidation)  # the Elucidation heading opens its own chunk
        start_line = _extend_heading(lines, c.line, floor, profile, furniture)
        num = "".join(
            p for p in (
                str(c.num_key[0]),
                f"{c.num_key[3] if len(c.num_key) > 3 else '.'}{c.num_key[1]}" if c.num_key[1] >= 0 else "",
                c.num_key[2],
            ) if p
        )
        part = part_at_line.get(c.line)
        label = f"{part} s. {num}" if part else f"s. {num}"
        boundaries.append((offsets[start_line], label, "section"))
        prev_line = c.line
    if elucidation is not None:
        # The general part is policy prose that no article heads.
        boundaries.append((offsets[elucidation], "elucidation (general)", "other"))

    # Tail: trailing SCHEDULE blocks, then back-matter markers.
    tail_from = kept[-1].line + 1
    tail_to = back if back is not None else len(lines)
    for i in range(tail_from, tail_to):
        if SCHEDULE_RE.match(lines[i]):
            boundaries.append((offsets[i], lines[i].strip()[:80], "schedule"))
            break
    if back is not None:
        for i in range(back, len(lines)):
            if lines[i].strip() in BACK_MATTER_MARKERS:
                boundaries.append((offsets[i], lines[i].strip().lower(), "other"))

    boundaries.sort(key=lambda b: b[0])
    return boundaries


# ---------------------------------------------------------------------------
# quote-anchored section-label repair
#
# The chunk-level label has the chunk boundary's granularity: any heading the
# splitter fails to detect is invisible, and every quote under it inherits the
# chunk-opening section's number. Two shipped failure modes: (a) the num_title
# profile caps alpha suffixes at two letters, so s. 27KBA never becomes a
# boundary and its body is absorbed into s. 27KB; (b) in amendment acts,
# sections INSERTED inside "Add:" schedule blocks (43C, 43E) never become
# boundaries, so their quotes carry unrelated labels (35B, 27KT).
#
# Rather than widening the chunker itself (moving boundaries would invalidate
# every chunk-keyed checkpoint downstream), this index re-derives the label at
# the QUOTE's character position: nearest genuine heading line at or above the
# quote, page furniture and repeated running titles filtered, suffixes up to
# four letters allowed. Chunk boundaries and ids never move.
# ---------------------------------------------------------------------------

# The profile patterns with the alpha-suffix class widened to {0,4}. Order
# mirrors PROFILES; a document's dominant pattern is used exclusively so that
# stray matches of another style (numbered list items, quoted fragments)
# cannot become heading candidates. The Section/dot styles accept LOWERCASE
# suffixes too: MY prints inserted sections as "230a.", "230b.".
# The num_title style stays uppercase-only - lowercase there would
# turn ordinals ("20th Century...", "5th day") into heading candidates.
_NUM_TITLE_REPAIR_RE = re.compile(r"^(?P<num>\d{1,3}(?:\.\d{1,3})?[A-Z]{0,4})\s+[A-Z“\"]")
_REPAIR_PATTERNS = (
    re.compile(r"^\s{0,2}Section\s+(?P<num>\d{1,3}[A-Za-z]{0,4})\.\s+\S"),
    ARTICLE_WORD_RE,
    ARTICLE_ZH_RE,
    ARTICLE_NUM_FIRST_RE,
    ARTICLE_EN_RE,
    re.compile(r"^(?P<num>\d{1,3}[A-Za-z]{0,4})\.\s*$"),
    re.compile(r"^(?P<num>\d{1,3}[A-Za-z]{0,4})\.(?:[—–-]\(|\s+\S)"),
    _NUM_TITLE_REPAIR_RE,
)
# The article styles are unambiguous (no English act carries their words), so
# where one of them heads two or more lines it is the document's heading
# style however many numbered items the dot styles also find: in an
# Indonesian act those are the definitions of article 1, and taking them as
# headings labelled every later quote "s. 2".
_ARTICLE_REPAIR_PATTERNS = (ARTICLE_WORD_RE, ARTICLE_ZH_RE, ARTICLE_NUM_FIRST_RE, ARTICLE_EN_RE)
# The article styles whose heading lines are read through _article_word_heading.
_ARTICLE_TAIL_PATTERNS = (ARTICLE_WORD_RE, ARTICLE_NUM_FIRST_RE, ARTICLE_EN_RE)

_LABEL_NUM_RE = re.compile(r"\bs\.\s*(\S+)\s*$")


# Amendment-act schedule ITEM lines ("24 Section 36", "25 At the end of
# Part 3") are pattern-shaped but are never the section a quote belongs to:
# their "title" is an amending instruction, not a section name.
_AMEND_INSTRUCTION_TITLE_RE = re.compile(
    r"^(?:Sections?|Subsections?|Paragraphs?|Subparagraphs?|Divisions?|"
    r"Subdivisions?|Parts?|Schedules?|The whole|Title|"
    r"At the end|After |Before |In the appropriate)\b"
)


class SectionLabelIndex:
    """Nearest-genuine-heading lookup over one canonical stream.

    Candidates are lines of the document's DOMINANT heading pattern, minus
    page furniture (exact-line repeats, and repeated running titles like
    "81 Cybersecurity Act 2018 2020 Ed." whose page number varies), noise,
    and amendment-instruction item lines. label_at is deliberately
    fail-closed: when the nearest pattern-SHAPED line above the position is
    not itself a surviving candidate (a killed running title, a schedule item
    line, a repeated clause title in a parallel-structure act), the zone is
    ambiguous and it returns None rather than guessing - the caller keeps the
    original label. Repairs therefore only fire where the nearest heading is
    unambiguous (the 27KBA / inserted-43C classes)."""

    def __init__(self, full_text: str):
        lines, offsets = _line_offsets(full_text)
        self._offsets = offsets
        furniture = _furniture(lines)

        by_pattern: dict[int, list[tuple[int, str, str]]] = {}
        raw_by_pattern: dict[int, list[int]] = {}
        for i, line in enumerate(lines):
            for p_idx, pattern in enumerate(_REPAIR_PATTERNS):
                m = pattern.match(line)
                if not m:
                    continue
                # A cross-reference sentence or a page-foot catchword is
                # known prose, not an unreadable heading, so it does not make
                # its zone ambiguous either: it is no match at all.
                if pattern in _ARTICLE_TAIL_PATTERNS:
                    heading = _article_word_heading(line, m)
                    if heading is None:
                        break
                    num, title = heading
                else:
                    num, title = m.group("num"), _heading_title(line, m)
                if pattern is ARTICLE_ZH_RE:
                    number = zh_numeral_to_int(num)
                    if number is None:
                        break
                    num = str(number)
                raw_by_pattern.setdefault(p_idx, []).append(i)
                # The instruction filter applies to the num_title pattern
                # only: there the captured "title" is a real heading title
                # ("24 Section 36" is an amendment item, not a section). For
                # the dot styles the text after the number is the section
                # BODY, which legitimately opens with the same words ("3.
                # Subsection 6(1) of the principal Act is amended...").
                if (
                    line.strip() in furniture
                    or DOT_LEADER_RE.search(line)
                    or (
                        pattern is _NUM_TITLE_REPAIR_RE
                        and (
                            _AMEND_INSTRUCTION_TITLE_RE.match(title)
                            or _bare_title_is_noise(title)
                        )
                    )
                ):
                    break
                by_pattern.setdefault(p_idx, []).append((i, num, title))
                break
        if not by_pattern:
            self._cand_lines: list[int] = []
            self._cand_by_line: dict[int, str] = {}
            self._raw_lines: list[int] = []
            self._part_at_line: dict[int, str | None] = {}
            return
        # The English article word leads on the chunker's share rule (_leads):
        # a few dozen scheduled convention articles never outrank an act.
        runs = {k: _longest_run([_num_key(c[1]) for c in v]) for k, v in by_pattern.items()}
        numbered_run = max(
            (runs[k] for k in by_pattern if _REPAIR_PATTERNS[k] not in _ARTICLE_REPAIR_PATTERNS),
            default=0,
        )
        articles = [
            k for k in by_pattern
            if _REPAIR_PATTERNS[k] in _ARTICLE_REPAIR_PATTERNS and len(by_pattern[k]) >= 2
            and (
                _REPAIR_PATTERNS[k] is not ARTICLE_EN_RE
                or runs[k] * ENGLISH_ARTICLE_SHARE >= numbered_run
            )
        ]
        dominant = max(articles or by_pattern, key=lambda k: len(by_pattern[k]))
        cands = by_pattern[dominant]
        self._raw_lines = raw_by_pattern[dominant]

        # Running page titles repeat the same title under a different page
        # number on every page, so the exact-line furniture filter misses
        # them; kill by repeated title (rule of _kill_repeated_bare_titles).
        # This also kills genuinely repeated clause titles in parallel-
        # structure acts - the ambiguity rule in label_at turns those into
        # conservative keeps instead of wrong guesses.
        title_counts: dict[str, int] = {}
        for _, _, title in cands:
            if title:
                title_counts[title] = title_counts.get(title, 0) + 1
        cands = [c for c in cands if not c[2] or title_counts.get(c[2], 0) < 5]

        self._cand_lines = [c[0] for c in cands]
        self._cand_by_line = {c[0]: c[1] for c in cands}

        # The Elucidation heading is heading-shaped but names no article, so
        # the general part below it is an ambiguous zone, never the last
        # article of the body.
        elucidation = _elucidation_line(lines)
        if elucidation is not None:
            self._raw_lines = sorted({*self._raw_lines, elucidation})

        # Part context, chunker rules: a Chapter heading resets the Part, and
        # a Chinese chapter (第N章) is the context itself, spelled "Chapter N"
        # as the chunker spells it. Unlike the chunker, a Part match followed
        # by "of ..." is SKIPPED: that is a wrapped cross-reference sentence
        # ("Part IIIC of the Privacy Act 1988 (notification..."), not a Part
        # heading of THIS document (the DAT Act has no Part IIIC of its own).
        self._part_at_line = {}
        current_part = None
        for i, line in enumerate(lines):
            if elucidation is not None and i >= elucidation:
                self._part_at_line[i] = ELUCIDATION
                continue
            zh = CHAPTER_ZH_RE.match(line)
            if zh:
                number = zh_numeral_to_int(zh.group("num"))
                current_part = f"Chapter {number}" if number is not None else None
                self._part_at_line[i] = current_part
                continue
            if CHAPTER_RE.match(line):
                current_part = None
            m = PART_RE.match(line)
            if m and not line[m.end() :].lstrip().startswith("of "):
                current_part = f"Part {m.group(1)}"
            self._part_at_line[i] = current_part

    def label_at(self, pos: int, end: int | None = None) -> str | None:
        """The chunker-style label ("Part X s. N" / "s. N") of the nearest
        heading at or above pos; None when no heading precedes it or when the
        nearest pattern-shaped line is not a surviving candidate (ambiguous:
        the caller must keep its existing label).

        When end is given (the span of a quote), a candidate heading INSIDE
        the span wins: a quote that captures a marginal note above its own
        section heading starts a line or two before the heading, and the
        nearest-preceding scan would land one section early."""
        return self.label_at_with_pos(pos, end)[0]

    def label_at_with_pos(self, pos: int, end: int | None = None) -> tuple[str | None, int]:
        """label_at, plus the character offset of the heading line it chose
        (-1 when there is no label). Callers that know where the quote's own
        chunk sits feed the offset to quote_is_above_its_chunks_heading, which
        tells them whether that heading belongs to an earlier section the
        record was never taken from."""
        if not self._cand_lines:
            return None, -1
        if end is not None and end > pos:
            first_line = bisect_right(self._offsets, pos) - 1
            last_line = bisect_right(self._offsets, end - 1) - 1
            k = bisect_right(self._cand_lines, first_line - 1)
            if k < len(self._cand_lines) and self._cand_lines[k] <= last_line:
                return self._label_of(self._cand_lines[k]), self._offsets[self._cand_lines[k]]
        line = bisect_right(self._offsets, pos) - 1
        idx = bisect_right(self._cand_lines, line) - 1
        if idx < 0:
            return None, -1
        cand_line = self._cand_lines[idx]
        raw_idx = bisect_right(self._raw_lines, line) - 1
        if raw_idx >= 0 and self._raw_lines[raw_idx] != cand_line:
            return None, -1
        return self._label_of(cand_line), self._offsets[cand_line]

    def first_heading_in(self, start: int, end: int) -> int:
        """Offset of the first heading candidate inside [start, end), or -1
        when that span carries no heading of its own. A chunk with a heading of
        its own owns the text below it; a chunk without one is a continuation
        of the section that opened before it."""
        if not self._cand_lines:
            return -1
        first_line = bisect_right(self._offsets, start - 1)
        k = bisect_left(self._cand_lines, first_line)
        if k >= len(self._cand_lines):
            return -1
        offset = self._offsets[self._cand_lines[k]]
        return offset if start <= offset < end else -1

    def _label_of(self, cand_line: int) -> str:
        num = self._cand_by_line[cand_line]
        part = self._part_at_line.get(cand_line)
        return f"{part} s. {num}" if part else f"s. {num}"


def quote_is_above_its_chunks_heading(
    index: "SectionLabelIndex",
    chunk_span: tuple[int, int] | None,
    qpos: int,
    heading_pos: int,
) -> bool:
    """True when the quote sits in its chunk's PREAMBLE: the chunk owns a
    heading, the quote lies above it, and the label derived at the quote
    therefore comes from a section that ended before this chunk began.

    The export's pointer gate shares this predicate with the repair, so the
    label a row keeps and the label the gate judges it by cannot drift."""
    if chunk_span is None or heading_pos < 0:
        return False
    start, end = chunk_span
    if heading_pos >= start:
        return False
    own = index.first_heading_in(start, end)
    return own >= 0 and qpos < own


def repair_section_labels(
    records: list,
    full_text: str,
    chunk_texts: dict[str, str] | None = None,
) -> tuple[list, list[dict]]:
    """Re-derive each record's section label at its quote's position; the label
    changes ONLY when the derived section number differs from the current one
    (same number keeps the original label verbatim, part prefix included).
    Records whose quote cannot be located, whose position has no preceding
    heading, or whose current label carries no section number are left
    untouched. Returns (records, decisions); decisions is the audit trail:
    one dict per record with outcome kept | repaired | no_heading |
    not_found | unlabelled.

    A relabel never carries a record backwards into the section ABOVE its own
    chunk's heading. When the chunk owns a heading (one lies inside it) and the
    quote sits in the preamble above that heading, the nearest preceding
    heading belongs to an earlier section the record was never taken from: the
    chunk that opens a Part carries the Part line and a division line above its
    own "Section N." heading, and a quote from those lines would otherwise take
    the LAST section of the previous Part. Such a record keeps the chunk's own
    label and is recorded no_heading. A chunk with no heading of its own is a
    continuation of the section that opened before it, so the heading above it
    IS its section and the repair proceeds normally. Where the chunk text
    cannot be located, the offset is unknown and the repair behaves as it does
    with no chunk text at all."""
    index = SectionLabelIndex(full_text)
    chunk_offset: dict[str, int] = {}
    out, decisions = [], []
    for rec in records:
        decision = {
            "mapping_id": rec.mapping_id,
            "outcome": "kept",
            "old": rec.section,
            "new": rec.section,
        }
        # None: nothing is known about where this record's chunk sits, so the
        # preamble rule below cannot be applied.
        chunk_span: tuple[int, int] | None = None
        if chunk_texts and rec.chunk_id in chunk_texts:
            if rec.chunk_id not in chunk_offset:
                chunk_offset[rec.chunk_id] = full_text.find(chunk_texts[rec.chunk_id])
            start = chunk_offset[rec.chunk_id]
            if start >= 0:
                chunk_span = (start, start + len(chunk_texts[rec.chunk_id]))
        base = chunk_span[0] if chunk_span is not None else 0
        qpos = full_text.find(rec.verbatim_quote, base)
        if qpos < 0:
            qpos = full_text.find(rec.verbatim_quote)
        if qpos < 0:
            decision["outcome"] = "not_found"
            out.append(rec)
            decisions.append(decision)
            continue
        current_num = _LABEL_NUM_RE.search(rec.section or "")
        if current_num is None:
            decision["outcome"] = "unlabelled"
            out.append(rec)
            decisions.append(decision)
            continue
        derived, heading_pos = index.label_at_with_pos(
            qpos, end=qpos + len(rec.verbatim_quote)
        )
        if derived is None or quote_is_above_its_chunks_heading(
            index, chunk_span, qpos, heading_pos
        ):
            decision["outcome"] = "no_heading"
            out.append(rec)
            decisions.append(decision)
            continue
        derived_num = _LABEL_NUM_RE.search(derived)
        if derived_num and derived_num.group(1) != current_num.group(1):
            decision["outcome"] = "repaired"
            decision["new"] = derived
            out.append(rec.model_copy(update={"section": derived}))
        elif derived_num and derived != rec.section and derived.startswith(
            ("Part ", f"{ELUCIDATION} ")
        ):
            # Same section number but a different Part prefix, and the index
            # has POSITIVE part context at the quote (a real Part heading of
            # this document precedes it, or the quote sits in the Elucidation,
            # where "Pasal 3" is the note on article 3, not article 3). The model's prefix can bleed from
            # cross-references ("Part IIIC of the Privacy Act 1988" inside
            # the DAT Act); the document's own heading wins.
            # When the derived label carries no part, the original is kept:
            # absence of part context is no evidence the model's part is
            # wrong.
            decision["outcome"] = "repaired"
            decision["new"] = derived
            out.append(rec.model_copy(update={"section": derived}))
        else:
            out.append(rec)
        decisions.append(decision)
    return out, decisions


# ---------------------------------------------------------------------------
# LLM boundary fallback: line positions and labels ONLY
# ---------------------------------------------------------------------------

_FALLBACK_PROMPT = """You are labelling the structure of a legal/government document.
Below is the full document with 1-based line numbers in the form NNNN|text.

Return a JSON array of boundary objects, one per structural region, in reading
order. Each object: {{"start_line": <int>, "label": "<short human label>",
"kind": "<one of: section, front_matter, toc, schedule, other>"}}.

Rules:
- start_line values must be strictly increasing and within the document.
- The first object should normally have start_line 1.
- Do NOT copy document text into the output. Positions and labels only.
- Output ONLY the JSON array, nothing else.

DOCUMENT:
{doc}"""

_STRICT_SUFFIX = """

IMPORTANT: your previous answer was not a valid JSON array of
{{"start_line": int, "label": str, "kind": str}} objects. Output ONLY the raw
JSON array. No prose, no markdown fences, no extra keys."""


def _default_completion(engine: Engine) -> CompletionFn:
    """The production path for one Engine: the shared transport (temperature 0,
    transport retries pinned to 0, the missing-key preflight). The boundary
    answer is a JSON ARRAY, so this stage imposes no response schema; the parser
    below is the guard, and any text the model emits is discarded unread."""
    return make_completion(engine, None, mock_response=_LITELLM_MOCK_RESPONSE)


def _parse_boundary_json(content: str, n_lines: int) -> list[dict] | None:
    text = content.strip()
    fence = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    if not text.startswith("["):
        lo, hi = text.find("["), text.rfind("]")
        if lo == -1 or hi <= lo:
            return None
        text = text[lo : hi + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list) or not data:
        return None
    prev = 0
    out = []
    for item in data:
        if not isinstance(item, dict):
            return None
        start = item.get("start_line")
        label = item.get("label")
        kind = item.get("kind", "other")
        if not isinstance(start, int) or not (1 <= start <= n_lines) or start <= prev:
            return None
        if not isinstance(label, str) or not label.strip():
            return None
        if kind not in CHUNK_KINDS:
            return None
        prev = start
        # positions and labels ONLY: every other key (e.g. "text") dies here
        out.append({"start_line": start, "label": label.strip()[:120], "kind": kind})
    return out


def _llm_boundaries_with_stats(
    canonical: CanonicalText,
    config: PipelineConfig,
    completion_fn: CompletionFn,
) -> tuple[list[BoundaryProposal], int]:
    lines, offsets = _line_offsets(canonical.full_text)
    if len(lines) > config.chunk_fallback_max_lines:
        return [], 0
    numbered = "\n".join(f"{i + 1:04d}|{l[:200]}" for i, l in enumerate(lines))
    base = _FALLBACK_PROMPT.format(doc=numbered)
    attempts = 0
    for attempt in range(config.chunk_fallback_attempts):
        strict = attempt > 0
        attempts += 1
        try:
            content = completion_fn(base + (_STRICT_SUFFIX if strict else ""), strict)
        except Exception:
            continue
        parsed = _parse_boundary_json(content, len(lines))
        if parsed is None:
            continue
        if parsed[0]["start_line"] > 1:
            parsed.insert(0, {"start_line": 1, "label": "front matter", "kind": "front_matter"})
        total = len(canonical.full_text)
        proposals = []
        for i, item in enumerate(parsed):
            start = offsets[item["start_line"] - 1]
            end = offsets[parsed[i + 1]["start_line"] - 1] if i + 1 < len(parsed) else total
            if end <= start:
                continue
            proposals.append(
                BoundaryProposal(
                    char_start=start,
                    char_end=end,
                    section_label=item["label"],
                    chunk_kind=item["kind"],
                )
            )
        if proposals:
            return proposals, attempts
    return [], attempts


def llm_boundaries(
    canonical: CanonicalText,
    config: PipelineConfig | None = None,
    completion_fn: CompletionFn | None = None,
    engine: Engine | None = None,
    config_dir=CONFIG_DIR,
) -> list[BoundaryProposal]:
    """Run the boundary fallback directly. An explicit completion_fn wins; else
    the named Engine, else the configured default Engine."""
    if completion_fn is None:
        completion_fn = _default_completion(engine or resolve_engine(None, config_dir))
    proposals, _ = _llm_boundaries_with_stats(
        canonical, config or PipelineConfig(), completion_fn
    )
    return proposals
