"""The prepared database a release ships, packed and loaded.

The pack script reads a working database and its data folder and writes one
archive; `regcompass load-data` downloads that archive, proves its SHA-256 and
unpacks it into an empty data folder. Everything here runs on a tiny database
built in a temporary folder, and every "download" is a file URL: nothing opens
a socket, and nothing reads the real database.
"""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import sys
import tarfile
import zlib
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from regcompass.cli import app
from regcompass.prepared_data import (
    ARCHIVE_DB_NAME,
    MANIFEST_NAME,
    PreparedDataError,
    archive_member_kind,
    configured_source,
    load_prepared_data,
)
from regcompass.storage import Storage

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import pack_prepared_data as packer  # noqa: E402

T = "2026-09-23T00:00:00Z"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _doc(conn, data_dir: Path, document_id: str, economy: str, *, kind="discovery",
         local_path: str | None = None, write=True) -> str:
    raw = f"%PDF bytes of {document_id}".encode()
    sha = _sha(raw)
    name = f"{sha[:12]}_{document_id}.pdf"
    stored = local_path or f"{economy}/raw/{name}"
    if write:
        target = data_dir / economy / "raw" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    conn.execute(
        "INSERT INTO documents (document_id, economy, source_url, source_sha256,"
        " local_path, full_text, source_kind, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (document_id, economy, f"https://portal.example/{document_id}", sha, stored,
         "Section 1. Personal data shall be protected.", kind, T),
    )
    chunk = f"{document_id}::c1"
    conn.execute(
        "INSERT INTO chunks (chunk_id, document_id, char_start, char_end, section_label,"
        " created_at) VALUES (?,?,?,?,?,?)", (chunk, document_id, 0, 20, "s1", T),
    )
    conn.execute(
        "INSERT INTO document_words VALUES (?,?,?,?,?,?,?,?,?)",
        (document_id, 0, 7, 0, 1, 1.0, 1.0, 2.0, 2.0),
    )
    conn.execute(
        "INSERT INTO shortlist_windows VALUES (?,?,?,?,?,?,?)",
        (document_id, 0, 0, 20, b"\x00" * 8, 2, T),
    )
    conn.execute("INSERT INTO gate_scores VALUES (?,?,?)", (chunk, "6.1", 0.5))
    canonical = zlib.compress(json.dumps({"document_id": document_id}).encode())
    conn.execute(
        "INSERT INTO extractions (extraction_key, source_sha256, extractor_version,"
        " ocr_languages, ocr_policy_id, source_format, extractor, n_chars, n_pages,"
        " n_low_yield_pages, canonical_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"key-{document_id}", sha, "v1", "eng", "p1", "pdf", "pdfplumber", 20, 1, 0,
         canonical, T),
    )
    conn.execute(
        "INSERT INTO crawl_manifest (url, economy, status, local_path, created_at)"
        " VALUES (?,?,?,?,?)",
        (f"https://portal.example/{document_id}", economy, "fetched", stored, T),
    )
    return chunk


def _run(conn, run_id: str, economy: str, *, engine="engine-a", status="completed",
         kind="run", chunk=None, document_id=None, details="{}"):
    conn.execute(
        "INSERT INTO runs (run_id, kind, economy, pillars, engine, status, started_at,"
        " details) VALUES (?,?,?,?,?,?,?,?)",
        (run_id, kind, economy, "[6]" if kind == "run" else "[]",
         engine if kind == "run" else None, status, T, details),
    )
    if chunk is None:
        return
    mapping = f"{chunk}::6.1"
    conn.execute(
        "INSERT INTO mappings (run_id, mapping_id, chunk_id, document_id, economy,"
        " indicator_id, indicator_name, section, verbatim_quote, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (run_id, mapping, chunk, document_id, economy, "6.1", "Cross-border", "s1",
         "Personal data", T),
    )
    group = f"g-{run_id}"
    conn.execute(
        "INSERT INTO source_groups (group_id, run_id, economy, indicator_id,"
        " authoritative_mapping_id, created_at) VALUES (?,?,?,?,?,?)",
        (group, run_id, economy, "6.1", mapping, T),
    )
    conn.execute(
        "INSERT INTO mapping_relationships VALUES (?,?,?,?)",
        (run_id, mapping, group, "sole_source"),
    )
    conn.execute(
        "INSERT INTO glosses (run_id, mapping_id, english, label, engine, drafted_at)"
        " VALUES (?,?,?,?,?,?)", (run_id, mapping, "x", "[label]", engine, T),
    )
    conn.execute(
        "INSERT INTO reviews (review_id, run_id, mapping_id, review_status, reviewer,"
        " reviewed_at) VALUES (?,?,?,?,?,?)",
        (f"r-{run_id}", run_id, mapping, "accepted", "A Reviewer", T),
    )
    conn.execute(
        "INSERT INTO audit_log (stage, method, decision, duration_ms, timestamp)"
        " VALUES (?,?,?,?,?)", ("m9_export", "13-column", f"battery green (run {run_id})", 1.0, T),
    )


