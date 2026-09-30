"""Plain wording for the Mapping Rationale.

The mapping prompt asks the Engine to name the "rung of the scoring ladder" a
provision matches, because naming it made the answers stable. A rung is the
Nth numbered line of the Indicator's RDTII scoring criteria
(config/indicators.json `criteria`), scored by `score_levels[N-1]`. The word
is ours, not the RDTII's, so wherever a person reads the rationale (the
Evidence detail, the Evidence Export) it is shown in the RDTII's own terms:
"rung 2" becomes "RDTII criterion 2 (score 0.5)". The stored text stays exactly
as the Engine wrote it.
"""

from __future__ import annotations

import re
from functools import lru_cache

from .config import CONFIG_DIR, load_indicators

# "rung 2", "rung (2)", "Rung 2", optionally followed by "of the scoring
# ladder", "on the indicator's ladder", "of the scoring ladder for indicator
# 6.4": the row already names its Indicator, so the tail says nothing more.
_LADDER_TAIL = (
    r"(?:\s+(?:of|on)\s+(?:the\s+)?(?:indicator's\s+)?(?:scoring\s+)?ladder"
    r"(?:\s+for\s+indicator\s+\d+(?:\.\d+)+)?)?"
)
_NUMBERED_RE = re.compile(
    r"(?P<prefix>\bRDTII\s+(?:\d+(?:\.\d+)+,?\s+)?)?\brung\s*(?:(?P<n>\d+)|\((?P<pn>\d+)\))" + _LADDER_TAIL,
    re.I,
)
_RUNG_OF_LADDER_RE = re.compile(
    r"\brung\s+(?:of|on)\s+(?:the\s+)?(?:indicator's\s+)?(?:scoring\s+)?ladder\b", re.I
)
_OWNED_LADDER_RE = re.compile(r"\b(RDTII\s+\d+(?:\.\d+)+'s)\s+(?:scoring\s+)?ladder\b", re.I)
_LADDER_RE = re.compile(r"\b(?:scoring\s+)?ladder\b", re.I)
_BARE_RUNG_RE = re.compile(r"\b(rungs?)\b", re.I)


@lru_cache(maxsize=4)
def _score_levels(config_dir: str) -> dict[str, list[str]]:
    return {k: list(v.score_levels) for k, v in load_indicators(config_dir).items()}


def _criterion(n: int, indicator_id: str, config_dir) -> str:
    levels = _score_levels(str(config_dir)).get(indicator_id, [])
    if 1 <= n <= len(levels):
        return f"criterion {n} (score {levels[n - 1]})"
    return f"criterion {n}"


def _same_case(word: str, replacement: str) -> str:
    return replacement[:1].upper() + replacement[1:] if word[:1].isupper() else replacement


def plain_rationale(text: str | None, indicator_id: str, config_dir=CONFIG_DIR) -> str | None:
    """The rationale in the RDTII's words: "rung N" names the criterion and its
    score for this row's Indicator, "scoring ladder" becomes "RDTII scoring
    criteria" and a bare "rung" becomes "criterion". Text without the jargon
    comes back unchanged."""
    if not text:
        return text

    def numbered(m: re.Match) -> str:
        n = int(m.group("n") or m.group("pn"))
        criterion = _criterion(n, indicator_id, config_dir)
        if m.group("prefix"):
            return m.group("prefix") + criterion
        return "RDTII " + criterion

    out = _NUMBERED_RE.sub(numbered, text)
    out = _RUNG_OF_LADDER_RE.sub("RDTII scoring criterion", out)
    out = _OWNED_LADDER_RE.sub(r"\1 scoring criteria", out)
    out = _LADDER_RE.sub("RDTII scoring criteria", out)
    out = _BARE_RUNG_RE.sub(
        lambda m: _same_case(m.group(1), "criteria" if m.group(1).lower() == "rungs" else "criterion"),
        out,
    )
    return out
