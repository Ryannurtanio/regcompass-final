"""The Comparison: two Runs on one Economy and Pillar side by side.

A reviewer runs the same Economy and Pillar on Engine A and then on Engine B
and asks one question: did the two Engines choose the same controlling
provision for each Indicator? Everything here runs offline over seeded Run
Records and seeded Mappings, so no model is ever called and the agreement rule
is pinned cell by cell rather than inferred from a pipeline's output.
"""

from __future__ import annotations

import csv
import io
import json

import pytest
from fastapi.testclient import TestClient

from regcompass.compare import (
    ComparisonMismatch,
    comparison_csv,
    comparison_filename,
    compare_runs,
)
from regcompass.contracts import MappingRecord, RunRecord
from regcompass.server import create_app
from regcompass.storage import Storage

ECONOMY = "SG"
PILLAR = 7
DOC_A = "doc_pdpa"
DOC_B = "doc_cyber"
TITLES = {DOC_A: "Personal Data Protection Act 2012", DOC_B: "Cybersecurity Act 2018"}


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def a_run(
    run_id: str,
    engine: str,
    *,
    started_at: str,
    ended_at: str | None = None,
    status: str = "completed",
    economy: str = ECONOMY,
    pillars: list[int] | None = None,
    indicators: list[str] | None = None,
    **kw,
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        kind="run",
        economy=economy,
        pillars=[PILLAR] if pillars is None else pillars,
        indicators=indicators,
        engine=engine,
        status=status,
        started_at=started_at,
        ended_at=ended_at,
        **kw,
    )


def a_mapping(
    indicator_id: str,
    document_id: str,
    section: str,
    *,
    subsection: str | None = None,
    chunk_id: str | None = None,
    quote: str = "No organisation shall process personal data without consent.",
    confidence: float | None = 0.6,
    controlling: bool | None = True,
    verification_status: str = "passed",
    page_number: int | None = 3,
) -> MappingRecord:
    chunk = chunk_id or f"{document_id}::c{indicator_id.replace('.', '_')}"
    return MappingRecord(
        mapping_id=f"{chunk}::{indicator_id}",
        document_id=document_id,
        chunk_id=chunk,
        economy=ECONOMY,
        indicator_id=indicator_id,
        indicator_name=f"Indicator {indicator_id}",
        section=section,
        subsection=subsection,
        verbatim_quote=quote,
        page_number=page_number,
        confidence=confidence,
        controlling_evidence=controlling,
        verification_status=verification_status,
    )


# ---------------------------------------------------------------------------
# acceptance box 2: the agreement rule, as unit tests on the pure builder
# ---------------------------------------------------------------------------


