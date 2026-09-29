"""Full three-economy ground-truth reproduction over the real crawled corpus
(the post-M11 step): M1/M2 -> M4 -> M5 -> M6 -> M7 per
economy, then M8 reconcile + M9 export + headline GT spot checks.

Everything is checkpointed under data/repro/ and resume-safe:
- canonical/, m4/, m5/: one .json.gz per document (skipped when present).
- m6/, m7/: one .jsonl per document, appended per (chunk, indicator) pair as
  it completes, so a crash never repays finished mapper calls. Malformed
  trailing lines (a kill mid-write) are dropped on load.

The canonical stream is re-extracted from the exact crawled bytes with the
same pinned extractors the M11 ingest used and ASSERTED byte-identical to the
stream stored in the documents table: the mapper and the substring check see
the one true stream, mechanically.

Usage (mapper + fallbacks need the key; gate needs ollama serve):
  set -a; source .env; set +a
  uv run python scripts/run_repro.py run --economy MY [--max-docs N] [--through m7]
  uv run python scripts/run_repro.py classify      # M12 over all verified rows
  uv run python scripts/run_repro.py finish        # M8 + M9 + GT spot checks

M12 (classify) checkpoints like M6: m12/<doc>.jsonl appended per record, keyed
by mapping_id, so a resume never repays a finished classification call. finish
uses the classifications when m12/ exists (rubric-derived scores); without m12/
it falls back to the presence-based scores.
"""

import argparse
import gzip
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

import httpx

from regcompass.chunk import _default_completion, repair_section_labels, split_document
from regcompass.classify import ProvisionClassification, classify_record
from regcompass.config import load_corpus, load_portals
from regcompass.contracts import CanonicalText, Chunk, GatedChunk, MappingRecord
from regcompass.export import derive_scores, export_all, pointer_gate
from regcompass.extract import extract_with_stats, sniff_format
from regcompass.gate import gate_document
from regcompass.map import ConfigError, map_gated_chunk
from regcompass.reconcile import reconcile_records
from regcompass.shortlist import should_ocr
from regcompass.storage import Storage, utc_now_iso
from regcompass.verify import verify_with_retry

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data/regcompass.db"
REPRO = ROOT / "data/repro"
ECONOMIES = ("SG", "AU", "MY")
WORKERS = 8

# Script-level retry for TRANSPORT failures only (TLS resets, connection
# drops on multi-hour runs). This does NOT touch the pinned-to-0 SDK retries:
# a failed handshake produced no model output, so attempt counting is intact,
# and every retry is logged loudly. A pair still failing after the ladder is
# SKIPPED (not written), so a resume run retries it; the stage then raises so
# the gap is never silent.
TRANSPORT_RETRIES = 4


def _with_transport_retry(label: str, fn, *args):
    for i in range(TRANSPORT_RETRIES):
        try:
            return fn(*args)
        except ConfigError:
            # A missing key cannot be retried into existence: fail the run
            # loudly instead of skip-and-logging every pair (mirrored from
            # the judge lane's ladder in pipeline.py).
            raise
        except Exception as e:  # noqa: BLE001 - content failures never raise here
            wait = 5 * 2**i
            print(f"  {label}: transport error {type(e).__name__}, retry {i + 1}/{TRANSPORT_RETRIES} in {wait}s")
            time.sleep(wait)
    print(f"  {label}: transport error persisted after {TRANSPORT_RETRIES} retries, pair skipped this run")
    return None

# The headline Round 1 Database spots the reproduction must hit.
# SG 6.3: the CII-designation mislabel shipped 1.0 against ground truth 0.
EXPECTED_SPOTS = {
    ("AU", "6.4"): 1.0,
    ("AU", "7.3"): 1.0,
    ("SG", "6.1"): 0.0,
    ("SG", "6.3"): 0.0,
    ("SG", "7.2"): 0.0,
    ("SG", "6.2"): 0.5,
    ("MY", "6.1"): 0.0,
    ("MY", "7.4"): 1.0,
    ("MY", "7.5"): 1.0,
}


