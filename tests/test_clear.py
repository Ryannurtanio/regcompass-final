"""Clearing the downloaded Documents and every cache, on screen and on the
command line.

A steward asks to see the tool start empty before the clock starts. That is one
control, scoped to one Economy or to everything, behind a confirmation that
states what is about to go: the Corpus rows and their stored bytes under the
data root, the stored extraction streams, and the Run Records with their
Mappings and Review Decisions.

The line these tests hold is what the clear is FOR. A second pass over an
untouched Corpus reuses the stored text and fetches nothing; after a clear the
same Economy extracts again, because nothing survived to be served. Nothing
outside the data root is ever removed, and the operation refuses while a job is
running rather than deleting rows a worker thread still holds open.
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import regcompass.cli as cli_mod
from regcompass.server import create_app
from regcompass.storage import Storage

from corpus_fixtures import seed_corpus  # noqa: E402

# Two Economies, so every scoped test can show that the other one survived.
ECONOMIES = ("SG", "MY")


@pytest.fixture(scope="module")
def corpus_template(tmp_path_factory):
    """One Corpus, filled the way Discovery fills one, built once. Ingest reads
    both Acts end to end, so the tests copy this rather than rebuilding it."""
    root = tmp_path_factory.mktemp("clear_template")
    storage = Storage(root / "clear.db")
    storage.apply_schema()
    ids = {economy: seed_corpus(storage, root / "data", economy) for economy in ECONOMIES}
    storage.close()
    return root, ids


@pytest.fixture()
def corpus(corpus_template, tmp_path):
    """A private copy of that Corpus: its own database file and its own data
    root, so a test that clears it cannot reach another test's rows."""
    root, ids = corpus_template
    db = tmp_path / "clear.db"
    data = tmp_path / "data"
    shutil.copy2(root / "clear.db", db)
    shutil.copytree(root / "data", data)
    storage = Storage(db)
    yield storage, db, data, ids
    storage.close()


def _stored_files(data_dir: Path) -> list[Path]:
    return sorted(p for p in Path(data_dir).rglob("*") if p.is_file())


# ---------------------------------------------------------------------------
# Box: the Storage operation
# ---------------------------------------------------------------------------


def _seed_run(
    storage: Storage,
    economy: str,
    run_id: str,
    started_at: str = "2026-09-16T00:00:00.000000Z",
    ended_at: str = "2026-09-16T00:01:00.000000Z",
) -> str:
    """One finished Run over this Economy, with one Mapping and one Review
    Decision filed under it. The times are settable so a test can put two Runs
    in overlapping windows."""
    from regcompass.contracts import MappingRecord

    document_id = storage.corpus_documents(economy)[0]["document_id"]
    # Ingest stores the text; a Run is what writes chunks, and this stands in
    # for one so the Mapping has the chunk row its foreign key needs.
    chunk_id = f"{document_id}:c0001"
    storage.conn.execute(
        "INSERT OR REPLACE INTO chunks (chunk_id, document_id, char_start, char_end,"
        " section_label, chunk_kind, created_at) VALUES (?, ?, 0, 10, 's. 1',"
        " 'section', '2026-09-16T00:00:00+00:00')",
        (chunk_id, document_id),
    )
    # The Gate cosine the confidence composite reads back. It carries no
    # foreign key, so a clear that forgot it would leave a score pointing at a
    # chunk that no longer exists.
    storage.upsert_gate_scores({(chunk_id, "7.1"): 0.5})
    storage.conn.commit()
    storage.run_start(
        run_id=run_id, kind="run", economy=economy, pillars=[7], indicators=None,
        engine="fake", started_at=started_at,
    )
    storage.run_finish(run_id, status="completed", ended_at=ended_at)
    mapping_id = f"map_{economy.lower()}"
    storage.upsert_mappings(
        [
            MappingRecord(
                mapping_id=mapping_id, document_id=document_id, chunk_id=chunk_id,
                economy=economy, indicator_id="7.1", indicator_name="An indicator",
                section="s. 1", verbatim_quote="some text",
                verification_status="passed",
            )
        ],
        run_id=run_id,
    )
    storage.review_set(
        run_id=run_id, mapping_id=mapping_id, review_status="accepted",
        reviewer="a reviewer",
    )
    return mapping_id