class TestAgreement:
    RUN_A = a_run("run_a", "engine-a", started_at="2026-09-01T00:00:00Z")
    RUN_B = a_run("run_b", "engine-b", started_at="2026-09-01T01:00:00Z")

    def build(self, mappings_a, mappings_b, indicators=("7.1",)):
        return compare_runs(
            self.RUN_A, self.RUN_B, mappings_a, mappings_b, indicators,
            document_titles=TITLES,
        )

    def test_the_same_document_and_location_agree(self):
        both = [a_mapping("7.1", DOC_A, "s. 13", subsection="(1)")]
        row = self.build(both, list(both)).rows[0]
        assert row.agreement == "agree"
        assert row.a is not None and row.b is not None
        assert row.a.document_title == TITLES[DOC_A]

    def test_a_different_subsection_of_one_section_disagrees(self):
        comparison = self.build(
            [a_mapping("7.1", DOC_A, "s. 13", subsection="(1)")],
            [a_mapping("7.1", DOC_A, "s. 13", subsection="(2)")],
        )
        assert comparison.rows[0].agreement == "disagree"
        assert comparison.n_disagree == 1

    def test_a_different_document_disagrees(self):
        comparison = self.build(
            [a_mapping("7.1", DOC_A, "s. 13")],
            [a_mapping("7.1", DOC_B, "s. 13")],
        )
        assert comparison.rows[0].agreement == "disagree"

    def test_an_indicator_missing_on_one_side_is_shown_as_such(self):
        only_a = self.build([a_mapping("7.1", DOC_A, "s. 13")], [])
        assert only_a.rows[0].agreement == "only_a"
        assert only_a.rows[0].b is None
        assert only_a.n_only_a == 1

        only_b = self.build([], [a_mapping("7.1", DOC_A, "s. 13")])
        assert only_b.rows[0].agreement == "only_b"
        assert only_b.rows[0].a is None
        assert only_b.n_only_b == 1

    def test_an_indicator_neither_engine_answered_is_neither(self):
        comparison = self.build([], [])
        assert comparison.rows[0].agreement == "neither"
        assert (comparison.rows[0].a, comparison.rows[0].b) == (None, None)
        assert comparison.n_neither == 1

    def test_spacing_and_case_in_the_location_do_not_break_a_match(self):
        # Section labels are repaired at export time and raw labels differ in
        # spacing, so the match normalises whitespace and case.
        comparison = self.build(
            [a_mapping("7.1", DOC_A, "s. 13", subsection="(1)")],
            [a_mapping("7.1", DOC_A, "S.  13\n", subsection=" (1) ")],
        )
        assert comparison.rows[0].agreement == "agree"

    def test_the_controlling_mapping_is_the_one_compared(self):
        # M8 flags exactly one controlling Mapping per (Run, Indicator); a
        # higher-Confidence sibling that lost reconciliation must not be the
        # one the Comparison shows.
        side_a = [
            a_mapping("7.1", DOC_A, "s. 13", chunk_id="ch1", confidence=0.4),
            a_mapping(
                "7.1", DOC_B, "s. 99", chunk_id="ch2", confidence=0.95,
                controlling=False,
            ),
        ]
        comparison = self.build(side_a, [a_mapping("7.1", DOC_A, "s. 13")])
        assert comparison.rows[0].a is not None
        assert comparison.rows[0].a.document_id == DOC_A
        assert comparison.rows[0].agreement == "agree"

    def test_without_a_flag_the_highest_confidence_mapping_stands_in(self):
        side_a = [
            a_mapping("7.1", DOC_A, "s. 13", chunk_id="ch1", confidence=0.4,
                      controlling=None),
            a_mapping("7.1", DOC_B, "s. 99", chunk_id="ch2", confidence=0.95,
                      controlling=None),
        ]
        comparison = self.build(side_a, [])
        assert comparison.rows[0].a is not None
        assert comparison.rows[0].a.document_id == DOC_B

    def test_a_dropped_mapping_is_not_evidence(self):
        comparison = self.build(
            [a_mapping("7.1", DOC_A, "s. 13", verification_status="dropped")],
            [],
        )
        assert comparison.rows[0].agreement == "neither"

    def test_every_indicator_of_the_pillar_gets_a_row_in_order(self):
        comparison = self.build(
            [a_mapping("7.2", DOC_A, "s. 13")], [],
            indicators=("7.1", "7.2", "7.3"),
        )
        assert [r.indicator_id for r in comparison.rows] == ["7.1", "7.2", "7.3"]
        assert comparison.n_indicators == 3
        assert comparison.n_agree + comparison.n_disagree + comparison.n_only_a \
            + comparison.n_only_b + comparison.n_neither == 3

    def test_indicator_names_come_from_the_registry_when_given(self):
        comparison = compare_runs(
            self.RUN_A, self.RUN_B, [], [],
            {"7.1": "Lack of comprehensive legal framework for data protection"},
        )
        assert comparison.rows[0].indicator_name.startswith("Lack of comprehensive")

    def test_evidence_outside_the_listed_indicators_is_never_dropped(self):
        comparison = self.build([a_mapping("7.4", DOC_A, "s. 13")], [], indicators=("7.1",))
        assert [r.indicator_id for r in comparison.rows] == ["7.1", "7.4"]

    def test_the_header_carries_both_run_records_with_duration(self):
        run_a = a_run(
            "run_a", "engine-a",
            started_at="2026-09-01T00:00:00Z", ended_at="2026-09-01T00:02:00Z",
            prompt_tokens=120, completion_tokens=34, cost_usd=0.75,
        )
        run_b = a_run(
            "run_b", "engine-b",
            started_at="2026-09-01T01:00:00Z", ended_at="2026-09-01T01:00:30Z",
            prompt_tokens=80, completion_tokens=20, cost_usd=0.04,
        )
        comparison = compare_runs(
            run_a, run_b, [], [], ("7.1",),
            engine_display_names={"engine-a": "Engine A", "engine-b": "Engine B"},
        )
        assert comparison.run_a.record.run_id == "run_a"
        assert comparison.run_a.engine_display_name == "Engine A"
        assert comparison.run_a.duration_s == 120.0
        assert comparison.run_b.duration_s == 30.0
        assert comparison.run_b.record.cost_usd == 0.04
        assert comparison.economy == ECONOMY
        assert comparison.pillars == [PILLAR]

    def test_two_runs_on_different_economies_are_refused(self):
        other = a_run("run_b", "engine-b", started_at="2026-09-01T01:00:00Z",
                      economy="MY")
        with pytest.raises(ComparisonMismatch) as excinfo:
            compare_runs(self.RUN_A, other, [], [], ("7.1",))
        assert "SG" in str(excinfo.value) and "MY" in str(excinfo.value)

    def test_two_runs_on_different_pillars_are_refused(self):
        other = a_run("run_b", "engine-b", started_at="2026-09-01T01:00:00Z",
                      pillars=[6])
        with pytest.raises(ComparisonMismatch):
            compare_runs(self.RUN_A, other, [], [], ("7.1",))

    def test_a_missing_confidence_falls_back_to_the_gate_composite(self):
        # mappings.confidence stays NULL until the composite is assigned, and
        # the audit view recomputes it from the Gate cosines. A Comparison that
        # showed a blank where the audit view shows a number would look broken.
        from regcompass.export import confidence_score

        record = a_mapping("7.1", DOC_A, "s. 13", chunk_id="ch1", confidence=None)
        comparison = compare_runs(
            self.RUN_A, self.RUN_B, [record], [], ("7.1",),
            gate_cosines={("ch1", "7.1"): 0.8},
        )
        expected = confidence_score(0.8, len(record.verbatim_quote), 1, 1)
        assert comparison.rows[0].a is not None
        assert comparison.rows[0].a.confidence == pytest.approx(expected)


