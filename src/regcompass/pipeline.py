"""Judge-path pipeline (P0): `regcompass run` and `regcompass export`.

The storage-attached pipeline over one Economy's CORPUS: M1 extract
(-> M2 OCR when the born-digital lane is unusable) -> M4 chunk -> M5 gate ->
M6 map -> M7 mechanical verify -> M3 gloss (non-English Documents only) ->
M8 reconcile, every stage logging to audit_log via log_stage, every record
persisted in the mappings table.
`export` is M9 only (no model calls): it rebuilds everything the export
battery needs from the database alone.

A Run never fetches. Its Documents come from the Corpus that
Discovery (regcompass.discovery) built, so two Runs with two Engines read
exactly the same Documents and only the first pass costs a request.

Engines: one selected Engine (config/models.yaml, `regcompass engines`) drives
every model-calling stage of a run, and its name goes into the audit trail, so
the Run Record names the model that actually answered. The fake Engine runs the
same code path offline. The dev-scale reproduction over the real 49-document
corpus lives in scripts/run_repro.py (checkpointed, resume-safe); THIS module is
the judge-facing lane over the committed fixture corpus.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import copy_context
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Sequence
from urllib.parse import urlparse
from .config import (
    CONFIG_DIR,
    load_corpus,
    load_indicators,
    load_law_metadata,
    load_pipeline,
    load_portals,
)
from .contracts import (
    MAX_CONCURRENCY,
    CanonicalText,
    CorpusDoc,
    Engine,
    GatedChunk,
    MappingRecord,
    PageSpan,
    PipelineConfig,
    Review,
)
from .chunk import quote_page, repair_section_labels, split_document
from .engines import cost_usd_for, embed_fn_for, read_meter, resolve_engine, start_meter
from .extract import (
    extract_with_stats,
    extraction_key,
    extraction_provenance,
    load_extraction,
    ocr_quality_columns,
    sniff_format,
    store_extraction,
)
from .export import ExportResult, SyntheticDoc, export_all, pillar_of, pointer_gate
from .gate import gate_document
from .languages import (
    is_english_language,
    keyword_tier_applies,
    ocr_policy,
    tesseract_language_note,
    tesseract_languages,
)
from .map import ConfigError, MapOutcome, map_gated_chunk
from .observability import log_stage
from .paths import fallback_places, resolve_stored_path, storable_local_path
from .reconcile import reconcile_records
from .run_progress import (
    CUT, DROPPED_BY_PROOF, GATE, GLOSS, MAP, MAPPED, MEANING_AND_KEYWORDS,
    MEANING_ONLY, NOT_APPLICABLE, PROVE, READ, RECONCILE, SCAN_CHECK, SKIPPED,
    RunProgress, StepTracker,
)
from .shortlist import should_ocr
from .storage import Storage, new_run_id, utc_now_iso, utc_now_z
from .verify import verify_with_retry

# Transport-retry ladder (same shape the repro script proved): a
# timeout or dropped connection is a TRANSPORT failure - no model output
# exists, so retrying it cannot corrupt the 3-attempt content budget. A slow
# machine WILL hit the 180s per-request ceiling occasionally (observed live: a
# local Engine timed out under concurrent CPU load and killed the whole run);
# three tries with backoff make the run survive it.
TRANSPORT_RETRIES = 3

# The one place this module waits. Named so a test can replace it without
# monkeypatching the standard library, and so both backoff ladders below are
# visibly the same clock.
_sleep = time.sleep

# A rate limit is the provider saying "slower", not "broken": the answer is a
# short wait and another try, not the transport ladder's minute of backoff.
# Five attempts, four waits, 7.5 seconds in all before the pair is given up.
RATE_LIMIT_WAITS = (0.5, 1.0, 2.0, 4.0)
RATE_LIMIT_ATTEMPTS = len(RATE_LIMIT_WAITS) + 1


def is_rate_limit_error(exc: BaseException) -> bool:
    """Whether this exception is a provider rate limit (HTTP 429).

    Matched three ways because the Engines reach us through LiteLLM, which
    wraps each provider's own exception: the class NAME (so the base tier never
    needs litellm importable), an explicit status code, and finally the text.
    A false positive costs a short wait; a false negative costs the transport
    ladder's full minute per pair, which is what the concurrency is for."""
    if type(exc).__name__ in ("RateLimitError", "TooManyRequests"):
        return True
    for attr in ("status_code", "code", "http_status"):
        if getattr(exc, attr, None) in (429, "429"):
            return True
    text = str(exc).lower()
    return "rate limit" in text or "too many requests" in text or "429" in text


def resolve_concurrency(engine: Engine, override: int | None = None) -> int:
    """How many Mapping calls this Run may have in flight: the Engine's own
    declared value unless the operator named one, bounded either way."""
    wanted = engine.concurrency if override is None else int(override)
    return max(1, min(int(wanted), MAX_CONCURRENCY))


def _with_transport_retry(
    label: str,
    progress: Callable[[str], None],
    fn,
    *args,
    required: bool = True,
    reraise_rate_limit: bool = False,
    on_error: Callable[[Exception], None] | None = None,
    **kwargs,
):
    """required=True raises after the ladder (a whole-stage call like M8 must
    not silently vanish); required=False returns None so a single dead PAIR is
    skipped-and-logged rather than killing the whole demo run - the same
    semantics the repro script uses.

    reraise_rate_limit hands a 429 straight back to the caller instead of
    burning 35 seconds of transport backoff on it. Only the Mapping pool sets
    it, because only the pool has the shorter ladder to hand it to; every other
    caller keeps today's behaviour exactly.

    on_error sees every failure the ladder swallowed. required=False returns a
    bare None, and a caller that must say WHY the pair was skipped (a dropped
    connection reads nothing like a call that blew its deadline) has no other
    way to know. Optional, so every other caller is untouched."""
    last: Exception | None = None
    for i in range(TRANSPORT_RETRIES):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 - content failures never raise here
            # A configuration problem is not a transport blip: retrying a
            # missing or rejected API key burns the whole backoff ladder per
            # pair and ends in a green banner over zero records.
            # ConfigError comes from our own preflight in map.py;
            # AuthenticationError is litellm's 401 (matched by name so the
            # base tier never needs litellm importable).
            if isinstance(e, ConfigError) or type(e).__name__ == "AuthenticationError":
                raise
            if reraise_rate_limit and is_rate_limit_error(e):
                raise
            last = e
            if on_error is not None:
                on_error(e)
            wait = 5 * 2**i
            progress(
                f"{label}: transport error {type(e).__name__},"
                f" retry {i + 1}/{TRANSPORT_RETRIES} in {wait}s"
            )
            _sleep(wait)
    if required:
        raise RuntimeError(
            f"{label}: transport error persisted after {TRANSPORT_RETRIES} tries"
        ) from last
    progress(f"{label}: transport error persisted; pair skipped (see audit_log)")
    return None


class EmptyCorpusError(RuntimeError):
    """This Economy has no Corpus yet, so there is nothing for a Run to read.
    A Run never fetches: Discovery fills the Corpus first."""

    def __init__(self, economy: str, official_name: str):
        self.economy = economy
        self.official_name = official_name
        super().__init__(
            f"no Documents in the Corpus for {official_name};"
            f" run `regcompass discover --economy {economy}` first"
        )


def no_section_chunks_warning(doc_id: str) -> str:
    """The one sentence three places say about a Document with no structure.

    The Run log says it at the chunk step, the Run Record carries it for the
    Evidence screen, and the export refusal repeats it instead of blaming the
    Engine. One string, so an operator who reads it twice reads the same
    diagnosis and not two competing ones."""
    return (
        f"{doc_id}: no section structure was found, so the whole Document became"
        " one unstructured chunk. The Gate only ever reads section chunks, so"
        " nothing was sent to the Engine and this Document can produce no"
        " Mappings. This is about the Document, not the Engine: check that the"
        " file is the statute text rather than a cover page, a form or a scan"
        " that did not read."
    )


@dataclass
class RunReport:
    """What one `regcompass run` invocation did, document by document.

    The bottom block is the Run Record's own half: the id the record was filed
    under, the two timestamps, and the measured spend. They are filled in by
    record_run, so a caller holding a finished report can print the record
    without going back to the database."""

    economy: str
    engine: str  # the selected Engine's name, as the Run Record reports it
    documents: list[str] = field(default_factory=list)
    n_chunks: int = 0
    # Every (chunk, Indicator) pair the Gate weighed, before it kept any.
    n_pairs_considered: int = 0
    n_pairs_gated: int = 0
    n_passed: int = 0
    n_no_evidence: int = 0
    n_dropped: int = 0
    n_groups: int = 0
    notes: list[str] = field(default_factory=list)
    run_id: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    # A Run reads the Corpus and never fetches; Discovery reports its own count.
    documents_fetched: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    provider_cost_usd: float | None = None
    # M3: Glosses drafted in this Run, and the wall time they cost. Time is
    # reported separately because the gloss lane is the one stage that can be
    # switched off, and an operator against the clock has to see its price.
    n_glossed: int = 0
    gloss_ms: float = 0.0
    # How many Mapping calls this Run kept in flight, and how many times a
    # provider told it to slow down. Both belong on the Run Record: the first
    # is why a Run that took 12 minutes did not take 44, and the second is the
    # honest reason a Run at concurrency 4 was not four times faster.
    engine_concurrency: int = 1
    rate_limit_retries: int = 0
    # Where each Document's text came from: 'extracted' (this Run read the
    # bytes, and paid the OCR if the text layer was unusable) or 'reused' (the
    # Corpus had already read them under the same key). One entry per Document,
    # and beside it the key that was looked up, so the Run Record alone answers
    # "which stream was this?" without anyone joining it to the audit trail.
    extraction: dict[str, str] = field(default_factory=dict)
    extraction_keys: dict[str, str] = field(default_factory=dict)
    # And, for a Document OCR read, which engine and which vendored language
    # files produced the text. None for a born-digital Document, which is the
    # honest answer rather than an empty pair of strings.
    extraction_provenance: dict[str, dict[str, str] | None] = field(default_factory=dict)
    # Plain sentences about something that went wrong in a way the Run cannot
    # fix and the operator has to know. Not notes: notes are for the reader of
    # the record afterwards, a warning is for the person watching the Run.
    warnings: list[str] = field(default_factory=list)


