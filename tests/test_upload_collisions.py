"""Two uploads that share one Source URL, and a Run killed the moment it starts.

The 22 Sep China re-rehearsal uploaded three statutes giving each the same
address: the ministry landing page they are all published under, which is
exactly what an assessor pastes when the file itself has no deep link. The
second and third uploads failed, and the FIRST Document silently took on the
third file's bytes: one law's text under another law's name, and the next Run
quietly lost two Mappings.

The cause was the fetch manifest, whose key is the URL alone. A manual upload
therefore landed on the row the previous upload had already claimed. The key
for an upload now carries the content digest beside the address, so the address
identifies where a Document is published and the digest identifies WHICH
Document, which is the distinction the manifest never had.

The last box here is the other half of the same rehearsal: a Run whose process
dies immediately after it is started must still leave a row, so the restart
sweep can report it as interrupted instead of losing it.
"""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from regcompass.server import create_app
from regcompass.storage import Storage

ROOT = Path(__file__).resolve().parents[1]
BORN_DIGITAL = ROOT / "tests/fixtures/sample_legislation/born_digital"
PDF_A = BORN_DIGITAL / "PERSONAL DATA PROTECTION ACT 2010.pdf"
PDF_B = BORN_DIGITAL / "Telecommunications Act 1999.pdf"

#: One address for several statutes: the landing page a ministry publishes its
#: whole collection on. Nothing about it is wrong, and it is the address that
#: belongs in the Source URL column of every row that came off it.
LANDING_PAGE = "https://lom.agc.gov.my/ilims/upload/portal/akta/outputaktap/"


@pytest.fixture()
def app_client(tmp_path):
    db = tmp_path / "server.db"
    out = tmp_path / "out"
    out.mkdir()
    app = create_app(
        db_path=db, out_dir=out, data_dir=tmp_path / "data", ui_dir=None
    )
    return TestClient(app), db, tmp_path / "data"


def upload(
    client: TestClient,
    economy: str,
    path: Path,
    *,
    url: str | None,
    title: str | None = None,
    language: str = "English",
    filename: str | None = None,
):
    data = {"economy": economy, "language": language}
    if url is not None:
        data["source_url"] = url
    if title is not None:
        data["title"] = title
    with path.open("rb") as fh:
        return client.post(
            "/api/documents/upload",
            data=data,
            files={"file": (filename or path.name, fh, "application/pdf")},
        )


def doc_row(db: Path, document_id: str):
    storage = Storage(db)
    try:
        return storage.conn.execute(
            "SELECT * FROM documents WHERE document_id = ?", (document_id,)
        ).fetchone()
    finally:
        storage.close()


# ---------------------------------------------------------------------------
# Box 1: two different files, one address, two Documents
# ---------------------------------------------------------------------------