def save_gz(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as f:
        json.dump(obj, f)


def load_gz(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def load_jsonl(path: Path) -> list[dict]:
    """Checkpointed pair outcomes; a truncated final line is dropped."""
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            print(f"  warning: dropped malformed checkpoint line in {path.name}")
    return rows


def open_checkpoint_append(path: Path):
    """Open a JSONL checkpoint for append, first sealing off any partial
    final line (a kill mid-write leaves bytes with no trailing newline;
    appending straight onto them would weld two records into one corrupt
    line that load_jsonl drops FOREVER, silently losing the recomputed
    pair). With the seal, the partial line is dropped exactly once with a
    warning and the recomputed record lands intact on its own line."""
    if path.exists():
        with path.open("rb+") as fh:
            fh.seek(0, 2)
            if fh.tell() > 0:
                fh.seek(-1, 2)
                if fh.read(1) != b"\n":
                    fh.write(b"\n")
    return path.open("a", encoding="utf-8")


# -- per-document stages ------------------------------------------------------


def stage_canonical(row, data_dir: Path) -> CanonicalText:
    doc_id = row["document_id"]
    out = REPRO / f"canonical/{doc_id}.json.gz"
    if out.exists():
        return CanonicalText.model_validate(load_gz(out))
    raw = (data_dir / row["local_path"]).read_bytes()
    fmt = sniff_format(raw)
    canonical, _ = extract_with_stats(raw, fmt, doc_id)
    if fmt == "pdf" and should_ocr(canonical):
        from regcompass.ocr import ocr_document

        canonical = ocr_document(raw, doc_id, languages="eng+msa")
    if canonical.full_text != row["full_text"]:
        raise RuntimeError(
            f"{doc_id}: re-extracted stream differs from the stored canonical "
            "stream - extractor drift, do not proceed"
        )
    save_gz(out, canonical.model_dump(mode="json"))
    return canonical


def stage_m4(canonical: CanonicalText) -> list[Chunk]:
    out = REPRO / f"m4/{canonical.document_id}.chunks.json.gz"
    if out.exists():
        return [Chunk.model_validate(c) for c in load_gz(out)["chunks"]]
    chunks, report = split_document(canonical, completion_fn=_default_completion)
    save_gz(out, {"report": asdict(report), "chunks": [c.model_dump(mode="json") for c in chunks]})
    print(
        f"  m4 {canonical.document_id}: style={report.style} chunks={report.n_chunks}"
        f" sections={report.n_sections} fallback={report.fallback_used}"
    )
    return chunks


def stage_m5(doc_id: str, chunks: list[Chunk]) -> list[GatedChunk]:
    out = REPRO / f"m5/{doc_id}.gated.json.gz"
    if out.exists():
        return [GatedChunk.model_validate(r) for r in load_gz(out)["passed"]]
    gated, report = gate_document(chunks)
    passed = [g for g in gated if g.gate_decision == "passed"]
    save_gz(
        out,
        {
            "report": asdict(report),
            "passed": [g.model_dump(mode="json") for g in passed],
            "decisions": [
                {
                    "chunk_id": g.chunk.chunk_id,
                    "section_label": g.chunk.section_label,
                    "indicator_id": g.indicator_id,
                    "cosine_pillar": round(g.cosine_pillar, 6),
                    "bm25_indicator": round(g.bm25_indicator, 6),
                    "gate_decision": g.gate_decision,
                }
                for g in gated
            ],
        },
    )
    print(
        f"  m5 {doc_id}: candidates={report.n_candidates} passed={len(passed)}"
        f" reduction={report.reduction_ratio:.3f}"
    )
    return passed


def stage_m6(doc_id: str, economy: str, passed: list[GatedChunk], workers: int) -> list[dict]:
    out = REPRO / f"m6/{doc_id}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    done = load_jsonl(out)
    done_keys = {(o["chunk_id"], o["indicator_id"]) for o in done}
    todo = [g for g in passed if (g.chunk.chunk_id, g.indicator_id) not in done_keys]
    if todo:
        lock = threading.Lock()
        n_done = 0
        n_skipped = 0
        with open_checkpoint_append(out) as f:

            def _one(g: GatedChunk) -> None:
                nonlocal n_done, n_skipped
                o = _with_transport_retry(f"m6 {doc_id}", map_gated_chunk, g, economy)
                if o is None:
                    with lock:
                        n_skipped += 1
                    return
                line = json.dumps(
                    {
                        "chunk_id": g.chunk.chunk_id,
                        "section_label": g.chunk.section_label,
                        "indicator_id": g.indicator_id,
                        "outcome": o.outcome,
                        "attempts": o.attempts,
                        "failures": o.failures,
                        "record": o.record.model_dump(mode="json") if o.record else None,
                    }
                )
                with lock:
                    f.write(line + "\n")
                    f.flush()
                    n_done += 1
                    if n_done % 50 == 0:
                        print(f"  m6 {doc_id}: {n_done}/{len(todo)} pairs mapped")

            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(_one, todo))
        if n_skipped:
            raise RuntimeError(
                f"{doc_id}: {n_skipped} pairs skipped on persistent transport errors; "
                "re-run to retry them (completed pairs are checkpointed)"
            )
        done = load_jsonl(out)
    counts: dict[str, int] = {}
    for o in done:
        counts[o["outcome"]] = counts.get(o["outcome"], 0) + 1
    print(f"  m6 {doc_id}: pairs={len(done)} resumed={len(done_keys)} counts={counts}")
    return done


def stage_m7(doc_id: str, economy: str, passed: list[GatedChunk], m6_rows: list[dict]) -> list[dict]:
    out = REPRO / f"m7/{doc_id}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    done_keys = {(o["chunk_id"], o["indicator_id"]) for o in load_jsonl(out)}
    gated_by = {(g.chunk.chunk_id, g.indicator_id): g for g in passed}
    retried = 0
    with open_checkpoint_append(out) as f:
        for o in m6_rows:
            key = (o["chunk_id"], o["indicator_id"])
            if key in done_keys:
                continue
            base = {k: o[k] for k in ("chunk_id", "section_label", "indicator_id")}
            if o["outcome"] == "dropped":
                row = {**base, "outcome": "dropped", "attempts": o["attempts"],
                       "failures": o["failures"], "record": None}
            elif o["outcome"] == "no_evidence":
                row = {**base, "outcome": "no_evidence", "attempts": o["attempts"],
                       "failures": [], "record": o["record"]}
            else:
                record = MappingRecord.model_validate(o["record"])
                v = _with_transport_retry(
                    f"m7 {doc_id}", verify_with_retry, record, gated_by[key], economy
                )
                if v is None:
                    raise RuntimeError(
                        f"{doc_id}: verify hit persistent transport errors; "
                        "re-run to resume (completed pairs are checkpointed)"
                    )
                if v.attempts > record.extraction_attempts:
                    retried += 1
                row = {**base, "outcome": v.outcome, "attempts": v.attempts,
                       "failures": v.failures, "record": v.record.model_dump(mode="json")}
            f.write(json.dumps(row) + "\n")
            f.flush()
    rows = load_jsonl(out)
    counts: dict[str, int] = {}
    for o in rows:
        counts[o["outcome"]] = counts.get(o["outcome"], 0) + 1
    print(f"  m7 {doc_id}: pairs={len(rows)} verify_retried={retried} counts={counts}")
    return rows


def stage_m12(doc_id: str, records: list[MappingRecord], texts: dict[str, str], workers: int) -> list[dict]:
    """Classify every verified record of one document (M12), checkpointed per
    mapping_id like M6 pairs."""
    out = REPRO / f"m12/{doc_id}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    done_ids = {o["mapping_id"] for o in load_jsonl(out)}
    todo = [r for r in records if r.mapping_id not in done_ids]
    if todo:
        lock = threading.Lock()
        n_done = 0
        n_skipped = 0
        with open_checkpoint_append(out) as f:

            def _one(r: MappingRecord) -> None:
                nonlocal n_done, n_skipped
                o = _with_transport_retry(f"m12 {doc_id}", classify_record, r, texts[r.chunk_id])
                if o is None:
                    with lock:
                        n_skipped += 1
                    return
                line = json.dumps(
                    {
                        "mapping_id": r.mapping_id,
                        "indicator_id": r.indicator_id,
                        "outcome": o.outcome,
                        "attempts": o.attempts,
                        "failures": o.failures,
                        "classification": o.classification.model_dump(mode="json")
                        if o.classification
                        else None,
                    }
                )
                with lock:
                    f.write(line + "\n")
                    f.flush()
                    n_done += 1
                    if n_done % 50 == 0:
                        print(f"  m12 {doc_id}: {n_done}/{len(todo)} records classified")

            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(_one, todo))
        if n_skipped:
            raise RuntimeError(
                f"{doc_id}: {n_skipped} classifications skipped on persistent transport "
                "errors; re-run to retry them (completed records are checkpointed)"
            )
    rows = load_jsonl(out)
    n_uncls = sum(1 for o in rows if o["outcome"] == "unclassified")
    n_refused = sum(
        1 for o in rows if o["classification"] and o["classification"].get("confirmed") is False
    )
    print(
        f"  m12 {doc_id}: records={len(rows)} resumed={len(done_ids)} "
        f"unclassified={n_uncls} confirmation_refused={n_refused}"
    )
    return rows