class TestTheCountsAndTheScope:
    def test_a_preview_counts_what_would_go_and_deletes_nothing(self, corpus):
        storage, _, data, ids = corpus
        before = _stored_files(data)
        report = storage.clear_preview("SG", data_dir=data)
        assert report.documents == len(ids["SG"])
        assert report.stored_files > 0
        assert report.bytes > 0
        assert _stored_files(data) == before
        assert len(storage.corpus_documents("SG")) == len(ids["SG"])

    def test_clearing_one_economy_leaves_another_alone(self, corpus):
        storage, _, data, ids = corpus
        report = storage.clear("SG", data_dir=data)
        assert report.documents == len(ids["SG"])
        assert storage.corpus_documents("SG") == []
        assert len(storage.corpus_documents("MY")) == len(ids["MY"])
        left = [str(p) for p in _stored_files(data)]
        assert any("/MY/" in p for p in left)
        assert not any("/SG/" in p for p in left)

    def test_the_preview_and_the_clear_agree(self, corpus):
        storage, _, data, _ = corpus
        preview = storage.clear_preview("SG", data_dir=data)
        removed = storage.clear("SG", data_dir=data)
        assert preview == removed

    def test_clearing_everything_empties_every_table(self, corpus):
        storage, _, data, _ = corpus
        _seed_run(storage, "SG", "run_sg")
        storage.clear(None, data_dir=data)
        for table in ("documents", "chunks", "mappings", "crawl_manifest",
                      "extractions", "runs", "shortlist_windows", "document_words",
                      "reviews", "review_history"):
            n = storage.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            assert n == 0, table
        assert _stored_files(data) == []

    def test_the_stored_bytes_are_gone_from_disk(self, corpus):
        storage, _, data, _ = corpus
        report = storage.clear("SG", data_dir=data)
        assert report.stored_files == 1
        assert report.bytes > 0
        assert not (data / "SG").exists(), "the emptied directory goes too"

    def test_an_unknown_economy_clears_nothing(self, corpus):
        storage, _, data, ids = corpus
        report = storage.clear("ZZ", data_dir=data)
        assert report.documents == 0
        assert report.stored_files == 0
        assert len(storage.corpus_documents("SG")) == len(ids["SG"])

    def test_the_clear_is_recorded_in_the_audit_trail(self, corpus):
        storage, _, data, _ = corpus
        storage.clear("SG", data_dir=data)
        rows = storage.conn.execute(
            "SELECT * FROM audit_log WHERE stage = 'clear'"
        ).fetchall()
        assert len(rows) == 1
        assert "SG" in rows[0]["method"]
        assert "documents" in rows[0]["decision"]


def _write_audit_row(storage: Storage, method: str, at: str) -> None:
    """One trail row, stamped at a time a test chooses."""
    from regcompass.contracts import StageLogEntry

    storage.write_audit(
        StageLogEntry(
            stage="m1_extract",
            input_hash=None,
            output_hash=None,
            method=method,
            decision="ok",
            duration_ms=1.0,
            timestamp=datetime.fromisoformat(at),
        )
    )


def _audit_rows(storage: Storage) -> list[tuple[str, str]]:
    return [
        (r["stage"], r["method"])
        for r in storage.conn.execute("SELECT stage, method FROM audit_log ORDER BY id")
    ]


