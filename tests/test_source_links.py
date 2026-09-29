"""Following a Mapping row to its official source at the cited article.

The rule the interface's "Open source" control obeys lives in ONE pure
function (regcompass.audit.build_source_link), so the audit view, the review
lane and the Comparison screen cannot drift apart about where a row points.

What is pinned here:

  * an official http(s) Source URL opens as it is, with the PDF page fragment
    appended when the row's Location Reference names a page (the same
    `PDF: page N` spelling regcompass.export writes into the Evidence Export,
    parsed rather than re-invented);
  * an HTML Document uses a section anchor when the Location Reference carries
    one, and a plain URL when it does not;
  * a Document with no http(s) Source URL of its own (a frozen bundle row, an
    upload recorded without one) falls back to the app's OWN stored-file
    endpoint, marked kind='local' so the interface can say "local copy";
  * a Source URL that already carries a fragment is never rewritten.

The endpoint tests assert the three fields travel with the row on both audit
lanes: the working database and the frozen bundle.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from regcompass.audit import build_source_link, media_type_for_stored_file
from regcompass.compare import compare_runs
from regcompass.contracts import Chunk, MappingRecord, RunRecord
from regcompass.export import location_reference
from regcompass.extract import format_for_extractor
from regcompass.server import create_app
from regcompass.storage import Storage, utc_now_iso

ROOT = Path(__file__).resolve().parents[1]

SSO_URL = "https://sso.agc.gov.sg/Act/TA1999"
# What fixtures.BUNDLED records for the Singapore fixture: a real Portal
# address, which is why a seeded fixture Document links OUT rather than to its
# local copy. The local lane is for Documents with no http(s) URL at all.
FIXTURE_URL = "https://sso.agc.gov.sg/Act/TA1999?ViewType=Pdf"


# ---------------------------------------------------------------------------
# the pure function
# ---------------------------------------------------------------------------


def _link(**kwargs):
    base = dict(
        document_id="doc_x",
        source_url=SSO_URL,
        format_tag="pdf",
        location_reference="PDF: page 12",
    )
    base.update(kwargs)
    return build_source_link(**base)


class TestBuildSourceLink:
    def test_pdf_with_a_page_carries_the_page_fragment(self):
        link = _link()
        assert link.kind == "official"
        assert link.page == 12
        assert link.href == f"{SSO_URL}#page=12"

    def test_pdf_without_a_page_is_the_plain_url(self):
        link = _link(location_reference="")
        assert (link.kind, link.page, link.href) == ("official", None, SSO_URL)

    def test_the_page_is_read_from_the_exported_spelling(self):
        """The Location Reference the Evidence Export writes is the ONLY format
        parsed here; export.location_reference is its single source."""
        record = MappingRecord(
            mapping_id="m1",
            document_id="doc_x",
            chunk_id="c1",
            economy="SG",
            indicator_id="7.1",
            indicator_name="Indicator 7.1",
            section="s. 3",
            verbatim_quote="A licensee shall publish its standard terms.",
            page_number=7,
        )
        assert location_reference(record) == "PDF: page 7"
        assert _link(location_reference=location_reference(record)).page == 7

    def test_html_with_a_section_anchor_uses_the_anchor(self):
        link = _link(format_tag="html", location_reference="HTML: #pt2-s13")
        assert link.href == f"{SSO_URL}#pt2-s13"
        # a page fragment is a PDF idea; an HTML row never gets one
        assert link.page is None

    def test_html_without_an_anchor_is_the_plain_url(self):
        link = _link(format_tag="html", location_reference="")
        assert (link.kind, link.href) == ("official", SSO_URL)

    def test_a_url_that_already_has_a_fragment_is_left_alone(self):
        deep = "https://www.legislation.gov.au/C2026C00098/latest/text#s473.1"
        assert _link(source_url=deep).href == deep

    def test_an_upload_with_no_official_url_opens_the_stored_copy(self):
        link = _link(source_url="")
        assert link.kind == "local"
        assert link.href == "/api/documents/doc_x/pdf#page=12"

    def test_a_missing_source_url_opens_the_stored_copy(self):
        link = _link(source_url=None, location_reference="")
        assert (link.kind, link.page) == ("local", None)
        assert link.href == "/api/documents/doc_x/pdf"

    def test_a_non_http_marker_is_not_an_official_source(self):
        link = _link(source_url="file:///Users/reviewer/Downloads/act.pdf")
        assert link.kind == "local"
        assert link.href.startswith("/api/documents/doc_x/pdf")

    @pytest.mark.parametrize(
        "url",
        [
            "javascript:alert(1)",
            "JavaScript:alert(1)",
            "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
            "vbscript:msgbox(1)",
        ],
    )
    def test_a_script_bearing_url_never_becomes_the_link(self, url):
        """Only http(s) is an official source. A Source URL in any other scheme
        is not put in an href at all: the link falls back to this app's own
        stored-file endpoint, so a stored javascript: or data: value can never
        be what the reviewer's browser follows."""
        link = _link(source_url=url)
        assert link.kind == "local"
        assert link.href == "/api/documents/doc_x/pdf#page=12"

    def test_a_fixture_document_keeps_its_real_portal_address(self):
        """The bundled fixture Documents ship WITH an official Source URL, so
        they link out like any Portal fetch; 'local copy' is reserved for rows
        that honestly have no Portal address recorded."""
        assert _link(source_url=FIXTURE_URL).kind == "official"

    def test_the_local_copy_stays_inside_the_named_run(self):
        link = _link(source_url=None, run_id="run 7")
        assert link.href == "/api/documents/doc_x/pdf?run_id=run%207#page=12"

    def test_the_document_id_is_escaped_in_the_local_href(self):
        link = _link(source_url=None, document_id="doc a/b", location_reference="")
        assert link.href == "/api/documents/doc%20a%2Fb/pdf"


