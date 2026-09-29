"""Correct: a reviewer's override of the Indicator an Engine chose.

A good quote under the wrong Indicator used to leave the reviewer one honest
option, Reject, which threw the evidence away. A correction is a fourth Review
Decision: the reviewer names the right Indicator (one of the Run's own Pillars,
never the Mapping's own) and says why. The Mapping row the Engine produced is
never touched; the correction lives only in the decision, and every decision
ever written is kept in an append-only history.

Offline, against the same hand-seeded database the review tests use: one Run
over Pillar 7 with four passed Mappings, 7.1 to 7.4.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from regcompass.config import load_indicators
from regcompass.storage import Storage

from test_reviews import (  # the seeded Run and the app, shared with the review tests
    DOC_ID,
    ROOT,
    _app,
    _seed_document,
    _seed_run,
    seeded,  # noqa: F401 - a fixture
)

TITLES = {k: d.name for k, d in load_indicators().items()}


def _correct(client, run_id, mapping_id, indicator, comment="Right provision, wrong Indicator.",
             reviewer="ryan"):
    return client.post(
        "/api/reviews",
        json={
            "run_id": run_id, "mapping_id": mapping_id,
            "review_status": "corrected", "corrected_indicator_id": indicator,
            "comment": comment, "reviewer": reviewer,
        },
    )


def _decide(client, run_id, mapping_id, status, comment=None):
    return client.post(
        "/api/reviews",
        json={"run_id": run_id, "mapping_id": mapping_id,
              "review_status": status, "comment": comment},
    )


def _mapping_row(db: Path, run_id: str, mapping_id: str) -> dict:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM mappings WHERE run_id = ? AND mapping_id = ?", (run_id, mapping_id)
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


class TestACorrectionIsSaved:
    def test_the_decision_carries_the_corrected_indicator_and_the_reason(self, seeded, tmp_path):
        db, run_id, ids = seeded
        client = TestClient(_app(db, tmp_path))
        r = _correct(client, run_id, ids[0], "7.3", "  Retention, not a framework.  ")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["review_status"] == "corrected"
        assert body["corrected_indicator_id"] == "7.3"
        assert body["comment"] == "Retention, not a framework."
        assert body["reviewer"] == "ryan"

    def test_the_mapping_row_is_never_modified(self, seeded, tmp_path):
        db, run_id, ids = seeded
        before = _mapping_row(db, run_id, ids[0])
        client = TestClient(_app(db, tmp_path))
        assert _correct(client, run_id, ids[0], "7.3").status_code == 200
        after = _mapping_row(db, run_id, ids[0])
        assert after == before
        assert after["indicator_id"] == "7.1"

    def test_a_300_character_reason_is_accepted(self, seeded, tmp_path):
        db, run_id, ids = seeded
        client = TestClient(_app(db, tmp_path))
        assert _correct(client, run_id, ids[0], "7.2", "x" * 300).status_code == 200


class TestACorrectionIsRefusedInPlainWords:
    @pytest.mark.parametrize(
        ("indicator", "comment", "words"),
        [
            ("9.99", "reason", "not an Indicator"),
            ("6.1", "reason", "not an Indicator"),  # a real Indicator, but not this Run's Pillar
            ("6.5", "reason", "not an Indicator"),
            ("7.1", "reason", "already mapped"),
            (None, "reason", "Choose the Indicator"),
            ("7.3", None, "reason"),
            ("7.3", "   ", "reason"),
            ("7.3", "x" * 301, "300"),
        ],
    )
    def test_refused_with_422(self, seeded, tmp_path, indicator, comment, words):
        db, run_id, ids = seeded
        client = TestClient(_app(db, tmp_path))
        r = _correct(client, run_id, ids[0], indicator, comment)
        assert r.status_code == 422, r.text
        detail = r.json()["detail"]
        assert isinstance(detail, str) and words in detail, detail
        storage = Storage(db)
        try:
            assert storage.review_get(run_id, ids[0]) is None, "nothing was written"
        finally:
            storage.close()

    def test_only_a_correction_names_an_indicator(self, seeded, tmp_path):
        db, run_id, ids = seeded
        client = TestClient(_app(db, tmp_path))
        r = client.post(
            "/api/reviews",
            json={"run_id": run_id, "mapping_id": ids[0], "review_status": "accepted",
                  "corrected_indicator_id": "7.3"},
        )
        assert r.status_code == 422
        assert isinstance(r.json()["detail"], str)


class TestTheCurrentDecisionStaysOnePerMapping:
    def test_a_later_decision_replaces_a_correction(self, seeded, tmp_path):
        db, run_id, ids = seeded
        client = TestClient(_app(db, tmp_path))
        assert _correct(client, run_id, ids[0], "7.3").status_code == 200
        assert _decide(client, run_id, ids[0], "accepted").status_code == 200
        listed = client.get("/api/reviews", params={"run_id": run_id}).json()["reviews"]
        assert len(listed) == 1
        assert listed[0]["review_status"] == "accepted"
        assert listed[0]["corrected_indicator_id"] is None

    def test_a_correction_can_be_changed_to_another_indicator(self, seeded, tmp_path):
        db, run_id, ids = seeded
        client = TestClient(_app(db, tmp_path))
        _correct(client, run_id, ids[0], "7.3")
        assert _correct(client, run_id, ids[0], "7.5", "On reflection, access.").status_code == 200
        listed = client.get("/api/reviews", params={"run_id": run_id}).json()["reviews"]
        assert [(r["review_status"], r["corrected_indicator_id"]) for r in listed] == [
            ("corrected", "7.5")
        ]


class TestTheHistoryKeepsEveryDecision:
    def test_every_write_is_kept_oldest_first(self, seeded, tmp_path):
        db, run_id, ids = seeded
        client = TestClient(_app(db, tmp_path))
        _correct(client, run_id, ids[0], "7.3", "first thought")
        _decide(client, run_id, ids[0], "rejected", "second thought")
        _correct(client, run_id, ids[0], "7.4", "third thought")
        r = client.get(
            "/api/reviews/history", params={"run_id": run_id, "mapping_id": ids[0]}
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["run_id"] == run_id and body["mapping_id"] == ids[0]
        got = [(h["review_status"], h["corrected_indicator_id"], h["comment"])
               for h in body["history"]]
        assert got == [
            ("corrected", "7.3", "first thought"),
            ("rejected", None, "second thought"),
            ("corrected", "7.4", "third thought"),
        ]
        assert all(h["decided_at"] for h in body["history"])
        assert body["history"][0]["reviewer"] == "ryan"

    def test_accept_all_writes_history_too(self, seeded, tmp_path):
        db, run_id, ids = seeded
        client = TestClient(_app(db, tmp_path))
        assert client.post("/api/reviews/accept-all", json={"run_id": run_id}).status_code == 200
        for mapping_id in ids:
            hist = client.get(
                "/api/reviews/history", params={"run_id": run_id, "mapping_id": mapping_id}
            ).json()["history"]
            assert [h["review_status"] for h in hist] == ["accepted"]

    def test_a_refused_correction_leaves_no_history(self, seeded, tmp_path):
        db, run_id, ids = seeded
        client = TestClient(_app(db, tmp_path))
        _correct(client, run_id, ids[0], "7.1")
        hist = client.get(
            "/api/reviews/history", params={"run_id": run_id, "mapping_id": ids[0]}
        ).json()["history"]
        assert hist == []

    def test_an_unknown_mapping_is_a_404(self, seeded, tmp_path):
        db, run_id, _ = seeded
        client = TestClient(_app(db, tmp_path))
        r = client.get(
            "/api/reviews/history", params={"run_id": run_id, "mapping_id": "nope"}
        )
        assert r.status_code == 404


class TestRecordsAndTheQueueShowTheCorrection:
    def test_the_record_carries_both_indicators_with_titles(self, seeded, tmp_path):
        db, run_id, ids = seeded
        client = TestClient(_app(db, tmp_path))
        _correct(client, run_id, ids[0], "7.3")
        detail = client.get(f"/api/records/{ids[0]}", params={"run_id": run_id}).json()
        assert detail["record"]["indicator_id"] == "7.1"
        assert detail["corrected_indicator_id"] == "7.3"
        assert detail["corrected_indicator_title"] == TITLES["7.3"]
        assert detail["review"]["review_status"] == "corrected"

        rows = client.get(
            f"/api/documents/{DOC_ID}/records", params={"run_id": run_id}
        ).json()
        row = {r["mapping_id"]: r for r in rows}[ids[0]]
        assert row["indicator_id"] == "7.1"
        assert row["corrected_indicator_id"] == "7.3"
        assert row["corrected_indicator_title"] == TITLES["7.3"]
        other = {r["mapping_id"]: r for r in rows}[ids[1]]
        assert other["corrected_indicator_id"] is None
        assert other["corrected_indicator_title"] is None

    def test_the_record_offers_the_run_s_pillar_indicators_but_its_own(self, seeded, tmp_path):
        db, run_id, ids = seeded
        client = TestClient(_app(db, tmp_path))
        detail = client.get(f"/api/records/{ids[0]}", params={"run_id": run_id}).json()
        choices = detail["correction_choices"]
        assert [c["id"] for c in choices] == ["7.2", "7.3", "7.4", "7.5"]
        assert all(c["pillar"] == 7 for c in choices)
        assert choices[0]["title"] == TITLES["7.2"]

    def test_the_queue_filters_to_corrected_and_counts_them_as_reviewed(self, seeded, tmp_path):
        db, run_id, ids = seeded
        client = TestClient(_app(db, tmp_path))
        _correct(client, run_id, ids[0], "7.3")
        _decide(client, run_id, ids[1], "accepted")
        whole = client.get("/api/records", params={"run_id": run_id}).json()
        assert whole["total"] == 4
        assert whole["unreviewed"] == 2
        assert whole["corrected"] == 1
        only = client.get("/api/records", params={"run_id": run_id, "corrected": True}).json()
        assert [r["mapping_id"] for r in only["records"]] == [ids[0]]
        assert only["records"][0]["corrected_indicator_title"] == TITLES["7.3"]
        assert only["unreviewed"] == 2, "the counts describe the whole Run"

    def test_the_review_counts_name_corrected(self, seeded, tmp_path):
        db, run_id, ids = seeded
        client = TestClient(_app(db, tmp_path))
        _correct(client, run_id, ids[0], "7.3")
        preview = client.get("/api/export/preview", params={"run_id": run_id}).json()
        assert preview["n_corrected"] == 1
        assert preview["n_unreviewed"] == 3
        docs = client.get("/api/documents", params={"run_id": run_id}).json()
        assert docs[0]["n_corrected"] == 1


class TestAcceptAllAndIsolation:
    def test_accept_all_leaves_a_correction_alone(self, seeded, tmp_path):
        db, run_id, ids = seeded
        client = TestClient(_app(db, tmp_path))
        _correct(client, run_id, ids[0], "7.3")
        bulk = client.post("/api/reviews/accept-all", json={"run_id": run_id}).json()
        assert bulk["n_newly_accepted"] == 3
        listed = {
            r["mapping_id"]: r
            for r in client.get("/api/reviews", params={"run_id": run_id}).json()["reviews"]
        }
        assert listed[ids[0]]["review_status"] == "corrected"
        assert listed[ids[0]]["corrected_indicator_id"] == "7.3"
        hist = client.get(
            "/api/reviews/history", params={"run_id": run_id, "mapping_id": ids[0]}
        ).json()["history"]
        assert [h["review_status"] for h in hist] == ["corrected"]

    def test_a_correction_in_run_a_never_reaches_run_b(self, tmp_path):
        db = tmp_path / "two_runs.db"
        storage = Storage(db)
        storage.apply_schema()
        _seed_document(storage)
        a_ids = _seed_run(storage, "run_a")
        b_ids = _seed_run(storage, "run_b")
        storage.close()
        client = TestClient(_app(db, tmp_path))
        assert _correct(client, "run_a", a_ids[0], "7.3").status_code == 200
        b_rows = client.get(f"/api/documents/{DOC_ID}/records", params={"run_id": "run_b"}).json()
        assert all(r["corrected_indicator_id"] is None for r in b_rows)
        b_queue = client.get("/api/records", params={"run_id": "run_b", "corrected": True}).json()
        assert b_queue["records"] == []
        b_hist = client.get(
            "/api/reviews/history", params={"run_id": "run_b", "mapping_id": b_ids[0]}
        ).json()["history"]
        assert b_hist == []

    def test_the_frozen_bundle_refuses_a_correction_with_409(self, tmp_path):
        manifest = ROOT / "audit_bundle" / "manifest.json"
        if not manifest.is_file():  # pragma: no cover - the bundle is committed
            pytest.skip("no committed audit bundle")
        client = TestClient(_app(tmp_path / "unused.db", tmp_path, bundle_manifest=manifest))
        docs = client.get("/api/documents").json()
        recs = client.get(f"/api/documents/{docs[0]['document_id']}/records").json()
        refused = _correct(client, None, recs[0]["mapping_id"], "7.3")
        assert refused.status_code == 409
        detail = client.get(f"/api/records/{recs[0]['mapping_id']}").json()
        assert detail["correction_choices"] == []
        hist = client.get(
            "/api/reviews/history", params={"mapping_id": recs[0]["mapping_id"]}
        ).json()
        assert hist["history"] == []


# ---------------------------------------------------------------------------
# Migration: a database whose reviews table predates Correct upgrades in place
# ---------------------------------------------------------------------------

_PRE_CORRECT_REVIEWS_DDL = """
CREATE TABLE reviews (
    review_id      TEXT NOT NULL,
    run_id         TEXT NOT NULL DEFAULT 'legacy',
    mapping_id     TEXT NOT NULL,
    review_status  TEXT NOT NULL,
    reviewer       TEXT,
    reviewed_at    TEXT NOT NULL,
    comment        TEXT,
    PRIMARY KEY (run_id, mapping_id),
    CHECK (review_status IN ('accepted', 'rejected', 'flagged')),
    FOREIGN KEY (run_id, mapping_id) REFERENCES mappings(run_id, mapping_id)
);
"""


class TestTheMigrationToCorrect:
    def test_an_old_database_upgrades_in_place_twice_over_and_keeps_its_decisions(
        self, seeded, tmp_path
    ):
        db, run_id, ids = seeded
        conn = sqlite3.connect(db)
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DROP TABLE reviews")
        conn.execute("DROP TABLE IF EXISTS review_history")
        conn.executescript(_PRE_CORRECT_REVIEWS_DDL)
        old_rows = [
            ("rev_1", run_id, ids[0], "accepted", "ryan", "2026-09-01T00:00:00+00:00", "kept"),
            ("rev_2", run_id, ids[1], "flagged", None, "2026-09-02T00:00:00+00:00", None),
        ]
        conn.executemany(
            "INSERT INTO reviews (review_id, run_id, mapping_id, review_status, reviewer,"
            " reviewed_at, comment) VALUES (?, ?, ?, ?, ?, ?, ?)",
            old_rows,
        )
        conn.commit()
        conn.close()

        for _ in range(2):  # idempotent: the second open finds nothing to do
            storage = Storage(db)
            storage.apply_schema()
            try:
                columns = {r["name"] for r in storage.conn.execute("PRAGMA table_info(reviews)")}
                assert "corrected_indicator_id" in columns
                assert "review_history" in storage.table_names()
                kept = storage.reviews_for_run(run_id)
                assert set(kept) == {ids[0], ids[1]}
                assert (kept[ids[0]].review_status, kept[ids[0]].comment) == ("accepted", "kept")
                assert kept[ids[1]].review_status == "flagged"
                assert kept[ids[0]].corrected_indicator_id is None
            finally:
                storage.close()

        client = TestClient(_app(db, tmp_path))
        assert _correct(client, run_id, ids[2], "7.1").status_code == 200
        listed = client.get("/api/reviews", params={"run_id": run_id}).json()["reviews"]
        assert len(listed) == 3


class TestAnInterruptedRebuildIsFinished:
    """A rebuild parks the old table, lets schema.sql build the new one, then
    copies the rows back. A process that dies between the steps must not
    strand the rows in the parked table: the next open finishes the copy."""

    _SUFFIX = Storage._REBUILD_SUFFIX

    def _old_rows(self, run_id, ids):
        return [
            ("rev_1", run_id, ids[0], "accepted", "ryan", "2026-09-01T00:00:00+00:00", "kept"),
            ("rev_2", run_id, ids[1], "flagged", None, "2026-09-02T00:00:00+00:00", None),
        ]

    def _park(self, db, run_id, ids, *, keep_fresh_table: bool):
        """The database as a crash leaves it: the old reviews table (with its
        rows) renamed aside, and either no reviews table at all (died before
        schema.sql ran) or a fresh empty one (died before the copy)."""
        conn = sqlite3.connect(db)
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("PRAGMA legacy_alter_table = ON")
        conn.execute("DROP TABLE reviews")
        conn.executescript(_PRE_CORRECT_REVIEWS_DDL)
        conn.executemany(
            "INSERT INTO reviews (review_id, run_id, mapping_id, review_status, reviewer,"
            " reviewed_at, comment) VALUES (?, ?, ?, ?, ?, ?, ?)",
            self._old_rows(run_id, ids),
        )
        conn.execute(f"ALTER TABLE reviews RENAME TO reviews{self._SUFFIX}")
        if keep_fresh_table:
            schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
            start = schema.index("CREATE TABLE IF NOT EXISTS reviews (")
            conn.executescript(schema[start:schema.index("\n);", start) + 3])
        conn.commit()
        conn.close()

    @pytest.mark.parametrize("keep_fresh_table", [True, False])
    def test_the_rows_come_back_and_the_parked_table_goes(
        self, seeded, keep_fresh_table
    ):
        db, run_id, ids = seeded
        self._park(db, run_id, ids, keep_fresh_table=keep_fresh_table)
        for _ in range(2):
            storage = Storage(db)
            storage.apply_schema()
            try:
                assert f"reviews{self._SUFFIX}" not in storage.table_names()
                kept = storage.reviews_for_run(run_id)
                assert set(kept) == {ids[0], ids[1]}
                assert (kept[ids[0]].review_status, kept[ids[0]].comment) == ("accepted", "kept")
                assert kept[ids[1]].review_status == "flagged"
                sql = storage.conn.execute(
                    "SELECT sql FROM sqlite_master WHERE name = 'reviews'"
                ).fetchone()[0]
                assert "'corrected'" in sql, "the table reached the current shape"
            finally:
                storage.close()

    def test_any_rebuilt_table_is_recovered_not_only_reviews(self, seeded):
        db, _, _ = seeded
        conn = sqlite3.connect(db)
        n_runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("PRAGMA legacy_alter_table = ON")
        conn.execute(f"ALTER TABLE runs RENAME TO runs{self._SUFFIX}")
        conn.commit()
        conn.close()
        storage = Storage(db)
        storage.apply_schema()
        try:
            assert f"runs{self._SUFFIX}" not in storage.table_names()
            assert storage.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == n_runs
        finally:
            storage.close()


class TestHistoryIsBackFilledFromEarlierDecisions:
    def test_each_standing_decision_gets_one_history_row_once(self, seeded, tmp_path):
        db, run_id, ids = seeded
        conn = sqlite3.connect(db)
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DROP TABLE reviews")
        conn.execute("DROP TABLE review_history")
        conn.executescript(_PRE_CORRECT_REVIEWS_DDL)
        conn.executemany(
            "INSERT INTO reviews (review_id, run_id, mapping_id, review_status, reviewer,"
            " reviewed_at, comment) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("rev_1", run_id, ids[0], "rejected", "ryan", "2026-09-01T00:00:00+00:00", "off topic"),
                ("rev_2", run_id, ids[1], "accepted", None, "2026-09-02T00:00:00+00:00", None),
            ],
        )
        conn.commit()
        conn.close()

        for _ in range(2):  # the second open adds nothing
            storage = Storage(db)
            storage.apply_schema()
            try:
                assert storage.conn.execute(
                    "SELECT COUNT(*) FROM review_history"
                ).fetchone()[0] == 2
                first = storage.review_history(run_id, ids[0])
                assert [(h["review_status"], h["reviewer"], h["decided_at"], h["comment"])
                        for h in first] == [
                    ("rejected", "ryan", "2026-09-01T00:00:00+00:00", "off topic")
                ]
            finally:
                storage.close()

        # Changing the earlier decision keeps it on record.
        client = TestClient(_app(db, tmp_path))
        assert _correct(client, run_id, ids[0], "7.3").status_code == 200
        hist = client.get(
            "/api/reviews/history", params={"run_id": run_id, "mapping_id": ids[0]}
        ).json()["history"]
        assert [h["review_status"] for h in hist] == ["rejected", "corrected"]
