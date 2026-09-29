"""Pack the prepared database a release ships beside its code.

A judge who opens the interface should find the pre-run Runs already there,
with the source PDF beside every quote. Neither the repository (GitHub refuses
a file over 100 MB) nor the image (`.dockerignore` leaves `data/` out) can
carry that, so a release attaches one archive and `regcompass load-data`
unpacks it into the data folder. This script makes the archive from a working
database and its data folder. It only ever READS those two; every change is
made on a copy.

What it does, in order:

1. Copies the database through SQLite's backup API, which reads a consistent
   snapshot even while the file is in write-ahead-log mode and its latest
   pages are still in the log. Copying the file itself would miss them.
2. Prunes the copy. What is kept, and why each other row goes:
   - Documents of the named Economies only (default: the six the submission
     declares, AU MY SG CN ID IN), minus any `--drop-document`, and never a
     `fixture` Document (a seeded demonstration row, not a collected one).
     Their chunks, word boxes, shortlist windows, gate scores and stored text
     streams follow them.
   - Runs (kind `run`) that are `completed`, on a declared Engine (default
     engine-a and engine-b, so the keyless `fake` Engine's Runs go), for a kept
     Economy, minus any `--drop-run`. `interrupted`, `failed` and `running`
     rows go: a Run nobody finished is not evidence. Discoveries are kept when
     completed, for a kept Economy.
   - Mappings, Glosses, reconciliation groups and their relationships are kept
     only for a kept Run. Mappings of the pre-Run-Record era (run id
     `legacy`) belong to no Run and go.
   - Review Decisions all go. Accepting or rejecting a Mapping is the
     reviewer's act, and the reviewer is whoever opens the release.
   - The crawl manifest keeps the kept Economies' rows.
   - The audit trail keeps every row except the ones that name a dropped Run
     or a dropped Document.
   A table this script does not know refuses the pack, so a table added later
   can never ship unexamined.
3. Checks the copy: foreign keys and integrity hold, and no text anywhere
   (every text column of every table, and every stored text stream) carries
   an absolute home or temporary path such as `/Users/...` or `/home/...`.
   A hit refuses the pack and names the table and column.
4. Collects the bytes each kept Document was read from (found the way the
   audit view finds them, and proven by their SHA-256), and only those. A kept
   Document whose bytes are missing refuses the pack, because its rows would
   open in the audit view with no source beside them. Each Document's
   local_path is written as `<ECONOMY>/raw/<file>`.
5. Writes `<name>.tar.gz` (the database, `prepared-data.json` and the raw
   files, laid out like a data folder), `<name>.tar.gz.sha256` in the
   `sha256sum` format, and `<name>.manifest.json`, a copy of the manifest to
   read without unpacking.

Usage:

  uv run python scripts/pack_prepared_data.py \
      --db data/regcompass.db --data-dir data --out dist/prepared

  # one more Economy, one Run left out, and a named archive
  uv run python scripts/pack_prepared_data.py --db data/regcompass.db \
      --data-dir data --out dist/prepared --economies AU,MY,SG,CN,ID,IN,LA \
      --drop-run run_20260922T192925Z_d34141 --name regcompass-prepared-test

Run it when no Run or Discovery is writing the database. Exit codes: 0 packed,
2 refused (the message says why; nothing is written to --out).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import tarfile
import tempfile
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from regcompass.paths import resolve_stored_path
from regcompass.prepared_data import (
    ARCHIVE_DB_NAME,
    BUNDLE_FORMAT,
    MANIFEST_NAME,
    sha256_file,
)

DEFAULT_ECONOMIES = ("AU", "MY", "SG", "CN", "ID", "IN")
DEFAULT_ENGINES = ("engine-a", "engine-b")
FIXTURE_SOURCE_KIND = "fixture"

# Every table the working database holds, each with a pruning rule below. A
# table not named here refuses the pack.
KNOWN_TABLES = frozenset({
    "documents", "chunks", "mappings", "source_groups", "mapping_relationships",
    "reviews", "review_history", "glosses", "crawl_manifest", "shortlist_windows",
    "document_words", "gate_scores", "audit_log", "runs", "extractions", "sqlite_sequence",
})

# An absolute path into someone's home or a machine's temporary folder. The
# look-behind keeps a web address (`https://example.gov/home/...`) and a
# relative path (`data/home/...`) out of it: only a path that starts the text,
# or follows a space, a quote or a bracket, counts.
ABSOLUTE_PATH = re.compile(
    r"(?<![\w.:/~-])(?:/Users/|/home/|/root/|/private/var/|/private/tmp/|/var/folders/|/tmp/)"
    r"|[A-Za-z]:\\+Users\\+"
)
# The same test, loose enough for SQL to pre-filter rows cheaply before the
# pattern above decides.
_SQL_HINTS = ("%/Users/%", "%/home/%", "%/root/%", "%/private/%", "%/var/folders/%",
              "%/tmp/%", "%:\\Users\\%")


class PackRefused(RuntimeError):
    """The pack stopped and wrote nothing to the output folder."""


@dataclass
class PackReport:
    archive: Path
    sha256_file: Path
    manifest_file: Path
    sha256: str
    archive_bytes: int
    manifest: dict = field(default_factory=dict)


def _q(values) -> str:
    """Placeholders for an IN list. An empty list reads as one empty string, a
    value no id or code ever has: `NOT IN (NULL)` would be NULL for every row
    and keep nothing."""
    return ",".join("?" for _ in values) or "''"


def _copy_database(source: Path, target: Path) -> None:
    """A consistent snapshot of `source` into `target`, source opened read-only."""
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()


def _ids(conn: sqlite3.Connection, sql: str, params=()) -> list[str]:
    return [r[0] for r in conn.execute(sql, params)]


def _prune(
    conn: sqlite3.Connection,
    *,
    economies: tuple[str, ...],
    engines: tuple[str, ...],
    drop_runs: tuple[str, ...],
    drop_documents: tuple[str, ...],
) -> dict:
    tables = set(_ids(conn, "SELECT name FROM sqlite_master WHERE type='table'"))
    unknown = sorted(tables - KNOWN_TABLES)
    if unknown:
        raise PackRefused(
            f"the database has table(s) this script has no rule for: {', '.join(unknown)}."
            " Decide what ships from them and add a rule before packing."
        )

    all_docs = _ids(conn, "SELECT document_id FROM documents")
    keep_docs = _ids(
        conn,
        f"SELECT document_id FROM documents WHERE economy IN ({_q(economies)})"
        f" AND source_kind != ? AND document_id NOT IN ({_q(drop_documents)})",
        (*economies, FIXTURE_SOURCE_KIND, *drop_documents),
    )
    dropped_docs = sorted(set(all_docs) - set(keep_docs))

    all_runs = _ids(conn, "SELECT run_id FROM runs")
    keep_runs = _ids(
        conn,
        f"SELECT run_id FROM runs WHERE status = 'completed'"
        f" AND economy IN ({_q(economies)}) AND run_id NOT IN ({_q(drop_runs)})"
        f" AND (kind = 'discovery' OR engine IN ({_q(engines)}))",
        (*economies, *drop_runs, *engines),
    )
    dropped_runs = sorted(set(all_runs) - set(keep_runs))

    before = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}

    conn.execute("CREATE TEMP TABLE keep_docs (id TEXT PRIMARY KEY)")
    conn.execute("CREATE TEMP TABLE keep_runs (id TEXT PRIMARY KEY)")
    conn.executemany("INSERT INTO keep_docs VALUES (?)", [(d,) for d in keep_docs])
    conn.executemany("INSERT INTO keep_runs VALUES (?)", [(r,) for r in keep_runs])
    kept_mapping = (
        "(run_id IN (SELECT id FROM keep_runs) AND EXISTS (SELECT 1 FROM mappings m"
        " WHERE m.run_id = {t}.run_id AND m.mapping_id = {t}.mapping_id))"
    )
    # Children before parents, so the foreign keys hold at every step.
    if "review_history" in tables:
        conn.execute("DELETE FROM review_history")
    conn.execute("DELETE FROM reviews")
    conn.execute(
        "DELETE FROM mapping_relationships WHERE NOT "
        + kept_mapping.format(t="mapping_relationships")
        + " OR group_id NOT IN (SELECT group_id FROM source_groups"
        " WHERE run_id IN (SELECT id FROM keep_runs))"
    )
    conn.execute("DELETE FROM glosses WHERE run_id NOT IN (SELECT id FROM keep_runs)")
    conn.execute("DELETE FROM source_groups WHERE run_id NOT IN (SELECT id FROM keep_runs)")
    conn.execute(
        "DELETE FROM mappings WHERE run_id NOT IN (SELECT id FROM keep_runs)"
        " OR document_id NOT IN (SELECT id FROM keep_docs)"
    )
    # A group or a Gloss whose Mapping left with a dropped Document goes too.
    conn.execute(
        "DELETE FROM mapping_relationships WHERE NOT "
        + kept_mapping.format(t="mapping_relationships")
    )
    conn.execute("DELETE FROM glosses WHERE NOT " + kept_mapping.format(t="glosses"))
    conn.execute(
        "DELETE FROM source_groups WHERE NOT EXISTS (SELECT 1 FROM mappings m"
        " WHERE m.run_id = source_groups.run_id"
        " AND m.mapping_id = source_groups.authoritative_mapping_id)"
    )
    conn.execute(
        "DELETE FROM gate_scores WHERE chunk_id NOT IN (SELECT chunk_id FROM chunks"
        " WHERE document_id IN (SELECT id FROM keep_docs))"
    )
    for table in ("chunks", "shortlist_windows", "document_words"):
        conn.execute(f"DELETE FROM {table} WHERE document_id NOT IN (SELECT id FROM keep_docs)")
    conn.execute("DELETE FROM documents WHERE document_id NOT IN (SELECT id FROM keep_docs)")
    conn.execute(
        "DELETE FROM extractions WHERE source_sha256 NOT IN"
        " (SELECT source_sha256 FROM documents)"
    )
    conn.execute(
        f"DELETE FROM crawl_manifest WHERE economy NOT IN ({_q(economies)})", economies
    )
    conn.execute("DELETE FROM runs WHERE run_id NOT IN (SELECT id FROM keep_runs)")
    named = dropped_runs + dropped_docs
    for chunk_start in range(0, len(named), 200):
        part = named[chunk_start:chunk_start + 200]
        clause = " OR ".join(["decision LIKE ? OR method LIKE ?"] * len(part))
        params = [p for name in part for p in (f"%{name}%", f"%{name}%")]
        conn.execute(f"DELETE FROM audit_log WHERE {clause}", params)
    conn.commit()

    after = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    return {
        "dropped_runs": dropped_runs,
        "dropped_documents": dropped_docs,
        "rows_before": before,
        "rows_after": after,
    }


def _absolute_paths(conn: sqlite3.Connection) -> list[str]:
    """Every (table, column) whose text holds an absolute home or temporary
    path, with the first offending value shortened, plus stored text streams."""
    hits: list[str] = []
    for (table,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name != 'sqlite_sequence'"
    ).fetchall():
        columns = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        for column in columns:
            where = " OR ".join(f'"{column}" LIKE ?' for _ in _SQL_HINTS)
            rows = conn.execute(
                f'SELECT "{column}" FROM {table} WHERE typeof("{column}") = \'text\''
                f" AND ({where})",
                _SQL_HINTS,
            )
            for (value,) in rows:
                match = ABSOLUTE_PATH.search(value)
                if match:
                    start = max(0, match.start() - 20)
                    hits.append(f"{table}.{column}: ...{value[start:match.end() + 40]}...")
                    break
    for key, blob in conn.execute("SELECT extraction_key, canonical_json FROM extractions"):
        text = zlib.decompress(blob).decode("utf-8", errors="replace")
        match = ABSOLUTE_PATH.search(text)
        if match:
            hits.append(f"extractions.canonical_json ({key[:12]}): {text[match.start():match.end() + 40]}")
    return hits


def _schema_sha256(conn: sqlite3.Connection) -> str:
    ddl = sorted(
        (r[0] or "") for r in conn.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
        )
    )
    return hashlib.sha256("\n".join(ddl).encode("utf-8")).hexdigest()


def _collect_raw_files(conn: sqlite3.Connection, data_dir: Path) -> list[tuple[Path, str]]:
    """(file on disk, name in the archive) for every kept Document, local_path
    rewritten to that name."""
    out: list[tuple[Path, str]] = []
    missing: list[str] = []
    rows = conn.execute(
        "SELECT document_id, economy, local_path, source_sha256 FROM documents"
        " ORDER BY economy, document_id"
    ).fetchall()
    for document_id, economy, local_path, sha in rows:
        found = resolve_stored_path(local_path, data_dir, economy=economy, sha256=sha)
        if found is None:
            missing.append(f"{document_id} ({local_path})")
            continue
        name = f"{economy}/raw/{found.path.name}"
        if name != local_path:
            conn.execute(
                "UPDATE documents SET local_path = ? WHERE document_id = ?",
                (name, document_id),
            )
        out.append((found.path, name))
    conn.commit()
    if missing:
        raise PackRefused(
            f"{len(missing)} kept Document(s) have no bytes under {data_dir}, so their"
            f" rows would open with no source beside them: {', '.join(missing[:5])}"
        )
    names = [n for _, n in out]
    if len(set(names)) != len(names):
        raise PackRefused("two kept Documents store their bytes under the same name")
    return out


def _manifest(conn: sqlite3.Connection, raws: list[tuple[Path, str]], pruned: dict,
              *, db_file: Path, source_db: Path, economies: tuple[str, ...]) -> dict:
    per_economy: dict[str, dict] = {}
    for code in economies:
        runs = conn.execute(
            "SELECT run_id, kind, engine, pillars FROM runs WHERE economy = ?"
            " ORDER BY started_at",
            (code,),
        ).fetchall()
        per_economy[code] = {
            "documents": conn.execute(
                "SELECT COUNT(*) FROM documents WHERE economy = ?", (code,)
            ).fetchone()[0],
            "raw_files": sum(1 for _, n in raws if n.startswith(f"{code}/")),
            "raw_bytes": sum(p.stat().st_size for p, n in raws if n.startswith(f"{code}/")),
            "mappings": conn.execute(
                "SELECT COUNT(*) FROM mappings WHERE economy = ?", (code,)
            ).fetchone()[0],
            "runs": [
                {"run_id": r[0], "engine": r[2], "pillars": json.loads(r[3] or "[]")}
                for r in runs if r[1] == "run"
            ],
            "discoveries": [r[0] for r in runs if r[1] == "discovery"],
        }
    return {
        "format": BUNDLE_FORMAT,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_database": source_db.name,
        "economies": per_economy,
        "database": {
            "file": ARCHIVE_DB_NAME,
            "bytes": db_file.stat().st_size,
            "sha256": sha256_file(db_file),
            "schema_sha256": _schema_sha256(conn),
            "tables": pruned["rows_after"],
        },
        "dropped": {
            "runs": pruned["dropped_runs"],
            "documents": pruned["dropped_documents"],
            "reviews": pruned["rows_before"].get("reviews", 0),
        },
    }


def _add(archive: tarfile.TarFile, path: Path, name: str) -> None:
    info = archive.gettarinfo(str(path), arcname=name)
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mode = 0o644
    with path.open("rb") as handle:
        archive.addfile(info, handle)


def pack(
    db: Path,
    data_dir: Path,
    out_dir: Path,
    *,
    name: str | None = None,
    economies: tuple[str, ...] = DEFAULT_ECONOMIES,
    engines: tuple[str, ...] = DEFAULT_ENGINES,
    drop_runs: tuple[str, ...] = (),
    drop_documents: tuple[str, ...] = (),
    progress=lambda _msg: None,
) -> PackReport:
    db = Path(db)
    data_dir = Path(data_dir)
    out_dir = Path(out_dir)
    if not db.is_file():
        raise PackRefused(f"no database at {db}")
    name = name or f"regcompass-prepared-{datetime.now(timezone.utc):%Y%m%d}"
    archive_path = out_dir / f"{name}.tar.gz"
    if archive_path.exists():
        raise PackRefused(f"{archive_path} already exists; remove it or pass another --name")

    with tempfile.TemporaryDirectory(prefix="regcompass-pack-") as tmp:
        work = Path(tmp) / ARCHIVE_DB_NAME
        progress(f"copying {db} (backup API, read-only)")
        _copy_database(db, work)
        conn = sqlite3.connect(work)
        try:
            conn.execute("PRAGMA journal_mode = DELETE")
            conn.execute("PRAGMA foreign_keys = ON")
            progress(f"pruning to {', '.join(economies)} on {', '.join(engines)}")
            pruned = _prune(
                conn, economies=economies, engines=engines,
                drop_runs=tuple(drop_runs), drop_documents=tuple(drop_documents),
            )
            raws = _collect_raw_files(conn, data_dir)
            broken = conn.execute("PRAGMA foreign_key_check").fetchall()
            if broken:
                raise PackRefused(f"the pruned copy breaks {len(broken)} foreign key(s): {broken[:3]}")
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise PackRefused(f"the pruned copy fails its integrity check: {integrity}")
            hits = _absolute_paths(conn)
            if hits:
                raise PackRefused(
                    "absolute home or temporary paths remain in the pruned copy; fix"
                    " the rows (or drop them) and pack again:\n  " + "\n  ".join(hits)
                )
            progress("compacting the copy")
            conn.execute("VACUUM")
            manifest = _manifest(conn, raws, pruned, db_file=work, source_db=db,
                                 economies=economies)
        finally:
            conn.close()

        manifest_path = Path(tmp) / MANIFEST_NAME
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                                 encoding="utf-8")
        out_dir.mkdir(parents=True, exist_ok=True)
        partial = out_dir / f".{name}.tar.gz.partial"
        progress(f"writing {archive_path} ({len(raws)} raw files)")
        try:
            with tarfile.open(partial, "w:gz", compresslevel=6) as archive:
                _add(archive, work, ARCHIVE_DB_NAME)
                _add(archive, manifest_path, MANIFEST_NAME)
                for path, member in sorted(raws, key=lambda pair: pair[1]):
                    _add(archive, path, member)
            partial.replace(archive_path)
        finally:
            partial.unlink(missing_ok=True)

    digest = sha256_file(archive_path)
    sha_path = out_dir / f"{name}.tar.gz.sha256"
    sha_path.write_text(f"{digest}  {archive_path.name}\n", encoding="utf-8")
    manifest_copy = out_dir / f"{name}.manifest.json"
    manifest_copy.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                             encoding="utf-8")
    return PackReport(
        archive=archive_path,
        sha256_file=sha_path,
        manifest_file=manifest_copy,
        sha256=digest,
        archive_bytes=archive_path.stat().st_size,
        manifest=manifest,
    )


def _codes(text: str) -> tuple[str, ...]:
    return tuple(c.strip() for c in text.split(",") if c.strip())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Pack the prepared database and its Corpus bytes into one release archive."
    )
    parser.add_argument("--db", type=Path, default=Path("data/regcompass.db"),
                        help="working database to pack (read only)")
    parser.add_argument("--data-dir", type=Path, default=Path("data"),
                        help="root the Documents' <ECONOMY>/raw/ bytes live under")
    parser.add_argument("--out", type=Path, required=True,
                        help="folder the archive, its .sha256 and its manifest are written to")
    parser.add_argument("--name", default=None,
                        help="archive base name (default regcompass-prepared-<UTC date>)")
    parser.add_argument("--economies", type=_codes, default=DEFAULT_ECONOMIES,
                        help="comma-separated Economy codes to keep (default %(default)s)")
    parser.add_argument("--engines", type=_codes, default=DEFAULT_ENGINES,
                        help="comma-separated Engines whose Runs are kept (default %(default)s)")
    parser.add_argument("--drop-run", action="append", default=[],
                        help="a Run id to leave out even though it qualifies (repeatable)")
    parser.add_argument("--drop-document", action="append", default=[],
                        help="a Document id to leave out (repeatable)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = pack(
            args.db, args.data_dir, args.out, name=args.name,
            economies=tuple(c.upper() for c in args.economies), engines=args.engines,
            drop_runs=tuple(args.drop_run), drop_documents=tuple(args.drop_document),
            progress=print,
        )
    except PackRefused as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    print(f"\n{report.archive}  {report.archive_bytes / 1e6:.1f} MB")
    print(f"sha256 {report.sha256}")
    for code, entry in report.manifest["economies"].items():
        print(f"  {code}: {entry['documents']} Documents, {len(entry['runs'])} Runs,"
              f" {entry['mappings']} Mappings, {entry['raw_bytes'] / 1e6:.1f} MB raw")
    dropped = report.manifest["dropped"]
    print(f"left out: {len(dropped['runs'])} Run Record(s), {len(dropped['documents'])}"
          f" Document(s), {dropped['reviews']} Review Decision(s)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
