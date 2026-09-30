"""Correcting a Document's Source URL and title in place, and the list of
Runs the Evidence screen chooses from.

A wrong link or a misread title used to mean removing the Document and adding
it again, which fetches it again, gives it a new id and orphans every Mapping
that quoted it. The edit changes the Document's metadata and nothing else, and
the next Evidence Export carries the corrected values. The Evidence screen's
Economy and Engine pickers are built from one small listing of finished Runs.
"""

from __future__ import annotations

import csv as csv_mod
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from regcompass.server import create_app
from regcompass.storage import Storage

from test_add_document import MY_PDF, MY_URL, SG_PDF, SG_URL, upload

NEW_TITLE = "Telecommunications Act 1999 (2020 Revised Edition)"
NEW_URL = "https://sso.agc.gov.sg/Act/TA1999"


@pytest.fixture()
def app_client(tmp_path):
    db = tmp_path / "server.db"
    st = Storage(db)
    st.apply_schema()
    st.conn.close()
    out = tmp_path / "out"
    out.mkdir()
    app = create_app(db_path=db, out_dir=out, data_dir=tmp_path / "data", ui_dir=None)
    return TestClient(app), db


def corpus_row(client: TestClient, economy: str, document_id: str) -> dict:
    listing = client.get("/api/corpus", params={"economy": economy}).json()
    return next(d for d in listing["documents"] if d["document_id"] == document_id)


# ---------------------------------------------------------------------------
# Editing a Document's metadata
# ---------------------------------------------------------------------------