class TestTwoUploadsUnderOneAddress:
    def test_both_documents_live_and_both_carry_the_address(self, app_client):
        client, db, _data = app_client
        first = upload(client, "MY", PDF_A, url=LANDING_PAGE, title="Act A")
        assert first.status_code == 200, first.text
        second = upload(client, "MY", PDF_B, url=LANDING_PAGE, title="Act B")
        assert second.status_code == 200, second.text

        ids = {first.json()["document_id"], second.json()["document_id"]}
        assert len(ids) == 2, "two files must be two Documents"

        storage = Storage(db)
        rows = storage.conn.execute(
            "SELECT document_id, source_url, source_sha256, title FROM documents"
            " WHERE economy = 'MY' ORDER BY document_id"
        ).fetchall()
        storage.close()
        assert len(rows) == 2, [dict(r) for r in rows]
        assert {r["source_url"] for r in rows} == {LANDING_PAGE}, (
            "the address the operator typed belongs on BOTH Corpus rows"
        )
        assert len({r["source_sha256"] for r in rows}) == 2, "different bytes"
        assert {r["title"] for r in rows} == {"Act A", "Act B"}

    def test_the_manifest_keeps_a_row_for_each_upload(self, app_client):
        """One manifest row per upload, each keyed by address AND digest, so
        neither can land on the other's row."""
        client, db, _data = app_client
        upload(client, "MY", PDF_A, url=LANDING_PAGE)
        upload(client, "MY", PDF_B, url=LANDING_PAGE)

        storage = Storage(db)
        rows = storage.conn.execute(
            "SELECT url, sha256, local_path FROM crawl_manifest WHERE economy = 'MY'"
        ).fetchall()
        storage.close()
        assert len(rows) == 2, [dict(r) for r in rows]
        assert len({r["sha256"] for r in rows}) == 2
        assert len({r["local_path"] for r in rows}) == 2
        assert all(r["url"].startswith(LANDING_PAGE) for r in rows), [
            r["url"] for r in rows
        ]

    def test_a_run_over_the_economy_reads_both_documents(self, app_client):
        """What the Run actually opens: the Corpus, with the bytes on disk."""
        from regcompass.pipeline import corpus_for_run

        client, db, data_dir = app_client
        first = upload(client, "MY", PDF_A, url=LANDING_PAGE, title="Act A")
        second = upload(client, "MY", PDF_B, url=LANDING_PAGE, title="Act B")

        storage = Storage(db)
        corpus = corpus_for_run(storage, "MY", data_dir)
        storage.close()
        seen = {row["document_id"]: path for row, path in corpus}
        assert set(seen) == {
            first.json()["document_id"], second.json()["document_id"]
        }
        bodies = {path.read_bytes() for path in seen.values()}
        assert len(bodies) == 2, "each Document must keep its own bytes on disk"

    def test_the_second_upload_changes_nothing_about_the_first(self, app_client):
        """The defect in one assertion: the first Document's bytes, its file,
        its page count and its name are what they were before."""
        client, db, _data = app_client
        first = upload(client, "MY", PDF_A, url=LANDING_PAGE, title="Act A")
        doc_id = first.json()["document_id"]
        before = dict(doc_row(db, doc_id))

        assert upload(
            client, "MY", PDF_B, url=LANDING_PAGE, title="Act B"
        ).status_code == 200
        after = dict(doc_row(db, doc_id))

        for column in ("source_sha256", "local_path", "n_pages", "title", "full_text"):
            assert after[column] == before[column], (
                f"the later upload changed {column} of an existing Document"
            )


    def test_two_files_of_one_name_are_refused_rather_than_merged(self, app_client):
        """The other half of the same defect, and the one the manifest key
        cannot fix: a Document's id is derived from the file NAME, so two
        different laws both saved as `act.pdf` would still land on one row.
        Refused, naming the fix, rather than silently overwritten."""
        client, db, _data = app_client
        assert upload(
            client, "MY", PDF_A, url=LANDING_PAGE, filename="act.pdf"
        ).status_code == 200
        clash = upload(client, "MY", PDF_B, url=LANDING_PAGE, filename="act.pdf")
        assert clash.status_code == 400, clash.text
        assert "rename" in clash.json()["detail"].lower()

        storage = Storage(db)
        rows = storage.conn.execute(
            "SELECT source_sha256 FROM documents WHERE economy = 'MY'"
        ).fetchall()
        storage.close()
        assert len(rows) == 1, "the refused upload must leave the first row alone"
        assert rows[0]["source_sha256"] == hashlib.sha256(
            PDF_A.read_bytes()
        ).hexdigest()


# ---------------------------------------------------------------------------
# Box 2: the same bytes under the same address are the same Document
# ---------------------------------------------------------------------------


