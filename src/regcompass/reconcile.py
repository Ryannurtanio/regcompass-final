"""M8 - reconcile verified records into (economy, indicator) groups.

The LLM PROPOSES relationships and a controlling record; the legal-hierarchy
ladder is enforced in CODE and always wins: Acts/Ordinances (0) > Regulations/
Rules/Orders (1) > Notices/Guidelines/Directions (2) > Codes of Practice/
Advisories (3) > unknown (4). Recency (last_amended) breaks ties only WITHIN a
rank: an older Act outranks a newer Notice. Losers stay recorded with
controlling_evidence False, never dropped (ESCAP records every instrument and
scores by one controlling source).

FIT-FILTERED LADDER: when M12 classifications are
provided, controlling selection first filters the group to members whose
classification actually EVIDENCES the indicator (record_contribution is not
None: right measure nature, confirmed, not government-data-excluded), and the
hierarchy ladder then breaks ties WITHIN that subset. Legal hierarchy alone
kept promoting provisions that do not evidence their indicator (a warrant-
consent sentence controlling a localization cell) while the on-point
provision sat demoted in the same group. When NO member fits, the ladder runs
over all members and the group is flagged no-fit-controlling instead of
silently promoting a misfit.

Single-record groups skip the LLM entirely (sole_source, authoritative).
Malformed or incoherent proposals retry with a stricter prompt (same
extraction_attempts budget shape as M6/M7) and then fall back to the pure
deterministic ladder: a group ALWAYS reconciles. Members the LLM marks
conflicting get the contradictory-sources uncertainty flag for the human
reviewer; the conflict is never auto-resolved away.
"""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from .classify import ProvisionClassification, boundary_excluded_61, record_contribution
from .config import CONFIG_DIR
from .contracts import (
    Engine,
    MappingRecord,
    PipelineConfig,
    ReconciledGroup,
    RelationshipToGroup,
)
from .engines import CompletionFn, make_completion, resolve_engine
from .observability import log_stage
from .storage import Storage

# Relationship choices INSIDE a multi-record group (sole_source is reserved for
# single-record groups and is incoherent anywhere else).
_MULTI_RELATIONSHIPS = ("complementary", "superseded_by", "supersedes", "conflicting")

# The ladder, most subordinate first so "Code of Practice" wins over the \bact\b
# hiding inside other words, and specific kinds are matched before generic ones.
_LADDER: tuple[tuple[int, str], ...] = (
    (3, r"\bcode of practice\b|\badvisor(y|ies)\b"),
    (2, r"\bnotice\b|\bguidelines?\b|\bdirections?\b|\bcirculars?\b"),
    (1, r"\bregulations?\b|\brules\b|\borders?\b|\bs\.?r\.?o\b"),
    (0, r"\bact\b|\bordinance\b"),
)
_UNKNOWN_RANK = 4

# Test hook mirroring map.py: canned string exercises the litellm path offline.
_LITELLM_MOCK_RESPONSE: str | None = None


def instrument_rank(law_name: str) -> int:
    """Legal-hierarchy rank of an instrument from its name; lower = stronger."""
    low = law_name.lower()
    for rank, pattern in _LADDER:
        if re.search(pattern, low):
            return rank
    return _UNKNOWN_RANK


def _recency_key(record: MappingRecord) -> int:
    """First 4-digit year in last_amended; unknown counts as oldest."""
    m = re.search(r"\d{4}", record.timeline.last_amended or "")
    return int(m.group(0)) if m else -1


class _RelEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mapping_id: str
    relationship: Literal["complementary", "superseded_by", "supersedes", "conflicting"]


class ReconcileProposal(BaseModel):
    """The ONLY thing the model may return for a group."""

    model_config = ConfigDict(extra="forbid")

    authoritative_mapping_id: str
    relationships: list[_RelEntry]
    notes: str = ""


def _response_format() -> dict:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "reconcile_proposal",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "authoritative_mapping_id": {"type": "string"},
                    "relationships": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "mapping_id": {"type": "string"},
                                "relationship": {
                                    "type": "string",
                                    "enum": list(_MULTI_RELATIONSHIPS),
                                },
                            },
                            "required": ["mapping_id", "relationship"],
                            "additionalProperties": False,
                        },
                    },
                    "notes": {"type": "string"},
                },
                "required": ["authoritative_mapping_id", "relationships", "notes"],
                "additionalProperties": False,
            },
        },
    }


