"""Storage and vector-search seams.

These two classes are the ONLY code that touches persistence, so the documented
PostgreSQL + pgvector swap (Phase 3) is a second implementation of the same
surface, not a rewrite. Keep every SQL statement inside this module.

VectorIndex is numpy exact brute-force cosine: at our scale (10^3-10^4 chunks)
that is sub-millisecond, and it avoids the sqlite-vec pre-v1 alpha trap.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
import zlib
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

import numpy as np

from regcompass.contracts import (
    GLOSS_LABEL,
    GlossRecord,
    Review,
    StageLogEntry,
    manifest_key_for_upload,
)

SCHEMA_FILENAME = "schema.sql"


def _schema_sql() -> str:
    """schema.sql lives at the repo root (checked in, raw SQL); fall back to a
    packaged copy if the project is pip-installed without the repo."""
    root_copy = Path(__file__).resolve().parents[2] / SCHEMA_FILENAME
    if root_copy.exists():
        return root_copy.read_text(encoding="utf-8")
    return (resources.files("regcompass") / SCHEMA_FILENAME).read_text(encoding="utf-8")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_now_z() -> str:
    """The Run Record's timestamp spelling: ISO 8601 UTC with a trailing Z.
    Microseconds are kept so two records opened in the same second still sort."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# The two Run Record kinds and their id prefixes. A Run and a Discovery live in
# one table (one place to read "what has this server done"), and the prefix
# makes a bare id self-describing in a filename or a hand-in.
RUN_ID_PREFIXES = {"run": "run", "discovery": "disc"}

# The Run a row is filed under when it predates Run ownership: a database
# written before mappings.run_id existed keeps every row, attributed honestly to
# no Run at all rather than to whichever Run happens to be newest.
LEGACY_RUN_ID = "legacy"


def new_run_id(kind: str) -> str:
    """`run_<YYYYMMDDTHHMMSSZ>_<6 hex>` for a Run, `disc_...` for a Discovery.
    The timestamp makes the id readable and roughly sortable; the random tail
    keeps two records opened in the same second distinct."""
    import secrets

    try:
        prefix = RUN_ID_PREFIXES[kind]
    except KeyError:
        raise ValueError(
            f"unknown Run Record kind '{kind}': expected one of"
            f" {', '.join(sorted(RUN_ID_PREFIXES))}"
        ) from None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}_{stamp}_{secrets.token_hex(3)}"


# JSON-encoded columns on the runs table, decoded on the way out so a caller
# never parses storage's own encoding.
_RUN_JSON_COLUMNS = ("pillars", "indicators", "details")


def decode_run_row(row) -> dict:
    """One runs row as a plain dict with its JSON columns decoded."""
    record = dict(row)
    for column in _RUN_JSON_COLUMNS:
        value = record.get(column)
        if isinstance(value, (str, bytes)):
            try:
                record[column] = json.loads(value)
            except json.JSONDecodeError:  # pragma: no cover - defensive
                record[column] = None
    return record


@dataclass(frozen=True)
class ClearReport:
    """What a clear removed, or what a preview says it would remove.

    One shape for both, so the confirmation a reviewer reads and the receipt
    they get afterwards are the same counts and can be compared directly.
    `refused_files` is the honest half of the guarantee: a stored path that
    resolves outside the data root is left alone and counted here rather than
    deleted or silently dropped.
    """

    economy: str | None = None
    documents: int = 0
    stored_files: int = 0
    bytes: int = 0
    extractions: int = 0
    runs: int = 0
    mappings: int = 0
    reviews: int = 0
    refused_files: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class DocumentRemoval:
    """What removing one Document takes, in the counts its confirmation
    shows: the Mappings that quote it (from how many Runs), the Review
    Decisions filed against those, and the stored files."""

    document_id: str
    economy: str
    title: str
    mappings: int = 0
    runs: int = 0
    reviews: int = 0
    stored_files: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


# How long a connection waits for another process's write lock before it
# gives up with "database is locked". Several Runs may share one working
# database (the pre-run job starts them side by side), and every write here
# commits as soon as it is made, so a wait is a queue of short writes; a
# minute and more is headroom, not an expected delay. SQLite's own default is
# five seconds.
BUSY_TIMEOUT_S = 120.0

# The pause between two attempts to switch a database into write-ahead-log
# mode while another process holds its write lock: short at first, doubling,
# never longer than the ceiling. The attempts stop at BUSY_TIMEOUT_S.
WAL_SWITCH_FIRST_PAUSE_S = 0.05
WAL_SWITCH_MAX_PAUSE_S = 1.0

# How long a closing connection waits to fold the write-ahead log back into
# the main file. Bounded and short, because closing must never stall behind
# another Run: when that Run closes last, SQLite folds the log in then.
CLOSE_CHECKPOINT_TIMEOUT_MS = 2000