def _passed_records_and_texts(eco: str) -> tuple[dict[str, list[MappingRecord]], dict[str, str]]:
    """Per-document verified records + chunk texts for one economy, from the
    m7/m5 checkpoints."""
    by_doc: dict[str, list[MappingRecord]] = {}
    texts: dict[str, str] = {}
    for m7_path in sorted((REPRO / "m7").glob(f"doc_{eco.lower()}_*.jsonl")):
        doc_id = m7_path.stem
        recs = [
            MappingRecord.model_validate(o["record"])
            for o in load_jsonl(m7_path)
            if o["outcome"] == "passed"
        ]
        if recs:
            by_doc[doc_id] = recs
        gated = load_gz(REPRO / f"m5/{doc_id}.gated.json.gz")
        for r in gated["passed"]:
            texts[r["chunk"]["chunk_id"]] = r["chunk"]["text"]
    return by_doc, texts


def load_classifications() -> tuple[dict[str, ProvisionClassification], int]:
    """All m12 checkpoints as mapping_id -> classification, plus the
    unclassified count. Empty dict when the classify stage has not run."""
    classifications: dict[str, ProvisionClassification] = {}
    n_unclassified = 0
    for p in sorted((REPRO / "m12").glob("*.jsonl")) if (REPRO / "m12").exists() else []:
        for o in load_jsonl(p):
            if o["classification"] is None:
                n_unclassified += 1
            else:
                classifications[o["mapping_id"]] = ProvisionClassification.model_validate(
                    o["classification"]
                )
    return classifications, n_unclassified


