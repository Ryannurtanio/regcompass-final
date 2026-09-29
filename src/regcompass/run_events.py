"""The Run's typed events: their names, their SSE form and their file.

The server's Run manager turns the pipeline's progress hook
(regcompass.run_progress) into these events. Each one is a flat JSON object
with its `type` (one of EVENT_NAMES), a `seq` that counts up from 0 within one
Run, a `ts` in UTC, and the fields of its kind:

    run_started       run_id, economy, pillars, indicators, engine,
                      documents: [{document_id, title, language, n_pages}]
    step_started      document_id, step
    step_finished     document_id, step, counts
    map_progress      document_id, done, total, engine_calls, cost_usd
    scan_flagged      document_id, reason
    document_finished document_id, mappings, engine_calls, cost_usd
    reconcile         before, after, engine_calls, cost_usd
    run_finished      status, totals, cost_usd, provider_cost_usd, engine_calls
    run_failed        document_id, step, message
    candidate         document_id, piece_id, indicator, cosine, bm25, lane,
                      outcome, section, page
    mapping_added     document_id, mapping_id, indicator, page

A candidate event settles one Candidate (a Piece and Indicator pair the Gate
kept) during Map, in Candidate order: cosine is the Piece's closeness of
meaning to the Pillar, bm25 its keyword score for the Indicator (0.0 in the
meaning_only lane, where the keyword tier does not apply), lane is
meaning_and_keywords or meaning_only, outcome is mapped, not_applicable,
dropped_by_proof or skipped (see regcompass.run_progress). A mapped
Candidate's Mapping id is "<piece_id>::<indicator>". mapping_added says a
proven Mapping is saved and can be opened through the record endpoints; page
is the page its Verbatim Quote is on. Pieces the Gate did not keep have no
event: a screen shows them only as the Gate's counts.

engine_calls and cost_usd are the Run's own meter at that moment (calls to the
Engine so far, and their cost at the Engine's declared prices), so a screen
can show the spend growing while the Run goes. reconcile says how many proven
Mappings Reconcile read (before) and how many groups it made of them (after).

A Run's first event may also say it was rebuilt from an older record
(`recorded_from`, and `not_recorded`: what that record never kept, such as
the Gate's scores per Candidate), so a screen can say what it cannot show.

document_id is None for Reconcile, which belongs to the whole Run. On the live
stream each event is a NAMED event (`event: <type>`), so a reader that only
listens for unnamed messages, the free-text lines, never sees one. In a file
each event is one JSON line, in order, and the file ends with the final event.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import IO

EVENT_NAMES = (
    "run_started",
    "step_started",
    "step_finished",
    "map_progress",
    "scan_flagged",
    "document_finished",
    "reconcile",
    "run_finished",
    "run_failed",
    "candidate",
    "mapping_added",
)
FINAL_EVENTS = frozenset({"run_finished", "run_failed"})

# Beside the working database, which is where every other record of a Run
# lives: the Run Record, its Mappings, its audit rows.
EVENTS_DIR = "run_events"


def events_file_name(run_id: str) -> str:
    """The file's name relative to the working database's directory, which is
    what a Run Record carries."""
    return f"{EVENTS_DIR}/{run_id}.jsonl"


def events_path(db_path: str | Path, run_id: str) -> Path:
    return Path(db_path).parent / events_file_name(run_id)


def read_events(path: str | Path) -> list[dict]:
    return [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def sse_event(event: dict) -> str:
    """One event as a named Server-Sent Event."""
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"


class EventFile:
    """One Run's events on disk, appended and flushed one line at a time so a
    Run that dies still leaves every event it reached."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh: IO[str] | None = self.path.open("w", encoding="utf-8")

    @property
    def closed(self) -> bool:
        return self._fh is None

    def write(self, event: dict) -> None:
        if self._fh is None:
            return
        self._fh.write(json.dumps(event) + "\n")
        self._fh.flush()
        if event.get("type") in FINAL_EVENTS:
            self.close()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


# Watching a Run again: its recorded events sent back in the live format.
# The recorded time between two events, divided by the chosen speed, is how
# long the replay waits before the second one; no wait is ever longer than
# this, so a Map that took minutes does not leave the screen still.
DEFAULT_REPLAY_SPEED = 20.0
MAX_REPLAY_SPEED = 1_000_000.0
MAX_REPLAY_GAP_S = 1.5


def _when(ts: object) -> datetime | None:
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def replay_delays(
    events: list[dict], speed: float, max_gap: float = MAX_REPLAY_GAP_S
) -> list[float]:
    """How long to wait before sending each event: the recorded gap from the
    one before, divided by `speed`, never more than `max_gap`. The first event
    goes at once, and a time that cannot be read or runs backwards waits
    nothing rather than stopping the replay."""
    delays: list[float] = []
    previous: datetime | None = None
    for event in events:
        when = _when(event.get("ts"))
        gap = 0.0
        if previous is not None and when is not None:
            try:
                gap = (when - previous).total_seconds()
            except TypeError:  # one time with a zone and one without
                gap = 0.0
        delays.append(min(max_gap, gap / speed) if gap > 0 else 0.0)
        if when is not None:
            previous = when
    return delays


def valid_speed(speed: float) -> bool:
    return math.isfinite(speed) and 0 < speed <= MAX_REPLAY_SPEED


def replay_stream(
    events: list[dict],
    speed: float = DEFAULT_REPLAY_SPEED,
    *,
    run_id: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[str]:
    """A recorded Run's events as the live stream sends them, paced, then the
    same `end` marker the live stream closes with, saying it was a replay."""
    yield ": connected\n\n"
    for event, delay in zip(events, replay_delays(events, speed)):
        if delay > 0:
            sleep(delay)
        yield sse_event(event)
    final = events[-1]["type"] if events else None
    end = {
        "replay": True,
        "run_id": run_id,
        "status": "error" if final == "run_failed" else "done",
        "active": False,
    }
    yield "event: end\ndata: " + json.dumps(end) + "\n\n"