class TestFormatForExtractor:
    def test_the_html_lane_is_read_off_the_extractor_name(self):
        assert format_for_extractor("bs4-lxml") == "html"

    def test_every_other_engine_is_a_pdf_reader(self):
        assert format_for_extractor("pdfplumber") == "pdf"
        assert format_for_extractor("pypdfium2") == "pdf"
        assert format_for_extractor(None) == "pdf"


class TestMediaTypeOfTheStoredFile:
    """The local-copy link opens the stored bytes in a browser tab, so the
    endpoint has to say what they ARE. The stored file's own extension is the
    first answer, the extractor that read it the fallback, and neither known
    means we do not claim a type at all."""

    def test_a_pdf(self):
        assert media_type_for_stored_file("act.pdf") == "application/pdf"

    def test_an_html_page_declares_its_charset(self):
        assert media_type_for_stored_file("act.html") == "text/html; charset=utf-8"
        assert media_type_for_stored_file("act.HTM") == "text/html; charset=utf-8"

    def test_a_word_document(self):
        assert media_type_for_stored_file("act.docx") == (
            "application/vnd.openxmlformats-officedocument"
            ".wordprocessingml.document"
        )

    def test_an_unknown_extension_falls_back_to_the_extractor(self):
        assert media_type_for_stored_file("act", "bs4-lxml") == "text/html; charset=utf-8"
        assert media_type_for_stored_file("act", "pdfplumber") == "application/pdf"

    def test_nothing_known_claims_nothing(self):
        assert media_type_for_stored_file("act") == "application/octet-stream"


# ---------------------------------------------------------------------------
# the API: the working-database lane
# ---------------------------------------------------------------------------

DOC_ID = "doc_seeded_act"
QUOTE = "The Authority may license any person to provide a telecommunication service."


@pytest.fixture()
def seeded_db(tmp_path) -> Path:
    db = tmp_path / "regcompass.db"
    storage = Storage(db)
    storage.apply_schema()
    storage.upsert_document(
        DOC_ID,
        "SG",
        "sha_seeded",
        full_text=QUOTE,
        title="Seeded Act 1999",
        n_pages=1,
        language="en",
        source_url=SSO_URL,
        extractor="pdfplumber",
    )
    storage.upsert_chunks(
        [
            Chunk(
                chunk_id=f"{DOC_ID}:c0",
                document_id=DOC_ID,
                char_start=0,
                char_end=len(QUOTE),
                text=QUOTE,
                section_label="s. 1",
                page_start=1,
                page_end=1,
            )
        ]
    )
    storage.run_start(
        run_id="run_a", kind="run", economy="SG", pillars=[7],
        indicators=None, engine="fake", started_at=utc_now_iso(),
    )
    storage.upsert_mappings(
        [
            MappingRecord(
                mapping_id=f"{DOC_ID}:c0::7.1",
                document_id=DOC_ID,
                chunk_id=f"{DOC_ID}:c0",
                economy="SG",
                indicator_id="7.1",
                indicator_name="Indicator 7.1",
                section="s. 1",
                verbatim_quote=QUOTE,
                page_number=3,
                verification_status="passed",
                controlling_evidence=True,
            )
        ],
        run_id="run_a",
    )
    storage.run_finish("run_a", status="completed", ended_at=utc_now_iso())
    storage.close()
    return db


