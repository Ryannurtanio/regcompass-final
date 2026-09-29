"""M6 - map one gated (chunk, indicator) pair to a MappingRecord.

The LLM only SELECTS: it returns a quote it claims is present in the provision,
a subsection reference, and a one-line impact. The code then ANCHORS the quote
to the chunk text: an exact byte-for-byte substring passes through; a quote
that differs only in whitespace is re-anchored to the exact chunk slice (the
shipped quote is always a slice of the canonical stream); anything else, a
paraphrase included, is malformed output. Malformed output of any kind counts
one extraction attempt and triggers a stricter retry; after
config.extraction_attempts total attempts the pair is dropped with its failure
log, never shipped.

Every Engine gets the SAME prompt and the SAME strict JSON schema; the swap is
pure config (models.yaml), so a Comparison between two Engines compares the
models, not two different prompts. Transport retries are pinned to 0 everywhere:
silent SDK retries corrupt attempt counting.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from .chunk import quote_page
from .config import CONFIG_DIR, load_indicators
from .contracts import (
    EconomyCode,
    Engine,
    GatedChunk,
    IndicatorDef,
    MappingRecord,
    PageSpan,
    PipelineConfig,
)
from .engines import (
    CompletionFn,
    ConfigError,
    completion_kwargs,
    make_completion,
    resolve_engine,
)

# ConfigError and CompletionFn live in engines.py now (one Engine, one place);
# they are imported here because the pipeline, the CLI, the gloss lane and the
# other stages import them from map.

MIN_QUOTE_CHARS = 15  # a shorter "quote" cannot span verb + object + condition
MAX_QUOTE_CHARS = 1500  # a longer one is the model dumping the section, not selecting

# Test hook: set to a canned string to exercise the production litellm call
# path (kwargs, schema, key handling) offline via litellm's mock_response.
_LITELLM_MOCK_RESPONSE: str | None = None


class MapperSelection(BaseModel):
    """The ONLY thing the model may return. extra='forbid' so any invented key
    (a self-reported confidence, say) is malformed output, not data."""

    model_config = ConfigDict(extra="forbid")

    maps_to_indicator: bool
    verbatim_quote: str = ""
    subsection: str | None = None
    impact: str | None = None

    @model_validator(mode="after")
    def _quote_rules(self) -> "MapperSelection":
        if self.maps_to_indicator:
            n = len(self.verbatim_quote.strip())
            if n < MIN_QUOTE_CHARS:
                raise ValueError(f"quote too short ({n} chars) for a substantive mapping")
            if n > MAX_QUOTE_CHARS:
                raise ValueError(f"quote too long ({n} chars): select, do not dump")
        return self


@dataclass
class MapOutcome:
    """What happened to one (chunk, indicator) pair."""

    record: MappingRecord | None
    outcome: Literal["mapped", "no_evidence", "dropped"]
    attempts: int
    failures: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# quote anchoring
# ---------------------------------------------------------------------------


def anchor_quote(chunk_text: str, quote: str) -> str | None:
    """Anchor a model-returned quote to the chunk text, byte-for-byte.

    Exact substring: returned as-is. Whitespace-only drift (the model reflowed
    a line break): re-anchored to the exact chunk slice, provided the match is
    UNIQUE. Every other difference (a changed word, a changed character, a
    paraphrase) fails. The returned string is always a slice of chunk_text."""
    tokens = quote.split()
    if not tokens:
        return None
    if quote in chunk_text:
        return quote
    pattern = r"\s+".join(re.escape(t) for t in tokens)
    matches = list(re.finditer(pattern, chunk_text))
    if len(matches) != 1:
        return None
    return matches[0].group(0)


# ---------------------------------------------------------------------------
# prompt
# ---------------------------------------------------------------------------

_ANATOMY_RULES = """How to read a statute:
- Obligations live in operative verbs: must, shall, may, should, can, and their
  negatives (must not, shall not). Evidence is a provision that imposes,
  conditions, or removes an obligation relevant to the indicator.
