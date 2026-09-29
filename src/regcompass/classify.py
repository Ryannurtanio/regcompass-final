"""M12 - post-classification of verified mapping records + rubric score derivation.

Why this module exists (M12): presence-based derive_scores saturates at corpus
scale, because
something maps for every indicator once 49 documents are in. Scores need to know
WHAT KIND of measure a provision is, not just that one exists.

Division of labour (same philosophy as M6/M7): the LLM only PICKS labels from a
fixed menu (strict JSON schema, temperature 0, transport retries 0); the score
itself is derived in PURE CODE from the RDTII 2.1 Guide's per-indicator rubric,
applied to the classified records. Labels are model
judgment and are surfaced as such; the byte-verified quote layer is untouched.
Classification never deletes a record: rows labeled not_data_measure stay in the
export and are flagged. A record that cannot be classified within the attempt
budget is "unclassified" and can NEVER drive a score.

Rubric criteria come from the Guide's definitions, never from tuning against the
Round 1 Database cells (the run-diagnosis constraint, tests/golden/repro/REPORT.md).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from .config import CONFIG_DIR, load_indicators
from .contracts import Engine, IndicatorDef, MappingRecord, PipelineConfig
from .engines import CompletionFn, make_completion, resolve_engine

# Context window around the quote shown to the classifier: enough statute to
# judge scope (definitions, conditions, carve-outs) without dumping a whole Act.
CONTEXT_CHARS = 2000

# Test hook, same idea as map._LITELLM_MOCK_RESPONSE: exercise the production
# litellm call path (kwargs, schema, key handling) offline via mock_response.
_LITELLM_MOCK_RESPONSE: str | None = None

MeasureNature = Literal[
    "transfer_ban",
    "conditional_transfer",
    "local_storage",
    "local_infrastructure",
    "retention_requirement",
    "dpo_requirement",
    "dpia_only",
    "government_access",
    "protection_framework",
    "other_data_measure",
    "not_data_measure",
]
DataScope = Literal["personal", "non_personal", "specific_dataset", "not_applicable"]
Application = Literal["horizontal", "sectoral", "single_economy", "not_applicable"]


class ProvisionClassification(BaseModel):
    """The ONLY thing the classifier model may return: one value per closed
    menu. extra='forbid' so any invented key is malformed output, not data."""

    model_config = ConfigDict(extra="forbid")

    measure_nature: MeasureNature
    data_scope: DataScope
    application: Application
    government_data_only: bool
    minimum_period_specified: bool | None = None
    dedicated_framework: bool | None = None
    comprehensive_framework: bool | None = None
    judicial_authorization_required: bool | None = None
    # Set by OUR code after the adversarial confirmation pass, never by the
    # model (the strict response schema has no such key). False = the skeptic
    # check refused the label's claim; the record keeps its label for audit
    # but can never drive a score (record_contribution returns None).
    confirmed: bool = True


@dataclass
class ClassifyOutcome:
    """What happened to one verified record."""

    classification: ProvisionClassification | None
    # "no_scoring_model" is the mappings-only path: the Indicator has no rubric,
    # so no model was called and no score can be derived. The Mapping itself is
    # untouched and still exports with its verbatim quote.
    outcome: Literal["classified", "unclassified", "no_scoring_model"]
    attempts: int
    failures: list[str] = field(default_factory=list)
    basis: str | None = None  # why, in one line, when there is no classification


@dataclass(frozen=True)
class ScoreCell:
    """One derived (economy, indicator) cell: the score, the provision that
    decided it (the human-review pointer), and a one-line basis."""

    score: float
    controlling_mapping_id: str | None
    basis: str


# ---------------------------------------------------------------------------
# prompt
# ---------------------------------------------------------------------------

_MENU = """Label menus - pick exactly one value per field.

measure_nature (what the quoted provision does):
- "transfer_ban": prohibits cross-border transfer of data, or requires data to be
  processed domestically
- "conditional_transfer": permits cross-border transfer of data subject to
  conditions (consent, comparable protection, adequacy, approval, safeguards)
- "local_storage": requires data or records (or a copy) to be kept or stored at
  a location WITHIN the jurisdiction (data localization). The provision must
  contain that domestic-location element: a duty to keep or retain records
  with no stated location is "retention_requirement"; a duty to publish,
  exhibit or disclose documents is not storage; a court or authority power to
  order deletion, forfeiture or disposal of data is "government_access" or
  "other_data_measure", never storage