def run_details(report: RunReport) -> dict:
    """The counters a Run Record carries in its details column: the funnel a
    reader wants beside the cost, and nothing the columns already hold."""
    return {
        "documents": list(report.documents),
        "extraction": dict(report.extraction),
        "extraction_keys": dict(report.extraction_keys),
        "extraction_provenance": dict(report.extraction_provenance),
        "n_chunks": report.n_chunks,
        "n_pairs_considered": report.n_pairs_considered,
        "n_pairs_gated": report.n_pairs_gated,
        "n_passed": report.n_passed,
        "n_no_evidence": report.n_no_evidence,
        "n_dropped": report.n_dropped,
        "n_groups": report.n_groups,
        "n_glossed": report.n_glossed,
        "gloss_ms": round(report.gloss_ms, 1),
        "engine_concurrency": report.engine_concurrency,
        "rate_limit_retries": report.rate_limit_retries,
        "notes": list(report.notes),
        # What the Evidence screen puts in front of the operator. Always
        # present, empty on a Run with nothing to say, so the interface reads
        # one shape whatever happened.
        "warnings": list(report.warnings),
    }


@contextmanager
def record_run(
    storage: Storage,
    report: RunReport,
    *,
    economy: str,
    pillars: tuple[int, ...],
    engine: Engine,
    indicators: list[str] | None = None,
    extra_details: Callable[[], dict] | None = None,
    run_id: str | None = None,
):
    """Open a Run Record, meter the run, and close the record either way.

    The row is written BEFORE the first model call, so a run that is still
    going (or one that died) is visible in the table instead of appearing only
    on success. On the way out the meter's totals and the cost computed from
    the Engine's declared prices land on both the record and the report; an
    escaping exception files a `failed` record with the error and re-raises it
    unchanged.

    `run_id` ADOPTS a record somebody else already opened rather than opening
    one. The server uses it: a Run starts on a worker thread, and a process
    killed in the seconds between "started" and the first model call used to
    leave no trace of a Run that had certainly begun. The server therefore
    opens the record itself, synchronously, before it answers the request, and
    hands the id down here to be closed exactly as this lane closes its own.

    extra_details() is the lane's own counters, merged into details at the end
    (the e2e lane's crawl split, for example)."""
    adopted = None if run_id is None else storage.run_get(run_id)
    if adopted is None:
        run_id = run_id or new_run_id("run")
        started_at = utc_now_z()
        storage.run_start(
            run_id=run_id, kind="run", economy=economy, pillars=list(pillars),
            indicators=indicators, engine=engine.name, started_at=started_at,
        )
    else:
        # The record's own clock, not this thread's: it started when the
        # request was answered, and the report has to say the same thing the
        # Runs list does.
        started_at = adopted["started_at"]
    report.run_id = run_id
    report.started_at = started_at
    start_meter()

    def _close(status: str, error: str | None) -> None:
        totals = read_meter()
        report.prompt_tokens = totals.prompt_tokens
        report.completion_tokens = totals.completion_tokens
        report.cost_usd = cost_usd_for(
            engine, totals.prompt_tokens, totals.completion_tokens
        )
        report.provider_cost_usd = totals.provider_cost_usd
        report.ended_at = utc_now_z()
        details = run_details(report)
        details["model_calls"] = totals.calls
        if extra_details is not None:
            details.update(extra_details())
        storage.run_finish(
            run_id, status=status, ended_at=report.ended_at,
            documents_fetched=report.documents_fetched,
            prompt_tokens=report.prompt_tokens,
            completion_tokens=report.completion_tokens,
            cost_usd=report.cost_usd,
            provider_cost_usd=report.provider_cost_usd,
            error=error, details=details,
        )

    try:
        yield run_id
    except BaseException as exc:
        _close("failed", f"{type(exc).__name__}: {exc}")
        raise
    _close("completed", None)