def cmd_reconfirm(args) -> int:
    """Re-run the adversarial confirmation for currently-CONFIRMED,
    score-eligible records of one measure nature against the CURRENT claim
    text, after a claim's text is edited. The m12 checkpoint
    line keeps its shape; only classification.confirmed and the appended
    failure trail change, so the paid classify lane stays frozen. Refusals
    fail closed exactly as in the original lane."""
    from regcompass.classify import (
        ELIGIBLE_NATURES,
        _default_confirm_completion,
        _run_confirmation,
    )
    from regcompass.engines import preflight_key, resolve_engine

    nature = args.nature
    if nature not in {n for s in ELIGIBLE_NATURES.values() for n in s}:
        raise SystemExit(f"nature {nature!r} is not score-eligible for any indicator")
    engine = resolve_engine(None)
    preflight_key(engine)

    def confirm_fn(prompt: str, strict: bool) -> str:
        return _default_confirm_completion(prompt, strict, engine)

    n_checked = n_refused = 0
    for p in sorted((REPRO / "m12").glob("*.jsonl")):
        doc_id = p.stem
        rows = load_jsonl(p)
        changed = False
        recs: dict[str, MappingRecord] | None = None
        texts: dict[str, str] = {}
        for o in rows:
            c = o.get("classification")
            if (
                not c
                or c["measure_nature"] != nature
                or not c["confirmed"]
                or nature not in ELIGIBLE_NATURES.get(o["indicator_id"], frozenset())
            ):
                continue
            if recs is None:
                recs = {
                    r["record"]["mapping_id"]: MappingRecord.model_validate(r["record"])
                    for r in load_jsonl(REPRO / f"m7/{doc_id}.jsonl")
                    if r["outcome"] == "passed"
                }
                gated = load_gz(REPRO / f"m5/{doc_id}.gated.json.gz")
                texts = {g["chunk"]["chunk_id"]: g["chunk"]["text"] for g in gated["passed"]}
            rec = recs[o["mapping_id"]]
            cls = ProvisionClassification.model_validate(c)
            failures = list(o.get("failures") or [])
            new_cls = _with_transport_retry(
                f"reconfirm {doc_id}",
                _run_confirmation,
                rec,
                texts[rec.chunk_id],
                cls,
                confirm_fn,
                failures,
            )
            if new_cls is None:
                raise RuntimeError(
                    f"{doc_id}: reconfirm hit persistent transport errors; re-run"
                )
            n_checked += 1
            if not new_cls.confirmed:
                n_refused += 1
            o["classification"] = new_cls.model_dump(mode="json")
            o["failures"] = failures
            changed = True
            print(f"  {o['mapping_id']}: confirmed={new_cls.confirmed}")
        if changed:
            tmp = p.with_suffix(".jsonl.tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                for o in rows:
                    fh.write(json.dumps(o) + "\n")
            tmp.replace(p)
    print(f"reconfirm[{nature}]: {n_checked} re-asked, {n_refused} refused")
    return 0


def cmd_classify(args) -> int:
    total = 0
    for eco in ECONOMIES if args.economy == "all" else (args.economy,):
        by_doc, texts = _passed_records_and_texts(eco)
        n_eco = sum(len(v) for v in by_doc.values())
        print(f"{eco}: {n_eco} verified records over {len(by_doc)} documents")
        for doc_id, recs in by_doc.items():
            stage_m12(doc_id, recs, texts, args.workers)
        total += n_eco
    classifications, n_unclassified = load_classifications()
    print(f"\nm12 complete: {len(classifications)} classified, {n_unclassified} unclassified "
          f"(unclassified records never drive a score), {total} records seen this pass")
    return 0


# -- commands -----------------------------------------------------------------

STAGE_ORDER = ("canonical", "m4", "m5", "m6", "m7")


def cmd_run(args) -> int:
    storage = Storage(DB)
    data_dir = ROOT / "data"
    economies = ECONOMIES if args.economy == "all" else (args.economy.upper(),)
    stop_at = STAGE_ORDER.index(args.through)
    for eco in economies:
        rows = storage.documents_for_economy(eco)
        if args.max_docs:
            rows = rows[: args.max_docs]
        print(f"== {eco}: {len(rows)} documents (through {args.through}) ==")
        t0 = time.time()
        summary = {"economy": eco, "documents": {}}
        for row in rows:
            doc_id = row["document_id"]
            print(f"- {doc_id}")
            canonical = stage_canonical(row, data_dir)
            doc_sum: dict = {"chars": len(canonical.full_text)}
            if stop_at >= 1:
                chunks = stage_m4(canonical)
                doc_sum["chunks"] = len(chunks)
            if stop_at >= 2:
                passed = stage_m5(doc_id, chunks)
                doc_sum["gate_passed"] = len(passed)
            if stop_at >= 3:
                m6_rows = stage_m6(doc_id, eco, passed, args.workers)
                doc_sum["m6"] = {
                    o: sum(1 for r in m6_rows if r["outcome"] == o)
                    for o in {r["outcome"] for r in m6_rows}
                }
            if stop_at >= 4:
                m7_rows = stage_m7(doc_id, eco, passed, m6_rows)
                doc_sum["m7"] = {
                    o: sum(1 for r in m7_rows if r["outcome"] == o)
                    for o in {r["outcome"] for r in m7_rows}
                }
            summary["documents"][doc_id] = doc_sum
        summary["duration_s"] = round(time.time() - t0, 1)
        report_path = REPRO / f"{eco}.report.json"
        report_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"== {eco} done in {summary['duration_s']}s -> {report_path} ==")
    return 0


