"""Turn a Run's command-line log and its Run Record into a recorded event file.

One-off. The Runs made before the app recorded its events (the pre-run over
the six Economies) left a text log per Run and a Run Record in the working
database, but no event file, so none of them could be watched again. This
rebuilds the event file of one such Run from those two records, in the exact
shape and order a Run started from the app writes (regcompass.run_events), and
checks every count it rebuilds against the Run Record before writing anything.

What the log never had is not invented:

- the Gate's scores per Candidate: the log only counts Candidates, so the file
  carries no Candidate events at all;
- the time of each Step: the log gives the Run's start and end, the Gate's and
  Reconcile's own durations, and Map's ticks, so every other time is an
  estimate spread over the Run's real length.

The first event says both (`recorded_from`, `not_recorded`), so the screen can
tell the reader plainly.

    uv run python scripts/convert_run_log.py \\
        --log data/pre_run_logs/SG-P7-engine-a.log \\
        --db data/regcompass.db --run-id run_20260923T041241Z_a07249 \\
        --out tests/fixtures/run_events

writes, into --out, the log with local paths taken out, the Run Record (with
the Corpus's titles and pages and each Document's stored Mappings), and the
event file. --install also puts the event file beside the database, where the
Runs list looks for it, so the Run shows 'Watch again'.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from regcompass.run_progress import (  # noqa: E402
    CUT,
    GATE,
    GLOSS,
    MAP,
    PROVE,
    READ,
    RECONCILE,
    SCAN_CHECK,
)

# What an older record never kept, as the first event names it.
NOT_RECORDED = ["gate_scores", "step_times"]

# A physical line of the log that begins a new logical line. Everything else
# is the rest of the line above, wrapped by the terminal at 80 columns.
_STARTS = re.compile(
    r"^(M\d|===|\$ |regcompass |[A-Z]{2}: \d+ documents|Run Record$|  \S|records in |this Run Record)"
)

# Local paths up to the repository, whatever the folder names hold (spaces too).
_LOCAL_REPO = re.compile(r"(?:/Users|/home|/private|/tmp)/[^\n]*?/regcompass(?=/)")

# Times the log does not give, in seconds: the relative weight of the quick
# Steps. Only their order and rough size matter; Map takes what is left.
_QUICK = {READ: 0.5, SCAN_CHECK: 0.1, PROVE: 0.2, GLOSS: 0.1}


def sanitize_log(text: str) -> str:
    """The log with every local path to the repository replaced by <repo>, and
    the spaces the terminal left at the end of wrapped lines taken off (the
    lines are joined back with one space either way)."""
    text = _LOCAL_REPO.sub("<repo>", text)
    return "".join(line.rstrip() + "\n" for line in text.splitlines())


def logical_lines(text: str) -> list[str]:
    """The log's lines as the Run printed them, with the terminal's wrapping
    undone. A wrapped line broke at a space: a trailing space stayed on the
    line above, or the space was swallowed by the break."""
    out: list[str] = []
    for raw in text.splitlines():
        if not raw.strip():
            out.append("")
            continue
        if out and out[-1] and not _STARTS.match(raw):
            out[-1] = out[-1] + ("" if out[-1].endswith(" ") else " ") + raw
        else:
            out.append(raw)
    return [line.rstrip() for line in out if line.strip()]


def _num(text: str) -> int:
    return int(text.replace(",", ""))


def parse_log(text: str) -> dict:
    """What the log says, per Document in the order the Run read them."""
    docs: dict[str, dict] = {}
    order: list[str] = []
    reconcile: dict | None = None
    summary: dict | None = None

    def doc(doc_id: str) -> dict:
        if doc_id not in docs:
            docs[doc_id] = {"ticks": []}
            order.append(doc_id)
        return docs[doc_id]

    for line in logical_lines(text):
        if m := re.match(r"M1 extract \| (\S+): .*?([\d,]+) chars, ocr=(\w+)", line):
            doc(m[1])["chars"] = _num(m[2])
        elif m := re.match(r"M2 ocr \| (\S+): (.*)", line):
            doc(m[1])["ocr_applied"] = 0 if m[2].startswith("skipped") else 1
        elif m := re.match(r"M4 chunk \| (\S+): (\d+) chunks.*\(([\d.]+)s\)$", line):
            doc(m[1]).update(pieces=_num(m[2]), cut_s=float(m[3]))
        elif m := re.match(
            r"M5 gate \| (\S+): (\d+) gate-passed of (\d+) .*, ([\d.]+)s\)$", line
        ):
            doc(m[1]).update(candidates=_num(m[2]), pairs=_num(m[3]), gate_s=float(m[4]))
        elif m := re.match(r"M6 map \+ M7 verify \| (\S+): (\d+)/(\d+) pairs", line):
            doc(m[1])["ticks"].append((_num(m[2]), _num(m[3])))
        elif m := re.match(
            r"M6/M7 \| (\S+): (\d+) passed, (\d+) no-evidence, (\d+) dropped so far", line
        ):
            doc(m[1])["so_far"] = (_num(m[2]), _num(m[3]), _num(m[4]))
        elif m := re.match(r"M3 gloss \| (\S+): (.*)", line):
            if m[2].startswith("skipped"):
                glossed = 0
            else:
                n = re.search(r"(\d+)", m[2])
                if n is None:
                    raise ValueError(f"a Gloss line the converter cannot read: {line}")
                glossed = _num(n[1])
            doc(m[1])["glossed"] = glossed
        elif m := re.match(
            r"M8 reconcile \| \S+: (\d+) records into (\d+) groups \(([\d.]+)s\)", line
        ):
            reconcile = {"records": _num(m[1]), "groups": _num(m[2]), "seconds": float(m[3])}
        elif m := re.match(
            r"[A-Z]{2}: (\d+) documents, (\d+) gated pairs -> (\d+) verified, (\d+) no-evidence,"
            r" (\d+) dropped, (\d+) groups",
            line,
        ):
            summary = dict(
                zip(
                    ("documents", "candidates", "proven", "no_evidence", "dropped", "groups"),
                    (_num(g) for g in m.groups()),
                )
            )
    return {"order": order, "documents": docs, "reconcile": reconcile, "summary": summary}


def read_record(db_path: str | Path, run_id: str) -> dict:
    """The Run Record as the database holds it, plus what the event file needs
    from the Corpus (titles, Languages, pages) and each Document's stored
    Mappings, read-only."""
    conn = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise SystemExit(f"no Run Record '{run_id}' in {db_path}")
        record = dict(row)
        for key in ("pillars", "indicators", "details"):
            if isinstance(record.get(key), str):
                record[key] = json.loads(record[key])
        ids = list(record["details"].get("documents") or [])
        corpus = []
        for doc_id in ids:
            d = conn.execute(
                "SELECT document_id, title, language, n_pages FROM documents"
                " WHERE document_id = ?",
                (doc_id,),
            ).fetchone()
            corpus.append(
                dict(d) if d else
                {"document_id": doc_id, "title": None, "language": None, "n_pages": None}
            )
        record["corpus"] = corpus
        counts = dict(
            conn.execute(
                "SELECT document_id, COUNT(*) FROM mappings"
                " WHERE run_id = ? AND verification_status = 'passed' GROUP BY document_id",
                (run_id,),
            ).fetchall()
        )
        record["mappings_by_document"] = {doc_id: counts.get(doc_id, 0) for doc_id in ids}
    finally:
        conn.close()
    return record


def _check(name: str, from_log: int, from_record: object) -> None:
    if from_record is not None and from_log != from_record:
        raise ValueError(
            f"the log and the Run Record disagree on {name}: {from_log} against {from_record}"
        )


def _utc(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)


def convert(log_text: str, record: dict) -> list[dict]:
    """The Run's events, in the order and shape a live Run emits them."""
    parsed = parse_log(log_text)
    details = record.get("details") or {}
    order = parsed["order"]
    docs = parsed["documents"]
    if not order or parsed["reconcile"] is None:
        raise ValueError("the log does not hold a whole Run: no Documents or no Reconcile")
    for doc_id in order:
        missing = [
            k for k in ("chars", "ocr_applied", "pieces", "candidates", "pairs", "glossed")
            if k not in docs[doc_id]
        ]
        if missing:
            raise ValueError(f"the log never finished {doc_id}: no {', '.join(missing)}")

    # Each Document's share of Prove, from the running totals the log prints.
    previous = (0, 0, 0)
    for doc_id in order:
        so_far = docs[doc_id].get("so_far", previous)
        docs[doc_id]["prove"] = tuple(a - b for a, b in zip(so_far, previous))
        previous = so_far

    totals = {
        "documents": len(order),
        "pieces": sum(docs[d]["pieces"] for d in order),
        "pairs": sum(docs[d]["pairs"] for d in order),
        "candidates": sum(docs[d]["candidates"] for d in order),
        "proven": previous[0],
        "no_evidence": previous[1],
        "dropped": previous[2],
        "glossed": sum(docs[d]["glossed"] for d in order),
        "groups": parsed["reconcile"]["groups"],
    }
    _check("Documents", order, details.get("documents"))
    _check("Pieces", totals["pieces"], details.get("n_chunks"))
    _check("pairs", totals["pairs"], details.get("n_pairs_considered"))
    _check("Candidates", totals["candidates"], details.get("n_pairs_gated"))
    _check("Mappings", totals["proven"], details.get("n_passed"))
    _check("no-evidence Candidates", totals["no_evidence"], details.get("n_no_evidence"))
    _check("dropped Candidates", totals["dropped"], details.get("n_dropped"))
    _check("glossed Mappings", totals["glossed"], details.get("n_glossed"))
    _check("groups", totals["groups"], details.get("n_groups"))
    summary = parsed["summary"] or {}
    for key, value in summary.items():
        _check(f"the closing line's {key}", totals[key], value)
    by_doc = record.get("mappings_by_document") or {}
    for doc_id in order:
        _check(f"the Mappings of {doc_id}", docs[doc_id]["prove"][0], by_doc.get(doc_id))

    # The clock. Integer microseconds, so the last event lands exactly on the
    # Run Record's end. Known durations are kept; Map takes what is left, in
    # proportion to its Candidates, and its ticks share it by pairs done.
    start = _utc(record["started_at"])
    end = _utc(record["ended_at"])
    total_us = int((end - start) / timedelta(microseconds=1))

    def us(seconds: float) -> int:
        return int(round(seconds * 1_000_000))

    fixed = us(parsed["reconcile"]["seconds"])
    for doc_id in order:
        d = docs[doc_id]
        fixed += us(d.get("cut_s", 0.0)) + us(d.get("gate_s", 0.0))
        fixed += sum(us(s) for s in _QUICK.values())
    map_us = max(0, total_us - fixed)
    weight = sum(max(1, docs[d]["candidates"]) for d in order)
    shares = [map_us * max(1, docs[d]["candidates"]) // weight for d in order]
    shares[-1] += map_us - sum(shares)
    scale = 1.0 if fixed <= total_us else total_us / fixed

    clock = 0
    events: list[dict] = []

    def emit(type_: str, after_us: int = 0, **fields) -> None:
        nonlocal clock
        clock = min(total_us, clock + max(0, int(after_us)))
        ts = (start + timedelta(microseconds=clock)).isoformat()
        events.append({"type": type_, "seq": len(events), "ts": ts, **fields})

    def quick(step: str) -> int:
        return int(us(_QUICK[step]) * scale)

    corpus = {c["document_id"]: c for c in record.get("corpus") or []}
    emit(
        "run_started",
        run_id=record["run_id"],
        economy=record["economy"],
        pillars=list(record.get("pillars") or []),
        indicators=record.get("indicators"),
        engine=record.get("engine"),
        documents=[
            {
                "document_id": doc_id,
                "title": (corpus.get(doc_id) or {}).get("title") or doc_id,
                "language": (corpus.get(doc_id) or {}).get("language"),
                "n_pages": (corpus.get(doc_id) or {}).get("n_pages"),
            }
            for doc_id in order
        ],
        recorded_from="log",
        not_recorded=list(NOT_RECORDED),
    )
    for doc_id, map_share in zip(order, shares):
        d = docs[doc_id]
        pages = (corpus.get(doc_id) or {}).get("n_pages") or 0
        emit("step_started", document_id=doc_id, step=READ)
        emit("step_finished", quick(READ), document_id=doc_id, step=READ,
             counts={"pages": pages, "chars": d["chars"]})
        emit("step_started", document_id=doc_id, step=SCAN_CHECK)
        emit("step_finished", quick(SCAN_CHECK), document_id=doc_id, step=SCAN_CHECK,
             counts={"ocr_applied": d["ocr_applied"]})
        emit("step_started", document_id=doc_id, step=CUT)
        emit("step_finished", int(us(d.get("cut_s", 0.0)) * scale), document_id=doc_id,
             step=CUT, counts={"pieces": d["pieces"]})
        emit("step_started", document_id=doc_id, step=GATE)
        emit("step_finished", int(us(d.get("gate_s", 0.0)) * scale), document_id=doc_id,
             step=GATE, counts={"pieces": d["pieces"], "pairs": d["pairs"],
                                "candidates": d["candidates"]})
        emit("step_started", document_id=doc_id, step=MAP)
        n = d["candidates"]
        spent = 0
        for done, total in d["ticks"]:
            at = map_share * done // total if total else map_share
            emit("map_progress", at - spent, document_id=doc_id, done=done, total=total)
            spent = at
        emit("step_finished", map_share - spent, document_id=doc_id, step=MAP,
             counts={"done": n, "total": n})
        emit("step_started", document_id=doc_id, step=PROVE)
        proven, no_evidence, dropped = d["prove"]
        emit("step_finished", quick(PROVE), document_id=doc_id, step=PROVE,
             counts={"proven": proven, "no_evidence": no_evidence, "dropped": dropped})
        emit("step_started", document_id=doc_id, step=GLOSS)
        emit("step_finished", quick(GLOSS), document_id=doc_id, step=GLOSS,
             counts={"glossed": d["glossed"]})
        emit("document_finished", document_id=doc_id, mappings=proven)
    emit("step_started", document_id=None, step=RECONCILE)
    emit("step_finished", total_us, document_id=None, step=RECONCILE,
         counts={"passed": totals["proven"], "records": parsed["reconcile"]["records"],
                 "groups": totals["groups"]})
    calls = (record.get("details") or {}).get("model_calls")
    emit("run_finished", status=record.get("status") or "completed", totals=totals,
         cost_usd=record.get("cost_usd"),
         provider_cost_usd=record.get("provider_cost_usd"),
         engine_calls=None if calls is None else int(calls))
    return events


def to_jsonl(events: list[dict]) -> str:
    """The event file's text, one JSON line per event, as the app writes it."""
    return "".join(json.dumps(e) + "\n" for e in events)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument(
        "--install", action="store_true",
        help="also write the event file beside the database, where the Runs list finds it",
    )
    args = parser.parse_args(argv)

    log_text = sanitize_log(args.log.read_text(encoding="utf-8"))
    record = read_record(args.db, args.run_id)
    events = convert(log_text, record)
    text = to_jsonl(events)

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / args.log.name).write_text(log_text, encoding="utf-8")
    (args.out / f"{args.run_id}.record.json").write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (args.out / f"{args.run_id}.jsonl").write_text(text, encoding="utf-8")
    print(f"{len(events)} events for {args.run_id} into {args.out}")
    if args.install:
        from regcompass.run_events import events_path

        target = events_path(args.db, args.run_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        print(f"installed beside the database: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