_STRICT_SUFFIX = """

IMPORTANT: your previous answer was rejected. Output ONLY the raw JSON object
with exactly the keys authoritative_mapping_id, relationships, notes. Every
member of the group must appear EXACTLY ONCE in relationships, using only the
allowed relationship values; authoritative_mapping_id must be one of the
member mapping_ids and must not be the superseded one. No prose, no fences."""


def build_group_prompt(
    economy: str,
    indicator_id: str,
    members: list[tuple[MappingRecord, str]],
    strict: bool,
    fit_ids: set[str] | None = None,
) -> str:
    lines = []
    for rec, law_name in members:
        lines.append(
            f"- mapping_id: {rec.mapping_id}\n"
            f"  instrument: {law_name}\n"
            f"  provision: {rec.section}"
            + (f" {rec.subsection}" if rec.subsection else "")
            + (f"\n  last_amended: {rec.timeline.last_amended}" if rec.timeline.last_amended else "")
            + f"\n  quote: {rec.verbatim_quote[:240]!r}"
            + (f"\n  impact: {rec.impact}" if rec.impact else "")
        )
    fit_clause = ""
    if fit_ids is not None:
        fit_clause = (
            "- The controlling record MUST be one of these members, whose "
            "classification evidences this indicator (the others stay recorded "
            "but cannot control): " + ", ".join(sorted(fit_ids)) + "\n"
        )
    prompt = (
        "Several verified provisions from the same economy all map to one UN ESCAP "
        f"RDTII 2.1 indicator. Economy: {economy}. Indicator: {indicator_id}.\n\n"
        "GROUP MEMBERS:\n" + "\n".join(lines) + "\n\n"
        "Task: classify how each member relates to the group and pick the single "
        "CONTROLLING record (the one the indicator should be scored from).\n"
        "- relationship values: complementary (contributes alongside the others), "
        "superseded_by (an amendment or later instrument replaces it), supersedes "
        "(it replaces an older member), conflicting (it contradicts another member).\n"
        + fit_clause +
        "- The controlling record should come from the instrument highest in the "
        "legal hierarchy (Acts > Regulations > Notices/Guidelines > Codes/"
        "Advisories); prefer the most recent only within the same level.\n"
        "- notes: one or two sentences explaining the choice.\n\n"
        "Answer with ONLY a JSON object, no prose, no markdown fences:\n"
        '{"authoritative_mapping_id": "...", "relationships": '
        '[{"mapping_id": "...", "relationship": "..."}], "notes": "..."}'
    )
    if strict:
        prompt += _STRICT_SUFFIX
    return prompt


def _parse_proposal(content: str, member_ids: set[str]) -> ReconcileProposal | str:
    """A coherent ReconcileProposal, or a short failure reason."""
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
    try:
        prop = ReconcileProposal.model_validate(data)
    except ValidationError as e:
        return f"schema violation: {e.errors()[0].get('msg', 'invalid')}"
    seen = [e.mapping_id for e in prop.relationships]
    if sorted(seen) != sorted(member_ids):
        return "relationships must cover every group member exactly once"
    if prop.authoritative_mapping_id not in member_ids:
        return "authoritative_mapping_id is not a group member"
    rel_by_id = {e.mapping_id: e.relationship for e in prop.relationships}
    if rel_by_id[prop.authoritative_mapping_id] == "superseded_by":
        return "the controlling record cannot be the superseded one"
    return prop


def _ladder_pick(records: list[MappingRecord], law_names: dict[str, str]) -> tuple[str, set[str]]:
    """Deterministic: best rank, then most recent, then stable id order.
    Returns (winner_id, top_rank_candidate_ids)."""
    ranked = sorted(
        records,
        key=lambda r: (
            instrument_rank(law_names.get(r.document_id, r.document_id.replace("_", " "))),
            -_recency_key(r),
            r.mapping_id,
        ),
    )
    top_rank = instrument_rank(
        law_names.get(ranked[0].document_id, ranked[0].document_id.replace("_", " "))
    )
    candidates = {
        r.mapping_id
        for r in records
        if instrument_rank(law_names.get(r.document_id, r.document_id.replace("_", " ")))
        == top_rank
    }
    return ranked[0].mapping_id, candidates


# The LACK indicators score INVERSELY: the 0.0 contributor (framework
# present) is the record that establishes the cell, so alignment takes the
# MINIMUM contribution there and the maximum everywhere else (mirrors every
# _score_cell lane).
_MIN_CONTRIBUTION_INDICATORS = ("7.1", "7.2")


