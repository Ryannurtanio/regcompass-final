"""The first replayable Run: Singapore P7 on Engine A, rebuilt from its log.

That Run was made from the command line before the app recorded any events,
so the one-off converter (scripts/convert_run_log.py) turns its text log plus
its Run Record into the same event file a Run started from the app writes. The
result is committed as a fixture and is the free test Run for the Run view.

What the log never had stays empty (the Gate's scores per Candidate) and the
file says so, in its first event, so the screen can say so too.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

import pytest

from regcompass.run_events import EVENT_NAMES, read_events
from regcompass.run_progress import DOCUMENT_STEPS, MAP, RECONCILE

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import convert_run_log  # noqa: E402

RUN_ID = "run_20260923T041241Z_a07249"
FIXTURES = ROOT / "tests" / "fixtures" / "run_events"
LOG = FIXTURES / "SG-P7-engine-a.log"
RECORD = FIXTURES / f"{RUN_ID}.record.json"
EVENTS = FIXTURES / f"{RUN_ID}.jsonl"


@pytest.fixture(scope="module")
def record() -> dict:
    return json.loads(RECORD.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def events() -> list[dict]:
    return read_events(EVENTS)


def _finished(events, step):
    return [e["counts"] for e in events if e["type"] == "step_finished" and e["step"] == step]


class TestTheConverter:
    def test_the_committed_file_is_exactly_what_the_converter_makes(self, record):
        made = convert_run_log.convert(LOG.read_text(encoding="utf-8"), record)
        assert made == read_events(EVENTS)
        assert convert_run_log.to_jsonl(made) == EVENTS.read_text(encoding="utf-8")

    def test_wrapped_lines_are_joined_back(self):
        text = (
            "M4 chunk | doc_x: 188 chunks, style=sg_num_dot, \n"
            "fallback=False (0.0s)\n"
            "M5 gate | doc_x: 76 gate-passed of 920 (chunk, \n"
            "indicator) pairs, pillars (7,) (bge-m3 + bm25, 16.4s)\n"
        )
        lines = convert_run_log.logical_lines(text)
        assert lines == [
            "M4 chunk | doc_x: 188 chunks, style=sg_num_dot, fallback=False (0.0s)",
            "M5 gate | doc_x: 76 gate-passed of 920 (chunk, indicator) pairs,"
            " pillars (7,) (bge-m3 + bm25, 16.4s)",
        ]

    def test_a_log_that_disagrees_with_its_record_is_refused(self, record):
        broken = dict(record, details=dict(record["details"], n_chunks=1))
        with pytest.raises(ValueError, match="Pieces"):
            convert_run_log.convert(LOG.read_text(encoding="utf-8"), broken)

    def test_local_paths_are_taken_out_of_the_log(self):
        raw = (
            "$ /Users/someone/Work Dir/regcompass/.venv/bin/regcompass run --db "
            "/Users/someone/Work Dir/regcompass/data/regcompass.db\n"
        )
        clean = convert_run_log.sanitize_log(raw)
        assert "/Users/" not in clean
        assert clean == "$ <repo>/.venv/bin/regcompass run --db <repo>/data/regcompass.db\n"


class TestTheConvertedRun:
    def test_it_is_a_well_formed_event_file(self, events):
        assert [e["seq"] for e in events] == list(range(len(events)))
        assert all(e["type"] in EVENT_NAMES for e in events)
        assert events[0]["type"] == "run_started"
        assert events[-1]["type"] == "run_finished"
        stamps = [datetime.fromisoformat(e["ts"]) for e in events]
        assert all(t.tzinfo is not None for t in stamps)
        assert stamps == sorted(stamps)

    def test_it_lies_inside_the_run_records_own_times(self, events, record):
        start = datetime.fromisoformat(record["started_at"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(record["ended_at"].replace("Z", "+00:00"))
        assert datetime.fromisoformat(events[0]["ts"]) == start
        assert datetime.fromisoformat(events[-1]["ts"]) == end

    def test_it_names_the_run_and_its_ten_documents(self, events, record):
        first = events[0]
        assert first["run_id"] == RUN_ID == record["run_id"]
        assert first["economy"] == "SG"
        assert first["pillars"] == [7]
        assert first["engine"] == "engine-a"
        docs = first["documents"]
        assert [d["document_id"] for d in docs] == record["details"]["documents"]
        assert len(docs) == 10
        for d in docs:
            assert d["title"] and d["n_pages"] > 0

    def test_every_document_walks_every_step_in_the_live_order(self, events):
        ids = [d["document_id"] for d in events[0]["documents"]]
        shape = [
            (e["type"], e.get("document_id"), e.get("step"))
            for e in events[1:-1] if e["type"] != "map_progress"
        ]
        expected = []
        for doc in ids:
            for step in DOCUMENT_STEPS:
                expected += [("step_started", doc, step), ("step_finished", doc, step)]
            expected.append(("document_finished", doc, None))
        expected += [("step_started", None, RECONCILE), ("step_finished", None, RECONCILE)]
        assert shape == expected

    def test_map_ticks_keep_the_live_cadence(self, events):
        for doc in [d["document_id"] for d in events[0]["documents"]]:
            ticks = [e for e in events if e["type"] == "map_progress" and e["document_id"] == doc]
            total = ticks[-1]["total"]
            assert [e["done"] for e in ticks] == sorted({*range(25, total + 1, 25), total})
            (mapped,) = [
                c for e in events if e["type"] == "step_finished"
                and e["document_id"] == doc and e["step"] == MAP for c in [e["counts"]]
            ]
            assert mapped == {"done": total, "total": total}

    def test_its_totals_equal_the_run_records(self, events, record):
        details = record["details"]
        assert sum(c["pieces"] for c in _finished(events, "cut")) == details["n_chunks"] == 2072
        assert sum(c["candidates"] for c in _finished(events, "gate")) == details["n_pairs_gated"] == 870
        assert sum(c["pairs"] for c in _finished(events, "gate")) == 10120
        assert sum(c["proven"] for c in _finished(events, "prove")) == details["n_passed"] == 148
        assert sum(c["no_evidence"] for c in _finished(events, "prove")) == details["n_no_evidence"] == 713
        assert sum(c["dropped"] for c in _finished(events, "prove")) == details["n_dropped"] == 9
        assert sum(c["glossed"] for c in _finished(events, "gloss")) == details["n_glossed"] == 0
        (reconcile,) = _finished(events, RECONCILE)
        assert reconcile["groups"] == details["n_groups"] == 5

        final = events[-1]
        assert final["status"] == record["status"] == "completed"
        assert final["totals"] == {
            "documents": 10,
            "pieces": 2072,
            "pairs": 10120,
            "candidates": 870,
            "proven": 148,
            "no_evidence": 713,
            "dropped": 9,
            "glossed": 0,
            "groups": 5,
        }
        assert final["cost_usd"] == pytest.approx(record["cost_usd"])

    def test_its_engine_calls_are_the_run_records(self, events, record):
        assert events[-1]["engine_calls"] == record["details"]["model_calls"] == 913

    def test_its_provider_bill_is_the_run_records(self, events, record):
        assert events[-1]["provider_cost_usd"] == pytest.approx(record["provider_cost_usd"])

    def test_each_documents_mappings_equal_the_stored_mappings(self, events, record):
        done = {e["document_id"]: e["mappings"] for e in events if e["type"] == "document_finished"}
        assert done == record["mappings_by_document"]
        assert sum(done.values()) == 148

    def test_what_the_log_never_had_is_named_not_invented(self, events):
        first = events[0]
        assert first["recorded_from"] == "log"
        assert "gate_scores" in first["not_recorded"]
        assert "step_times" in first["not_recorded"]
        # No Candidate is invented: the log only ever counted them.
        assert not [e for e in events if e["type"] == "candidate"]


class TestTheFixturesAreSafeToCommit:
    @pytest.mark.parametrize("path", [LOG, RECORD, EVENTS], ids=lambda p: p.name)
    def test_no_local_paths_and_no_keys(self, path):
        text = path.read_text(encoding="utf-8")
        assert "/Users/" not in text and "/home/" not in text
        assert not re.search(r"sk-[A-Za-z0-9-]{12,}", text)
        assert "OPENROUTER" not in text.upper()
