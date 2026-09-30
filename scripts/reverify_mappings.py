"""Re-check the stored Mappings of a database with today's verification rules.

The word-for-word check (regcompass.verify.verify_record) is the gate every
shipped Mapping passes. When its rules change, this script answers "which
Mappings that a past Run dropped would pass now, and does anything that passed
before fail now?" without calling an Engine and without touching the Runs: it
opens the database read-only and prints counts per Economy and Engine.

Point it at a COPY of the database, never at the one the app is using.

Usage:
  uv run python scripts/reverify_mappings.py path/to/copy.db
  uv run python scripts/reverify_mappings.py path/to/copy.db --run-id run_... --json
"""

from __future__ import annotations

import argparse
import json
import sys

from regcompass.contracts import Chunk, MappingRecord
from regcompass.storage import Storage
from regcompass.verify import verify_record

FIELDS = ("dropped", "now_pass", "still_dropped", "passed", "passed_now_fail")


def reverify(storage: Storage, run_ids: list[str] | None = None) -> list[dict]:
    """One row per (Economy, Engine): stored dropped and passed Mappings,
    re-verified against their chunk as it is stored today."""
    engines = {
        r["run_id"]: r["engine"] or "unknown"
        for r in storage.conn.execute("SELECT run_id, engine FROM runs")
    }
    chunk_rows = {
        r["chunk_id"]: r
        for r in storage.conn.execute(
            "SELECT chunk_id, document_id, char_start, char_end, section_label FROM chunks"
        )
    }
    texts = storage.chunk_texts()

    where, params = "", []
    if run_ids:
        where = f" AND run_id IN ({', '.join('?' * len(run_ids))})"
        params = list(run_ids)
    rows = storage.conn.execute(
        "SELECT run_id, mapping_id FROM mappings WHERE insufficient_evidence = 0"
        f" AND verification_status IN ('passed', 'dropped'){where}",
        params,
    ).fetchall()
    wanted = {(r["run_id"], r["mapping_id"]) for r in rows}

    groups: dict[tuple[str, str], dict] = {}
    for run_id in sorted({r for r, _ in wanted}):
        for record in storage.load_mappings(run_id=run_id):
            if (run_id, record.mapping_id) not in wanted:
                continue
            key = (record.economy, engines.get(run_id, "unknown"))
            g = groups.setdefault(key, {"runs": set(), **{f: 0 for f in FIELDS}})
            g["runs"].add(run_id)
            ok = _passes_now(record, chunk_rows.get(record.chunk_id), texts)
            if record.verification_status == "dropped":
                g["dropped"] += 1
                g["now_pass" if ok else "still_dropped"] += 1
            else:
                g["passed"] += 1
                g["passed_now_fail"] += 0 if ok else 1
    return [
        {"economy": e, "engine": eng, "runs": len(g["runs"]), **{f: g[f] for f in FIELDS}}
        for (e, eng), g in sorted(groups.items())
    ]


def _passes_now(record: MappingRecord, row, texts: dict[str, str]) -> bool:
    if row is None:  # the chunk is gone (its Document was removed)
        return False
    chunk = Chunk(
        chunk_id=row["chunk_id"],
        document_id=row["document_id"],
        char_start=row["char_start"],
        char_end=row["char_end"],
        text=texts.get(row["chunk_id"], ""),
        section_label=row["section_label"],
    )
    return not verify_record(record, chunk)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("db", help="path to a COPY of the database (opened read-only)")
    parser.add_argument("--run-id", action="append", help="limit to this Run (repeatable)")
    parser.add_argument("--json", action="store_true", help="print the rows as JSON")
    args = parser.parse_args(argv)

    storage = Storage.read_only(args.db)
    try:
        rows = reverify(storage, args.run_id)
    finally:
        storage.conn.close()

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    header = ("Economy", "Engine", "Runs", "Dropped", "Now pass", "Still dropped",
              "Passed", "Passed now fail")
    table = [header] + [
        (r["economy"], r["engine"], *(str(r[k]) for k in ("runs", *FIELDS))) for r in rows
    ]
    widths = [max(len(str(line[i])) for line in table) for i in range(len(header))]
    for line in table:
        print("  ".join(str(c).ljust(w) for c, w in zip(line, widths)))
    total = {f: sum(r[f] for r in rows) for f in FIELDS}
    print(f"Total: {total['now_pass']} of {total['dropped']} dropped Mappings pass now;"
          f" {total['passed_now_fail']} of {total['passed']} passed Mappings fail now.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
