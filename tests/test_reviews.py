"""Review Decisions that survive restarts.

A Review Decision (accept, reject or flag, with an optional note) belongs to
ONE Mapping of ONE Run and lives in the working database, keyed by
(run_id, mapping_id). It therefore survives a page refresh, a server restart
and a second Run over the same Economy, and only accepted Mappings enter the
Evidence Export.

Everything here runs offline against a hand-seeded database: no Engine, no
network, no model call. The Runs are seeded rather than executed because what
is under test is the store and its API surface, not the pipeline.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from regcompass.contracts import Chunk, MappingRecord
from regcompass.server import create_app
from regcompass.storage import LEGACY_RUN_ID, Storage, utc_now_iso

ROOT = Path(__file__).resolve().parents[1]

DOC_ID = "doc_seeded_act"
QUOTES = [
    "The Authority may license any person to provide a telecommunication service.",
    "A licensee shall not discriminate between customers of the same class.",
    "The Minister may make regulations for the purposes of this Part.",
    "Every licensee shall publish its standard terms of service.",
]
FULL_TEXT = "\n\n".join(QUOTES)


def _seed_document(storage: Storage) -> None:
    storage.upsert_document(
        DOC_ID,
        "SG",
        "sha_seeded",
        full_text=FULL_TEXT,
        title="Seeded Act 1999",
        n_pages=1,
        language="en",
    )
    chunks = []
    offset = 0
    for i, quote in enumerate(QUOTES):
        start = FULL_TEXT.index(quote)
        end = start + len(quote)
        chunks.append(
            Chunk(
                chunk_id=f"{DOC_ID}:c{i}",
                document_id=DOC_ID,
                char_start=start,
                char_end=end,
                text=FULL_TEXT[start:end],
                section_label=f"s. {i + 1}",
                page_start=1,
                page_end=1,
            )
        )
        offset = end
    assert offset
    storage.upsert_chunks(chunks)


def _seed_run(storage: Storage, run_id: str, *, n: int = 4) -> list[str]:
    """One completed Run Record over the seeded Document, with n passed
    Mappings. Returns their mapping ids in order."""
    storage.run_start(
        run_id=run_id, kind="run", economy="SG", pillars=[7],
        indicators=None, engine="fake", started_at=utc_now_iso(),
    )
    records = []
    for i in range(n):
        records.append(
            MappingRecord(
                mapping_id=f"{DOC_ID}:c{i}::7.{i + 1}",
                document_id=DOC_ID,
                chunk_id=f"{DOC_ID}:c{i}",
                economy="SG",
                indicator_id=f"7.{i + 1}",
                indicator_name=f"Indicator 7.{i + 1}",
                section=f"s. {i + 1}",
                verbatim_quote=QUOTES[i],
                page_number=1,
                verification_status="passed",
                controlling_evidence=True,
            )
        )
    storage.upsert_mappings(records, run_id=run_id)
    storage.run_finish(run_id, status="completed", ended_at=utc_now_iso())
    return [r.mapping_id for r in records]


@pytest.fixture()
def seeded(tmp_path):
    """(db path, run id, mapping ids) for one seeded Run."""
    db = tmp_path / "regcompass.db"
    storage = Storage(db)
    storage.apply_schema()
    _seed_document(storage)
    ids = _seed_run(storage, "run_a")
    storage.close()
    return db, "run_a", ids


def _app(db, tmp_path, **kwargs):
    return create_app(
        db_path=db, out_dir=tmp_path / "out", data_dir=tmp_path / "data",
        ui_dir=None, **kwargs,
    )


def _post(client, run_id, mapping_id, status, comment=None):
    return client.post(
        "/api/reviews",
        json={
            "run_id": run_id, "mapping_id": mapping_id,
            "review_status": status, "comment": comment,
        },
    )


# ---------------------------------------------------------------------------
# Box 1: a decision with a note, read back, then read back after a restart
# ---------------------------------------------------------------------------


class TestADecisionSurvivesARestart:
    def test_posted_read_back_and_still_there_after_a_restart(self, seeded, tmp_path):
        db, run_id, ids = seeded
        client = TestClient(_app(db, tmp_path))
        posted = _post(client, run_id, ids[0], "accepted", "quote checked against s. 1")
        assert posted.status_code == 200, posted.text
        body = posted.json()
        assert body["run_id"] == run_id
        assert body["mapping_id"] == ids[0]
        assert body["comment"] == "quote checked against s. 1"

        listed = client.get("/api/reviews", params={"run_id": run_id}).json()["reviews"]
        assert [(r["mapping_id"], r["review_status"]) for r in listed] == [
            (ids[0], "accepted")
        ]
        detail = client.get(f"/api/records/{ids[0]}", params={"run_id": run_id}).json()
        assert detail["review"]["review_status"] == "accepted"
        assert detail["review"]["comment"] == "quote checked against s. 1"

        # The restart: a NEW app over the SAME database file, as a rebooted
        # server would be. Nothing is carried over in memory.
        del client
        restarted = TestClient(_app(db, tmp_path))
        again = restarted.get("/api/reviews", params={"run_id": run_id}).json()["reviews"]
        assert [(r["mapping_id"], r["review_status"], r["comment"]) for r in again] == [
            (ids[0], "accepted", "quote checked against s. 1")
        ]
        detail = restarted.get(f"/api/records/{ids[0]}", params={"run_id": run_id}).json()
        assert detail["review"]["review_status"] == "accepted"

    def test_the_records_list_carries_the_decision_and_its_note(self, seeded, tmp_path):
        db, run_id, ids = seeded
        client = TestClient(_app(db, tmp_path))
        assert _post(client, run_id, ids[1], "flagged", "check the amendment").status_code == 200
        rows = client.get(
            f"/api/documents/{DOC_ID}/records", params={"run_id": run_id}
        ).json()
        by_id = {r["mapping_id"]: r for r in rows}
        assert by_id[ids[1]]["review_status"] == "flagged"
        assert by_id[ids[1]]["review_note"] == "check the amendment"
        assert by_id[ids[0]]["review_status"] is None
        assert by_id[ids[0]]["review_note"] is None

    def test_an_unknown_mapping_is_a_404(self, seeded, tmp_path):
        db, run_id, _ = seeded
        client = TestClient(_app(db, tmp_path))
        assert _post(client, run_id, "no_such_mapping", "accepted").status_code == 404


# ---------------------------------------------------------------------------
# Box 2: a second decision replaces the first and keeps the later time
# ---------------------------------------------------------------------------


class TestASecondDecisionReplacesTheFirst:
    def test_replaced_in_place_with_the_later_time(self, seeded, tmp_path):
        db, run_id, ids = seeded
        client = TestClient(_app(db, tmp_path))
        first = _post(client, run_id, ids[0], "accepted", "looks right").json()
        second = _post(client, run_id, ids[0], "rejected", "second thoughts").json()

        assert second["review_status"] == "rejected"
        assert second["comment"] == "second thoughts"
        assert second["reviewed_at"] >= first["reviewed_at"]

        listed = client.get("/api/reviews", params={"run_id": run_id}).json()["reviews"]
        assert len(listed) == 1, "one decision per Mapping, not a history"
        assert listed[0]["review_status"] == "rejected"
        assert listed[0]["reviewed_at"] == second["reviewed_at"]

    def test_the_replacement_survives_a_restart_too(self, seeded, tmp_path):
        db, run_id, ids = seeded
        client = TestClient(_app(db, tmp_path))
        _post(client, run_id, ids[0], "accepted")
        _post(client, run_id, ids[0], "flagged", "ask the team")
        restarted = TestClient(_app(db, tmp_path))
        listed = restarted.get("/api/reviews", params={"run_id": run_id}).json()["reviews"]
        assert [(r["review_status"], r["comment"]) for r in listed] == [
            ("flagged", "ask the team")
        ]


# ---------------------------------------------------------------------------
# Box 3: the export preview counts equal the database
# ---------------------------------------------------------------------------


class TestExportPreview:
    def test_counts_and_accepted_ids_match_the_database(self, seeded, tmp_path):
        db, run_id, ids = seeded
        client = TestClient(_app(db, tmp_path))
        _post(client, run_id, ids[0], "accepted")
        _post(client, run_id, ids[1], "rejected")
        _post(client, run_id, ids[2], "flagged")
        # ids[3] is left unreviewed on purpose.

        preview = client.get("/api/export/preview", params={"run_id": run_id}).json()
        assert preview["run_id"] == run_id
        assert preview["gated"] is True
        assert preview["n_verified"] == 4
        assert preview["n_accepted"] == 1
        assert preview["n_rejected"] == 1
        assert preview["n_flagged"] == 1
        assert preview["n_unreviewed"] == 1
        assert preview["accepted_mapping_ids"] == [ids[0]]

        storage = Storage(db)
        try:
            counts = storage.review_counts(run_id)
        finally:
            storage.close()
        for key in ("n_verified", "n_accepted", "n_rejected", "n_flagged", "n_unreviewed"):
            assert counts[key] == preview[key], key

    def test_an_untouched_run_previews_as_all_unreviewed(self, seeded, tmp_path):
        db, run_id, ids = seeded
        client = TestClient(_app(db, tmp_path))
        preview = client.get("/api/export/preview", params={"run_id": run_id}).json()
        assert (preview["n_verified"], preview["n_unreviewed"]) == (len(ids), len(ids))
        assert preview["n_accepted"] == 0
        assert preview["accepted_mapping_ids"] == []

    def test_accept_all_unreviewed_fills_the_gap_without_touching_decisions(
        self, seeded, tmp_path
    ):
        db, run_id, ids = seeded
        client = TestClient(_app(db, tmp_path))
        _post(client, run_id, ids[1], "rejected", "off topic")
        bulk = client.post("/api/reviews/accept-all", json={"run_id": run_id})
        assert bulk.status_code == 200, bulk.text
        assert bulk.json()["n_newly_accepted"] == 3

        preview = client.get("/api/export/preview", params={"run_id": run_id}).json()
        assert preview["n_accepted"] == 3
        assert preview["n_rejected"] == 1
        assert preview["n_unreviewed"] == 0
        assert ids[1] not in preview["accepted_mapping_ids"]


# ---------------------------------------------------------------------------
# The composite key: a decision belongs to ONE Run
# ---------------------------------------------------------------------------


class TestDecisionsAreScopedToOneRun:
    def test_run_a_s_decision_does_not_appear_on_run_b(self, tmp_path):
        db = tmp_path / "two_runs.db"
        storage = Storage(db)
        storage.apply_schema()
        _seed_document(storage)
        a_ids = _seed_run(storage, "run_a")
        b_ids = _seed_run(storage, "run_b")
        storage.close()
        assert a_ids == b_ids, "both Runs mapped the same provisions"

        client = TestClient(_app(db, tmp_path))
        assert _post(client, "run_a", a_ids[0], "accepted", "Run A only").status_code == 200

        a_listed = client.get("/api/reviews", params={"run_id": "run_a"}).json()["reviews"]
        b_listed = client.get("/api/reviews", params={"run_id": "run_b"}).json()["reviews"]
        assert len(a_listed) == 1 and b_listed == []

        b_rows = client.get(
            f"/api/documents/{DOC_ID}/records", params={"run_id": "run_b"}
        ).json()
        assert all(r["review_status"] is None for r in b_rows)
        b_preview = client.get(
            "/api/export/preview", params={"run_id": "run_b"}
        ).json()
        assert b_preview["n_accepted"] == 0
        assert b_preview["n_unreviewed"] == 4

    def test_a_database_with_mappings_but_no_run_record_still_takes_a_decision(
        self, tmp_path
    ):
        """A database seeded straight from checkpoints, or migrated from before
        Runs existed, has Mappings and no Run Record. The decision must land on
        the run_id those Mappings carry, or the composite foreign key refuses
        it and the reviewer sees a 500."""
        db = tmp_path / "no_run_record.db"
        storage = Storage(db)
        storage.apply_schema()
        _seed_document(storage)
        ids = _seed_run(storage, "run_seeded")
        storage.conn.execute("DELETE FROM runs")
        storage.conn.commit()
        storage.close()

        client = TestClient(_app(db, tmp_path))
        posted = _post(client, None, ids[0], "accepted", "still reviewable")
        assert posted.status_code == 200, posted.text
        assert posted.json()["run_id"] == "run_seeded"

        listed = client.get("/api/reviews").json()
        assert listed["run_id"] == "run_seeded"
        assert [r["mapping_id"] for r in listed["reviews"]] == [ids[0]]
        preview = client.get("/api/export/preview").json()
        assert (preview["n_accepted"], preview["n_unreviewed"]) == (1, 3)
        rows = client.get(f"/api/documents/{DOC_ID}/records").json()
        assert {r["mapping_id"]: r["review_status"] for r in rows}[ids[0]] == "accepted"

    def test_a_decision_on_an_unknown_run_is_a_404(self, seeded, tmp_path):
        db, _, ids = seeded
        client = TestClient(_app(db, tmp_path))
        assert _post(client, "run_never_happened", ids[0], "accepted").status_code == 404


# ---------------------------------------------------------------------------
# Box 5: the old standalone store is gone; the bundle lane says so
# ---------------------------------------------------------------------------


class TestTheOldStoreIsRetired:
    def test_no_source_file_mentions_the_standalone_review_store(self):
        offenders = []
        for path in sorted((ROOT / "src").rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            if "ReviewStore" in text or "reviews.db" in text:
                offenders.append(path.name)
        assert offenders == []

    def test_review_store_cannot_be_imported(self):
        with pytest.raises(ImportError):
            from regcompass.storage import ReviewStore  # noqa: F401

    def test_create_app_takes_no_reviews_db(self):
        import inspect

        assert "reviews_db" not in inspect.signature(create_app).parameters

    def test_bundle_mode_refuses_a_decision_with_409(self, tmp_path):
        manifest = ROOT / "audit_bundle" / "manifest.json"
        if not manifest.is_file():  # pragma: no cover - the bundle is committed
            pytest.skip("no committed audit bundle")
        client = TestClient(
            _app(tmp_path / "unused.db", tmp_path, bundle_manifest=manifest)
        )
        docs = client.get("/api/documents").json()
        recs = client.get(f"/api/documents/{docs[0]['document_id']}/records").json()
        refused = _post(client, None, recs[0]["mapping_id"], "accepted")
        assert refused.status_code == 409
        assert "working database" in refused.json()["detail"]
        assert client.post("/api/reviews/accept-all", json={}).status_code == 409
        preview = client.get("/api/export/preview").json()
        assert preview["gated"] is False


# ---------------------------------------------------------------------------
# Migration: a database written before the review tables still opens
# ---------------------------------------------------------------------------


_PRE_07_REVIEWS_DDL = """
CREATE TABLE reviews (
    review_id      TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL DEFAULT 'legacy',
    mapping_id     TEXT NOT NULL,
    review_status  TEXT NOT NULL,
    reviewer       TEXT,
    reviewed_at    TEXT NOT NULL,
    comment        TEXT,
    CHECK (review_status IN ('accepted', 'rejected', 'flagged'))
);
"""

_PRE_21_REVIEWS_DDL = """
CREATE TABLE reviews (
    review_id      TEXT PRIMARY KEY,
    mapping_id     TEXT NOT NULL,
    review_status  TEXT NOT NULL,
    reviewer       TEXT,
    reviewed_at    TEXT NOT NULL,
    comment        TEXT,
    CHECK (review_status IN ('accepted', 'rejected', 'flagged'))
);
"""


def _rewrite_reviews_table(db: Path, ddl: str, rows: list[tuple]) -> None:
    """Put the reviews table back into an older shape, rows and all, so the
    next Storage() open has a real migration to perform."""
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DROP TABLE reviews")
    conn.executescript(ddl)
    placeholders = ", ".join("?" * len(rows[0]))
    columns = "review_id, run_id, mapping_id, review_status, reviewer, reviewed_at, comment"
    if "run_id" not in ddl:
        columns = "review_id, mapping_id, review_status, reviewer, reviewed_at, comment"
    conn.executemany(f"INSERT INTO reviews ({columns}) VALUES ({placeholders})", rows)
    conn.commit()
    conn.close()


class TestMigration:
    def test_an_append_only_table_rebuilds_with_the_latest_decision_governing(
        self, seeded
    ):
        db, run_id, ids = seeded
        _rewrite_reviews_table(
            db,
            _PRE_07_REVIEWS_DDL,
            [
                ("rev_1", run_id, ids[0], "accepted", "ryan", "2026-09-01T00:00:00+00:00", "first"),
                ("rev_2", run_id, ids[0], "rejected", "ryan", "2026-09-02T00:00:00+00:00", "second"),
                ("rev_3", run_id, ids[1], "flagged", None, "2026-09-03T00:00:00+00:00", None),
            ],
        )
        storage = Storage(db)
        storage.apply_schema()
        try:
            pk = tuple(
                r["name"]
                for r in storage.conn.execute("PRAGMA table_info(reviews)")
                if r["pk"]
            )
            assert pk == ("run_id", "mapping_id"), "the table was actually rebuilt"
            assert storage.conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 2
            kept = storage.reviews_for_run(run_id)
            assert set(kept) == {ids[0], ids[1]}
            assert kept[ids[0]].review_status == "rejected", "the latest row governs"
            assert kept[ids[0]].comment == "second"
            # and the rebuilt table still accepts a new decision, foreign keys on
            storage.review_set(
                run_id=run_id, mapping_id=ids[2], review_status="accepted",
            )
            assert storage.review_get(run_id, ids[2]).review_status == "accepted"
        finally:
            storage.close()

    def test_a_pre_run_scope_table_migrates_its_rows_to_the_legacy_run(self, tmp_path):
        db = tmp_path / "legacy.db"
        storage = Storage(db)
        storage.apply_schema()
        _seed_document(storage)
        ids = _seed_run(storage, LEGACY_RUN_ID)
        storage.close()
        _rewrite_reviews_table(
            db,
            _PRE_21_REVIEWS_DDL,
            [("rev_1", ids[0], "accepted", "ryan", "2026-09-01T00:00:00+00:00", "kept")],
        )
        storage = Storage(db)
        storage.apply_schema()
        try:
            kept = storage.reviews_for_run(LEGACY_RUN_ID)
            assert kept[ids[0]].review_status == "accepted"
            assert kept[ids[0]].comment == "kept"
            assert kept[ids[0]].run_id == LEGACY_RUN_ID
        finally:
            storage.close()
