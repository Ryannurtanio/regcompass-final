"""Adding a Document, and the lists and files built from what is added, tell
the truth.

From the 28 Sep audits: two Documents fetched from one Portal's generic URLs
("/pdf/0", "/content") collided on one id; a busy afternoon of adds pushed
real Runs off the newest-50 Run list; a Pillar 6 and 7 Run was paired with a
Pillar 6 Run and the Comparison refused; an Export of a Run with nothing
accepted spoke for Pillars it never searched; a Word file entered the Corpus
as zip bytes and an error page as a statute; the only way out of a bad add was
clearing the whole Economy; Set Source URL refused an address an upload may
share; titles arrived as "eLectrOnIc cOMMerce Act 2006" or with the site's
name on; a refused add read "Stopped"; and an English list of devices blocked
the India export as non-English. Nothing here touches the network.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_add_document import (  # noqa: E402
    MY_PDF,
    SG_PDF,
    SpyLimiter,
    recorded_fetch,
    upload,
)
from test_compare import MAPPINGS_A, a_run, _finish, paired_db  # noqa: E402,F401

from regcompass.corpus import (  # noqa: E402
    TooLittleTextError,
    UnreadableFileError,
    add_document,
    add_document_from_url,
    clean_added_title,
    normalise_case,
    readable_kind,
    strip_site_name,
)
from regcompass.export import looks_non_english  # noqa: E402
from regcompass.server import create_app  # noqa: E402
from regcompass.storage import Storage  # noqa: E402

AU_URL_1 = "https://www.legislation.gov.au/C2004A01214/2016-03-10/2016-03-10/text/original/pdf/0"
AU_URL_2 = "https://www.legislation.gov.au/C2006A00088/2021-09-01/2021-09-01/text/original/pdf/0"
IN_URL_1 = "https://indiacode.gov.in/server/api/core/bitstreams/971e6b80-4222-4d05-b548-5d845b126b90/content"
IN_URL_2 = "https://indiacode.gov.in/server/api/core/bitstreams/770a7d02-48f2-4274-bab9-10d0f48ee264/content"


@pytest.fixture()
def storage(tmp_path):
    st = Storage(tmp_path / "add.db")
    st.apply_schema()
    yield st
    st.close()


@pytest.fixture()
def app_client(tmp_path):
    db = tmp_path / "server.db"
    st = Storage(db)
    st.apply_schema()
    st.conn.close()
    out = tmp_path / "out"
    out.mkdir()
    app = create_app(db_path=db, out_dir=out, data_dir=tmp_path / "data", ui_dir=None)
    return TestClient(app), db, tmp_path / "data"


def _from_url(storage, tmp_path, economy, url, body, **kw):
    return add_document_from_url(
        storage, tmp_path / "data", economy, url, language="English",
        fetch=recorded_fetch({url: body}), limiter=SpyLimiter(), **kw,
    )


def _post_file(client, economy, name, body, **data):
    return client.post(
        "/api/documents/upload",
        data={"economy": economy, "language": "English", **data},
        files={"file": (name, body, "application/octet-stream")},
    )


# ---------------------------------------------------------------------------
# 1. URL adds from one Portal's generic addresses get ids of their own
# ---------------------------------------------------------------------------


class TestUrlIds:
    def test_two_adds_ending_in_pdf_0_both_enter_the_corpus(self, storage, tmp_path):
        first = _from_url(storage, tmp_path, "AU", AU_URL_1, MY_PDF.read_bytes())
        second = _from_url(storage, tmp_path, "AU", AU_URL_2, SG_PDF.read_bytes())
        assert first.document_id != second.document_id
        # the register number in the address, not the Portal's "0"
        assert first.document_id == "doc_au_C2004A01214"
        assert second.document_id == "doc_au_C2006A00088"
        assert len(storage.corpus_documents("AU")) == 2

    def test_two_adds_ending_in_content_both_enter_the_corpus(self, storage, tmp_path):
        first = _from_url(storage, tmp_path, "IN", IN_URL_1, MY_PDF.read_bytes())
        second = _from_url(storage, tmp_path, "IN", IN_URL_2, SG_PDF.read_bytes())
        assert first.document_id != second.document_id
        assert not first.document_id.endswith("_content")
        assert len(storage.corpus_documents("IN")) == 2

    def test_a_typed_law_name_names_a_generic_address(self, storage, tmp_path):
        added = _from_url(
            storage, tmp_path, "IN", IN_URL_1, MY_PDF.read_bytes(),
            title="Indian Telegraph Act 1885",
        )
        assert added.document_id == "doc_in_Indian_Telegraph_Act_1885"

    def test_an_address_that_names_the_law_keeps_the_id_it_always_had(
        self, storage, tmp_path
    ):
        url = "https://lom.agc.gov.my/ilims/upload/portal/akta/outputaktap/Act%20709%20ori.pdf"
        added = _from_url(storage, tmp_path, "MY", url, MY_PDF.read_bytes())
        assert added.document_id == "doc_my_Act_709_ori"

    def test_an_id_already_taken_by_another_address_gains_a_stable_suffix(
        self, storage, tmp_path
    ):
        a = "https://lom.agc.gov.my/one/Act%20709.pdf"
        b = "https://lom.agc.gov.my/two/Act%20709.pdf"
        first = _from_url(storage, tmp_path, "MY", a, MY_PDF.read_bytes())
        second = _from_url(storage, tmp_path, "MY", b, SG_PDF.read_bytes())
        assert first.document_id == "doc_my_Act_709"
        assert second.document_id.startswith("doc_my_Act_709_")
        assert second.document_id != first.document_id


# ---------------------------------------------------------------------------
# 2. the Run list pages, and a kind/status filter reaches every finished Run
# ---------------------------------------------------------------------------


class TestRunLists:
    def _many_adds(self, db, n):
        storage = Storage(db)
        for i in range(n):
            rid = f"disc_20260928T0100{i:02d}Z_{i:06x}"
            storage.run_start(
                run_id=rid, kind="discovery", economy="SG", pillars=[],
                indicators=None, engine=None,
                started_at=f"2026-09-28T02:{i // 60:02d}:{i % 60:02d}Z",
            )
            storage.run_finish(rid, status="completed", ended_at="2026-09-28T03:00:00Z")
        storage.close()

    def test_finished_runs_survive_a_burst_of_adds(self, app_client):
        client, db, _ = app_client
        storage = Storage(db)
        _finish(storage, "run_old", a_run(
            "run_old", "engine-a", economy="SG",
            started_at="2026-09-01T00:00:00Z", ended_at="2026-09-01T00:01:00Z",
        ))
        storage.close()
        self._many_adds(db, 60)

        default = client.get("/api/runs").json()
        assert len(default["runs"]) == 50
        assert default["total"] == 61
        assert "run_old" not in [r["run_id"] for r in default["runs"]]

        finished = client.get(
            "/api/runs", params={"kind": "run", "status": "completed", "limit": 500}
        ).json()
        assert [r["run_id"] for r in finished["runs"]] == ["run_old"]
        assert finished["total"] == 1

        older = client.get("/api/runs", params={"offset": 50}).json()
        assert "run_old" in [r["run_id"] for r in older["runs"]]
        assert len(older["runs"]) == 11


# ---------------------------------------------------------------------------
# 3. Comparison pairs Runs over the same Pillar set
# ---------------------------------------------------------------------------


class TestPairing:
    def test_a_two_pillar_run_pairs_with_a_two_pillar_run(self, paired_db):
        client, path = paired_db
        storage = Storage(path)
        # Engine A: a newer Run over Pillars 6 and 7. Engine B: only Pillar 7
        # (run_b), and an older Run over 6 and 7.
        _finish(storage, "run_a67", a_run(
            "run_a67", "engine-a", pillars=[6, 7],
            started_at="2026-09-05T00:00:00Z", ended_at="2026-09-05T00:01:00Z",
        ))
        _finish(storage, "run_b67", a_run(
            "run_b67", "engine-b", pillars=[6, 7],
            started_at="2026-08-30T00:00:00Z", ended_at="2026-08-30T00:01:00Z",
        ))
        storage.upsert_mappings(MAPPINGS_A, run_id="run_a67")
        storage.close()
        r = client.get("/api/compare", params={"economy": "SG", "pillar": 7})
        assert r.status_code == 200, r.text
        body = r.json()
        assert {body["run_a"]["record"]["run_id"], body["run_b"]["record"]["run_id"]} == {
            "run_a67", "run_b67",
        }
        # A Pillar 7 Comparison lists Pillar 7 Indicators only.
        assert all(row["indicator_id"].startswith("7.") for row in body["rows"])

    def test_no_shared_pillar_set_is_explained_in_plain_words(self, paired_db):
        client, path = paired_db
        storage = Storage(path)
        storage.conn.execute("DELETE FROM mappings WHERE run_id = 'run_b'")
        storage.conn.execute("DELETE FROM runs WHERE run_id = 'run_b'")
        storage.conn.commit()
        _finish(storage, "run_b67", a_run(
            "run_b67", "engine-b", pillars=[6, 7],
            started_at="2026-08-30T00:00:00Z", ended_at="2026-08-30T00:01:00Z",
        ))
        storage.close()
        r = client.get("/api/compare", params={"economy": "SG", "pillar": 7})
        assert r.status_code == 404
        detail = r.json()["detail"]
        assert "same Pillars" in detail
        assert "Pillar 6 and Pillar 7" in detail and "Pillar 7 on engine-a" in detail


# ---------------------------------------------------------------------------
# 4. the Export speaks for the Run's own Pillars, and its Notes are true
# ---------------------------------------------------------------------------


def _no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a Run must not touch the network")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


class TestExportScope:
    def test_nothing_accepted_writes_only_the_runs_pillars_with_true_notes(
        self, tmp_path, monkeypatch
    ):
        import csv

        from regcompass.engines import fake_completion, fake_embed, resolve_engine
        from regcompass.fixtures import seed_economy
        from regcompass.pipeline import export_from_db, run_economy

        storage = Storage(tmp_path / "run.db")
        storage.apply_schema()
        data_dir = tmp_path / "data"
        seed_economy(storage, data_dir, "SG")
        _no_network(monkeypatch)
        report = run_economy(
            storage, "SG", (7,), resolve_engine("fake"), data_dir=data_dir,
            completion_fn=fake_completion, embed_fn=fake_embed,
        )
        monkeypatch.undo()
        # Another Run's Gate scores on the same database, over Pillar 6.
        chunk = storage.conn.execute("SELECT chunk_id FROM chunks LIMIT 1").fetchone()[0]
        storage.upsert_gate_scores({(chunk, "6.1"): 0.9, (chunk, "6.2"): 0.8})
        ids = storage.unreviewed_mapping_ids(report.run_id)
        assert ids
        for mapping_id in ids:
            storage.review_set(
                run_id=report.run_id, mapping_id=mapping_id, review_status="rejected",
            )
        reviews = {m: r.review_status for m, r in storage.reviews_for_run(report.run_id).items()}
        result = export_from_db(storage, tmp_path / "out", run_id=report.run_id, reviews=reviews)
        storage.close()
        rows = list(csv.DictReader(result.csv_path.open(encoding="utf-8-sig")))
        assert rows
        assert all(r["Indicator ID"].startswith("7.") for r in rows), [
            r["Indicator ID"] for r in rows
        ]
        withheld = [r for r in rows if "not accepted in review" in r["Notes"]]
        assert withheld, "no row says its evidence was withheld by review"
        for row in withheld:
            assert "none passed the gate" not in row["Notes"]
        assert not any("demo-corpus" in r["Notes"] for r in rows)


# ---------------------------------------------------------------------------
# 5. an add refuses what it cannot read, and pages with almost no text
# ---------------------------------------------------------------------------


class TestContentChecks:
    def test_kinds(self):
        assert readable_kind(b"%PDF-1.7\n...") == "pdf"
        assert readable_kind(b"\xef\xbb\xbf  <!DOCTYPE html><html>") == "html"
        assert readable_kind(b"GOVERNMENT PROCEEDINGS ACT 1956\n1. Short title") == "text"
        for raw in (b"PK\x03\x04docx", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", b"\x00\xff\x00\x01"):
            with pytest.raises(UnreadableFileError):
                readable_kind(raw)

    def test_a_word_file_is_refused_in_plain_words_and_leaves_nothing(self, app_client):
        client, db, data = app_client
        r = _post_file(client, "SG", "SG_POFMA_part.docx", b"PK\x03\x04" + b"x" * 500)
        assert r.status_code == 400
        assert "Word" in r.json()["detail"] and "PDF" in r.json()["detail"]
        storage = Storage(db)
        assert storage.corpus_documents("SG") == []
        [record] = storage.runs_list(kind="discovery")
        storage.close()
        assert record["details"]["outcome"] == "refused"
        raw = data / "SG" / "raw"
        assert not raw.exists() or not list(raw.iterdir()), "refused bytes were stored"

    def test_an_error_page_is_refused_and_undone(self, storage, tmp_path):
        url = "https://lom.agc.gov.my/act-detail.php?act=658&lang=BI"
        with pytest.raises(TooLittleTextError) as err:
            _from_url(storage, tmp_path, "MY", url, b"<html><body>Invalid request</body></html>")
        assert "almost no text" in str(err.value)
        assert storage.corpus_documents("MY") == []
        assert storage.conn.execute("SELECT COUNT(*) FROM crawl_manifest").fetchone()[0] == 0
        assert storage.conn.execute("SELECT COUNT(*) FROM extractions").fetchone()[0] == 0
        raw = tmp_path / "data" / "MY" / "raw"
        assert not raw.exists() or not list(raw.iterdir())

    def test_a_pdf_with_almost_no_text_is_kept_with_a_warning(
        self, storage, tmp_path, monkeypatch
    ):
        """A scan OCR read badly is still that law, so a thin PDF is not
        refused: the add says plainly that a Run will find little in it."""
        import regcompass.shortlist as shortlist_mod

        real = shortlist_mod.extract_with_stats

        def thin(raw, fmt, doc_id, *a, **k):
            canonical, stats = real(raw, fmt, doc_id, *a, **k)
            text = "Act 709"
            return canonical.model_copy(update={
                "full_text": text,
                "pages": [canonical.pages[0].model_copy(update={"char_start": 0, "char_end": len(text)})],
                "words": [],
            }), stats

        monkeypatch.setattr(shortlist_mod, "extract_with_stats", thin)
        monkeypatch.setattr(shortlist_mod, "should_ocr", lambda canonical: False)
        added = add_document(
            storage, tmp_path / "data", "MY", MY_PDF.read_bytes(), source_url=None,
            filename_hint="scan.pdf",
        )
        assert added.warning and "almost no text" in added.warning
        assert len(storage.corpus_documents("MY")) == 1

    def test_a_plain_text_statute_is_still_accepted(self, storage, tmp_path):
        text = ("GOVERNMENT PROCEEDINGS ACT 1956\n" + "1. This Act may be cited. " * 40).encode()
        added = add_document(
            storage, tmp_path / "data", "MY", text, source_url=None,
            filename_hint="MY_Government Proceedings Act 1956.txt",
        )
        assert added.title == "Government Proceedings Act 1956"


# ---------------------------------------------------------------------------
# 6. remove one Document
# ---------------------------------------------------------------------------


class TestRemoveOne:
    def test_preview_then_remove_takes_only_that_document(self, app_client):
        client, db, data = app_client
        keep = upload(client, "MY", MY_PDF, url=None, language="English").json()
        drop = upload(client, "MY", SG_PDF, url=None, language="English").json()
        preview = client.get(f"/api/documents/{drop['document_id']}/removal")
        assert preview.status_code == 200, preview.text
        body = preview.json()
        assert body["mappings"] == 0
        assert "will be removed" in body["summary"] and "No Run has mapped it" in body["summary"]
        # a preview removes nothing
        assert client.get("/api/corpus", params={"economy": "MY"}).json()["n"] == 2

        gone = client.delete(f"/api/documents/{drop['document_id']}")
        assert gone.status_code == 200, gone.text
        assert gone.json()["corpus_documents"] == 1
        listing = client.get("/api/corpus", params={"economy": "MY"}).json()
        assert [d["document_id"] for d in listing["documents"]] == [keep["document_id"]]
        storage = Storage(db)
        sha_rows = storage.conn.execute("SELECT COUNT(*) FROM crawl_manifest").fetchone()[0]
        storage.close()
        assert sha_rows == 1
        # the same file can be added again afterwards
        again = upload(client, "MY", SG_PDF, url=None, language="English")
        assert again.status_code == 200, again.text

    def test_mappings_in_past_runs_go_with_it_and_the_run_record_stays(self, paired_db):
        client, path = paired_db
        preview = client.get("/api/documents/doc_pdpa/removal").json()
        assert preview["mappings"] > 0 and preview["runs"] == 2
        assert "Run Records themselves stay" in preview["summary"]
        assert client.delete("/api/documents/doc_pdpa").status_code == 200
        storage = Storage(path)
        assert storage.conn.execute(
            "SELECT COUNT(*) FROM mappings WHERE document_id = 'doc_pdpa'"
        ).fetchone()[0] == 0
        assert storage.run_get("run_a") is not None
        assert storage.conn.execute(
            "SELECT COUNT(*) FROM mappings WHERE document_id = 'doc_cyber'"
        ).fetchone()[0] > 0
        storage.close()

    def test_an_unknown_id_is_404_and_a_running_job_blocks(self, app_client, monkeypatch):
        client, _db, _ = app_client
        added = upload(client, "MY", MY_PDF, url=None, language="English").json()
        assert client.delete("/api/documents/doc_not_here").status_code == 404
        import regcompass.server as server_mod

        monkeypatch.setattr(server_mod.RunManager, "status", lambda self: {"active": True})
        refused = client.delete(f"/api/documents/{added['document_id']}")
        assert refused.status_code == 409


# ---------------------------------------------------------------------------
# 7. Set Source URL may share an address, as upload may
# ---------------------------------------------------------------------------


class TestSharedSourceUrl:
    def test_a_scan_can_take_its_laws_official_address(self, app_client):
        client, db, _ = app_client
        official = "https://lom.agc.gov.my/ilims/upload/portal/akta/LOM/EN/Act%20709.pdf"
        first = upload(client, "MY", MY_PDF, url=official, language="English")
        assert first.status_code == 200
        scan = upload(client, "MY", SG_PDF, url=None, language="English").json()
        r = client.patch(f"/api/documents/{scan['document_id']}", json={"source_url": official})
        assert r.status_code == 200, r.text
        storage = Storage(db)
        urls = [
            row["source_url"]
            for row in storage.conn.execute("SELECT source_url FROM documents")
        ]
        keys = [row["url"] for row in storage.conn.execute("SELECT url FROM crawl_manifest")]
        storage.close()
        assert urls == [official, official]
        assert len(keys) == len(set(keys)) == 2


# ---------------------------------------------------------------------------
# 8. Law Names for adds
# ---------------------------------------------------------------------------


class TestLawNames:
    @pytest.mark.parametrize(
        "given,expected",
        [
            ("eLectrOnIc cOMMerce Act 2006", "Electronic Commerce Act 2006"),
            ("MANIPULATION ACT 2019", "Manipulation Act 2019"),
            (
                "COMMUNICATIONS AND MULTIMEDIA (AMENDMENT) ACT 2025",
                "Communications and Multimedia (Amendment) Act 2025",
            ),
            ("Personal Data Protection Act 2012", "Personal Data Protection Act 2012"),
            ("UU Nomor 35 Tahun 2014", "UU Nomor 35 Tahun 2014"),
            ("网络数据安全管理条例", "网络数据安全管理条例"),
        ],
    )
    def test_case(self, given, expected):
        assert normalise_case(given) == expected

    def test_site_names_go(self):
        assert strip_site_name("未成年人网络保护条例_中央网络安全和信息化委员会办公室") == "未成年人网络保护条例"
        assert strip_site_name("Spam Act 2003 - Federal Register of Legislation") == "Spam Act 2003"
        assert strip_site_name("India Code") == "India Code"

    def test_a_file_name_title_loses_the_economy_prefix(self):
        assert clean_added_title(
            "ID UU Nomor 35 Tahun 2014", raw=b"%PDF-", kind="pdf", economy="ID",
            hint="ID_UU_Nomor_35_Tahun_2014.pdf",
        ) == "UU Nomor 35 Tahun 2014"

    def test_a_statute_heading_keeps_its_dash(self):
        assert clean_added_title(
            "Act A1743 - Amendment Act 2025", raw=b"%PDF-", kind="pdf", economy="MY",
            hint="x.pdf",
        ) == "Act A1743 - Amendment Act 2025"

    def test_an_html_add_stores_the_clean_name(self, storage, tmp_path):
        body = (
            "<html><head><title>网络数据安全管理条例_中央网络安全和信息化委员会办公室</title></head>"
            "<body><p>" + "第一条 为了规范网络数据处理活动，保障网络数据安全。" * 20 + "</p></body></html>"
        ).encode("utf-8")
        url = "https://www.cac.gov.cn/2024-09/30/c_1729384452307680.htm"
        added = add_document_from_url(
            storage, tmp_path / "data", "CN", url, language="Chinese",
            fetch=recorded_fetch({url: body}), limiter=SpyLimiter(),
        )
        assert added.title == "网络数据安全管理条例"
        row = storage.conn.execute(
            "SELECT title FROM documents WHERE document_id = ?", (added.document_id,)
        ).fetchone()
        assert row["title"] == "网络数据安全管理条例"

    def test_a_typed_name_is_kept_exactly(self, app_client):
        client, _db, _ = app_client
        r = upload(client, "MY", MY_PDF, url=None, language="English", title="PDPA 2010 (as typed)")
        assert r.json()["title"] == "PDPA 2010 (as typed)"


# ---------------------------------------------------------------------------
# 9. a refused add is recorded as refused
# ---------------------------------------------------------------------------


class TestRefusedRecord:
    def test_a_duplicate_under_another_address_is_recorded_refused(self, app_client):
        client, db, _ = app_client
        assert upload(client, "MY", MY_PDF, url="https://lom.agc.gov.my/a.pdf", language="English").status_code == 200
        assert upload(client, "MY", MY_PDF, url="https://lom.agc.gov.my/b.pdf", language="English").status_code == 409
        storage = Storage(db)
        records = storage.runs_list(kind="discovery")
        storage.close()
        failed = [r for r in records if r["status"] == "failed"]
        assert failed and failed[0]["details"]["outcome"] == "refused"
        assert failed[0]["ended_at"] >= failed[0]["started_at"]


# ---------------------------------------------------------------------------
# 10. an English list is not a foreign sentence
# ---------------------------------------------------------------------------


class TestLanguageProbe:
    def test_the_it_act_device_list_reads_as_english(self):
        assert not looks_non_english("digital audio, digital video, cell phones, digital fax machines.]")

    def test_foreign_sentences_are_still_flagged(self):
        assert looks_non_english(
            "Setiap orang berhak memperoleh informasi publik sesuai dengan ketentuan undang-undang ini"
        )
        assert looks_non_english(
            "Menteri boleh, melalui perintah yang disiarkan dalam Warta, menetapkan apa-apa"
        )
        assert looks_non_english("网络数据安全管理条例第一条")
