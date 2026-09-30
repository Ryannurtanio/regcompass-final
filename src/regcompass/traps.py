"""The Indicator Reference's scoring traps, as flags on exported rows.

The organizers' Indicator Reference (A81 to A85) names rows that score zero
however well they are cited: a draft, a repealed provision, an amending act
cited in place of the principal act, a 7.3 row without a specified minimum
duration, a 6.1 row whose transfer is allowed on a condition (a 6.4 shape).
These are string tests on the law name and the Verbatim Quote, not legal
review, so they never drop a row: they write a "Check before submitting"
flag into Notes, where the reviewer sees it before the file is handed in.
The per-provision 7.1/7.2 rule is the export's own collapse, not a flag.
"""

from __future__ import annotations

import re

FLAG_PREFIX = "Check before submitting: "

_DRAFT_RE = re.compile(
    r"\bdraft\b|\bbill\b|\brancangan\b|\bRUU\b|草案|征求意见稿|送审稿", re.I
)
_REPEALED_NAME_RE = re.compile(r"\brepealed\b|\bdicabut\b|已废止|已失效", re.I)
_REPEALED_STATUS_RE = re.compile(r"\b(?:repeal|cancel|replac|revok)", re.I)
# "not repealed", "has not been repealed", "never revoked", "unrepealed" and
# "in force" say the opposite; they are taken out before the test.
_NOT_REPEALED_RE = re.compile(
    r"\b(?:not|never)\s+(?:been\s+|yet\s+)?(?:repealed|cancell?ed|replaced|revoked)\b"
    r"|\bun(?:repealed|revoked)\b|\bin\s+force\b",
    re.I,
)
# English names say "Amendment"; Indonesian ones "Perubahan"; Chinese ones
# 修正案 or 修改...的决定. The Indonesian Corpus names its two amending laws of
# the ITE Law (UU 11/2008) in the short "UU Nomor x Tahun y" form, so they are
# listed by name.
_AMENDING_RE = re.compile(r"\bamendment\b|\bamending\b|\bperubahan\b|修正案|修改.{0,40}的决定", re.I)
AMENDING_LAW_NAMES = frozenset({
    "UU Nomor 19 Tahun 2016",  # first amendment of UU 11/2008
    "UU Nomor 1 Tahun 2024",  # second amendment of UU 11/2008
})
# 7.3 needs a SPECIFIED minimum duration: a number next to a time unit, in
# English, Indonesian or Chinese. English numbers may be words, compounds
# included ("forty-five", "one hundred and eighty").
_EN_NUMBER = (
    r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen"
    r"|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty"
    r"|fifty|sixty|seventy|eighty|ninety|hundred)"
)
_DURATION_RE = re.compile(
    rf"(\b(\d+|{_EN_NUMBER}(?:[-\s]+(?:and[-\s]+)?{_EN_NUMBER})*)\s*(\(\w+\)\s*)?(calendar\s+)?"
    r"(years?|months?|weeks?|days?|hours?)\b)"
    r"|(\b(\d+|satu|dua|tiga|empat|lima|enam|tujuh|delapan|sembilan|sepuluh)"
    r"\s*(\(\w+\)\s*)?(tahun|bulan|minggu|hari|jam)\b)"
    r"|((?<![0-9])([一二三四五六七八九十百]+|[0-9]{1,4})(年|个月|日|天))",
    re.I,
)
# "must not transfer UNLESS [condition]" is a conditional flow regime (6.4),
# never a ban (6.1). "data subject to" is the data subject, not a condition.
_CONDITIONAL_RE = re.compile(
    r"\bunless\b|\bexcept\b|\bother\s+than\b|(?<!data )\bsubject\s+to\b"
    r"|\bwithout\s+the\s+(prior\s+)?(written\s+)?(consent|approval|authori[sz]ation)\b"
    r"|\bkecuali\b|非经|未经|除.{0,30}外",
    re.I,
)


def is_amending(law_name: str) -> bool:
    return bool(_AMENDING_RE.search(law_name or "")) or (law_name or "").strip() in AMENDING_LAW_NAMES


def trap_flags(row: dict, indicator_id: str) -> list[str]:
    """Every scoring trap one exported row may fall into, in plain words."""
    name = str(row.get("Law Name") or "")
    snippet = str(row.get("Verbatim Snippet") or "")
    flags: list[str] = []
    if _DRAFT_RE.search(name):
        flags.append("the law name reads as a draft or bill; drafts score zero, cite the enacted law")
    repeal = str(row.get("_repeal_status") or "")
    repealed = _REPEALED_NAME_RE.search(_NOT_REPEALED_RE.sub("", name)) or _REPEALED_STATUS_RE.search(
        _NOT_REPEALED_RE.sub("", repeal)
    )
    if repealed and "repealed-but-recorded" not in str(row.get("Notes") or ""):
        flags.append("the instrument is recorded as repealed; repealed provisions score zero")
    if is_amending(name):
        flags.append(
            "cited from an amending act; an amending act cited in place of the"
            " principal act scores zero, re-cite the principal act"
        )
    if str(row.get("Article / Section") or "").startswith("Elucidation"):
        flags.append("an Elucidation note, not the operative article")
    if indicator_id == "7.3" and not _DURATION_RE.search(snippet):
        flags.append("7.3 needs a specified minimum duration and the quote names none")
    if indicator_id == "6.1" and _CONDITIONAL_RE.search(snippet):
        flags.append("the quote allows transfer on a condition, a 6.4 shape rather than a 6.1 ban")
    return flags


def flag_traps(row: dict, indicator_id: str) -> dict:
    """Append the row's trap flags to its Notes, in place; a row that falls
    into none is left exactly as it was."""
    flags = trap_flags(row, indicator_id)
    if flags:
        notes = str(row.get("Notes") or "")
        flag = FLAG_PREFIX + "; ".join(flags)
        row["Notes"] = f"{notes}; {flag}" if notes else flag
    return row
