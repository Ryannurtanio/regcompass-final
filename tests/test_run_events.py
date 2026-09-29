"""The Run's typed events: on the live stream, in a file per Run, on the record.

The free-text lines stay exactly as they were (tests/test_run_progress.py pins
them by snapshot). Beside them the stream carries named events a program can
follow: which Document is on which Step, with what counts, how the Run ended.
Every Run started from the app records the same events, in order, to a file
named after its Run id, and the Run Record points at that file.

No network and no paid model: every Run goes through the fake Engine over a
Corpus seeded from the committed fixture Documents.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from regcompass.run_events import EVENT_NAMES, events_path
from regcompass.run_progress import DOCUMENT_STEPS, MAP, RECONCILE
from regcompass.server import create_app
from regcompass.storage import Storage

from corpus_fixtures import BUNDLED, seed_corpus  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _two_document_app(tmp_path: Path):
    """SG's Document plus MY's bytes filed under SG: two Documents, one Run."""
    from regcompass.corpus import add_document

    db = tmp_path / "web.db"
    data = tmp_path / "data"
    out = tmp_path / "out"
    out.mkdir()
    storage = Storage(db)
    storage.apply_schema()
    seed_corpus(storage, data, "SG")
    my = BUNDLED["MY"][0]
    add_document(
        storage, data, "SG", my.path.read_bytes(), source_url=my.source_url,
        language="en", filename_hint=my.filename_hint,
    )
    ids = [r["document_id"] for r in storage.corpus_documents("SG")]
    storage.conn.close()
    assert len(ids) == 2
    app = create_app(db_path=db, out_dir=out, data_dir=data, ui_dir=None)
    return TestClient(app), db, ids


def _wait_done(client, timeout=240.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = client.get("/api/status").json()
        if not st["active"] and st["status"] in ("done", "error"):
            return st
        time.sleep(0.02)
    raise AssertionError("the job did not finish in time")


def _read_stream(client) -> tuple[list[str], list[dict], list[tuple[str, dict]]]:
    """The whole stream, split the way a browser's EventSource splits it:
    unnamed messages (the text lines), and named events with their payloads."""
    text_lines: list[str] = []
    typed: list[dict] = []
    other: list[tuple[str, dict]] = []
    name: str | None = None
    with client.stream("GET", "/api/events") as resp:
        for line in resp.iter_lines():
            if line.startswith("event: "):
                name = line[len("event: "):]
                continue
            if line.startswith("data: "):
                obj = json.loads(line[len("data: "):])
                if name is None:
                    text_lines.append(obj["msg"])
                elif name in EVENT_NAMES:
                    assert obj["type"] == name
                    typed.append(obj)
                else:
                    other.append((name, obj))
                continue
            if line == "":
                name = None
    return text_lines, typed, other


@pytest.fixture(scope="module")
def two_document_run(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("events")
    with pytest.MonkeyPatch.context() as mp:
        mp.chdir(ROOT)
        c, db, ids = _two_document_app(tmp_path)
        started = c.post("/api/run", json={"economy": "SG", "pillars": [7], "engine": "fake"})
        assert started.status_code == 200, started.text
        st = _wait_done(c)
        assert st["status"] == "done", st
        text_lines, events, other = _read_stream(c)
    record = c.get("/api/runs").json()["runs"][0]
    return c, db, ids, text_lines, events, other, record


class TestTheTypedEventsOfARun:
    def test_every_event_has_a_type_a_time_and_a_rising_sequence_number(
        self, two_document_run
    ):
        _, _, _, _, events, _, _ = two_document_run
        assert [e["seq"] for e in events] == list(range(len(events)))
        for e in events:
            assert datetime.fromisoformat(e["ts"]).tzinfo is not None
        assert all(e["type"] in EVENT_NAMES for e in events)
        assert all("msg" not in e for e in events), "old readers take `msg` as a line"

    def test_the_full_order_for_a_small_corpus(self, two_document_run):
        _, _, ids, _, events, _, record = two_document_run
        first, last = events[0], events[-1]
        assert first["type"] == "run_started"
        assert first["run_id"] == record["run_id"]
        assert first["economy"] == "SG"
        assert first["pillars"] == [7]
        assert first["engine"] == "fake"
        assert [d["document_id"] for d in first["documents"]] == ids
        for d in first["documents"]:
            assert d["title"] and d["language"]
            assert d["n_pages"] is None or d["n_pages"] > 0
        assert last["type"] == "run_finished"

        # Without Map's ticks and the per-Candidate and per-Mapping detail
        # (tests/test_run_drilldown.py): every Document walks every Step in
        # order, is finished, and then Reconcile runs once for the whole Run.
        shape = [
            (e["type"], e.get("document_id"), e.get("step"))
            for e in events[1:-1]
            if e["type"] not in ("map_progress", "candidate", "mapping_added")
        ]
        expected = []
        for doc in ids:
            for step in DOCUMENT_STEPS:
                expected += [("step_started", doc, step), ("step_finished", doc, step)]
            expected.append(("document_finished", doc, None))
        expected += [
            ("step_started", None, RECONCILE), ("step_finished", None, RECONCILE),
            ("reconcile", None, None),
        ]
        assert shape == expected

    def test_map_ticks_fall_inside_their_documents_map_step(self, two_document_run):
        _, _, ids, _, events, _, _ = two_document_run
        for doc in ids:
            mine = [
                (i, e) for i, e in enumerate(events) if e.get("document_id") == doc
            ]
            start = next(i for i, e in mine if e["type"] == "step_started" and e["step"] == MAP)
            end = next(i for i, e in mine if e["type"] == "step_finished" and e["step"] == MAP)
            ticks = [(i, e) for i, e in mine if e["type"] == "map_progress"]
            assert ticks, doc
            assert all(start < i < end for i, _ in ticks)
            done = [e["done"] for _, e in ticks]
            total = ticks[-1][1]["total"]
            assert done == sorted(done)
            assert done[-1] == total
            # The text lines' cadence: every 25 pairs, and the last one.
            assert all(d % 25 == 0 or d == total for d in done)
            assert done == sorted({*range(25, total + 1, 25), total})

    def test_the_counts_add_up_to_the_run_records_totals(self, two_document_run):
        _, _, _, _, events, _, record = two_document_run
        details = record["details"]

        def finished(step):
            return [e["counts"] for e in events
                    if e["type"] == "step_finished" and e["step"] == step]

        assert sum(c["pieces"] for c in finished("cut")) == details["n_chunks"]
        assert sum(c["pairs"] for c in finished("gate")) == details["n_pairs_considered"]
        assert sum(c["candidates"] for c in finished("gate")) == details["n_pairs_gated"]
        assert sum(c["proven"] for c in finished("prove")) == details["n_passed"]
        assert sum(c["no_evidence"] for c in finished("prove")) == details["n_no_evidence"]
        assert sum(c["dropped"] for c in finished("prove")) == details["n_dropped"]
        assert sum(c["glossed"] for c in finished("gloss")) == details["n_glossed"]
        (reconcile,) = finished(RECONCILE)
        assert reconcile["groups"] == details["n_groups"]

        done_docs = [e for e in events if e["type"] == "document_finished"]
        proven = {
            e["document_id"]: e["counts"]["proven"]
            for e in events if e["type"] == "step_finished" and e["step"] == "prove"
        }
        assert {e["document_id"]: e["mappings"] for e in done_docs} == proven
        assert sum(e["mappings"] for e in done_docs) == details["n_passed"]

        final = events[-1]
        assert final["status"] == "completed" == record["status"]
        assert final["totals"] == {
            "documents": len(details["documents"]),
            "pieces": details["n_chunks"],
            "pairs": details["n_pairs_considered"],
            "candidates": details["n_pairs_gated"],
            "proven": details["n_passed"],
            "no_evidence": details["n_no_evidence"],
            "dropped": details["n_dropped"],
            "glossed": details["n_glossed"],
            "groups": details["n_groups"],
        }
        assert final["cost_usd"] == record["cost_usd"]

    def test_the_text_lines_are_still_there_beside_them(self, two_document_run):
        _, _, _, text_lines, _, other, _ = two_document_run
        assert text_lines[0].startswith("M0 start")
        assert any(line.startswith("M9 done") for line in text_lines)
        assert [name for name, _ in other] == ["end"], "the stream still ends as before"


class TestTheEventFile:
    def test_the_run_writes_its_events_in_order_to_a_file_named_after_it(
        self, two_document_run
    ):
        _, db, _, _, events, _, record = two_document_run
        path = events_path(db, record["run_id"])
        assert path.parent == Path(db).parent / "run_events"
        assert path.name == f"{record['run_id']}.jsonl"
        on_disk = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
        assert on_disk == events
        assert on_disk[-1]["type"] == "run_finished", "closed by the final event"

    def test_the_run_record_points_to_it(self, two_document_run):
        c, db, _, _, _, _, record = two_document_run
        assert record["events_file"] == f"run_events/{record['run_id']}.jsonl"
        assert (Path(db).parent / record["events_file"]).is_file()
        one = c.get(f"/api/runs/{record['run_id']}").json()
        assert one["events_file"] == record["events_file"]

    def test_a_run_record_with_no_event_file_says_so(self, two_document_run):
        c, db, _, _, _, _, record = two_document_run
        events_path(db, record["run_id"]).rename(Path(db).parent / "moved.jsonl")
        try:
            assert c.get(f"/api/runs/{record['run_id']}").json()["events_file"] is None
        finally:
            (Path(db).parent / "moved.jsonl").rename(events_path(db, record["run_id"]))

    def test_the_progress_log_hands_back_the_events_for_a_page_reload(
        self, two_document_run
    ):
        c, _, _, _, events, _, _ = two_document_run
        body = c.get("/api/run/log").json()
        assert body["events"] == events


class TestAFailureIsAnEvent:
    def test_an_injected_failure_names_the_document_and_the_step(
        self, tmp_path, monkeypatch
    ):
        import regcompass.pipeline as pipeline_mod

        monkeypatch.chdir(ROOT)
        c, db, ids = _two_document_app(tmp_path)

        def boom(*args, **kwargs):
            raise RuntimeError("the embedder fell over")

        monkeypatch.setattr(pipeline_mod, "gate_document", boom)
        started = c.post("/api/run", json={"economy": "SG", "pillars": [7], "engine": "fake"})
        assert started.status_code == 200, started.text
        st = _wait_done(c)
        assert st["status"] == "error", st

        text_lines, events, other = _read_stream(c)
        assert any(line.startswith("ERROR | RuntimeError") for line in text_lines)
        assert events[0]["type"] == "run_started"
        failures = [e for e in events if e["type"] == "run_failed"]
        assert failures == [events[-1]], "one failure, and it ends the Run"
        assert failures[0]["document_id"] == ids[0]
        assert failures[0]["step"] == "gate"
        assert failures[0]["message"] == "RuntimeError: the embedder fell over"
        assert not [e for e in events if e["type"] == "run_finished"]

        record = c.get("/api/runs").json()["runs"][0]
        assert record["status"] == "failed"
        on_disk = [
            json.loads(line)
            for line in events_path(db, record["run_id"]).read_text("utf-8").splitlines()
        ]
        assert on_disk == events, "the file is closed by run_failed"
        assert record["events_file"] == f"run_events/{record['run_id']}.jsonl"

    def test_a_failure_before_the_first_step_names_neither(self, tmp_path, monkeypatch):
        import regcompass.pipeline as pipeline_mod

        monkeypatch.chdir(ROOT)
        c, _, _ = _two_document_app(tmp_path)

        def boom(*args, **kwargs):
            raise OSError("the disk went away")

        monkeypatch.setattr(pipeline_mod, "corpus_for_run", boom)
        assert c.post(
            "/api/run", json={"economy": "SG", "pillars": [7], "engine": "fake"}
        ).status_code == 200
        assert _wait_done(c)["status"] == "error"
        _, events, _ = _read_stream(c)
        assert events[-1]["type"] == "run_failed"
        assert events[-1]["document_id"] is None and events[-1]["step"] is None
        assert events[-1]["message"] == "OSError: the disk went away"
        assert len([e for e in events if e["type"] == "run_failed"]) == 1


    def test_a_database_that_fails_to_open_still_lists_the_documents(
        self, tmp_path, monkeypatch
    ):
        import regcompass.server as server_mod

        c, _, ids = _two_document_app(tmp_path)
        real_open = server_mod._open_for_write
        calls = {"n": 0}

        def open_once(db_path):
            # The endpoint's opening of the Run Record succeeds; the worker's
            # own opening of the database is the one that fails.
            calls["n"] += 1
            if calls["n"] > 1:
                raise OSError("the database is locked")
            return real_open(db_path)

        monkeypatch.setattr(server_mod, "_open_for_write", open_once)
        assert c.post(
            "/api/run", json={"economy": "SG", "pillars": [7], "engine": "fake"}
        ).status_code == 200
        assert _wait_done(c)["status"] == "error"
        _, events, _ = _read_stream(c)
        assert [e["type"] for e in events] == ["run_started", "run_failed"]
        assert [d["document_id"] for d in events[0]["documents"]] == ids
        assert events[1]["message"] == "OSError: the database is locked"


class TestMapProgressKeepsTheTextCadence:
    def test_the_bar_moves_every_pair_but_an_event_goes_every_25(self):
        from regcompass.server import RunManager, RunTelemetry

        manager = RunManager()
        manager._telemetry = RunTelemetry()
        for done in range(1, 61):
            manager.map_progress("doc_a", done, 60)
        assert manager._telemetry.pairs_done == 60
        assert [e["done"] for e in manager.events_since(0)] == [25, 50, 60]


class TestALateTabSeesTheSamePicture:
    def test_a_stream_opened_mid_run_replays_every_event_so_far(self):
        """A reload in the middle of a Run opens a fresh stream: it must start
        from the Run's first event, not from whatever happens next."""
        from regcompass.server import RunManager, _sse

        manager = RunManager()
        halfway = threading.Event()
        release = threading.Event()

        def worker(m):
            m.emit("run_started", run_id="run_x", economy="SG", pillars=[7],
                   indicators=None, engine="fake",
                   documents=[{"document_id": "doc_a", "title": "A",
                               "language": "en", "n_pages": 3}])
            m.step_started("doc_a", "read")
            m.step_finished("doc_a", "read", {"pages": 3, "chars": 10})
            halfway.set()
            release.wait(5)
            m.step_started("doc_a", "scan_check")
            m.emit("run_finished", status="completed", totals={}, cost_usd=0.0)
            m._finish("done")

        assert manager.start(worker, {"economy": "SG", "engine": "fake"})
        assert halfway.wait(5)

        seen: list[dict] = []
        name = None
        for chunk in _sse(manager):
            for line in chunk.split("\n"):
                if line.startswith("event: "):
                    name = line[len("event: "):]
                elif line.startswith("data: ") and name in EVENT_NAMES:
                    seen.append(json.loads(line[len("data: "):]))
                elif line == "":
                    name = None
            if len(seen) == 3:
                release.set()
        assert [e["type"] for e in seen] == [
            "run_started", "step_started", "step_finished", "step_started",
            "run_finished",
        ]
        assert [e["seq"] for e in seen] == [0, 1, 2, 3, 4]

    def test_a_new_run_starts_its_events_from_nothing(self):
        from regcompass.server import RunManager

        manager = RunManager()
        done = threading.Event()

        def worker(m):
            m.step_started("doc_a", "read")
            m._finish("done")
            done.set()

        assert manager.start(worker, {"economy": "SG"})
        assert done.wait(5)
        assert [e["seq"] for e in manager.events_since(0)] == [0]
        done.clear()
        while manager.status()["active"]:
            time.sleep(0.01)
        assert manager.start(worker, {"economy": "SG"})
        assert done.wait(5)
        assert [e["seq"] for e in manager.events_since(0)] == [0]


