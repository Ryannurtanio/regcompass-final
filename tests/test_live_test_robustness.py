"""Four defects the 22 Sep judge's-hour rehearsal found on the path an
assessor walks: the first upload on a fresh install, a Run whose chunker
matched nothing, a Run left behind by a dead process, and an OCR escalation
that never reached the Document row.

Every one of them is a first-five-minutes failure rather than a deep one, so
each test starts from the state the assessor starts from: an empty directory,
a Document with no structure, a database with a stale row in it, a scan.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from regcompass.server import create_app
from regcompass.storage import Storage

ROOT = Path(__file__).resolve().parents[1]
BORN_DIGITAL = ROOT / "tests/fixtures/sample_legislation/born_digital"
MY_PDF = BORN_DIGITAL / "PERSONAL DATA PROTECTION ACT 2010.pdf"


# ---------------------------------------------------------------------------
# Box 1: the first upload on a fresh install
# ---------------------------------------------------------------------------


class TestTheFirstUploadOnAFreshInstall:
    """No Run has ever been started here, so nothing has applied the schema.

    The other entry points apply it on the way in; the manual add path opened
    the database and wrote a Run Record straight into a table that did not
    exist yet, and the assessor saw a bare Internal Server Error as the very
    first thing the app ever said to them.
    """

    @pytest.fixture()
    def fresh_client(self, tmp_path):
        """A server over a working database FILE THAT DOES NOT EXIST."""
        db = tmp_path / "fresh.db"
        out = tmp_path / "out"
        out.mkdir()
        app = create_app(
            db_path=db, out_dir=out, data_dir=tmp_path / "data", ui_dir=None
        )
        return TestClient(app), db

    @pytest.fixture()
    def clone_client(self, tmp_path):
        """A server over a FRESH CLONE: neither the database nor the directory
        it lives in exists, because `data/` is gitignored and a clone has no
        copy of it."""
        db = tmp_path / "data" / "regcompass.db"
        app = create_app(
            db_path=db,
            out_dir=tmp_path / "out",
            data_dir=tmp_path / "data",
            ui_dir=None,
        )
        return TestClient(app), db

    def test_uploading_first_thing_makes_the_directory_the_database_lives_in(
        self, clone_client
    ):
        """The clean-clone dry run of 22 September: the interface came up, the
        first upload died with `unable to open database file`, and the reviewer
        saw nothing at all. The write makes its own directory."""
        client, db = clone_client
        assert not db.parent.exists(), "the point of the test: a fresh clone"

        with MY_PDF.open("rb") as fh:
            r = client.post(
                "/api/documents/upload",
                data={"economy": "MY", "language": "English"},
                files={"file": (MY_PDF.name, fh, "application/pdf")},
            )
        assert r.status_code == 200, r.text
        assert db.parent.is_dir(), "the write made the directory it needed"
        assert db.is_file(), "and the database is in it"

    def test_a_read_only_status_check_makes_neither_database_nor_directory(
        self, clone_client
    ):
        """The other half of the guarantee: reading the state of a fresh
        install leaves it fresh. Only a write creates anything."""
        client, db = clone_client
        assert client.get("/api/status").json()["active"] is False
        assert not db.exists()
        assert not db.parent.exists()

    def test_uploading_first_thing_succeeds_and_leaves_a_run_record(
        self, fresh_client
    ):
        client, db = fresh_client
        assert not db.exists(), "the point of the test: nothing has made it yet"

        with MY_PDF.open("rb") as fh:
            r = client.post(
                "/api/documents/upload",
                data={"economy": "MY", "language": "English"},
                files={"file": (MY_PDF.name, fh, "application/pdf")},
            )
        assert r.status_code == 200, r.text
        document_id = r.json()["document_id"]

        storage = Storage(db)
        try:
            runs = storage.runs_list(kind="discovery")
            assert len(runs) == 1, "the add is filed as a Run Record like any other"
            assert runs[0]["status"] == "completed"
            assert runs[0]["details"]["manual"] is True
            row = storage.conn.execute(
                "SELECT document_id FROM documents WHERE document_id = ?",
                (document_id,),
            ).fetchone()
            assert row is not None
        finally:
            storage.close()


# ---------------------------------------------------------------------------
# Box 2: a Run whose chunker matched nothing
# ---------------------------------------------------------------------------


UNSTRUCTURED = (
    "This memorandum records the views of the working group on electronic"
    " commerce. It is a discussion paper and carries no numbered provisions of"
    " any kind. "
) * 40


def _run_over_prose(tmp_path, body: str | None = None):
    from regcompass.corpus import add_document
    from regcompass.engines import fake_completion, fake_embed, resolve_engine
    from regcompass.pipeline import run_economy

    storage = Storage(tmp_path / "prose.db")
    storage.apply_schema()
    data_dir = tmp_path / "data"
    added = add_document(
        storage, data_dir, "SG",
        f"<html><body>{body or f'<p>{UNSTRUCTURED}</p>'}</body></html>".encode(),
        source_url="https://sso.agc.gov.sg/Act/Memorandum",
        language="English",
        filename_hint="working group memorandum.html",
        title="Working Group Memorandum",
    )
    lines: list[str] = []
    report = run_economy(
        storage, "SG", (7,), resolve_engine("fake"), data_dir=data_dir,
        completion_fn=fake_completion, embed_fn=fake_embed,
        progress=lines.append,
    )
    return storage, report, lines, added.document_id


class TestARunOverProseReadsItInPassages:
    """A Document with no headings at all is split into numbered passages, so
    the Gate reads it like any other and the Run can find evidence in it."""

    def test_the_run_log_says_passages_and_raises_no_warning(self, tmp_path):
        storage, report, lines, document_id = _run_over_prose(tmp_path)
        said = " ".join(ln for ln in lines if ln.startswith("M4 chunk")).lower()
        assert "numbered passages" in said
        assert report.warnings == []
        labels = {
            r["section_label"] for r in storage.conn.execute(
                "SELECT section_label FROM chunks WHERE document_id = ?", (document_id,)
            )
        }
        assert labels and all(label.startswith("Passage ") for label in labels)

    def test_a_passage_mapping_exports(self, tmp_path):
        """An amending act's shape: two numbered amendments, too few to be
        structure, yet heading-shaped lines the export's section check can
        see. A passage has no heading to hold its label against, so the check
        notes the row instead of refusing the whole export."""
        from regcompass.pipeline import export_from_db

        prose = "<p>" + (
            "Personal data collected by a service provider shall be stored on servers"
            " located within the territory and shall not be transferred abroad"
            " without the approval of the Ministry. A provider shall notify the"
            " Ministry of any breach of personal data within seventy-two hours. "
        ) * 6 + "</p>"
        body = (
            "<p>1. This Act may be cited as the Electronic Commerce (Amendment) Act.</p>"
            + prose
            + "<p>2. Section 5 of the principal Act is amended by deleting subsection (3).</p>"
            + prose
        )
        storage, report, _, document_id = _run_over_prose(tmp_path, body)
        lines: list[str] = []
        result = export_from_db(
            storage, tmp_path / "out", run_id=report.run_id, progress=lines.append
        )
        assert result.battery_failures == []
        gate = [ln for ln in lines if "pointer-gate" in ln]
        assert gate and "0 failures" in gate[0] and "0 fail-closed" not in gate[0], gate
        sections = {
            r["section_label"] for r in storage.conn.execute(
                "SELECT c.section_label FROM mappings m JOIN chunks c USING (chunk_id)"
                " WHERE m.document_id = ?", (document_id,)
            )
        }
        assert sections and all(label.startswith("Passage ") for label in sections)


class TestARunThatFoundNoStructure:
    """The chunker produced no section chunk (a Document with no text; prose
    is read in passages, above), so the Gate saw 0 of 0 pairs, the Run ended
    `completed` with no Mappings, and the export said the Engine had selected
    no evidence and suggested trying the other one.

    Every one of those statements is true and the conclusion is wrong: no
    Engine was ever asked anything. Switching Engines costs the assessor
    minutes they do not have in a live hour, and ends in the same place.
    """

    @pytest.fixture()
    def run_over_prose(self, tmp_path, monkeypatch):
        import regcompass.pipeline as pipeline_mod
        from regcompass.chunk import ChunkingReport
        from regcompass.contracts import Chunk

        def no_sections(canonical, config=None, completion_fn=None):
            text = canonical.full_text
            chunk = Chunk.from_stream(
                canonical, chunk_id=f"{canonical.document_id}:c0000", char_start=0,
                char_end=len(text), section_label="no structure", chunk_kind="other",
            )
            return [chunk], ChunkingReport(n_chunks=1, coverage_chars=len(text))

        monkeypatch.setattr(pipeline_mod, "split_document", no_sections)
        return _run_over_prose(tmp_path)

    def test_the_run_log_names_the_chunk_step_as_the_cause(self, run_over_prose):
        _, _, lines, _ = run_over_prose
        chunk_lines = [ln for ln in lines if ln.startswith("M4 chunk")]
        said = " ".join(chunk_lines).lower()
        assert "no section" in said or "no structure" in said, chunk_lines
        assert "gate" in said, "the log has to say what the consequence is"

    def test_the_run_record_carries_a_warning_the_operator_can_read(
        self, run_over_prose
    ):
        storage, report, _, document_id = run_over_prose
        assert report.warnings, "the report is what the record is written from"
        record = storage.run_get(report.run_id)
        warnings = record["details"]["warnings"]
        assert warnings, "the Run Record is what survives the restart"
        joined = " ".join(warnings)
        assert document_id in joined, "the warning names the Document it is about"
        assert "chunk" in joined.lower()

    def test_the_export_refusal_names_chunking_and_not_the_engine(
        self, run_over_prose
    ):
        from regcompass.pipeline import export_from_db

        storage, report, _, _ = run_over_prose
        with pytest.raises(RuntimeError) as exc:
            export_from_db(storage, Path("out-never-written"), run_id=report.run_id)
        message = str(exc.value)
        assert "chunk" in message.lower(), message
        assert "Try the other Engine" not in message, message

    def test_the_evidence_screen_can_read_the_warning_off_the_audit_run(
        self, run_over_prose, tmp_path
    ):
        """The screen shows what /api/audit/run hands it, and it resolves the
        Run by itself on a plain page load. If the warning is not on that
        answer the banner has nothing to draw."""
        storage, report, _, _ = run_over_prose
        db = storage.db_path
        storage.close()
        out = tmp_path / "out"
        out.mkdir()
        client = TestClient(
            create_app(db_path=db, out_dir=out, data_dir=tmp_path / "data", ui_dir=None)
        )
        body = client.get("/api/audit/run").json()
        assert body["run_id"] == report.run_id
        warnings = body["record"]["details"]["warnings"]
        assert warnings and "chunk" in " ".join(warnings).lower()

    def test_a_structured_document_still_blames_the_engine_when_it_selects_none(
        self, tmp_path
    ):
        """The Engine wording is the right wording for the genuine case, so it
        has to survive: a Document the chunker read fine, an Engine that chose
        nothing in it."""
        import hashlib

        from regcompass.contracts import Chunk
        from regcompass.pipeline import export_from_db
        from regcompass.storage import utc_now_z

        storage = Storage(tmp_path / "engine.db")
        storage.apply_schema()
        text = "16. A licensee shall keep records of every transfer.\n"
        storage.upsert_document(
            "doc_sg_quiet", "SG", hashlib.sha256(text.encode()).hexdigest(),
            full_text=text,
        )
        storage.upsert_chunks(
            [
                Chunk(
                    chunk_id="doc_sg_quiet:c0000",
                    document_id="doc_sg_quiet",
                    char_start=0,
                    char_end=len(text),
                    text=text,
                    section_label="s. 16",
                    chunk_kind="section",
                )
            ]
        )
        storage.run_start(
            run_id="run-quiet-0001", kind="run", economy="SG", pillars=[7],
            indicators=None, engine="fake", started_at=utc_now_z(),
        )
        storage.run_finish(
            "run-quiet-0001", status="completed", ended_at=utc_now_z(),
            details={"documents": ["doc_sg_quiet"], "warnings": []},
        )
        with pytest.raises(RuntimeError) as exc:
            export_from_db(
                storage, tmp_path / "out-never-written", run_id="run-quiet-0001"
            )
        assert "Try the other Engine" in str(exc.value)


# ---------------------------------------------------------------------------
# Box 3: a Run whose process died
# ---------------------------------------------------------------------------


def open_run(storage: Storage, run_id: str, *, economy: str = "MY") -> None:
    """A Run Record opened and never closed: what a kill leaves behind."""
    from regcompass.storage import utc_now_z

    storage.run_start(
        run_id=run_id, kind="run", economy=economy, pillars=[6],
        indicators=None, engine="fake", started_at=utc_now_z(),
    )


class TestARunLeftBehindByADeadProcess:
    """A server restart or a kill leaves the row at `running` forever, because
    only the process that opened it ever closes it. The Runs list then shows a
    Run that is going nowhere, and the assessor reads it as the app being busy.

    Nothing in the database can be running the moment a server starts: the one
    worker thread lives in the process that just began. So the sweep is safe,
    and it is the only moment at which the truth is knowable.
    """

    def test_the_stale_run_is_marked_interrupted_at_server_start(self, tmp_path):
        db = tmp_path / "server.db"
        storage = Storage(db)
        storage.apply_schema()
        open_run(storage, "run-dead-0001")
        storage.close()

        out = tmp_path / "out"
        out.mkdir()
        app = create_app(
            db_path=db, out_dir=out, data_dir=tmp_path / "data", ui_dir=None
        )
        # The sweep is start-up work, not build work, so the client is entered:
        # building an application must open no database at all.
        with TestClient(app) as client:
            runs = client.get("/api/runs").json()["runs"]
        assert [r["run_id"] for r in runs] == ["run-dead-0001"]
        assert runs[0]["status"] == "interrupted"
        assert runs[0]["ended_at"], "an interrupted Run is over, so it has an end"
        assert "interrupted" in (runs[0]["error"] or "").lower()

    def test_the_server_reports_itself_idle_so_a_new_run_is_not_refused(
        self, tmp_path
    ):
        db = tmp_path / "server.db"
        storage = Storage(db)
        storage.apply_schema()
        open_run(storage, "run-dead-0002")
        storage.close()

        out = tmp_path / "out"
        out.mkdir()
        client = TestClient(
            create_app(
                db_path=db, out_dir=out, data_dir=tmp_path / "data", ui_dir=None
            )
        )
        status = client.get("/api/status").json()
        assert status["active"] is False
        assert status["status"] == "idle"

    def test_a_run_this_process_opened_is_left_alone(self, tmp_path):
        """The sweep runs once, at start. A Run opened afterwards is this
        process's own and must survive every later read of the list."""
        db = tmp_path / "server.db"
        storage = Storage(db)
        storage.apply_schema()
        storage.close()

        out = tmp_path / "out"
        out.mkdir()
        client = TestClient(
            create_app(
                db_path=db, out_dir=out, data_dir=tmp_path / "data", ui_dir=None
            )
        )
        live = Storage(db)
        open_run(live, "run-live-0001")
        live.close()

        runs = client.get("/api/runs").json()["runs"]
        assert runs[0]["status"] == "running"

    def test_a_working_database_that_does_not_exist_yet_is_not_created(
        self, tmp_path
    ):
        """A fresh install has no database, and starting the server must not
        make an empty one: the first upload is what creates it."""
        db = tmp_path / "absent.db"
        out = tmp_path / "out"
        out.mkdir()
        create_app(db_path=db, out_dir=out, data_dir=tmp_path / "data", ui_dir=None)
        assert not db.exists()

    def test_an_older_database_gains_the_status_the_sweep_needs(self, tmp_path):
        """The runs table declared its three statuses in a CHECK constraint,
        and a CHECK cannot be altered in place. A database written before
        `interrupted` existed must still take the sweep rather than refuse the
        write, so the schema rebuilds that one table and keeps its rows."""
        import sqlite3

        db = tmp_path / "old.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE runs ("
            " run_id TEXT PRIMARY KEY, kind TEXT NOT NULL,"
            " economy TEXT NOT NULL, pillars TEXT NOT NULL DEFAULT '[]',"
            " indicators TEXT, engine TEXT,"
            " status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),"
            " started_at TEXT NOT NULL, ended_at TEXT,"
            " documents_fetched INTEGER NOT NULL DEFAULT 0,"
            " prompt_tokens INTEGER NOT NULL DEFAULT 0,"
            " completion_tokens INTEGER NOT NULL DEFAULT 0,"
            " cost_usd REAL NOT NULL DEFAULT 0.0, provider_cost_usd REAL,"
            " error TEXT, details TEXT NOT NULL DEFAULT '{}')"
        )
        conn.execute(
            "INSERT INTO runs (run_id, kind, economy, status, started_at)"
            " VALUES ('run-old-0001', 'run', 'MY', 'running', '2026-09-22T09:00:00Z')"
        )
        conn.commit()
        conn.close()

        storage = Storage(db)
        try:
            storage.apply_schema()
            assert storage.mark_interrupted_runs() == 1
            record = storage.run_get("run-old-0001")
        finally:
            storage.close()
        assert record is not None, "the row survives the rebuild"
        assert record["status"] == "interrupted"
        assert record["economy"] == "MY"


