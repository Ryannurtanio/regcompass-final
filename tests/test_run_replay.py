"""Watch a Run again: a recorded Run's events streamed back in the live format.

The replay endpoint reads a Run's event file and sends each event exactly as
the live stream does (a named Server-Sent Event), paced by the recorded times
at a chosen speed, with a cap so a long gap never stalls the screen. Pausing is
closing the stream; resuming asks for the rest from the next sequence number.

The recorded Run used here is the converted Singapore P7 Run on Engine A
(tests/fixtures/run_events), so nothing runs and nothing is paid for.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from regcompass.run_events import (
    MAX_REPLAY_GAP_S,
    events_path,
    read_events,
    replay_delays,
    replay_stream,
)
from regcompass.server import create_app
from regcompass.storage import Storage

ROOT = Path(__file__).resolve().parents[1]
RUN_ID = "run_20260923T041241Z_a07249"
FIXTURE = ROOT / "tests" / "fixtures" / "run_events" / f"{RUN_ID}.jsonl"
RECORD = ROOT / "tests" / "fixtures" / "run_events" / f"{RUN_ID}.record.json"


def _read_replay(client, url) -> tuple[list[dict], list[tuple[str, dict]], list[str]]:
    """The stream as a browser splits it: named typed events, other named
    events, and unnamed messages."""
    typed: list[dict] = []
    other: list[tuple[str, dict]] = []
    unnamed: list[str] = []
    name = None
    with client.stream("GET", url) as resp:
        assert resp.status_code == 200, resp.read()
        assert resp.headers["content-type"].startswith("text/event-stream")
        for line in resp.iter_lines():
            if line.startswith("event: "):
                name = line[len("event: "):]
            elif line.startswith("data: "):
                obj = json.loads(line[len("data: "):])
                if name is None:
                    unnamed.append(obj)
                elif name == "end":
                    other.append((name, obj))
                else:
                    assert obj["type"] == name
                    typed.append(obj)
            elif line == "":
                name = None
    return typed, other, unnamed


def _insert_record(db: Path) -> None:
    """The Run Record of the converted Run, filed in a fresh database."""
    record = json.loads(RECORD.read_text(encoding="utf-8"))
    storage = Storage(db)
    storage.apply_schema()
    storage.conn.close()
    conn = sqlite3.connect(db)
    columns = [r[1] for r in conn.execute("PRAGMA table_info(runs)")]
    row = {k: record.get(k) for k in columns}
    for key in ("pillars", "indicators", "details"):
        if row.get(key) is not None:
            row[key] = json.dumps(row[key])
    conn.execute(
        f"INSERT INTO runs ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
        [row[k] for k in columns],
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def app(tmp_path):
    db = tmp_path / "web.db"
    _insert_record(db)
    target = events_path(db, RUN_ID)
    target.parent.mkdir(parents=True)
    shutil.copy(FIXTURE, target)
    out = tmp_path / "out"
    out.mkdir()
    return TestClient(create_app(db_path=db, out_dir=out, data_dir=tmp_path / "data", ui_dir=None)), db


class TestTheReplayEndpoint:
    def test_the_replayed_sequence_equals_the_recorded_one(self, app):
        client, _ = app
        typed, other, unnamed = _read_replay(client, f"/api/runs/{RUN_ID}/replay?speed=100000")
        assert typed == read_events(FIXTURE)
        # The same end marker the live stream sends, saying it was a replay.
        assert [name for name, _ in other] == ["end"]
        assert other[0][1]["replay"] is True
        assert other[0][1]["run_id"] == RUN_ID
        assert unnamed == []

    def test_resuming_sends_the_rest_from_the_next_sequence_number(self, app):
        client, _ = app
        recorded = read_events(FIXTURE)
        typed, _, _ = _read_replay(client, f"/api/runs/{RUN_ID}/replay?speed=100000&from_seq=40")
        assert typed == recorded[40:]
        typed, _, _ = _read_replay(
            client, f"/api/runs/{RUN_ID}/replay?speed=100000&from_seq={len(recorded)}"
        )
        assert typed == []

    def test_the_runs_list_names_the_event_file_so_the_screen_offers_watch_again(self, app):
        client, db = app
        (record,) = client.get("/api/runs").json()["runs"]
        assert record["events_file"] == f"run_events/{RUN_ID}.jsonl"
        events_path(db, RUN_ID).unlink()
        (record,) = client.get("/api/runs").json()["runs"]
        assert record["events_file"] is None

    def test_a_run_with_no_event_file_cannot_be_watched_again(self, app):
        client, db = app
        events_path(db, RUN_ID).unlink()
        assert client.get(f"/api/runs/{RUN_ID}/replay").status_code == 404
        assert client.get("/api/runs/run_nobody/replay").status_code == 404

    @pytest.mark.parametrize("speed", ["0", "-2", "nan", "inf", "1000001"])
    def test_a_speed_that_is_not_a_speed_is_refused(self, app, speed):
        client, _ = app
        assert client.get(f"/api/runs/{RUN_ID}/replay?speed={speed}").status_code in (400, 422)

    def test_a_negative_start_is_refused(self, app):
        client, _ = app
        assert client.get(f"/api/runs/{RUN_ID}/replay?from_seq=-1").status_code in (400, 422)


def _ev(seq: int, ts: str) -> dict:
    return {"type": "step_started", "seq": seq, "ts": ts, "document_id": "d", "step": "read"}


class TestThePace:
    def test_gaps_are_the_recorded_ones_divided_by_the_speed(self):
        events = [
            _ev(0, "2026-09-23T04:00:00+00:00"),
            _ev(1, "2026-09-23T04:00:10+00:00"),
            _ev(2, "2026-09-23T04:00:11+00:00"),
        ]
        assert replay_delays(events, speed=10) == pytest.approx([0.0, 1.0, 0.1])

    def test_a_long_gap_is_capped_so_the_screen_never_stalls(self):
        events = [_ev(0, "2026-09-23T04:00:00Z"), _ev(1, "2026-09-23T05:00:00Z")]
        assert replay_delays(events, speed=2) == [0.0, MAX_REPLAY_GAP_S]

    def test_times_that_run_backwards_or_cannot_be_read_wait_nothing(self):
        events = [
            _ev(0, "2026-09-23T04:00:10Z"),
            _ev(1, "2026-09-23T04:00:00Z"),
            _ev(2, "not a time"),
        ]
        assert replay_delays(events, speed=1) == [0.0, 0.0, 0.0]

    def test_the_stream_waits_before_each_event_and_sends_it_unchanged(self):
        events = [_ev(0, "2026-09-23T04:00:00Z"), _ev(1, "2026-09-23T04:00:02Z")]
        waited: list[float] = []
        chunks = list(replay_stream(events, speed=2, sleep=waited.append, run_id="r"))
        assert waited == [pytest.approx(1.0)]
        typed = [c for c in chunks if c.startswith("event: step_started")]
        assert [json.loads(c.split("data: ", 1)[1]) for c in typed] == events
        assert chunks[-1].startswith("event: end\n")