class TestReconcileIsAnEvent:
    def test_one_reconcile_event_says_how_many_mappings_became_how_many_groups(
        self, two_document_run
    ):
        _, _, _, _, events, _, record = two_document_run
        details = record["details"]
        (event,) = [e for e in events if e["type"] == "reconcile"]
        (counts,) = [
            e["counts"] for e in events
            if e["type"] == "step_finished" and e["step"] == RECONCILE
        ]
        assert event["before"] == counts["passed"] == details["n_passed"]
        assert event["after"] == counts["groups"] == details["n_groups"]
        # Right after Reconcile finishes, and before the Run does.
        i = events.index(event)
        assert events[i - 1]["type"] == "step_finished"
        assert events[i - 1]["step"] == RECONCILE
        assert events[i + 1]["type"] == "run_finished"


class TestTheRunsOwnMeterIsOnTheStream:
    METERED = ("map_progress", "document_finished", "reconcile", "run_finished")

    def test_cost_so_far_and_engine_calls_ride_on_the_run_s_events(
        self, two_document_run
    ):
        _, _, _, _, events, _, record = two_document_run
        metered = [e for e in events if e["type"] in self.METERED]
        assert {e["type"] for e in metered} == set(self.METERED)
        for e in metered:
            assert isinstance(e["engine_calls"], int) and e["engine_calls"] >= 0
            assert isinstance(e["cost_usd"], float) and e["cost_usd"] >= 0.0
        calls = [e["engine_calls"] for e in metered]
        costs = [e["cost_usd"] for e in metered]
        assert calls == sorted(calls), "the meter only grows"
        assert costs == sorted(costs)
        final = events[-1]
        assert final["type"] == "run_finished"
        # The last reading is the Run Record's own: the same meter closed it.
        assert final["engine_calls"] == record["details"]["model_calls"] > 0
        assert final["cost_usd"] == record["cost_usd"]

    def test_run_finished_carries_the_provider_s_own_bill(self, two_document_run):
        _, _, _, _, events, _, record = two_document_run
        final = events[-1]
        # The fake Engine sends no bill, so it is None, as on the Run Record.
        assert "provider_cost_usd" in final
        assert final["provider_cost_usd"] == record["provider_cost_usd"]

    def test_the_provider_bill_is_passed_on_when_there_is_one(self):
        from types import SimpleNamespace

        from regcompass.server import RunManager

        manager = RunManager()
        manager.finish_run(SimpleNamespace(cost_usd=0.58, provider_cost_usd=0.66))
        (event,) = manager.events_since(0)
        assert event["cost_usd"] == 0.58 and event["provider_cost_usd"] == 0.66

    def test_a_manager_with_no_meter_yet_reads_zero(self):
        from regcompass.server import RunManager, RunTelemetry

        manager = RunManager()
        manager._telemetry = RunTelemetry()
        manager.map_progress("doc_a", 25, 25)
        (tick,) = manager.events_since(0)
        assert tick["engine_calls"] == 0 and tick["cost_usd"] == 0.0

    def test_the_meter_is_read_live_as_it_grows(self):
        from regcompass.engines import UsageTotals
        from regcompass.server import RunManager, RunTelemetry

        manager = RunManager()
        manager._telemetry = RunTelemetry()
        meter = UsageTotals()
        manager.attach_meter(meter)
        meter.calls, meter.prompt_tokens = 3, 1000
        manager.map_progress("doc_a", 25, 50)
        meter.calls, meter.prompt_tokens = 7, 3000
        manager.map_progress("doc_a", 50, 50)
        assert [e["engine_calls"] for e in manager.events_since(0)] == [3, 7]