def row_value(row, key: str):
    """One column of a database row, or None when this row has no such column.
    A sqlite3.Row raises on a name it does not carry, and the callers below
    read columns an older row (or a hand-made test row) may not have."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def corpus_document_path(row, data_dir: Path, progress=None) -> Path:
    """Where this Corpus Document's exact bytes live.

    The row records its path relative to the data folder, so the same database
    works here and inside the image. Rows written before that was enforced hold
    an absolute path, or one with a stray `data/` in front, and both stop
    opening the moment the folder moves. So a path that does not open is looked
    for under this Economy's own raw folder, and the candidate has to hash to
    the digest the row already carries before it is accepted: the bytes are the
    Document's identity, a matching file name is not. A repair is announced
    through `progress`, because the row it came from still needs rewriting.

    Nothing plausible on disk is still a refusal, and it names every place that
    was tried.
    """
    resolved = resolve_stored_path(
        row_value(row, "local_path"),
        data_dir,
        economy=row_value(row, "economy"),
        sha256=row_value(row, "source_sha256"),
    )
    if resolved is None:
        stored = row_value(row, "local_path")
        if not stored:
            raise RuntimeError(
                f"Corpus Document missing on disk: {row_value(row, 'document_id')}"
                " records no stored file at all. Add the Document again, or run"
                " `regcompass discover` for its Economy."
            )
        tried = [
            str(Path(str(stored)) if Path(str(stored)).is_absolute()
                else Path(data_dir) / str(stored))
        ]
        tried += [
            str(place)
            for place, _ in fallback_places(
                str(stored), data_dir, row_value(row, "economy")
            )
        ]
        raise RuntimeError(
            f"Corpus Document missing on disk: {tried[0]}. Document"
            f" {row_value(row, 'document_id')} stores its path relative to the"
            " --data-dir of the Discovery that fetched it; point --data-dir at"
            " that folder (or run `regcompass discover` again into a fresh"
            f" --db). Looked in: {', '.join(tried)}."
        )
    if resolved.repaired and progress is not None:
        progress(
            f"M0 corpus | {row_value(row, 'document_id')}: stored path"
            f" '{resolved.stored}' does not open here; read from"
            f" {resolved.path} ({resolved.form}, digest verified)"
        )
    return resolved.path


def corpus_for_run(
    storage: Storage,
    economy: str,
    data_dir: Path,
    config_dir=CONFIG_DIR,
    progress=None,
):
    """The Documents of one Economy's Corpus, each with its bytes on disk.
    Raises EmptyCorpusError when Discovery has not run yet."""
    rows = storage.corpus_documents(economy)
    if not rows:
        portals = load_portals(config_dir)
        portal = portals.get(economy)
        raise EmptyCorpusError(
            economy, portal.official_name if portal is not None else economy
        )
    return [(row, corpus_document_path(row, data_dir, progress)) for row in rows]


# ---------------------------------------------------------------------------
# M6 map + M7 verify: the bounded pair pool
# ---------------------------------------------------------------------------
#
# The division of labour is the whole design. A WORKER thread does one thing:
# call the Engine for one pair and hand back what came out (or raise). It never
# touches storage, never writes an audit row, never narrates. The MAIN thread
# consumes those outcomes in PAIR ORDER (the futures are read in submission
# order, not as they complete) and does exactly what the sequential loop always
# did: the m6 audit row, M7 verify, the record, the counters, the progress tick.
#
# That is what keeps a parallel Run honest. The audit trail, the record order
# and therefore the export bytes do not depend on which call finished first,
# and SQLite is only ever touched by the thread that opened it.


@dataclass
class _PairResult:
    """One pair's trip to the Engine, as the worker saw it.

    notes carries the worker's would-be progress lines instead of printing
    them, so all narration still happens on the main thread and in pair order.
    """

    outcome: MapOutcome | None
    duration_ms: float = 0.0
    rate_limit_retries: int = 0
    notes: list[str] = field(default_factory=list)
    skip_reason: str | None = None


# A skipped pair's reason ends up on the Run Record and in the audit row, and
# it is the only thing a steward reading the record afterwards has. "transport
# failure" alone does not distinguish a provider that dropped the connection
# from one that accepted the request and never answered, so the last failure
# names itself here. Capped, because a provider's own error text runs long.
_SKIP_REASON_DETAIL_CHARS = 160


def _transport_skip_reason(swallowed: list[Exception]) -> str:
    if not swallowed:  # pragma: no cover - the ladder always swallows one
        return "transport failure persisted; pair skipped"
    last = swallowed[-1]
    detail = f"{type(last).__name__}: {last}"[:_SKIP_REASON_DETAIL_CHARS]
    return f"transport failure persisted ({detail}); pair skipped"


def _map_one_pair(
    g: GatedChunk,
    *,
    economy: str,
    config: PipelineConfig,
    engine: Engine,
    completion_fn: Callable | None,
    indicator_defs: dict,
    config_dir,
    pages: Sequence[PageSpan] | None = None,
) -> _PairResult:
    """WORKER BODY: map one gated pair, with the rate-limit ladder around the
    transport ladder. Runs on a pool thread, so it touches nothing shared
    except the Engine and the usage meter.

    A rate limit is retried here, on the short backoff, and counted. Everything
    else keeps the transport ladder it has always had; a failure that outlives
    both ladders comes back as an outcome of None, the skipped-pair lane, so
    one dead pair never costs the other pairs' records. A ConfigError (no key)
    is raised through, because no amount of waiting fixes it and a green Run
    over zero records is worse than a loud stop."""
    label = f"m6 {g.chunk.chunk_id}::{g.indicator_id}"
    notes: list[str] = []
    swallowed: list[Exception] = []
    retries = 0
    started = time.perf_counter()
    for attempt in range(1, RATE_LIMIT_ATTEMPTS + 1):
        try:
            outcome = _with_transport_retry(
                label, notes.append,
                map_gated_chunk,
                g, economy, config=config, engine=engine,
                completion_fn=completion_fn, indicator_defs=indicator_defs,
                config_dir=config_dir, pages=pages,
                required=False, reraise_rate_limit=True,
                on_error=swallowed.append,
            )
            return _PairResult(
                outcome=outcome,
                duration_ms=(time.perf_counter() - started) * 1000.0,
                rate_limit_retries=retries,
                notes=notes,
                skip_reason=(
                    None if outcome is not None else _transport_skip_reason(swallowed)
                ),
            )
        except Exception as e:  # noqa: BLE001 - only a rate limit is handled here
            if not is_rate_limit_error(e):
                raise
            if attempt == RATE_LIMIT_ATTEMPTS:
                notes.append(
                    f"{label}: rate limited {attempt} times; pair skipped (see audit_log)"
                )
                return _PairResult(
                    outcome=None,
                    duration_ms=(time.perf_counter() - started) * 1000.0,
                    rate_limit_retries=retries,
                    notes=notes,
                    skip_reason=f"rate limited {attempt} times; pair skipped",
                )
            wait = RATE_LIMIT_WAITS[attempt - 1]
            retries += 1
            notes.append(
                f"{label}: rate limited, retry {attempt}/{RATE_LIMIT_ATTEMPTS - 1}"
                f" in {wait}s"
            )
            _sleep(wait)
    raise AssertionError("unreachable: the rate-limit ladder always returns")


@contextmanager
def _pair_results(
    passed: list[GatedChunk], concurrency: int, work: Callable[[GatedChunk], _PairResult]
) -> Iterator[Iterator[_PairResult]]:
    """An iterator of _PairResult in PAIR ORDER, however many are in flight.

    concurrency 1 is the sequential path exactly as it always was: the work
    happens lazily, one pair at a time, as the consumer asks for it. Above 1 the
    pairs are submitted in order to a bounded pool and the futures are read back
    in that same order, so a fast pair finishing early waits its turn rather
    than jumping the queue. Leaving the block cancels whatever is still queued,
    which matters when the consumer raised: a Run that has already failed must
    not keep paying for calls."""
    if concurrency <= 1:
        yield (work(g) for g in passed)
        return
    executor = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="rc-map")
    try:
        # copy_context() per submission: a bare worker thread starts with an
        # EMPTY context, and the usage meter (engines.py) lives in one. Without
        # the copy every token of a parallel Run would go unmetered and the Run
        # Record would report a free Run.
        futures = [executor.submit(copy_context().run, work, g) for g in passed]
        yield (f.result() for f in futures)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def map_and_verify_pairs(
    storage: Storage,
    passed: list[GatedChunk],
    *,
    doc_id: str,
    economy: str,
    engine: Engine,
    config: PipelineConfig,
    report: RunReport,
    completion_fn: Callable | None = None,
    canonical: CanonicalText | None = None,
    config_dir=CONFIG_DIR,
    concurrency: int = 1,
    progress: Callable[[str], None] = lambda s: None,
    pair_progress: Callable[[str, int, int], None] | None = None,
    pair_outcome: Callable[[GatedChunk, str], None] | None = None,
) -> list[MappingRecord]:
    """M6 map + M7 verify over one Document's gate-passed pairs, returning every
    record produced (passed, dropped and no-evidence) in pair order.

    pair_outcome, when given, hears each pair once, in pair order, as soon as
    it is settled: mapped, not_applicable, dropped_by_proof or skipped (the
    Candidate outcomes of regcompass.run_progress). It only listens; the
    counters, records and audit rows are the same with or without it.

    The Engine calls may run several at a time (see the note above); everything
    else on this path stays on the calling thread. The counters on the report
    and the audit rows are identical to the one-at-a-time lane, which is what
    makes the two exports the same bytes."""
    records: list[MappingRecord] = []
    n_pairs = len(passed)
    if not n_pairs:
        return records
    progress(
        f"M6 map + M7 verify | {doc_id}: mapping {n_pairs} pairs"
        f" through Engine {engine.name}, concurrency {concurrency}"
    )

    # The Indicator registry is read and validated ONCE for the whole Document.
    # map_gated_chunk would otherwise re-read indicators.json per pair, which
    # was merely wasteful in a sequential Run and is four threads fighting over
    # the same file in a parallel one.
    indicator_defs = load_indicators(config_dir)
    # Each record cites the page its quote starts on, read off this page table.
    pages = canonical.pages if canonical is not None else None

    def work(g: GatedChunk) -> _PairResult:
        return _map_one_pair(
            g, economy=economy, config=config, engine=engine,
            completion_fn=completion_fn, indicator_defs=indicator_defs,
            config_dir=config_dir, pages=pages,
        )

    def settled(g: GatedChunk, outcome: str) -> None:
        if pair_outcome is not None:
            pair_outcome(g, outcome)

    with _pair_results(passed, concurrency, work) as results:
        stream = iter(results)
        for i, g in enumerate(passed, 1):
            # An error that no retry can fix (a missing key) comes back HERE,
            # attributed to its own pair because the stream is read in pair
            # order. It gets its m6 row saying so before it takes the Run down,
            # exactly as it did when the call sat inside the with-block.
            try:
                pair = next(stream)
            except StopIteration:  # pragma: no cover - one result per pair
                break
            except BaseException as exc:
                with log_stage(
                    storage, stage="m6_map", method=f"engine:{engine.name}",
                    input_data=f"{g.chunk.chunk_id}::{g.indicator_id}",
                ):
                    raise exc
            for line in pair.notes:
                progress(line)
            report.rate_limit_retries += pair.rate_limit_retries
            with log_stage(
                storage, stage="m6_map", method=f"engine:{engine.name}",
                input_data=f"{g.chunk.chunk_id}::{g.indicator_id}",
            ) as sr:
                # The call happened on a worker thread, so this block timed
                # nothing: give the row the Engine's own measured time.
                sr.duration_ms_override = pair.duration_ms
                o = pair.outcome
                if o is None:
                    # The note says WHICH wall the pair hit, because a Run that
                    # lost pairs to rate limiting wants a lower concurrency,
                    # and one that lost them to transport errors does not.
                    reason = pair.skip_reason or "pair skipped"
                    sr.decision = reason
                    report.notes.append(
                        f"{g.chunk.chunk_id}::{g.indicator_id}: {reason}"
                    )
                    report.n_dropped += 1
                    settled(g, SKIPPED)
                    continue
                sr.decision = f"{o.outcome} (attempts {o.attempts})"
                sr.output_data = o.record.verbatim_quote if o.record else None
            if o.outcome == "mapped" and o.record is not None:
                v = _with_transport_retry(
                    f"m7 {g.chunk.chunk_id}::{g.indicator_id}", progress,
                    verify_with_retry,
                    o.record, g, economy, config=config, engine=engine,
                    completion_fn=completion_fn, indicator_defs=indicator_defs,
                    storage=storage, canonical=canonical, config_dir=config_dir,
                    required=False,
                )
                if v is None:
                    report.notes.append(
                        f"{g.chunk.chunk_id}::{g.indicator_id}:"
                        f" verify skipped on persistent transport errors"
                    )
                    report.n_dropped += 1
                    settled(g, SKIPPED)
                    continue
                records.append(v.record)
                if v.outcome == "passed":
                    report.n_passed += 1
                    settled(g, MAPPED)
                else:
                    report.n_dropped += 1
                    # A quote that failed the proof is dropped, even when the
                    # retry then said the Piece does not apply: Prove counts it
                    # dropped, and so does this.
                    settled(g, DROPPED_BY_PROOF)
            elif o.outcome == "no_evidence" and o.record is not None:
                records.append(o.record)
                report.n_no_evidence += 1
                settled(g, NOT_APPLICABLE)
            else:
                # No attempt gave a quote that could be found word for word
                # in the Piece (or a well-formed answer at all).
                report.n_dropped += 1
                settled(g, DROPPED_BY_PROOF)
            if pair_progress is not None:
                pair_progress(doc_id, i, n_pairs)
            if i % 25 == 0 or i == n_pairs:
                progress(
                    f"M6 map + M7 verify | {doc_id}: {i}/{n_pairs} pairs,"
                    f" concurrency {concurrency}"
                )
    return records


# A stage that did not run is the most alarming thing on the progress log: the
# numbers jump (M1 to M4, nothing between) and a steward reading over a
# reviewer's shoulder sees a failure where there was none. So every stage the
# Run decided to skip says so in its own words, and says why.


def _ocr_skipped(doc_id: str) -> str:
    return (
        f"M2 ocr | {doc_id}: skipped, no OCR needed:"
        " this Document carries its own readable text layer"
    )


def _gloss_skipped(doc_id: str, reason: str) -> str:
    return f"M3 gloss | {doc_id}: skipped, {reason}"


def scan_flag_reason(quality) -> str | None:
    """Why a Document's text is flagged as unreliable, in plain words, or None
    when it is not. Only a scan read by OCR is ever flagged: the OCR verdict
    (manual_review) is the flag, and the Run still maps the Document."""
    if quality is None or not quality.manual_review:
        return None
    if quality.manual_review_reason:
        # The recorded reason names tesseract data; the screen gets the gist.
        return (
            "Read by OCR with no reading model for its language,"
            " so the text needs a person to check it"
        )
    reason = f"Read by OCR with {quality.mean_word_confidence:.0%} word confidence"
    if quality.dictionary_hit_rate is not None:
        reason += f" and {quality.dictionary_hit_rate:.0%} of words found in the dictionary"
    return reason


def _report_scan_flag(hook: RunProgress, doc_id: str, canonical: CanonicalText) -> None:
    reason = scan_flag_reason(canonical.ocr_quality)
    if reason is not None:
        hook.scan_flagged(doc_id, reason)


def run_document(
    storage: Storage,
    doc_id: str,
    pdf_path: Path,
    economy: str,
    pillars: tuple[int, ...],
    engine: Engine,
    run_id: str,
    indicators: tuple[str, ...] | None = None,
    config: PipelineConfig | None = None,
    completion_fn: Callable | None = None,
    embed_fn: Callable | None = None,
    config_dir=CONFIG_DIR,
    report: RunReport | None = None,
    progress: Callable[[str], None] = lambda s: None,
    source_url: str | None = None,
    title: str | None = None,
    filename_hint: str | None = None,
    pair_progress: Callable[[str, int, int], None] | None = None,
    language: str | None = None,
    concurrency: int | None = None,
    data_dir: Path | str | None = None,
    hook: RunProgress | None = None,
) -> list[MappingRecord]:
    """M1..M7 for one document, storage-attached. Returns every produced
    record (passed, dropped, and no-evidence) after persisting them under
    run_id, the Run that asked for them.

    indicators narrows the Gate to those exact ids (None = every Indicator of
    the Run's Pillars).

    source_url / title (off-corpus lanes: crawl e2e, map-pdf) are stored on the
    documents row so the export can synthesize a CorpusDoc; when title is None
    and filename_hint is given, a title is mechanically derived. The OCR quality
    proxies (M2) are threaded through the upsert so scanned documents populate
    the 5 documents columns (Stage A left this gap on the judge lanes).
    pair_progress(doc_id, i, total) is an optional per-pair tick for a live
    M6/M7 progress bar; the string narration goes through progress.

    language is the Document's Language as the Corpus recorded it. It decides
    three things and nothing else: the tesseract language string when M2 fires,
    whether M5 keeps its English keyword tier (see regcompass.languages), and
    whether M3 drafts English Glosses for the verified quotes. None keeps the
    English OCR and Gate behaviour, so a Document stored before the column was
    threaded through runs exactly as it did; the Gloss lane then reads the
    quotes themselves rather than assuming English.

    concurrency is how many Mapping calls may be in flight at once; None takes
    the Engine's own declared value.

    data_dir is the folder the Corpus's bytes hang off. It decides ONE thing:
    the spelling the documents row records for this file. Given, a file under
    that folder is written down as `<ECONOMY>/raw/<file>`, which is the only
    spelling that survives the database being opened on another machine, and a
    row that arrived here holding one machine's absolute path is rewritten to
    it. None keeps the path as given, which is right for a one-off file mapped
    where it lies and outside any Corpus.

    hook hears each Step start and finish with its counts, and every Map tick
    (regcompass.run_progress). None means nobody is listening."""
    config = config or load_pipeline(config_dir)
    report = report or RunReport(economy=economy, engine=engine.name)
    hook = hook if hook is not None else RunProgress()
    hook.step_started(doc_id, READ)
    raw = pdf_path.read_bytes()
    fmt = sniff_format(raw)
    ocr_languages = tesseract_languages(language, economy)
    policy = ocr_policy(language, economy)

    # The key this Document's canonical stream is stored under: its bytes, the
    # extraction logic and libraries, the tesseract language data and the OCR
    # ladder for its script. Everything that changes the text is in there, so a
    # stored stream that matches IS the stream this Run would have produced -
    # which is what makes skipping the read safe rather than merely fast.
    key = extraction_key(
        hashlib.sha256(raw).hexdigest(),
        ocr_languages=ocr_languages,
        policy=policy,
        config=config,
    )
    stored = load_extraction(storage, key, doc_id)

    if stored is not None:
        # M1 (and M2 where it fired) reused. The audit trail keeps one row per
        # stage per Document either way, with the same decision strings; only
        # the method says where the text came from, and it names the key.
        canonical = stored
        ocr_applied = canonical.ocr_applied
        with log_stage(
            storage, stage="m1_extract", method=f"reuse:{key.digest}", input_data=raw
        ) as sr:
            if ocr_applied:
                with log_stage(
                    storage, stage="m2_ocr", method=f"reuse:{key.digest}", input_data=raw
                ) as sr2:
                    sr2.output_data = canonical.full_text
                    sr2.decision = f"ocr applied: {len(canonical.full_text)} chars"
            sr.output_data = canonical.full_text
            sr.decision = (
                f"{canonical.extractor}-{canonical.extractor_version}:"
                f" {len(canonical.full_text)} chars, ocr={ocr_applied}"
            )
        progress(
            f"M1 extract | {doc_id}: reused the stored extraction"
            f" ({canonical.extractor}-{canonical.extractor_version},"
            f" {len(canonical.full_text):,} chars, ocr={ocr_applied});"
            " the Corpus had already read this Document"
        )
        hook.step_finished(
            doc_id, READ,
            {"pages": len(canonical.pages), "chars": len(canonical.full_text)},
        )
        hook.step_started(doc_id, SCAN_CHECK)
        hook.step_finished(doc_id, SCAN_CHECK, {"ocr_applied": int(ocr_applied)})
        _report_scan_flag(hook, doc_id, canonical)
        if not ocr_applied:
            progress(_ocr_skipped(doc_id))
        report.extraction[doc_id] = "reused"
        report.extraction_keys[doc_id] = key.digest
        report.extraction_provenance[doc_id] = extraction_provenance(canonical, key)
    else:
        # M1 extract (M2 OCR escalation when the born-digital text is unusable)
        with log_stage(storage, stage="m1_extract", method=f"{fmt}:auto", input_data=raw) as sr:
            canonical, stats = extract_with_stats(raw, fmt, doc_id)
            hook.step_finished(
                doc_id, READ,
                {"pages": len(canonical.pages), "chars": len(canonical.full_text)},
            )
            hook.step_started(doc_id, SCAN_CHECK)
            ocr_applied = False
            if fmt == "pdf" and should_ocr(canonical):
                from .ocr import ocr_document

                n_low = len(canonical.low_yield_pages)
                n_pg = len(canonical.pages)
                note = tesseract_language_note(language, economy)
                if note is not None:
                    progress(f"M2 ocr | {doc_id}: {note}")
                ladder = (
                    "RapidOCR fallback on low confidence"
                    if policy.rapidocr_escalation
                    else "no RapidOCR model for this script, tesseract only"
                )
                if not policy.dictionary_proxy:
                    ladder += ", English dictionary proxy not applicable"
                progress(
                    f"M2 ocr | {doc_id}: {n_low}/{n_pg} pages low-yield -> OCR escalation"
                    f" (tesseract {ocr_languages}, {ladder})"
                )
                with log_stage(
                    storage, stage="m2_ocr", method=f"tesseract:{ocr_languages}", input_data=raw
                ) as sr2:
                    # The Run hands the OCR engine the SAME PipelineConfig the
                    # ingest does. Without that, an operator who changed the
                    # rasterization DPI would get one text at ingest and
                    # another on the Run, from the same bytes.
                    canonical = ocr_document(
                        raw, doc_id, languages=ocr_languages, config=config,
                        policy=policy,
                    )
                    ocr_applied = True
                    sr2.output_data = canonical.full_text
                    sr2.decision = f"ocr applied: {len(canonical.full_text)} chars"
                q = canonical.ocr_quality
                if q is not None:
                    dict_hit = (
                        "n/a (non-Latin script)"
                        if q.dictionary_hit_rate is None
                        else f"{q.dictionary_hit_rate:.2f}"
                    )
                    progress(
                        f"M2 ocr | {doc_id}: {len(canonical.full_text)} chars,"
                        f" engine={canonical.extractor}, mean_conf={q.mean_word_confidence:.2f},"
                        f" dict_hit={dict_hit}, escalated={q.escalated_to_rapidocr},"
                        f" manual_review={q.manual_review} ({sr2.duration_ms / 1000:.1f}s)"
                    )
                    if q.manual_review_reason is not None:
                        progress(f"M2 ocr | {doc_id}: manual review: {q.manual_review_reason}")
            hook.step_finished(doc_id, SCAN_CHECK, {"ocr_applied": int(ocr_applied)})
            _report_scan_flag(hook, doc_id, canonical)
            sr.output_data = canonical.full_text
            sr.decision = (
                f"{canonical.extractor}-{canonical.extractor_version}:"
                f" {len(canonical.full_text)} chars, ocr={ocr_applied}"
            )
        progress(
            f"M1 extract | {doc_id}: {canonical.extractor}-{canonical.extractor_version},"
            f" {len(canonical.full_text):,} chars, ocr={ocr_applied} ({sr.duration_ms / 1000:.1f}s)"
        )
        if not ocr_applied:
            progress(_ocr_skipped(doc_id))
        # Stored for the next Run, and never allowed to be the thing that kills
        # this one: a Run that cannot write the cache has simply done the work.
        try:
            store_extraction(storage, key, canonical, source_format=fmt)
        except Exception as exc:  # noqa: BLE001 - the cache is an optimisation
            report.notes.append(f"{doc_id}: extraction not stored ({type(exc).__name__})")
        report.extraction[doc_id] = "extracted"
        report.extraction_keys[doc_id] = key.digest
        report.extraction_provenance[doc_id] = extraction_provenance(canonical, key)

    # Off-corpus lanes store source_url + a mechanically derived title so the
    # export can synthesize a CorpusDoc; the OCR quality proxies (when a scan
    # was OCR'd) populate the 5 documents columns Stage A left unfilled.
    doc_fields: dict[str, object] = {
        "full_text": canonical.full_text,
        "extractor": canonical.extractor,
        "extractor_version": canonical.extractor_version,
        "ocr_applied": int(ocr_applied),
        # Relative to the data folder whenever there is one, never this
        # machine's absolute path: a Run used to write back the path it had
        # just resolved, which turned every relative row it touched into a
        # local one and made the shipped database unusable anywhere else.
        "local_path": (
            str(pdf_path) if data_dir is None
            else storable_local_path(pdf_path, data_dir)
        ),
    }
    if source_url is not None:
        doc_fields["source_url"] = source_url
    doc_title = title
    if doc_title is None and filename_hint is not None:
        from .shortlist import derive_title

        doc_title = derive_title(raw, fmt, canonical.full_text, filename_hint)
    if doc_title is not None:
        doc_fields["title"] = doc_title
    # One helper writes these five columns wherever a Corpus row is written, so
    # the ingest lane and the Run lane cannot drift apart again.
    doc_fields.update(ocr_quality_columns(canonical))
    storage.upsert_document(
        doc_id,
        economy,
        hashlib.sha256(raw).hexdigest(),
        **doc_fields,
    )
    # The highlight geometry is persisted HERE, beside the stream it indexes.
    # Without it a Run started from the interface produces Mappings the audit
    # view cannot draw a rectangle for, and re-extracting on demand would mean
    # pdfplumber (or worse, OCR) over a whole act every time a reviewer opens a
    # record.
    storage.store_words(doc_id, canonical.words)

    # M4 chunk (deterministic; no LLM fallback on the judge path - a document
    # with no credible structure degrades to one chunk, which is honest)
    hook.step_started(doc_id, CUT)
    with log_stage(storage, stage="m4_chunk", method="deterministic", input_data=canonical.full_text) as sr:
        chunks, chunk_report = split_document(canonical, config)
        sr.output_data = str(len(chunks))
        sr.decision = f"style={chunk_report.style} chunks={len(chunks)} fallback={chunk_report.fallback_used}"
    storage.upsert_chunks(chunks)
    report.n_chunks += len(chunks)
    progress(
        f"M4 chunk | {doc_id}: {len(chunks)} chunks, style={chunk_report.style},"
        f" fallback={chunk_report.fallback_used} ({sr.duration_ms / 1000:.1f}s)"
    )
    hook.step_finished(doc_id, CUT, {"pieces": len(chunks)})
    # A Document no style profile matched degrades to one chunk of kind
    # `other`, and the Gate reads section chunks only. So the Run is already
    # over for this Document, and saying so HERE is the difference between an
    # operator who knows to look at the Document and one who spends the live
    # hour switching Engines. The wording is the same in three places on
    # purpose: this log line, the Run Record's warning, and the export refusal.
    if not any(c.chunk_kind == "section" for c in chunks):
        warning = no_section_chunks_warning(doc_id)
        progress(f"M4 chunk | {warning}")
        if warning not in report.warnings:
            report.warnings.append(warning)

    # M5 gate. The keyword tier is English (tokenizer, stopwords and Indicator
    # vocabularies alike), so a Document it cannot read is shortlisted by
    # meaning alone rather than excluded chunk by chunk.
    hook.step_started(doc_id, GATE)
    keyword_tier = keyword_tier_applies(
        language, canonical.full_text, config.gate_non_latin_share_max
    )
    gate_method = "bge-m3+bm25" if keyword_tier else "bge-m3"
    with log_stage(storage, stage="m5_gate", method=gate_method, input_data=doc_id) as sr:
        gated, gate_report = gate_document(
            chunks, config,
            embed_fn=embed_fn if embed_fn is not None else embed_fn_for(engine),
            storage=storage, config_dir=config_dir, pillars=pillars,
            indicators=indicators, keyword_tier=keyword_tier,
        )
        passed = [g for g in gated if g.gate_decision == "passed"]
        sr.output_data = str(len(passed))
        scope = (
            f"pillars {pillars}" if indicators is None
            else f"indicators {', '.join(indicators)}"
        )
        sr.decision = (
            f"passed={len(passed)} of {len(gated)} pairs ({scope},"
            f" mode={gate_report.gate_mode})"
        )
    storage.upsert_gate_scores(
        {(g.chunk.chunk_id, g.indicator_id): g.cosine_pillar for g in passed}
    )
    report.n_pairs_considered += len(gated)
    report.n_pairs_gated += len(passed)
    progress(
        f"M5 gate | {doc_id}: {len(passed)} gate-passed of {len(gated)} (chunk, indicator)"
        f" pairs, {scope} (bge-m3 + bm25, {sr.duration_ms / 1000:.1f}s)"
    )
    hook.step_finished(
        doc_id, GATE,
        {"pieces": len(chunks), "pairs": len(gated), "candidates": len(passed)},
    )

    # M6 map + M7 verify (shared attempt budget; verify logs its own m7 rows)
    n_pairs = len(passed)
    # Stamped HERE and not only in run_economy, because map-pdf calls this
    # function directly: without it that lane's Run Record would claim one call
    # at a time while four were in flight.
    n_workers = resolve_concurrency(engine, concurrency)
    report.engine_concurrency = n_workers

    def map_ticks(tick_doc: str, done: int, total: int) -> None:
        if pair_progress is not None:
            pair_progress(tick_doc, done, total)
        hook.map_progress(tick_doc, done, total)

    # Each Candidate, as it is settled, with the Gate's two scores for it: the
    # database keeps only the kept pairs' cosine, never the keyword score.
    lane = MEANING_AND_KEYWORDS if keyword_tier else MEANING_ONLY

    def candidate_settled(g: GatedChunk, outcome: str) -> None:
        hook.candidate(
            doc_id, g.chunk.chunk_id, g.indicator_id, float(g.cosine_pillar),
            float(g.bm25_indicator), lane, outcome,
            section=g.chunk.section_label, page=g.chunk.page_start,
        )

    # Map and Prove run pair by pair, interleaved, so the report's counters
    # before and after the call give this Document's share of each.
    before = (report.n_passed, report.n_no_evidence, report.n_dropped)
    hook.step_started(doc_id, MAP)
    records = map_and_verify_pairs(
        storage, passed, doc_id=doc_id, economy=economy, engine=engine,
        config=config, report=report, completion_fn=completion_fn,
        canonical=canonical, config_dir=config_dir, concurrency=n_workers,
        progress=progress, pair_progress=map_ticks, pair_outcome=candidate_settled,
    )
    hook.step_finished(doc_id, MAP, {"done": n_pairs, "total": n_pairs})
    hook.step_started(doc_id, PROVE)

    storage.upsert_mappings(records, run_id=run_id)
    # Saved, so each proven Mapping can now be opened while the Run goes on.
    for r in records:
        if r.verification_status == "passed":
            hook.mapping_added(doc_id, r.mapping_id, r.indicator_id, r.page_number)
    if n_pairs:
        progress(
            f"M6/M7 | {doc_id}: {report.n_passed} passed, {report.n_no_evidence} no-evidence,"
            f" {report.n_dropped} dropped so far"
        )
    hook.step_finished(
        doc_id, PROVE,
        {
            "proven": report.n_passed - before[0],
            "no_evidence": report.n_no_evidence - before[1],
            "dropped": report.n_dropped - before[2],
        },
    )

    # M3 gloss: a verified quote that is not English gets an English rendering
    # from the SAME Engine, in the same Run, stored beside the Mapping.
    hook.step_started(doc_id, GLOSS)
    n_glossed = gloss_document(
        storage, records, run_id=run_id, doc_id=doc_id, engine=engine, language=language,
        config=config, completion_fn=completion_fn, report=report, progress=progress,
    )
    hook.step_finished(doc_id, GLOSS, {"glossed": n_glossed})

    report.documents.append(doc_id)
    return records


def gloss_items(records: list[MappingRecord], language: str | None) -> list[tuple[str, str]]:
    """The (mapping_id, quote) pairs of one Document that need an English
    Gloss, in record order.

    The Document's Language decides: an English Document is never glossed, and
    every verified Mapping of a non-English one is. A Document whose Language
    was never recorded falls back to reading the quotes themselves, which is the
    same detector the Evidence Export's battery uses (widened to non-Latin
    scripts), so nothing that will need a translation column goes undrafted."""
    from .translate import needs_gloss

    passed = [r for r in records if r.verification_status == "passed"]
    if not passed:
        return []
    if is_english_language(language):
        return []
    if language:
        return [(r.mapping_id, r.verbatim_quote) for r in passed]
    return [
        (r.mapping_id, r.verbatim_quote) for r in passed if needs_gloss(r.verbatim_quote)
    ]


def gloss_document(
    storage: Storage,
    records: list[MappingRecord],
    *,
    run_id: str,
    doc_id: str,
    engine: Engine,
    language: str | None,
    config: PipelineConfig,
    completion_fn: Callable | None = None,
    report: RunReport,
    progress: Callable[[str], None] = lambda s: None,
) -> int:
    """Draft and store this Document's Glosses. Returns how many were stored.

    One call to the selected Engine covers the whole Document, so a Run over a
    non-English Economy costs one extra round trip per Document rather than one
    per Mapping. Every Gloss is stored UNREVIEWED and labelled: the Evidence
    Export drops the label only where a named person has approved the text.

    The lane never blocks a Run. A transport failure or a malformed answer
    lands as a Gloss with no English and the no-clear-translation-equivalent
    flag, which the export then treats as a missing translation."""
    from .translate import draft_document_glosses

    if not config.gloss_enabled:
        progress(_gloss_skipped(doc_id, "translation is off for this Run"))
        return 0
    items = gloss_items(records, language)
    if not items:
        progress(
            _gloss_skipped(
                doc_id,
                "nothing to translate: this Document is in English"
                if is_english_language(language)
                else "no verified quote needs an English rendering",
            )
        )
        return 0
    started = time.perf_counter()
    with log_stage(
        storage, stage="m3_gloss", method=f"engine:{engine.name}", input_data=doc_id
    ) as sr:
        glosses = draft_document_glosses(
            items, engine, config, completion_fn=completion_fn
        )
        for g in glosses:
            storage.gloss_set(
                run_id=run_id,
                mapping_id=g.key,
                english=g.english,
                engine=g.engine,
                source_language=g.source_language,
                uncertainty_flag=g.uncertainty_flag,
            )
        n_failed = sum(1 for g in glosses if g.english is None)
        sr.output_data = str(len(glosses))
        sr.decision = f"drafted={len(glosses) - n_failed} of {len(glosses)}, unreviewed"
    elapsed_ms = (time.perf_counter() - started) * 1000
    report.n_glossed += len(glosses)
    report.gloss_ms += elapsed_ms
    if n_failed:
        report.notes.append(
            f"{doc_id}: {n_failed} of {len(glosses)} Glosses could not be drafted"
        )
    progress(
        f"M3 gloss | {doc_id}: {len(glosses) - n_failed} of {len(glosses)} English"
        f" Glosses drafted by Engine {engine.name}, all unreviewed"
        f" ({elapsed_ms / 1000:.1f}s)"
    )
    return len(glosses)


def run_economy(
    storage: Storage,
    economy: str,
    pillars: tuple[int, ...],
    engine: Engine,
    config_dir=CONFIG_DIR,
    completion_fn: Callable | None = None,
    embed_fn: Callable | None = None,
    data_dir: Path = Path("data"),
    indicators: list[str] | None = None,
    progress: Callable[[str], None] = lambda s: None,
    pair_progress: Callable[[str, int, int], None] | None = None,
    concurrency: int | None = None,
    run_id: str | None = None,
    report_watch: Callable[[RunReport], None] | None = None,
    hook: RunProgress | None = None,
) -> RunReport:
    """The full judge-path Run for one Economy over its CORPUS: M1..M7 per
    Document, then M8 reconcile over this Run's passed records.

    A Run never fetches. Its Documents come from the Corpus that
    Discovery built, so a second Run with another Engine reads exactly the same
    Documents and its Run Record honestly says Documents fetched 0. That second
    Run now files its Mappings under its own id beside the first Run's instead
    of overwriting them, which is what makes comparing two Engines possible.

    indicators narrows the Run to those exact Indicator ids, which must belong
    to the Run's Pillars; the list is stored on the Run Record.

    concurrency overrides how many Mapping calls the Run keeps in flight; None
    takes the selected Engine's own declared value. Whatever it is, it lands on
    the Run Record, because a Run's wall time cannot be read without it.

    The whole lane runs inside a Run Record: the row is filed before the first
    model call and closed with the measured tokens and cost. `run_id` adopts a
    record the caller already opened instead of opening one, which is how the
    server guarantees a Run it has answered "started" to exists in the table
    even if the process dies the next second.

    report_watch receives the report the moment it exists, so a reader on
    another thread can follow its counters while the Run fills them in (the
    server's live funnel); the Run itself never waits on it.

    hook hears every Step of every Document, then Reconcile, and a failure
    inside the Run Record is reported to it once, naming the Document and
    Step it happened in (regcompass.run_progress)."""
    from .config import narrow_indicators

    report = RunReport(economy=economy, engine=engine.name)
    if report_watch is not None:
        report_watch(report)
    n_workers = resolve_concurrency(engine, concurrency)
    report.engine_concurrency = n_workers
    # Refused BEFORE the Run Record is opened: a typo is not a Run that failed.
    chosen = narrow_indicators(indicators, pillars, config_dir)
    corpus = corpus_for_run(
        storage, economy, data_dir, config_dir=config_dir, progress=progress
    )
    with record_run(
        storage, report, economy=economy, pillars=pillars, engine=engine,
        indicators=None if chosen is None else list(chosen), run_id=run_id,
    ) as run_id:
        config = load_pipeline(config_dir)
        progress(
            f"M0 corpus | {economy}: {len(corpus)} Document(s) in the Corpus; a Run"
            " reads them and makes no request"
        )
        if chosen is not None:
            progress(
                f"M0 scope | {economy}: narrowed to Indicator(s)"
                f" {', '.join(chosen)} of Pillar(s)"
                f" {', '.join(str(p) for p in pillars)}"
            )
        tracker = StepTracker(hook)
        try:
            for row, path in corpus:
                run_document(
                    storage, row["document_id"], path, economy, pillars, engine, run_id,
                    indicators=chosen,
                    config=config, completion_fn=completion_fn, embed_fn=embed_fn,
                    config_dir=config_dir, report=report, progress=progress,
                    pair_progress=pair_progress,
                    source_url=row["source_url"], title=row["title"],
                    language=row["language"], concurrency=n_workers,
                    data_dir=data_dir, hook=tracker,
                )

            reconcile_economy(
                storage, economy, engine, report, run_id=run_id,
                config_dir=config_dir, completion_fn=completion_fn, progress=progress,
                hook=tracker,
            )
        except Exception as exc:
            tracker.failed_here(exc)
            raise
    return report


def reconcile_economy(
    storage: Storage,
    economy: str,
    engine: Engine,
    report: RunReport,
    *,
    run_id: str,
    config_dir=CONFIG_DIR,
    completion_fn: Callable | None = None,
    progress: Callable[[str], None] = lambda s: None,
    hook: RunProgress | None = None,
) -> None:
    """M8 reconcile over ONE Run's passed records for one Economy. The judge
    path runs without M12 classifications: the pure legal-hierarchy ladder, as
    documented. Crawled or user-supplied documents missing from corpus.yaml
    still reconcile: their law name falls back inside reconcile_records to the
    readable document id.

    The Run scope is the point, and it is required rather than resolved. Exactly
    one record per (economy, indicator) group may be controlling; reconciling
    two Engines' records together would crown one winner across both and
    silently attribute the loser's evidence to the wrong Engine. The id is
    written into the m8_reconcile audit row."""
    hook = hook if hook is not None else RunProgress()
    hook.step_started(None, RECONCILE)
    all_passed = storage.load_mappings(
        economy=economy, verification_status="passed", run_id=run_id
    )
    corpus = load_corpus(config_dir)
    law_names = {k: v.law_name for k, v in corpus.items() if v.economy == economy}
    progress(
        f"M8 reconcile | {economy}: legal-hierarchy ladder over {len(all_passed)}"
        f" passed records of Run {run_id}"
    )
    with log_stage(
        storage, stage="m8_reconcile", method=f"engine:{engine.name}", input_data=economy
    ) as sr:
        groups, updated = _with_transport_retry(
            f"m8 {economy}", progress,
            reconcile_records,
            all_passed, law_names, engine=engine,
            completion_fn=completion_fn, storage=storage, config_dir=config_dir,
        )
        sr.output_data = str(len(groups))
        sr.decision = f"{len(updated)} records into {len(groups)} groups (run {run_id})"
    storage.upsert_mappings(updated, run_id=run_id)
    report.n_groups = len(groups)
    progress(
        f"M8 reconcile | {economy}: {len(updated)} records into {len(groups)} groups"
        f" ({sr.duration_ms / 1000:.1f}s)"
    )
    hook.step_finished(
        None, RECONCILE,
        {"passed": len(all_passed), "records": len(updated), "groups": len(groups)},
    )


class NoCompletedRunError(RuntimeError):
    """This database files Run Records but holds no COMPLETED Run, so there is
    nothing an unqualified export could honestly mean. A missing prerequisite,
    not a failure: the caller names a Run or finishes one."""


def _resolved_run(storage: Storage, run_id: str | None, economy: str | None = None):
    """The Run a caller meant. An explicit id wins; otherwise the newest
    completed Run.

    Two different silences, kept apart. A database with NO Run Records at all
    (a golden seeded from checkpoints, a hand-built fixture) predates Run
    ownership, and None reads every row exactly as it did before. A database
    that DOES file Run Records but has only running or failed ones is a
    different situation entirely: falling back to every row there would export a
    half-written Run, or several Runs mixed together, under a line claiming the
    database has no Run Records. Say so instead."""
    if run_id is not None:
        return run_id
    latest = storage.latest_run_id(economy)
    if latest is not None:
        return latest
    if storage.runs_list(economy=economy, limit=1):
        scope = "" if economy is None else f" for {economy}"
        raise NoCompletedRunError(
            f"no completed Run in this database{scope}: every Run Record is"
            " running, interrupted or failed. Name one with --run-id (see"
            " `regcompass runs`), or finish a Run first."
        )
    return None


def _no_mappings_reason(storage: Storage, run_id: str) -> str:
    """Why a completed Run has no Mappings, in the words that send the operator
    the right way.

    Two causes look identical from the export's side and lead opposite ways. If
    every Document of the Run reached the Gate with no section chunk, nothing
    was ever asked of the Engine and switching Engines changes nothing; the
    chunk step is the thing to look at. Otherwise the Engine really did read
    candidate provisions and select nothing, and the other Engine is worth a
    try. The Run's own chunk rows decide which, so the answer survives a
    restart and does not depend on anyone still holding the report."""
    try:
        record = storage.run_get(run_id)
        details = (record or {}).get("details") or {}
        doc_ids = [str(d) for d in (details.get("documents") or [])]
        counts = storage.section_chunk_counts(doc_ids) if doc_ids else {}
    except sqlite3.Error:  # pragma: no cover - a database with no chunks table
        doc_ids, counts = [], {}
    if doc_ids:
        unstructured = [d for d in doc_ids if counts.get(d, 0) == 0]
        if len(unstructured) == len(doc_ids):
            lead = (
                f"Run {run_id} completed with no Mappings, and the cause is the"
                " chunk step rather than the Engine."
            )
            return " ".join([lead, *(no_section_chunks_warning(d) for d in unstructured)])
    return (
        f"Run {run_id} completed with no Mappings: this Engine selected no"
        " evidence for the Indicators it was given, so there is nothing to"
        " export. Try the other Engine, or another Pillar or Economy."
    )


def synthesize_offcorpus_docs(
    document_meta: dict[str, dict], config_dir=CONFIG_DIR
) -> dict[str, SyntheticDoc]:
    """A SyntheticDoc for every document in the DB that is NOT in corpus.yaml
    (a live crawl catch, a map-pdf input), built from the documents row alone so
    the export never KeyErrors on an off-corpus document_id. law_name is the
    mechanically derived title (best effort); source_url is the stored URL (the
    crawl record, so a portal catch stays whitelist-satisfied). A document with
    no usable https URL is skipped: no shippable row can be built for it.

    allow_any_host is False for a discovered document: the whitelist exemption
    is a human opt-in that only an explicit override SyntheticDoc (map-pdf
    --allow-any-host) may carry, so a plain re-export of an off-whitelist
    leftover fails the battery instead of silently self-approving. A MANUALLY
    ADDED document (source kind 'manual') is the one exception, and it is the
    same opt-in rather than a hole in it: a person typed that Source URL and
    vouched for it, the Portal may have no verified host at all, and both facts
    are disclosed in the row's Notes. A manually added Document whose host IS
    whitelisted needs no exemption, so it carries none and is not described as
    off-whitelist.

    Law Name, Law Number / Ref and Last Amended come from
    config/law_metadata.yaml when it has an entry for the document id; a law
    name recorded there is a person's reading of the official text, so the row
    no longer says it was derived from the title. Without an entry the name is
    the derived title and the other two stay blank."""
    corpus = load_corpus(config_dir)
    law_metadata = load_law_metadata(config_dir)
    portals = load_portals(config_dir)
    out: dict[str, SyntheticDoc] = {}
    for doc_id, meta in document_meta.items():
        if doc_id in corpus:
            continue
        src = str(meta.get("source_url") or "")
        econ = meta.get("economy")
        if not re.match(r"^https?://", src) or not econ:
            continue
        known = law_metadata.get(doc_id)
        law_name = (
            (known.law_name if known else None)
            or (meta.get("title") or "").strip()
            or doc_id.removeprefix("doc_").replace("_", " ")
        )
        manual = meta.get("source_kind") == "manual"
        portal = portals.get(econ)
        whitelisted = portal is not None and urlparse(src).netloc in portal.hosts
        out[doc_id] = SyntheticDoc(
            CorpusDoc(
                economy=econ, law_name=law_name, source_url=src, url_is_direct=True,
                law_number_ref=known.law_number_ref if known else None,
                last_amended=known.last_amended if known else None,
            ),
            law_name_mechanical=not (known and known.law_name),
            allow_any_host=manual and not whitelisted,
            manual_added=manual,
        )
    return out


def engine_of_record(storage: Storage, config_dir=CONFIG_DIR) -> Engine:
    """The Engine that produced the records in this database, read off the
    audit trail the run itself wrote (m6_map / m8_reconcile rows carry
    method="engine:<name>"). The most recent row wins, so re-running an economy
    on another Engine re-stamps the export. A database with no such row (a
    golden seeded straight from checkpoints, a pre-registry db) falls back to
    the configured default Engine. An Engine name the registry no longer
    declares also falls back: the export never invents a model."""
    row = storage.conn.execute(
        "SELECT method FROM audit_log WHERE method LIKE 'engine:%'"
        " ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is not None:
        try:
            return resolve_engine(row["method"].split(":", 1)[1], config_dir)
        except ValueError:
            pass
    return resolve_engine(None, config_dir)


@dataclass
class PageMove:
    """One Mapping whose cited page the repair corrects."""

    run_id: str
    mapping_id: str
    old: int | None
    new: int


@dataclass
class PageRepairReport:
    """What repair_page_numbers found. examined counts the Mappings that carry
    a quote; a no-evidence row carries none and is counted in no_quote. Of the
    examined, moved changed page and undetermined could not be placed (both
    kinds left as they were)."""

    examined: int = 0
    moved: int = 0
    undetermined: int = 0
    no_quote: int = 0
    moves: list[PageMove] = field(default_factory=list)


def _run_recorded(storage: Storage, run_id: str) -> bool:
    """Whether the database holds a Run Record under this id. A database from
    before Run Records existed has no runs table and so holds none."""
    try:
        row = storage.conn.execute(
            "SELECT 1 FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def repair_page_numbers(
    storage: Storage, *, run_id: str | None = None, dry_run: bool = False
) -> PageRepairReport:
    """Re-derive the cited page of stored Mappings from where each Verbatim
    Quote sits in its Document's stored text, the same rule the Map step now
    applies at creation (chunk.quote_page). Mappings stored before that rule
    carry the page their Piece starts on, which can be pages before the quote.

    Deterministic and offline: no Engine call. Only page_number is written, and
    only where it differs, so a second pass moves nothing. A no-evidence row
    has no quote to place and is only counted. A Mapping whose Piece or
    Document the database no longer holds, or whose Document's text has no
    stored page table, is counted undetermined and left as it was. run_id
    narrows the pass to one Run (a LookupError when the database knows no such
    Run); dry_run counts without writing."""
    sql = (
        "SELECT run_id, mapping_id, chunk_id, document_id, verbatim_quote, page_number"
        " FROM mappings"
    )
    params: tuple = ()
    if run_id is not None:
        sql += " WHERE run_id = ?"
        params = (run_id,)
    rows = storage.conn.execute(sql + " ORDER BY run_id, mapping_id", params).fetchall()
    if run_id is not None and not rows and not _run_recorded(storage, run_id):
        raise LookupError(f"Run {run_id} not found in this database")
    report = PageRepairReport()
    texts: dict[str, str] = {}
    page_tables: dict[str, list[PageSpan]] = {}
    for row in rows:
        if not row["verbatim_quote"]:
            report.no_quote += 1
            continue
        report.examined += 1
        doc_id = row["document_id"]
        chunk = storage.chunk_row(row["chunk_id"])
        if doc_id not in texts:
            texts[doc_id] = storage.document_full_text(doc_id)
            page_tables[doc_id] = storage.document_pages(doc_id)
        text, pages = texts[doc_id], page_tables[doc_id]
        if chunk is None or not pages or not text:
            report.undetermined += 1
            continue
        start, end = chunk["char_start"], chunk["char_end"]
        new = quote_page(pages, text[start:end], start, row["verbatim_quote"], None)
        if new is None:
            report.undetermined += 1
            continue
        if new != row["page_number"]:
            report.moved += 1
            report.moves.append(
                PageMove(row["run_id"], row["mapping_id"], row["page_number"], new)
            )
    if not dry_run and report.moves:
        storage.conn.executemany(
            "UPDATE mappings SET page_number = ? WHERE run_id = ? AND mapping_id = ?",
            [(m.new, m.run_id, m.mapping_id) for m in report.moves],
        )
        storage.conn.commit()
    return report


def export_from_db(
    storage: Storage,
    outdir: Path,
    config_dir=CONFIG_DIR,
    liveness_fn: Callable[[str], bool] | None = None,
    check_pointers: bool = True,
    synthetic_docs: dict[str, SyntheticDoc] | None = None,
    run_id: str | None = None,
    reviews: dict[str, str] | None = None,
    corrections: dict[str, Review] | None = None,
    progress: Callable[[str], None] = lambda s: None,
) -> ExportResult:
    """M9 from the database alone (no model calls): records, chunk texts and
    gate cosines all come back off storage; the pointer gate re-derives every
    controlling label against the stored canonical stream before anything is
    written; liveness is OFF by default (the judge path must work offline).

    ONE Run is exported. Naming it is how two Engines' answers become two
    comparable submissions rather than one mixed file. A caller that names none
    gets the newest completed Run, and the resolved id goes into the m9_export
    audit row so the file can always be traced back to the Run that made it; a
    database with no Run Records at all (a golden seeded from checkpoints)
    exports every row exactly as it did before.

    Off-corpus documents (crawl e2e, map-pdf) are handled by synthesizing a
    CorpusDoc per document_id absent from corpus.yaml; caller-supplied
    synthetic_docs (map-pdf's explicit law name / number / date, honestly not
    marked mechanical) override the DB-derived ones.

    reviews (optional): mapping_id -> review_status. Given, the human review
    gate applies exactly as it does on the bundle lane, so the interface's
    Export button ships the same accepted-only file whichever source it read.

    corrections (optional): mapping_id -> the Review Decision of each corrected
    Mapping, which then ships under the reviewer's Indicator (export_all)."""
    run_id = _resolved_run(storage, run_id)
    records = storage.load_mappings(run_id=run_id)
    if not any(r.verification_status == "passed" for r in records):
        # Three different silences, and only one of them means "you forgot to
        # run". A completed Run that mapped nothing is an ANSWER: the Engine
        # found no evidence in this Corpus for these Indicators. Sending the
        # operator back to `regcompass run` sent them round a loop they had
        # just finished (paid smoke, 16 Sep 2026, Engine A on SG Pillar 6).
        if run_id is None:
            raise RuntimeError(
                "no passed records in the database: run `regcompass run` first"
            )
        if not records:
            raise RuntimeError(_no_mappings_reason(storage, run_id))
        raise RuntimeError(
            f"Run {run_id} produced {len(records)} Mapping(s) but none passed"
            " verification, so there is nothing to export"
        )
    progress(
        "M9 export | exporting Run"
        f" {run_id if run_id is not None else '(every record: this database has no Run Records)'}"
    )
    texts = storage.chunk_texts()
    cosines = storage.load_gate_scores()
    # The Pillars this Run was asked to search, off its own Run Record. Gate
    # scores are kept for every Run on the database, so reading Pillars off
    # them (or off the records alone) could write absence rows for a Pillar
    # this Run never searched.
    run_record = storage.run_get(run_id) if run_id is not None else None
    run_pillars = (
        tuple(sorted(set(run_record["pillars"])))
        if run_record and run_record.get("pillars")
        else None
    )

    full_texts: dict[str, str] = {}

    def _loader(doc_id: str) -> str:
        if doc_id not in full_texts:
            full_texts[doc_id] = storage.document_full_text(doc_id)
        return full_texts[doc_id]

    # Quote-anchored section-label repair, same rule set the repro lane runs
    # before ITS pointer gate (scripts/run_repro.py): the label is re-derived
    # at the quote's exact position in the stored canonical stream, so the
    # fail-closed gate below judges repaired labels in BOTH lanes, not raw
    # chunk labels in one and repaired ones in the other.
    # Deterministic and free: recomputed every export, decisions audit-logged.
    by_doc: dict[str, list] = {}
    for rec in records:
        by_doc.setdefault(rec.chunk_id.rsplit(":", 1)[0], []).append(rec)
    repaired_by_id: dict[str, MappingRecord] = {}
    n_repaired = 0
    with log_stage(
        storage, stage="m9_label_repair", method="quote-anchored", input_data=str(len(records))
    ) as sr:
        for doc_id, doc_recs in by_doc.items():
            fixed, decisions = repair_section_labels(doc_recs, _loader(doc_id), texts)
            n_repaired += sum(1 for d in decisions if d["outcome"] == "repaired")
            repaired_by_id.update({r.mapping_id: r for r in fixed})
        sr.output_data = str(len(records))
        sr.decision = f"repaired={n_repaired} of {len(records)} labels"
    progress(
        f"M9 label-repair | quote-anchored: repaired {n_repaired} of {len(records)}"
        f" section labels ({sr.duration_ms / 1000:.1f}s)"
    )
    # Original load_mappings order preserved (deterministic CSV row order).
    records = [repaired_by_id.get(r.mapping_id, r) for r in records]

    if check_pointers:
        failures, notes = pointer_gate(records, _loader, texts)
        progress(
            f"M9 pointer-gate | controlling rows checked against their quotes:"
            f" {len(failures)} failures, {len(notes)} fail-closed notes"
        )
        if failures:
            raise RuntimeError(
                "pointer gate: controlling labels contradict their quotes: "
                + "; ".join(failures)
            )

    # Coverage stats per economy, rebuilt from the database
    coverage: dict[str, dict] = {}
    for eco in sorted({r.economy for r in records}):
        n_docs = storage.conn.execute(
            "SELECT COUNT(*) AS n FROM documents WHERE economy = ?", (eco,)
        ).fetchone()["n"]
        doc_ids = [
            r["document_id"]
            for r in storage.conn.execute(
                "SELECT document_id FROM documents WHERE economy = ?", (eco,)
            ).fetchall()
        ]
        n_sections = storage.conn.execute(
            f"SELECT COUNT(*) AS n FROM chunks WHERE document_id IN ({', '.join('?' * len(doc_ids))})",
            doc_ids,
        ).fetchone()["n"]
        in_scope = set(doc_ids)
        n_pairs = sum(
            1 for (chunk_id, indicator_id) in cosines
            if chunk_id.rsplit(":", 1)[0] in in_scope
            and (run_pillars is None or pillar_of(indicator_id) in run_pillars)
        )
        coverage[eco] = {
            "law": f"{n_docs} legislative documents in this Economy's Corpus",
            "sections": n_sections,
            "pairs_gated": n_pairs,
        }

    # submission.json inputs from the database alone (no model calls): per-doc
    # metadata (source_pdf_path + OCR quality proxies) and run-level stage
    # timing. document_meta() is tolerant of a pre-migration DB (emits nulls).
    document_meta = storage.document_meta()
    processing_time = {
        "granularity": "run",
        "unit": "ms",
        "stages": storage.audit_durations(),
        "note": (
            "run-level totals per stage from audit_log; per-document timing is"
            " not recorded (audit_log has no document_id)"
        ),
    }

    # Off-corpus documents get a synthetic CorpusDoc so the export never
    # KeyErrors on their document_id; caller-supplied overrides (map-pdf) win.
    merged_synthetic = synthesize_offcorpus_docs(document_meta, config_dir)
    if synthetic_docs:
        merged_synthetic.update(synthetic_docs)
    if merged_synthetic:
        progress(
            f"M9 export | {len(merged_synthetic)} off-corpus document(s) carried by"
            f" synthetic corpus entries (crawl / user-supplied lane)"
        )

    with log_stage(storage, stage="m9_export", method="13-column+battery", input_data=str(len(records))) as sr:
        result = export_all(
            outdir,
            records,
            chunk_text_lookup=texts,
            gate_cosine_lookup=cosines,
            coverage_stats=coverage,
            liveness_fn=liveness_fn or (lambda url: True),
            config_dir=config_dir,
            document_meta=document_meta,
            processing_time=processing_time,
            generated_at=utc_now_iso(),
            synthetic_docs=merged_synthetic or None,
            engine=engine_of_record(storage, config_dir),
            reviews=reviews,
            corrections=corrections,
            # This Run's Glosses, looked up BY RUN: a Gloss drafted in another
            # Run must never be read onto this Run's identically-named Mapping.
            glosses=storage.glosses_for_run(run_id),
            run_pillars=run_pillars,
        )
        sr.output_data = result.csv_path.read_bytes()
        sr.decision = (
            f"battery green: {len(result.rows)} rows"
            f" (run {run_id if run_id is not None else 'unscoped'})"
        )
    progress(
        f"M9 export | battery GREEN: {len(result.rows)} rows -> {result.csv_path}"
        f" (+ submission.json, supplementary.json, {len(result.working_json_paths)}"
        f" working JSONs) ({sr.duration_ms / 1000:.1f}s)"
    )
    if result.glosses_unavailable:
        # Said out loud because the alternative is a green Export whose
        # translations are all missing: the rows disclose it one by one, and
        # nobody opens every cell.
        progress(
            f"M9 gloss | {result.glosses_unavailable} of {len(result.rows)} row(s)"
            " carry no English rendering: the translation lane produced none for"
            " those quotes, and each row says so in its Verbatim English column"
        )
    return result


@dataclass
class E2EReport:
    """What one `regcompass e2e` invocation did, end to end: a Discovery
    followed by a Run over the Corpus it built, then the export."""

    economy: str
    crawl_discovered: int = 0
    crawl_fetched: int = 0
    crawl_deduplicated: int = 0
    crawl_failed: int = 0
    documents_mapped: list[str] = field(default_factory=list)
    discovery_run_id: str | None = None
    run: RunReport | None = None
    export: ExportResult | None = None


def run_e2e(
    storage: Storage,
    economy: str,
    pillars: tuple[int, ...],
    engine: Engine,
    data_dir: Path,
    outdir: Path,
    *,
    config_dir=CONFIG_DIR,
    max_documents: int | None = None,
    completion_fn: Callable | None = None,
    embed_fn: Callable | None = None,
    fetcher: Callable | None = None,
    limiter=None,
    get_json=None,
    liveness_fn: Callable[[str], bool] | None = None,
    seeds=None,
    refresh: bool = False,
    indicators: list[str] | None = None,
    progress: Callable[[str], None] = lambda s: None,
    pair_progress: Callable[[str, int, int], None] | None = None,
    concurrency: int | None = None,
    report_watch: Callable[[RunReport], None] | None = None,
    hook: RunProgress | None = None,
) -> E2EReport:
    """Discovery then a Run, in that order, with the export at the end: the
    convenience wrapper over the two separated lanes, kept so the
    Docker health check and the reproduction path stay one scriptable command.

    Discovery is the only step that touches the internet; it fills the
    Economy's Corpus. The Run then reads that Corpus and makes no request, which
    is why running this twice with two Engines fetches only once.

    fetcher / limiter / get_json are injectable so tests drive Discovery over
    recorded Portal answers and make NO live call; the CLI leaves them None to
    use the Economy's configured strategy and the robots-aware politeness floor.
    """
    from .discovery import discover_economy

    report = E2EReport(economy=economy)

    # Two records, not one. Discovery files its own, carrying the count of
    # Documents fetched; the Run that follows files a Run Record whose
    # documents_fetched is 0 by construction. That separation is the
    # whole point: the second Engine's pass over the same Corpus fetches
    # nothing, and the table shows it.
    discovery = discover_economy(
        economy, storage, refresh=refresh, config_dir=config_dir, data_dir=data_dir,
        seeds=seeds, fetch=fetcher, get_json=get_json, limiter=limiter,
        max_documents=max_documents, progress=progress,
    )
    report.discovery_run_id = discovery.run_id
    report.crawl_discovered = discovery.discovered
    report.crawl_fetched = discovery.fetched
    report.crawl_deduplicated = discovery.deduplicated
    report.crawl_failed = discovery.failed
    for miss in discovery.misses:
        progress(f"M10 crawl | miss: {miss}")

    if not storage.corpus_documents(economy):
        raise RuntimeError(
            f"Discovery fetched no usable {economy} Document"
            " (the Portal may be refusing us; see the crawl_manifest table)"
        )

    run_report = run_economy(
        storage, economy, pillars, engine, config_dir=config_dir,
        completion_fn=completion_fn, embed_fn=embed_fn, data_dir=data_dir,
        indicators=indicators, progress=progress, pair_progress=pair_progress,
        concurrency=concurrency, report_watch=report_watch, hook=hook,
    )
    report.run = run_report
    report.documents_mapped = list(run_report.documents)

    # The export ships THIS Run, not whatever else the database holds: an e2e
    # over a database that already carries another Engine's Run must not mix
    # the two into one submission file.
    report.export = export_from_db(
        storage, outdir, config_dir=config_dir, run_id=run_report.run_id,
        liveness_fn=liveness_fn, progress=progress,
    )
    return report