class TestTheAuditTrailIsScopedHonestly:
    """An audit_log row names its stage, never its Economy or its Run, so a
    clear of one Economy cannot tell its rows from another's. Attributing them
    by the clock would be a guess, and a wrong one the moment two Runs overlap.
    A scoped clear therefore leaves the trail alone; only a clear of everything
    empties it."""

    def test_a_scoped_clear_keeps_every_earlier_row(self, corpus):
        storage, _, data, _ = corpus
        # Two Runs whose windows overlap, and a Malaysian trail row written
        # while both were open. Attributing rows by time would have taken it.
        _seed_run(
            storage, "SG", "run_sg",
            started_at="2026-09-16T00:00:00.000000Z",
            ended_at="2026-09-16T01:00:00.000000Z",
        )
        _seed_run(
            storage, "MY", "run_my",
            started_at="2026-09-16T00:30:00.000000Z",
            ended_at="2026-09-16T00:45:00.000000Z",
        )
        _write_audit_row(storage, "MY:pdf", "2026-09-16T00:40:00+00:00")
        _write_audit_row(storage, "SG:pdf", "2026-09-16T00:10:00+00:00")
        before = _audit_rows(storage)
        storage.clear("SG", data_dir=data)
        after = _audit_rows(storage)
        assert after[: len(before)] == before, "no earlier trail row was removed"
        assert ("m1_extract", "MY:pdf") in after
        assert after[-1] == ("clear", "clear:SG"), "the clear records itself"
        assert len(after) == len(before) + 1

    def test_clearing_everything_empties_the_trail_but_says_so(self, corpus):
        storage, _, data, _ = corpus
        _write_audit_row(storage, "SG:pdf", "2026-09-16T00:10:00+00:00")
        storage.clear(None, data_dir=data)
        assert _audit_rows(storage) == [("clear", "clear:all")]


class TestTheStoredStreamsGoWithTheirDocument:
    def test_a_documents_bytes_are_its_own(self, corpus):
        """The premise the stream removal rests on: source_sha256 is UNIQUE, so
        no surviving Document can carry a removed Document's bytes."""
        storage, _, _, _ = corpus
        sha = storage.corpus_documents("SG")[0]["source_sha256"]
        with pytest.raises(sqlite3.IntegrityError):
            storage.upsert_document("doc_twin", "MY", sha)

    def test_every_stream_over_the_removed_bytes_goes(self, corpus):
        """The same bytes can have more than one stored stream: a different
        extraction version or OCR policy is a different key over them. All of
        them are readings of a Document that is leaving the Corpus."""
        storage, _, data, _ = corpus
        sha = storage.corpus_documents("SG")[0]["source_sha256"]
        surviving_sha = storage.corpus_documents("MY")[0]["source_sha256"]
        storage.extraction_store(
            "a second key over the same bytes", '{"full_text": "x"}',
            source_sha256=sha, extractor_version="v-test", ocr_languages="eng",
            ocr_policy_id="english", source_format="pdf", extractor="test",
            ocr_applied=False, n_chars=1, n_pages=1, n_low_yield_pages=0,
        )
        report = storage.clear("SG", data_dir=data)
        assert report.extractions == 2
        left = [
            r["source_sha256"]
            for r in storage.conn.execute("SELECT source_sha256 FROM extractions")
        ]
        assert left == [surviving_sha]


class TestNothingOutsideTheDataRootIsTouched:
    def test_a_path_outside_the_data_root_is_refused_and_counted(self, corpus, tmp_path):
        storage, _, data, _ = corpus
        outside = tmp_path / "elsewhere" / "treaty.pdf"
        outside.parent.mkdir(parents=True)
        outside.write_bytes(b"not ours to delete")
        storage.upsert_document(
            "doc_outside", "SG", "f" * 64, local_path=str(outside), full_text="x"
        )
        report = storage.clear("SG", data_dir=data)
        assert outside.is_file(), "a file outside the data root survives the clear"
        assert report.refused_files == 1

    def test_a_symlink_pointing_out_of_the_data_root_is_refused(self, corpus, tmp_path):
        storage, _, data, _ = corpus
        outside = tmp_path / "elsewhere" / "treaty.pdf"
        outside.parent.mkdir(parents=True)
        outside.write_bytes(b"not ours to delete")
        link = data / "SG" / "raw" / "link.pdf"
        link.symlink_to(outside)
        storage.upsert_document(
            "doc_link", "SG", "e" * 64, local_path=str(link.relative_to(data)),
            full_text="x",
        )
        report = storage.clear("SG", data_dir=data)
        assert outside.is_file()
        assert report.refused_files == 1


