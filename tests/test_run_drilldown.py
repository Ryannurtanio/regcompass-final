"""Clicking deeper into a Run: one Document, one Candidate, one Mapping.

The Run's stream says, for every Candidate (a Piece and Indicator pair the
Gate kept), the Gate's two scores for it, the lane it was judged in and what
became of it; and, for every proven Mapping, that it is saved and on which
page its Verbatim Quote sits. A Piece endpoint serves one Piece's text as the
slice of its Document's stored text at the Piece's offsets. Nothing here
changes the database: the Gate's keyword score and the rejected pairs are
never stored, so they live in the events or nowhere.

No network and no paid model: the fake Engine over the committed fixtures.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from regcompass.run_progress import CANDIDATE_OUTCOMES, MAP, PROVE
from regcompass.storage import Storage

from test_run_events import ROOT, _read_stream, _two_document_app, _wait_done


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("drilldown")
    with pytest.MonkeyPatch.context() as mp:
        mp.chdir(ROOT)
        c, db, ids = _two_document_app(tmp_path)
        started = c.post("/api/run", json={"economy": "SG", "pillars": [7], "engine": "fake"})
        assert started.status_code == 200, started.text
        st = _wait_done(c)
        assert st["status"] == "done", st
        _, events, _ = _read_stream(c)
    record = c.get("/api/runs").json()["runs"][0]
    return c, db, ids, events, record


def _of(events, type_, doc=None):
    return [
        e for e in events
        if e["type"] == type_ and (doc is None or e.get("document_id") == doc)
    ]


def _finished(events, step, doc):
    (e,) = [
        e for e in _of(events, "step_finished", doc) if e["step"] == step
    ]
    return e["counts"]


class TestCandidateEvents:
    def test_one_event_per_candidate_the_gate_kept(self, run):
        _, _, ids, events, _ = run
        for doc in ids:
            kept = _finished(events, "gate", doc)["candidates"]
            assert kept > 0, doc
            assert len(_of(events, "candidate", doc)) == kept

    def test_each_carries_its_piece_indicator_scores_lane_and_outcome(self, run):
        _, _, _, events, _ = run
        for e in _of(events, "candidate"):
            assert e["piece_id"].startswith(e["document_id"] + ":")
            assert e["indicator"].startswith("7.")
            assert -1.0 <= e["cosine"] <= 1.0
            assert e["bm25"] >= 0.0
            assert e["lane"] in ("meaning_and_keywords", "meaning_only")
            assert e["outcome"] in CANDIDATE_OUTCOMES
            assert e["section"]
            assert e["page"] is None or e["page"] >= 1

    def test_one_lane_per_document_and_its_keyword_scores_fit_it(self, run):
        _, _, ids, events, _ = run
        for doc in ids:
            mine = _of(events, "candidate", doc)
            (lane,) = {e["lane"] for e in mine}
            if lane == "meaning_and_keywords":
                # A pair kept in this lane always had a keyword match.
                assert all(e["bm25"] > 0 for e in mine)
            else:
                # The keyword tier never ran: no keyword score to show.
                assert all(e["bm25"] == 0 for e in mine)

    def test_the_cosine_is_the_one_the_gate_stored(self, run):
        _, db, _, events, _ = run
        storage = Storage(db)
        try:
            stored = storage.load_gate_scores()
        finally:
            storage.close()
        for e in _of(events, "candidate"):
            assert e["cosine"] == pytest.approx(stored[(e["piece_id"], e["indicator"])], abs=1e-4)

    def test_candidates_are_settled_during_map(self, run):
        _, _, ids, events, _ = run
        for doc in ids:
            mine = [(i, e) for i, e in enumerate(events) if e.get("document_id") == doc]
            start = next(i for i, e in mine if e["type"] == "step_started" and e["step"] == MAP)
            end = next(i for i, e in mine if e["type"] == "step_finished" and e["step"] == MAP)
            assert all(start < i < end for i, e in mine if e["type"] == "candidate")

    def test_outcomes_add_up_to_what_prove_counted(self, run):
        _, _, ids, events, record = run
        for doc in ids:
            prove = _finished(events, "prove", doc)
            outcomes = Counter(e["outcome"] for e in _of(events, "candidate", doc))
            assert outcomes["mapped"] == prove["proven"]
            assert outcomes["not_applicable"] == prove["no_evidence"]
            assert outcomes["dropped_by_proof"] + outcomes["skipped"] == prove["dropped"]
        assert sum(
            1 for e in _of(events, "candidate") if e["outcome"] == "mapped"
        ) == record["details"]["n_passed"]


class TestMappingAddedEvents:
    def test_one_per_proven_mapping_of_each_document(self, run):
        _, _, ids, events, _ = run
        for doc in ids:
            (done,) = _of(events, "document_finished", doc)
            assert len(_of(events, "mapping_added", doc)) == done["mappings"]

    def test_they_are_the_candidates_that_were_mapped(self, run):
        _, _, _, events, _ = run
        mapped = {
            f"{e['piece_id']}::{e['indicator']}"
            for e in _of(events, "candidate") if e["outcome"] == "mapped"
        }
        added = {e["mapping_id"] for e in _of(events, "mapping_added")}
        assert added == mapped
        assert added, "the fake Engine proves at least one Mapping"

    def test_sent_while_the_documents_prove_step_is_open(self, run):
        _, _, ids, events, _ = run
        for doc in ids:
            mine = [(i, e) for i, e in enumerate(events) if e.get("document_id") == doc]
            start = next(i for i, e in mine if e["type"] == "step_started" and e["step"] == PROVE)
            end = next(i for i, e in mine if e["type"] == "step_finished" and e["step"] == PROVE)
            assert all(start < i < end for i, e in mine if e["type"] == "mapping_added")

    def test_each_opens_through_the_record_endpoint_on_its_page(self, run):
        c, _, _, events, record = run
        for e in _of(events, "mapping_added"):
            r = c.get(f"/api/records/{e['mapping_id']}", params={"run_id": record["run_id"]})
            assert r.status_code == 200, r.text
            detail = r.json()
            assert detail["record"]["document_id"] == e["document_id"]
            assert detail["record"]["indicator_id"] == e["indicator"]
            assert detail["record"]["page_number"] == e["page"]

    def test_the_recorded_file_and_the_replay_carry_them_too(self, run):
        c, db, _, events, record = run
        from regcompass.run_events import events_path, read_events

        on_file = read_events(events_path(db, record["run_id"]))
        assert on_file == events
        body = c.get(
            f"/api/runs/{record['run_id']}/replay", params={"speed": 1_000_000}
        ).text
        assert body.count("event: candidate\n") == len(_of(events, "candidate"))
        assert body.count("event: mapping_added\n") == len(_of(events, "mapping_added"))


class TestPieceEndpoint:
    def test_the_text_is_the_documents_stored_text_at_the_offsets(self, run):
        c, db, _, events, _ = run
        storage = Storage(db)
        try:
            for e in _of(events, "candidate")[:12]:
                r = c.get(f"/api/pieces/{e['piece_id']}")
                assert r.status_code == 200, r.text
                piece = r.json()
                row = storage.chunk_row(e["piece_id"])
                full = storage.document_full_text(row["document_id"])
                assert piece["text"] == full[row["char_start"]:row["char_end"]]
                assert piece["text"]
                assert (piece["char_start"], piece["char_end"]) == (
                    row["char_start"], row["char_end"],
                )
                assert piece["document_id"] == e["document_id"]
                assert piece["page"] == e["page"] == row["page_start"]
                assert piece["section"] == e["section"]
                assert piece["document_title"]
                assert piece["format"] == "pdf"
        finally:
            storage.close()

    def test_a_web_page_piece_says_it_is_one(self, run):
        """A web page has no pages: the drill-down reads `format` to drop the
        page wording, so the endpoint has to say which it is."""
        c, db, _, events, _ = run
        piece_id = _of(events, "candidate")[0]["piece_id"]
        storage = Storage(db)
        doc = storage.conn.execute(
            "SELECT document_id, extractor FROM documents WHERE document_id ="
            " (SELECT document_id FROM chunks WHERE chunk_id = ?)",
            (piece_id,),
        ).fetchone()
        try:
            storage.conn.execute(
                "UPDATE documents SET extractor = 'bs4-lxml' WHERE document_id = ?",
                (doc["document_id"],),
            )
            storage.conn.commit()
            assert c.get(f"/api/pieces/{piece_id}").json()["format"] == "html"
            from regcompass.server import _run_documents

            economy = storage.conn.execute(
                "SELECT d.economy FROM documents d JOIN chunks c ON"
                " c.document_id = d.document_id WHERE c.chunk_id = ?",
                (piece_id,),
            ).fetchone()[0]
            formats = {d["format"] for d in _run_documents(storage, economy)}
            assert "html" in formats
        finally:
            # The module's other tests read the same Run: put it back.
            storage.conn.execute(
                "UPDATE documents SET extractor = ? WHERE document_id = ?",
                (doc["extractor"], doc["document_id"]),
            )
            storage.conn.commit()
            storage.close()

    def test_a_mappings_quote_sits_inside_its_piece(self, run):
        c, _, _, events, record = run
        e = _of(events, "mapping_added")[0]
        quote = c.get(
            f"/api/records/{e['mapping_id']}", params={"run_id": record["run_id"]}
        ).json()["record"]["verbatim_quote"]
        piece_id = e["mapping_id"].rsplit("::", 1)[0]
        assert quote in c.get(f"/api/pieces/{piece_id}").json()["text"]

    def test_an_unknown_piece_is_404(self, run):
        c, _, _, _, _ = run
        assert c.get("/api/pieces/doc_nowhere:0").status_code == 404

    def test_no_database_yet_is_404(self, tmp_path: Path):
        from fastapi.testclient import TestClient

        from regcompass.server import create_app

        app = create_app(db_path=tmp_path / "none.db", out_dir=tmp_path, ui_dir=None)
        assert TestClient(app).get("/api/pieces/doc_a:0").status_code == 404


class Candidates:
    """A hook that keeps only what the drill-down reads."""

    def __init__(self) -> None:
        from regcompass.run_progress import RunProgress

        outer = self

        class _Hook(RunProgress):
            def candidate(self, document_id, piece_id, indicator, cosine, bm25, lane,
                          outcome, section=None, page=None):
                outer.outcomes.append(outcome)

            def mapping_added(self, document_id, mapping_id, indicator, page):
                outer.added.append(mapping_id)

        self.hook = _Hook()
        self.outcomes: list[str] = []
        self.added: list[str] = []


def _sg_run(tmp_path, monkeypatch, completion_fn, hook):
    from corpus_fixtures import seed_corpus

    from regcompass.engines import fake_embed, resolve_engine
    from regcompass.pipeline import run_economy

    monkeypatch.chdir(ROOT)
    storage = Storage(tmp_path / "outcomes.db")
    storage.apply_schema()
    data_dir = tmp_path / "data"
    seed_corpus(storage, data_dir, "SG")
    try:
        return run_economy(
            storage, "SG", (7,), resolve_engine("fake"), data_dir=data_dir,
            completion_fn=completion_fn, embed_fn=fake_embed, hook=hook,
            concurrency=1,
        )
    finally:
        storage.close()


class TestEveryOutcomeIsNamed:
    """The outcomes a fake Run never reaches, forced through the Engine call.
    The counters of the Run are the same with or without the hook."""

    def test_quotes_that_cannot_be_found_word_for_word_are_dropped_by_proof(
        self, tmp_path, monkeypatch
    ):
        import json as _json

        def paraphrase(prompt: str, strict: bool) -> str:
            return _json.dumps({
                "maps_to_indicator": True,
                "verbatim_quote": "words that appear nowhere in this law at all",
                "subsection": None,
                "impact": "restricts",
            })

        seen = Candidates()
        report = _sg_run(tmp_path, monkeypatch, paraphrase, seen.hook)
        assert seen.outcomes and set(seen.outcomes) == {"dropped_by_proof"}
        assert len(seen.outcomes) == report.n_pairs_gated == report.n_dropped
        assert seen.added == []

    def test_calls_that_never_come_back_are_skipped(self, tmp_path, monkeypatch):
        import regcompass.pipeline as pipeline

        monkeypatch.setattr(pipeline, "_sleep", lambda s: None)

        def dead_line(prompt: str, strict: bool) -> str:
            raise ConnectionError("the line went dead")

        seen = Candidates()
        report = _sg_run(tmp_path, monkeypatch, dead_line, seen.hook)
        assert seen.outcomes and set(seen.outcomes) == {"skipped"}
        assert len(seen.outcomes) == report.n_pairs_gated == report.n_dropped