class TestReUploadingTheSameFile:
    def test_the_same_bytes_and_address_return_the_existing_document(
        self, app_client
    ):
        client, db, _data = app_client
        first = upload(client, "MY", PDF_A, url=LANDING_PAGE, title="Act A")
        assert first.status_code == 200, first.text
        again = upload(client, "MY", PDF_A, url=LANDING_PAGE, title="Act A")
        assert again.status_code == 200, again.text
        assert again.json()["document_id"] == first.json()["document_id"]

        storage = Storage(db)
        n_documents = storage.conn.execute(
            "SELECT COUNT(*) AS n FROM documents WHERE economy = 'MY'"
        ).fetchone()["n"]
        storage.close()
        assert n_documents == 1, "one Document, uploaded twice"

    def test_the_repeat_upload_leaves_the_row_untouched(self, app_client):
        client, db, _data = app_client
        first = upload(client, "MY", PDF_A, url=LANDING_PAGE, title="Act A")
        doc_id = first.json()["document_id"]
        before = dict(doc_row(db, doc_id))

        upload(client, "MY", PDF_A, url=LANDING_PAGE, title="Something Else")
        after = dict(doc_row(db, doc_id))
        assert after == before, "a repeat upload is a no-op, not an edit"

    def test_an_address_less_repeat_is_the_same_document_too(self, app_client):
        client, _db, _data = app_client
        first = upload(client, "MY", PDF_A, url=None)
        assert first.status_code == 200, first.text
        again = upload(client, "MY", PDF_A, url=None)
        assert again.status_code == 200, again.text
        assert again.json()["document_id"] == first.json()["document_id"]

    def test_the_same_bytes_under_a_different_address_are_still_refused(
        self, app_client
    ):
        """Not the same fact: these bytes would then claim two addresses, and
        only a person can say which one is where the law is published."""
        client, db, _data = app_client
        assert upload(client, "MY", PDF_A, url=LANDING_PAGE).status_code == 200
        clash = upload(
            client, "MY", PDF_A, url="https://lom.agc.gov.my/ilims/somewhere-else"
        )
        assert clash.status_code == 409, clash.text
        assert "already" in clash.json()["detail"].lower()

        storage = Storage(db)
        n_documents = storage.conn.execute(
            "SELECT COUNT(*) AS n FROM documents WHERE economy = 'MY'"
        ).fetchone()["n"]
        storage.close()
        assert n_documents == 1


# ---------------------------------------------------------------------------
# Box 3: a Run killed the moment it starts still leaves a row
# ---------------------------------------------------------------------------