@pytest.fixture()
def source(tmp_path):
    """A working database in write-ahead-log mode with its data folder: two
    kept Economies, one Economy outside the six, a seeded fixture Document, and
    one Run of every kind that must not ship."""
    data = tmp_path / "data"
    db = data / "regcompass.db"
    data.mkdir()
    storage = Storage(db)
    storage.apply_schema()
    storage.close()
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode = WAL")
    au = _doc(conn, data, "doc_au_privacy", "AU")
    sg = _doc(conn, data, "doc_sg_pdpa", "SG")
    la = _doc(conn, data, "doc_la_decree", "LA")
    fx = _doc(conn, data, "doc_au_fixture", "AU", kind="fixture")
    _run(conn, "run_au_a", "AU", chunk=au, document_id="doc_au_privacy")
    _run(conn, "run_au_b_cut", "AU", engine="engine-b", status="interrupted", chunk=au,
         document_id="doc_au_privacy")
    _run(conn, "run_au_fake", "AU", engine="fake", chunk=fx, document_id="doc_au_fixture")
    _run(conn, "run_sg_b", "SG", engine="engine-b", chunk=sg, document_id="doc_sg_pdpa")
    _run(conn, "run_sg_failed", "SG", engine="engine-b", status="failed")
    _run(conn, "run_la_a", "LA", chunk=la, document_id="doc_la_decree")
    _run(conn, "disc_au", "AU", kind="discovery")
    _run(conn, "disc_la", "LA", kind="discovery")
    conn.execute(
        "INSERT INTO mappings (run_id, mapping_id, chunk_id, document_id, economy,"
        " indicator_id, indicator_name, section, verbatim_quote, created_at)"
        " VALUES ('legacy', 'm-old', ?, 'doc_sg_pdpa', 'SG', '6.1', 'n', 's1', 'q', ?)",
        (sg, T),
    )
    # A file under a kept Economy's raw folder that no Document points at.
    (data / "SG" / "raw" / "index_page.html").write_bytes(b"<html>index</html>")
    conn.commit()
    conn.close()
    return db, data


def _pack(source, tmp_path, **kwargs):
    db, data = source
    return packer.pack(db, data, tmp_path / "dist", name="bundle", **kwargs)


def _unpacked_db(report, tmp_path) -> sqlite3.Connection:
    target = tmp_path / "peek"
    with tarfile.open(report.archive) as archive:
        archive.extract(ARCHIVE_DB_NAME, target, filter="data")
    return sqlite3.connect(target / ARCHIVE_DB_NAME)


def _names(report) -> set[str]:
    with tarfile.open(report.archive) as archive:
        return set(archive.getnames())


def _ids(conn, sql) -> set[str]:
    return {r[0] for r in conn.execute(sql)}


# -- pack -----------------------------------------------------------------