- Preambles, purpose clauses, short-title and commencement provisions state
  intent, not obligations: NEVER map them.
- Enforcement, penalty and appeal provisions are not substantive obligations,
  UNLESS the indicator itself concerns government powers over data (then powers
  of production, inspection, search or seizure are exactly the evidence).
- Definitions that use "includes" are non-exhaustive; watch Schedules and
  exemptions for carve-outs that change an obligation's scope."""

# The impact sentence names the rung it matches. That is not decoration: naming
# the rung inside the one free-text field the schema allows made Luna's answer
# STABLE (16 Sep 2026: 8 of 10 EVAL_SET passes above the bar without it, 10 of
# 10 with it), because the decision now has to be stated, not just taken. The
# 200-character bound is the export's: M9 truncates the rationale column at 300
# characters, and unbounded "state the rung" answers ran to 387.
_TASK_RULES = """Task: decide whether THIS provision contains concrete textual evidence for the
indicator above (evidence that the measure exists, or that transfers/processing
are expressly conditioned or restricted as the indicator describes).
- If yes: copy ONE quote of roughly 10 to 80 words from the provision text,
  EXACTLY character for character: keep the original line breaks, punctuation
  and spelling; never paraphrase, never normalize whitespace. The quote should
  span the operative verb, its object, and any condition.
- subsection: the smallest subsection reference containing the quote, exactly
  as written in the provision, e.g. "(1)" or "(2)(a)"; null if none.
- impact: ONE short sentence, at most 200 characters, stating what the
  provision requires or permits, for whom, and which rung of the scoring ladder
  above it matches. State the rung; do not argue for it.

Answer with ONLY a JSON object, no prose, no markdown fences:
{"maps_to_indicator": true, "verbatim_quote": "...", "subsection": "(1)", "impact": "..."}
or, if the provision is not evidence for this indicator:
{"maps_to_indicator": false, "verbatim_quote": "", "subsection": null, "impact": null}"""

# The Indicator's own scoring ladder, inlined for the Indicator being judged.
# Measured 16 Sep 2026 on the EVAL_SET: without it Engine A (Luna) scored 0.73
# and mapped nothing at all on Singapore Pillar 6, because it read the bare
# definition as an all-or-nothing test and declined every partial match. The
# ladder's half-score rungs ARE the partial matches, so the note spells that
# out; the text itself is the registry's, never a paraphrase.
_PARTIAL_EVIDENCE_NOTE = """Note on partial evidence: the provision does NOT have to satisfy the indicator
completely. A provision that satisfies the indicator's criteria EVEN PARTIALLY
(one sector, one class of data, one condition among several, a partial
restriction) is evidence and must be mapped: those are exactly the
half-score rungs of the ladder above. Only answer false when the provision
carries no obligation, permission or restriction of the kind the indicator
describes at all."""

# Indicators cover neighbouring ground on purpose (6.1 bans, 6.4 conditions).
# Luna refused otherwise-correct citations on the ground that a neighbour owned
# the provision, which is the SCORER's judgement, not the selector's: the
# reviewer and the Coverage Matrix settle the boundary downstream. Generic on
# purpose: it names no Indicator, so it cannot teach a pair of them apart.
_OVERLAP_RULE = """One more rule, about neighbouring indicators: you are SELECTING a citation, not
assigning the economy's score. Indicators cover neighbouring ground, and a later
stage weighs the citations and settles which indicator and which rung a measure
finally scores on. So a single provision may legitimately be cited under more
than one indicator. Never answer false on the ground that a NEIGHBOURING
indicator looks like the better fit for this provision; answer false only when
the provision carries no obligation, permission or restriction that bears on
this indicator at all.
Do not require the provision to repeat the indicator's own words. A general
obligation that governs the whole class of activity the indicator names (a duty
on the handling of data, where the indicator asks about conditions on moving it)
is evidence even when the provision does not spell out the indicator's exact
case. What must be present is the obligation, permission or restriction itself,
not the indicator's vocabulary.
Check the provision against the rungs of the ladder above before you answer.
The obligation you found must be of the KIND the rungs score: a ban or a local
processing requirement where the rungs speak of bans, a condition where they
speak of conditions, a retention period where they speak of retention. A
provision whose obligation matches NO rung of this indicator's ladder is not
evidence for this indicator, however clearly it bears on data."""