def _seed_stored_document(
    storage: Storage, document_id: str, local_path: Path, extractor: str, run_id: str
) -> str:
    """One Document whose bytes really are on disk, with one passed Mapping so
    the audit lane will serve it. Returns its mapping id."""
    storage.upsert_document(
        document_id,
        "SG",
        f"sha_{document_id}",
        full_text=QUOTE,
        title=document_id,
        n_pages=1,
        language="en",
        source_url="",
        extractor=extractor,
        local_path=str(local_path),
    )
    storage.upsert_chunks(
        [
            Chunk(
                chunk_id=f"{document_id}:c0",
                document_id=document_id,
                char_start=0,
                char_end=len(QUOTE),
                text=QUOTE,
                section_label="s. 1",
                page_start=1,
                page_end=1,
            )
        ]
    )
    record = MappingRecord(
        mapping_id=f"{document_id}:c0::7.1",
        document_id=document_id,
        chunk_id=f"{document_id}:c0",
        economy="SG",
        indicator_id="7.1",
        indicator_name="Indicator 7.1",
        section="s. 1",
        verbatim_quote=QUOTE,
        page_number=1,
        verification_status="passed",
        controlling_evidence=True,
    )
    storage.upsert_mappings([record], run_id=run_id)
    return record.mapping_id


@pytest.fixture()
def stored_files_db(tmp_path) -> Path:
    """A working database holding three uploads that are NOT all PDFs: the
    stored-file endpoint is what the local-copy link opens, so what it says
    they are has to be true."""
    files = tmp_path / "raw"
    files.mkdir()
    (files / "act.pdf").write_bytes(b"%PDF-1.7\n%stub\n")
    (files / "act.html").write_text("<html><body>An Act</body></html>", encoding="utf-8")
    (files / "act.docx").write_bytes(b"PK\x03\x04stub")

    db = tmp_path / "regcompass.db"
    storage = Storage(db)
    storage.apply_schema()
    storage.run_start(
        run_id="run_files", kind="run", economy="SG", pillars=[7],
        indicators=None, engine="fake", started_at=utc_now_iso(),
    )
    _seed_stored_document(storage, "doc_pdf", files / "act.pdf", "pdfplumber", "run_files")
    _seed_stored_document(storage, "doc_html", files / "act.html", "bs4-lxml", "run_files")
    _seed_stored_document(storage, "doc_docx", files / "act.docx", "bs4-lxml", "run_files")
    storage.run_finish("run_files", status="completed", ended_at=utc_now_iso())
    storage.close()
    return db