def _fit_subset(
    records: list[MappingRecord],
    classifications: dict[str, ProvisionClassification] | None,
    boundary_excluded: set[str] | None = None,
) -> set[str] | None:
    """mapping_ids whose classification evidences the group's indicator AND
    whose contribution establishes the cell's rubric score (the fit
    filter, score-aligned), or None when no classifications were provided
    (legacy behavior: every member is a controlling candidate).

    Score alignment matters where one measure nature serves several rubric
    lanes: on 7.2 a sectoral banking-secrecy framework and the dedicated
    Cybersecurity Act are both protection_framework, but the cell scores 0.0
    from the dedicated one - the controlling record must be the record the
    score actually derives from, not merely one of the right nature.
    Unclassified and confirmation-refused members are NOT fit (fail-closed)
    but remain valid ladder fallback when nothing fits."""
    if classifications is None:
        return None
    contributions: dict[str, float] = {}
    for r in records:
        if boundary_excluded and r.mapping_id in boundary_excluded:
            continue
        cls = classifications.get(r.mapping_id)
        if cls is not None:
            c = record_contribution(r, cls)
            if c is not None:
                contributions[r.mapping_id] = c
    if not contributions:
        return set()
    best = (
        min(contributions.values())
        if records[0].indicator_id in _MIN_CONTRIBUTION_INDICATORS
        else max(contributions.values())
    )
    return {mid for mid, c in contributions.items() if c == best}


NO_FIT_NOTE = (
    "no-fit-controlling: no member's classification evidences this indicator; "
    "hierarchy ladder applied over all members"
)