class TestARunKilledRightAfterStart:
    def test_the_record_exists_before_the_start_call_answers(
        self, app_client, monkeypatch
    ):
        """The Run Record used to be opened by the worker thread, so a process
        killed between "started" and the first model call left nothing at all:
        the Run had happened and the app could not say so."""
        import regcompass.server as server_mod

        client, db, _data = app_client
        assert upload(client, "MY", PDF_A, url=LANDING_PAGE).status_code == 200

        blocked = threading.Event()

        def make_stuck_worker(*args, **kwargs):
            def worker(manager):
                blocked.wait(timeout=10.0)
                manager._finish("done")

            return worker

        monkeypatch.setattr(server_mod, "_make_real_worker", make_stuck_worker)
        started = client.post(
            "/api/run", json={"economy": "MY", "pillars": [7], "engine": "fake"}
        )
        assert started.status_code == 200, started.text
        assert started.json()["status"] == "started"

        # The worker has done nothing and never will. The row must be there.
        storage = Storage(db)
        runs = storage.conn.execute(
            "SELECT run_id, kind, economy, status, engine FROM runs WHERE kind = 'run'"
        ).fetchall()
        storage.close()
        assert len(runs) == 1, [dict(r) for r in runs]
        assert runs[0]["status"] == "running"
        assert runs[0]["economy"] == "MY"
        assert runs[0]["engine"] == "fake"
        blocked.set()

    def test_the_lost_run_is_listed_as_interrupted_after_a_restart(
        self, app_client, monkeypatch, tmp_path
    ):
        import regcompass.server as server_mod

        client, db, data_dir = app_client
        assert upload(client, "MY", PDF_A, url=LANDING_PAGE).status_code == 200

        blocked = threading.Event()

        def make_stuck_worker(*args, **kwargs):
            def worker(manager):
                blocked.wait(timeout=10.0)
                manager._finish("done")

            return worker

        monkeypatch.setattr(server_mod, "_make_real_worker", make_stuck_worker)
        started = client.post(
            "/api/run", json={"economy": "MY", "pillars": [7], "engine": "fake"}
        )
        assert started.status_code == 200, started.text

        # The process dies here: nothing closes the record. A new server opens
        # over the same working database.
        out2 = tmp_path / "out2"
        out2.mkdir()
        # Entered, because the sweep belongs to the moment the new server
        # STARTS: merely building an application opens no database.
        with TestClient(
            create_app(db_path=db, out_dir=out2, data_dir=data_dir, ui_dir=None)
        ) as restarted:
            runs = restarted.get("/api/runs").json()["runs"]
        lost = [r for r in runs if r["kind"] == "run"]
        assert len(lost) == 1, runs
        assert lost[0]["status"] == "interrupted"
        assert "interrupted" in (lost[0]["error"] or "").lower()
        blocked.set()

    def test_a_real_run_adopts_that_record_rather_than_opening_a_second(
        self, app_client
    ):
        """The other side of the same change. The Run the worker actually
        performs must CLOSE the record the request opened, not file one of its
        own beside it: two rows for one Run would be worse than none."""
        client, db, _data = app_client
        assert upload(client, "MY", PDF_A, url=LANDING_PAGE).status_code == 200

        started = client.post(
            "/api/run", json={"economy": "MY", "pillars": [7], "engine": "fake"}
        )
        assert started.status_code == 200, started.text
        deadline = time.time() + 180.0
        status = client.get("/api/status").json()
        while time.time() < deadline:
            status = client.get("/api/status").json()
            if not status["active"] and status["status"] in ("done", "error"):
                break
            time.sleep(0.05)
        assert status["status"] == "done", status

        storage = Storage(db)
        runs = storage.conn.execute(
            "SELECT run_id, status FROM runs WHERE kind = 'run'"
        ).fetchall()
        storage.close()
        assert len(runs) == 1, [dict(r) for r in runs]
        assert runs[0]["status"] == "completed", dict(runs[0])

    def test_a_worker_that_dies_before_the_pipeline_closes_the_record_as_failed(
        self, app_client, monkeypatch
    ):
        """The record is opened before the worker runs, and the pipeline closes
        it once it has adopted it. A failure BEFORE that adoption (the Corpus
        read raising, say) would otherwise leave the row at `running` until
        the next restart, with the Runs list showing a Run in progress that
        died on its first step. The worker must close it as failed itself."""
        import regcompass.pipeline as pipeline_mod

        client, db, _data = app_client
        assert upload(client, "MY", PDF_A, url=LANDING_PAGE).status_code == 200

        def explode(*args, **kwargs):
            raise RuntimeError("corpus read fell over before the first model call")

        monkeypatch.setattr(pipeline_mod, "run_economy", explode)
        started = client.post(
            "/api/run", json={"economy": "MY", "pillars": [7], "engine": "fake"}
        )
        assert started.status_code == 200, started.text
        deadline = time.time() + 30.0
        status = client.get("/api/status").json()
        while time.time() < deadline:
            status = client.get("/api/status").json()
            if not status["active"] and status["status"] in ("done", "error"):
                break
            time.sleep(0.05)
        assert status["status"] == "error", status

        storage = Storage(db)
        runs = storage.conn.execute(
            "SELECT run_id, status, error FROM runs WHERE kind = 'run'"
        ).fetchall()
        storage.close()
        assert len(runs) == 1, [dict(r) for r in runs]
        assert runs[0]["status"] == "failed", dict(runs[0])
        assert "fell over" in (runs[0]["error"] or "")

        listed = client.get("/api/runs").json()["runs"]
        assert [r["status"] for r in listed if r["kind"] == "run"] == ["failed"]

    def test_a_refused_start_does_not_leave_a_running_record(
        self, app_client, monkeypatch
    ):
        """The one worker slot is still the guard. A Run refused because
        another job holds it must not leave an open record behind: the record
        is opened before the slot is taken, so the refusal has to close it."""
        import regcompass.server as server_mod

        client, db, _data = app_client
        assert upload(client, "MY", PDF_A, url=LANDING_PAGE).status_code == 200

        blocked = threading.Event()

        def make_stuck_worker(*args, **kwargs):
            def worker(manager):
                blocked.wait(timeout=10.0)
                manager._finish("done")

            return worker

        monkeypatch.setattr(server_mod, "_make_real_worker", make_stuck_worker)
        first = client.post(
            "/api/run", json={"economy": "MY", "pillars": [7], "engine": "fake"}
        )
        assert first.status_code == 200, first.text
        second = client.post(
            "/api/run", json={"economy": "MY", "pillars": [7], "engine": "fake"}
        )
        assert second.status_code == 409, second.text

        storage = Storage(db)
        open_rows = storage.conn.execute(
            "SELECT run_id FROM runs WHERE kind = 'run' AND status = 'running'"
        ).fetchall()
        storage.close()
        assert len(open_rows) == 1, [dict(r) for r in open_rows]
        blocked.set()