class TestTheEndpointBehindTheControl:
    @pytest.fixture()
    def served(self, corpus, tmp_path):
        """The app over that private Corpus, with its own data root."""
        storage, db, data, ids = corpus
        _seed_run(storage, "SG", "run_sg")
        _seed_run(storage, "MY", "run_my")
        out = tmp_path / "out"
        out.mkdir()
        app = create_app(db_path=db, out_dir=out, data_dir=data, ui_dir=None)
        return TestClient(app), app, storage, data, ids

    def test_the_preview_counts_and_deletes_nothing(self, served):
        client, _, storage, data, ids = served
        before = _stored_files(data)
        r = client.get("/api/clear/preview?economy=SG")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["economy"] == "SG"
        assert body["documents"] == len(ids["SG"])
        assert body["runs"] == 1
        assert body["mappings"] == 1
        assert body["reviews"] == 1
        assert body["stored_files"] == 1
        assert body["bytes"] > 0
        assert _stored_files(data) == before
        assert len(storage.corpus_documents("SG")) == len(ids["SG"])

    def test_the_preview_of_everything_covers_both_economies(self, served):
        client, _, _, _, ids = served
        body = client.get("/api/clear/preview").json()
        assert body["economy"] is None
        assert body["documents"] == len(ids["SG"]) + len(ids["MY"])
        assert body["runs"] == 2

    def test_clearing_one_economy_leaves_the_other_whole(self, served):
        client, _, storage, data, ids = served
        r = client.post("/api/clear", json={"economy": "SG", "confirm": True})
        assert r.status_code == 200, r.text
        assert r.json()["documents"] == len(ids["SG"])
        assert client.get("/api/clear/preview?economy=SG").json()["documents"] == 0
        assert len(storage.corpus_documents("MY")) == len(ids["MY"])
        assert client.get("/api/runs").json()["runs"][0]["run_id"] == "run_my"
        left = [str(p) for p in _stored_files(data)]
        assert left and not any("/SG/" in p for p in left)

    def test_clearing_everything_empties_the_corpus_and_the_runs(self, served):
        client, _, _, data, _ = served
        r = client.post("/api/clear", json={"economy": None, "confirm": True})
        assert r.status_code == 200, r.text
        assert client.get("/api/runs").json()["runs"] == []
        assert client.get("/api/documents").json() == []
        assert _stored_files(data) == []

    def test_a_stored_path_outside_the_data_root_is_refused(self, served, tmp_path):
        client, _, storage, data, _ = served
        outside = tmp_path / "elsewhere" / "treaty.pdf"
        outside.parent.mkdir(parents=True)
        outside.write_bytes(b"not ours to delete")
        storage.upsert_document(
            "doc_outside", "SG", "f" * 64, local_path=str(outside), full_text="x"
        )
        body = client.post("/api/clear", json={"economy": "SG", "confirm": True}).json()
        assert body["refused_files"] == 1
        assert outside.is_file()

    def test_it_refuses_without_the_confirmation(self, served):
        client, _, storage, _, ids = served
        r = client.post("/api/clear", json={"economy": "SG"})
        assert r.status_code == 400
        assert len(storage.corpus_documents("SG")) == len(ids["SG"])

    def test_it_refuses_while_a_job_is_running(self, served, monkeypatch):
        client, app, storage, _, ids = served
        monkeypatch.setattr(app.state.manager, "_active", True)
        r = client.post("/api/clear", json={"economy": "SG", "confirm": True})
        assert r.status_code == 409
        assert "active" in r.json()["detail"]
        assert len(storage.corpus_documents("SG")) == len(ids["SG"])

    def test_an_unknown_economy_code_is_refused(self, served):
        client, _, _, _, _ = served
        assert client.post(
            "/api/clear", json={"economy": "ZZ", "confirm": True}
        ).status_code == 404
        assert client.get("/api/clear/preview?economy=ZZ").status_code == 404

    def test_the_frozen_bundle_lane_has_nothing_to_clear(self, tmp_path):
        manifest = Path(__file__).resolve().parents[1] / "audit_bundle" / "manifest.json"
        if not manifest.is_file():  # pragma: no cover - the bundle is committed
            pytest.skip("no committed audit bundle")
        out = tmp_path / "out"
        out.mkdir()
        client = TestClient(
            create_app(
                db_path=tmp_path / "unused.db", out_dir=out, data_dir=tmp_path / "data",
                bundle_manifest=manifest, ui_dir=None,
            )
        )
        assert client.get("/api/clear/preview").status_code == 409
        assert client.post(
            "/api/clear", json={"economy": None, "confirm": True}
        ).status_code == 409


