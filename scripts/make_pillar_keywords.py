"""Generate config/pillar_N_keywords.json for the ten pillars that have none.
LOCAL DEV ONLY, like scripts/make_indicators.py. Run it with:

    uv run python scripts/make_pillar_keywords.py

The Gate's lexical tier needs a phrase vocabulary per indicator and its meaning
tier needs one description per pillar. No organizer file supplies either, and
pillars 6 and 7 got theirs by hand in Round 1. This script derives a first pass
for pillars 1 to 5 and 8 to 12 MECHANICALLY from the indicator's own text in
config/indicators.json (name, scoring criteria, exception note). There is no
model call, paid or local.

How a phrase is chosen: lowercase the text, split it into runs of consecutive
tokens that are neither English stopwords (the same bm25s list gate.py's
tokenizer drops) nor numbering debris, take every 1-to-4 token window of each
run, and rank the windows by how often they occur, then by length, then by
where they first appear. Every multi-word span the text puts in quotation marks
is kept as well. The result is thin by construction, which is why it is written
with _reviewed: false: it is a starting point for a human pass, and the
meaning-based Gate tier carries the weight until then.

PILLARS 6 AND 7 ARE NEVER WRITTEN. Their files are hand-curated and the Round 1
golden evidence depends on them; a test asserts this script leaves them byte
for byte alone.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CURATED_PILLARS = (6, 7)
PHRASES_PER_INDICATOR = 14
MIN_PHRASES = 5
MAX_PHRASE_TOKENS = 4
DESCRIPTION_TARGET_CHARS = 300

# Numbering debris from the criteria text ("1)", "2)") and bare figures carry no
# lexical signal; the gate tokenizer would drop them anyway.
_TOKEN = re.compile(r"[a-z][a-z0-9-]+")
_QUOTED = re.compile(r"[\"“‘']([^\"”’']{4,60})[\"”’']")

# Words that are not English stopwords but are pure scaffolding in this corpus:
# they describe the scoring machinery, not the policy the Gate is looking for.
_SCAFFOLD = frozenset(
    {
        "any", "case", "cases", "e.g", "eg", "etc", "exception", "for", "measure",
        "measures", "no", "not", "other", "others", "score", "scored", "scoring",
    }
)


def _stopwords() -> frozenset:
    from bm25s.stopwords import STOPWORDS_EN

    return frozenset(STOPWORDS_EN) | _SCAFFOLD


def _runs(text: str, stopwords: frozenset) -> list[list[str]]:
    """Maximal runs of consecutive content tokens. Splitting on stopwords and
    punctuation keeps a phrase from straddling a clause boundary."""
    runs: list[list[str]] = []
    for segment in re.split(r"[\n.;:,()/]+", text.lower()):
        current: list[str] = []
        for raw in segment.split():
            token = _TOKEN.match(raw.strip("'\"“”‘’"))
            word = token.group(0) if token else None
            if word is None or word in stopwords or len(word) < 3:
                if current:
                    runs.append(current)
                    current = []
                continue
            current.append(word)
        if current:
            runs.append(current)
    return runs


def phrases_for(indicator: dict, stopwords: frozenset) -> list[str]:
    source = "\n".join(
        part
        for part in (indicator["name"], indicator.get("criteria"), indicator.get("exception"))
        if part
    )
    counts: Counter[str] = Counter()
    first_seen: dict[str, int] = {}
    position = 0
    for run in _runs(source, stopwords):
        for size in range(1, MAX_PHRASE_TOKENS + 1):
            for start in range(0, len(run) - size + 1):
                phrase = " ".join(run[start : start + size])
                counts[phrase] += 1
                first_seen.setdefault(phrase, position)
                position += 1
    ranked = sorted(
        counts,
        key=lambda p: (-counts[p], -p.count(" "), first_seen[p], p),
    )
    chosen = ranked[:PHRASES_PER_INDICATOR]

    for quoted in _QUOTED.findall(source):
        phrase = " ".join(
            w for w in re.findall(_TOKEN, quoted.lower()) if w not in stopwords
        )
        if " " in phrase and phrase not in chosen:
            chosen.append(phrase)

    if len(chosen) < MIN_PHRASES:
        raise ValueError(
            f"indicator {indicator['name']!r} yielded only {len(chosen)} phrases;"
            f" the mechanical rule needs at least {MIN_PHRASES}"
        )
    return chosen


def description_for(pillar_name: str, indicators: list[dict]) -> str:
    """Pillar name plus its policy issues, trimmed at a phrase boundary to the
    ~300-character scale of the hand-written pillar 6 and 7 descriptions."""
    issues = [" ".join(i["name"].split()).rstrip(".") for i in indicators]
    text = f"{pillar_name}: "
    for i, issue in enumerate(issues):
        candidate = text + ("; " if i and not text.endswith(" ") else "") + issue
        if len(candidate) > DESCRIPTION_TARGET_CHARS and i:
            return text.rstrip("; ") + "."
        text = candidate
    return text.rstrip("; ") + "."


def build(pillar: int, pillar_name: str, indicators: dict[str, dict], stopwords) -> dict:
    ordered = [i for i in indicators.values()]
    out: dict = {
        "_comment": (
            f"Pillar {pillar} '{pillar_name}' per-indicator vocabulary, DERIVED"
            " mechanically by scripts/make_pillar_keywords.py from the indicator"
            " name, scoring criteria and exception note in config/indicators.json."
            " No model wrote any of it. A human review pass flips _reviewed."
        ),
        "_pillar_description": description_for(pillar_name, ordered),
        "_derived": True,
        "_source": "config/indicators.json (name + criteria + exception)",
        "_reviewed": False,
    }
    for indicator_id, entry in indicators.items():
        out[indicator_id] = phrases_for(entry, stopwords)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config-dir", type=Path, default=ROOT / "config")
    args = ap.parse_args()

    indicators = {
        k: v
        for k, v in json.loads(
            (args.config_dir / "indicators.json").read_text(encoding="utf-8")
        ).items()
        if not k.startswith("_")
    }
    pillars = {
        k: v
        for k, v in json.loads(
            (args.config_dir / "pillars.json").read_text(encoding="utf-8")
        ).items()
        if not k.startswith("_")
    }
    stopwords = _stopwords()

    for key, pillar_def in pillars.items():
        if not key.isdigit():
            continue  # default_pillars and other metadata keys
        pillar = int(key)
        if pillar in CURATED_PILLARS:
            print(f"pillar {pillar}: hand-curated, left untouched")
            continue
        mine = {i: indicators[i] for i in pillar_def["indicator_ids"]}
        payload = build(pillar, pillar_def["name"], mine, stopwords)
        path = args.config_dir / f"pillar_{pillar}_keywords.json"
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"pillar {pillar}: {len(mine)} indicators -> {path.name}")


if __name__ == "__main__":
    main()