def live_liveness(url: str) -> bool:
    try:
        r = httpx.get(
            url,
            timeout=30.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
        )
        print(f"  liveness {url} -> HTTP {r.status_code}")
        return r.status_code < 400
    except httpx.HTTPError as e:
        print(f"  liveness {url} -> {type(e).__name__}")
        return False


def cmd_finish(args) -> int:
    corpus = load_corpus()
    portals = load_portals()
    # Loaded BEFORE reconcile: M8's controlling selection is fit-filtered by
    # the M12 classifications. M12 runs before M8 in
    # this script (run -> classify -> finish), inverting the module
    # numbering on purpose.
    classifications, n_unclassified = load_classifications()
    if classifications:
        print(f"m12 classifications loaded: {len(classifications)} ({n_unclassified} unclassified)")
    else:
        print("no m12 checkpoints: reconcile runs unfiltered and scores fall back "
              "to presence-based (run 'classify' first)")
    all_records: list[MappingRecord] = []
    texts: dict[str, str] = {}
    cosines: dict = {}
    coverage: dict[str, dict] = {}
    for eco in ECONOMIES:
        law_names = {k: v.law_name for k, v in corpus.items() if v.economy == eco}
        eco_passed: list[MappingRecord] = []
        n_sections = 0
        n_pairs = 0
        n_docs = 0
        n_repaired = 0
        for m7_path in sorted((REPRO / "m7").glob(f"doc_{eco.lower()}_*.jsonl")):
            doc_id = m7_path.stem
            doc_passed = [
                MappingRecord.model_validate(o["record"])
                for o in load_jsonl(m7_path)
                if o["outcome"] == "passed"
            ]
            gated = load_gz(REPRO / f"m5/{doc_id}.gated.json.gz")
            n_docs += 1
            n_pairs += len(gated["passed"])
            n_sections += len({d["chunk_id"] for d in gated["decisions"]})
            doc_texts: dict[str, str] = {}
            for r in gated["passed"]:
                doc_texts[r["chunk"]["chunk_id"]] = r["chunk"]["text"]
                cosines[(r["chunk"]["chunk_id"], r["indicator_id"])] = r["cosine_pillar"]
            texts.update(doc_texts)
            if doc_passed:
                # Quote-anchored section-label repair (round 3 item E): the
                # label is re-derived at the quote's exact position; chunk
                # boundaries and ids never move, so no checkpoint is
                # invalidated. Deterministic and free: recomputed every
                # finish, decisions logged per document.
                canonical = load_gz(REPRO / f"canonical/{doc_id}.json.gz")
                doc_passed, decisions = repair_section_labels(
                    doc_passed, canonical["full_text"], doc_texts
                )
                n_repaired += sum(1 for d in decisions if d["outcome"] == "repaired")
                repair_dir = REPRO / "label_repair"
                repair_dir.mkdir(exist_ok=True)
                with (repair_dir / f"{doc_id}.jsonl").open("w", encoding="utf-8") as fh:
                    for d in decisions:
                        fh.write(json.dumps(d, ensure_ascii=False) + "\n")
            eco_passed.extend(doc_passed)
        if n_repaired:
            print(f"{eco}: section labels repaired at quote position: {n_repaired}")
        res = _with_transport_retry(
            f"m8 {eco}",
            lambda recs=eco_passed, names=law_names: reconcile_records(
                recs, names, classifications=classifications or None
            ),
        )
        if res is None:
            raise RuntimeError(f"{eco}: reconcile hit persistent transport errors; re-run finish")
        groups, updated = res
        save_gz(
            REPRO / f"m8/{eco}.reconciled.json.gz",
            {
                "groups": [g.model_dump(mode="json") for g in groups],
                "records": [r.model_dump(mode="json") for r in updated],
            },
        )
        print(f"{eco}: m8 reconciled {len(updated)} records into {len(groups)} groups")
        all_records.extend(updated)
        coverage[eco] = {
            "law": f"{n_docs} {portals[eco].official_name} legislative documents (full corpus sweep)",
            "sections": n_sections,
            "pairs_gated": n_pairs,
        }
    return _export_and_report(all_records, texts, cosines, coverage, classifications)