- "local_infrastructure": requires use of local computing facilities or network
  infrastructure for data processing
- "retention_requirement": requires keeping data or records for some period
- "dpo_requirement": requires appointing a data protection officer (with or
  without impact assessments)
- "dpia_only": requires data protection impact assessments but NO officer
- "government_access": empowers a government authority to access, obtain,
  produce or intercept data
- "protection_framework": establishes or forms part of a data-protection or
  cybersecurity regulatory framework (data-subject rights, controller
  obligations, regulators, incident duties)
- "other_data_measure": regulates data but fits none of the above
- "not_data_measure": does not regulate data or information at all (e.g.
  transfer of a business, shares, property or proceedings; generic corporate,
  tax or licensing rules)

data_scope (what data the measure covers; "not_applicable" only when
measure_nature is "not_data_measure"):
- "personal": personal data / personal information about individuals
- "non_personal": data generally or business data, not specifically personal
- "specific_dataset": one named dataset or record type only (e.g. health
  records, accounting records, subscriber data)
- "not_applicable"

application (how broadly the measure applies; "not_applicable" only when
measure_nature is "not_data_measure"):
- "horizontal": all sectors of the economy
- "sectoral": one sector or activity only (e.g. banking, telecommunications,
  health)
- "single_economy": governs flows with one named foreign economy only
- "not_applicable"

government_data_only (true/false): true ONLY if the measure governs the
government's own data or government-to-government flows, not commercial activity.

Qualifiers - answer with true/false when the question applies to this
provision, null when it does not:
- minimum_period_specified: for retention measures: true if a MINIMUM retention
  period with a stated duration is specified; false if the duration is
  unspecified ("as long as necessary") or only a maximum is set
- dedicated_framework: for cybersecurity frameworks: true if this is a
  DEDICATED cybersecurity law, false if cybersecurity rules ride inside a law
  about something else
- comprehensive_framework: for data-protection frameworks: true if the
  framework is a comprehensive data-protection law granting core data-subject
  rights (access, correction), false if partial or fragmentary
- judicial_authorization_required: for government access powers: true if access
  requires prior authorization by a court or independent judicial authority,
  false if the authority can act without one"""

_TASK = """Task: label WHAT KIND of measure the quoted provision is, using only the menus
above. Classify what the provision text ACTUALLY DOES - its operative verb and
object - not the topic it mentions and NOT the indicator it was mapped to. If
the provision does not do what the indicator describes, say so through the
menus: pick the nature it really has (or "not_data_measure"). Two traps:
- A provision that merely lists subjects a code of practice or regulations MAY
  cover imposes no requirement itself: it is "protection_framework" if it
  builds a data framework, otherwise "other_data_measure".
