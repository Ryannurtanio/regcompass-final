"""M7 - mechanical verification. This module is the product guarantee: 100% of
shipped records pass these checks because the checks are code, not model.

Checks, in order (stdlib string/regex only, deliberately boring):
1. verbatim_quote is a non-trivial byte-for-byte substring of the chunk text
   (which is itself a slice of the canonical stream; pass canonical to re-prove
   that identity too).
2. Every component of the cited subsection reference ("(2)(a)" -> "(2)", "(a)")
   is actually present in the chunk text; a reference with no parseable
   component is a failure.

A failure triggers the stricter-retry escalation: re-map the pair (M6) with
mechanical feedback appended to the prompt, sharing the SAME attempt budget
(extraction_attempts, max 3 total across map + verify). Exhausted -> the record
is marked dropped and logged, never shipped. This loop is hand-written and
visible here; no library retry magic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from .config import CONFIG_DIR
from .contracts import (
    CanonicalText,
    Chunk,
    EconomyCode,
    Engine,
    GatedChunk,
    IndicatorDef,
    MappingRecord,
    PipelineConfig,
)
from .map import MIN_QUOTE_CHARS, CompletionFn, map_gated_chunk
from .observability import log_stage
from .storage import Storage

_SUBSECTION_COMPONENT = re.compile(r"\([0-9A-Za-z]+\)")


def verify_record(
    record: MappingRecord, chunk: Chunk, canonical: CanonicalText | None = None
) -> list[str]:
    """The pure mechanical checks. Empty list = verified. Raises on caller
    errors (wrong chunk, insufficient-evidence record, corrupted slice):
    those are pipeline bugs, not model failures to retry."""
    if record.insufficient_evidence:
        raise ValueError("verification applies to substantive mappings, not insufficient-evidence records")
    if record.chunk_id != chunk.chunk_id:
        raise ValueError(f"record cites chunk {record.chunk_id} but got chunk {chunk.chunk_id}")
    if canonical is not None and canonical.slice(chunk.char_start, chunk.char_end) != chunk.text:
        raise ValueError(
            f"chunk {chunk.chunk_id} is not a slice of the canonical stream: stream corrupted"
        )

    failures: list[str] = []
    quote = record.verbatim_quote
    if len(quote.strip()) < MIN_QUOTE_CHARS:
        failures.append(f"quote too short or trivial ({len(quote.strip())} chars after strip)")
    elif quote not in chunk.text:
        failures.append("quote is not a byte-for-byte substring of the chunk text")
    if record.subsection is not None:
        components = _SUBSECTION_COMPONENT.findall(record.subsection)
        if not components:
            failures.append(f"unparseable subsection reference {record.subsection!r}")
        else:
            missing = [c for c in components if c not in chunk.text]
            if missing:
                failures.append(
                    f"subsection component(s) {', '.join(missing)} not present in the chunk"
                )
    return failures


@dataclass
class VerifyOutcome:
    """The final word on one (chunk, indicator) pair."""

    record: MappingRecord
    outcome: Literal["passed", "no_evidence", "dropped"]
    attempts: int
    failures: list[str] = field(default_factory=list)


def _hint_for(failures: list[str], record: MappingRecord) -> str:
    parts = []
    if any("byte-for-byte" in f or "too short" in f for f in failures):
        parts.append(
            "Your previous quote could not be found byte-for-byte in the provision. "
            "Choose a SHORTER contiguous span (a single clause) and copy it exactly "
            "as printed, including any page numbers or line breaks inside it."
        )
    if any("subsection" in f for f in failures):
        parts.append(
            f"Your previous subsection reference {record.subsection!r} is not present "
            "in the provision text. Cite a marker that actually appears in it, or use "
            "null if the provision has no subsection markers."
        )
    return " ".join(parts) or "; ".join(failures)


def verify_with_retry(
    record: MappingRecord,
    gated: GatedChunk,
    economy: EconomyCode,
    config: PipelineConfig | None = None,
    engine: Engine | None = None,
    completion_fn: CompletionFn | None = None,
    indicator_defs: dict[str, IndicatorDef] | None = None,
    storage: Storage | None = None,
    canonical: CanonicalText | None = None,
    config_dir=CONFIG_DIR,
) -> VerifyOutcome:
    """Verify one mapped record; on mechanical failure, re-map with feedback
    until it verifies, comes back as no-evidence, or the attempt budget dies."""
    config = config or PipelineConfig()

    def _audit(decision: str, rec: MappingRecord) -> None:
        if storage is None:
            return
        with log_stage(
            storage, stage="m7_verify", method="substring+subsection", input_data=rec.mapping_id
        ) as sr:
            sr.output_data = rec.verbatim_quote
            sr.decision = decision

    if record.insufficient_evidence:
        _audit("no_evidence: verification not applicable", record)
        return VerifyOutcome(record=record, outcome="no_evidence", attempts=record.extraction_attempts)

    current = record
    attempts = record.extraction_attempts
    all_failures: list[str] = []
    while True:
        failures = verify_record(current, gated.chunk, canonical)
        if not failures:
            passed = current.model_copy(
                update={"verification_status": "passed", "extraction_attempts": attempts}
            )
            _audit(f"passed (attempt {attempts})", passed)
            return VerifyOutcome(
                record=passed, outcome="passed", attempts=attempts, failures=all_failures
            )
        all_failures.extend(f"attempt {attempts}: {f}" for f in failures)
        if attempts >= config.extraction_attempts:
            dropped = current.model_copy(
                update={"verification_status": "dropped", "extraction_attempts": attempts}
            )
            _audit(f"dropped after {attempts} attempts: {'; '.join(failures)}", dropped)
            return VerifyOutcome(
                record=dropped, outcome="dropped", attempts=attempts, failures=all_failures
            )
        _audit(f"retry attempt {attempts + 1}: {'; '.join(failures)}", current)
        out = map_gated_chunk(
            gated,
            economy,
            config=config,
            engine=engine,
            completion_fn=completion_fn,
            indicator_defs=indicator_defs,
            config_dir=config_dir,
            start_attempt=attempts + 1,
            hint=_hint_for(failures, current),
            pages=canonical.pages if canonical is not None else None,
        )
        attempts = out.attempts
        all_failures.extend(out.failures)
        if out.record is None or out.outcome == "dropped":
            dropped = current.model_copy(
                update={"verification_status": "dropped", "extraction_attempts": attempts}
            )
            _audit(f"dropped after {attempts} attempts: re-map produced no valid output", dropped)
            return VerifyOutcome(
                record=dropped, outcome="dropped", attempts=attempts, failures=all_failures
            )
        if out.outcome == "no_evidence":
            _audit(f"no_evidence on retry (attempt {attempts})", out.record)
            return VerifyOutcome(
                record=out.record, outcome="no_evidence", attempts=attempts, failures=all_failures
            )
        current = out.record