# ---------------------------------------------------------------------------
# a seeded working database: two Runs, two Engines, one Economy, one Pillar
# ---------------------------------------------------------------------------


def _seed_documents(storage: Storage) -> None:
    for i, (doc_id, title) in enumerate(TITLES.items()):
        storage.conn.execute(
            "INSERT INTO documents (document_id, economy, source_url, source_sha256,"
            " title, n_pages, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (doc_id, ECONOMY, f"https://example.gov/{doc_id}", f"sha{i}", title, 40,
             "2026-09-01T00:00:00Z"),
        )
    storage.conn.commit()


def _seed_chunks(storage: Storage, records) -> None:
    seen = set()
    for r in records:
        if r.chunk_id in seen:
            continue
        seen.add(r.chunk_id)
        storage.conn.execute(
            "INSERT INTO chunks (chunk_id, document_id, char_start, char_end,"
            " section_label, created_at) VALUES (?, ?, 0, 100, ?, ?)",
            (r.chunk_id, r.document_id, r.section, "2026-09-01T00:00:00Z"),
        )
    storage.conn.commit()


def _finish(storage: Storage, run_id: str, record: RunRecord) -> None:
    storage.run_start(
        run_id=run_id, kind="run", economy=record.economy,
        pillars=record.pillars, indicators=record.indicators,
        engine=record.engine, started_at=record.started_at,
    )
    storage.run_finish(
        run_id, status=record.status, ended_at=record.ended_at or "",
        prompt_tokens=record.prompt_tokens,
        completion_tokens=record.completion_tokens, cost_usd=record.cost_usd,
    )


