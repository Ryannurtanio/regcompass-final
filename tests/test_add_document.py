"""A reviewer adds a Document by upload or by Source URL.

Nothing here touches the network. The upload lane reads committed fixture
bytes; the URL lane is driven over a hand-written recorded response, the same
shape tests/test_discovery.py injects, so the polite-fetch promises (robots.txt
consulted, the rate limiter waited on, the host whitelist enforced) are checked
rather than assumed.

The permanent refusal of Discovery keys on the Portal's own `manual_only`
flag, which says its site rules forbid automated collection, rather than on
its Discovery strategy: several Economies carry strategy `manual` today only
because no Discovery strategy has been wired for them yet, and they may lose
it. No shipped Economy carries the flag now, so that behaviour is driven over
the synthetic Portal in tests/forbidden_portal.py.
"""

from __future__ import annotations

import socket
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from regcompass.config import load_portals
from regcompass.corpus import (
    HostNotAllowedError,
    ManualOnlyEconomyError,
    add_document,
    add_document_from_url,
)
from regcompass.server import create_app
from regcompass.storage import Storage

from forbidden_portal import FORBIDDEN, FORBIDDEN_NAME, forbidden_config, point_loaders_at

ROOT = Path(__file__).resolve().parents[1]
BORN_DIGITAL = ROOT / "tests/fixtures/sample_legislation/born_digital"
MY_PDF = BORN_DIGITAL / "PERSONAL DATA PROTECTION ACT 2010.pdf"
SG_PDF = BORN_DIGITAL / "Telecommunications Act 1999.pdf"

MY_URL = "https://lom.agc.gov.my/ilims/upload/portal/akta/outputaktap/Act%20709%20ori.pdf"
SG_URL = "https://sso.agc.gov.sg/Act/TA1999?ViewType=Pdf"
OFF_WHITELIST_URL = "https://example.org/an-official-looking.pdf"

PERMISSIVE_ROBOTS = "User-agent: *\nDisallow: /private\n"


# ---------------------------------------------------------------------------
# recorded Portal answers + spies
# ---------------------------------------------------------------------------


def recorded_fetch(bodies: dict[str, bytes], *, robots: str = PERMISSIVE_ROBOTS):
    """A fetch over hand-written answers, counting what it was asked for. A
    robots.txt request is answered separately so a test can see whether the
    single-URL lane asked for the Portal's published rules at all."""
    from regcompass.crawl import FetchResult

    calls: list[str] = []
    robots_calls: list[str] = []

    def fetch(url: str) -> FetchResult:
        if url.endswith("/robots.txt"):
            robots_calls.append(url)
            return FetchResult(url, url, 200, robots.encode(), "text/plain", "httpx")
        calls.append(url)
        if url not in bodies:
            return FetchResult(url, url, 404, b"", "text/html", "httpx")
        return FetchResult(url, url, 200, bodies[url], "application/pdf", "httpx")

    fetch.calls = calls  # type: ignore[attr-defined]
    fetch.robots_calls = robots_calls  # type: ignore[attr-defined]
    return fetch


class SpyLimiter:
    """A RateLimiter that records instead of sleeping."""

    def __init__(self, min_interval: float = 0.0):
        self.min_interval = min_interval
        self.waited: list[str] = []

    def wait(self, url: str) -> None:
        self.waited.append(url)


@pytest.fixture()
def storage(tmp_path):
    st = Storage(tmp_path / "add.db")
    st.apply_schema()
    return st


@pytest.fixture()
def app_client(tmp_path):
    """The one server over an empty working database, no built bundle."""
    db = tmp_path / "server.db"
    st = Storage(db)
    st.apply_schema()
    st.conn.close()
    out = tmp_path / "out"
    out.mkdir()
    app = create_app(
        db_path=db, out_dir=out, data_dir=tmp_path / "data", ui_dir=None
    )
    return TestClient(app), db


def upload(
    client: TestClient,
    economy: str,
    path: Path,
    *,
    url: str | None,
    language: str,
    title: str | None = None,
):
    data = {"economy": economy, "language": language}
    # Left OUT rather than sent empty, exactly as the interface sends it.
    if url is not None:
        data["source_url"] = url
    if title is not None:
        data["title"] = title
    with path.open("rb") as fh:
        return client.post(
            "/api/documents/upload",
            data=data,
            files={"file": (path.name, fh, "application/pdf")},
        )


# ---------------------------------------------------------------------------
# Box 1: an uploaded PDF enters the Corpus as a manual Document
# ---------------------------------------------------------------------------