# ---------------------------------------------------------------------------
# Box 4: an OCR escalation that reaches the Document row
# ---------------------------------------------------------------------------


class TestOcrEscalationIsRecordedOnTheDocumentRow:
    """A scanned Chinese statute came in reading `tesseract+rapidocr`, and the
    row beside it said nothing: escalated_to_rapidocr null, mean confidence
    null. The extractor string is the only place the escalation showed, so the
    evidence story rested on a string nobody thinks to read.

    The ingest is the lane that writes the row on the upload path, and it wrote
    the extractor and the page counts but none of the five OCR proxies.
    """

    @pytest.fixture()
    def escalated_upload(self, tmp_path, monkeypatch):
        """One upload whose read escalates, over a faked OCR engine.

        Faked because the real ladder rasterizes every page twice and takes
        minutes; what is under test is whether the verdict the ladder already
        computes survives the trip to the Document row.
        """
        from regcompass.contracts import CanonicalText, OcrQuality, PageSpan

        text = "Article 1 The State protects electronic commerce.\n"

        def fake_ocr_document(raw, document_id, **kwargs):
            import hashlib

            return CanonicalText(
                document_id=document_id,
                source_sha256=hashlib.sha256(raw).hexdigest(),
                extractor="tesseract+rapidocr",
                extractor_version="5.5.1+3.4.2",
                full_text=text,
                pages=[PageSpan(page_number=1, char_start=0, char_end=len(text))],
                ocr_applied=True,
                ocr_quality=OcrQuality(
                    mean_word_confidence=0.71,
                    dictionary_hit_rate=None,
                    escalated_to_rapidocr=True,
                    cer_proxy_flag=True,
                    manual_review=False,
                ),
            )

        monkeypatch.setattr("regcompass.ocr.ocr_document", fake_ocr_document)
        monkeypatch.setattr("regcompass.shortlist.should_ocr", lambda canonical: True)

        db = tmp_path / "server.db"
        storage = Storage(db)
        storage.apply_schema()
        storage.conn.close()
        out = tmp_path / "out"
        out.mkdir()
        app = create_app(
            db_path=db, out_dir=out, data_dir=tmp_path / "data", ui_dir=None
        )
        client = TestClient(app)
        with MY_PDF.open("rb") as fh:
            r = client.post(
                "/api/documents/upload",
                data={"economy": "MY", "language": "English"},
                files={"file": (MY_PDF.name, fh, "application/pdf")},
            )
        assert r.status_code == 200, r.text
        return db, r.json()["document_id"]

    def test_the_row_records_the_escalation_and_the_confidence(
        self, escalated_upload
    ):
        db, document_id = escalated_upload
        storage = Storage(db)
        try:
            row = storage.conn.execute(
                "SELECT extractor, ocr_applied, escalated_to_rapidocr,"
                " mean_word_confidence, cer_proxy_flag, manual_review"
                " FROM documents WHERE document_id = ?",
                (document_id,),
            ).fetchone()
        finally:
            storage.close()
        assert row["extractor"] == "tesseract+rapidocr"
        assert row["ocr_applied"] == 1
        assert row["escalated_to_rapidocr"] == 1
        assert row["mean_word_confidence"] == pytest.approx(0.71)
        assert row["cer_proxy_flag"] == 1
        assert row["manual_review"] == 0

    def test_the_document_metadata_the_export_reads_carries_them_too(
        self, escalated_upload
    ):
        """document_meta is what the submission's ocr_quality block is built
        from, so the row alone is not the whole promise."""
        db, document_id = escalated_upload
        storage = Storage(db)
        try:
            meta = storage.document_meta()[document_id]
        finally:
            storage.close()
        assert meta["escalated_to_rapidocr"] is True
        assert meta["mean_word_confidence"] == pytest.approx(0.71)