MAPPINGS_A = [
    a_mapping("7.1", DOC_A, "s. 13", subsection="(1)", confidence=0.71),
    a_mapping("7.2", DOC_B, "s. 5", subsection="(2)", confidence=None,
              chunk_id="ch_a72"),
    a_mapping("7.3", DOC_A, "s. 24", confidence=0.5),
]
MAPPINGS_B = [
    a_mapping("7.1", DOC_A, "S.  13", subsection=" (1)", confidence=0.66),
    a_mapping("7.2", DOC_B, "s. 5", subsection="(3)", confidence=0.4,
              chunk_id="ch_b72"),
    a_mapping("7.4", DOC_B, "s. 8", confidence=0.9),
]


@pytest.fixture
def paired_db(tmp_path):
    """Two completed Runs, one per declared Engine, over one Economy and Pillar
    with deliberately different answers: 7.1 agrees, 7.2 disagrees on the
    subsection, 7.3 is Engine A only, 7.4 is Engine B only, 7.5 is neither."""
    path = tmp_path / "web.db"
    storage = Storage(path)
    storage.apply_schema()
    _seed_documents(storage)
    _seed_chunks(storage, MAPPINGS_A + MAPPINGS_B)
    _finish(storage, "run_a", a_run(
        "run_a", "engine-a",
        started_at="2026-09-01T00:00:00Z", ended_at="2026-09-01T00:02:00Z",
        prompt_tokens=120, completion_tokens=34, cost_usd=0.75,
    ))
    _finish(storage, "run_b", a_run(
        "run_b", "engine-b",
        started_at="2026-09-01T01:00:00Z", ended_at="2026-09-01T01:00:30Z",
        prompt_tokens=80, completion_tokens=20, cost_usd=0.04,
    ))
    storage.upsert_mappings(MAPPINGS_A, run_id="run_a")
    storage.upsert_mappings(MAPPINGS_B, run_id="run_b")
    storage.upsert_gate_scores({("ch_a72", "7.2"): 0.82})
    storage.conn.close()
    out = tmp_path / "out"
    out.mkdir()
    return TestClient(create_app(db_path=path, out_dir=out, ui_dir=None)), path


# ---------------------------------------------------------------------------
# acceptance box 1: the API
# ---------------------------------------------------------------------------