- A definition or interpretation provision ("X means...", "X is in the
  jurisdiction if...") describes meaning, never an obligation: it is NEVER a
  storage, transfer, infrastructure or retention measure; label it
  "protection_framework" if it defines a data framework's terms, otherwise
  "other_data_measure".
Do not consider policy merit.

Answer with ONLY a JSON object, no prose, no markdown fences, with exactly
these keys:
{"measure_nature": "...", "data_scope": "...", "application": "...",
"government_data_only": false, "minimum_period_specified": null,
"dedicated_framework": null, "comprehensive_framework": null,
"judicial_authorization_required": null}"""

_STRICT_SUFFIX = """

IMPORTANT: your previous answer was rejected. Output ONLY the raw JSON object
with exactly the keys measure_nature, data_scope, application,
government_data_only, minimum_period_specified, dedicated_framework,
comprehensive_framework, judicial_authorization_required. Every value must
come from its menu (or true/false/null). No other keys, no prose, no fences."""


def _context_window(chunk_text: str, quote: str) -> str:
    """The quote plus up to CONTEXT_CHARS of surrounding statute, so the
    classifier sees conditions and carve-outs without a whole-Act dump."""
    pos = chunk_text.find(quote)
    if pos == -1:  # quote is always a chunk slice (M6 anchor); guard anyway
        return chunk_text[: 2 * CONTEXT_CHARS]
    lo = max(0, pos - CONTEXT_CHARS)
    hi = min(len(chunk_text), pos + len(quote) + CONTEXT_CHARS)
    return chunk_text[lo:hi]


def build_classify_prompt(
    record: MappingRecord, chunk_text: str, ind_def: IndicatorDef, strict: bool
) -> str:
    context = _context_window(chunk_text, record.verbatim_quote)
    prompt = (
        "You are classifying one verified provision mapping for the UN ESCAP "
        "RDTII 2.1 framework (Regional Digital Trade Integration Index).\n\n"
        f"INDICATOR {record.indicator_id} - {record.indicator_name}\n"
        f"Definition: {ind_def.definition}\n\n"
        f"PROVISION CONTEXT ({record.section}; document {record.document_id}; "
        f"economy {record.economy}):\n"
        f"<<<CONTEXT\n{context}\nCONTEXT>>>\n\n"
        f"THE MAPPED QUOTE (already byte-verified against the source):\n"
        f"<<<QUOTE\n{record.verbatim_quote}\nQUOTE>>>\n\n"
        f"{_MENU}\n\n{_TASK}"
    )
    if strict:
        prompt += _STRICT_SUFFIX
    return prompt


# ---------------------------------------------------------------------------
# adversarial confirmation (M7's philosophy applied to labels): before a label
# may make a record score-eligible, a second call must answer a skeptical
# yes/no question derived from the label alone. Refusal is fail-closed.
# ---------------------------------------------------------------------------

# One claim per nature, phrased as what ELIGIBILITY actually requires (e.g.
# government_access is only 7.5-eligible without judicial authorization, so
# that clause is part of the claim).
CONFIRM_CLAIMS: dict[str, str] = {
    "transfer_ban": (
        "prohibit transferring data out of the jurisdiction, or require that "
        "data be processed only domestically"
    ),
    "conditional_transfer": (
        "impose conditions that must be satisfied before data may be "
        "transferred out of the jurisdiction"
    ),
    "local_storage": (
        "require that data or records (or a copy of them) be kept or stored "
        "at a location within the jurisdiction"
    ),
    # The SG Cybersecurity Act
    # CII-DESIGNATION power survived the original wording ("require the use
    # of..."). A designation/classification power imposes no localization
    # obligation and is not an infrastructure requirement under the Guide's
    # 6.3 criteria ("a data-centre requirement not tied to data transfer is
    # not scored").
    "local_infrastructure": (
        "impose an obligation to use, install or maintain computing "
        "facilities or network infrastructure located within the "
        "jurisdiction, as a condition of operating, processing data or "
        "providing a service (a provision that merely designates, classifies "
        "or regulates computers or systems that happen to be located there "
        "imposes no such obligation)"
    ),
    "retention_requirement": (
        "require that data or records be kept for at least a stated minimum "
        "period of time"
    ),
    "dpo_requirement": (
        "require the appointment of a person responsible for data protection "
        "(a data protection officer or equivalent)"
    ),
    "dpia_only": "require data protection impact assessments to be carried out",
    "government_access": (
        "empower a government authority to access, obtain or intercept data "
        "without prior authorization by a court or judicial officer"
    ),
    "protection_framework": (
        "establish or form part of a data-protection or cybersecurity "
        "regulatory framework"
    ),
}


def build_confirm_prompt(record: MappingRecord, chunk_text: str, claim: str, strict: bool) -> str:
    context = _context_window(chunk_text, record.verbatim_quote)
    prompt = (
        "You are auditing one label given to a statutory provision. Be "
        "skeptical: answer from the provision's own words only.\n\n"
        f"PROVISION CONTEXT ({record.section}; document {record.document_id}):\n"
        f"<<<CONTEXT\n{context}\nCONTEXT>>>\n\n"
        f"THE QUOTED PROVISION:\n<<<QUOTE\n{record.verbatim_quote}\nQUOTE>>>\n\n"
        f"QUESTION: does the quoted provision itself {claim}?\n"
        "Answer true ONLY if the provision's own words clearly do this. A "
        "provision that merely mentions the subject, defines a term, requires "
        "a notice or filing about it, or empowers someone else to make rules "
        "about it, does NOT do this: answer false.\n\n"
        'Answer with ONLY a JSON object, no prose: {"claim_supported": true} '
        'or {"claim_supported": false}'
    )
    if strict:
        prompt += (
            "\n\nIMPORTANT: your previous answer was rejected. Output ONLY "
            'the raw JSON object {"claim_supported": true} or '
            '{"claim_supported": false}.'
        )
    return prompt


def _confirm_response_format() -> dict:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "claim_confirmation",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {"claim_supported": {"type": "boolean"}},
                "required": ["claim_supported"],
                "additionalProperties": False,
            },
        },
    }


def _parse_confirmation(content: str) -> bool | str:
    """True/False, or a short failure reason."""
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
    if not isinstance(data, dict) or not isinstance(data.get("claim_supported"), bool):
        return "claim_supported missing or not a boolean"
    return data["claim_supported"]


# ---------------------------------------------------------------------------
# model transport (same contract as M6: strict schema, retries pinned 0)
# ---------------------------------------------------------------------------


def _nullable_bool() -> dict:
    return {"type": ["boolean", "null"]}


def _response_format() -> dict:
    """Strict closed schema: every key required, enums verbatim from the
    Literal menus, additionalProperties false."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "provision_classification",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "measure_nature": {"type": "string", "enum": list(MeasureNature.__args__)},
                    "data_scope": {"type": "string", "enum": list(DataScope.__args__)},
                    "application": {"type": "string", "enum": list(Application.__args__)},
                    "government_data_only": {"type": "boolean"},
                    "minimum_period_specified": _nullable_bool(),
                    "dedicated_framework": _nullable_bool(),
                    "comprehensive_framework": _nullable_bool(),
                    "judicial_authorization_required": _nullable_bool(),
                },
                "required": [
                    "measure_nature",
                    "data_scope",
                    "application",
                    "government_data_only",
                    "minimum_period_specified",
                    "dedicated_framework",
                    "comprehensive_framework",
                    "judicial_authorization_required",
                ],
                "additionalProperties": False,
            },
        },
    }