def _repro_document_meta(all_records) -> dict[str, dict]:
    """Per-document metadata for submission.json from the repro checkpoints,
    zero model calls: extractor + OCR quality from the canonical .json.gz (the
    ingest truth), source_pdf_path from the working DB when it is present. A
    missing checkpoint or DB simply leaves the field null."""
    db_meta: dict[str, dict] = {}
    if DB.exists():
        storage = Storage(DB)
        try:
            db_meta = storage.document_meta()
        finally:
            storage.close()
    meta: dict[str, dict] = {}
    for doc_id in {r.document_id for r in all_records}:
        entry = dict(db_meta.get(doc_id, {}))
        cpath = REPRO / f"canonical/{doc_id}.json.gz"
        if cpath.exists():
            canonical = load_gz(cpath)
            if not entry.get("extractor"):
                entry["extractor"] = canonical.get("extractor")
                entry["extractor_version"] = canonical.get("extractor_version")
            if entry.get("ocr_applied") is None:
                entry["ocr_applied"] = canonical.get("ocr_applied")
            oq = canonical.get("ocr_quality") or {}
            for f in (
                "mean_word_confidence",
                "dictionary_hit_rate",
                "cer_proxy_flag",
                "escalated_to_rapidocr",
                "manual_review",
            ):
                if entry.get(f) is None:
                    entry[f] = oq.get(f)
        meta[doc_id] = entry
    return meta


