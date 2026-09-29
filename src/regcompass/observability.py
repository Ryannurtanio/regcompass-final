"""Stage observability: every pipeline stage logs input hash, output hash, method,
timestamp, decision, duration into audit_log (project hard constraint).

Usage:

    with log_stage(storage, stage="m1_extract", method="pdfplumber-0.11.10",
                   input_data=raw_bytes) as rec:
        canonical = extract(raw_bytes)
        rec.output_data = canonical.full_text.encode()
        rec.decision = "extracted"

The hash of whatever is assigned to output_data is computed on exit; an exception
inside the block is logged with decision='error: <ExcType>' and re-raised.
"""

from __future__ import annotations

import hashlib
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from regcompass.contracts import StageLogEntry
from regcompass.storage import Storage


def sha256_hex(data: bytes | str | None) -> str | None:
    if data is None:
        return None
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


class StageRecord:
    """Mutable carrier the with-block fills in before exit.

    duration_ms is filled by log_stage's finally (the same wall-time written to
    audit_log), so narration AFTER the with-block can read sr.duration_ms and
    surface the stage's live cost without a second clock.

    duration_ms_override is for the one shape the with-block cannot time: work
    that already happened on a worker thread, where the block only records the
    result. Set it to the measured wall time of that work and the audit row
    carries the stage's real cost instead of the microseconds it took to write
    the row down. Left None (the normal case), the block times itself.
    """

    def __init__(self, decision: str = "ok"):
        self.output_data: bytes | str | None = None
        self.decision = decision
        self.duration_ms: float = 0.0
        self.duration_ms_override: float | None = None


@contextmanager
def log_stage(
    storage: Storage,
    stage: str,
    method: str,
    input_data: bytes | str | None = None,
) -> Iterator[StageRecord]:
    rec = StageRecord()
    start = time.perf_counter()
    try:
        yield rec
    except BaseException as exc:
        rec.decision = f"error: {type(exc).__name__}"
        raise
    finally:
        measured = (time.perf_counter() - start) * 1000.0
        rec.duration_ms = (
            measured if rec.duration_ms_override is None
            else float(rec.duration_ms_override)
        )
        entry = StageLogEntry(
            stage=stage,
            input_hash=sha256_hex(input_data),
            output_hash=sha256_hex(rec.output_data),
            method=method,
            decision=rec.decision,
            duration_ms=rec.duration_ms,
            timestamp=datetime.now(timezone.utc),
        )
        storage.write_audit(entry)