class TestPack:
    def test_keeps_only_completed_runs_on_declared_engines_in_kept_economies(self, source, tmp_path):
        report = _pack(source, tmp_path, economies=("AU", "SG"))
        conn = _unpacked_db(report, tmp_path)
        assert _ids(conn, "SELECT run_id FROM runs") == {"run_au_a", "run_sg_b", "disc_au"}
        assert _ids(conn, "SELECT DISTINCT run_id FROM mappings") == {"run_au_a", "run_sg_b"}
        assert _ids(conn, "SELECT run_id FROM glosses") == {"run_au_a", "run_sg_b"}
        assert _ids(conn, "SELECT run_id FROM source_groups") == {"run_au_a", "run_sg_b"}
        assert _ids(conn, "SELECT run_id FROM mapping_relationships") == {"run_au_a", "run_sg_b"}

    def test_drops_the_other_economies_and_fixture_documents_with_everything_derived(self, source, tmp_path):
        report = _pack(source, tmp_path, economies=("AU", "SG"))
        conn = _unpacked_db(report, tmp_path)
        kept = {"doc_au_privacy", "doc_sg_pdpa"}
        assert _ids(conn, "SELECT document_id FROM documents") == kept
        for table in ("chunks", "document_words", "shortlist_windows"):
            assert _ids(conn, f"SELECT DISTINCT document_id FROM {table}") == kept, table
        assert _ids(conn, "SELECT chunk_id FROM gate_scores") == {f"{d}::c1" for d in kept}
        assert conn.execute("SELECT COUNT(*) FROM extractions").fetchone()[0] == 2
        assert _ids(conn, "SELECT DISTINCT economy FROM crawl_manifest") == {"AU", "SG"}

    def test_review_decisions_never_ship(self, source, tmp_path):
        report = _pack(source, tmp_path, economies=("AU", "SG"))
        conn = _unpacked_db(report, tmp_path)
        assert conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0
        assert report.manifest["dropped"]["reviews"] == 5

    def test_the_audit_trail_loses_only_rows_naming_what_was_dropped(self, source, tmp_path):
        report = _pack(source, tmp_path, economies=("AU", "SG"))
        conn = _unpacked_db(report, tmp_path)
        decisions = " ".join(_ids(conn, "SELECT decision FROM audit_log"))
        assert "run_au_a" in decisions and "run_sg_b" in decisions
        for gone in ("run_la_a", "run_au_fake", "run_au_b_cut"):
            assert gone not in decisions

    def test_a_named_run_or_document_is_left_out(self, source, tmp_path):
        report = _pack(source, tmp_path, economies=("AU", "SG"),
                       drop_runs=("run_sg_b",), drop_documents=("doc_sg_pdpa",))
        conn = _unpacked_db(report, tmp_path)
        assert "run_sg_b" not in _ids(conn, "SELECT run_id FROM runs")
        assert _ids(conn, "SELECT document_id FROM documents") == {"doc_au_privacy"}
        assert not any(n.startswith("SG/") for n in _names(report))

    def test_the_copy_keeps_its_foreign_keys_and_integrity(self, source, tmp_path):
        conn = _unpacked_db(_pack(source, tmp_path, economies=("AU", "SG")), tmp_path)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"

    def test_ships_only_the_bytes_kept_documents_read(self, source, tmp_path):
        report = _pack(source, tmp_path, economies=("AU", "SG"))
        names = _names(report)
        raws = {n for n in names if "/raw/" in n}
        assert len(raws) == 2
        assert all(n.startswith(("AU/raw/", "SG/raw/")) for n in raws)
        assert "SG/raw/index_page.html" not in names
        assert {ARCHIVE_DB_NAME, MANIFEST_NAME} <= names
        assert all(archive_member_kind(n) for n in names)

    def test_the_sha256_file_names_the_archive_and_matches_it(self, source, tmp_path):
        report = _pack(source, tmp_path, economies=("AU", "SG"))
        digest, name = report.sha256_file.read_text().split()
        assert name == "bundle.tar.gz"
        assert digest == _sha(report.archive.read_bytes()) == report.sha256

    def test_the_manifest_counts_per_economy_and_names_the_runs(self, source, tmp_path):
        report = _pack(source, tmp_path, economies=("AU", "SG"))
        manifest = json.loads(report.manifest_file.read_text())
        assert manifest["format"] == "regcompass-prepared-data/1"
        assert manifest["economies"]["AU"]["documents"] == 1
        assert manifest["economies"]["AU"]["raw_files"] == 1
        assert [r["run_id"] for r in manifest["economies"]["AU"]["runs"]] == ["run_au_a"]
        assert manifest["economies"]["AU"]["discoveries"] == ["disc_au"]
        assert manifest["economies"]["SG"]["mappings"] == 1
        assert len(manifest["database"]["schema_sha256"]) == 64
        assert manifest["source_database"] == "regcompass.db", "a file name, never a path"
        assert "run_la_a" in manifest["dropped"]["runs"]
        with tarfile.open(report.archive) as archive:
            inside = json.load(archive.extractfile(MANIFEST_NAME))
        assert inside == manifest

    def test_reads_rows_still_in_the_write_ahead_log(self, source, tmp_path):
        db, _ = source
        writer = sqlite3.connect(db)
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute("UPDATE runs SET error = 'late write' WHERE run_id = 'run_au_a'")
        writer.commit()
        try:
            conn = _unpacked_db(_pack(source, tmp_path, economies=("AU", "SG")), tmp_path)
            assert conn.execute(
                "SELECT error FROM runs WHERE run_id = 'run_au_a'"
            ).fetchone()[0] == "late write"
        finally:
            writer.close()

    def test_never_changes_the_source(self, source, tmp_path):
        db, data = source
        before = {p: p.read_bytes() for p in data.rglob("*") if p.is_file() and "-wal" not in p.name
                  and "-shm" not in p.name}
        _pack(source, tmp_path, economies=("AU", "SG"))
        conn = sqlite3.connect(db)
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 8
        assert conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 5
        conn.close()
        for path, content in before.items():
            if path != db:
                assert path.read_bytes() == content

    def test_an_absolute_path_in_the_kept_rows_refuses_the_pack(self, source, tmp_path):
        db, _ = source
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE runs SET details = ? WHERE run_id = 'run_au_a'",
            (json.dumps({"log": "/Users/someone/regcompass/data/x.log"}),),
        )
        conn.commit()
        conn.close()
        with pytest.raises(packer.PackRefused, match=r"runs\.details"):
            _pack(source, tmp_path, economies=("AU", "SG"))
        assert not (tmp_path / "dist" / "bundle.tar.gz").exists()

    def test_an_absolute_path_only_in_a_dropped_row_does_not(self, source, tmp_path):
        db, _ = source
        conn = sqlite3.connect(db)
        conn.execute("UPDATE runs SET error = '/home/op/crash' WHERE run_id = 'run_la_a'")
        conn.commit()
        conn.close()
        _pack(source, tmp_path, economies=("AU", "SG"))

    @pytest.mark.parametrize("text", [
        "https://portal.example/home/act.pdf",
        "see data/home/notes",
        "AU/raw/abc_act.pdf",
    ])
    def test_a_web_address_or_relative_path_is_not_an_absolute_path(self, text):
        assert packer.ABSOLUTE_PATH.search(text) is None

    @pytest.mark.parametrize("text", [
        "/Users/someone/x", "error at /home/op/run.log", '"/tmp/regcompass/x"',
        "C:\\Users\\op\\x", "/private/var/folders/x",
    ])
    def test_home_and_temporary_paths_are(self, text):
        assert packer.ABSOLUTE_PATH.search(text) is not None

    def test_an_absolute_local_path_is_rewritten_relative(self, source, tmp_path):
        db, data = source
        conn = sqlite3.connect(db)
        stored = conn.execute(
            "SELECT local_path FROM documents WHERE document_id = 'doc_au_privacy'"
        ).fetchone()[0]
        conn.execute(
            "UPDATE documents SET local_path = ? WHERE document_id = 'doc_au_privacy'",
            (f"data/{stored}",),
        )
        conn.commit()
        conn.close()
        out = _unpacked_db(_pack(source, tmp_path, economies=("AU", "SG")), tmp_path)
        assert out.execute(
            "SELECT local_path FROM documents WHERE document_id = 'doc_au_privacy'"
        ).fetchone()[0] == stored

    def test_a_kept_document_without_its_bytes_refuses_the_pack(self, source, tmp_path):
        _, data = source
        for path in (data / "SG" / "raw").glob("*doc_sg_pdpa*"):
            path.unlink()
        with pytest.raises(packer.PackRefused, match="doc_sg_pdpa"):
            _pack(source, tmp_path, economies=("AU", "SG"))

    def test_a_table_without_a_rule_refuses_the_pack(self, source, tmp_path):
        db, _ = source
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE secrets (x TEXT)")
        conn.commit()
        conn.close()
        with pytest.raises(packer.PackRefused, match="secrets"):
            _pack(source, tmp_path, economies=("AU", "SG"))

    def test_the_default_economies_are_the_six_the_submission_declares(self):
        assert set(packer.DEFAULT_ECONOMIES) == {"AU", "MY", "SG", "CN", "ID", "IN"}
        assert set(packer.DEFAULT_ENGINES) == {"engine-a", "engine-b"}

    def test_the_command_line_packs_and_refuses_with_exit_2(self, source, tmp_path, capsys):
        db, data = source
        out = tmp_path / "cli"
        argv = ["--db", str(db), "--data-dir", str(data), "--out", str(out),
                "--economies", "AU,SG", "--name", "b"]
        assert packer.main(argv) == 0
        assert (out / "b.tar.gz").is_file() and (out / "b.tar.gz.sha256").is_file()
        assert packer.main(argv) == 2, "an existing archive is never overwritten"
        assert "already exists" in capsys.readouterr().err


