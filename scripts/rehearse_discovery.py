"""Rehearse the free first part of the live hour for every pool Economy and
Pillar: Discovery by Pillar, then each Document read, split into sections and
put through the Gate, stopping where the Engine would first be called.

Nothing here can spend money. Before any work the process forgets every Engine
key it could find in its own environment, and the Map step is replaced by a
recorder that counts the Candidates the Gate passed per Indicator and returns
nothing. Any call that still reaches an Engine completion raises at once.

Each cell (one Economy, one Pillar) runs in a fresh data folder of its own,
the way the live hour starts on a fresh instance: the Corpus is empty, so the
cell's time and requests are what a judge would see. Economies run in
parallel, one process each; the Pillars of one Economy run one after another,
so no host is ever asked by two Discoveries at once. Robots rules and the
delay floor apply exactly as in the app, because this is the app's own crawler.

Resumable: each finished cell lands as one line in
`<out>/results/<Economy>.jsonl`, and a cell already there is skipped when the
same command is run again. `--render-only` rewrites the table from the lines
without running anything.

    uv run python scripts/rehearse_discovery.py --out rehearsal --table rehearsal/table.md
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
import traceback
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlsplit

POOL = ("ID", "IN", "LA", "CN", "TH", "MN", "VN", "RU", "KZ", "TL")
PILLARS = tuple(range(1, 13))
DEFAULT_CAP = 12
# A laptop runs this: one Economy at a time unless asked, and every OCR
# engine held to one thread, so the machine stays usable.
DEFAULT_PARALLEL = 1
THREAD_LIMITS = {"OMP_THREAD_LIMIT": "1", "OMP_NUM_THREADS": "1"}


def limit_threads(environ=os.environ) -> None:
    for name, value in THREAD_LIMITS.items():
        environ.setdefault(name, value)


class EngineCallRefused(RuntimeError):
    """A rehearsal reached a paid Engine call. It never should."""


# ---------------------------------------------------------------------------
# the guard: no key, no completion, no Map
# ---------------------------------------------------------------------------


def _refuse(*_args, **_kwargs):
    raise EngineCallRefused("the rehearsal never calls an Engine")


# Candidates the Gate passed, per Document, per Indicator: filled by the
# recorder that stands in for the Map step.
CANDIDATES: dict[str, Counter] = {}


def record_candidates(storage, passed, *, doc_id, **_kwargs):
    """Stands in for pipeline.map_and_verify_pairs: counts what the Engine
    would have been sent and sends nothing."""
    CANDIDATES[doc_id] = Counter(g.indicator_id for g in passed)
    return []


def install_guard(environ=os.environ) -> list[str]:
    """Make a paid call impossible in this process. Returns the names of the
    environment variables it removed (names only, never values)."""
    import regcompass.map as map_module
    from regcompass import engines, pipeline

    removed = []
    for name in list(environ):
        if name.endswith("_API_KEY") or name.startswith("OPENROUTER"):
            environ.pop(name, None)
            removed.append(name)
    engines._SESSION_KEYS.clear()
    engines.make_completion = _refuse
    map_module.map_gated_chunk = _refuse
    pipeline.map_gated_chunk = _refuse
    pipeline.map_and_verify_pairs = record_candidates
    pipeline.gloss_document = lambda *a, **k: 0
    try:
        import litellm

        litellm.completion = _refuse
        litellm.acompletion = _refuse
    except ImportError:
        pass
    return removed


# ---------------------------------------------------------------------------
# one cell
# ---------------------------------------------------------------------------


def text_quality(text: str) -> dict:
    """Plain signals that a text layer is unusable: how much of what is not
    white space is letters (combining marks count, so Thai and Lao vowels
    are letters here), and how many broken-glyph marks (cid codes,
    replacement characters) it carries."""
    import unicodedata

    n = len(text)
    visible = [ch for ch in text if not ch.isspace()]
    if not visible:
        return {"chars": n, "letter_share": 0.0, "broken_marks": 0, "garbage": True}
    letters = sum(1 for ch in visible if unicodedata.category(ch)[0] in "LM")
    broken = text.count("(cid:") + text.count("\ufffd")
    share = letters / len(visible)
    garbage = share < 0.5 or broken > max(20, n // 500)
    return {
        "chars": n, "letter_share": round(share, 3), "broken_marks": broken,
        "garbage": garbage,
    }


def _host(url: str) -> str:
    return urlsplit(url).netloc


_NO_ANSWER = re.compile(r"([\w.-]+\.[a-z]{2,}) did not answer")
_REFUSED = re.compile(r"([\w.-]+\.[a-z]{2,}) refused (.+?)(?: earlier)?; not asked again")
_STATUS_FROM = re.compile(r"HTTP (\d{3}) from (https?://[^\s)]+)")


def unreachable_hosts(skipped_detail: list[dict]) -> dict[str, str]:
    """Each host a baseline law could not be fetched from, with what it did:
    'no answer' (a transport failure, the host is then not asked again), the
    HTTP status it refused with, or what it refused earlier in the Discovery
    (a kind of address, several kinds, or its robots.txt). Read from the
    reason sentence, which names the one address that failed, not every
    address the law lists."""
    out: dict[str, str] = {}
    for s in skipped_detail:
        if s.get("code") != "unreachable":
            continue
        reason = s.get("reason") or ""
        m = _STATUS_FROM.search(reason)
        if m:
            out.setdefault(_host(m.group(2)), f"HTTP {m.group(1)}")
            continue
        m = _NO_ANSWER.search(reason)
        if m:
            out[m.group(1)] = "no answer"
            continue
        m = _REFUSED.search(reason)
        if m:
            out.setdefault(m.group(1), f"refused {m.group(2)}")
    return dict(sorted(out.items()))


CRAWLER_FOUND_BY = "portal crawler"


def _named_law(found: dict) -> bool:
    """A Document fetched because a list named it for these Indicators (the
    2025 baseline, or an Economy's official source list), as against one
    the Portal crawler turned up from a seed."""
    return str(found.get("found_by", "")) != CRAWLER_FOUND_BY


def recount(row: dict) -> dict:
    """Re-derive the named-law and crawler counts from `found_by`, so a line
    written before the split was drawn this way reads the same as a new one."""
    found = row.get("found_by")
    if found is None:
        return row
    fetched = [f for f in found if f.get("status") == "fetched"]
    row["baseline_fetched"] = sum(1 for f in fetched if _named_law(f))
    row["crawler_fetched"] = sum(1 for f in fetched if not _named_law(f))
    return row


def summarise_discovery(report) -> dict:
    """The facts of one Discovery by Pillar the table needs."""
    baseline = [f for f in report.found_by if _named_law(f)]
    crawler = [f for f in report.found_by if not _named_law(f)]
    skipped = Counter(s["code"] for s in report.baseline_skipped)
    detail = [
        {"law": s["law"][:120], "code": s["code"], "reason": s["reason"][:240]}
        for s in report.baseline_skipped
    ]
    return {
        "baseline_fetched": sum(1 for f in baseline if f.get("status") == "fetched"),
        "crawler_fetched": sum(1 for f in crawler if f.get("status") == "fetched"),
        "baseline_laws_cited": len(baseline) + len(report.baseline_skipped),
        "skipped": dict(skipped),
        "skipped_detail": detail,
        "dead_hosts": unreachable_hosts(detail),
        # The crawler stage's own failures, which the baseline list does not
        # carry: a crawler that found its seeds but fetched none says why here.
        "crawler_failed": report.failed,
        "crawler_misses": [m[:200] for m in report.misses[:5]],
        "notes": list(report.notes),
        "found_by": [
            {k: f.get(k) for k in ("title", "url", "found_by", "status")}
            for f in report.found_by
        ],
    }


def rehearse_cell(
    economy: str,
    pillar: int,
    work_root: Path,
    *,
    cap: int = DEFAULT_CAP,
    discover=None,
    prepare=None,
    clock=time.monotonic,
) -> dict:
    """Discovery by Pillar into a fresh folder, then every free step of the
    Run over what it fetched. `discover` and `prepare` are injectable so the
    tests drive a cell without a network, a model or an OCR engine."""
    from regcompass.config import indicator_ids
    from regcompass.storage import Storage

    indicators = list(indicator_ids((pillar,)))
    cell_dir = work_root / economy / f"P{pillar}"
    if cell_dir.exists():
        shutil.rmtree(cell_dir)
    data_dir = cell_dir / "data"
    data_dir.mkdir(parents=True)
    storage = Storage(cell_dir / "rehearsal.db")
    storage.apply_schema()
    row: dict = {
        "economy": economy, "pillar": pillar, "indicators": indicators, "cap": cap,
    }
    started = clock()
    try:
        if discover is None:
            from regcompass.discovery import discover_economy as discover
        try:
            report = discover(
                economy, storage, data_dir=data_dir, pillar=pillar,
                indicators=indicators, max_documents=cap,
            )
        except Exception as exc:  # noqa: BLE001 - the cell says what went wrong
            row.update(
                status="discovery_failed",
                error=f"{type(exc).__name__}: {exc}"[:400],
                discovery_seconds=round(clock() - started, 1),
            )
            return row
        row.update(summarise_discovery(report))
        row["discovery_seconds"] = round(clock() - started, 1)
        prep_started = clock()
        docs = (prepare or prepare_documents)(storage, economy, pillar, data_dir)
        row["documents"] = docs
        row["prepare_seconds"] = round(clock() - prep_started, 1)
        totals = Counter()
        for d in docs:
            totals.update(d.get("candidates") or {})
        row["candidates"] = {i: totals.get(i, 0) for i in indicators}
        row["sections"] = sum(d.get("sections", 0) for d in docs)
        row["fetched"] = len(docs)
        row["status"] = "ok"
    finally:
        row["seconds"] = round(clock() - started, 1)
        storage.close()
    return row


def prepare_documents(
    storage, economy: str, pillar: int, data_dir: Path, *, embed_fn=None
) -> list[dict]:
    """Read, split and Gate each Document of the cell's Corpus through the
    Run's own code, with the Map step recorded instead of called. The Gate
    embeds with the local embedder (free) unless `embed_fn` is given."""
    from regcompass.engines import resolve_engine
    from regcompass.gate import embed_ollama
    from regcompass.pipeline import corpus_document_path, run_document

    engine = resolve_engine("fake")
    embed_fn = embed_fn or embed_ollama
    out = []
    for row in storage.corpus_documents(economy):
        doc_id = row["document_id"]
        text = row["full_text"] or ""
        info = {
            "document_id": doc_id,
            "title": (row["title"] or "")[:160],
            "source_url": row["source_url"],
            "language": row["language"],
            "ocr_applied": bool(row["ocr_applied"]),
            "manual_review": bool(row["manual_review"]),
            **text_quality(text),
        }
        CANDIDATES.pop(doc_id, None)
        try:
            path = corpus_document_path(row, data_dir)
            run_document(
                storage, doc_id, path, economy, (pillar,), engine, "rehearsal",
                completion_fn=_refuse, embed_fn=embed_fn,
                language=row["language"], data_dir=data_dir,
                source_url=row["source_url"], title=row["title"],
            )
        except EngineCallRefused:
            raise
        except Exception as exc:  # noqa: BLE001 - per Document, reported
            info["error"] = f"{type(exc).__name__}: {exc}"[:300]
        kinds = Counter(
            r[0] for r in storage.conn.execute(
                "SELECT chunk_kind FROM chunks WHERE document_id = ?", (doc_id,)
            )
        )
        info["sections"] = kinds.get("section", 0)
        info["pieces"] = sum(kinds.values())
        info["candidates"] = dict(CANDIDATES.get(doc_id) or {})
        out.append(info)
    return out


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------


def load_results(results_dir: Path) -> dict[tuple[str, int], dict]:
    """Every finished cell, the last line winning when a cell was re-run."""
    done: dict[tuple[str, int], dict] = {}
    if not results_dir.exists():
        return done
    for path in sorted(results_dir.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue  # a line cut short by a kill
            done[(row["economy"], int(row["pillar"]))] = recount(row)
    return done


def cells_to_run(
    economies, pillars, done: dict[tuple[str, int], dict], *, redo_failed: bool = False
) -> dict[str, list[int]]:
    todo: dict[str, list[int]] = {}
    for e in economies:
        for p in pillars:
            row = done.get((e, p))
            if row is not None and not (redo_failed and row.get("status") != "ok"):
                continue
            todo.setdefault(e, []).append(p)
    return todo


def append_result(results_dir: Path, row: dict) -> None:
    results_dir.mkdir(parents=True, exist_ok=True)
    with open(results_dir / f"{row['economy']}.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# the table
# ---------------------------------------------------------------------------


def verdict(row: dict) -> str:
    """One word per cell: ok (a right law with Candidates for every
    Indicator), partial, or gap."""
    if row.get("status") != "ok":
        return "gap"
    cands = row.get("candidates") or {}
    if not row.get("fetched"):
        return "gap"
    if cands and all(v > 0 for v in cands.values()):
        return "ok" if row.get("baseline_fetched") else "partial"
    if any(v > 0 for v in cands.values()):
        return "partial"
    return "gap"


def why(row: dict) -> str:
    """The plain reason a cell is not ok."""
    if row.get("status") != "ok":
        return (row.get("error") or row.get("status") or "")[:90]
    parts = []
    if not row.get("fetched"):
        skipped = row.get("skipped") or {}
        if skipped:
            parts.append("nothing fetched: " + ", ".join(f"{k} {v}" for k, v in sorted(skipped.items())))
        elif not row.get("baseline_laws_cited"):
            parts.append("no baseline law for this Pillar")
        notes = [n for n in row.get("notes") or [] if "crawler" in n or "baseline" in n]
        if notes and not skipped:
            parts.append(notes[0][:80])
    else:
        if not row.get("baseline_fetched"):
            parts.append("crawler only, no baseline law fetched")
        zero = [i for i, v in (row.get("candidates") or {}).items() if v == 0]
        if zero:
            parts.append("0 candidates: " + ", ".join(zero))
        bad = [d for d in row.get("documents") or [] if d.get("garbage")]
        if bad:
            parts.append(f"{len(bad)} garbage text")
    if row.get("crawler_failed"):
        parts.append(f"crawler failed {row['crawler_failed']}")
    hosts = unreachable_hosts(row.get("skipped_detail") or [])
    if hosts:
        parts.append("unreachable: " + ", ".join(f"{h} ({v})" for h, v in hosts.items()))
    return "; ".join(parts)


def _fmt_skipped(skipped: dict) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(skipped.items())) or "-"


def _fmt_cands(cands: dict) -> str:
    return " ".join(f"{i}:{v}" for i, v in cands.items()) or "-"


def render_table(rows: dict[tuple[str, int], dict], economies=POOL, pillars=PILLARS) -> str:
    head = (
        "| Economy | Pillar | Verdict | Fetched (named+crawler) | Right law | Skipped by reason"
        " | Seconds | OCR / garbage | Sections | Candidates per Indicator | Why |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|\n"
    )
    lines = []
    for e in economies:
        for p in pillars:
            row = rows.get((e, p))
            if row is None:
                lines.append(f"| {e} | {p} | not run | | | | | | | | |")
                continue
            docs = row.get("documents") or []
            ocr = sum(1 for d in docs if d.get("ocr_applied"))
            garbage = sum(1 for d in docs if d.get("garbage"))
            right = "y" if row.get("baseline_fetched") else "n"
            lines.append(
                f"| {e} | {p} | {verdict(row)} |"
                f" {row.get('fetched', 0)} ({row.get('baseline_fetched', 0)}+{row.get('crawler_fetched', 0)}) |"
                f" {right} | {_fmt_skipped(row.get('skipped') or {})} |"
                f" {row.get('seconds', '')} | {ocr} / {garbage} | {row.get('sections', 0)} |"
                f" {_fmt_cands(row.get('candidates') or {})} | {why(row).replace('|', '/')} |"
            )
    return head + "\n".join(lines) + "\n"


def render_summary(rows: dict[tuple[str, int], dict], economies=POOL, pillars=PILLARS) -> str:
    out = [
        "| Economy | ok | partial | gap | not run | median seconds | max seconds |",
        "|---|---|---|---|---|---|---|",
    ]
    for e in economies:
        mine = [rows[(e, p)] for p in pillars if (e, p) in rows]
        v = Counter(verdict(r) for r in mine)
        secs = sorted(r.get("seconds", 0) for r in mine)
        med = secs[len(secs) // 2] if secs else "-"
        out.append(
            f"| {e} | {v['ok']} | {v['partial']} | {v['gap']} |"
            f" {len(pillars) - len(mine)} | {med} | {secs[-1] if secs else '-'} |"
        )
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# the job
# ---------------------------------------------------------------------------


def run_economy_cells(economy: str, pillars: list[int], work_root: str, results_dir: str, cap: int) -> str:
    """One process: the guard first, then this Economy's Pillars in order."""
    limit_threads()
    removed = install_guard()
    if removed:
        print(f"[{economy}] guard: removed {', '.join(removed)} from this process", flush=True)
    for p in pillars:
        print(f"[{economy}] Pillar {p}: start", flush=True)
        try:
            row = rehearse_cell(economy, p, Path(work_root), cap=cap)
        except EngineCallRefused:
            raise
        except Exception as exc:  # noqa: BLE001 - recorded, the job goes on
            row = {
                "economy": economy, "pillar": p, "status": "crashed",
                "error": f"{type(exc).__name__}: {exc}"[:400],
                "trace": traceback.format_exc()[-1500:],
            }
        append_result(Path(results_dir), row)
        print(
            f"[{economy}] Pillar {p}: {row.get('status')} fetched={row.get('fetched', 0)}"
            f" baseline={row.get('baseline_fetched', 0)} seconds={row.get('seconds')}"
            f" candidates={row.get('candidates')}",
            flush=True,
        )
        # The cell's bytes are not needed after its line is written.
        shutil.rmtree(Path(work_root) / economy / f"P{p}", ignore_errors=True)
    return economy


def write_markdown(
    path: Path, rows, previous=None, notes: str = "", cap: int = DEFAULT_CAP
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = (
        "# Discovery rehearsal (every pool Economy x Pillar, no Engine call)\n\n"
        "Generated by `scripts/rehearse_discovery.py`. Each cell: Discovery by Pillar with all the Pillar's"
        f" Indicators, cap {cap}, fresh data folder, then read, split into sections and the Gate, stopping"
        " before the first Engine call. Fetched = named + crawler Documents, where a named law is one the"
        " 2025 baseline or the Economy's official source list cites for these Indicators. Right law = at"
        " least one named law fetched. OCR / garbage = Documents read by OCR / Documents whose text layer looks"
        " unusable. Verdict ok = a right law and Candidates for every Indicator.\n\n"
        + (notes.rstrip() + "\n\n" if notes else "")
        + "## Summary\n\n" + render_summary(rows)
        + (
            "\n## Summary of the previous pass\n\n" + render_summary(previous)
            if previous else ""
        )
        + "\n## All cells\n\n" + render_table(rows)
    )
    path.write_text(text, encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, help="working folder (results and temp data)")
    ap.add_argument("--table", help="markdown file to write the table to")
    ap.add_argument("--economies", default=",".join(POOL))
    ap.add_argument("--pillars", default=",".join(str(p) for p in PILLARS))
    ap.add_argument("--cap", type=int, default=DEFAULT_CAP)
    ap.add_argument("--parallel", type=int, default=DEFAULT_PARALLEL)
    ap.add_argument("--redo-failed", action="store_true")
    ap.add_argument("--render-only", action="store_true")
    ap.add_argument("--previous", help="an earlier --out folder, summarised beside this pass")
    ap.add_argument("--notes", help="a markdown file placed above the tables (findings)")
    args = ap.parse_args(argv)
    limit_threads()

    out = Path(args.out).resolve()
    results_dir = out / "results"
    economies = [e.strip().upper() for e in args.economies.split(",") if e.strip()]
    pillars = [int(p) for p in args.pillars.split(",") if p.strip()]
    install_guard()

    if not args.render_only:
        todo = cells_to_run(economies, pillars, load_results(results_dir), redo_failed=args.redo_failed)
        print(f"cells to run: {sum(len(v) for v in todo.values())} over {len(todo)} Economies", flush=True)
        work_root = out / "work"
        with ProcessPoolExecutor(max_workers=max(1, args.parallel)) as pool:
            futures = {
                pool.submit(run_economy_cells, e, ps, str(work_root), str(results_dir), args.cap): e
                for e, ps in todo.items()
            }
            for fut in as_completed(futures):
                e = futures[fut]
                try:
                    fut.result()
                    print(f"[{e}] done", flush=True)
                except EngineCallRefused:
                    print(f"[{e}] STOPPED: an Engine call was attempted", flush=True)
                    raise
    rows = load_results(results_dir)
    if args.table:
        previous = load_results(Path(args.previous) / "results") if args.previous else None
        notes = Path(args.notes).read_text(encoding="utf-8") if args.notes else ""
        write_markdown(Path(args.table), rows, previous, notes, cap=args.cap)
        print(f"table written: {args.table}", flush=True)
    else:
        print(render_summary(rows, economies, pillars))
    return 0


if __name__ == "__main__":
    sys.exit(main())