def _repro_processing_time() -> dict:
    """Per-economy wall-clock from the repro run reports (duration_s). Per
    document timing is not recorded (the checkpoints time whole stages)."""
    economies = {}
    for eco in ECONOMIES:
        rp = REPRO / f"{eco}.report.json"
        if rp.exists():
            d = json.loads(rp.read_text(encoding="utf-8"))
            if "duration_s" in d:
                economies[eco] = d["duration_s"]
    return {
        "granularity": "economy",
        "unit": "s",
        "economies": economies,
        "note": (
            "per-economy wall-clock from the repro run reports; per-document"
            " timing is not recorded"
        ),
    }


def _export_and_report(all_records, texts, cosines, coverage, classifications) -> int:
    """Shared tail of finish/export: pointer gate, M9 export, GT spots."""
    # Pointer battery: every controlling row's label must name the
    # document's own nearest heading at its quote. Runs BEFORE anything is
    # written; a failure means the export is not produced.
    failures, notes = pointer_gate(
        all_records,
        lambda doc_id: load_gz(REPRO / f"canonical/{doc_id}.json.gz")["full_text"],
        texts,
    )
    for n in notes:
        print(f"pointer gate note: {n}")
    if failures:
        print(f"POINTER GATE: {len(failures)} controlling row(s) failed; export NOT written:")
        for f in failures:
            print(f"  {f}")
        return 1
    print(f"pointer gate: all controlling labels match their quote's heading "
          f"({len(notes)} fail-closed note(s))")
    outdir = REPRO / "export"
    result = export_all(
        outdir,
        all_records,
        chunk_text_lookup=texts,
        gate_cosine_lookup=cosines,
        coverage_stats=coverage,
        liveness_fn=live_liveness,
        classifications=classifications or None,
        document_meta=_repro_document_meta(all_records),
        processing_time=_repro_processing_time(),
        generated_at=utc_now_iso(),
    )
    supp = json.loads(result.supplementary_path.read_text(encoding="utf-8"))
    print(
        f"export GREEN: {supp['n_rows']} rows ({supp['n_substantive']} substantive"
        f" + {supp['n_absence']} absence) -> {outdir}"
    )
    if "review_drops" in supp:
        print(f"review drops applied: {supp['review_drops']['n_dropped']}")
    scores = {
        (eco, ind): s
        for eco, cells in supp["derived_scores"].items()
        for ind, s in cells.items()
    }
    v1 = derive_scores([r for r in all_records if r.verification_status == "passed"])
    print(f"\nheadline GT spot checks ({supp['score_method'].split(':')[0]}):")
    misses = 0
    for (eco, ind), want in EXPECTED_SPOTS.items():
        got = scores.get((eco, ind))
        ok = got == want
        misses += 0 if ok else 1
        basis = ""
        if "score_details" in supp:
            d = supp["score_details"][eco][ind]
            basis = f"  [{d['basis']}; controlling={d['controlling_mapping_id']}]"
        print(f"  {eco} {ind}: expected {want} got {got} {'OK' if ok else 'MISS'}{basis}")
    print(f"\nall derived scores: {json.dumps({f'{e} {i}': s for (e, i), s in sorted(scores.items())})}")
    if classifications:
        print(f"presence-based v1 for comparison: "
              f"{json.dumps({f'{e} {i}': s for (e, i), s in sorted(v1.items())})}")
    return 1 if misses else 0