class TestEditDocument:
    def test_title_and_url_change_in_place_and_say_who_changed_them(self, app_client):
        client, db = app_client
        added = upload(client, "MY", MY_PDF, url=MY_URL, language="English")
        doc_id = added.json()["document_id"]
        st = Storage(db)
        chunks_before = st.conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE document_id = ?", (doc_id,)
        ).fetchone()[0]
        text_before = st.conn.execute(
            "SELECT full_text FROM documents WHERE document_id = ?", (doc_id,)
        ).fetchone()[0]
        st.close()

        new_url = "https://lom.agc.gov.my/act-709.pdf"
        answer = client.patch(
            f"/api/documents/{doc_id}",
            json={
                "title": "  Personal Data Protection Act 2010  ",
                "source_url": new_url,
                "reviewer": "Feng",
            },
        )
        assert answer.status_code == 200, answer.text
        body = answer.json()
        assert body["document_id"] == doc_id
        assert body["title"] == "Personal Data Protection Act 2010"
        assert body["source_url"] == new_url
        assert body["previous_source_url"] == MY_URL
        assert body["edited_by"] == "Feng"
        assert body["edited_at"]

        row = corpus_row(client, "MY", doc_id)
        assert row["title"] == "Personal Data Protection Act 2010"
        assert row["source_url"] == new_url
        assert row["edited_by"] == "Feng"
        assert row["edited_at"] == body["edited_at"]
        # The stored file is still there to open.
        assert row["has_copy"] is True
        assert client.get(f"/api/documents/{doc_id}/pdf").status_code == 200

        # Metadata only: the id, the text and the chunks are what they were.
        st = Storage(db)
        try:
            assert st.conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE document_id = ?", (doc_id,)
            ).fetchone()[0] == chunks_before
            assert st.conn.execute(
                "SELECT full_text FROM documents WHERE document_id = ?", (doc_id,)
            ).fetchone()[0] == text_before
        finally:
            st.close()

    def test_the_title_alone_can_change_and_the_name_is_optional(self, app_client):
        client, _ = app_client
        added = upload(client, "MY", MY_PDF, url=MY_URL, language="English")
        doc_id = added.json()["document_id"]
        answer = client.patch(f"/api/documents/{doc_id}", json={"title": "PDPA 2010"})
        assert answer.status_code == 200, answer.text
        assert answer.json()["edited_by"] is None
        row = corpus_row(client, "MY", doc_id)
        assert row["title"] == "PDPA 2010"
        assert row["source_url"] == MY_URL  # untouched

    def test_refusals_are_plain_and_change_nothing(self, app_client):
        client, _ = app_client
        added = upload(client, "MY", MY_PDF, url=MY_URL, language="English")
        doc_id = added.json()["document_id"]
        before = corpus_row(client, "MY", doc_id)

        for bad in ("javascript:alert(1)", "ftp://host/a.pdf", "agc.gov.my", " "):
            answer = client.patch(
                f"/api/documents/{doc_id}", json={"source_url": bad, "title": "X"}
            )
            assert answer.status_code == 400, (bad, answer.text)
            assert "http" in answer.json()["detail"]

        blank = client.patch(f"/api/documents/{doc_id}", json={"title": "   "})
        assert blank.status_code == 400
        assert "title" in blank.json()["detail"].lower()

        nothing = client.patch(f"/api/documents/{doc_id}", json={})
        assert nothing.status_code == 400

        assert client.patch(
            "/api/documents/doc_not_here", json={"title": "X"}
        ).status_code == 404

        assert corpus_row(client, "MY", doc_id) == before

    def test_a_document_with_no_stored_file_has_no_copy_to_open(self, app_client):
        client, db = app_client
        st = Storage(db)
        st.conn.execute(
            "INSERT INTO documents (document_id, economy, source_url, source_sha256,"
            " full_text, title, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("doc_my_text_only", "MY", MY_URL, "abc123", "Section 1.", "Text only",
             "2026-09-29T00:00:00Z"),
        )
        st.conn.commit()
        st.close()
        assert corpus_row(client, "MY", "doc_my_text_only")["has_copy"] is False

    def test_the_next_export_carries_the_edit_and_the_mappings_stay(self, tmp_path):
        from regcompass.corpus import add_document
        from regcompass.engines import resolve_engine
        from regcompass.pipeline import export_from_db, run_economy

        db = tmp_path / "edit.db"
        data = tmp_path / "data"
        storage = Storage(db)
        storage.apply_schema()
        added = add_document(
            storage, data, "SG", SG_PDF.read_bytes(), source_url=SG_URL,
            language="English", filename_hint=SG_PDF.name,
            title="Telecommunications Act",
        )
        report = run_economy(
            storage, "SG", pillars=(7,), engine=resolve_engine("fake"), data_dir=data
        )
        mappings_before = sorted(
            tuple(r) for r in storage.conn.execute(
                "SELECT mapping_id, document_id FROM mappings WHERE run_id = ?",
                (report.run_id,),
            )
        )
        storage.close()
        assert mappings_before, "the fake Engine mapped nothing to check against"

        client = TestClient(
            create_app(db_path=db, out_dir=tmp_path / "o", data_dir=data, ui_dir=None)
        )
        answer = client.patch(
            f"/api/documents/{added.document_id}",
            json={"title": NEW_TITLE, "source_url": NEW_URL, "reviewer": "Feng"},
        )
        assert answer.status_code == 200, answer.text

        st = Storage(db)
        try:
            assert sorted(
                tuple(r) for r in st.conn.execute(
                    "SELECT mapping_id, document_id FROM mappings WHERE run_id = ?",
                    (report.run_id,),
                )
            ) == mappings_before
            result = export_from_db(st, tmp_path / "out", run_id=report.run_id)
        finally:
            st.close()
        rows = list(csv_mod.DictReader(result.csv_path.open(encoding="utf-8-sig")))
        mine = [r for r in rows if r["Law Name"] == NEW_TITLE]
        assert mine, f"no row carries the edited title: {[r['Law Name'] for r in rows]}"
        assert all(r["Source URL"] == NEW_URL for r in mine)
        # A person typed this name, so the row no longer says it was derived.
        assert not any("mechanically derived" in r["Notes"] for r in mine)


    def test_renaming_a_document_outside_the_corpus_file_keeps_its_discovery_tag(
        self, tmp_path
    ):
        from regcompass.config import load_corpus, load_law_metadata
        from regcompass.corpus import add_document
        from regcompass.engines import resolve_engine
        from regcompass.pipeline import export_from_db, run_economy

        db = tmp_path / "rename.db"
        data = tmp_path / "data"
        storage = Storage(db)
        storage.apply_schema()
        added = add_document(
            storage, data, "SG", SG_PDF.read_bytes(), source_url=SG_URL,
            language="English", filename_hint=SG_PDF.name,
            title="Telecommunications Act",
        )
        doc_id = added.document_id
        # The case this guards: no curated entry, so the export builds the Law
        # Name from the stored title.
        assert doc_id not in load_corpus()
        assert doc_id not in load_law_metadata()
        report = run_economy(
            storage, "SG", pillars=(7,), engine=resolve_engine("fake"), data_dir=data
        )

        def tags(st: Storage, out: str) -> list[tuple[str, str, str]]:
            result = export_from_db(st, tmp_path / out, run_id=report.run_id)
            rows = csv_mod.DictReader(result.csv_path.open(encoding="utf-8-sig"))
            return sorted(
                (r["Indicator ID"], r["Discovery Tag"], r["Novelty Scope"])
                for r in rows if r["Source URL"] == SG_URL
            )

        before = tags(storage, "before")
        # The ESCAP database knows this law by its original name: a row is
        # KNOWN, or NEW at provision level (the law known, not the section).
        assert before and any(t[1] == "KNOWN" or t[2] == "provision" for t in before)
        storage.edit_document(doc_id, title="A name the ESCAP database never used")
        storage.edit_document(doc_id, title="And renamed once more")
        assert storage.conn.execute(
            "SELECT derived_title FROM documents WHERE document_id = ?", (doc_id,)
        ).fetchone()[0] == "Telecommunications Act"
        try:
            assert tags(storage, "after") == before
        finally:
            storage.close()