class TestUploadingADocument:
    def test_the_uploaded_pdf_is_in_the_corpus_with_its_language_and_source_url(
        self, app_client
    ):
        client, db = app_client
        r = upload(client, "MY", MY_PDF, url=MY_URL, language="English")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["source_kind"] == "manual"
        assert body["n_pages"] > 0
        assert body["economy"] == "MY"

        st = Storage(db)
        row = st.conn.execute(
            "SELECT * FROM documents WHERE document_id = ?", (body["document_id"],)
        ).fetchone()
        assert row is not None, "the Document must be in the Corpus"
        assert row["source_kind"] == "manual"
        assert row["language"] == "English"
        assert row["source_url"] == MY_URL
        assert row["full_text"], "the same extraction path a discovered Document takes"
        assert len(st.corpus_documents("MY")) == 1

    def test_the_operators_law_name_becomes_the_documents_title(self, app_client):
        """The name a reviewer types wins over the one derived from the file.

        It is the string the Evidence Export writes into the organizer's Law
        Name column, and a file called `upload sample.pdf` otherwise names the
        law "upload sample", which is the name of no statute anywhere."""
        client, db = app_client
        given = "Personal Data Protection Act 2010"
        r = upload(client, "MY", MY_PDF, url=MY_URL, language="English", title=given)
        assert r.status_code == 200, r.text
        assert r.json()["title"] == given

        row = Storage(db).conn.execute(
            "SELECT title FROM documents WHERE document_id = ?",
            (r.json()["document_id"],),
        ).fetchone()
        assert row["title"] == given

    def test_a_law_name_left_blank_falls_back_to_the_derived_title(self, app_client):
        """The field is optional, so an empty one changes nothing at all."""
        client, db = app_client
        without = upload(client, "MY", MY_PDF, url=MY_URL, language="English")
        assert without.status_code == 200, without.text
        derived = without.json()["title"]
        assert derived

        blank = upload(
            client, "SG", SG_PDF, url=SG_URL, language="English", title="   "
        )
        assert blank.status_code == 200, blank.text
        row = Storage(db).conn.execute(
            "SELECT title FROM documents WHERE document_id = ?",
            (blank.json()["document_id"],),
        ).fetchone()
        assert row["title"] == blank.json()["title"]
        assert row["title"].strip(), "a blank Law name must not blank the title"

    def test_an_upload_needs_no_source_url_and_links_as_a_local_copy(
        self, app_client
    ):
        """A file saved by hand during the live hour has to be able to enter
        the Corpus NOW. The address is a fact that can be recorded later; being
        unable to add the Document at all is not recoverable inside the hour.

        What must not happen is the Document pretending to have an address. The
        Corpus row records none, so the source link falls back to this app's own
        copy and says so, and the Evidence Export refuses the row by name."""
        from regcompass.audit import build_source_link

        client, db = app_client
        r = upload(client, "MY", MY_PDF, url=None, language="English")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["source_url"] is None
        assert body["source_kind"] == "manual"
        assert body["n_pages"] > 0

        row = Storage(db).conn.execute(
            "SELECT source_url, full_text FROM documents WHERE document_id = ?",
            (body["document_id"],),
        ).fetchone()
        # No address at all, not a marker that would read as a broken one.
        assert row["source_url"] is None
        assert row["full_text"], "the Document is readable: a Run can map it"

        link = build_source_link(
            document_id=body["document_id"],
            source_url=row["source_url"],
            format_tag="pdf",
            location_reference="p. 3",
        )
        assert link.kind == "local"
        assert link.href.startswith(f"/api/documents/{body['document_id']}/pdf")

    def test_an_upload_without_a_url_is_in_the_corpus_listing_at_once(
        self, app_client
    ):
        """The reason this matters: /api/documents answers what a Run MAPPED,
        so before the first Run an upload is invisible there and looks lost."""
        client, _ = app_client
        assert client.get("/api/corpus", params={"economy": "MY"}).json()["n"] == 0
        r = upload(client, "MY", MY_PDF, url=None, language="English")
        assert r.status_code == 200, r.text

        listing = client.get("/api/corpus", params={"economy": "MY"}).json()
        assert listing["n"] == 1
        entry = listing["documents"][0]
        assert entry["document_id"] == r.json()["document_id"]
        assert entry["source_kind"] == "manual"
        assert entry["language"] == "English"
        assert entry["source_url"] is None
        assert entry["n_pages"] > 0
        assert entry["added_at"]

        # The Run-scoped list still says nothing: nothing has mapped it yet.
        assert client.get("/api/documents").json() == []

    def test_exporting_a_document_with_no_url_refuses_by_name(self, tmp_path):
        """The other half of the bargain for an optional Source URL: a
        Document with no address may enter the Corpus and be Run, but it may
        never SHIP, and the refusal has to say which Document and what to do.

        Before this it surfaced as a bare KeyError on the document id, which
        told a reviewer nothing at all."""
        from regcompass.corpus import add_document
        from regcompass.engines import resolve_engine
        from regcompass.export import ExportGateError
        from regcompass.pipeline import export_from_db, run_economy

        storage = Storage(tmp_path / "nourl.db")
        storage.apply_schema()
        data = tmp_path / "data"
        added = add_document(
            storage, data, "SG", SG_PDF.read_bytes(), source_url=None,
            language="English", filename_hint=SG_PDF.name,
            title="Telecommunications Act 1999",
        )
        assert added.source_url is None
        report = run_economy(
            storage, "SG", pillars=(7,), engine=resolve_engine("fake"), data_dir=data
        )
        assert report.n_passed > 0, "the Document was readable and Runs normally"

        with pytest.raises(ExportGateError) as caught:
            export_from_db(storage, tmp_path / "out", run_id=report.run_id)
        joined = "\n".join(caught.value.failures)
        # Named by its title, as every list on screen names it, not by its id.
        assert "Telecommunications Act 1999" in joined
        assert added.document_id not in joined
        assert "no Source URL recorded" in joined
        assert "Add document" in joined, "the refusal must name the way to fix it"
        assert "Set Source URL" in joined
        # ... by the names the screens really carry: there is no "Add
        # document screen", Add document is a control on Start a Run.
        assert "Add document screen" not in joined
        assert "Start a Run" in joined

    def test_recording_the_url_afterwards_lets_the_export_through(self, tmp_path):
        """The other half of an optional Source URL, end to end.

        A Document added without an address is refused by the export. After
        the address is recorded on the Document ALREADY in the Corpus, the
        same export ships it, the organizer's Source URL cell carries the real
        address, and the row's link stops being a local copy. Re-adding by URL
        would have fetched the file again under a second id, which is a
        duplicate rather than a correction, so this is the only honest way."""
        import csv as csv_mod

        from regcompass.audit import build_source_link
        from regcompass.corpus import add_document
        from regcompass.engines import resolve_engine
        from regcompass.export import ExportGateError
        from regcompass.pipeline import export_from_db, run_economy
        from regcompass.server import create_app

        db = tmp_path / "patch.db"
        data = tmp_path / "data"
        storage = Storage(db)
        storage.apply_schema()
        added = add_document(
            storage, data, "SG", SG_PDF.read_bytes(), source_url=None,
            language="English", filename_hint=SG_PDF.name,
            title="Telecommunications Act 1999",
        )
        report = run_economy(
            storage, "SG", pillars=(7,), engine=resolve_engine("fake"), data_dir=data
        )
        storage.close()
        with pytest.raises(ExportGateError):
            st = Storage(db)
            try:
                export_from_db(st, tmp_path / "out1", run_id=report.run_id)
            finally:
                st.close()

        client = TestClient(
            create_app(db_path=db, out_dir=tmp_path / "out2", data_dir=data, ui_dir=None)
        )
        patched = client.patch(
            f"/api/documents/{added.document_id}", json={"source_url": SG_URL}
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["source_url"] == SG_URL
        assert patched.json()["previous_source_url"] is None

        # The same export now ships, and the cell carries the real address.
        st = Storage(db)
        try:
            row = st.conn.execute(
                "SELECT source_url FROM documents WHERE document_id = ?",
                (added.document_id,),
            ).fetchone()
            assert row["source_url"] == SG_URL
            result = export_from_db(st, tmp_path / "out2", run_id=report.run_id)
        finally:
            st.close()
        rows = list(
            csv_mod.DictReader(result.csv_path.open(encoding="utf-8-sig"))
        )
        # This Document's own rows. The export also writes absence rows for
        # Indicators nothing matched, and those cite the curated Corpus.
        mine = [r for r in rows if r["Law Name"] == "Telecommunications Act 1999"]
        assert mine, f"the export shipped no row for this Document: {rows[:1]}"
        assert all(r["Source URL"] == SG_URL for r in mine), mine[0]

        # And the row's way out to the source is the official one now.
        link = build_source_link(
            document_id=added.document_id, source_url=SG_URL,
            format_tag="pdf", location_reference="p. 3",
        )
        assert link.kind == "official"
        assert link.href.startswith(SG_URL)

    def test_the_url_edit_refuses_a_bad_scheme_and_an_unknown_id_and_shares_an_address(
        self, app_client
    ):
        client, _ = app_client
        first = upload(client, "MY", MY_PDF, url=None, language="English")
        doc_id = first.json()["document_id"]

        for bad in ("", "   ", "ftp://host/a.pdf", "file:///tmp/a.pdf", "agc.gov.my"):
            answer = client.patch(f"/api/documents/{doc_id}", json={"source_url": bad})
            assert answer.status_code == 400, (bad, answer.text)

        assert client.patch(
            "/api/documents/doc_not_here", json={"source_url": MY_URL}
        ).status_code == 404

        # Two Documents MAY share one address, exactly as two uploads may: a
        # landing page publishes a collection, and a scan uploaded by hand has
        # to be able to take its law's official address. Each keeps its own
        # manifest row.
        assert client.patch(
            f"/api/documents/{doc_id}", json={"source_url": MY_URL}
        ).status_code == 200
        second = upload(client, "MY", SG_PDF, url=None, language="English")
        shared = client.patch(
            f"/api/documents/{second.json()['document_id']}",
            json={"source_url": MY_URL},
        )
        assert shared.status_code == 200, shared.text
        assert shared.json()["source_url"] == MY_URL

    def test_the_recorded_url_survives_a_later_add_to_the_same_economy(
        self, app_client
    ):
        """The ingest copies the crawl manifest's url onto the Corpus row every
        time it runs, and it runs on every add. An edit that wrote only the
        Corpus row would therefore be undone by the next upload, silently and
        much later. Both rows are written, so it holds."""
        client, db = app_client
        first = upload(client, "MY", MY_PDF, url=None, language="English")
        doc_id = first.json()["document_id"]
        assert client.patch(
            f"/api/documents/{doc_id}", json={"source_url": MY_URL}
        ).status_code == 200

        # A second Document for the same Economy re-runs the ingest.
        assert upload(
            client, "MY", SG_PDF, url="https://lom.agc.gov.my/other.pdf",
            language="English",
        ).status_code == 200

        row = Storage(db).conn.execute(
            "SELECT source_url FROM documents WHERE document_id = ?", (doc_id,)
        ).fetchone()
        assert row["source_url"] == MY_URL, "the recorded address was overwritten"

    def test_the_recorded_url_leaves_one_manifest_row_and_survives_a_reingest(
        self, app_client, tmp_path
    ):
        """The manifest is what REBUILDS a Corpus row once the documents table
        is gone, so the address has to live there too, and it has to live there
        alone. The upload lane files an address-less Document under a local
        marker; leaving that marker beside the real address would leave two
        fetched rows for one set of bytes, and a re-ingest reads whichever it
        meets first, so the address would come back blank half the time."""
        from regcompass.shortlist import ingest_economy

        client, db = app_client
        added = upload(client, "MY", MY_PDF, url=None, language="English")
        doc_id = added.json()["document_id"]

        storage = Storage(db)
        sha = storage.conn.execute(
            "SELECT source_sha256 FROM documents WHERE document_id = ?", (doc_id,)
        ).fetchone()["source_sha256"]
        before = [
            r["url"]
            for r in storage.conn.execute(
                "SELECT url FROM crawl_manifest WHERE sha256 = ?", (sha,)
            )
        ]
        assert before and all(u.startswith("local-upload:") for u in before), before
        storage.close()

        # The address is ALREADY registered for these same bytes: a Discovery
        # fetched them before the reviewer uploaded their own copy. Two fetched
        # rows for one digest is exactly the state that must not survive.
        storage = Storage(db)
        local_path = storage.conn.execute(
            "SELECT local_path FROM crawl_manifest WHERE sha256 = ?", (sha,)
        ).fetchone()["local_path"]
        storage.manifest_add_pending(MY_URL, "MY", filename_hint="Act 709 ori.pdf")
        storage.manifest_mark_fetched(
            MY_URL, http_status=200, method="manual", sha256=sha,
            content_type="application/pdf", size_bytes=1, local_path=local_path,
        )
        assert len(
            storage.conn.execute(
                "SELECT url FROM crawl_manifest WHERE sha256 = ?", (sha,)
            ).fetchall()
        ) == 2
        storage.close()

        assert client.patch(
            f"/api/documents/{doc_id}", json={"source_url": MY_URL}
        ).status_code == 200

        storage = Storage(db)
        after = [
            r["url"]
            for r in storage.conn.execute(
                "SELECT url FROM crawl_manifest WHERE sha256 = ?", (sha,)
            )
        ]
        assert after == [MY_URL], f"one manifest row per digest, got {after}"

        # Start over from the manifest: drop the Corpus rows and re-ingest the
        # bytes still on disk. The Document comes back WITH its address.
        storage.conn.execute("DELETE FROM documents WHERE document_id = ?", (doc_id,))
        storage.conn.commit()
        results, excluded = ingest_economy(
            storage, tmp_path / "data", "MY", language="English"
        )
        assert results, f"the re-ingest produced nothing: {excluded}"
        rebuilt = storage.conn.execute(
            "SELECT source_url FROM documents WHERE source_sha256 = ?", (sha,)
        ).fetchone()
        storage.close()
        assert rebuilt is not None, "the Document did not come back"
        assert rebuilt["source_url"] == MY_URL

    def test_the_url_edit_waits_for_a_running_job(self, app_client, monkeypatch):
        client, _ = app_client
        added = upload(client, "MY", MY_PDF, url=None, language="English")

        import regcompass.server as server_mod

        monkeypatch.setattr(
            server_mod.RunManager, "status", lambda self: {"active": True}
        )
        refused = client.patch(
            f"/api/documents/{added.json()['document_id']}",
            json={"source_url": MY_URL},
        )
        assert refused.status_code == 409
        assert "active" in refused.json()["detail"]

    def test_the_corpus_listing_is_scoped_and_refuses_an_unknown_economy(
        self, app_client
    ):
        client, _ = app_client
        upload(client, "MY", MY_PDF, url=MY_URL, language="English")
        assert client.get("/api/corpus", params={"economy": "MY"}).json()["n"] == 1
        assert client.get("/api/corpus", params={"economy": "SG"}).json()["n"] == 0
        # lower case is the same Economy
        assert client.get("/api/corpus", params={"economy": "my"}).json()["n"] == 1
        assert client.get("/api/corpus", params={"economy": "ZZ"}).status_code == 404

    def test_the_corpus_summary_counts_every_economy_now_with_its_documents_languages(
        self, app_client
    ):
        """The Start a Run cards: the Corpus as it is now, in the languages its
        own Documents are, never the Portal's declared languages or the size of
        the Corpus at the last Run."""
        client, db = app_client
        a = upload(client, "MY", MY_PDF, url=None, language="English")
        b = upload(client, "MY", SG_PDF, url=None, language="English")
        c = upload(
            client, "SG", BORN_DIGITAL / "C2026C00098VOL01.pdf", url=None,
            language="English",
        )
        assert [r.status_code for r in (a, b, c)] == [200] * 3, [r.text for r in (a, b, c)]
        st = Storage(db)
        st.conn.execute(
            "UPDATE documents SET language = 'Malay' WHERE document_id = ?",
            (b.json()["document_id"],),
        )
        st.conn.execute(
            "UPDATE documents SET language = NULL WHERE document_id = ?",
            (c.json()["document_id"],),
        )
        st.conn.commit()
        st.conn.close()

        summary = client.get("/api/corpus/summary").json()["economies"]
        assert summary["MY"] == {"n": 2, "languages": ["English", "Malay"], "unrecorded": 0}
        # A Document with no language recorded takes its Portal's language
        # where the Portal declares exactly one (Singapore: English).
        assert summary["SG"] == {"n": 1, "languages": ["English"], "unrecorded": 0}
        # Every configured Economy is there, an empty Corpus as zero.
        assert set(summary) == set(load_portals())
        assert summary["IN"] == {"n": 0, "languages": [], "unrecorded": 0}

    def test_an_unrecorded_language_stays_unrecorded_where_the_portal_has_several(
        self, app_client
    ):
        client, db = app_client
        r = upload(client, "IN", MY_PDF, url=None, language="English")
        assert r.status_code == 200, r.text
        st = Storage(db)
        st.conn.execute("UPDATE documents SET language = NULL")
        st.conn.commit()
        st.conn.close()
        summary = client.get("/api/corpus/summary").json()["economies"]
        # India declares English and Hindi: nothing to read it as.
        assert summary["IN"] == {"n": 1, "languages": [], "unrecorded": 1}

    def test_the_corpus_summary_over_no_database_is_all_zero(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        client = TestClient(
            create_app(
                db_path=tmp_path / "none.db", out_dir=out,
                data_dir=tmp_path / "data", ui_dir=None,
            )
        )
        summary = client.get("/api/corpus/summary").json()["economies"]
        assert summary["MY"] == {"n": 0, "languages": [], "unrecorded": 0}

    def test_the_add_files_a_discovery_record_marked_manual(self, app_client):
        client, db = app_client
        r = upload(client, "MY", MY_PDF, url=MY_URL, language="English")
        assert r.status_code == 200, r.text

        st = Storage(db)
        records = st.runs_list(kind="discovery")
        assert len(records) == 1
        record = records[0]
        assert record["run_id"] == r.json()["run_id"]
        assert record["status"] == "completed"
        assert record["engine"] is None
        # a file from the reviewer's machine is not a Document downloaded
        assert record["documents_fetched"] == 0
        assert record["details"]["manual"] is True
        assert record["details"]["source"] == "upload"
        assert record["details"]["source_url"] == MY_URL
        assert record["details"]["document_id"] == r.json()["document_id"]

    def test_the_language_defaults_to_the_economys_first_configured_language(
        self, app_client
    ):
        client, db = app_client
        with MY_PDF.open("rb") as fh:
            r = client.post(
                "/api/documents/upload",
                data={"economy": "MY", "source_url": MY_URL},
                files={"file": (MY_PDF.name, fh, "application/pdf")},
            )
        assert r.status_code == 200, r.text
        assert r.json()["language"] == load_portals()["MY"].languages[0]

    def test_a_language_off_the_organizers_list_is_refused(self, app_client):
        client, _ = app_client
        with MY_PDF.open("rb") as fh:
            r = client.post(
                "/api/documents/upload",
                data={"economy": "MY", "language": "Klingon", "source_url": MY_URL},
                files={"file": (MY_PDF.name, fh, "application/pdf")},
            )
        assert r.status_code == 400
        assert "Klingon" in r.json()["detail"]

    def test_an_unknown_economy_is_refused_before_anything_is_written(self, app_client):
        client, db = app_client
        r = upload(client, "XX", MY_PDF, url=MY_URL, language="English")
        assert r.status_code == 400
        assert Storage(db).runs_list(kind="discovery") == []

    def test_the_same_bytes_twice_are_one_document_not_two(self, app_client):
        """The same file under the same address is one Document uploaded
        twice, which during a timed assessment is the most ordinary mistake
        there is. The operator gets back the Document they already have.
        Refusing it made a reviewer who had lost track think the upload had
        broken. What IS still refused is these bytes under a SECOND address,
        below: only a person can say where a law is published."""
        client, db = app_client
        first = upload(client, "MY", MY_PDF, url=MY_URL, language="English")
        assert first.status_code == 200, first.text
        again = upload(client, "MY", MY_PDF, url=MY_URL, language="English")
        assert again.status_code == 200, again.text
        assert again.json()["document_id"] == first.json()["document_id"]
        assert len(Storage(db).corpus_documents("MY")) == 1

    def test_the_same_bytes_under_a_second_address_are_refused(self, app_client):
        client, db = app_client
        assert upload(client, "MY", MY_PDF, url=MY_URL, language="English").status_code == 200
        again = upload(
            client, "MY", MY_PDF, url=MY_URL + "?v=2", language="English"
        )
        assert again.status_code == 409
        assert "already" in again.json()["detail"].lower()
        assert len(Storage(db).corpus_documents("MY")) == 1

    def test_an_add_waits_for_the_one_job_slot(self, app_client, monkeypatch):
        """An add writes to the working database, so it takes the same single
        slot a Run and a Discovery share. Two writers on one SQLite file is
        not a race worth having."""
        import threading

        import regcompass.discovery as discovery_mod
        from regcompass.discovery import DiscoveryReport

        release = threading.Event()

        def slow_discover(economy, storage, **kwargs):
            release.wait(timeout=5.0)
            return DiscoveryReport(economy=economy, strategy="httpx", run_id="disc_z")

        monkeypatch.setattr(discovery_mod, "discover_economy", slow_discover)
        client, db = app_client
        assert client.post("/api/discover", json={"economy": "SG"}).status_code == 200

        refused = upload(client, "MY", MY_PDF, url=MY_URL, language="English")
        assert refused.status_code == 409
        assert "already active" in refused.json()["detail"]
        assert Storage(db).corpus_documents("MY") == []

        release.set()
        deadline = time.time() + 10.0
        while time.time() < deadline and client.get("/api/status").json()["active"]:
            time.sleep(0.05)
        assert upload(
            client, "MY", MY_PDF, url=MY_URL, language="English"
        ).status_code == 200, "the slot frees and the add goes through"


# ---------------------------------------------------------------------------
# Box 2: add by Source URL, over a recorded answer
# ---------------------------------------------------------------------------


class TestAddingByUrl:
    def test_the_fetched_document_lands_exactly_as_an_uploaded_one_does(
        self, storage, tmp_path
    ):
        fetch = recorded_fetch({SG_URL: SG_PDF.read_bytes()})
        limiter = SpyLimiter()
        result = add_document_from_url(
            storage, tmp_path / "data", "SG", SG_URL,
            language="English", fetch=fetch, limiter=limiter,
        )
        row = storage.conn.execute(
            "SELECT * FROM documents WHERE document_id = ?", (result.document_id,)
        ).fetchone()
        assert row["source_kind"] == "manual"
        assert row["language"] == "English"
        assert row["source_url"] == SG_URL
        assert row["full_text"]

    def test_a_seeded_official_address_takes_its_seeds_title_and_language(
        self, storage, tmp_path
    ):
        """An address the source list already names is a known law: added by
        URL with no title or Language given, it is filed under the law's own
        name and Language, not its file name and the Portal's default."""
        from regcompass.config import load_crawl_seeds

        seed = load_crawl_seeds()["KZ"].families["data_protection"]
        url = seed.urls[0]
        fetch = recorded_fetch({url: SG_PDF.read_bytes()})
        result = add_document_from_url(
            storage, tmp_path / "data", "KZ", url, fetch=fetch, limiter=SpyLimiter(),
        )
        row = storage.conn.execute(
            "SELECT * FROM documents WHERE document_id = ?", (result.document_id,)
        ).fetchone()
        assert row["title"] == seed.law
        assert row["language"] == seed.language == "English"
        assert result.language == "English"

    def test_the_operators_own_title_and_language_still_win_on_a_seeded_address(
        self, storage, tmp_path
    ):
        from regcompass.config import load_crawl_seeds

        url = load_crawl_seeds()["KZ"].families["data_protection"].urls[0]
        fetch = recorded_fetch({url: SG_PDF.read_bytes()})
        result = add_document_from_url(
            storage, tmp_path / "data", "KZ", url, language="Russian",
            title="My own name", fetch=fetch, limiter=SpyLimiter(),
        )
        row = storage.conn.execute(
            "SELECT * FROM documents WHERE document_id = ?", (result.document_id,)
        ).fetchone()
        assert (row["title"], row["language"]) == ("My own name", "Russian")

    def test_robots_is_read_and_the_rate_limiter_is_waited_on_before_the_fetch(
        self, storage, tmp_path
    ):
        fetch = recorded_fetch({SG_URL: SG_PDF.read_bytes()})
        limiter = SpyLimiter()
        add_document_from_url(
            storage, tmp_path / "data", "SG", SG_URL,
            language="English", fetch=fetch, limiter=limiter,
        )
        assert fetch.robots_calls == ["https://sso.agc.gov.sg/robots.txt"]
        assert limiter.waited == [SG_URL]
        assert fetch.calls == [SG_URL], "one URL, one request"

    def test_a_url_robots_disallows_is_refused_without_being_requested(
        self, storage, tmp_path
    ):
        from regcompass.crawl import RobotsDisallowedError

        blocked = "https://sso.agc.gov.sg/private/draft.pdf"
        fetch = recorded_fetch({blocked: SG_PDF.read_bytes()})
        with pytest.raises(RobotsDisallowedError):
            add_document_from_url(
                storage, tmp_path / "data", "SG", blocked,
                language="English", fetch=fetch, limiter=SpyLimiter(),
            )
        assert fetch.calls == [], "the refusal happens before the request"

    def test_a_host_outside_the_whitelist_needs_the_operators_override(
        self, storage, tmp_path
    ):
        fetch = recorded_fetch({OFF_WHITELIST_URL: SG_PDF.read_bytes()})
        with pytest.raises(HostNotAllowedError) as exc:
            add_document_from_url(
                storage, tmp_path / "data", "SG", OFF_WHITELIST_URL,
                language="English", fetch=fetch, limiter=SpyLimiter(),
            )
        assert "example.org" in str(exc.value)
        assert fetch.calls == []

        result = add_document_from_url(
            storage, tmp_path / "data", "SG", OFF_WHITELIST_URL,
            language="English", allow_any_host=True,
            fetch=fetch, limiter=SpyLimiter(),
        )
        assert fetch.calls == [OFF_WHITELIST_URL]
        assert result.document_id

    def test_a_portal_error_is_a_readable_failure_not_a_half_written_corpus(
        self, storage, tmp_path
    ):
        fetch = recorded_fetch({})  # every document URL answers 404
        with pytest.raises(RuntimeError, match="404"):
            add_document_from_url(
                storage, tmp_path / "data", "SG", SG_URL,
                language="English", fetch=fetch, limiter=SpyLimiter(),
            )
        assert storage.corpus_documents("SG") == []

    def test_the_endpoint_drives_the_same_lane(self, app_client, monkeypatch):
        import regcompass.crawl as crawl_mod

        fetch = recorded_fetch({SG_URL: SG_PDF.read_bytes()})
        real_fetch_one = crawl_mod.fetch_one

        def offline_fetch_one(url, economy, **kwargs):
            kwargs["fetch"] = fetch
            kwargs["limiter"] = SpyLimiter()
            return real_fetch_one(url, economy, **kwargs)

        monkeypatch.setattr(crawl_mod, "fetch_one", offline_fetch_one)

        client, db = app_client
        r = client.post(
            "/api/documents/add-url",
            json={"economy": "SG", "source_url": SG_URL, "language": "English"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["source_kind"] == "manual"
        st = Storage(db)
        assert len(st.corpus_documents("SG")) == 1
        assert st.runs_list(kind="discovery")[0]["details"]["source"] == "url"

    def test_the_endpoint_refuses_an_off_whitelist_host_without_the_tick(
        self, app_client
    ):
        client, db = app_client
        r = client.post(
            "/api/documents/add-url",
            json={"economy": "SG", "source_url": OFF_WHITELIST_URL, "language": "English"},
        )
        assert r.status_code == 400
        assert "example.org" in r.json()["detail"]
        assert Storage(db).runs_list(kind="discovery") == []


IN_ACT_URL = (
    "https://indiacode.gov.in/server/api/core/bitstreams/"
    "52f9ecbb-b927-4ba6-ae6f-ca2fac50d4df/content"
)


def india_fetch(status: int = 500, *, body: bytes | None = None):
    """India Code with its robots.txt answering a server error, which is what
    it answered on 16 Sep 2026, and the Act address answering a Document.

    The Act bytes are a committed born-digital PDF standing in for the statute:
    this lane is being tested for what it does about the Portal's unreadable
    rules, and the recorded India PDF is a 64 KB head that cannot be parsed."""
    from regcompass.crawl import FetchResult

    calls: list[str] = []
    robots_calls: list[str] = []
    document = body if body is not None else MY_PDF.read_bytes()

    def fetch(url: str) -> FetchResult:
        if url.endswith("/robots.txt"):
            robots_calls.append(url)
            return FetchResult(url, url, status, b"<h1>error</h1>", "text/html", "httpx")
        calls.append(url)
        if url != IN_ACT_URL:
            return FetchResult(url, url, 404, b"", "text/html", "httpx")
        return FetchResult(url, url, 200, document, "application/pdf", "httpx")

    fetch.calls = calls  # type: ignore[attr-defined]
    fetch.robots_calls = robots_calls  # type: ignore[attr-defined]
    return fetch


def config_with_the_default_policy(tmp_path: Path) -> Path:
    """A copy of config/ with India's operator policy line taken out: the
    one-line change the configuration comment names, which puts this Portal
    back on the default refusal."""
    import shutil

    from regcompass.config import CONFIG_DIR

    config_dir = tmp_path / "default-config"
    shutil.copytree(CONFIG_DIR, config_dir)
    portals_file = config_dir / "portals.yaml"
    text = portals_file.read_text(encoding="utf-8")
    line = "    robots_unavailable_policy: proceed"
    assert line in text, "the India policy line moved; fix this helper"
    portals_file.write_text(
        "\n".join(r for r in text.splitlines() if not r.startswith(line)) + "\n",
        encoding="utf-8",
    )
    return config_dir


class TestAddingAnIndiaActWhileItsRulesAreUnreadable:
    """India Code's robots.txt answers a server error, and India carries the
    operator's decision to proceed anyway. The add lane follows the Portal's
    policy exactly as Discovery does, and says so in its answer."""

    def test_the_lane_fetches_and_reports_the_status_and_the_policy(
        self, storage, tmp_path
    ):
        fetch = india_fetch(500)
        added = add_document_from_url(
            storage, tmp_path / "data", "IN", IN_ACT_URL,
            language="English", fetch=fetch, limiter=SpyLimiter(),
        )
        assert fetch.robots_calls == ["https://indiacode.gov.in/robots.txt"]
        assert fetch.calls == [IN_ACT_URL]
        assert added.robots_unavailable_status == 500
        assert added.robots_unavailable_policy == "proceed"
        assert "500" in added.robots_note and "proceed" in added.robots_note
        assert storage.corpus_documents("IN")

    def test_the_default_refuses_the_same_address_on_the_same_day(
        self, storage, tmp_path
    ):
        from regcompass.crawl import RobotsUnavailableError

        fetch = india_fetch(502)
        with pytest.raises(RobotsUnavailableError):
            add_document_from_url(
                storage, tmp_path / "data", "IN", IN_ACT_URL,
                language="English", fetch=fetch, limiter=SpyLimiter(),
                config_dir=config_with_the_default_policy(tmp_path),
            )
        assert fetch.calls == [], "the Act itself was never requested"
        assert storage.corpus_documents("IN") == []

    def test_the_endpoint_accepts_the_act_and_its_answer_carries_both_facts(
        self, app_client, monkeypatch
    ):
        import regcompass.crawl as crawl_mod

        fetch = india_fetch(500)
        real_fetch_one = crawl_mod.fetch_one

        def offline_fetch_one(url, economy, **kwargs):
            kwargs["fetch"] = fetch
            kwargs["limiter"] = SpyLimiter()
            return real_fetch_one(url, economy, **kwargs)

        monkeypatch.setattr(crawl_mod, "fetch_one", offline_fetch_one)

        client, db = app_client
        r = client.post(
            "/api/documents/add-url",
            json={"economy": "IN", "source_url": IN_ACT_URL, "language": "English"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["robots_unavailable_status"] == 500
        assert body["robots_unavailable_policy"] == "proceed"
        assert "500" in body["robots"] and "proceed" in body["robots"]
        record = Storage(db).runs_list(kind="discovery")[0]
        assert record["details"]["robots_unavailable_status"] == 500
        assert record["details"]["robots_unavailable_policy"] == "proceed"
        # adding by address IS a download, and counts as one
        assert record["documents_fetched"] == 1

    def test_the_endpoint_answers_503_on_the_default_policy(
        self, app_client, monkeypatch, tmp_path
    ):
        """Same address, same answer from the Portal, policy line removed: the
        lane refuses and the interface is told to come back later."""
        import regcompass.crawl as crawl_mod

        fetch = india_fetch(500)
        real_fetch_one = crawl_mod.fetch_one
        default_config = config_with_the_default_policy(tmp_path)

        def offline_fetch_one(url, economy, **kwargs):
            kwargs["fetch"] = fetch
            kwargs["limiter"] = SpyLimiter()
            kwargs["config_dir"] = default_config
            return real_fetch_one(url, economy, **kwargs)

        monkeypatch.setattr(crawl_mod, "fetch_one", offline_fetch_one)

        client, db = app_client
        r = client.post(
            "/api/documents/add-url",
            json={"economy": "IN", "source_url": IN_ACT_URL, "language": "English"},
        )
        assert r.status_code == 503, r.text
        assert "robots.txt" in r.json()["detail"]
        assert fetch.calls == []
        assert Storage(db).corpus_documents("IN") == []


# ---------------------------------------------------------------------------
# Box 3: the next Run reads the added Document
# ---------------------------------------------------------------------------


class TestARunAfterTheAdd:
    def test_the_fake_engine_maps_the_added_document_without_touching_the_network(
        self, storage, tmp_path, monkeypatch
    ):
        from regcompass.engines import fake_completion, fake_embed, resolve_engine
        from regcompass.pipeline import run_economy

        added = add_document(
            storage, tmp_path / "data", "MY", MY_PDF.read_bytes(),
            source_url=MY_URL, language="English",
            filename_hint="personal_data_protection_act_2010.pdf",
        )

        def refuse(*args, **kwargs):
            raise AssertionError("a Run must not touch the network")

        monkeypatch.setattr(socket, "socket", refuse)
        monkeypatch.setattr(socket, "create_connection", refuse)
        monkeypatch.setattr(socket, "getaddrinfo", refuse)

        report = run_economy(
            storage, "MY", (7,), resolve_engine("fake"),
            data_dir=tmp_path / "data",
            completion_fn=fake_completion, embed_fn=fake_embed,
        )
        mapped = storage.load_mappings(economy="MY", run_id=report.run_id)
        assert any(m.document_id == added.document_id for m in mapped), (
            f"the Run found no Mapping from {added.document_id}"
        )

    def test_the_export_reads_the_source_kind_off_the_added_row(
        self, storage, tmp_path
    ):
        """The link the Evidence Export depends on: an added Document's row
        carries source kind 'manual', document_meta surfaces it, and the
        off-corpus synthesizer turns it into the whitelist exemption plus the
        Notes disclosure. What those Notes then say is pinned in
        tests/test_export.py::TestManuallyAddedDocuments."""
        from regcompass.pipeline import synthesize_offcorpus_docs

        added = add_document(
            storage, tmp_path / "data", "MY", MY_PDF.read_bytes(),
            source_url=OFF_WHITELIST_URL, language="English",
            filename_hint="added_by_hand_act_2026.pdf",
        )
        meta = storage.document_meta([added.document_id])
        assert meta[added.document_id]["source_kind"] == "manual"

        synthetic = synthesize_offcorpus_docs(meta)[added.document_id]
        assert synthetic.manual_added is True
        assert synthetic.allow_any_host is True


# ---------------------------------------------------------------------------
# Box 4: a manual-only Economy refuses Discovery and accepts a manual Document
# ---------------------------------------------------------------------------


class TestManualOnlyEconomies:
    @pytest.fixture()
    def forbidden(self, tmp_path, monkeypatch):
        config_dir = forbidden_config(tmp_path)
        point_loaders_at(monkeypatch, config_dir)
        return config_dir

    def test_no_shipped_economy_is_manual_only(self):
        """China carried the flag while its only known host was a law database
        whose robots.txt disallows every agent. Its statutes are now fetched
        from a whitelisted host that permits them, so the flag went with it.
        An Economy whose strategy is `manual` is merely waiting for a Discovery
        strategy of its own and must never inherit the permanent refusal."""
        portals = load_portals()
        assert [c for c, p in portals.items() if p.manual_only] == []
        assert portals["CN"].strategy == "manual"
        assert portals["ID"].strategy != "manual"

    def test_discovery_refuses_a_manual_only_economy_by_name(
        self, storage, tmp_path, forbidden
    ):
        from regcompass.discovery import ManualEconomyError, discover_economy

        with pytest.raises(ManualEconomyError) as exc:
            discover_economy(
                FORBIDDEN, storage, config_dir=forbidden, data_dir=tmp_path / "data"
            )
        message = str(exc.value)
        assert FORBIDDEN_NAME in message and "manual" in message.lower()
        assert "Add document" in message
        assert storage.runs_list(kind="discovery") == []

    def test_the_url_lane_is_closed_on_a_manual_only_economy(
        self, storage, tmp_path, forbidden
    ):
        with pytest.raises(ManualOnlyEconomyError) as exc:
            add_document_from_url(
                storage, tmp_path / "data", FORBIDDEN, "https://www.gov.zz/act.pdf",
                language="English", allow_any_host=True, config_dir=forbidden,
                fetch=recorded_fetch({}), limiter=SpyLimiter(),
            )
        assert "manual-only" in str(exc.value)

    def test_the_endpoint_refuses_add_by_url_and_accepts_an_upload(
        self, app_client, forbidden, monkeypatch
    ):
        import regcompass.corpus as corpus_mod

        real_add = corpus_mod.add_document

        def add_under_forbidden(*args, **kwargs):
            kwargs.setdefault("config_dir", forbidden)
            return real_add(*args, **kwargs)

        monkeypatch.setattr(corpus_mod, "add_document", add_under_forbidden)
        client, db = app_client
        refused = client.post(
            "/api/documents/add-url",
            json={
                "economy": FORBIDDEN,
                "source_url": "https://www.gov.zz/act.pdf",
                "language": "English",
                "allow_any_host": True,
            },
        )
        assert refused.status_code == 400
        assert "manual-only" in refused.json()["detail"]

        accepted = upload(
            client, FORBIDDEN, MY_PDF, url="https://www.gov.zz/act.pdf",
            language="English",
        )
        assert accepted.status_code == 200, accepted.text
        assert len(Storage(db).corpus_documents(FORBIDDEN)) == 1

    def test_the_status_registry_tells_the_interface_which_economies_are_manual_only(
        self, app_client, forbidden
    ):
        client, _ = app_client
        status = client.get("/api/status").json()
        assert status["manual_only"] == [FORBIDDEN]
        assert status["economy_languages"]["MY"] == load_portals()["MY"].languages

    def test_the_status_registry_separates_forbidden_from_not_yet_configured(
        self, app_client, forbidden
    ):
        """The interface has to tell the two apart to explain either one: an
        Economy waiting for a Discovery plan keeps its add-by-URL lane, and a
        manual-only one never had one."""
        client, _ = app_client
        status = client.get("/api/status").json()
        portals = load_portals()
        expected = [
            code for code, portal in portals.items()
            if portal.strategy == "manual" and not portal.manual_only
        ]
        assert status["no_discovery"] == expected
        assert "VN" in status["no_discovery"]
        assert "CN" in status["no_discovery"]
        assert FORBIDDEN not in status["no_discovery"]

    def test_the_discover_endpoint_tells_the_two_refusals_apart(
        self, app_client, forbidden
    ):
        client, _ = app_client
        forbidding = client.post("/api/discover", json={"economy": FORBIDDEN})
        assert forbidding.status_code == 400
        assert "do not permit automated collection" in forbidding.json()["detail"]

        waiting = client.post("/api/discover", json={"economy": "VN"})
        assert waiting.status_code == 400
        detail = waiting.json()["detail"]
        assert "no Discovery strategy configured" in detail
        assert "manual-only" not in detail


class TestChinaAddsByUrlFromTheRegulator:
    """China's statutes come from the Cyberspace Administration of China, the
    regulator that enforces them and republishes the National People's
    Congress text. That host is whitelisted and its rules permit the law
    pages, so the add-by-URL lane is open for it; the national law database,
    whose robots.txt disallows every agent, is never whitelisted."""

    CAC_PIPL = "https://www.cac.gov.cn/2021-08/20/c_1631050028355286.htm"

    def test_the_regulator_is_the_portal_host_and_the_law_database_is_never_listed(self):
        china = load_portals()["CN"]
        assert china.manual_only is False
        assert china.hosts[0] == "www.cac.gov.cn"
        assert "flk.npc.gov.cn" not in china.hosts
        assert china.min_interval_seconds >= 3.0

    def test_a_statute_on_the_regulator_host_is_fetched_politely_and_added(
        self, storage, tmp_path
    ):
        law = (
            "<html><body><h1>中华人民共和国个人信息保护法</h1>"
            "<p>第一条 为了保护个人信息权益，规范个人信息处理活动，"
            "促进个人信息合理利用，根据宪法，制定本法。</p></body></html>"
        ).encode("utf-8")
        fetch = recorded_fetch({self.CAC_PIPL: law})
        limiter = SpyLimiter()
        result = add_document_from_url(
            storage, tmp_path / "data", "CN", self.CAC_PIPL,
            language="Chinese", fetch=fetch, limiter=limiter,
        )
        assert fetch.robots_calls == ["https://www.cac.gov.cn/robots.txt"]
        assert limiter.waited == [self.CAC_PIPL]
        assert fetch.calls == [self.CAC_PIPL], "one URL, one request"
        row = storage.conn.execute(
            "SELECT * FROM documents WHERE document_id = ?", (result.document_id,)
        ).fetchone()
        assert row["source_url"] == self.CAC_PIPL
        assert row["language"] == "Chinese"
        assert "个人信息" in row["full_text"]

    @pytest.mark.parametrize(
        "url",
        [
            "https://flk.npc.gov.cn/detail2.html?ZmY4MDgxODE3YjY0NzJhMzAxN2I2NTA",
            "http://www.npc.gov.cn/npc/c2/c30834/202108/t20210820_313088.html",
            "https://cac.gov.cn/2021-08/20/c_1631050028355286.htm",
        ],
    )
    def test_any_other_host_is_refused_before_a_request(self, storage, tmp_path, url):
        fetch = recorded_fetch({})
        with pytest.raises(HostNotAllowedError):
            add_document_from_url(
                storage, tmp_path / "data", "CN", url,
                language="Chinese", fetch=fetch, limiter=SpyLimiter(),
            )
        assert fetch.calls == []
        assert storage.corpus_documents("CN") == []


# ---------------------------------------------------------------------------
# Box 5: the Document list marks a manual Document
# ---------------------------------------------------------------------------


class TestTheDocumentListMarksManualDocuments:
    def test_the_documents_endpoint_carries_the_source_kind(self, app_client):
        client, db = app_client
        added = upload(client, "MY", MY_PDF, url=MY_URL, language="English")
        assert added.status_code == 200, added.text

        started = client.post(
            "/api/run", json={"economy": "MY", "pillars": [7], "engine": "fake"}
        )
        assert started.status_code == 200, started.text
        deadline = time.time() + 180.0
        while time.time() < deadline:
            st = client.get("/api/status").json()
            if not st["active"] and st["status"] in ("done", "error"):
                break
            time.sleep(0.05)
        assert st["status"] == "done", st

        docs = client.get("/api/documents").json()
        assert docs, "the Run mapped nothing from the added Document"
        assert {d["source_kind"] for d in docs} == {"manual"}


# ---------------------------------------------------------------------------
# the seam the test fixtures share
# ---------------------------------------------------------------------------


class TestTheCorpusSeam:
    def test_seed_corpus_goes_through_the_same_add_document(self, storage, tmp_path):
        """tests/corpus_fixtures.py is not a second write path: it calls the
        one in src/, which is why a fixture Corpus and a hand-added Document
        are the same thing to every later stage."""
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from corpus_fixtures import seed_corpus

        ids = seed_corpus(storage, tmp_path / "data", "SG")
        assert len(ids) == 1
        row = storage.conn.execute(
            "SELECT source_kind FROM documents WHERE document_id = ?", (ids[0],)
        ).fetchone()
        assert row["source_kind"] == "discovery", (
            "a seeded fixture Document stands in for a DISCOVERED one"
        )