class Storage:
    """Thin SQLite wrapper owning the connection and the schema.

    Every connection runs in write-ahead-log mode with a long busy timeout, so
    several processes can use one database at once: readers never block the
    writer, and a writer waits its turn for the lock instead of failing. The
    mode is a property of the database file, which is why the log (`-wal`) and
    its index (`-shm`) appear beside it while a connection is open. `close()`
    folds the log back in, so a database nobody has open is one complete file.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.conn = sqlite3.connect(self.db_path, timeout=BUSY_TIMEOUT_S)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_S * 1000)}")
        self._use_write_ahead_log()
        self.conn.execute("PRAGMA foreign_keys = ON")

    @classmethod
    def read_only(cls, db_path: str | Path) -> "Storage":
        """A Storage over a read-only connection: no journal-mode switch, no
        schema, and SQLite refuses any write. For a lane that must only look
        (a dry run), so the database file is left exactly as it was."""
        self = cls.__new__(cls)
        self.db_path = Path(db_path)
        uri = self.db_path.resolve().as_uri() + "?mode=ro"
        self.conn = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_S)
        self.conn.row_factory = sqlite3.Row
        return self

    def _use_write_ahead_log(self) -> None:
        """Put the database file in write-ahead-log mode, once.

        A file already in the mode is left alone: reading the mode takes no
        lock, and every open after the first does only that. Switching takes
        the write lock, and a connection that asks for it while another
        process holds it (the case when several Runs open a fresh database at
        the same moment) is refused at once rather than queued behind the busy
        timeout. So the switch is tried again with a short, growing pause
        until the busy timeout has passed, then given up with a message that
        says what was being attempted."""
        mode = self.conn.execute("PRAGMA journal_mode").fetchone()[0]
        if str(mode).lower() == "wal":
            return
        deadline = time.monotonic() + BUSY_TIMEOUT_S
        pause = WAL_SWITCH_FIRST_PAUSE_S
        while True:
            try:
                self.conn.execute("PRAGMA journal_mode = WAL").fetchone()
                return
            except sqlite3.OperationalError as e:
                if "locked" not in str(e) and "busy" not in str(e):
                    raise
                left = deadline - time.monotonic()
                if left <= 0:
                    self.conn.close()
                    raise sqlite3.OperationalError(
                        f"database is locked: could not switch {self.db_path} to"
                        f" write-ahead-log mode within {BUSY_TIMEOUT_S:g} s,"
                        " because another process held its write lock the whole time"
                    ) from e
                time.sleep(min(pause, left))
                pause = min(pause * 2, WAL_SWITCH_MAX_PAUSE_S)

    # Columns added to existing tables after S0. CREATE TABLE IF NOT EXISTS
    # never alters an existing table, so apply_schema back-fills these on DBs
    # created before the column existed (fresh DBs get them from schema.sql).
    _MIGRATIONS = {
        "documents": {
            "title": "TEXT",
            "n_pages": "INTEGER",
            "n_low_yield_pages": "INTEGER",
            # OCR quality proxies (M2), added after S0: back-filled on DBs
            # created before the columns existed so document_meta() has them.
            "mean_word_confidence": "REAL",
            "dictionary_hit_rate": "REAL",
            "cer_proxy_flag": "INTEGER",
            "escalated_to_rapidocr": "INTEGER",
            "manual_review": "INTEGER",
            # How the Document entered the Corpus. Every row that
            # predates the column was written by Discovery, so the back-fill
            # default states the truth rather than guessing.
            "source_kind": "TEXT NOT NULL DEFAULT 'discovery'",
            # A secondary reference the Portal published beside this Document:
            # the Lao Official Gazette's English rendering. Null on
            # every row that predates the column, which is the truth about them.
            "notes": "TEXT",
            # A reviewer's correction of the title or the Source URL. Null on
            # every row that predates the columns: nobody had edited them.
            "edited_fields": "TEXT",
            "edited_by": "TEXT",
            "edited_at": "TEXT",
            # The title before its first edit, for the KNOWN/NEW match.
            "derived_title": "TEXT",
        },
        "crawl_manifest": {
            # Where that reference is recorded first: Discovery finds it on the
            # listing row, ingest copies it onto the Corpus row.
            "notes": "TEXT",
            # The Portal listing's own name for the Document, the same way.
            "title": "TEXT",
        },
        "extractions": {
            # Provenance of an OCR'd stream, added after the table:
            # which tesseract read it and which traineddata files it loaded.
            # Null on every row that predates the columns, and on every stream
            # that never went near OCR.
            "ocr_engine_version": "TEXT",
            "tessdata_sha256": "TEXT",
        },
    }

    # Per-document OCR proxy columns emitted in submission.json's ocr_quality
    # block. Kept beside the schema so document_meta() reads a single source.
    _OCR_PROXY_COLUMNS = (
        "mean_word_confidence",
        "dictionary_hit_rate",
        "cer_proxy_flag",
        "escalated_to_rapidocr",
        "manual_review",
    )

    # Tables whose PRIMARY KEY gained run_id. A composite key cannot be added by
    # ALTER TABLE, so these need SQLite's table-rebuild dance rather than the
    # ADD COLUMN back-fill above.
    _RUN_SCOPED_TABLES = ("mappings", "source_groups", "mapping_relationships", "reviews")
    _REBUILD_SUFFIX = "__pre_run_scope"

    # The key each rebuilt table must end up with. A database can carry run_id
    # and STILL have the wrong key: `reviews` was append-only under a
    # review_id key before Review Decisions became one-per-Mapping, and
    # rebuilding it is what makes the upsert possible.
    _EXPECTED_KEYS = {
        "mappings": ("run_id", "mapping_id"),
        "source_groups": ("group_id",),
        "mapping_relationships": ("run_id", "mapping_id", "group_id"),
        "reviews": ("run_id", "mapping_id"),
    }

    # Tables whose CHECK constraint gained a value after S0, and the value that
    # proves a database already has it. SQLite cannot alter a CHECK in place, so
    # a database written before the value needs the same table-rebuild dance the
    # run-scoped tables use: park it, let schema.sql build the new one, copy the
    # rows back.
    # The quotes matter for `reviews`: the column corrected_indicator_id also
    # contains the word, and only the CHECK value proves the table is current.
    _CHECK_REBUILD = {"runs": "interrupted", "reviews": "'corrected'"}

    def apply_schema(self) -> None:
        self._finish_interrupted_rebuild()
        stale = self._tables_needing_rebuild()
        if stale:
            self._park_for_rebuild(stale)
        self.conn.executescript(_schema_sql())
        if stale:
            self._copy_parked_rows(stale)
        for table in self._MIGRATIONS:
            self._backfill_columns(table)
        self._backfill_review_history()
        self.conn.commit()

    def _finish_interrupted_rebuild(self) -> None:
        """Finish a rebuild a crash cut short, before anything else looks at
        the tables.

        A rebuild parks the old table under a suffix, lets schema.sql build the
        new one, then copies the rows across. A process that died in between
        leaves the rows in the parked table, and the next rebuild of the same
        table would drop it. So a parked table found on open is dealt with
        first: with no live table beside it (the crash came before schema.sql
        ran) it simply takes its name back, and the ordinary rebuild below then
        runs from the start; with a live table beside it (the crash came before
        the copy) its rows are copied in now and it is dropped."""
        rebuilt = (*self._RUN_SCOPED_TABLES, *self._CHECK_REBUILD)
        existing = self.table_names()
        parked = [
            t for t in dict.fromkeys(rebuilt) if f"{t}{self._REBUILD_SUFFIX}" in existing
        ]
        if not parked:
            return
        self.conn.commit()
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.conn.execute("PRAGMA legacy_alter_table = ON")
        to_copy: dict[str, list[str]] = {}
        for table in parked:
            aside = f"{table}{self._REBUILD_SUFFIX}"
            if table in existing:
                to_copy[table] = self._columns(aside)
            else:
                self.conn.execute(f"ALTER TABLE {aside} RENAME TO {table}")
        self.conn.commit()
        if to_copy:
            self._copy_parked_rows(to_copy)  # restores both pragmas
        else:
            self.conn.execute("PRAGMA legacy_alter_table = OFF")
            self.conn.execute("PRAGMA foreign_keys = ON")

    def _backfill_review_history(self) -> None:
        """Give every standing Review Decision that has no history row one,
        taken from the decision itself. A database made before the history
        existed would otherwise lose its earlier decision the first time a
        reviewer changed it. Only decisions with no history at all are copied,
        so a second open adds nothing."""
        tables = self.table_names()
        if "reviews" not in tables or "review_history" not in tables:
            return
        self.conn.execute(
            "INSERT INTO review_history (run_id, mapping_id, review_status,"
            " corrected_indicator_id, reviewer, decided_at, comment)"
            " SELECT r.run_id, r.mapping_id, r.review_status,"
            " r.corrected_indicator_id, r.reviewer, r.reviewed_at, r.comment"
            " FROM reviews r WHERE NOT EXISTS (SELECT 1 FROM review_history h"
            " WHERE h.run_id = r.run_id AND h.mapping_id = r.mapping_id)"
            " ORDER BY r.rowid"
        )

    def _backfill_columns(self, table: str) -> None:
        """Add the columns this table gained after its first release. CREATE
        TABLE IF NOT EXISTS never alters an existing table, so a database made
        by an older build reaches the current shape only here."""
        columns = self._MIGRATIONS.get(table)
        if not columns:
            return
        existing = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
        if not existing:  # the table itself is not there yet
            return
        for name, decl in columns.items():
            if name not in existing:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def _columns(self, table: str) -> list[str]:
        return [r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")]

    def _primary_key(self, table: str) -> tuple[str, ...]:
        """This table's primary key as SQLite reports it, in key order."""
        cols = [r for r in self.conn.execute(f"PRAGMA table_info({table})") if r["pk"]]
        return tuple(r["name"] for r in sorted(cols, key=lambda r: r["pk"]))

    def _tables_needing_rebuild(self) -> dict[str, list[str]]:
        """The run-scoped tables this database still carries in an older shape,
        each with the columns worth copying forward: either run_id is missing
        altogether, or the primary key is not the one schema.sql now declares.
        An empty mapping is the common case: a fresh database, or one already
        rebuilt."""
        existing = self.table_names()
        stale: dict[str, list[str]] = {}
        for table in self._RUN_SCOPED_TABLES:
            if table not in existing:
                continue
            columns = self._columns(table)
            wrong_key = self._primary_key(table) != self._EXPECTED_KEYS[table]
            if "run_id" not in columns or wrong_key:
                stale[table] = columns
        for table, token in self._CHECK_REBUILD.items():
            if table not in existing or table in stale:
                continue
            row = self.conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            if row is not None and token not in (row["sql"] or ""):
                stale[table] = self._columns(table)
        return stale

    def _park_for_rebuild(self, stale: dict[str, list[str]]) -> None:
        """Rename the old tables aside so schema.sql's own CREATE statements
        build the new ones. Reusing schema.sql is what keeps the migrated shape
        and the fresh-database shape identical by construction, instead of a
        second copy of the DDL drifting inside this module.

        Indexes follow a renamed table, so they are dropped first: otherwise
        CREATE INDEX IF NOT EXISTS would see the name taken, skip the new index,
        and then lose it with the parked table."""
        self.conn.commit()
        self.conn.execute("PRAGMA foreign_keys = OFF")
        # legacy_alter_table keeps RENAME from rewriting the foreign keys of
        # tables that point at these; every one of them is rebuilt here anyway.
        self.conn.execute("PRAGMA legacy_alter_table = ON")
        for table in stale:
            for row in self.conn.execute(f"PRAGMA index_list({table})").fetchall():
                if row["origin"] == "c":  # named CREATE INDEX, not an autoindex
                    self.conn.execute(f"DROP INDEX IF EXISTS {row['name']}")
            self.conn.execute(f"DROP TABLE IF EXISTS {table}{self._REBUILD_SUFFIX}")
            self.conn.execute(
                f"ALTER TABLE {table} RENAME TO {table}{self._REBUILD_SUFFIX}"
            )
        self.conn.commit()

    def _copy_parked_rows(self, stale: dict[str, list[str]]) -> None:
        """Move every parked row into its rebuilt table: under its own run_id
        when the old table had one, under LEGACY_RUN_ID when it did not.
        Columns the old database never had are left to their defaults, so a
        database from any earlier schema still opens.

        The copy is INSERT OR REPLACE in rowid order, because a table that gains
        a narrower key can hold rows that now collide: the append-only reviews
        table kept every decision a reviewer ever made, and the rule there has
        always been that the LATEST one governs. Row order is that rule."""
        # schema.sql opens with `PRAGMA foreign_keys = ON`, so the switch
        # _park_for_rebuild threw is back on by now. The parked tables still
        # carry their old single-column references to `mappings`, which no
        # longer match its composite key, and SQLite refuses to read from them
        # while enforcement is on. They are dropped a few lines below.
        self.conn.commit()
        self.conn.execute("PRAGMA foreign_keys = OFF")
        for table, old_columns in stale.items():
            new_columns = set(self._columns(table))
            shared = [c for c in old_columns if c in new_columns and c != "run_id"]
            had_run_id = "run_id" in old_columns
            names = ", ".join(["run_id", *shared])
            self.conn.execute(
                f"INSERT OR REPLACE INTO {table} ({names})"
                f" SELECT {'run_id' if had_run_id else '?'}, {', '.join(shared)}"
                f" FROM {table}{self._REBUILD_SUFFIX} ORDER BY rowid",
                () if had_run_id else (LEGACY_RUN_ID,),
            )
            self.conn.execute(f"DROP TABLE {table}{self._REBUILD_SUFFIX}")
        self.conn.commit()
        self.conn.execute("PRAGMA legacy_alter_table = OFF")
        self.conn.execute("PRAGMA foreign_keys = ON")
        violations = self.conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:  # pragma: no cover - the rebuild is copy-only
            raise RuntimeError(
                f"the schema migration left {len(violations)} dangling reference(s):"
                f" {violations[:3]}"
            )

    def table_names(self) -> set[str]:
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return {r["name"] for r in rows}

    def ensure_table(self, table: str) -> None:
        """Add one table (and its indexes) from schema.sql to a database that
        predates it.

        The same trick the run-scope rebuild uses: schema.sql stays the single
        copy of the DDL, so a table added on first use and a table created with
        the rest of the schema cannot drift apart. A database made by an older
        build opens either way; it just gains the table the moment something
        writes to it."""
        if table in self.table_names():
            self._backfill_columns(table)
            return
        for statement in _schema_sql().split(";"):
            # Splitting on ";" leaves each statement carrying the comment block
            # written above it, so the comments come off before the statement is
            # recognised by its first word.
            body = "\n".join(
                line for line in statement.splitlines()
                if not line.strip().startswith("--")
            ).strip()
            if body.upper().startswith("CREATE") and table in body:
                self.conn.execute(body)
        self.conn.commit()

    def extraction_get(self, extraction_key: str) -> str | None:
        """The canonical stream stored under this key, as the JSON it was
        stored as, or None when it is not there.

        A database with no extractions table has no stored stream rather than
        an error (the glosses rule): the reader lanes open whatever database an
        operator points at, and one written before this table existed must not
        take a Run down. The caller reads the Document itself, which is what it
        did before the cache existed."""
        try:
            row = self.conn.execute(
                "SELECT canonical_json FROM extractions WHERE extraction_key = ?",
                (extraction_key,),
            ).fetchone()
        except sqlite3.Error:  # a database with no extractions table
            return None
        if row is None:
            return None
        return zlib.decompress(row["canonical_json"]).decode("utf-8")

    def extraction_store(
        self,
        extraction_key: str,
        canonical_json: str,
        *,
        source_sha256: str,
        extractor_version: str,
        ocr_languages: str,
        ocr_policy_id: str,
        source_format: str,
        extractor: str,
        ocr_applied: bool,
        n_chars: int,
        n_pages: int,
        n_low_yield_pages: int,
        ocr_engine_version: str | None = None,
        tessdata_sha256: str | None = None,
    ) -> None:
        """Keep one canonical stream under its key, replacing any earlier copy.

        The payload is deflated because the word boxes of a 200-page scanned Act
        are far larger than its text, and they are exactly what makes the stored
        stream worth having: without them a reused Run could not draw the audit
        highlight. Deflate is lossless, so what comes back is byte-for-byte what
        the extractor produced.

        ocr_engine_version and tessdata_sha256 are provenance for a stream OCR
        read: they say which tesseract and which language files produced it.
        They are stored, never keyed."""
        self.ensure_table("extractions")
        self.conn.execute(
            "INSERT INTO extractions (extraction_key, source_sha256, extractor_version,"
            " ocr_languages, ocr_policy_id, source_format, extractor, ocr_applied,"
            " ocr_engine_version, tessdata_sha256,"
            " n_chars, n_pages, n_low_yield_pages, canonical_json, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(extraction_key) DO UPDATE SET"
            " canonical_json = excluded.canonical_json,"
            " extractor = excluded.extractor,"
            " ocr_applied = excluded.ocr_applied,"
            " ocr_engine_version = excluded.ocr_engine_version,"
            " tessdata_sha256 = excluded.tessdata_sha256,"
            " n_chars = excluded.n_chars,"
            " n_pages = excluded.n_pages,"
            " n_low_yield_pages = excluded.n_low_yield_pages,"
            " created_at = excluded.created_at",
            (
                extraction_key, source_sha256, extractor_version, ocr_languages,
                ocr_policy_id, source_format, extractor, int(ocr_applied),
                ocr_engine_version, tessdata_sha256,
                n_chars, n_pages, n_low_yield_pages,
                zlib.compress(canonical_json.encode("utf-8")),
                utc_now_iso(),
            ),
        )
        self.conn.commit()

    def write_audit(self, entry: StageLogEntry) -> None:
        self.conn.execute(
            "INSERT INTO audit_log (stage, input_hash, output_hash, method, decision,"
            " duration_ms, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                entry.stage,
                entry.input_hash,
                entry.output_hash,
                entry.method,
                entry.decision,
                entry.duration_ms,
                entry.timestamp.isoformat(),
            ),
        )
        self.conn.commit()

    def upsert_document(
        self,
        document_id: str,
        economy: str,
        source_sha256: str,
        **optional: object,
    ) -> None:
        """Minimal document row so chunk/embedding rows can attach (M4/M5).
        On conflict only the PROVIDED fields are overwritten: a thin re-upsert
        (id/economy/sha) must never blank full_text, title, or page stats, and
        created_at keeps the original row's value."""
        cols = {"document_id": document_id, "economy": economy, "source_sha256": source_sha256}
        cols.update(optional)
        names = ", ".join([*cols, "created_at"])
        marks = ", ".join("?" * (len(cols) + 1))
        updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "document_id")
        self.conn.execute(
            f"INSERT INTO documents ({names}) VALUES ({marks})"
            f" ON CONFLICT(document_id) DO UPDATE SET {updates}",
            (*cols.values(), utc_now_iso()),
        )
        self.conn.commit()

    def upsert_chunks(self, chunks: Iterable) -> None:
        """Persist Chunk contract rows (text lives in documents.full_text; the
        chunks table stores offsets only, per the slice-not-reemit rule)."""
        now = utc_now_iso()
        self.conn.executemany(
            "INSERT OR REPLACE INTO chunks (chunk_id, document_id, char_start, char_end,"
            " section_label, chunk_kind, page_start, page_end, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (c.chunk_id, c.document_id, c.char_start, c.char_end,
                 c.section_label, c.chunk_kind, c.page_start, c.page_end, now)
                for c in chunks
            ],
        )
        self.conn.commit()

    _MAPPING_COLS = (
        "run_id", "mapping_id", "chunk_id", "document_id", "economy", "indicator_id",
        "indicator_name", "section", "subsection", "verbatim_quote",
        "page_number", "impact", "rdtii_score_contribution",
        "insufficient_evidence", "confidence", "verification_status",
        "discovery_tag", "measure_type", "timeline_adopted",
        "timeline_entry_into_force", "timeline_last_amended",
        "timeline_repeal_status", "source_archived_url", "source_access_date",
        "uncertainty_flags", "controlling_evidence", "novelty_scope",
        "relationship_to_group", "extraction_attempts",
    )

    def upsert_mappings(self, records: Iterable, *, run_id: str) -> None:
        """Persist MappingRecord contract rows (judge-path M6..M8) as the work
        of one Run. Timeline flattens to the four timeline_* columns;
        uncertainty_flags stores as a JSON array; booleans as 0/1.

        INSERT OR REPLACE is now scoped by the composite key, which is the whole
        point: the M8 pass re-upserts the same ids IN PLACE inside its own Run
        (controlling_evidence and relationship_to_group land on the rows that
        M6 wrote), while a second Run over the same Economy adds its own set
        beside the first instead of destroying it. run_id is required rather
        than defaulted: a caller that forgets it would silently re-create the
        bug this key exists to fix."""
        if not run_id:
            raise ValueError("upsert_mappings needs the Run id these records belong to")
        now = utc_now_iso()
        rows = []
        for r in records:
            rows.append((
                run_id, r.mapping_id, r.chunk_id, r.document_id, r.economy,
                r.indicator_id, r.indicator_name, r.section, r.subsection,
                r.verbatim_quote, r.page_number, r.impact,
                r.rdtii_score_contribution, int(r.insufficient_evidence),
                r.confidence, r.verification_status, r.discovery_tag,
                r.measure_type, r.timeline.adopted, r.timeline.entry_into_force,
                r.timeline.last_amended, r.timeline.repeal_status,
                r.source_archived_url, r.source_access_date,
                json.dumps(list(r.uncertainty_flags)),
                None if r.controlling_evidence is None else int(r.controlling_evidence),
                r.novelty_scope, r.relationship_to_group, r.extraction_attempts,
                now,
            ))
        marks = ", ".join("?" * (len(self._MAPPING_COLS) + 1))
        self.conn.executemany(
            f"INSERT OR REPLACE INTO mappings ({', '.join(self._MAPPING_COLS)},"
            f" created_at) VALUES ({marks})",
            rows,
        )
        self.conn.commit()

    def load_mappings(
        self,
        economy: str | None = None,
        verification_status: str | None = None,
        run_id: str | None = None,
    ) -> list:
        """MappingRecord contract objects back out of the mappings table (the
        judge-path export input). Filters are optional and ANDed.

        run_id=None means every Run in the database, which is only ever what a
        caller wants for a whole-table view (the `drops` listing, a migration
        check). Reconciliation, export and the audit view all name a Run:
        mixing two Engines' records would put two controlling rows in one
        group and ship whichever the ladder happened to pick."""
        from regcompass.contracts import MappingRecord, Timeline

        clauses, params = [], []
        if economy is not None:
            clauses.append("economy = ?")
            params.append(economy)
        if verification_status is not None:
            clauses.append("verification_status = ?")
            params.append(verification_status)
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"SELECT * FROM mappings{where} ORDER BY run_id, mapping_id", params
        ).fetchall()
        out = []
        for row in rows:
            out.append(MappingRecord(
                mapping_id=row["mapping_id"],
                document_id=row["document_id"],
                chunk_id=row["chunk_id"],
                economy=row["economy"],
                indicator_id=row["indicator_id"],
                indicator_name=row["indicator_name"],
                section=row["section"],
                subsection=row["subsection"],
                verbatim_quote=row["verbatim_quote"],
                page_number=row["page_number"],
                impact=row["impact"],
                rdtii_score_contribution=row["rdtii_score_contribution"],
                insufficient_evidence=bool(row["insufficient_evidence"]),
                confidence=row["confidence"],
                verification_status=row["verification_status"],
                discovery_tag=row["discovery_tag"],
                measure_type=row["measure_type"],
                timeline=Timeline(
                    adopted=row["timeline_adopted"],
                    entry_into_force=row["timeline_entry_into_force"],
                    last_amended=row["timeline_last_amended"],
                    repeal_status=row["timeline_repeal_status"],
                ),
                source_archived_url=row["source_archived_url"],
                source_access_date=row["source_access_date"],
                uncertainty_flags=json.loads(row["uncertainty_flags"]),
                controlling_evidence=(
                    None if row["controlling_evidence"] is None
                    else bool(row["controlling_evidence"])
                ),
                novelty_scope=row["novelty_scope"],
                relationship_to_group=row["relationship_to_group"],
                extraction_attempts=row["extraction_attempts"],
            ))
        return out

    def upsert_gate_scores(self, scores: dict[tuple[str, str], float]) -> None:
        """M5 gate cosines keyed (chunk_id, indicator_id): the export's
        gate_cosine_lookup, persisted so the M9 confidence composite can be
        rebuilt from the database alone."""
        self.conn.executemany(
            "INSERT OR REPLACE INTO gate_scores (chunk_id, indicator_id,"
            " cosine_pillar) VALUES (?, ?, ?)",
            [(c, i, v) for (c, i), v in scores.items()],
        )
        self.conn.commit()

    def load_gate_scores(self) -> dict[tuple[str, str], float]:
        rows = self.conn.execute(
            "SELECT chunk_id, indicator_id, cosine_pillar FROM gate_scores"
        ).fetchall()
        return {(r["chunk_id"], r["indicator_id"]): r["cosine_pillar"] for r in rows}

    def chunk_texts(self, document_ids: list[str] | None = None) -> dict[str, str]:
        """chunk_id -> text, SLICED from documents.full_text by the stored
        offsets (the slice-not-reemit rule; chunks never store text)."""
        where, params = "", []
        if document_ids:
            where = f" WHERE c.document_id IN ({', '.join('?' * len(document_ids))})"
            params = list(document_ids)
        rows = self.conn.execute(
            "SELECT c.chunk_id, c.char_start, c.char_end, d.full_text"
            " FROM chunks c JOIN documents d ON d.document_id = c.document_id"
            f"{where}",
            params,
        ).fetchall()
        return {
            r["chunk_id"]: (r["full_text"] or "")[r["char_start"]:r["char_end"]]
            for r in rows
        }

    def chunk_row(self, chunk_id: str) -> sqlite3.Row | None:
        """One chunk's row (its character range and section label). The audit
        view anchors a quote search to this range, the way a bundle reader
        anchors it to the chunk it loaded from JSON."""
        return self.conn.execute(
            "SELECT * FROM chunks WHERE chunk_id = ?", (chunk_id,)
        ).fetchone()

    def document_full_text(self, document_id: str) -> str:
        """THE canonical stream of one document, or "" when the row carries
        none. Every reader that needs text for offsets goes through here rather
        than writing the same SELECT again."""
        row = self.conn.execute(
            "SELECT full_text FROM documents WHERE document_id = ?", (document_id,)
        ).fetchone()
        return (row["full_text"] or "") if row is not None else ""

    def document_pages(self, document_id: str) -> list:
        """The page table (PageSpan list) of one Document's stored text, or []
        when none is known.

        The table lives in the stored extraction of the Document's bytes. Only
        a stream whose text IS the Document's full_text counts: the same bytes
        read by another extractor version can sit beside it, and its offsets
        mean nothing for this Document's Pieces. A database with no extractions
        table has no page table rather than an error (the extraction_get rule)."""
        from regcompass.contracts import PageSpan

        row = self.conn.execute(
            "SELECT source_sha256, full_text FROM documents WHERE document_id = ?",
            (document_id,),
        ).fetchone()
        if row is None or not row["full_text"]:
            return []
        try:
            stored = self.conn.execute(
                "SELECT canonical_json FROM extractions WHERE source_sha256 = ?"
                " ORDER BY created_at DESC, extraction_key",
                (row["source_sha256"],),
            ).fetchall()
        except sqlite3.Error:  # a database with no extractions table
            return []
        for s in stored:
            try:
                payload = json.loads(zlib.decompress(s["canonical_json"]).decode("utf-8"))
            except (zlib.error, ValueError):
                continue
            if payload.get("full_text") == row["full_text"]:
                return [PageSpan.model_validate(p) for p in payload.get("pages") or []]
        return []

    # -- U0 highlight geometry ----------------------------------------------

    def store_words(self, document_id: str, words: Iterable) -> None:
        """Persist a document's WordBox geometry at ingest, replacing whatever
        was there (re-ingesting a document produces a new stream, so the old
        offsets are stale and must not survive). The word's text is not stored:
        it is full_text[char_start:char_end] on both the born-digital and the
        OCR lane, and re-emitting it would be a second copy of the stream."""
        rows = [
            (document_id, w.char_start, w.char_end, i, w.page, w.x0, w.y0, w.x1, w.y1)
            for i, w in enumerate(words)
        ]
        self.conn.execute(
            "DELETE FROM document_words WHERE document_id = ?", (document_id,)
        )
        self.conn.executemany(
            "INSERT OR REPLACE INTO document_words (document_id, char_start, char_end,"
            " idx, page, x0, y0, x1, y1) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        self.conn.commit()

    def words_for(
        self,
        document_id: str,
        char_start: int | None = None,
        char_end: int | None = None,
    ) -> list:
        """A document's WordBox list, in stream order. Given a character span,
        only the words overlapping it come back: highlighting one quote out of a
        666-page volume is then a range scan over a handful of rows instead of
        180,000 objects. The text is sliced back from the canonical stream."""
        from regcompass.contracts import WordBox

        sql = "SELECT * FROM document_words WHERE document_id = ?"
        params: list[object] = [document_id]
        if char_start is not None and char_end is not None:
            sql += " AND char_start < ? AND char_end > ?"
            params += [char_end, char_start]
        rows = self.conn.execute(sql + " ORDER BY char_start", params).fetchall()
        if not rows:
            return []
        full_text = self.document_full_text(document_id)
        return [
            WordBox(
                text=full_text[r["char_start"]:r["char_end"]],
                page=r["page"], x0=r["x0"], y0=r["y0"], x1=r["x1"], y1=r["y1"],
                char_start=r["char_start"], char_end=r["char_end"],
            )
            for r in rows
        ]

    def store_embedding(self, chunk_id: str, vector: np.ndarray) -> None:
        """Persist as float32 C-order bytes with the dimension recorded.
        Raises if the chunk row does not exist (never a silent no-op)."""
        v = np.ascontiguousarray(vector, dtype=np.float32)
        if v.ndim != 1:
            raise ValueError(f"embedding must be 1-D, got shape {v.shape}")
        cur = self.conn.execute(
            "UPDATE chunks SET embedding = ?, embedding_dim = ?, embedding_dtype = 'float32'"
            " WHERE chunk_id = ?",
            (v.tobytes(order="C"), v.shape[0], chunk_id),
        )
        if cur.rowcount == 0:
            raise KeyError(f"chunk_id {chunk_id!r} has no row in chunks; upsert_chunks first")
        self.conn.commit()

    def load_embeddings(self, document_ids: list[str] | None = None) -> tuple[list[str], np.ndarray]:
        """Return (chunk_ids, matrix) for all chunks that have an embedding."""
        sql = (
            "SELECT chunk_id, embedding, embedding_dim, embedding_dtype"
            " FROM chunks WHERE embedding IS NOT NULL"
        )
        params: tuple = ()
        if document_ids:
            placeholders = ",".join("?" * len(document_ids))
            sql += f" AND document_id IN ({placeholders})"
            params = tuple(document_ids)
        rows = self.conn.execute(sql, params).fetchall()
        if not rows:
            return [], np.empty((0, 0), dtype=np.float32)
        dims = {r["embedding_dim"] for r in rows}
        if len(dims) != 1:
            raise ValueError(f"mixed embedding dimensions in store: {sorted(dims)}")
        dtypes = {r["embedding_dtype"] or "float32" for r in rows}
        if len(dtypes) != 1:
            raise ValueError(f"mixed embedding dtypes in store: {sorted(dtypes)}")
        dtype = np.dtype(dtypes.pop())
        matrix = np.vstack([np.frombuffer(r["embedding"], dtype=dtype) for r in rows])
        return [r["chunk_id"] for r in rows], matrix

    # -- M11 shortlist windows ----------------------------------------------

    def store_window_embeddings(
        self, document_id: str, spans: list[tuple[int, int]], matrix: np.ndarray
    ) -> None:
        """Replace a document's window embeddings atomically (delete + insert).
        Raises if the document row does not exist (never a silent no-op)."""
        m = np.ascontiguousarray(matrix, dtype=np.float32)
        if m.ndim != 2 or m.shape[0] != len(spans):
            raise ValueError(f"matrix shape {m.shape} does not match {len(spans)} spans")
        row = self.conn.execute(
            "SELECT 1 FROM documents WHERE document_id = ?", (document_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"document_id {document_id!r} has no row in documents")
        now = utc_now_iso()
        self.conn.execute(
            "DELETE FROM shortlist_windows WHERE document_id = ?", (document_id,)
        )
        self.conn.executemany(
            "INSERT INTO shortlist_windows (document_id, window_index, char_start,"
            " char_end, embedding, embedding_dim, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (document_id, i, start, end, m[i].tobytes(order="C"), m.shape[1], now)
                for i, (start, end) in enumerate(spans)
            ],
        )
        self.conn.commit()

    def clear_window_embeddings(self, document_id: str) -> None:
        """Drop a document's window embeddings (its text changed: they are
        stale and must not satisfy the embed-resume skip)."""
        self.conn.execute(
            "DELETE FROM shortlist_windows WHERE document_id = ?", (document_id,)
        )
        self.conn.commit()

    def load_window_embeddings(self, document_id: str) -> tuple[list[tuple[int, int]], np.ndarray]:
        """A document's window spans and embedding matrix, in window order."""
        rows = self.conn.execute(
            "SELECT char_start, char_end, embedding, embedding_dim FROM shortlist_windows"
            " WHERE document_id = ? ORDER BY window_index",
            (document_id,),
        ).fetchall()
        if not rows:
            return [], np.empty((0, 0), dtype=np.float32)
        matrix = np.vstack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
        return [(r["char_start"], r["char_end"]) for r in rows], matrix

    def documents_for_economy(self, economy: str) -> list[sqlite3.Row]:
        """All document rows for an economy, ordered by document_id (stable)."""
        return self.conn.execute(
            "SELECT * FROM documents WHERE economy = ? ORDER BY document_id", (economy,)
        ).fetchall()

    def corpus_documents(self, economy: str) -> list[sqlite3.Row]:
        """The Corpus of one Economy: the Documents a Run reads. A Document
        counts once it carries content Discovery (or "Add document") stored for
        it, canonical text or bytes on disk. A thin row written only so chunk
        or embedding rows could attach is not a Document and is left out, so an
        Economy with nothing fetched reads as an empty Corpus rather than as a
        Run with nothing to say."""
        return self.conn.execute(
            "SELECT * FROM documents WHERE economy = ?"
            " AND (full_text IS NOT NULL OR local_path IS NOT NULL)"
            " ORDER BY document_id",
            (economy,),
        ).fetchall()

    def corpus_languages(self) -> dict[str, dict[str | None, int]]:
        """Per Economy, how many Corpus Documents carry each language, None for
        a Document with no language recorded. The same Documents as
        corpus_documents, counted for every Economy in one query."""
        out: dict[str, dict[str | None, int]] = {}
        for row in self.conn.execute(
            "SELECT economy, language, COUNT(*) AS n FROM documents"
            " WHERE full_text IS NOT NULL OR local_path IS NOT NULL"
            " GROUP BY economy, language"
        ):
            language = (row["language"] or "").strip() or None
            per = out.setdefault(row["economy"], {})
            per[language] = per.get(language, 0) + row["n"]
        return out

    def corpus_source_urls(self, economy: str) -> set[str]:
        """Every Source URL already in this Economy's Corpus. Discovery asks
        this before it asks the Portal: a Document already fetched is skipped
        without a request unless a refresh was asked for."""
        return {
            row["source_url"]
            for row in self.corpus_documents(economy)
            if row["source_url"]
        }

    def section_chunk_counts(self, document_ids: list[str]) -> dict[str, int]:
        """How many SECTION chunks each of these Documents has, zero included.

        The Gate scores section chunks and nothing else, so this is the answer
        to "could this Document have produced a Mapping at all?", asked of the
        database rather than of a report somebody has to still be holding."""
        out = {doc_id: 0 for doc_id in document_ids}
        if not out:
            return out
        marks = ", ".join("?" for _ in out)
        rows = self.conn.execute(
            "SELECT document_id, COUNT(*) AS n FROM chunks"
            f" WHERE chunk_kind = 'section' AND document_id IN ({marks})"
            " GROUP BY document_id",
            list(out),
        ).fetchall()
        for row in rows:
            out[row["document_id"]] = row["n"]
        return out

    def document_meta(self, document_ids: list[str] | None = None) -> dict[str, dict]:
        """Per-document metadata for the submission.json ocr_quality block and
        source_pdf_path: local_path, extractor + version, ocr_applied, and the
        OCR quality proxies. Tolerant of DBs created before the proxy columns
        existed (a missing column reads back as None), so an old data/ DB never
        crashes the export - it just emits nulls."""
        rows = self.conn.execute("SELECT * FROM documents").fetchall()
        out: dict[str, dict] = {}
        wanted = set(document_ids) if document_ids is not None else None
        for row in rows:
            doc_id = row["document_id"]
            if wanted is not None and doc_id not in wanted:
                continue
            cols = row.keys()

            def _get(name: str, _cols=cols, _row=row):
                return _row[name] if name in _cols else None

            def _flag(name: str):
                v = _get(name)
                return None if v is None else bool(v)

            out[doc_id] = {
                "economy": _get("economy"),
                "source_url": _get("source_url"),
                "title": _get("title"),
                # the Document's own Language: the Evidence Export's Language
                # of Source column (organizer column N, criterion C1c)
                "language": _get("language"),
                # 'discovery' or 'manual'. A database that predates
                # the column reads None, which every caller treats as discovery.
                "source_kind": _get("source_kind"),
                # A secondary reference the Portal published beside this
                # Document (the Lao Official Gazette's English rendering). The
                # export appends it to the row's Notes; None is the common case.
                "notes": _get("notes"),
                # Which of title and source_url a reviewer corrected: those
                # win over the curated corpus file in the export.
                "edited_fields": [
                    f for f in (_get("edited_fields") or "").split(",") if f
                ],
                # The title before a reviewer renamed it; None when nobody did.
                "derived_title": _get("derived_title"),
                "local_path": _get("local_path"),
                # When the bytes were fetched: tells a Document prepared in
                # advance from one Discovery caught during the Run's pass.
                "fetched_at": _get("fetched_at"),
                "extractor": _get("extractor"),
                "extractor_version": _get("extractor_version"),
                "ocr_applied": _flag("ocr_applied"),
                "ocr_quality_cer": _get("ocr_quality_cer"),
                "mean_word_confidence": _get("mean_word_confidence"),
                "dictionary_hit_rate": _get("dictionary_hit_rate"),
                "cer_proxy_flag": _flag("cer_proxy_flag"),
                "escalated_to_rapidocr": _flag("escalated_to_rapidocr"),
                "manual_review": _flag("manual_review"),
            }
        return out

    def audit_durations(self) -> dict[str, float]:
        """Run-level processing time per stage: total duration_ms grouped by
        stage from audit_log. Per-document timing is not available (audit_log
        has no document_id column), so this is the only honest granularity."""
        rows = self.conn.execute(
            "SELECT stage, SUM(duration_ms) AS total FROM audit_log GROUP BY stage"
        ).fetchall()
        return {r["stage"]: r["total"] for r in rows}

    # -- M10 crawl manifest -------------------------------------------------

    def manifest_add_pending(
        self,
        url: str,
        economy: str,
        *,
        kind: str = "document",
        source_family: str | None = None,
        filename_hint: str | None = None,
        notes: str | None = None,
        title: str | None = None,
    ) -> bool:
        """Register a URL for crawling. INSERT OR IGNORE: a row that is already
        fetched or failed is never demoted back to pending. Returns True if the
        row was newly created.

        `notes` is a secondary reference Discovery found beside the Document
        (the Lao Official Gazette's English rendering of an instrument); ingest
        copies it onto the Corpus row, and it never becomes a Source URL.
        `title` is the Portal listing's own name for it, which ingest makes the
        Corpus row's title in place of one read off the file name."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO crawl_manifest"
            " (url, economy, kind, source_family, filename_hint, notes, title,"
            " status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (url, economy, kind, source_family, filename_hint, notes, title, utc_now_iso()),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def manifest_mark_fetched(
        self,
        url: str,
        *,
        http_status: int,
        method: str,
        sha256: str,
        content_type: str | None,
        size_bytes: int,
        local_path: str,
        is_duplicate_of: str | None = None,
    ) -> None:
        self.conn.execute(
            "UPDATE crawl_manifest SET status = 'fetched', http_status = ?, method = ?,"
            " sha256 = ?, content_type = ?, size_bytes = ?, local_path = ?,"
            " is_duplicate_of = ?, error = NULL, attempts = attempts + 1, fetched_at = ?"
            " WHERE url = ?",
            (http_status, method, sha256, content_type, size_bytes, local_path,
             is_duplicate_of, utc_now_iso(), url),
        )
        self.conn.commit()

    def manifest_mark_failed(
        self,
        url: str,
        *,
        error: str,
        http_status: int | None = None,
        method: str | None = None,
    ) -> None:
        """Record a failure. A failed row stays failed: re-runs skip it instead
        of retrying forever (the retry budget lives INSIDE one fetch, tenacity)."""
        self.conn.execute(
            "UPDATE crawl_manifest SET status = 'failed', http_status = ?, method = ?,"
            " error = ?, attempts = attempts + 1 WHERE url = ?",
            (http_status, method, error, url),
        )
        self.conn.commit()

    def manifest_rows(
        self, economy: str | None = None, status: str | None = None
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM crawl_manifest WHERE 1=1"
        params: list[object] = []
        if economy is not None:
            sql += " AND economy = ?"
            params.append(economy)
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        return self.conn.execute(sql + " ORDER BY created_at, url", params).fetchall()

    def manifest_reset_pending(self, urls: Iterable[str]) -> int:
        """Send these manifest rows back to pending so a refresh re-fetches
        them. manifest_add_pending is INSERT OR IGNORE by design (a resumed
        crawl must never demote a fetched row), so an explicit refresh is the
        only way back. Returns how many rows changed."""
        urls = list(urls)
        if not urls:
            return 0
        marks = ", ".join("?" * len(urls))
        cur = self.conn.execute(
            "UPDATE crawl_manifest SET status = 'pending', http_status = NULL,"
            " method = NULL, sha256 = NULL, content_type = NULL, size_bytes = NULL,"
            " local_path = NULL, is_duplicate_of = NULL, error = NULL, fetched_at = NULL"
            f" WHERE url IN ({marks})",
            urls,
        )
        self.conn.commit()
        return cur.rowcount

    def manifest_get(self, url: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM crawl_manifest WHERE url = ?", (url,)
        ).fetchone()

    def manifest_find_original(self, sha256: str) -> sqlite3.Row | None:
        """The first fetched row carrying these exact bytes (dedupe anchor)."""
        return self.conn.execute(
            "SELECT * FROM crawl_manifest WHERE sha256 = ? AND status = 'fetched'"
            " AND is_duplicate_of IS NULL ORDER BY fetched_at LIMIT 1",
            (sha256,),
        ).fetchone()

    def set_document_source_url(self, document_id: str, source_url: str) -> str | None:
        """Record where an already-added Document is published. Returns the
        address it had before, or None when it had none.

        BOTH rows are written, and that is the point of this method existing.
        The Corpus row is what the export and the source link read. The crawl
        manifest is what REBUILDS that row: an ingest skips a digest the
        documents table already holds, so it does not overwrite a live row,
        but once the documents rows are gone (a clear, or a rebuilt working
        database) the next ingest reads the address back off the manifest.
        Writing only the Corpus row would therefore lose the address exactly
        when somebody starts over, which is the moment they can least afford
        to discover it. The manifest is keyed by url, so this is an update of
        a primary key.

        SEVERAL Documents may share one address, exactly as they may on the
        upload lane: a ministry landing page publishes a whole collection, and a
        scan uploaded by hand has to be able to take its law's official address
        even when a fetched copy already sits there. Where the address is
        already another Document's, or the manifest already files other bytes
        under it, this Document's manifest row takes the upload lane's key (the
        address plus its own digest), so each keeps a row of its own and the
        Corpus row still records the address alone.

        Whatever else carried this digest goes: the upload lane files an
        address-less Document under a local marker, and leaving that marker
        beside the real address would leave two fetched manifest rows for one
        set of bytes. A re-ingest reads whichever it meets first, so the
        address would come back blank half the time.

        Raises LookupError for an unknown Document."""
        with self.conn:  # one transaction: the rows never disagree
            return self._write_source_url(document_id, source_url)

    def _write_source_url(self, document_id: str, source_url: str) -> str | None:
        """The writes behind set_document_source_url, left to the caller's
        transaction so a wider edit can commit them together with its own."""
        row = self.conn.execute(
            "SELECT source_url, source_sha256 FROM documents WHERE document_id = ?",
            (document_id,),
        ).fetchone()
        if row is None:
            raise LookupError(document_id)
        previous = row["source_url"]
        if previous == source_url:
            return previous

        shared = self.conn.execute(
            "SELECT 1 FROM documents WHERE source_url = ? AND document_id != ?",
            (source_url, document_id),
        ).fetchone()
        manifest_clash = self.conn.execute(
            "SELECT sha256 FROM crawl_manifest WHERE url = ?", (source_url,)
        ).fetchone()
        key = source_url
        if shared is not None or (
            manifest_clash is not None and manifest_clash["sha256"] != row["source_sha256"]
        ):
            key = manifest_key_for_upload(source_url, row["source_sha256"])
            manifest_clash = self.conn.execute(
                "SELECT sha256 FROM crawl_manifest WHERE url = ?", (key,)
            ).fetchone()

        # Which manifest row becomes the one carrying the address. The fetched
        # original wins where there is one; a digest can have more than one row
        # (Discovery files a second find of the same bytes as a duplicate), so
        # renaming them all at once would collide on the primary key.
        keeper = self.manifest_find_original(row["source_sha256"])
        if keeper is None:
            keeper = self.conn.execute(
                "SELECT url FROM crawl_manifest WHERE sha256 = ? ORDER BY url LIMIT 1",
                (row["source_sha256"],),
            ).fetchone()

        self.conn.execute(
            "UPDATE documents SET source_url = ? WHERE document_id = ?",
            (source_url, document_id),
        )
        if manifest_clash is None and keeper is not None:
            self.conn.execute(
                "UPDATE crawl_manifest SET url = ? WHERE url = ?",
                (key, keeper["url"]),
            )
        # Exactly one manifest row per digest afterwards, and it is the one
        # holding the address. Anything else for these bytes is a stale
        # marker, and a re-ingest reading it would blank the address again.
        self.conn.execute(
            "DELETE FROM crawl_manifest WHERE sha256 = ? AND url != ?",
            (row["source_sha256"], key),
        )
        return previous

    def edit_document(
        self,
        document_id: str,
        *,
        title: str | None = None,
        source_url: str | None = None,
        editor: str | None = None,
        edited_at: str | None = None,
    ) -> dict:
        """Correct a Document's title, its Source URL, or both, and nothing
        else: the id, the text, the chunks and every Mapping that quotes it stay
        exactly as they were. Which fields were corrected, by whom (a name is
        optional, as it is on a Review Decision) and when are recorded on the
        row, and the export reads a corrected field over the curated corpus
        file. Returns the row's values before and after.

        Raises LookupError for an unknown Document."""
        row = self.conn.execute(
            "SELECT * FROM documents WHERE document_id = ?", (document_id,)
        ).fetchone()
        if row is None:
            raise LookupError(document_id)
        before = dict(row)
        fields = [f for f in (before.get("edited_fields") or "").split(",") if f]
        if source_url is not None and "source_url" not in fields:
            fields.append("source_url")
        if title is not None and "title" not in fields:
            fields.append("title")
        stamp = edited_at or utc_now_z()
        with self.conn:  # one transaction: the address and the title together
            if source_url is not None:
                self._write_source_url(document_id, source_url)
            # The first rename keeps the derived title aside, so the KNOWN/NEW
            # match still finds the law by the name it was added under.
            self.conn.execute(
                "UPDATE documents SET derived_title = CASE WHEN ? IS NULL"
                " THEN derived_title ELSE COALESCE(derived_title, title) END,"
                " title = COALESCE(?, title), edited_fields = ?,"
                " edited_by = ?, edited_at = ? WHERE document_id = ?",
                (title, title, ",".join(fields), editor, stamp, document_id),
            )
        after = self.conn.execute(
            "SELECT title, source_url, edited_by, edited_at FROM documents"
            " WHERE document_id = ?",
            (document_id,),
        ).fetchone()
        return {
            "document_id": document_id,
            "title": after["title"],
            "source_url": after["source_url"],
            "previous_title": before.get("title"),
            "previous_source_url": before.get("source_url"),
            "edited_by": after["edited_by"],
            "edited_at": after["edited_at"],
        }

    # -- Removing one Document ---------------------------------------------
    #
    # The way out of one bad add without clearing the whole Economy and its
    # Runs. The Document goes with everything read off its bytes: its chunks,
    # Gate scores, windows, word boxes, stored streams, manifest rows and the
    # stored file. Mappings that quote it go too, with the Review Decisions,
    # Glosses and reconciliation rows filed against them, because a Mapping
    # whose source is gone cannot be checked against anything. The Run Records
    # themselves stay: they are what happened, and their counts describe the
    # Run as it ran. The preview says all of this in numbers before anything
    # is removed, so the confirmation a reviewer agrees to is the operation
    # they get.

    def remove_document_preview(
        self, document_id: str, *, data_dir: str | Path
    ) -> DocumentRemoval:
        """What removing this Document would take. Reads only."""
        return self._remove_document(document_id, data_dir=Path(data_dir), remove=False)

    def remove_document(
        self, document_id: str, *, data_dir: str | Path
    ) -> DocumentRemoval:
        """Remove one Document from its Corpus. Raises LookupError for an
        unknown id. Rows go in one transaction, the stored file afterwards, and
        nothing outside the data root is ever touched."""
        return self._remove_document(document_id, data_dir=Path(data_dir), remove=True)

    def _remove_document(
        self, document_id: str, *, data_dir: Path, remove: bool
    ) -> DocumentRemoval:
        started = time.perf_counter()
        tables = self.table_names()
        doc = self.conn.execute(
            "SELECT document_id, economy, title, source_sha256, local_path"
            " FROM documents WHERE document_id = ?",
            (document_id,),
        ).fetchone()
        if doc is None:
            raise LookupError(document_id)
        sha = doc["source_sha256"]
        owned = self.conn.execute(
            "SELECT run_id, mapping_id FROM mappings WHERE document_id = ?",
            (document_id,),
        ).fetchall()
        run_ids = sorted({r["run_id"] for r in owned})
        manifest_rows = (
            self.conn.execute(
                "SELECT url, local_path FROM crawl_manifest WHERE sha256 = ?", (sha,)
            ).fetchall()
            if "crawl_manifest" in tables and sha
            else []
        )
        extraction_keys = self._doomed_extraction_keys({sha} if sha else set(), tables)
        n_reviews = (
            self.conn.execute(
                "SELECT COUNT(*) AS n FROM reviews t WHERE EXISTS (SELECT 1 FROM"
                " mappings m WHERE m.run_id = t.run_id AND m.mapping_id = t.mapping_id"
                " AND m.document_id = ?)",
                (document_id,),
            ).fetchone()["n"]
            if "reviews" in tables
            else 0
        )
        removable, _refused = self._stored_file_plan([doc], manifest_rows, data_dir)
        present = [p for p in removable if p.is_file()]
        report = DocumentRemoval(
            document_id=document_id,
            economy=doc["economy"],
            title=doc["title"] or document_id,
            mappings=len(owned),
            runs=len(run_ids),
            reviews=n_reviews,
            stored_files=len(present),
        )
        if not remove:
            return report

        mine = (
            "EXISTS (SELECT 1 FROM mappings m WHERE m.run_id = {t}.run_id"
            " AND m.mapping_id = {t}.mapping_id AND m.document_id = ?)"
        )
        try:
            if "source_groups" in tables:
                # A reconciliation group whose authoritative Mapping goes is a
                # group about nothing, so it goes too, with every row filed
                # under it (other Documents' Mappings keep their own rows).
                doomed_groups = (
                    "SELECT g.group_id FROM source_groups g WHERE EXISTS (SELECT 1"
                    " FROM mappings m WHERE m.run_id = g.run_id AND m.mapping_id ="
                    " g.authoritative_mapping_id AND m.document_id = ?)"
                )
                if "mapping_relationships" in tables:
                    self.conn.execute(
                        f"DELETE FROM mapping_relationships WHERE group_id IN ({doomed_groups})",
                        (document_id,),
                    )
                    self.conn.execute(
                        "DELETE FROM mapping_relationships WHERE"
                        f" {mine.format(t='mapping_relationships')}",
                        (document_id,),
                    )
                self.conn.execute(
                    f"DELETE FROM source_groups WHERE group_id IN ({doomed_groups})",
                    (document_id,),
                )
            for table in ("reviews", "review_history", "glosses"):
                if table in tables:
                    self.conn.execute(
                        f"DELETE FROM {table} WHERE {mine.format(t=table)}",
                        (document_id,),
                    )
            self.conn.execute("DELETE FROM mappings WHERE document_id = ?", (document_id,))
            if "gate_scores" in tables:
                self.conn.execute(
                    "DELETE FROM gate_scores WHERE chunk_id IN (SELECT chunk_id FROM"
                    " chunks WHERE document_id = ?)",
                    (document_id,),
                )
            for table in ("chunks", "shortlist_windows", "document_words"):
                if table in tables:
                    self.conn.execute(
                        f"DELETE FROM {table} WHERE document_id = ?", (document_id,)
                    )
            self.conn.execute("DELETE FROM documents WHERE document_id = ?", (document_id,))
            if manifest_rows:
                self.conn.execute("DELETE FROM crawl_manifest WHERE sha256 = ?", (sha,))
            self._delete_in(
                "DELETE FROM extractions WHERE extraction_key IN ({marks})",
                extraction_keys,
            )
            self.conn.commit()
        except sqlite3.Error:
            self.conn.rollback()
            raise
        for path in present:
            try:
                path.unlink()
            except OSError:  # pragma: no cover - a file the process cannot remove
                pass
            self._prune_empty_dirs(path.parent, data_dir)
        self.write_audit(
            StageLogEntry(
                stage="remove_document",
                input_hash=None,
                output_hash=None,
                method=f"remove:{document_id}",
                decision=json.dumps(report.as_dict(), sort_keys=True),
                duration_ms=(time.perf_counter() - started) * 1000.0,
                timestamp=datetime.now(timezone.utc),
            )
        )
        return report

    # -- Run Records ------------------------------------------------------
    # The saved facts of one Run or one Discovery. Two moments: a
    # row written BEFORE the first model call (status running), completed at
    # the end (completed, or failed with the error). A crash therefore leaves
    # a visible unfinished record rather than silence.

    def run_start(
        self,
        *,
        run_id: str,
        kind: str,
        economy: str,
        pillars: list[int],
        indicators: list[str] | None,
        engine: str | None,
        started_at: str,
    ) -> None:
        """Open a Run Record. indicators is None when the Run was not narrowed;
        Indicator IDs are stored as TEXT (4.01 and 12.4.1 are not numbers)."""
        self.conn.execute(
            "INSERT INTO runs (run_id, kind, economy, pillars, indicators, engine,"
            " status, started_at) VALUES (?, ?, ?, ?, ?, ?, 'running', ?)",
            (
                run_id,
                kind,
                economy,
                json.dumps([int(p) for p in pillars]),
                None if indicators is None else json.dumps([str(i) for i in indicators]),
                engine,
                started_at,
            ),
        )
        self.conn.commit()

    def run_finish(
        self,
        run_id: str,
        *,
        status: str,
        ended_at: str,
        documents_fetched: int = 0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost_usd: float = 0.0,
        provider_cost_usd: float | None = None,
        error: str | None = None,
        details: dict | None = None,
    ) -> None:
        """Close a Run Record with its measured totals."""
        self.conn.execute(
            "UPDATE runs SET status = ?, ended_at = ?, documents_fetched = ?,"
            " prompt_tokens = ?, completion_tokens = ?, cost_usd = ?,"
            " provider_cost_usd = ?, error = ?, details = ? WHERE run_id = ?",
            (
                status,
                ended_at,
                int(documents_fetched),
                int(prompt_tokens),
                int(completion_tokens),
                float(cost_usd),
                None if provider_cost_usd is None else float(provider_cost_usd),
                error,
                json.dumps(details or {}, sort_keys=True),
                run_id,
            ),
        )
        self.conn.commit()

    # The message an interrupted Run carries in its error column. It is the
    # whole explanation: nothing failed, nobody was told, the process that owned
    # the row is gone.
    INTERRUPTED_ERROR = (
        "interrupted: the process running this Run ended before it could finish"
        " (a restart, or the server was stopped). Nothing was written after this"
        " point; start the Run again."
    )

    def mark_interrupted_runs(self, *, ended_at: str | None = None) -> int:
        """Close every Run Record still reading `running`, and say how many.

        Only ever called at server start, and only sound there: the single
        worker thread lives in the process that has just begun, so a row that
        says running was opened by a process that is gone. Left alone it sits in
        the Runs list forever, reading as a Run still going."""
        stamp = ended_at or utc_now_z()
        cur = self.conn.execute(
            "UPDATE runs SET status = 'interrupted', ended_at = ?, error = ?"
            " WHERE status = 'running'",
            (stamp, self.INTERRUPTED_ERROR),
        )
        self.conn.commit()
        return cur.rowcount

    def run_get(self, run_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return None if row is None else decode_run_row(row)

    def runs_list(
        self,
        *,
        economy: str | None = None,
        kind: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Past Run Records, newest first. rowid breaks a tie so two records
        opened inside the same clock tick still come back in a stable order."""
        sql = "SELECT * FROM runs WHERE 1=1"
        params: list[object] = []
        if economy is not None:
            sql += " AND economy = ?"
            params.append(economy)
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY started_at DESC, rowid DESC LIMIT ?"
        params.append(max(1, int(limit)))
        return [decode_run_row(r) for r in self.conn.execute(sql, params)]

    def latest_run_id(
        self,
        economy: str | None = None,
        *,
        kind: str = "run",
        status: str = "completed",
    ) -> str | None:
        """The newest finished Run, or None when this database holds none. It
        is what a caller who named no Run means by "the results": the last Run
        that actually completed, never a half-written one and never a mixture of
        several. The resolved id is written into the audit trail by the caller,
        so a reader can always see which Run an unqualified export shipped."""
        rows = self.conn.execute(
            "SELECT run_id FROM runs WHERE kind = ? AND status = ?"
            + ("" if economy is None else " AND economy = ?")
            + " ORDER BY started_at DESC, rowid DESC LIMIT 1",
            [kind, status] + ([] if economy is None else [economy]),
        ).fetchone()
        return None if rows is None else rows["run_id"]

    # -- Review Decisions ----------------------------------------------------
    #
    # A Review Decision belongs to one Mapping of one Run and lives HERE, in
    # the working database, beside the Mapping it judges. That is what makes it
    # survive a restart, and what keeps Run A's decision off Run B's
    # identically-named Mapping. There is one decision per Mapping: a reviewer
    # who changes their mind replaces it, and the stored time is the time of the
    # decision that stands.

    _REVIEW_COLUMNS = (
        "review_id", "run_id", "mapping_id", "review_status", "reviewer",
        "reviewed_at", "comment", "corrected_indicator_id",
    )

    @staticmethod
    def _review_from_row(row) -> Review:
        return Review(
            review_id=row["review_id"],
            run_id=row["run_id"],
            mapping_id=row["mapping_id"],
            review_status=row["review_status"],
            reviewer=row["reviewer"],
            reviewed_at=datetime.fromisoformat(row["reviewed_at"]),
            comment=row["comment"],
            corrected_indicator_id=(
                row["corrected_indicator_id"]
                if "corrected_indicator_id" in row.keys() else None
            ),
        )

    def review_set(
        self,
        *,
        run_id: str,
        mapping_id: str,
        review_status: str,
        reviewer: str | None = None,
        comment: str | None = None,
        reviewed_at: str | None = None,
        review_id: str | None = None,
        corrected_indicator_id: str | None = None,
    ) -> Review:
        """Record the decision that stands for this Mapping of this Run,
        replacing any earlier one, and add it to the Mapping's history in the
        same transaction, so the two can never disagree. The foreign key
        refuses a Mapping the Run never produced, so a typo cannot create a
        decision about nothing.

        Whether a correction's Indicator is a valid one is the caller's rule
        (it depends on the Run's Pillars); this stores what it is given."""
        stamped = reviewed_at or utc_now_iso()
        try:
            self.conn.execute(
                "INSERT INTO reviews (review_id, run_id, mapping_id, review_status,"
                " reviewer, reviewed_at, comment, corrected_indicator_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(run_id, mapping_id) DO UPDATE SET"
                " review_id = excluded.review_id,"
                " review_status = excluded.review_status,"
                " reviewer = excluded.reviewer,"
                " reviewed_at = excluded.reviewed_at,"
                " comment = excluded.comment,"
                " corrected_indicator_id = excluded.corrected_indicator_id",
                (
                    review_id or f"rev_{uuid.uuid4().hex[:12]}",
                    run_id, mapping_id, review_status, reviewer, stamped, comment,
                    corrected_indicator_id,
                ),
            )
            self.conn.execute(
                "INSERT INTO review_history (run_id, mapping_id, review_status,"
                " corrected_indicator_id, reviewer, decided_at, comment)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id, mapping_id, review_status, corrected_indicator_id,
                    reviewer, stamped, comment,
                ),
            )
            self.conn.commit()
        except sqlite3.Error:
            self.conn.rollback()
            raise
        stored = self.review_get(run_id, mapping_id)
        assert stored is not None  # just written, inside this connection
        return stored

    def review_get(self, run_id: str, mapping_id: str) -> Review | None:
        row = self.conn.execute(
            "SELECT * FROM reviews WHERE run_id = ? AND mapping_id = ?",
            (run_id, mapping_id),
        ).fetchone()
        return None if row is None else self._review_from_row(row)

    def review_history(self, run_id: str, mapping_id: str) -> list[dict]:
        """Every decision ever written for this Mapping of this Run, oldest
        first. A database from before the history existed has none to give,
        which reads as an empty list rather than an error: reads never apply
        the schema."""
        try:
            rows = self.conn.execute(
                "SELECT history_id, run_id, mapping_id, review_status,"
                " corrected_indicator_id, reviewer, decided_at, comment"
                " FROM review_history WHERE run_id = ? AND mapping_id = ?"
                " ORDER BY history_id",
                (run_id, mapping_id),
            ).fetchall()
        except sqlite3.Error:
            return []
        return [dict(r) for r in rows]

    def reviews_for_run(self, run_id: str) -> dict[str, Review]:
        """mapping_id -> the Review Decision that stands, for one Run."""
        rows = self.conn.execute(
            "SELECT * FROM reviews WHERE run_id = ? ORDER BY mapping_id", (run_id,)
        ).fetchall()
        return {r["mapping_id"]: self._review_from_row(r) for r in rows}

    def review_counts(self, run_id: str) -> dict[str, int]:
        """What the export gate will do to this Run, counted off the database:
        the verified Mappings, how many carry each decision, and how many carry
        none. Only verified (passed) Mappings are counted, because those are the
        only ones the export would have considered anyway."""
        rows = self.conn.execute(
            "SELECT r.review_status AS status FROM mappings m"
            " LEFT JOIN reviews r ON r.run_id = m.run_id AND r.mapping_id = m.mapping_id"
            " WHERE m.run_id = ? AND m.verification_status = 'passed'",
            (run_id,),
        ).fetchall()
        statuses = [r["status"] for r in rows]
        return {
            "n_verified": len(statuses),
            "n_accepted": statuses.count("accepted"),
            "n_rejected": statuses.count("rejected"),
            "n_flagged": statuses.count("flagged"),
            "n_corrected": statuses.count("corrected"),
            "n_unreviewed": statuses.count(None),
        }

    def unreviewed_mapping_ids(self, run_id: str) -> list[str]:
        """The verified Mappings of this Run that carry no decision yet, in a
        stable order. The bulk accept in the interface works off this list."""
        rows = self.conn.execute(
            "SELECT m.mapping_id AS mapping_id FROM mappings m"
            " LEFT JOIN reviews r ON r.run_id = m.run_id AND r.mapping_id = m.mapping_id"
            " WHERE m.run_id = ? AND m.verification_status = 'passed'"
            " AND r.review_status IS NULL ORDER BY m.mapping_id",
            (run_id,),
        ).fetchall()
        return [r["mapping_id"] for r in rows]

    # -- Glosses -------------------------------------------------------------
    #
    # A Gloss belongs to one Mapping of one Run and lives HERE, beside the quote
    # it renders, for the same reason a Review Decision does: it survives a
    # restart, and Run A's Gloss can never be read onto Run B's identically
    # named Mapping. The export consults this table FIRST and the reviewed
    # config/verbatim_english.json only where the table is silent.

    @staticmethod
    def _gloss_from_row(row) -> GlossRecord:
        return GlossRecord(
            run_id=row["run_id"],
            mapping_id=row["mapping_id"],
            english=row["english"],
            label=row["label"],
            reviewed=bool(row["reviewed"]),
            reviewed_by=row["reviewed_by"],
            reviewed_at=(
                None if row["reviewed_at"] is None else datetime.fromisoformat(row["reviewed_at"])
            ),
            source_language=row["source_language"] or "und",
            engine=row["engine"],
            uncertainty_flag=row["uncertainty_flag"],
            drafted_at=datetime.fromisoformat(row["drafted_at"]),
        )

    def gloss_set(
        self,
        *,
        run_id: str,
        mapping_id: str,
        english: str | None,
        engine: str,
        source_language: str = "und",
        uncertainty_flag: str | None = None,
        label: str = GLOSS_LABEL,
        drafted_at: str | None = None,
    ) -> GlossRecord:
        """Store the DRAFT Gloss for this Mapping of this Run, replacing any
        earlier draft. A draft is never reviewed: re-drafting a Mapping whose
        Gloss a person had approved would launder their name onto text they
        never read, so the review columns are reset with the text."""
        self.conn.execute(
            "INSERT INTO glosses (run_id, mapping_id, english, label, reviewed,"
            " reviewed_by, reviewed_at, source_language, engine, uncertainty_flag,"
            " drafted_at) VALUES (?, ?, ?, ?, 0, NULL, NULL, ?, ?, ?, ?)"
            " ON CONFLICT(run_id, mapping_id) DO UPDATE SET"
            " english = excluded.english,"
            " label = excluded.label,"
            " reviewed = 0,"
            " reviewed_by = NULL,"
            " reviewed_at = NULL,"
            " source_language = excluded.source_language,"
            " engine = excluded.engine,"
            " uncertainty_flag = excluded.uncertainty_flag,"
            " drafted_at = excluded.drafted_at",
            (
                run_id, mapping_id, english, label, source_language, engine,
                uncertainty_flag, drafted_at or utc_now_iso(),
            ),
        )
        self.conn.commit()
        stored = self.gloss_get(run_id, mapping_id)
        assert stored is not None  # just written, inside this connection
        return stored

    def gloss_review(
        self,
        *,
        run_id: str,
        mapping_id: str,
        english: str,
        reviewed_by: str | None,
        reviewed_at: str | None = None,
    ) -> GlossRecord:
        """Approve this Gloss as a named person, with the text they approved.

        reviewed_by is REQUIRED and must not be blank. A Reviewed Gloss is the
        one thing that lets an AI translation ship without its label, so an
        unnamed approval would put an unlabelled machine rendering into the
        Evidence Export on nobody's authority."""
        named = (reviewed_by or "").strip()
        if not named:
            raise ValueError(
                "a Reviewed Gloss must name the person who approved it:"
                " reviewed_by is required and cannot be blank"
            )
        text = (english or "").strip()
        if not text:
            raise ValueError("a Reviewed Gloss needs the English text that was approved")
        existing = self.gloss_get(run_id, mapping_id)
        self.conn.execute(
            "INSERT INTO glosses (run_id, mapping_id, english, label, reviewed,"
            " reviewed_by, reviewed_at, source_language, engine, uncertainty_flag,"
            " drafted_at) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, NULL, ?)"
            " ON CONFLICT(run_id, mapping_id) DO UPDATE SET"
            " english = excluded.english,"
            " reviewed = 1,"
            " reviewed_by = excluded.reviewed_by,"
            " reviewed_at = excluded.reviewed_at,"
            " uncertainty_flag = NULL",
            (
                run_id, mapping_id, text,
                existing.label if existing is not None else GLOSS_LABEL,
                named, reviewed_at or utc_now_iso(),
                existing.source_language if existing is not None else "und",
                # A Gloss a person wrote from scratch names them as its author:
                # the column says where the text came from, never "fake".
                existing.engine if existing is not None else f"human:{named}",
                existing.drafted_at.isoformat() if existing is not None else utc_now_iso(),
            ),
        )
        self.conn.commit()
        stored = self.gloss_get(run_id, mapping_id)
        assert stored is not None  # just written, inside this connection
        return stored

    def gloss_get(self, run_id: str, mapping_id: str) -> GlossRecord | None:
        """This Run's Gloss for one Mapping, or None. A database made before the
        table existed has no Glosses rather than an error: the reader lanes
        (record detail, the export) open whatever database the operator points
        at, and a reused working database from an earlier build must not take
        the interface down."""
        try:
            row = self.conn.execute(
                "SELECT * FROM glosses WHERE run_id = ? AND mapping_id = ?",
                (run_id, mapping_id),
            ).fetchone()
        except sqlite3.Error:  # a database with no glosses table
            return None
        return None if row is None else self._gloss_from_row(row)

    def glosses_for_run(self, run_id: str | None) -> dict[str, GlossRecord]:
        """mapping_id -> the Gloss that stands, for ONE Run. A caller naming no
        Run gets nothing: a Gloss without its Run is exactly the mix-up the
        composite key exists to prevent. A database with no glosses table is
        likewise empty, not an error (see gloss_get)."""
        if run_id is None:
            return {}
        try:
            rows = self.conn.execute(
                "SELECT * FROM glosses WHERE run_id = ? ORDER BY mapping_id", (run_id,)
            ).fetchall()
        except sqlite3.Error:  # a database with no glosses table
            return {}
        return {r["mapping_id"]: self._gloss_from_row(r) for r in rows}

    def run_id_of_mapping(self, mapping_id: str) -> str | None:
        """Which Run produced this Mapping. Used only to place a decision when
        the caller named no Run and the database holds no Run Records (a
        pre-Run-scope database, whose rows are all 'legacy')."""
        row = self.conn.execute(
            "SELECT run_id FROM mappings WHERE mapping_id = ? ORDER BY created_at DESC"
            " LIMIT 1",
            (mapping_id,),
        ).fetchone()
        return None if row is None else row["run_id"]

    # -- Starting empty ------------------------------------------------------
    #
    # Before a sealed test a steward asks to see the tool start empty. That is
    # this: the Corpus rows and the bytes they were stored as, the canonical
    # streams kept so a second pass runs no OCR, and the Run Records with the
    # Mappings and Review Decisions filed under them. Scoped to one Economy, or
    # to everything.
    #
    # Two guarantees hold it together. Nothing outside the data root is ever
    # removed: a stored path is resolved first, and a path that lands anywhere
    # else (a symlink out of the tree included) is left alone and counted as
    # refused. And a preview does the same reckoning without deleting, so the
    # confirmation a reviewer agrees to is the operation they get.
    #
    # The audit trail is the exception to the scoping, and deliberately so. An
    # audit_log row names its stage, not its Run or its Economy, so a clear of
    # one Economy has no way to tell that Economy's rows from another's: the
    # only honest attribution would be the clock, and two Runs whose times
    # overlap would have one deleting the other's trail. A scoped clear
    # therefore leaves audit_log entirely alone, and only a clear of everything
    # empties it. Either way the clear writes its own row saying what it took.

    # The order a delete has to take. A table that points at another is emptied
    # first, or the foreign keys refuse the row still being referenced.
    _CLEAR_TABLE_ORDER = (
        "mapping_relationships",
        "source_groups",
        "reviews",
        "review_history",
        "glosses",
        "mappings",
        "gate_scores",
        "chunks",
        "shortlist_windows",
        "document_words",
        "documents",
        "crawl_manifest",
        "extractions",
        "runs",
        # Only a clear of everything reaches this one; see the note above.
        "audit_log",
    )

    def clear_preview(self, economy: str | None, *, data_dir: str | Path) -> ClearReport:
        """What a clear of this scope would remove. Reads only."""
        return self._clear(economy, data_dir=Path(data_dir), remove=False)

    def clear(self, economy: str | None, *, data_dir: str | Path) -> ClearReport:
        """Remove one Economy's downloaded Documents and every cache derived
        from them, or all of them when `economy` is None. The rows go in one
        transaction; the stored bytes go afterwards, so a failed delete leaves
        files whose rows are gone rather than rows pointing at nothing.

        `data_dir` is the root the bytes were stored under and the fence the
        operation will not reach past."""
        return self._clear(economy, data_dir=Path(data_dir), remove=True)

    def _clear(
        self, economy: str | None, *, data_dir: Path, remove: bool
    ) -> ClearReport:
        started = time.perf_counter()
        tables = self.table_names()
        where = "" if economy is None else " WHERE economy = ?"
        params: tuple = () if economy is None else (economy,)

        doc_rows = self.conn.execute(
            f"SELECT document_id, source_sha256, local_path FROM documents{where}",
            params,
        ).fetchall()
        document_ids = [r["document_id"] for r in doc_rows]
        doomed_shas = {r["source_sha256"] for r in doc_rows if r["source_sha256"]}

        manifest_rows = (
            self.conn.execute(
                f"SELECT local_path FROM crawl_manifest{where}", params
            ).fetchall()
            if "crawl_manifest" in tables
            else []
        )
        run_ids = (
            [
                r["run_id"]
                for r in self.conn.execute(f"SELECT run_id FROM runs{where}", params)
            ]
            if "runs" in tables
            else []
        )

        n_mappings = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM mappings{where}", params
        ).fetchone()["n"]
        n_reviews = self._count_mapping_owned("reviews", economy) if "reviews" in tables else 0
        extraction_keys = self._doomed_extraction_keys(doomed_shas, tables)

        removable, refused = self._stored_file_plan(doc_rows, manifest_rows, data_dir)
        present = [p for p in removable if p.is_file()]
        report = ClearReport(
            economy=economy,
            documents=len(document_ids),
            stored_files=len(present),
            bytes=sum(p.stat().st_size for p in present),
            extractions=len(extraction_keys),
            runs=len(run_ids),
            mappings=n_mappings,
            reviews=n_reviews,
            refused_files=refused,
        )
        if not remove:
            return report

        self._delete_rows(
            economy,
            tables=tables,
            document_ids=document_ids,
            extraction_keys=extraction_keys,
            run_ids=run_ids,
        )
        for path in present:
            try:
                path.unlink()
            except OSError:  # pragma: no cover - a file the process cannot remove
                pass
            self._prune_empty_dirs(path.parent, data_dir)
        self.write_audit(
            StageLogEntry(
                stage="clear",
                input_hash=None,
                output_hash=None,
                method=f"clear:{economy or 'all'}",
                decision=json.dumps(report.as_dict(), sort_keys=True),
                duration_ms=(time.perf_counter() - started) * 1000.0,
                timestamp=datetime.now(timezone.utc),
            )
        )
        return report

    def _count_mapping_owned(self, table: str, economy: str | None) -> int:
        if economy is None:
            return self.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        return self.conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} t WHERE EXISTS (SELECT 1 FROM mappings m"
            " WHERE m.run_id = t.run_id AND m.mapping_id = t.mapping_id"
            " AND m.economy = ?)",
            (economy,),
        ).fetchone()["n"]

    def _doomed_extraction_keys(self, doomed_shas: set[str], tables: set[str]) -> list[str]:
        """The stored streams of the Documents being removed.

        Every stream keyed on those bytes goes, and no check for a second owner
        is needed: documents.source_sha256 is UNIQUE, so one Document's bytes
        are that Document's alone and no surviving Document can carry the sha
        of a removed one. The same bytes CAN have several stored streams (a
        different extraction version or OCR policy is a different key over the
        same bytes); all of them are readings of a Document that is no longer
        in the Corpus, so all of them go."""
        if "extractions" not in tables or not doomed_shas:
            return []
        return [
            r["extraction_key"]
            for r in self.conn.execute(
                "SELECT extraction_key, source_sha256 FROM extractions"
            )
            if r["source_sha256"] in doomed_shas
        ]

    def _stored_file_plan(
        self, doc_rows: list, manifest_rows: list, data_dir: Path
    ) -> tuple[list[Path], int]:
        """The files this clear may remove, and how many stored paths it
        refused. A relative path is read under the data root (how ingest stores
        it) and an absolute one as it stands (how the pipeline stores it); both
        are then resolved, so a symlink is judged by where it actually points."""
        root = data_dir.resolve()
        seen: dict[Path, bool] = {}
        refused = 0
        raw_paths = [r["local_path"] for r in doc_rows]
        raw_paths += [r["local_path"] for r in manifest_rows]
        for raw in raw_paths:
            if not raw:
                continue
            candidate = Path(raw)
            if not candidate.is_absolute():
                candidate = root / candidate
            try:
                resolved = candidate.resolve()
            except OSError:  # pragma: no cover - an unreadable path
                refused += 1
                continue
            if resolved in seen:
                continue
            inside = resolved != root and root in resolved.parents
            seen[resolved] = inside
            if not inside:
                refused += 1
        return [path for path, inside in seen.items() if inside], refused

    def _delete_rows(
        self,
        economy: str | None,
        *,
        tables: set[str],
        document_ids: list[str],
        extraction_keys: list[str],
        run_ids: list[str],
    ) -> None:
        """Every row of this scope, in one transaction."""
        try:
            if economy is None:
                for table in self._CLEAR_TABLE_ORDER:
                    if table in tables:
                        self.conn.execute(f"DELETE FROM {table}")
            else:
                # mapping_relationships points at source_groups, which points at
                # mappings, so the three go in that order.
                if "mapping_relationships" in tables:
                    self._delete_mapping_owned("mapping_relationships", economy)
                if "source_groups" in tables:
                    self.conn.execute(
                        "DELETE FROM source_groups WHERE economy = ?", (economy,)
                    )
                for table in ("reviews", "review_history", "glosses"):
                    if table in tables:
                        self._delete_mapping_owned(table, economy)
                self.conn.execute("DELETE FROM mappings WHERE economy = ?", (economy,))
                if "gate_scores" in tables and "chunks" in tables:
                    self._delete_in(
                        "DELETE FROM gate_scores WHERE chunk_id IN (SELECT chunk_id FROM"
                        " chunks WHERE document_id IN ({marks}))",
                        document_ids,
                    )
                for table in ("chunks", "shortlist_windows", "document_words"):
                    if table in tables:
                        self._delete_in(
                            f"DELETE FROM {table} WHERE document_id IN ({{marks}})",
                            document_ids,
                        )
                self.conn.execute("DELETE FROM documents WHERE economy = ?", (economy,))
                if "crawl_manifest" in tables:
                    self.conn.execute(
                        "DELETE FROM crawl_manifest WHERE economy = ?", (economy,)
                    )
                self._delete_in(
                    "DELETE FROM extractions WHERE extraction_key IN ({marks})",
                    extraction_keys,
                )
                self._delete_in(
                    "DELETE FROM runs WHERE run_id IN ({marks})", run_ids
                )
                # audit_log is untouched here: its rows name a stage, never an
                # Economy, so there is no honest way to scope them. See the
                # note above the table order.
            self.conn.commit()
        except sqlite3.Error:
            self.conn.rollback()
            raise

    def _delete_mapping_owned(self, table: str, economy: str) -> None:
        """Empty a table whose rows belong to a Mapping, for one Economy. The
        row carries no Economy of its own: the Mapping it judges does."""
        self.conn.execute(
            f"DELETE FROM {table} WHERE EXISTS (SELECT 1 FROM mappings m"
            f" WHERE m.run_id = {table}.run_id AND m.mapping_id = {table}.mapping_id"
            " AND m.economy = ?)",
            (economy,),
        )

    def _delete_in(self, sql: str, values: list[str]) -> None:
        """A delete over a list of ids, in batches SQLite's parameter limit
        accepts. An empty list deletes nothing rather than every row."""
        for start in range(0, len(values), 500):
            batch = values[start:start + 500]
            self.conn.execute(sql.format(marks=", ".join("?" * len(batch))), batch)

    @staticmethod
    def _prune_empty_dirs(directory: Path, data_dir: Path) -> None:
        """Remove the directories a cleared file leaves behind, up to (never
        including) the data root."""
        root = data_dir.resolve()
        current = directory
        while current != root and root in current.parents:
            try:
                current.rmdir()
            except OSError:
                return
            current = current.parent

    def close(self) -> None:
        """Fold the write-ahead log into the main file, then close.

        Best effort and bounded: if another process is mid-write the fold
        stops after a short wait and the last connection to close finishes
        it, which SQLite does on its own."""
        try:
            self.conn.execute(f"PRAGMA busy_timeout = {CLOSE_CHECKPOINT_TIMEOUT_MS}")
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        except sqlite3.Error:
            pass
        self.conn.close()


class VectorIndex:
    """Exact cosine search over an in-memory float32 matrix."""

    def __init__(self, ids: list[str], matrix: np.ndarray):
        if len(ids) != matrix.shape[0]:
            raise ValueError("ids/matrix row count mismatch")
        self.ids = ids
        norms = np.linalg.norm(matrix, axis=1, keepdims=True) if matrix.size else np.empty((0, 1))
        self._unit = matrix / np.clip(norms, 1e-12, None) if matrix.size else matrix

    def search(self, query: np.ndarray, top_k: int = 10) -> list[tuple[str, float]]:
        if not self.ids:
            return []
        q = np.asarray(query, dtype=np.float32).ravel()
        q = q / max(float(np.linalg.norm(q)), 1e-12)
        scores = self._unit @ q
        order = np.argsort(-scores)[:top_k]
        return [(self.ids[i], float(scores[i])) for i in order]