# -- load -----------------------------------------------------------------


@pytest.fixture()
def bundle(source, tmp_path):
    return _pack(source, tmp_path, economies=("AU", "SG"))


def _url(report) -> str:
    return report.archive.resolve().as_uri()


class TestLoad:
    def test_fills_an_empty_data_folder(self, bundle, tmp_path):
        data = tmp_path / "judge"
        report = load_prepared_data(
            _url(bundle), bundle.sha256, db_path=data / "regcompass.db", data_dir=data
        )
        assert report.documents == 2 and report.runs == 2
        assert report.raw_files_written == 2
        storage = Storage(data / "regcompass.db")
        try:
            for row in storage.conn.execute("SELECT local_path, source_sha256 FROM documents"):
                assert _sha((data / row["local_path"]).read_bytes()) == row["source_sha256"]
        finally:
            storage.close()
        assert (data / MANIFEST_NAME).is_file()
        assert not [p for p in data.iterdir() if p.name.startswith(".prepared-")]

    def test_a_checksum_mismatch_unpacks_nothing(self, bundle, tmp_path):
        data = tmp_path / "judge"
        with pytest.raises(PreparedDataError, match="checksum mismatch"):
            load_prepared_data(
                _url(bundle), "0" * 64, db_path=data / "regcompass.db", data_dir=data
            )
        assert list(data.iterdir()) == [], "no database, no raw files, no leftovers"

    def test_a_malformed_digest_is_refused_before_downloading(self, bundle, tmp_path):
        with pytest.raises(PreparedDataError, match="not a SHA-256"):
            load_prepared_data(_url(bundle), "abc", db_path=tmp_path / "x.db",
                               data_dir=tmp_path / "d")

    def test_a_database_with_runs_is_not_replaced_without_force(self, bundle, tmp_path, source):
        data = tmp_path / "judge"
        data.mkdir()
        db = data / "regcompass.db"
        storage = Storage(db)
        storage.apply_schema()
        storage.conn.execute(
            "INSERT INTO runs (run_id, kind, economy, status, started_at)"
            " VALUES ('mine', 'run', 'AU', 'completed', ?)", (T,),
        )
        storage.conn.commit()
        storage.close()
        with pytest.raises(PreparedDataError, match="--force"):
            load_prepared_data(_url(bundle), bundle.sha256, db_path=db, data_dir=data)
        assert _ids(sqlite3.connect(db), "SELECT run_id FROM runs") == {"mine"}
        report = load_prepared_data(_url(bundle), bundle.sha256, db_path=db, data_dir=data,
                                    force=True)
        assert report.replaced_database
        assert "mine" not in _ids(sqlite3.connect(db), "SELECT run_id FROM runs")

    def test_the_empty_database_a_first_server_start_leaves_is_replaced(self, bundle, tmp_path):
        data = tmp_path / "judge"
        data.mkdir()
        db = data / "regcompass.db"
        storage = Storage(db)
        storage.apply_schema()
        storage.close()
        report = load_prepared_data(_url(bundle), bundle.sha256, db_path=db, data_dir=data)
        assert report.replaced_database and report.runs == 2

    def test_a_stored_file_with_other_bytes_is_not_overwritten_without_force(self, bundle, tmp_path):
        data = tmp_path / "judge"
        with tarfile.open(bundle.archive) as archive:
            raw = next(n for n in archive.getnames() if n.startswith("AU/raw/"))
        (data / "AU" / "raw").mkdir(parents=True)
        (data / raw).write_bytes(b"someone else's file")
        with pytest.raises(PreparedDataError, match="other bytes"):
            load_prepared_data(_url(bundle), bundle.sha256, db_path=data / "regcompass.db",
                               data_dir=data)
        assert (data / raw).read_bytes() == b"someone else's file"
        assert not (data / "regcompass.db").exists()

    def test_the_same_bytes_already_in_place_are_left_alone(self, bundle, tmp_path):
        data = tmp_path / "judge"
        load_prepared_data(_url(bundle), bundle.sha256, db_path=data / "regcompass.db",
                           data_dir=data)
        again = load_prepared_data(_url(bundle), bundle.sha256,
                                   db_path=data / "other.db", data_dir=data)
        assert again.raw_files_written == 0 and again.raw_files_already_present == 2

    def test_a_plain_local_path_works_as_the_address(self, bundle, tmp_path):
        data = tmp_path / "judge"
        report = load_prepared_data(str(bundle.archive), bundle.sha256,
                                    db_path=data / "regcompass.db", data_dir=data)
        assert report.runs == 2

    @pytest.mark.parametrize("evil", ["../escape.txt", "/etc/passwd", "AU/raw/../../x",
                                      "notes.txt", "AU/raw/sub/file.pdf"])
    def test_an_archive_writing_outside_a_data_folder_is_refused(self, tmp_path, evil):
        archive_path = tmp_path / "evil.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            for name, data in ((ARCHIVE_DB_NAME, b"db"), (MANIFEST_NAME, b"{}"), (evil, b"x")):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        data_dir = tmp_path / "judge"
        with pytest.raises(PreparedDataError, match="never holds"):
            load_prepared_data(archive_path.as_uri(), _sha(archive_path.read_bytes()),
                               db_path=data_dir / "regcompass.db", data_dir=data_dir)
        assert list(data_dir.iterdir()) == []

    def test_a_link_member_is_refused(self, tmp_path):
        archive_path = tmp_path / "link.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            info = tarfile.TarInfo("AU/raw/act.pdf")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            archive.addfile(info)
        with pytest.raises(PreparedDataError, match="never holds"):
            load_prepared_data(archive_path.as_uri(), _sha(archive_path.read_bytes()),
                               db_path=tmp_path / "d" / "x.db", data_dir=tmp_path / "d")

    def test_the_address_and_digest_come_from_config_when_blank(self, tmp_path):
        assert configured_source(tmp_path) is None
        (tmp_path / "prepared_data.yaml").write_text(yaml.safe_dump({"url": "", "sha256": ""}))
        assert configured_source(tmp_path) is None
        (tmp_path / "prepared_data.yaml").write_text(
            yaml.safe_dump({"url": "https://x.example/a.tar.gz", "sha256": "AB" * 32})
        )
        source = configured_source(tmp_path)
        assert source.url == "https://x.example/a.tar.gz" and source.sha256 == "ab" * 32

    def test_the_committed_config_is_blank_or_complete(self):
        raw = yaml.safe_load((ROOT / "config" / "prepared_data.yaml").read_text())
        assert set(raw) == {"url", "sha256"}
        assert bool(raw["url"]) == bool(raw["sha256"])