class TestCompareApi:
    def test_two_run_ids_pair_the_runs_indicator_by_indicator(self, paired_db):
        client, _ = paired_db
        body = client.get(
            "/api/compare", params={"run_a": "run_a", "run_b": "run_b"}
        ).json()

        assert body["run_a"]["record"]["run_id"] == "run_a"
        assert body["run_b"]["record"]["run_id"] == "run_b"
        assert body["run_a"]["engine_display_name"] == "Engine A: GPT-5.6 Luna"
        assert body["run_a"]["duration_s"] == 120.0
        assert body["run_b"]["record"]["cost_usd"] == 0.04
        assert body["economy"] == ECONOMY and body["pillars"] == [PILLAR]

        rows = body["rows"]
        assert [r["indicator_id"] for r in rows] == ["7.1", "7.2", "7.3", "7.4", "7.5"]
        assert [r["agreement"] for r in rows] == [
            "agree", "disagree", "only_a", "only_b", "neither",
        ]
        assert body["n_indicators"] == 5
        assert (body["n_agree"], body["n_disagree"]) == (1, 1)
        assert (body["n_only_a"], body["n_only_b"], body["n_neither"]) == (1, 1, 1)

    def test_a_row_carries_both_sides_evidence(self, paired_db):
        client, _ = paired_db
        rows = client.get(
            "/api/compare", params={"run_a": "run_a", "run_b": "run_b"}
        ).json()["rows"]
        first = rows[0]
        assert first["indicator_name"].startswith("Lack of comprehensive")
        assert first["a"]["document_title"] == TITLES[DOC_A]
        assert first["a"]["section"] == "s. 13"
        assert first["b"]["section"] == "S.  13"
        assert first["a"]["confidence"] == 0.71
        assert first["a"]["verbatim_quote"].startswith("No organisation")
        assert first["a"]["review_status"] is None
        # the NULL Confidence is filled from the Gate composite, not left blank
        assert rows[1]["a"]["confidence"] is not None

    def test_economy_and_pillar_auto_pair_the_newest_run_per_engine(self, paired_db):
        client, path = paired_db
        storage = Storage(path)
        _finish(storage, "run_a2", a_run(
            "run_a2", "engine-a",
            started_at="2026-09-02T00:00:00Z", ended_at="2026-09-02T00:01:00Z",
        ))
        storage.upsert_mappings(MAPPINGS_A, run_id="run_a2")
        storage.conn.close()

        body = client.get(
            "/api/compare", params={"economy": ECONOMY, "pillar": PILLAR}
        ).json()
        assert body["run_a"]["record"]["run_id"] == "run_a2"
        assert body["run_b"]["record"]["run_id"] == "run_b"
        assert len(body["rows"]) == 5

    def test_an_unfinished_or_failed_run_is_never_paired(self, paired_db):
        client, path = paired_db
        storage = Storage(path)
        storage.run_start(
            run_id="run_a3", kind="run", economy=ECONOMY, pillars=[PILLAR],
            indicators=None, engine="engine-a",
            started_at="2026-09-03T00:00:00Z",
        )
        _finish(storage, "run_a4", a_run(
            "run_a4", "engine-a", status="failed",
            started_at="2026-09-04T00:00:00Z", ended_at="2026-09-04T00:00:10Z",
        ))
        # a completed Run that simply covered a different Pillar
        _finish(storage, "run_a5", a_run(
            "run_a5", "engine-a", pillars=[6],
            started_at="2026-09-05T00:00:00Z", ended_at="2026-09-05T00:01:00Z",
        ))
        storage.conn.close()
        body = client.get(
            "/api/compare", params={"economy": ECONOMY, "pillar": PILLAR}
        ).json()
        assert body["run_a"]["record"]["run_id"] == "run_a"

    def test_a_newer_fake_engine_run_never_displaces_a_declared_engine(
        self, paired_db
    ):
        # The pair is chosen by the Engine registry, not by recency across
        # Engines: a rehearsal on the fake Engine run minutes ago must not push
        # Engine A out of the Comparison the judges see.
        client, path = paired_db
        storage = Storage(path)
        _finish(storage, "run_fake", a_run(
            "run_fake", "fake",
            started_at="2026-09-09T00:00:00Z", ended_at="2026-09-09T00:00:05Z",
        ))
        storage.upsert_mappings(MAPPINGS_A, run_id="run_fake")
        storage.conn.close()
        body = client.get(
            "/api/compare", params={"economy": ECONOMY, "pillar": PILLAR}
        ).json()
        assert body["run_a"]["record"]["engine"] == "engine-a"
        assert body["run_b"]["record"]["engine"] == "engine-b"
        assert body["note"] is None

    def test_a_non_declared_engine_is_paired_only_as_a_last_resort_and_said_so(
        self, tmp_path
    ):
        path = tmp_path / "fallback.db"
        storage = Storage(path)
        storage.apply_schema()
        _seed_documents(storage)
        _seed_chunks(storage, MAPPINGS_A + MAPPINGS_B)
        _finish(storage, "run_fake", a_run(
            "run_fake", "fake",
            started_at="2026-09-01T00:00:00Z", ended_at="2026-09-01T00:00:05Z",
        ))
        _finish(storage, "run_b", a_run(
            "run_b", "engine-b",
            started_at="2026-09-02T00:00:00Z", ended_at="2026-09-02T00:00:30Z",
        ))
        storage.upsert_mappings(MAPPINGS_A, run_id="run_fake")
        storage.upsert_mappings(MAPPINGS_B, run_id="run_b")
        storage.conn.close()
        out = tmp_path / "out"
        out.mkdir()
        client = TestClient(create_app(db_path=path, out_dir=out, ui_dir=None))

        body = client.get(
            "/api/compare", params={"economy": ECONOMY, "pillar": PILLAR}
        ).json()
        assert body["run_a"]["record"]["engine"] == "engine-b"
        assert body["run_b"]["record"]["engine"] == "fake"
        assert "fake" in body["note"]

        csv_text = client.get(
            "/api/compare/download",
            params={"economy": ECONOMY, "pillar": PILLAR},
        ).text
        assert "note" in csv_text and "fake" in csv_text

    def test_one_engine_only_is_a_404_that_says_what_to_do(self, tmp_path):
        path = tmp_path / "lonely.db"
        storage = Storage(path)
        storage.apply_schema()
        _finish(storage, "run_a", a_run(
            "run_a", "engine-a",
            started_at="2026-09-01T00:00:00Z", ended_at="2026-09-01T00:01:00Z",
        ))
        storage.conn.close()
        out = tmp_path / "out"
        out.mkdir()
        client = TestClient(create_app(db_path=path, out_dir=out, ui_dir=None))
        r = client.get("/api/compare", params={"economy": ECONOMY, "pillar": PILLAR})
        assert r.status_code == 404
        assert "Engine" in r.json()["detail"]

    def test_an_unknown_run_id_is_a_404(self, paired_db):
        client, _ = paired_db
        r = client.get("/api/compare", params={"run_a": "nope", "run_b": "run_b"})
        assert r.status_code == 404
        assert "nope" in r.json()["detail"]

    def test_two_economies_is_a_400_naming_both(self, paired_db):
        client, path = paired_db
        storage = Storage(path)
        _finish(storage, "run_my", a_run(
            "run_my", "engine-b", economy="MY",
            started_at="2026-09-05T00:00:00Z", ended_at="2026-09-05T00:01:00Z",
        ))
        storage.conn.close()
        r = client.get("/api/compare", params={"run_a": "run_a", "run_b": "run_my"})
        assert r.status_code == 400
        assert "SG" in r.json()["detail"] and "MY" in r.json()["detail"]

    def test_half_a_pair_is_a_400(self, paired_db):
        client, _ = paired_db
        r = client.get("/api/compare", params={"run_a": "run_a"})
        assert r.status_code == 400

    def test_an_unknown_pillar_is_a_400(self, paired_db):
        client, _ = paired_db
        r = client.get("/api/compare", params={"economy": ECONOMY, "pillar": 99})
        assert r.status_code == 400

    def test_two_fake_engine_runs_are_two_runs(self, tmp_path):
        # Acceptance box 3, as the API contract the screen renders: the header
        # and both sides come back for two Runs of the fake Engine, so the
        # interface is exercised without a paid call or a second model.
        path = tmp_path / "fake.db"
        storage = Storage(path)
        storage.apply_schema()
        _seed_documents(storage)
        _seed_chunks(storage, MAPPINGS_A + MAPPINGS_B)
        _finish(storage, "fake_1", a_run(
            "fake_1", "fake",
            started_at="2026-09-01T00:00:00Z", ended_at="2026-09-01T00:00:10Z",
        ))
        _finish(storage, "fake_2", a_run(
            "fake_2", "fake",
            started_at="2026-09-01T00:10:00Z", ended_at="2026-09-01T00:10:05Z",
        ))
        storage.upsert_mappings(MAPPINGS_A, run_id="fake_1")
        storage.upsert_mappings(MAPPINGS_B, run_id="fake_2")
        storage.conn.close()
        out = tmp_path / "out"
        out.mkdir()
        client = TestClient(create_app(db_path=path, out_dir=out, ui_dir=None))

        body = client.get(
            "/api/compare", params={"run_a": "fake_1", "run_b": "fake_2"}
        ).json()
        assert body["run_a"]["engine_display_name"].startswith("Fake Engine")
        assert body["run_b"]["duration_s"] == 5.0
        assert [r["agreement"] for r in body["rows"]] == [
            "agree", "disagree", "only_a", "only_b", "neither",
        ]
        assert body["rows"][0]["a"]["document_title"] == TITLES[DOC_A]
        assert body["rows"][0]["b"]["verbatim_quote"]

    def test_narrowed_runs_compare_only_the_indicators_they_ran(self, tmp_path):
        # The live test names one Pillar and two Indicators: a two-Indicator
        # Comparison must have two rows, not five with three empty pairs.
        path = tmp_path / "narrow.db"
        storage = Storage(path)
        storage.apply_schema()
        _seed_documents(storage)
        _seed_chunks(storage, MAPPINGS_A)
        for run_id, engine in (("run_a", "engine-a"), ("run_b", "engine-b")):
            _finish(storage, run_id, a_run(
                run_id, engine, indicators=["7.1", "7.2"],
                started_at=f"2026-09-01T0{1 if engine.endswith('b') else 0}:00:00Z",
                ended_at="2026-09-01T02:00:00Z",
            ))
            storage.upsert_mappings(MAPPINGS_A[:2], run_id=run_id)
        storage.conn.close()
        out = tmp_path / "out"
        out.mkdir()
        client = TestClient(create_app(db_path=path, out_dir=out, ui_dir=None))
        body = client.get(
            "/api/compare", params={"run_a": "run_a", "run_b": "run_b"}
        ).json()
        assert [r["indicator_id"] for r in body["rows"]] == ["7.1", "7.2"]