class TestEditsReachCuratedEntries:
    """A Document in the curated corpus file takes its export metadata from
    that file. A reviewer's later edit is the newer word, so it wins, and the
    Discovery Tag still matches the ESCAP database by the old name."""

    def test_an_edit_overrides_the_curated_entry(self):
        from regcompass.config import load_corpus
        from regcompass.export import with_document_edits

        corpus = load_corpus()
        doc_id = "doc_sg_telecommunications_act_1999"
        original = corpus[doc_id]
        meta = {
            doc_id: {
                "title": NEW_TITLE,
                "source_url": NEW_URL,
                "edited_fields": ["title", "source_url"],
            },
            "doc_my_personal_data_protection_act_2010": {
                "title": "a derived title nobody edited",
                "source_url": "https://example.org/x.pdf",
                "edited_fields": [],
            },
        }
        edited = with_document_edits(corpus, meta)
        assert edited[doc_id].law_name == NEW_TITLE
        assert edited[doc_id].source_url == NEW_URL
        assert edited[doc_id].url_is_direct is True
        assert edited[doc_id].known_matrix_law_name == (
            original.known_matrix_law_name or original.law_name
        )
        assert edited[doc_id].law_number_ref == original.law_number_ref
        # Untouched Documents keep the curated values.
        untouched = "doc_my_personal_data_protection_act_2010"
        assert edited[untouched] == corpus[untouched]
        # The input is not changed under the caller.
        assert corpus[doc_id] == original

    def test_only_the_edited_field_moves(self):
        from regcompass.config import load_corpus
        from regcompass.export import with_document_edits

        corpus = load_corpus()
        doc_id = "doc_sg_telecommunications_act_1999"
        edited = with_document_edits(
            corpus,
            {doc_id: {"title": "derived", "source_url": NEW_URL, "edited_fields": ["source_url"]}},
        )
        assert edited[doc_id].law_name == corpus[doc_id].law_name
        assert edited[doc_id].source_url == NEW_URL


# ---------------------------------------------------------------------------
# The Runs the Evidence screen chooses from
# ---------------------------------------------------------------------------


def seed_run(st: Storage, run_id, economy, engine, pillars, started, *, kind="run",
             status="completed"):
    st.run_start(
        run_id=run_id, kind=kind, economy=economy, pillars=pillars, indicators=None,
        engine=engine, started_at=started,
    )
    if status != "running":
        st.run_finish(run_id, status=status, ended_at=started)


class TestEvidenceRuns:
    def test_an_empty_database_lists_nothing(self, app_client):
        client, _ = app_client
        assert client.get("/api/evidence/runs").json() == {"economies": []}

    def test_every_economy_with_finished_runs_newest_per_engine_and_pillars(
        self, app_client
    ):
        client, db = app_client
        st = Storage(db)
        seed_run(st, "run_id_b_old", "ID", "engine-b", [6, 7], "2026-09-20T00:00:00Z")
        seed_run(st, "run_id_b_new", "ID", "engine-b", [6, 7], "2026-09-22T00:00:00Z")
        seed_run(st, "run_id_b_p7", "ID", "engine-b", [7], "2026-09-21T00:00:00Z")
        seed_run(st, "run_id_a", "ID", "engine-a", [6, 7], "2026-09-23T00:00:00Z")
        seed_run(st, "run_my_b", "MY", "engine-b", [6, 7], "2026-09-24T00:00:00Z")
        # Not finished Runs: never offered.
        seed_run(st, "run_sg_fail", "SG", "engine-b", [6], "2026-09-25T00:00:00Z",
                 status="failed")
        seed_run(st, "run_sg_live", "SG", "engine-b", [6], "2026-09-26T00:00:00Z",
                 status="running")
        seed_run(st, "disc_au", "AU", None, [], "2026-09-27T00:00:00Z",
                 kind="discovery")
        st.close()

        body = client.get("/api/evidence/runs").json()
        economies = body["economies"]
        # Newest finished Run first, so the first entry is the sensible default.
        assert [e["economy"] for e in economies] == ["MY", "ID"]
        assert [e["name"] for e in economies] == ["Malaysia", "Indonesia"]
        indonesia = economies[1]
        assert indonesia["newest_run_id"] == "run_id_a"
        assert [
            (r["run_id"], r["engine"], r["pillars"]) for r in indonesia["runs"]
        ] == [
            ("run_id_a", "engine-a", [6, 7]),
            ("run_id_b_new", "engine-b", [6, 7]),
            ("run_id_b_p7", "engine-b", [7]),
        ]
        assert all(r["started_at"] for r in indonesia["runs"])