def _default_completion(prompt: str, strict: bool, engine: Engine) -> str:
    """The classification call on one Engine. Kept as a named function because
    the repro script and the tests wire it directly."""
    return make_completion(
        engine, _response_format(), mock_response=_LITELLM_MOCK_RESPONSE
    )(prompt, strict)


def _default_confirm_completion(prompt: str, strict: bool, engine: Engine) -> str:
    """The adversarial confirmation call on one Engine (its own strict schema)."""
    return make_completion(
        engine, _confirm_response_format(), mock_response=_LITELLM_MOCK_RESPONSE
    )(prompt, strict)


def _parse_classification(content: str) -> ProvisionClassification | str:
    """A ProvisionClassification, or a short failure reason."""
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
        return ProvisionClassification.model_validate(data)
    except ValidationError as e:
        return f"schema violation: {e.errors()[0].get('msg', 'invalid')}"


# ---------------------------------------------------------------------------
# the classifier
# ---------------------------------------------------------------------------


def classify_record(
    record: MappingRecord,
    chunk_text: str,
    config: PipelineConfig | None = None,
    engine: Engine | None = None,
    completion_fn: CompletionFn | None = None,
    confirm_completion_fn: CompletionFn | None = None,
    indicator_defs: dict[str, IndicatorDef] | None = None,
    config_dir=CONFIG_DIR,
) -> ClassifyOutcome:
    """Classify one verified record. Never raises on model behaviour: a record
    that cannot be classified within the attempt budget (same 3-total
    convention as M6) is returned unclassified, and unclassified records can
    never drive a score (derive_scores_v2 skips them).

    A label that would make the record SCORE-ELIGIBLE must additionally
    survive the adversarial confirmation question (one call + one strict
    retry). Refusal or persistent malformed output sets confirmed=False
    (fail-closed): the label ships for audit but never drives a score."""
    if record.verification_status != "passed":
        raise ValueError(f"refusing to classify an unverified record: {record.mapping_id}")
    if not has_scoring_model(record.indicator_id):
        # Mappings-only path: this Indicator has no rubric, so there is nothing
        # for a classifier to decide. Returning early also means no model call
        # and no cost on the ten Pillars that are mapped but not scored.
        return ClassifyOutcome(
            classification=None,
            outcome="no_scoring_model",
            attempts=0,
            basis=NO_SCORING_MODEL_BASIS,
        )
    config = config or PipelineConfig()
    defs = indicator_defs or load_indicators(config_dir)
    ind_def = defs[record.indicator_id]
    if completion_fn is None or confirm_completion_fn is None:
        eng = engine or resolve_engine(None, config_dir)
        if completion_fn is None:
            completion_fn = make_completion(
                eng, _response_format(), mock_response=_LITELLM_MOCK_RESPONSE
            )
        if confirm_completion_fn is None:
            confirm_completion_fn = make_completion(
                eng, _confirm_response_format(), mock_response=_LITELLM_MOCK_RESPONSE
            )

    failures: list[str] = []
    for attempt in range(1, config.extraction_attempts + 1):
        content = completion_fn(
            build_classify_prompt(record, chunk_text, ind_def, strict=attempt > 1), attempt > 1
        )
        cls = _parse_classification(content)
        if isinstance(cls, str):
            failures.append(f"attempt {attempt}: {cls}")
            continue
        if record_contribution(record, cls) is not None:
            cls = _run_confirmation(record, chunk_text, cls, confirm_completion_fn, failures)
        return ClassifyOutcome(
            classification=cls, outcome="classified", attempts=attempt, failures=failures
        )
    return ClassifyOutcome(
        classification=None,
        outcome="unclassified",
        attempts=config.extraction_attempts,
        failures=failures,
    )