def _run_extraction_map(storage: Storage, run_id: str) -> dict:
    """What one Run Record says about each Document's text: reused or extracted."""
    import json

    row = storage.conn.execute(
        "SELECT details FROM runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    return json.loads(row["details"])["extraction"]


def _ingest_decisions(storage: Storage) -> list[str]:
    """What each ingest did with the Document it was handed."""
    return [
        r["decision"]
        for r in storage.conn.execute(
            "SELECT decision FROM audit_log WHERE stage = 'm11_ingest' ORDER BY id"
        )
    ]


@pytest.fixture(scope="module")
def passes(tmp_path_factory):
    """Two passes over one Corpus, a clear, the same bytes added again, and a
    third pass. Built once: each pass reads a 130-page Act end to end."""
    from regcompass.engines import fake_completion, fake_embed, resolve_engine
    from regcompass.pipeline import run_economy

    root = tmp_path_factory.mktemp("clear_reuse")
    data = root / "data"
    storage = Storage(root / "reuse.db")
    storage.apply_schema()
    doc_ids = seed_corpus(storage, data, "SG")

    def a_run():
        return run_economy(
            storage, "SG", pillars=(7,), engine=resolve_engine("fake"),
            completion_fn=fake_completion, embed_fn=fake_embed, data_dir=data,
        )

    second = (a_run(), a_run())[1]
    # Read the second pass's Run Record NOW: the clear takes the Run Records of
    # the Economy with everything else.
    second_extraction = _run_extraction_map(storage, second.run_id)
    report = storage.clear("SG", data_dir=data)
    emptied = {
        "documents": storage.corpus_documents("SG"),
        "files": _stored_files(data),
        "extractions": storage.conn.execute(
            "SELECT COUNT(*) AS n FROM extractions"
        ).fetchone()["n"],
    }
    seed_corpus(storage, data, "SG")  # the same bytes, added again
    third = a_run()
    yield storage, doc_ids, second_extraction, report, emptied, third
    storage.close()


class TestTheCacheDoesNotSurviveAClear:
    """The promise in both directions, on the offline fake Engine.

    Before a clear, a second pass over the same Corpus reads no Document again:
    every stream comes back from the store. After a clear there is nothing left
    to serve, so the same bytes are read from scratch when they return.

    Where the reading happens is worth naming, because it is not where a first
    reading of the code expects. Ingest is what extracts: it stores the stream
    the moment a Document enters the Corpus, and a Run reads it back. So a Run
    over a Corpus that is already in place says `reused` whether or not a clear
    happened in between, and the fact that matters is the one the ingest trail
    records: the Document that came back after the clear was READ, not served.
    """

    def test_before_a_clear_a_second_pass_reuses_the_text(self, passes):
        _, doc_ids, second_extraction, _, _, _ = passes
        assert second_extraction == {d: "reused" for d in doc_ids}

    def test_the_clear_takes_the_documents_the_bytes_and_the_streams(self, passes):
        _, doc_ids, _, report, emptied, _ = passes
        assert report.documents == len(doc_ids)
        assert report.extractions == len(doc_ids)
        assert report.stored_files == len(doc_ids)
        assert emptied["documents"] == []
        assert emptied["files"] == []
        assert emptied["extractions"] == 0

    def test_after_a_clear_the_same_bytes_are_extracted_again(self, passes):
        storage, doc_ids, _, _, _, _ = passes
        decisions = _ingest_decisions(storage)
        # One ingest before the clear and one after, and NEITHER reused a
        # stored stream: the second had nothing left to reuse.
        assert len(decisions) == 2 * len(doc_ids)
        assert all(d.startswith("ingested:") for d in decisions)
        assert not any("reuse" in d for d in decisions)

    def test_the_corpus_and_its_stream_are_back_after_the_re_ingest(self, passes):
        storage, doc_ids, _, _, _, third = passes
        assert len(storage.corpus_documents("SG")) == len(doc_ids)
        assert storage.conn.execute(
            "SELECT COUNT(*) AS n FROM extractions"
        ).fetchone()["n"] == len(doc_ids)
        # The Run after the clear reads the stream THIS pass produced: the
        # re-ingest wrote it seconds earlier, and nothing older survived.
        assert _run_extraction_map(storage, third.run_id) == {
            d: "reused" for d in doc_ids
        }


class TestTheCommandLine:
    """The same operation for the runbook, where the interface is not the lane."""

    def _invoke(self, *args, **kwargs):
        return CliRunner().invoke(cli_mod.app, list(args), **kwargs)

    def test_it_shows_the_counts_and_clears_after_a_typed_yes(self, corpus):
        storage, db, data, ids = corpus
        r = self._invoke(
            "clear", "--economy", "SG", "--db", str(db), "--data-dir", str(data),
            input="yes\n",
        )
        assert r.exit_code == 0, r.output
        assert "1 Document" in r.output
        assert "stored file" in r.output
        assert storage.corpus_documents("SG") == []
        assert len(storage.corpus_documents("MY")) == len(ids["MY"])

    def test_anything_but_yes_clears_nothing(self, corpus):
        storage, db, data, ids = corpus
        r = self._invoke(
            "clear", "--economy", "SG", "--db", str(db), "--data-dir", str(data),
            input="no\n",
        )
        assert r.exit_code == 1, r.output
        assert "nothing was cleared" in r.output
        assert len(storage.corpus_documents("SG")) == len(ids["SG"])
        assert _stored_files(data)

    def test_the_yes_flag_skips_the_prompt(self, corpus):
        storage, db, data, _ = corpus
        r = self._invoke(
            "clear", "--economy", "SG", "--db", str(db), "--data-dir", str(data),
            "--yes",
        )
        assert r.exit_code == 0, r.output
        assert storage.corpus_documents("SG") == []

    def test_all_clears_every_economy(self, corpus):
        storage, db, data, _ = corpus
        r = self._invoke(
            "clear", "--all", "--db", str(db), "--data-dir", str(data), "--yes"
        )
        assert r.exit_code == 0, r.output
        assert storage.corpus_documents("SG") == []
        assert storage.corpus_documents("MY") == []
        assert _stored_files(data) == []

    def test_it_needs_exactly_one_of_economy_and_all(self, corpus):
        _, db, data, _ = corpus
        for args in (
            ("clear", "--db", str(db), "--data-dir", str(data), "--yes"),
            ("clear", "--economy", "SG", "--all", "--db", str(db),
             "--data-dir", str(data), "--yes"),
        ):
            assert self._invoke(*args).exit_code == 2

    def test_an_unknown_economy_code_is_refused(self, corpus):
        storage, db, data, ids = corpus
        r = self._invoke(
            "clear", "--economy", "ZZ", "--db", str(db), "--data-dir", str(data),
            "--yes",
        )
        assert r.exit_code == 2, r.output
        assert len(storage.corpus_documents("SG")) == len(ids["SG"])


class TestTheRunRecordsGoWithTheirEvidence:
    def test_a_run_its_mappings_and_its_review_go_together(self, corpus):
        storage, _, data, _ = corpus
        _seed_run(storage, "SG", "run_sg")
        _seed_run(storage, "MY", "run_my")
        report = storage.clear("SG", data_dir=data)
        assert (report.runs, report.mappings, report.reviews) == (1, 1, 1)
        assert [r["run_id"] for r in storage.conn.execute("SELECT run_id FROM runs")] == [
            "run_my"
        ]
        assert [
            r["run_id"] for r in storage.conn.execute("SELECT run_id FROM mappings")
        ] == ["run_my"]
        assert storage.review_get("run_my", "map_my") is not None
        # The Gate scores follow their chunks: one Economy's are gone, the
        # other's are untouched.
        left = list(storage.load_gate_scores())
        assert len(left) == 1
        assert all(not chunk.startswith("doc_sg") for chunk, _ in left)