_STRICT_SUFFIX = """

IMPORTANT: your previous answer was rejected. Output ONLY the raw JSON object
with exactly the keys maps_to_indicator, verbatim_quote, subsection, impact.
verbatim_quote must be copied character-for-character from the provision text
above (identical bytes, including line breaks) or "" if maps_to_indicator is
false. No other keys, no prose, no markdown fences."""


def _criteria_block(ind_def: IndicatorDef) -> str:
    """This Indicator's scoring ladder and exception, verbatim from the registry,
    with the partial-evidence note underneath it. An Indicator that declares no
    criteria or no exception contributes an empty line rather than a different
    shape: the block's layout is what the measured prompt had."""
    return (
        "SCORING CRITERIA for this indicator (the ESCAP scoring ladder):\n"
        f"{ind_def.criteria or ''}\n{ind_def.exception or ''}\n"
        f"{_PARTIAL_EVIDENCE_NOTE}"
    )


def build_prompt(
    gated: GatedChunk, economy: str, ind_def: IndicatorDef, strict: bool, hint: str | None = None
) -> str:
    """One prompt for every Engine: the swap changes config, not words.
    hint carries M7's mechanical-check feedback into the stricter retry."""
    c = gated.chunk
    prompt = (
        "You are mapping national legislation to one indicator of the UN ESCAP "
        "RDTII 2.1 framework (Regional Digital Trade Integration Index).\n\n"
        f"INDICATOR {gated.indicator_id} - {ind_def.name}\n"
        f"Definition: {ind_def.definition}\n\n"
        f"{_criteria_block(ind_def)}\n\n\n"
        f"{_OVERLAP_RULE}\n\n\n"
        f"PROVISION ({c.section_label}; document {c.document_id}; economy {economy}):\n"
        f"<<<PROVISION\n{c.text}\nPROVISION>>>\n\n"
        f"{_ANATOMY_RULES}\n\n{_TASK_RULES}"
    )
    if strict:
        prompt += _STRICT_SUFFIX
    if hint:
        prompt += f"\n\nMECHANICAL CHECK FEEDBACK: {hint}"
    return prompt


# ---------------------------------------------------------------------------
# model transport (LiteLLM, every Engine, retries pinned 0)
# ---------------------------------------------------------------------------