def _run_confirmation(
    record: MappingRecord,
    chunk_text: str,
    cls: ProvisionClassification,
    confirm_fn: CompletionFn,
    failures: list[str],
) -> ProvisionClassification:
    claim = CONFIRM_CLAIMS[cls.measure_nature]
    for attempt in (1, 2):
        content = confirm_fn(
            build_confirm_prompt(record, chunk_text, claim, strict=attempt > 1), attempt > 1
        )
        verdict = _parse_confirmation(content)
        if isinstance(verdict, str):
            failures.append(f"confirmation attempt {attempt}: {verdict}")
            continue
        if verdict:
            return cls
        failures.append(f"confirmation refused: provision does not {claim}")
        return cls.model_copy(update={"confirmed": False})
    failures.append("confirmation unparseable after strict retry: fail-closed")
    return cls.model_copy(update={"confirmed": False})


# ---------------------------------------------------------------------------
# rubric (per the RDTII 2.1 Guide, pure code)
# ---------------------------------------------------------------------------

# Which measure natures are rubric-eligible evidence for each indicator.
#
# THE MAPPINGS-ONLY PATH. This table, and every table keyed like it, covers the
# nine Pillar 6 and 7 Indicators and nothing else: the RDTII 2.1 Guide's scoring
# rubric (eligibility by measure nature, the 0/0.25/0.5/1 value sets, the
# inverse direction on 7.1 and 7.2) exists only for those two Pillars. The other
# ten Pillars are fully supported for MAPPING - the Gate shortlists for them,
# the Engine quotes for them, verification and export carry their Mappings with
# the verbatim quotes - but no score is derived. Rather than raise on an
# Indicator the rubric never covered, every scoring entry point asks
# has_scoring_model first and returns None, which the export writes as a blank
# score cell. See the same note in export.py and in the README.
ELIGIBLE_NATURES: dict[str, frozenset[str]] = {
    "6.1": frozenset({"transfer_ban"}),
    "6.2": frozenset({"local_storage"}),
    "6.3": frozenset({"local_infrastructure"}),
    "6.4": frozenset({"conditional_transfer"}),
    "7.1": frozenset({"protection_framework"}),
    "7.2": frozenset({"protection_framework"}),
    "7.3": frozenset({"retention_requirement"}),
    "7.4": frozenset({"dpo_requirement", "dpia_only"}),
    "7.5": frozenset({"government_access"}),
}

# Government data is never scored for the Pillar 6 indicators or 7.3
# (per the RDTII 2.1 Guide + 7.3 rule). 7.5 is ABOUT government access, and
# 7.1/7.2/7.4 concern frameworks/obligations, so the flag does not exclude there.
_GOV_EXCLUDED = frozenset({"6.1", "6.2", "6.3", "6.4", "7.3"})

# The score an empty cell takes (the INVERSE direction): no
# restriction found = open = 0, except 7.1/7.2 where no framework = 1.
ABSENCE_SCORE = {
    "6.1": 0.0,
    "6.2": 0.0,
    "6.3": 0.0,
    "6.4": 0.0,
    "7.1": 1.0,
    "7.2": 1.0,
    "7.3": 0.0,
    "7.4": 0.0,
    "7.5": 0.0,
}


# The one registry of Indicators the RDTII 2.1 scoring rubric covers. Every
# per-indicator table in this module and in export.py is keyed off it.
SCORED_INDICATORS: frozenset[str] = frozenset(ELIGIBLE_NATURES)

NO_SCORING_MODEL_BASIS = "no scoring model for this indicator: mappings only"