# ---------------------------------------------------------------------------
# acceptance box 4: the download is the same content as the screen
# ---------------------------------------------------------------------------


class TestDownload:
    def _payload(self, client):
        return client.get(
            "/api/compare", params={"run_a": "run_a", "run_b": "run_b"}
        ).json()

    def test_the_csv_body_equals_the_payload_cell_by_cell(self, paired_db):
        client, _ = paired_db
        payload = self._payload(client)
        r = client.get(
            "/api/compare/download",
            params={"run_a": "run_a", "run_b": "run_b", "format": "csv"},
        )
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")
        assert "attachment" in r.headers["content-disposition"]

        all_rows = list(csv.reader(io.StringIO(r.text)))
        blank = all_rows.index([])
        header_block = {row[0]: row[1:] for row in all_rows[:blank] if row}
        assert header_block["run_id"] == ["run_a", "run_b"]
        assert header_block["engine"] == [
            "Engine A: GPT-5.6 Luna", "Engine B: Qwen3-30B-A3B-Instruct-2507",
        ]
        assert header_block["cost_usd"] == ["0.75", "0.04"]
        assert header_block["duration_s"] == ["120.0", "30.0"]

        table_rows = all_rows[blank + 1:]
        columns = table_rows[0]
        table = [dict(zip(columns, row)) for row in table_rows[1:]]
        assert len(table) == len(payload["rows"])
        for cells, row in zip(table, payload["rows"]):
            assert cells["indicator_id"] == row["indicator_id"]
            assert cells["indicator_name"] == row["indicator_name"]
            assert cells["agreement"] == row["agreement"]
            for prefix, side in (("a", row["a"]), ("b", row["b"])):
                if side is None:
                    assert cells[f"{prefix}_mapping_id"] == ""
                    continue
                assert cells[f"{prefix}_mapping_id"] == side["mapping_id"]
                assert cells[f"{prefix}_document_title"] == side["document_title"]
                assert cells[f"{prefix}_section"] == side["section"]
                assert cells[f"{prefix}_subsection"] == (side["subsection"] or "")
                assert cells[f"{prefix}_verbatim_quote"] == side["verbatim_quote"]
                assert float(cells[f"{prefix}_confidence"]) == pytest.approx(
                    side["confidence"]
                )

    def test_the_json_download_is_the_payload(self, paired_db):
        client, _ = paired_db
        payload = self._payload(client)
        r = client.get(
            "/api/compare/download",
            params={"run_a": "run_a", "run_b": "run_b", "format": "json"},
        )
        assert r.status_code == 200
        assert json.loads(r.text) == payload
        assert "attachment" in r.headers["content-disposition"]

    def test_the_filename_names_the_economy_the_pillar_and_both_engines(
        self, paired_db
    ):
        client, _ = paired_db
        r = client.get(
            "/api/compare/download", params={"run_a": "run_a", "run_b": "run_b"}
        )
        disposition = r.headers["content-disposition"]
        assert "comparison_SG_p7_engine-a_vs_engine-b.csv" in disposition

    def test_csv_is_the_default_format(self, paired_db):
        client, _ = paired_db
        r = client.get(
            "/api/compare/download", params={"run_a": "run_a", "run_b": "run_b"}
        )
        assert r.headers["content-type"].startswith("text/csv")

    def test_an_unknown_format_is_a_400(self, paired_db):
        client, _ = paired_db
        r = client.get(
            "/api/compare/download",
            params={"run_a": "run_a", "run_b": "run_b", "format": "pdf"},
        )
        assert r.status_code == 400

    def test_the_writers_are_pure_and_reusable(self):
        comparison = compare_runs(
            a_run("run_a", "engine-a", started_at="2026-09-01T00:00:00Z"),
            a_run("run_b", "engine-b", started_at="2026-09-01T01:00:00Z"),
            [a_mapping("7.1", DOC_A, "s. 13")], [], ("7.1",),
            document_titles=TITLES,
        )
        text = comparison_csv(comparison)
        assert "indicator_id" in text and "only_a" in text
        assert comparison_filename(comparison, "csv") == (
            "comparison_SG_p7_engine-a_vs_engine-b.csv"
        )