class TestTheStoredFileIsServedAsWhatItIs:
    def _client(self, db, tmp_path) -> TestClient:
        return TestClient(
            create_app(
                db_path=db, out_dir=tmp_path / "out",
                data_dir=tmp_path / "data", ui_dir=None,
            )
        )

    def test_a_pdf_upload_is_still_a_pdf(self, stored_files_db, tmp_path):
        r = self._client(stored_files_db, tmp_path).get("/api/documents/doc_pdf/pdf")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/pdf"

    def test_an_html_upload_is_served_as_html(self, stored_files_db, tmp_path):
        """Before this, an HTML upload opened through the local-copy link was
        announced as a PDF and rendered as a broken document."""
        r = self._client(stored_files_db, tmp_path).get("/api/documents/doc_html/pdf")
        assert r.status_code == 200
        assert r.headers["content-type"] == "text/html; charset=utf-8"
        assert "An Act" in r.text

    def test_a_stored_web_page_cannot_run_its_scripts(self, stored_files_db, tmp_path):
        """A Portal's page is served from this app's own address, so its
        scripts would run with this app's login. The sandbox header stops that."""
        r = self._client(stored_files_db, tmp_path).get("/api/documents/doc_html/pdf")
        assert r.headers["content-security-policy"] == "sandbox"
        assert r.headers["x-content-type-options"] == "nosniff"

    def test_a_pdf_is_not_sandboxed(self, stored_files_db, tmp_path):
        """The browser's own PDF viewer is what opens the local copy."""
        r = self._client(stored_files_db, tmp_path).get("/api/documents/doc_pdf/pdf")
        assert "content-security-policy" not in r.headers
        assert r.headers["x-content-type-options"] == "nosniff"

    def test_a_word_upload_is_served_as_a_word_document(self, stored_files_db, tmp_path):
        r = self._client(stored_files_db, tmp_path).get("/api/documents/doc_docx/pdf")
        assert r.status_code == 200
        assert r.headers["content-type"] == (
            "application/vnd.openxmlformats-officedocument"
            ".wordprocessingml.document"
        )

    def test_the_local_link_and_the_endpoint_agree(self, stored_files_db, tmp_path):
        """The row's own link is what a reviewer clicks, so it must be the path
        that answers with the right type."""
        client = self._client(stored_files_db, tmp_path)
        rows = client.get(
            "/api/documents/doc_html/records", params={"run_id": "run_files"}
        ).json()
        link = rows[0]["source_link"]
        assert link["kind"] == "local"
        assert client.get(link["href"]).headers["content-type"] == "text/html; charset=utf-8"


class TestTheRowCarriesItsSource:
    def test_a_database_row_links_out_at_the_cited_page(self, seeded_db, tmp_path):
        client = TestClient(
            create_app(
                db_path=seeded_db, out_dir=tmp_path / "out",
                data_dir=tmp_path / "data", ui_dir=None,
            )
        )
        rows = client.get(
            f"/api/documents/{DOC_ID}/records", params={"run_id": "run_a"}
        ).json()
        assert len(rows) == 1
        row = rows[0]
        assert row["source_url"] == SSO_URL
        assert row["location_reference"] == "PDF: page 3"
        assert row["source_link"] == {
            "href": f"{SSO_URL}#page=3",
            "kind": "official",
            "page": 3,
        }

    def test_a_bundle_row_falls_back_to_the_stored_copy(self, tmp_path):
        """The frozen bundle's manifest records no Source URL for a Document
        unless the builder put one there, and a row with none opens the copy
        the app is serving, labelled as such."""
        from test_audit import SLUG_SG, write_manifest

        manifest = write_manifest(tmp_path, [SLUG_SG])
        client = TestClient(
            create_app(
                db_path=tmp_path / "unused.db", out_dir=tmp_path / "out",
                data_dir=tmp_path / "data", bundle_manifest=manifest, ui_dir=None,
            )
        )
        rows = client.get(f"/api/documents/doc_{SLUG_SG}/records").json()
        assert rows
        link = rows[0]["source_link"]
        assert link["kind"] == "local"
        assert link["href"].startswith(f"/api/documents/doc_{SLUG_SG}/pdf")
        # the stored-file endpoint the href names really serves the file
        assert client.get(f"/api/documents/doc_{SLUG_SG}/pdf").status_code == 200

    def test_a_bundle_row_links_out_when_the_manifest_carries_the_url(self, tmp_path):
        from test_audit import SLUG_SG, write_manifest

        path = write_manifest(tmp_path, [SLUG_SG])
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["documents"][0]["source_url"] = SSO_URL
        path.write_text(json.dumps(manifest), encoding="utf-8")
        client = TestClient(
            create_app(
                db_path=tmp_path / "unused.db", out_dir=tmp_path / "out",
                data_dir=tmp_path / "data", bundle_manifest=path, ui_dir=None,
            )
        )
        rows = client.get(f"/api/documents/doc_{SLUG_SG}/records").json()
        assert rows[0]["source_link"]["kind"] == "official"
        assert rows[0]["source_link"]["href"].startswith(SSO_URL)


class TestTheCommittedBundleCarriesPortalAddresses:
    def test_every_fixture_document_names_its_official_source(self):
        """audit_bundle/manifest.json is what a desk reviewer runs with
        `regcompass serve --bundle audit_bundle`; its rows must be followable
        to the Portal, not only to the copy on disk."""
        manifest = json.loads(
            (ROOT / "audit_bundle" / "manifest.json").read_text(encoding="utf-8")
        )
        for doc in manifest["documents"]:
            assert doc.get("source_url", "").startswith("https://"), doc["document_id"]