def has_scoring_model(indicator_id: str) -> bool:
    """True when the Guide gives this Indicator a scoring rubric. False means
    the mappings-only path: quotes are mapped, verified and exported, and no
    score is derived."""
    return indicator_id in SCORED_INDICATORS


def _strong(cls: ProvisionClassification) -> bool:
    """The 'covers PERSONAL data OR is HORIZONTAL' test shared by 6.1/6.2/6.4
    (per the Guide: personal data scores 1 whether
    horizontal or sectoral; 'HORIZONTAL even if only non-personal' also scores
    1). A measure on ONE NAMED DATASET is never 'horizontal' in data coverage,
    whatever the sector spread of the obligated entities: the Guide's 0.5 lane
    names 'a specific data set' explicitly, and reading horizontality off the
    entity scope would make that lane unreachable for any dataset rule binding
    all companies (e.g. accounting-records storage)."""
    if cls.data_scope == "personal":
        return True
    return cls.application == "horizontal" and cls.data_scope == "non_personal"


def record_contribution(record: MappingRecord, cls: ProvisionClassification) -> float | None:
    """What this record ALONE implies for its indicator's cell (the value that
    lands in MappingRecord.rdtii_score_contribution). None = rubric-ineligible
    (wrong nature for the indicator, government-data-only where excluded, or
    the label failed the adversarial confirmation): the record stays in the
    export but contributes nothing to the score."""
    ind = record.indicator_id
    if not has_scoring_model(ind):
        return None  # mappings-only path: the record ships, no score is derived
    if not cls.confirmed:
        return None
    if cls.measure_nature not in ELIGIBLE_NATURES[ind]:
        return None
    if ind in _GOV_EXCLUDED and cls.government_data_only:
        return None
    if ind in ("6.1", "6.2", "6.4"):
        return 1.0 if _strong(cls) else 0.5
    if ind == "6.3":
        return 1.0
    if ind == "7.1":
        return 0.0 if (cls.comprehensive_framework and cls.application == "horizontal") else 0.5
    if ind == "7.2":
        return 0.0 if (cls.dedicated_framework and cls.application == "horizontal") else 0.5
    if ind == "7.3":
        return 1.0 if cls.minimum_period_specified else None
    if ind == "7.4":
        if cls.measure_nature == "dpia_only":
            return 0.25
        return 1.0 if cls.application == "horizontal" else 0.5
    if ind == "7.5":
        return 1.0 if cls.judicial_authorization_required is False else None
    raise ValueError(f"unknown indicator {ind}")  # unreachable: IndicatorId is closed


def boundary_excluded_61(records: list[MappingRecord]) -> set[str]:
    """The 6.1/6.4 boundary rule (per the RDTII 2.1 Guide), one implementation
    for every consumer: mapping_ids of 6.1 records whose chunk ALSO produced a
    6.4 record in the same economy - conditional-transfer evidence, never ban
    evidence. Used by apply_classifications, derive_scores_v2, and M8's
    fit-filtered controlling selection so a boundary-excluded record can
    neither carry a 6.1 contribution nor control the 6.1 cell."""
    chunks_64: dict[str, set[str]] = {}
    for r in records:
        if r.indicator_id == "6.4" and r.verification_status == "passed":
            chunks_64.setdefault(r.economy, set()).add(r.chunk_id)
    return {
        r.mapping_id
        for r in records
        if r.indicator_id == "6.1" and r.chunk_id in chunks_64.get(r.economy, set())
    }


def apply_classifications(
    records: list[MappingRecord],
    classifications: dict[str, ProvisionClassification],
) -> list[MappingRecord]:
    """Fill measure_type + rdtii_score_contribution (the M6 'left None,
    downstream' deferral) on copies; unclassified records stay None. The
    6.1/6.4 boundary rule applies here too: a boundary-excluded 6.1 record
    keeps its label but contributes nothing, exactly as it scores."""
    excluded = boundary_excluded_61(records)
    out: list[MappingRecord] = []
    for r in records:
        cls = classifications.get(r.mapping_id)
        if cls is None or r.verification_status != "passed":
            out.append(r)
            continue
        out.append(
            r.model_copy(
                update={
                    "measure_type": cls.measure_nature,
                    "rdtii_score_contribution": (
                        None
                        if r.mapping_id in excluded
                        else record_contribution(r, cls)
                    ),
                }
            )
        )
    return out