def reconcile_group(
    records: list[MappingRecord],
    law_names: dict[str, str],
    config: PipelineConfig | None = None,
    engine: Engine | None = None,
    completion_fn: CompletionFn | None = None,
    storage: Storage | None = None,
    config_dir=CONFIG_DIR,
    classifications: dict[str, ProvisionClassification] | None = None,
    boundary_excluded: set[str] | None = None,
) -> tuple[ReconciledGroup, list[MappingRecord]]:
    """Reconcile one group. Always returns a valid ReconciledGroup with exactly
    one controlling record; the LLM can only influence, never violate, the
    hierarchy ladder. With classifications, both the LLM's choice and the
    ladder are restricted to the indicator-fit subset; an empty fit
    subset falls back to the full-member ladder plus the NO_FIT_NOTE flag."""
    if not records:
        raise ValueError("empty group")
    keys = {(r.economy, r.indicator_id) for r in records}
    if len(keys) > 1:
        raise ValueError(f"records span multiple group keys: {sorted(keys)}")
    not_passed = [r.mapping_id for r in records if r.verification_status != "passed"]
    if not_passed:
        raise ValueError(f"only 'passed' records reconcile; got: {not_passed}")
    economy, indicator = records[0].economy, records[0].indicator_id
    group_id = f"{economy}:{indicator}"
    config = config or PipelineConfig()
    fit_ids = _fit_subset(records, classifications, boundary_excluded)
    no_fit = fit_ids is not None and not fit_ids

    def _audit(decision: str) -> None:
        if storage is None:
            return
        with log_stage(
            storage, stage="m8_reconcile", method="ladder+llm", input_data=group_id
        ) as sr:
            sr.output_data = decision
            sr.decision = decision

    # single-record short-circuit: no LLM, no ladder needed
    if len(records) == 1:
        rec = records[0]
        notes = "single verified record for this indicator"
        if no_fit:
            notes += f"; {NO_FIT_NOTE}"
        group = ReconciledGroup(
            group_id=group_id,
            economy=economy,
            indicator_id=indicator,
            mapping_ids=[rec.mapping_id],
            authoritative_mapping_id=rec.mapping_id,
            relationships={rec.mapping_id: "sole_source"},
            reconciliation_notes=notes,
        )
        updated = rec.model_copy(
            update={"relationship_to_group": "sole_source", "controlling_evidence": True}
        )
        _audit(f"sole_source: {rec.mapping_id}" + (" (no fit)" if no_fit else ""))
        return group, [updated]

    member_ids = {r.mapping_id for r in records}
    members = [
        (r, law_names.get(r.document_id, r.document_id.replace("_", " "))) for r in records
    ]
    if completion_fn is None:
        eng = engine or resolve_engine(None, config_dir)
        completion_fn = make_completion(
            eng, _response_format(), mock_response=_LITELLM_MOCK_RESPONSE
        )

    prompt_fit = fit_ids if fit_ids else None  # None also when empty: no clause
    proposal: ReconcileProposal | None = None
    for attempt in range(1, config.extraction_attempts + 1):
        content = completion_fn(
            build_group_prompt(
                economy, indicator, members, strict=attempt > 1, fit_ids=prompt_fit
            ),
            attempt > 1,
        )
        parsed = _parse_proposal(content, member_ids)
        if isinstance(parsed, ReconcileProposal):
            proposal = parsed
            break

    # The ladder runs WITHIN the fit subset when one exists; the full-member
    # ladder is the fallback for legacy calls and no-fit groups.
    ladder_pool = (
        [r for r in records if r.mapping_id in fit_ids] if fit_ids else records
    )
    ladder_winner, ladder_candidates = _ladder_pick(ladder_pool, law_names)
    notes_parts: list[str] = []
    if no_fit:
        notes_parts.append(NO_FIT_NOTE)
    if proposal is None:
        authoritative = ladder_winner
        relationships: dict[str, RelationshipToGroup] = {
            mid: "complementary" for mid in member_ids
        }
        notes_parts.append(
            "deterministic ladder fallback: no valid LLM proposal within the attempt budget"
        )
        _audit(f"ladder fallback: authoritative={authoritative}")
    else:
        relationships = {e.mapping_id: e.relationship for e in proposal.relationships}
        if proposal.notes:
            notes_parts.append(proposal.notes)
        if proposal.authoritative_mapping_id in ladder_candidates:
            authoritative = proposal.authoritative_mapping_id
        else:
            # the RULE wins: indicator fit, then hierarchy (then recency),
            # overrides the model
            authoritative = ladder_winner
            if relationships[authoritative] == "superseded_by":
                relationships[authoritative] = "complementary"
            notes_parts.append(
                f"hierarchy override: the model proposed {proposal.authoritative_mapping_id} "
                f"but the {'fit-filtered ' if prompt_fit else ''}legal-hierarchy ladder "
                f"selects {authoritative} "
                "(indicator fit first, then Acts > Regulations > Notices > Codes; "
                "hierarchy beats recency)"
            )
        _audit(
            f"reconciled: authoritative={authoritative}"
            + (" (hierarchy override)" if authoritative != proposal.authoritative_mapping_id else "")
        )

    conflicted = [mid for mid, rel in relationships.items() if rel == "conflicting"]
    if conflicted:
        notes_parts.append(
            "conflicting records flagged for human review (contradictory-sources); "
            "not auto-resolved"
        )

    group = ReconciledGroup(
        group_id=group_id,
        economy=economy,
        indicator_id=indicator,
        mapping_ids=sorted(member_ids),
        authoritative_mapping_id=authoritative,
        relationships=relationships,
        reconciliation_notes="; ".join(notes_parts) or None,
    )
    updated = []
    for r in records:
        flags = list(r.uncertainty_flags)
        if r.mapping_id in conflicted and "contradictory-sources" not in flags:
            flags.append("contradictory-sources")
        updated.append(
            r.model_copy(
                update={
                    "relationship_to_group": relationships[r.mapping_id],
                    "controlling_evidence": r.mapping_id == authoritative,
                    "uncertainty_flags": flags,
                }
            )
        )
    return group, updated


def reconcile_records(
    records: list[MappingRecord],
    law_names: dict[str, str],
    **kwargs,
) -> tuple[list[ReconciledGroup], list[MappingRecord]]:
    """Group all passed records by (economy, indicator) and reconcile each.
    The 6.1/6.4 boundary exclusions are computed HERE (they need the 6.4
    groups' chunk ids, which a single 6.1 group cannot see) and threaded into
    every group's fit filter."""
    grouped: dict[tuple[str, str], list[MappingRecord]] = {}
    for r in records:
        grouped.setdefault((r.economy, r.indicator_id), []).append(r)
    if kwargs.get("classifications") is not None and "boundary_excluded" not in kwargs:
        kwargs["boundary_excluded"] = boundary_excluded_61(records)
    groups: list[ReconciledGroup] = []
    updated: list[MappingRecord] = []
    for key in sorted(grouped):
        g, u = reconcile_group(grouped[key], law_names, **kwargs)
        groups.append(g)
        updated.extend(u)
    return groups, updated