# ---------------------------------------------------------------------------
# the Review Decision column: each side reads its OWN Run's decisions
# ---------------------------------------------------------------------------


class TestReviewDecisionsPerSide:
    """Engine A and Engine B answer 7.1 with the SAME Mapping id here, which is
    exactly the case the composite key exists for: a decision accepted on Run A
    says nothing about Run B's Mapping of the same name."""

    def _rows(self, client):
        return client.get(
            "/api/compare", params={"run_a": "run_a", "run_b": "run_b"}
        ).json()["rows"]

    def _decide(self, client, run_id, mapping_id, status):
        return client.post(
            "/api/reviews",
            json={"run_id": run_id, "mapping_id": mapping_id, "review_status": status},
        )

    def test_a_decision_on_side_a_shows_on_side_a_only(self, paired_db):
        client, _ = paired_db
        shared = MAPPINGS_A[0].mapping_id
        assert shared == MAPPINGS_B[0].mapping_id, "both Engines chose this Mapping"
        assert self._rows(client)[0]["a"]["review_status"] is None

        assert self._decide(client, "run_a", shared, "accepted").status_code == 200
        first = self._rows(client)[0]
        assert first["a"]["review_status"] == "accepted"
        assert first["b"]["review_status"] is None

    def test_the_two_sides_carry_their_own_decisions(self, paired_db):
        client, _ = paired_db
        shared = MAPPINGS_A[0].mapping_id
        self._decide(client, "run_a", shared, "accepted")
        self._decide(client, "run_b", shared, "rejected")
        first = self._rows(client)[0]
        assert (first["a"]["review_status"], first["b"]["review_status"]) == (
            "accepted", "rejected",
        )
        # a decision on an Indicator only Engine A answered stays on that side
        self._decide(client, "run_a", MAPPINGS_A[2].mapping_id, "flagged")
        only_a = [r for r in self._rows(client) if r["indicator_id"] == "7.3"][0]
        assert only_a["a"]["review_status"] == "flagged"
        assert only_a["b"] is None

    def test_the_downloaded_csv_carries_both_sides_decisions(self, paired_db):
        client, _ = paired_db
        shared = MAPPINGS_A[0].mapping_id
        self._decide(client, "run_a", shared, "accepted")
        self._decide(client, "run_b", shared, "flagged")
        r = client.get(
            "/api/compare/download",
            params={"run_a": "run_a", "run_b": "run_b", "format": "csv"},
        )
        all_rows = list(csv.reader(io.StringIO(r.text)))
        table_rows = all_rows[all_rows.index([]) + 1:]
        table = [dict(zip(table_rows[0], row)) for row in table_rows[1:]]
        by_indicator = {row["indicator_id"]: row for row in table}
        assert by_indicator["7.1"]["a_review_status"] == "accepted"
        assert by_indicator["7.1"]["b_review_status"] == "flagged"
        assert by_indicator["7.2"]["a_review_status"] == ""