# ---------------------------------------------------------------------------
# the Comparison screen's rows carry the same link
# ---------------------------------------------------------------------------


def _run(run_id: str, engine: str) -> RunRecord:
    return RunRecord(
        run_id=run_id, kind="run", economy="SG", pillars=[7], indicators=None,
        engine=engine, status="completed", started_at=utc_now_iso(),
        ended_at=utc_now_iso(),
    )


def _record(mapping_id: str, page: int) -> MappingRecord:
    return MappingRecord(
        mapping_id=mapping_id,
        document_id=DOC_ID,
        chunk_id=f"{DOC_ID}:c0",
        economy="SG",
        indicator_id="7.1",
        indicator_name="Indicator 7.1",
        section="s. 1",
        verbatim_quote=QUOTE,
        page_number=page,
        verification_status="passed",
        controlling_evidence=True,
    )


class TestComparisonSidesLinkOut:
    def test_each_side_carries_its_own_source_link(self):
        comparison = compare_runs(
            _run("run_a", "engine_a"),
            _run("run_b", "engine_b"),
            [_record("m_a", 3)],
            [_record("m_b", 9)],
            {"7.1": "Indicator 7.1"},
            document_sources={DOC_ID: {"source_url": SSO_URL, "extractor": "pdfplumber"}},
        )
        row = comparison.rows[0]
        assert row.a.location_reference == "PDF: page 3"
        assert row.a.source_link.href == f"{SSO_URL}#page=3"
        assert row.b.source_link.href == f"{SSO_URL}#page=9"

    def test_without_document_sources_a_side_opens_the_stored_copy(self):
        comparison = compare_runs(
            _run("run_a", "engine_a"),
            _run("run_b", "engine_b"),
            [_record("m_a", 3)],
            [],
            {"7.1": "Indicator 7.1"},
        )
        link = comparison.rows[0].a.source_link
        assert link.kind == "local"
        assert link.href == f"/api/documents/{DOC_ID}/pdf#page=3"


class TestTheAuditViewKnowsWhatItIsShowing:
    """The audit view's left pane draws PDFs. A web page fed to it fails with
    the renderer's own error, so the record detail says what the stored file
    is and, when it is not a PDF, carries the text to show instead."""

    def _detail(self, db, tmp_path, document_id: str) -> dict:
        client = TestClient(
            create_app(
                db_path=db, out_dir=tmp_path / "out",
                data_dir=tmp_path / "data", ui_dir=None,
            )
        )
        r = client.get(
            f"/api/records/{document_id}:c0::7.1", params={"run_id": "run_files"}
        )
        assert r.status_code == 200
        return r.json()

    def test_a_pdf_is_drawn_as_before(self, stored_files_db, tmp_path):
        d = self._detail(stored_files_db, tmp_path, "doc_pdf")
        assert d["source_format"] == "pdf"
        assert d["source_text"] is None

    def test_each_row_says_whether_it_has_pages(self, stored_files_db, tmp_path):
        """An HTML quote is stored as page 1; the row's format is what lets a
        screen avoid calling that a PDF page."""
        client = TestClient(
            create_app(
                db_path=stored_files_db, out_dir=tmp_path / "out",
                data_dir=tmp_path / "data", ui_dir=None,
            )
        )
        for document_id, fmt in (("doc_pdf", "pdf"), ("doc_html", "html")):
            rows = client.get(
                f"/api/documents/{document_id}/records", params={"run_id": "run_files"}
            ).json()
            assert rows[0]["format"] == fmt

    def test_a_web_page_carries_its_text(self, stored_files_db, tmp_path):
        d = self._detail(stored_files_db, tmp_path, "doc_html")
        assert d["source_format"] == "html"
        assert QUOTE in d["source_text"]

    def test_another_kind_of_file_carries_its_text(self, stored_files_db, tmp_path):
        d = self._detail(stored_files_db, tmp_path, "doc_docx")
        assert d["source_format"] == "other"
        assert QUOTE in d["source_text"]

    def test_a_missing_file_still_shows_the_text(self, stored_files_db, tmp_path):
        (tmp_path / "raw" / "act.pdf").unlink()
        d = self._detail(stored_files_db, tmp_path, "doc_pdf")
        assert d["source_format"] == "missing"
        assert QUOTE in d["source_text"]