def derive_scores_v2(
    records: list[MappingRecord],
    classifications: dict[str, ProvisionClassification],
) -> dict[tuple[str, str], ScoreCell]:
    """Classification-based derivation per the Guide rubric. Deterministic:
    eligible records are considered with M8's
    controlling-evidence record first, then mapping_id order; the controlling
    record named on a cell is the first that establishes the cell's score.
    The 6.1/6.4 boundary rule applies before eligibility, exactly as in the
    presence-based derive_scores. Unclassified records never drive a score."""
    # Controlling-evidence records sort first: when several records establish
    # the same cell score, the named controlling provision is the one the CSV
    # flags as Controlling Evidence (M8's fit-filtered ladder pick), so the
    # supplementary and the submission table tell one story. The cell SCORE
    # never depends on this order (_score_cell scans all records per lane).
    passed = sorted(
        (r for r in records if r.verification_status == "passed"),
        key=lambda r: (not r.controlling_evidence, r.mapping_id),
    )
    excluded = boundary_excluded_61(passed)

    eligible: dict[tuple[str, str], list[tuple[MappingRecord, ProvisionClassification]]] = {}
    economies: set[str] = set()
    for r in passed:
        economies.add(r.economy)
        cls = classifications.get(r.mapping_id)
        if cls is None:
            continue
        if r.mapping_id in excluded:
            continue
        if record_contribution(r, cls) is None:
            continue
        eligible.setdefault((r.economy, r.indicator_id), []).append((r, cls))

    # Only Indicators with a rubric get a cell. An Indicator on the
    # mappings-only path is absent from this dict, and the export writes a
    # blank score for it rather than inventing one.
    scores: dict[tuple[str, str], ScoreCell] = {}
    for econ in sorted(economies):
        for ind in sorted(SCORED_INDICATORS):
            recs = eligible.get((econ, ind), [])
            scores[(econ, ind)] = _score_cell(ind, recs)
    return scores


def _score_cell(
    ind: str, recs: list[tuple[MappingRecord, ProvisionClassification]]
) -> ScoreCell:
    if not recs:
        return ScoreCell(ABSENCE_SCORE[ind], None, "no rubric-eligible measure found")

    if ind in ("6.1", "6.2", "6.4"):
        for r, cls in recs:
            if _strong(cls):
                return ScoreCell(
                    1.0,
                    r.mapping_id,
                    f"{cls.measure_nature} covering "
                    + ("personal data" if cls.data_scope == "personal" else "all sectors"),
                )
        # 6.1/6.2 only: two or more DISTINCT requirements on non-personal /
        # specific data also score 1 (Guide). 6.4 has no such lane.
        if ind in ("6.1", "6.2"):
            distinct = {(r.document_id, r.section) for r, _ in recs}
            if len(distinct) >= 2:
                return ScoreCell(
                    1.0,
                    recs[0][0].mapping_id,
                    f"{len(distinct)} distinct non-personal/specific requirements",
                )
        r, cls = recs[0]
        return ScoreCell(0.5, r.mapping_id, f"{cls.measure_nature} on {cls.data_scope} data")

    if ind == "6.3":
        return ScoreCell(1.0, recs[0][0].mapping_id, "local infrastructure requirement")

    if ind in ("7.1", "7.2"):
        for r, cls in recs:
            if record_contribution(r, cls) == 0.0:
                kind = "comprehensive" if ind == "7.1" else "dedicated"
                return ScoreCell(0.0, r.mapping_id, f"{kind} horizontal framework")
        label = "sectoral or partial" if ind == "7.1" else "non-dedicated or sectoral"
        return ScoreCell(0.5, recs[0][0].mapping_id, f"{label} framework only")

    if ind == "7.3":
        return ScoreCell(1.0, recs[0][0].mapping_id, "minimum retention period specified")

    if ind == "7.4":
        for r, cls in recs:
            if cls.measure_nature == "dpo_requirement" and cls.application == "horizontal":
                return ScoreCell(1.0, r.mapping_id, "DPO required horizontally")
        for r, cls in recs:
            if cls.measure_nature == "dpo_requirement":
                return ScoreCell(0.5, r.mapping_id, "DPO required for a specific sector")
        return ScoreCell(0.25, recs[0][0].mapping_id, "DPIA-only requirement")

    if ind == "7.5":
        return ScoreCell(
            1.0, recs[0][0].mapping_id, "government access without judicial authorization"
        )

    raise ValueError(f"unknown indicator {ind}")  # unreachable: IndicatorId is closed