def cmd_export(args) -> int:
    """M9 export from the FROZEN m8 checkpoints: zero model calls.

    Re-applies the CURRENT quote-anchored label repair to the reconciled
    records (labels feed no score, no Discovery Tag, and no reconcile
    decision), runs the pointer gate over the controlling rows, then
    exports and reports the GT spots. This is the lane for regenerating the
    export after a label-repair or export-side fix without paying the ~26
    sequential 30B reconcile calls a full finish costs."""
    corpus = load_corpus()
    portals = load_portals()
    classifications, n_unclassified = load_classifications()
    if classifications:
        print(f"m12 classifications loaded: {len(classifications)} ({n_unclassified} unclassified)")
    all_records: list[MappingRecord] = []
    texts: dict[str, str] = {}
    cosines: dict = {}
    coverage: dict[str, dict] = {}
    for eco in ECONOMIES:
        m8_path = REPRO / f"m8/{eco}.reconciled.json.gz"
        if not m8_path.exists():
            raise RuntimeError(f"{eco}: no m8 checkpoint ({m8_path}); run finish first")
        eco_records = [
            MappingRecord.model_validate(o) for o in load_gz(m8_path)["records"]
        ]
        by_doc: dict[str, list[MappingRecord]] = {}
        for r in eco_records:
            by_doc.setdefault(r.document_id, []).append(r)
        n_sections = 0
        n_pairs = 0
        n_docs = 0
        n_repaired = 0
        repaired_by_id: dict[str, MappingRecord] = {}
        # Mirror cmd_finish's doc walk (the m7 glob) so coverage_stats come
        # out identical to a full finish over the same checkpoints.
        for m7_path in sorted((REPRO / "m7").glob(f"doc_{eco.lower()}_*.jsonl")):
            doc_id = m7_path.stem
            gated = load_gz(REPRO / f"m5/{doc_id}.gated.json.gz")
            n_docs += 1
            n_pairs += len(gated["passed"])
            n_sections += len({d["chunk_id"] for d in gated["decisions"]})
            doc_texts: dict[str, str] = {}
            for r in gated["passed"]:
                doc_texts[r["chunk"]["chunk_id"]] = r["chunk"]["text"]
                cosines[(r["chunk"]["chunk_id"], r["indicator_id"])] = r["cosine_pillar"]
            texts.update(doc_texts)
            doc_recs = by_doc.get(doc_id)
            if doc_recs:
                canonical = load_gz(REPRO / f"canonical/{doc_id}.json.gz")
                repaired, decisions = repair_section_labels(
                    doc_recs, canonical["full_text"], doc_texts
                )
                n_repaired += sum(1 for d in decisions if d["outcome"] == "repaired")
                repair_dir = REPRO / "label_repair"
                repair_dir.mkdir(exist_ok=True)
                with (repair_dir / f"{doc_id}.jsonl").open("w", encoding="utf-8") as fh:
                    for d in decisions:
                        fh.write(json.dumps(d, ensure_ascii=False) + "\n")
                repaired_by_id.update({r.mapping_id: r for r in repaired})
        # Original m8 record order preserved.
        all_records.extend(repaired_by_id.get(r.mapping_id, r) for r in eco_records)
        if n_repaired:
            print(f"{eco}: section labels repaired at quote position: {n_repaired}")
        coverage[eco] = {
            "law": f"{n_docs} {portals[eco].official_name} legislative documents (full corpus sweep)",
            "sections": n_sections,
            "pairs_gated": n_pairs,
        }
    return _export_and_report(all_records, texts, cosines, coverage, classifications)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="per-economy M1..M7 with checkpoints")
    run.add_argument("--economy", default="all", help="SG, AU, MY or all")
    run.add_argument("--max-docs", type=int, default=None)
    run.add_argument("--through", choices=STAGE_ORDER, default="m7")
    run.add_argument("--workers", type=int, default=WORKERS)
    run.set_defaults(fn=cmd_run)
    cl = sub.add_parser("classify", help="M12: classify all verified records (checkpointed)")
    cl.add_argument("--economy", default="all", help="SG, AU, MY or all")
    cl.add_argument("--workers", type=int, default=WORKERS)
    cl.set_defaults(fn=cmd_classify)
    rc = sub.add_parser(
        "reconfirm",
        help="re-run the adversarial confirmation for one measure nature "
        "against the current claim text (after a claim's text has been edited)",
    )
    rc.add_argument("--nature", required=True, help="e.g. local_infrastructure")
    rc.set_defaults(fn=cmd_reconfirm)
    fin = sub.add_parser("finish", help="M8 reconcile + M9 export + GT spot checks")
    fin.set_defaults(fn=cmd_finish)
    ex = sub.add_parser(
        "export",
        help="M9 export from the frozen m8 checkpoints (label repair + "
        "pointer gate + export; zero model calls)",
    )
    ex.set_defaults(fn=cmd_export)
    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