def _response_format() -> dict:
    """The strict JSON schema every Engine enforces. Hand-written (not derived
    from the Pydantic model) so it stays a stable closed shape: all four keys
    required, additionalProperties false, nullable via type unions."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "mapper_selection",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "maps_to_indicator": {"type": "boolean"},
                    "verbatim_quote": {"type": "string"},
                    "subsection": {"type": ["string", "null"]},
                    "impact": {"type": ["string", "null"]},
                },
                "required": ["maps_to_indicator", "verbatim_quote", "subsection", "impact"],
                "additionalProperties": False,
            },
        },
    }


def _completion_kwargs(engine: Engine, response_format: dict | None = None) -> dict:
    """M6's view of the shared transport contract (engines.completion_kwargs):
    everything but the messages, with the mapper selection schema as the default
    response_format. Other stages (M8 reconcile, M12 classify) pass their own
    strict schema through the same transport."""
    return completion_kwargs(
        engine, response_format, default_response_format=_response_format()
    )


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def _parse_selection(content: str) -> MapperSelection | str:
    """A MapperSelection, or a short failure reason (malformed output)."""
    text = content.strip()
    fence = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    if not text.startswith("{"):
        lo, hi = text.find("{"), text.rfind("}")
        if lo == -1 or hi <= lo:
            return "no JSON object in output"
        text = text[lo : hi + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return f"invalid JSON: {e.msg}"
    if not isinstance(data, dict):
        return "output is not a JSON object"
    try:
        return MapperSelection.model_validate(data)
    except ValidationError as e:
        return f"schema violation: {e.errors()[0].get('msg', 'invalid')}"


# ---------------------------------------------------------------------------
# the mapper
# ---------------------------------------------------------------------------


def map_gated_chunk(
    gated: GatedChunk,
    economy: EconomyCode,
    config: PipelineConfig | None = None,
    engine: Engine | None = None,
    completion_fn: CompletionFn | None = None,
    indicator_defs: dict[str, IndicatorDef] | None = None,
    config_dir=CONFIG_DIR,
    start_attempt: int = 1,
    hint: str | None = None,
    pages: Sequence[PageSpan] | None = None,
) -> MapOutcome:
    """Map one passed (chunk, indicator) pair. Never raises on model behaviour:
    a pair that cannot be mapped within the attempt budget is a logged DROP.

    pages is the Document's page table (CanonicalText.pages). Given, the record
    cites the page its quote starts on; without it (or for a quote that cannot
    be placed) the record cites the Piece's first page (chunk.quote_page).

    start_attempt/hint serve M7's retry escalation: the verify stage resumes
    the SAME attempt budget where M6 left off, with mechanical feedback."""
    if gated.gate_decision != "passed":
        raise ValueError(
            f"refusing to map an excluded pair: {gated.chunk.chunk_id} x {gated.indicator_id}"
        )
    config = config or PipelineConfig()
    defs = indicator_defs or load_indicators(config_dir)
    ind_def = defs[gated.indicator_id]
    if completion_fn is None:
        eng = engine or resolve_engine(None, config_dir)
        completion_fn = make_completion(
            eng, _response_format(), mock_response=_LITELLM_MOCK_RESPONSE
        )

    failures: list[str] = []
    for attempt in range(start_attempt, config.extraction_attempts + 1):
        content = completion_fn(
            build_prompt(gated, economy, ind_def, strict=attempt > 1, hint=hint), attempt > 1
        )
        sel = _parse_selection(content)
        if isinstance(sel, str):
            failures.append(f"attempt {attempt}: {sel}")
            continue
        quote = ""
        if sel.maps_to_indicator:
            anchored = anchor_quote(gated.chunk.text, sel.verbatim_quote)
            if anchored is None:
                failures.append(
                    f"attempt {attempt}: quote failed to anchor byte-for-byte "
                    f"(paraphrase or mangled): {sel.verbatim_quote[:80]!r}"
                )
                continue
            quote = anchored
        record = MappingRecord(
            mapping_id=f"{gated.chunk.chunk_id}::{gated.indicator_id}",
            document_id=gated.chunk.document_id,
            chunk_id=gated.chunk.chunk_id,
            economy=economy,
            indicator_id=gated.indicator_id,
            indicator_name=ind_def.name,
            section=gated.chunk.section_label,
            subsection=sel.subsection if sel.maps_to_indicator else None,
            verbatim_quote=quote,
            page_number=quote_page(
                pages or (), gated.chunk.text, gated.chunk.char_start, quote,
                gated.chunk.page_start,
            ),
            impact=sel.impact,
            insufficient_evidence=not sel.maps_to_indicator,
            extraction_attempts=attempt,
        )
        return MapOutcome(
            record=record,
            outcome="mapped" if sel.maps_to_indicator else "no_evidence",
            attempts=attempt,
            failures=failures,
        )
    return MapOutcome(
        record=None, outcome="dropped", attempts=config.extraction_attempts, failures=failures
    )