class TestAFlaggedScanIsAnEvent:
    def test_a_scan_read_with_low_confidence_is_flagged_and_still_mapped(
        self, tmp_path, monkeypatch
    ):
        import regcompass.pipeline as pipeline_mod
        from regcompass.contracts import OcrQuality

        monkeypatch.chdir(ROOT)
        c, _, ids = _two_document_app(tmp_path)
        real = pipeline_mod.extract_with_stats

        def poorly_read(raw, fmt, doc_id):
            canonical, stats = real(raw, fmt, doc_id)
            if doc_id != ids[0]:
                return canonical, stats
            quality = OcrQuality(mean_word_confidence=0.41, manual_review=True)
            return canonical.model_copy(update={"ocr_quality": quality}), stats

        real_load = pipeline_mod.load_extraction

        def poorly_stored(storage, key, doc_id):
            canonical = real_load(storage, key, doc_id)
            if canonical is None or doc_id != ids[0]:
                return canonical
            quality = OcrQuality(mean_word_confidence=0.41, manual_review=True)
            return canonical.model_copy(update={"ocr_quality": quality})

        # Whether the Run reads the file afresh or reuses the Corpus's stored
        # text, the first Document comes back as a poorly read scan.
        monkeypatch.setattr(pipeline_mod, "extract_with_stats", poorly_read)
        monkeypatch.setattr(pipeline_mod, "load_extraction", poorly_stored)
        assert c.post(
            "/api/run", json={"economy": "SG", "pillars": [7], "engine": "fake"}
        ).status_code == 200
        assert _wait_done(c)["status"] == "done"
        _, events, _ = _read_stream(c)

        flags = [e for e in events if e["type"] == "scan_flagged"]
        assert [f["document_id"] for f in flags] == [ids[0]]
        assert "41%" in flags[0]["reason"]
        # Straight after its Scan check, and the Document goes on to Cut.
        i = events.index(flags[0])
        assert (events[i - 1]["type"], events[i - 1]["step"]) == ("step_finished", "scan_check")
        assert (events[i + 1]["type"], events[i + 1]["step"]) == ("step_started", "cut")
        assert [e["document_id"] for e in events if e["type"] == "document_finished"] == ids

    def test_the_manager_turns_the_hook_call_into_an_event(self):
        from regcompass.server import RunManager

        manager = RunManager()
        manager.scan_flagged("doc_a", "Read by OCR with 41% word confidence")
        (event,) = manager.events_since(0)
        assert event["type"] == "scan_flagged"
        assert event["document_id"] == "doc_a"
        assert event["reason"] == "Read by OCR with 41% word confidence"