class TestLoadCommand:
    def test_loads_and_reports(self, bundle, tmp_path):
        data = tmp_path / "judge"
        result = CliRunner().invoke(app, [
            "load-data", "--url", _url(bundle), "--sha256", bundle.sha256,
            "--db", str(data / "regcompass.db"), "--data-dir", str(data),
        ])
        assert result.exit_code == 0, result.output
        assert "SHA-256 verified" in result.output
        assert "AU: 1 Document, 1 Run" in result.output

    def test_a_mismatch_exits_1_and_says_so(self, bundle, tmp_path):
        data = tmp_path / "judge"
        result = CliRunner().invoke(app, [
            "load-data", "--url", _url(bundle), "--sha256", "f" * 64,
            "--db", str(data / "regcompass.db"), "--data-dir", str(data),
        ])
        assert result.exit_code == 1
        assert "checksum mismatch" in result.output

    def test_reads_the_paths_the_container_sets(self, bundle, tmp_path):
        data = tmp_path / "volume"
        result = CliRunner().invoke(
            app, ["load-data", "--url", _url(bundle), "--sha256", bundle.sha256],
            env={"REGCOMPASS_DB": str(data / "regcompass.db"), "REGCOMPASS_DATA": str(data)},
        )
        assert result.exit_code == 0, result.output
        assert (data / "regcompass.db").is_file()

    def test_without_an_address_it_says_where_to_find_one(self, tmp_path, monkeypatch):
        import regcompass.prepared_data as prepared

        monkeypatch.setattr(prepared, "CONFIG_DIR", tmp_path)
        result = CliRunner().invoke(app, ["load-data", "--db", str(tmp_path / "x.db")])
        assert result.exit_code == 2
        assert "--url" in result.output and "--sha256" in result.output
