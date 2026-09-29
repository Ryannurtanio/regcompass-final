"""The RegCompass server: ONE FastAPI app behind one URL.

A reviewer opens the root and gets the React interface: a Run panel (Economy,
Pillar, Indicators, Engine, Run, live progress), a Runs list of past Run
Records, the audit view with the source PDF and the quote highlighted, and a
Settings screen for an OpenRouter key. Everything that interface needs is on
this same app, so there is one port, one process and one thing to deploy.

Two data sources sit behind the four audit read endpoints, chosen once at
startup and invisible to the interface:

  * the WORKING DATABASE (the default): the Mappings of one Run, with
    highlight rectangles built from the word boxes stored at ingest. This is
    what a reviewer sees for a Run they just started.
  * a frozen BUNDLE (``--bundle``): the Round 1 golden outputs, the judge's
    keyless path, unchanged.

Base tier only for import: fastapi / starlette / uvicorn / pydantic. The
pipeline and Storage are imported lazily, inside the worker thread or inside
the endpoint, so this module imports and its tests run without the live extras
and without any model call.

Run it locally:

    regcompass serve --port 8000

Environment knobs the serve command reads for its --db, --out and --data-dir
(all optional, and a flag on the command line wins over the variable):
    REGCOMPASS_DB    working database (default data/regcompass.db)
    REGCOMPASS_OUT   export directory (default out)
    REGCOMPASS_DATA  root for a Corpus Document's stored bytes; must match the
                     data dir Discovery fetched them with (default data)

Two more, for a hosted copy only (both unset on the local path, and then there
is no login):
    REGCOMPASS_AUTH_USER      user name for the /login page (a script may send
                              the pair as an HTTP Basic header instead)
    REGCOMPASS_AUTH_PASSWORD  its password; set both or neither

Importing this module builds nothing and opens nothing. The application is
built by create_app, and the start-up sweep of Run Records abandoned by a dead
process runs when that application STARTS, not when it is built.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Literal
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from pydantic import BaseModel

from regcompass.audit import (
    AcceptAllRequest,
    AuditBundle,
    DatabaseAuditSource,
    DocumentSummary,
    ExportFile,
    ExportPreview,
    ExportSummary,
    GlossReviewRequest,
    RecordDetail,
    RecordSummary,
    ReviewHistory,
    ReviewHistoryEntry,
    ReviewQueue,
    ReviewRequest,
    build_review_queue,
    correction_problem,
)
from regcompass.contracts import (
    MANUAL_STRATEGY,
    MAX_CONCURRENCY,
    GlossRecord,
    PortalConfig,
    Review,
    UnknownEngine,
)
from regcompass.engines import (
    ConfigError,
    forget_session_key,
    key_is_set,
    preflight_key,
    resolve_engine,
    set_session_key,
)
from regcompass.export import (
    REVIEW_CONFIDENCE_THRESHOLD,
    ExportGateError,
    ExportResult,
    duplicate_collapse_notice,
    export_all,
)
from regcompass.extract import format_for_extractor
from regcompass.login_page import LOGIN_PAGE_HTML
from regcompass.run_events import (
    DEFAULT_REPLAY_SPEED,
    FINAL_EVENTS,
    EventFile,
    events_file_name,
    events_path,
    read_events,
    replay_stream,
    sse_event,
    valid_speed,
)
from regcompass.discovery_progress import DiscoveryProgress
from regcompass.run_progress import GLOSS, PROVE, RECONCILE, RunProgress

UI_DIST = Path(__file__).resolve().parent / "ui_dist"

# A Review Decision needs a Run to belong to. The frozen bundle is a snapshot of
# a pipeline that already finished and has no Run to write against, so it stays
# a reading room.
BUNDLE_REVIEW_MESSAGE = (
    "this server is reading a frozen bundle: Review Decisions need a working"
    " database Run. Start the server without --bundle and run an Economy first."
)

# The same reading-room rule for the clear: a frozen bundle is a snapshot, and
# nothing here downloaded anything to remove.
BUNDLE_CLEAR_MESSAGE = (
    "this server is reading a frozen bundle: it holds no downloaded Documents"
    " and no caches to clear. Start the server without --bundle to clear a"
    " working database."
)


# The optional login for a hosted copy. Both variables set and non-empty turns
# it on; neither leaves the local path exactly as it was. One without the other
# is a mistake the server refuses to start with, rather than a server that is
# silently open.
AUTH_USER_ENV = "REGCOMPASS_AUTH_USER"
AUTH_PASSWORD_ENV = "REGCOMPASS_AUTH_PASSWORD"

# The paths left open under the login: the Docker health check and CI call
# /api/status with no credentials, and it reports nothing but the server's own
# state; nobody could sign in without the other two.
AUTH_OPEN_PATHS = frozenset({"/api/status", "/login", "/api/login"})

# The browser's session: a signed token in this cookie, good for 12 hours.
SESSION_COOKIE = "regcompass_session"
SESSION_SECONDS = 12 * 3600

# How long a wrong login waits before it is answered; wrong logins queue, so
# this also caps guessing at about two a second across all visitors.
LOGIN_FAIL_DELAY = 0.5
LOGIN_FAIL_MESSAGE = "wrong user name or password"
LOGIN_REQUIRED_BODY = b'{"detail":"login required"}'


def _session_clock() -> float:
    """The time a session is checked against (a seam for the expiry tests)."""
    return time.time()


def login_settings(
    user: str | None = None, password: str | None = None,
) -> tuple[str, str] | None:
    """The login pair, or None when the login is off.

    An argument left as None falls back to its variable. Both empty means off;
    exactly one empty raises ValueError naming both variables, so a half-set
    login stops the server at start instead of leaving it open."""
    user = os.environ.get(AUTH_USER_ENV, "") if user is None else user
    password = os.environ.get(AUTH_PASSWORD_ENV, "") if password is None else password
    if not user and not password:
        return None
    if not user or not password:
        raise ValueError(
            f"login is half set: set both {AUTH_USER_ENV} and"
            f" {AUTH_PASSWORD_ENV} to turn it on, or neither to leave it off"
        )
    if ":" in user:
        # Basic auth splits the credentials at the first colon, so a user name
        # holding one could never log in from a script.
        raise ValueError(f"{AUTH_USER_ENV} must not contain a colon")
    try:
        user.encode("utf-8")
        password.encode("utf-8")
    except UnicodeEncodeError as err:
        raise ValueError(
            f"{AUTH_USER_ENV} and {AUTH_PASSWORD_ENV} must be valid text"
        ) from err
    return user, password


def credentials_match(
    user: bytes, password: bytes, want_user: bytes, want_password: bytes,
) -> bool:
    """Both parts compared in constant time, and both always compared, so a
    wrong user and a wrong password take the same path."""
    user_ok = secrets.compare_digest(user, want_user)
    password_ok = secrets.compare_digest(password, want_password)
    return user_ok and password_ok


def safe_next(raw: str | None) -> str:
    """Where to go after signing in: a path on this site, or the root.

    Only a relative path starting with a single slash survives. A second slash
    or a backslash would let a browser read it as another host, a scheme is
    another site outright, and a control character could split a header, so
    each of those falls back to the root. The login page itself is not a place
    to come back to."""
    if not raw or not isinstance(raw, str):
        return "/"
    if not raw.startswith("/") or raw.startswith("//") or "\\" in raw:
        return "/"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        return "/"
    if raw.split("?", 1)[0].rstrip("/") == "/login":
        return "/"
    return raw


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class SessionTokens:
    """Signed session tokens for the browser login.

    A token is ``<payload>.<signature>``: the payload holds the user name, the
    issue time and the expiry, and the signature is HMAC-SHA256 over it with a
    key drawn fresh at every server start. Nothing is stored on the server, and
    a restart signs everyone out."""

    def __init__(self, user: str, key: bytes | None = None) -> None:
        self._user = user.encode("utf-8")
        self._key = key if key is not None else secrets.token_bytes(32)

    def _sign(self, payload: str) -> str:
        return _b64(hmac.new(self._key, payload.encode("ascii"), hashlib.sha256).digest())

    def issue(
        self, user: str, issued: float | None = None, expires: float | None = None,
    ) -> str:
        issued = _session_clock() if issued is None else issued
        expires = issued + SESSION_SECONDS if expires is None else expires
        body = b"\n".join(
            [user.encode("utf-8"), str(int(issued)).encode(), str(int(expires)).encode()]
        )
        payload = _b64(body)
        return f"{payload}.{self._sign(payload)}"

    def valid(self, token: str) -> bool:
        payload, sep, signature = token.rpartition(".")
        if not sep or not payload or not signature:
            return False
        try:
            if not secrets.compare_digest(
                self._sign(payload).encode("ascii"), signature.encode("ascii"),
            ):
                return False
            user, issued, expires = _unb64(payload).split(b"\n")
            issued_at, expires_at = int(issued), int(expires)
        except (ValueError, UnicodeError, binascii.Error):
            return False
        now = _session_clock()
        if not (issued_at <= now + 60 and now < expires_at):
            return False
        if expires_at - issued_at > SESSION_SECONDS:
            return False
        return secrets.compare_digest(user, self._user)


class LoginMiddleware:
    """The login in front of the whole app, as plain ASGI.

    Plain ASGI rather than a response-wrapping middleware, so a streamed
    response (the Run progress log) passes through untouched once the request
    is let in. A request is let in with a valid session cookie, or with the
    right HTTP Basic header (for curl and scripts). Otherwise a browser
    navigation is sent to the login page, and everything else gets a 401 in
    JSON with no Basic challenge, so the browser never draws its own box."""

    def __init__(self, app, user: str, password: str, sessions: SessionTokens) -> None:
        self.app = app
        self._user = user.encode("utf-8")
        self._password = password.encode("utf-8")
        self._sessions = sessions

    @staticmethod
    def _header(scope, name: bytes) -> bytes:
        for key, value in scope.get("headers") or ():
            if key == name:
                return value
        return b""

    def _session_ok(self, scope) -> bool:
        for key, value in scope.get("headers") or ():
            if key != b"cookie":
                continue
            for part in value.split(b";"):
                name, sep, token = part.strip().partition(b"=")
                if sep and name == SESSION_COOKIE.encode():
                    # a stale copy may ride beside a fresh one: any valid one will do
                    try:
                        if self._sessions.valid(token.decode("ascii")):
                            return True
                    except UnicodeDecodeError:
                        continue
        return False

    def _basic_ok(self, scope) -> bool:
        scheme, _, token = self._header(scope, b"authorization").partition(b" ")
        if scheme.lower() != b"basic" or not token:
            return False
        try:
            decoded = base64.b64decode(token.strip(), validate=True)
        except (binascii.Error, ValueError):
            return False
        user, sep, password = decoded.partition(b":")
        if not sep:
            return False
        return credentials_match(user, password, self._user, self._password)

    @staticmethod
    def _wants_page(scope) -> bool:
        return scope.get("method") in ("GET", "HEAD") and (
            b"text/html" in LoginMiddleware._header(scope, b"accept")
        )

    @staticmethod
    def _login_location(scope) -> bytes:
        raw = scope.get("raw_path") or scope["path"].encode("utf-8")
        target = raw.decode("latin-1")
        query = scope.get("query_string") or b""
        if query:
            target += "?" + query.decode("latin-1")
        return ("/login?next=" + quote(safe_next(target), safe="")).encode("ascii")

    async def __call__(self, scope, receive, send) -> None:
        kind = scope["type"]
        if kind not in ("http", "websocket") or scope["path"] in AUTH_OPEN_PATHS:
            await self.app(scope, receive, send)
            return
        if self._session_ok(scope) or self._basic_ok(scope):
            await self.app(scope, receive, send)
            return
        if kind == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if self._wants_page(scope):
            await send(
                {
                    "type": "http.response.start",
                    "status": 303,
                    "headers": [
                        (b"location", self._login_location(scope)),
                        (b"cache-control", b"no-store"),
                        (b"content-length", b"0"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b""})
            return
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"cache-control", b"no-store"),
                    (b"content-length", str(len(LOGIN_REQUIRED_BODY)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": LOGIN_REQUIRED_BODY})


def economies() -> dict[str, "PortalConfig"]:
    """The Economy registry, read at request time so adding an Economy to
    config/portals.yaml is visible to a running server on the next request and
    needs no code edit."""
    from regcompass.config import load_portals

    return load_portals()


def valid_pillars() -> tuple[int, ...]:
    from regcompass.config import load_pillars

    return tuple(sorted(load_pillars()))


def default_pillars() -> tuple[int, ...]:
    """What a Run covers when the request names no Pillar: the configured
    default pair, never all twelve."""
    from regcompass.config import load_default_pillars

    return load_default_pillars()


# The two rules the Settings card "Polite crawling" states under its list, in
# the crawler's own terms (crawl.read_robots_policy, crawl._robots_text and
# the max(floor, crawl-delay) spacing in discovery and crawl.fetch_one).
CRAWL_DELAY_RULE = (
    "A crawl-delay published in a site's robots.txt can only lengthen the wait:"
    " the wait used is the longer of the two, never the shorter."
)
ROBOTS_UNREACHABLE_RULE = (
    "If robots.txt is missing or refused (a 4xx answer) or the site cannot be"
    " reached, the site has published no rules and the minimum wait still"
    " applies. If robots.txt"
    " answers with a server error, the rules may exist but cannot be read, so"
    " each site's own policy below applies."
)


def robots_unavailable_words(
    portal: "PortalConfig", today: "date | None" = None
) -> str:
    """What this Portal's crawler does when its robots.txt answers with a
    server error, in plain words, derived from the same fields and the same
    date test crawl.read_robots_policy applies: the per-Portal policy, and the
    recorded first-failure date that starts RFC 9309's 30-day clock (lifted
    once today - since > the grace period, i.e. from since + 30 days + 1)."""
    from datetime import date, timedelta

    from regcompass.contracts import ROBOTS_UNREACHABLE_GRACE_DAYS

    if portal.robots_unavailable_policy == "proceed":
        return (
            "Read as publishing no rules (a recorded decision for this site)."
            " The minimum wait still applies."
        )
    since = portal.robots_unreachable_since
    if since is not None:
        today = today or date.today()
        grace = timedelta(days=ROBOTS_UNREACHABLE_GRACE_DAYS)
        lifts_on = since + grace + timedelta(days=1)
        if today - since > grace:
            return (
                f"Read as publishing no rules since {lifts_on.isoformat()}:"
                f" robots.txt was first recorded failing on {since.isoformat()},"
                f" and RFC 9309 allows that after {ROBOTS_UNREACHABLE_GRACE_DAYS}"
                " days. The minimum wait still applies."
            )
        return (
            "Nothing is fetched. robots.txt was first recorded failing on"
            f" {since.isoformat()}; from {lifts_on.isoformat()} it is read as"
            " publishing no rules (RFC 9309)."
        )
    return "Nothing is fetched until robots.txt can be read again."


# Read-only browser: only these tables are reachable; the name is interpolated
# into SQL, so it MUST come from this set and nowhere else.
WHITELIST_TABLES = ("documents", "chunks", "mappings", "gate_scores", "audit_log")
VERIFICATION_STATUSES = ("unverified", "passed", "dropped")

DEFAULT_LIMIT = 50
MAX_LIMIT = 500
CSV_PREVIEW_ROWS = 200
JSON_PREVIEW_MAX_BYTES = 6 * 1024 * 1024

# Media types for the download endpoint (keyed by suffix).
_MEDIA = {
    ".csv": "text/csv",
    ".json": "application/json",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".log": "text/plain",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _review_run_id(
    source: "DatabaseAuditSource", mapping_id: str | None = None
) -> str:
    """The Run a Review Decision belongs to.

    A database with no Run Records resolves to no Run at all, and its Mappings
    can still carry any run_id (rows migrated from before Runs existed carry the
    legacy one; a database seeded straight from checkpoints carries whatever
    seeded it). The decision has to land on the SAME run_id as the Mapping it
    judges, or the composite foreign key refuses it, so when a Mapping is named
    its own run_id decides. The legacy id is the last resort."""
    from regcompass.storage import LEGACY_RUN_ID

    if source.run_id:
        return source.run_id
    named = mapping_id
    if named is None:
        # No Mapping named (a listing, a count): the Mappings on screen are all
        # this source has, so the first one places the whole lane. Listing and
        # writing must agree, or a decision would be stored and never shown.
        records = source.records()
        named = records[0].mapping_id if records else None
    if named is None:
        return LEGACY_RUN_ID
    return source.storage.run_id_of_mapping(named) or LEGACY_RUN_ID


def _review_statuses(storage, run_id: str | None) -> dict[str, str]:
    """mapping_id -> the decision that stands, for ONE Run, as the export gate
    and the Comparison both want it: the status alone, no Review object."""
    if run_id is None:
        return {}
    return {
        mapping_id: review.review_status
        for mapping_id, review in storage.reviews_for_run(run_id).items()
    }


@dataclass
class RunTelemetry:
    """Live counters for the verification funnel, shaped like ``RunReport``
    plus the mapped-pair progress the funnel bar animates against. One instance
    per Run, mutated in place by the worker thread and read (under lock) by
    /api/stats."""

    economy: str | None = None
    engine: str | None = None
    mode: str | None = None
    n_chunks: int = 0
    n_pairs_considered: int = 0
    n_pairs_gated: int = 0
    n_passed: int = 0
    n_no_evidence: int = 0
    n_dropped: int = 0
    n_groups: int = 0
    pairs_done: int = 0
    pairs_total: int = 0
    n_documents: int = 0


# How often a map_progress event is sent: the same 25-pair cadence as the
# Run's "M6 map + M7 verify" text lines.
MAP_TICK_EVERY = 25


class PieceDetail(BaseModel):
    """One Piece: a slice of its Document's stored text, and where it sits."""

    piece_id: str
    document_id: str
    document_title: str | None = None
    section: str | None = None
    page: int | None = None
    page_end: int | None = None
    char_start: int
    char_end: int
    text: str
    # 'html' when the Document is a web page, whose single "page" is not one.
    format: Literal["pdf", "html"] = "pdf"


class RunManager(RunProgress):
    """Owns the single-job-at-a-time lifecycle: the progress-line buffer, the
    worker thread, and a lock guarding both. One job at a time by construction;
    a second start attempt while active is refused (the API turns that into a
    409).

    It is also the Run's progress hook: the pipeline reports its Steps to it
    (regcompass.run_progress), and each Map tick moves the funnel bar. Every
    hook call becomes a typed event (regcompass.run_events) beside the text
    lines: buffered for the stream, so a late tab replays them from the first,
    and written to the Run's event file when the worker names one."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._lines: list[dict] = []
        self._active = False
        self._status = "idle"  # idle | running | done | error
        self._error: str | None = None
        self._meta: dict = {}
        self._thread: threading.Thread | None = None
        self._telemetry: RunTelemetry | None = None
        self._started_monotonic: float | None = None
        self._finished_monotonic: float | None = None
        self._seen_docs: set[str] = set()
        self._pairs: dict[str, tuple[int, int]] = {}
        # The usage meter of the Run in flight (or the last one), handed over
        # by the pipeline the moment it starts metering: engines.watch_meter.
        self._meter = None
        # The Run's own report while it is being filled in, so the funnel
        # moves during the Run rather than jumping at its end.
        self._report = None
        # The typed events of the job in flight (or the last one), numbered
        # from 0, and the file they are also written to, when there is one.
        self._events: list[dict] = []
        self._event_file: EventFile | None = None
        # Mappings each Document proved, for its document_finished event.
        self._proven: dict[str, int] = {}
        # The Engine the meter's cost is priced at, resolved once per Run.
        self._cost_engine: object | None = None
        self._cost_engine_name: str | None = None

    # -- writer side (worker thread) --
    def push(self, msg: object) -> None:
        with self._lock:
            self._lines.append({"seq": len(self._lines), "ts": _utc_now(), "msg": str(msg)})

    def record_pairs(self, doc_id: str, done: int, total: int) -> None:
        """pair_progress callback (matches pipeline's (doc_id, i, n_pairs)):
        record live mapped-pair progress for the funnel bar. The bar counts
        the whole Run: every Document's pairs so far, not only the current
        Document's, so it never runs back to zero between Documents."""
        with self._lock:
            if self._telemetry is not None:
                self._pairs[doc_id] = (int(done), int(total))
                self._telemetry.pairs_done = sum(d for d, _ in self._pairs.values())
                self._telemetry.pairs_total = sum(t for _, t in self._pairs.values())
                self._seen_docs.add(doc_id)
                self._telemetry.n_documents = len(self._seen_docs)

    # -- typed events --
    def emit(self, type_: str, **fields) -> dict:
        """Append one typed event (and write it to the event file, if any)."""
        with self._lock:
            event = {"type": type_, "seq": len(self._events), "ts": _utc_now(), **fields}
            self._events.append(event)
            if self._event_file is not None:
                try:
                    self._event_file.write(event)
                except OSError:
                    # The file is a record of the Run, never a reason for the
                    # Run to fail: the stream still carries every event.
                    self._event_file = None
            return event

    def record_events_to(self, path: str | Path) -> None:
        """Write this job's events to `path` from now on, starting with every
        event already emitted, so the file always begins at the first one."""
        with self._lock:
            if self._event_file is not None:
                self._event_file.close()
            try:
                self._event_file = EventFile(path)
                for event in self._events:
                    self._event_file.write(event)
            except OSError:
                self._event_file = None

    def events_since(self, seq: int) -> list[dict]:
        with self._lock:
            return [dict(event) for event in self._events[seq:]]

    def _ended(self) -> bool:
        return bool(self._events) and self._events[-1]["type"] in FINAL_EVENTS

    def step_started(self, document_id: str | None, step: str) -> None:
        self.emit("step_started", document_id=document_id, step=step)

    def meter_fields(self) -> dict:
        """The Run's own meter now: Engine calls so far and their cost at the
        Engine's declared prices, the same arithmetic that closes the Run
        Record. Zero before the Run has started metering."""
        with self._lock:
            totals = self._meter
            name = self._meta.get("engine")
        if totals is None:
            return {"engine_calls": 0, "cost_usd": 0.0}
        from regcompass.engines import copy_meter, cost_usd_for

        snap = copy_meter(totals)
        if name != self._cost_engine_name:
            self._cost_engine = _engine_or_none(name)
            self._cost_engine_name = name
        cost = 0.0
        if self._cost_engine is not None:
            cost = float(cost_usd_for(
                self._cost_engine, snap.prompt_tokens, snap.completion_tokens
            ))
        return {"engine_calls": int(snap.calls), "cost_usd": cost}

    def scan_flagged(self, document_id: str, reason: str) -> None:
        self.emit("scan_flagged", document_id=document_id, reason=reason)

    def step_finished(self, document_id: str | None, step: str, counts: dict) -> None:
        self.emit("step_finished", document_id=document_id, step=step, counts=dict(counts))
        if document_id is None:
            if step == RECONCILE:
                self.emit(
                    "reconcile",
                    before=int(counts.get("passed", 0)),
                    after=int(counts.get("groups", 0)),
                    **self.meter_fields(),
                )
            return
        if step == PROVE:
            with self._lock:
                self._proven[document_id] = int(counts.get("proven", 0))
        elif step == GLOSS:
            # Gloss is a Document's last Step: it is finished.
            with self._lock:
                mappings = self._proven.get(document_id, 0)
            self.emit(
                "document_finished", document_id=document_id, mappings=mappings,
                **self.meter_fields(),
            )

    def map_progress(self, document_id: str, done: int, total: int) -> None:
        # The funnel bar moves with every pair; the typed event keeps the text
        # lines' cadence (every 25 pairs, and the last one), so a long Map
        # does not flood the stream or the event file.
        self.record_pairs(document_id, done, total)
        if done % MAP_TICK_EVERY == 0 or done == total:
            self.emit(
                "map_progress", document_id=document_id, done=int(done), total=int(total),
                **self.meter_fields(),
            )

    def candidate(self, document_id: str, piece_id: str, indicator: str, cosine: float,
                  bm25: float, lane: str, outcome: str, section: str | None = None,
                  page: int | None = None) -> None:
        self.emit(
            "candidate", document_id=document_id, piece_id=piece_id, indicator=indicator,
            cosine=round(float(cosine), 4), bm25=round(float(bm25), 4), lane=lane,
            outcome=outcome, section=section, page=page,
        )

    def mapping_added(
        self, document_id: str, mapping_id: str, indicator: str, page: int | None
    ) -> None:
        self.emit(
            "mapping_added", document_id=document_id, mapping_id=mapping_id,
            indicator=indicator, page=page,
        )

    def failed(self, document_id: str | None, step: str | None, message: str) -> None:
        self.fail_run(message, document_id=document_id, step=step)

    def fail_run(
        self, message: str, document_id: str | None = None, step: str | None = None
    ) -> None:
        """The Run's one run_failed. The pipeline reports a failure where it
        happened; the worker reports it again, without a place, for failures
        outside the pipeline. Only the first one is an event."""
        with self._lock:
            if self._ended():
                return
            self.emit("run_failed", document_id=document_id, step=step, message=message)

    def finish_run(self, report: object) -> None:
        """run_finished, with the Run's totals as its report closed them."""

        def n(name: str) -> int:
            return int(getattr(report, name, 0) or 0)

        totals = {
            "documents": len(getattr(report, "documents", None) or []),
            "pieces": n("n_chunks"),
            "pairs": n("n_pairs_considered"),
            "candidates": n("n_pairs_gated"),
            "proven": n("n_passed"),
            "no_evidence": n("n_no_evidence"),
            "dropped": n("n_dropped"),
            "glossed": n("n_glossed"),
            "groups": n("n_groups"),
        }
        cost = getattr(report, "cost_usd", None)
        billed = getattr(report, "provider_cost_usd", None)
        calls = self.meter_fields()["engine_calls"]
        with self._lock:
            if self._ended():
                return
            self.emit(
                "run_finished", status="completed", totals=totals,
                cost_usd=None if cost is None else float(cost),
                # What the provider itself billed, when it sends a figure.
                provider_cost_usd=None if billed is None else float(billed),
                engine_calls=calls,
            )

    def attach_report(self, report: object) -> None:
        """report_watch callback: keep the Run's report while it fills in."""
        with self._lock:
            self._report = report

    def absorb_report(self, report: object) -> None:
        """Copy the final RunReport counters into the live telemetry once a Run
        returns one (run_economy/run_e2e only hand it back at the end)."""
        with self._lock:
            tel = self._telemetry
            if tel is None:
                return
            docs = getattr(report, "documents", None)
            if docs:
                tel.n_documents = max(tel.n_documents, len(docs))
            for field_name in (
                "n_chunks", "n_pairs_considered", "n_pairs_gated", "n_passed",
                "n_no_evidence", "n_dropped", "n_groups",
            ):
                val = getattr(report, field_name, None)
                if val is not None:
                    setattr(tel, field_name, int(val))
            if tel.pairs_total:
                tel.pairs_done = tel.pairs_total

    def attach_meter(self, totals: object) -> None:
        """watch_meter callback: keep the Run's live meter, so /api/stats can
        show what this Run has spent so far, not only once it has finished."""
        with self._lock:
            self._meter = totals

    def meter_snapshot(self) -> dict | None:
        """This session's Run's model usage so far: tokens, calls and the
        provider's own figure. None when no Run of this session has metered."""
        with self._lock:
            totals = self._meter
        if totals is None:
            return None
        from regcompass.engines import copy_meter

        return asdict(copy_meter(totals))

    def set_run_id(self, run_id: str | None) -> None:
        """Publish the Run Record id the worker just opened, so the interface
        can scope the audit view to THIS Run the moment it finishes."""
        with self._lock:
            self._meta["run_id"] = run_id

    def set_discovery_report(self, report: object) -> None:
        """Publish a finished Discovery's counts alongside the status, so the
        interface can show what the last Discovery fetched."""
        with self._lock:
            self._meta["discovery"] = {
                "run_id": getattr(report, "run_id", None),
                "strategy": getattr(report, "strategy", None),
                "fetched": getattr(report, "fetched", 0),
                "skipped_existing": getattr(report, "skipped_existing", 0),
                "documents_stored": getattr(report, "documents_stored", 0),
                "failed": getattr(report, "failed", 0),
                "disallowed": getattr(report, "disallowed", 0),
                "refresh": getattr(report, "refresh", False),
                "escalated_to_impersonation": getattr(
                    report, "escalated_to_impersonation", False
                ),
            }

    def telemetry_snapshot(self) -> dict:
        with self._lock:
            zero = RunTelemetry()
            tel = self._telemetry or zero
            out = asdict(tel)
            report = self._report if self._active else None
        if report is not None:
            # The live report's counters, which only grow while the Run goes;
            # absorb_report copies the final ones in when it returns.
            for field_name in (
                "n_chunks", "n_pairs_considered", "n_pairs_gated", "n_passed",
                "n_no_evidence", "n_dropped", "n_groups",
            ):
                val = getattr(report, field_name, None)
                if isinstance(val, int):
                    out[field_name] = max(out[field_name], val)
        return out

    def elapsed_s(self) -> float:
        with self._lock:
            if self._started_monotonic is None:
                return 0.0
            end = self._finished_monotonic
            if end is None:
                end = time.monotonic()
            return max(0.0, end - self._started_monotonic)

    def _finish(self, status: str, error: str | None = None) -> None:
        with self._lock:
            self._active = False
            self._status = status
            self._error = error
            self._finished_monotonic = time.monotonic()
            self._meta["finished_at"] = _utc_now()

    # -- reader side (request threads) --
    def lines_since(self, seq: int) -> list[dict]:
        with self._lock:
            return [dict(line) for line in self._lines if line["seq"] >= seq]

    def status(self) -> dict:
        with self._lock:
            out = {
                "active": self._active,
                "status": self._status,
                "error": self._error,
                "n_lines": len(self._lines),
            }
            out.update(self._meta)
            return out

    def start(self, worker: Callable[["RunManager"], None], meta: dict) -> bool:
        """Reset the buffer and launch the worker. Returns False if a job is
        already active (nothing is reset in that case)."""
        with self._lock:
            if self._active:
                return False
            self._lines = []
            self._active = True
            self._status = "running"
            self._error = None
            self._meta = dict(meta)
            self._meta["started_at"] = _utc_now()
            self._seen_docs = set()
            self._pairs = {}
            self._meter = None
            self._report = None
            self._events = []
            if self._event_file is not None:
                self._event_file.close()
            self._event_file = None
            self._proven = {}
            self._cost_engine = None
            self._cost_engine_name = None
            self._telemetry = RunTelemetry(
                economy=self._meta.get("economy"),
                engine=self._meta.get("engine"),
                mode=self._meta.get("mode"),
            )
            self._started_monotonic = time.monotonic()
            self._finished_monotonic = None
            thread = threading.Thread(target=worker, args=(self,), daemon=True)
            self._thread = thread
        thread.start()
        return True


def _open_for_write(db_path: str | Path):
    """Open the working database for a lane that WRITES, and make room for it.

    A fresh clone has no `data/` at all: it is gitignored, so nothing in the
    repository carries it. The interface still starts, and the first write then
    died inside sqlite with `unable to open database file`, which reached the
    reviewer as a bare 500 with nothing on the screen. So the directory the
    database lives in is made here, and the schema applied, for every lane that
    writes: an add, a Run, a Discovery. This is the one place in the server
    where either happens, so the next write lane cannot forget one.

    Read lanes do NOT call this. They check `Path(db_path).is_file()` and
    answer empty, which is what keeps a status check on a fresh install from
    leaving a database, or a directory, behind it.

    Storage is imported here rather than at module scope because the Run lanes
    call this from their worker thread, and the module must stay importable on
    the base tier.
    """
    from regcompass.storage import Storage

    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    storage = Storage(db_path)
    storage.apply_schema()
    return storage


def _close_orphaned_run(db_path: str | Path, run_id: str, exc: BaseException) -> None:
    """Close a Run Record the endpoint opened when the worker fell over BEFORE
    the pipeline adopted it: the database failing to open, the Corpus read
    raising, an import that could not load. Once the pipeline has adopted the
    record it closes it itself, so a record already past `running` is left
    exactly as the pipeline wrote it. Without this, such a failure left the
    row at `running` until the next restart sweep, and the Runs list showed a
    Run in progress that had in fact died on its first step."""
    try:
        from regcompass.storage import utc_now_z

        storage = _open_for_write(db_path)
        try:
            row = storage.run_get(run_id)
            if row is not None and row.get("status") == "running":
                storage.run_finish(
                    run_id, status="failed", ended_at=utc_now_z(),
                    error=f"{type(exc).__name__}: {exc}",
                )
        finally:
            storage.close()
    except Exception:
        # The record is the diagnostic, not the Run. A failure closing it must
        # not mask the error the worker is about to report to the screen.
        return


def _make_real_worker(
    economy: str,
    pillars: tuple[int, ...],
    engine,
    db_path: str,
    data_dir: str,
    indicators: tuple[str, ...] | None = None,
    concurrency: int | None = None,
    run_id: str | None = None,
    documents: list[dict] | None = None,
):
    """Build the worker that runs the actual pipeline over this Economy's
    Corpus. `data_dir` is where the Corpus's stored bytes live, and must be the
    one the Discovery that fetched them used. The pipeline and Storage are
    imported HERE (thread body) so the module stays importable on the base
    tier, and the SQLite connection is created in the thread that uses it.

    `run_id` is the Run Record the endpoint has ALREADY opened, on the request
    thread, before it answered "started". The worker adopts it rather than
    opening its own, so a process killed in the seconds before the first model
    call still leaves a row for the restart sweep to mark interrupted."""

    def worker(manager: RunManager) -> None:
        try:
            from regcompass.engines import watch_meter
            from regcompass.pipeline import run_economy

            watch_meter(manager.attach_meter)

            narrowed = "" if indicators is None else f" indicators={','.join(indicators)}"
            manager.push(
                f"M0 start | economy={economy} pillars={pillars}{narrowed}"
                f" Engine={engine.name} ({engine.display_name})"
            )
            if run_id is not None and _RUN_ID_RE.match(run_id):
                manager.record_events_to(events_path(db_path, run_id))
            # Sent BEFORE the worker opens the database, with the Document list
            # the endpoint read when it opened the Run Record: a Run whose
            # database then fails to open still shows the Documents it was for.
            manager.emit(
                "run_started", run_id=run_id, economy=economy,
                pillars=list(pillars),
                indicators=None if indicators is None else list(indicators),
                engine=engine.name, documents=list(documents or []),
            )
            storage = _open_for_write(db_path)
            report = run_economy(
                storage, economy, pillars, engine, data_dir=Path(data_dir),
                indicators=None if indicators is None else list(indicators),
                progress=manager.push, hook=manager,
                concurrency=concurrency, run_id=run_id,
                report_watch=manager.attach_report,
            )
            manager.absorb_report(report)
            manager.set_run_id(getattr(report, "run_id", None))
            manager.push(
                f"M9 done | {economy}: {len(report.documents)} docs, "
                f"{report.n_pairs_gated} gated -> {report.n_passed} passed, "
                f"{report.n_no_evidence} no-evidence, {report.n_dropped} dropped, "
                f"{report.n_groups} groups reconciled"
            )
            manager.finish_run(report)
            manager._finish("done")
        except Exception as exc:
            manager.push(f"ERROR | {type(exc).__name__}: {exc}")
            if run_id is not None:
                _close_orphaned_run(db_path, run_id, exc)
            manager.fail_run(f"{type(exc).__name__}: {exc}")
            manager._finish("error", str(exc))

    return worker


def _make_e2e_worker(
    economy: str,
    pillars: tuple[int, ...],
    engine,
    db_path: str,
    data_dir: str,
    out_dir: str,
    max_documents: int,
    indicators: tuple[str, ...] | None = None,
    concurrency: int | None = None,
):
    """Build the worker for the LIVE crawl e2e lane: a bounded Discovery flows
    straight into extract -> map -> reconcile -> export. Same lazy-import
    discipline as the Corpus worker so the base tier stays importable, and the
    SQLite connection is created inside the thread."""

    def worker(manager: RunManager) -> None:
        try:
            from regcompass.engines import watch_meter
            from regcompass.pipeline import run_e2e

            watch_meter(manager.attach_meter)

            manager.push(
                f"M0 start | e2e economy={economy} pillars={pillars}"
                f" Engine={engine.name} ({engine.display_name})"
                f" max_documents={max_documents}"
            )
            # Discovery comes first, so neither the Run id nor its Documents
            # are known yet: a Document appears with its first Step, and the
            # event file is written once the Run has an id.
            manager.emit(
                "run_started", run_id=None, economy=economy, pillars=list(pillars),
                indicators=None if indicators is None else list(indicators),
                engine=engine.name, documents=[],
            )
            storage = _open_for_write(db_path)
            report = run_e2e(
                storage, economy, pillars, engine,
                Path(data_dir), Path(out_dir),
                max_documents=max_documents,
                indicators=None if indicators is None else list(indicators),
                progress=manager.push, hook=manager,
                concurrency=concurrency, report_watch=manager.attach_report,
            )
            run = report.run
            if run is not None:
                manager.absorb_report(run)
                manager.set_run_id(getattr(run, "run_id", None))
            manager.push(
                f"M9 done | {economy}: Discovery fetched={report.crawl_fetched}"
                f" deduplicated={report.crawl_deduplicated} failed={report.crawl_failed}"
                f" -> mapped {len(report.documents_mapped)} doc(s), {run.n_passed} passed,"
                f" {run.n_groups} groups; export -> {out_dir}"
            )
            # The event file is written only here, after success: this lane
            # learns its Run id only when run_e2e returns, so a failed e2e Run
            # leaves its events on the stream but no file.
            e2e_run_id = getattr(run, "run_id", None)
            if isinstance(e2e_run_id, str) and _RUN_ID_RE.match(e2e_run_id):
                manager.record_events_to(events_path(db_path, e2e_run_id))
            manager.finish_run(run)
            manager._finish("done")
        except Exception as exc:
            manager.push(f"ERROR | {type(exc).__name__}: {exc}")
            manager.fail_run(f"{type(exc).__name__}: {exc}")
            manager._finish("error", str(exc))

    return worker


def _run_documents(storage, economy: str) -> list[dict]:
    """The Documents a Run over this Economy's Corpus reads, as run_started
    lists them. A Corpus that cannot be read here lists nothing: the Run
    itself then says why."""
    try:
        rows = storage.corpus_documents(economy)
    except sqlite3.Error:
        return []
    out = []
    for row in rows:
        d = dict(row)
        out.append(
            {
                "document_id": d["document_id"],
                "title": d.get("title") or d["document_id"],
                "language": d.get("language"),
                "n_pages": d.get("n_pages"),
                # 'html' for a web page, which has no pages to count.
                "format": format_for_extractor(d.get("extractor")),
            }
        )
    return out


class _DiscoveryEvents(DiscoveryProgress):
    """Discovery's progress hook on the server: each call becomes a named
    event on the Run manager's stream, beside the text lines, which stay the
    ones Discovery always wrote. Exactly one closing event: finished or
    failed, whichever comes first."""

    def __init__(self, manager: "RunManager") -> None:
        self.manager = manager
        self.ended = False

    def portal(self, **fields) -> None:
        self.manager.emit("discovery_portal", **fields)

    def found(self, url: str, name: str | None) -> None:
        self.manager.emit("discovery_found", url=url, name=name)

    def fetched(self, url: str, size_bytes: int, method: str) -> None:
        self.manager.emit(
            "discovery_fetched", url=url, size_bytes=int(size_bytes), method=method
        )

    def added(self, url, document_id, title, n_pages, ocr_applied) -> None:
        self.manager.emit(
            "discovery_added", url=url, document_id=document_id, title=title,
            n_pages=int(n_pages), ocr_applied=bool(ocr_applied),
        )

    def skipped(
        self, url: str, code: str, reason: str, title: str | None = None
    ) -> None:
        self.manager.emit(
            "discovery_skipped", url=url, code=code, reason=reason, title=title
        )

    def finished(self, counts: dict) -> None:
        if not self.ended:
            self.ended = True
            self.manager.emit("discovery_finished", counts=dict(counts))

    def failed(self, message: str) -> None:
        if not self.ended:
            self.ended = True
            self.manager.emit("discovery_failed", message=message)

    def finished_from(self, report: object) -> None:
        """The closing counts from the report, for a Discovery that returned
        without ever calling finished."""

        def n(name: str) -> int:
            return int(getattr(report, name, 0) or 0)

        self.finished(
            {
                "run_id": getattr(report, "run_id", None),
                "found": n("discovered"),
                "fetched": n("fetched"),
                "added": n("documents_stored"),
                "skipped": n("skipped_existing") + n("failed") + n("disallowed")
                + n("deduplicated") + n("skipped_off_host"),
                "already_in_corpus": n("skipped_existing"),
                "failed": n("failed"),
                "disallowed": n("disallowed"),
                "duplicates": n("deduplicated"),
                "off_whitelist": n("skipped_off_host"),
                "spacing_seconds": float(getattr(report, "min_interval_seconds", 0) or 0),
            }
        )


def _make_discover_worker(
    economy: str, refresh: bool, db_path: str, data_dir: str
):
    """Build the worker that fills one Economy's Corpus. Discovery is the only
    step that touches the internet, so it runs in the same single worker slot a
    Run uses: the interface never has a Discovery and a Run competing for the
    same database. Lazy imports keep the module importable on the base tier."""

    def worker(manager: RunManager) -> None:
        events = _DiscoveryEvents(manager)
        try:
            from regcompass import discovery as discovery_mod

            manager.push(
                f"M0 start | discover economy={economy} refresh={refresh}"
            )
            storage = _open_for_write(db_path)
            report = discovery_mod.discover_economy(
                economy, storage, refresh=refresh, data_dir=Path(data_dir),
                progress=manager.push, hook=events,
            )
            events.finished_from(report)
            manager.set_discovery_report(report)
            for miss in report.misses:
                manager.push(f"Discovery | miss: {miss}")
            manager.push(
                f"M10 done | {economy}: fetched {report.fetched},"
                f" skipped {report.skipped_existing} already in the Corpus,"
                f" stored {report.documents_stored} Document(s);"
                f" Discovery record {report.run_id}"
            )
            manager._finish("done")
        except Exception as exc:
            manager.push(f"ERROR | {type(exc).__name__}: {exc}")
            events.failed(f"{type(exc).__name__}: {exc}"[:500])
            manager._finish("error", str(exc))

    return worker


class DiscoverRequest(BaseModel):
    economy: str
    refresh: bool = False


class RunRequest(BaseModel):
    economy: str
    pillars: list[int] | None = None
    # Narrow the Run to these Indicator ids; they must belong to the Run's
    # Pillars. None or [] means every Indicator of those Pillars.
    indicators: list[str] | None = None
    engine: str | None = None  # an Engine name from config/models.yaml
    mode: str = "run"  # "run" (read the Corpus) or "e2e" (Discovery then a Run)
    max_documents: int | None = None
    # Mapping calls to keep in flight. None takes the Engine's own declared
    # value; the records and the export are the same bytes either way.
    concurrency: int | None = None


class AddUrlRequest(BaseModel):
    """"Add document" by Source URL. `allow_any_host` is the operator ticking
    "official source outside the configured Portal": they vouch for a host the
    Portal whitelist does not carry, and the Evidence Export discloses it."""

    economy: str
    source_url: str
    language: str | None = None
    allow_any_host: bool = False
    # The operator's own statute name, which the Evidence Export ships in the
    # organizer's Law Name column. Left out, the ingest derives one.
    title: str | None = None


class ClearRequest(BaseModel):
    """Clear the downloaded Documents and every cache of one Economy, or of all
    of them when `economy` is null. `confirm` is the operator saying yes: the
    interface only sends it after showing the counts, and nothing is removed
    without it."""

    economy: str | None = None
    confirm: bool = False


class SetSourceUrlRequest(BaseModel):
    """Where an already-added Document is published. The upload lane lets a
    reviewer skip this when the clock is running; this is how they supply it
    afterwards, on the Document that is already in the Corpus."""

    source_url: str


class SettingsKeyRequest(BaseModel):
    """A key typed into Settings, named either by its Engine or by the
    environment variable it fills. It is held in this process only."""

    engine: str | None = None
    env: str | None = None
    key: str


def _sse(manager: RunManager) -> Iterator[str]:
    """Replay every buffered line from the current job, then stream new ones,
    then emit a terminal ``end`` event and stop. Polling (not blocking on a
    queue) keeps this robust for a single-client interface and lets a late tab
    replay the whole Run."""
    yield ": connected\n\n"
    last = 0
    last_event = 0
    while True:
        for line in manager.lines_since(last):
            last = line["seq"] + 1
            yield "data: " + json.dumps({"ts": line["ts"], "msg": line["msg"]}) + "\n\n"
        # The typed events, as named events: a reader listening only for
        # unnamed messages (the text lines) never sees them.
        for event in manager.events_since(last_event):
            last_event = event["seq"] + 1
            yield sse_event(event)
        st = manager.status()
        if (
            st["status"] in ("done", "error", "idle")
            and not manager.lines_since(last)
            and not manager.events_since(last_event)
        ):
            yield "event: end\ndata: " + json.dumps(st) + "\n\n"
            return
        time.sleep(0.2)


def _read_table(
    db_path: str,
    table: str,
    limit: int,
    offset: int,
    verification_status: str | None,
) -> dict:
    path = Path(db_path)
    if not path.exists():
        raise HTTPException(404, f"no database at {db_path}: start a Run first")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.OperationalError as exc:  # pragma: no cover - fs-dependent
        raise HTTPException(500, f"cannot open database read-only: {exc}")
    conn.row_factory = sqlite3.Row
    try:
        where, params = "", []
        if verification_status is not None:
            where = " WHERE verification_status = ?"
            params.append(verification_status)
        # table is whitelisted (checked by the caller); values are parameterized.
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM {table}{where}", params
        ).fetchone()["n"]
        cur = conn.execute(
            f"SELECT * FROM {table}{where} LIMIT ? OFFSET ?", [*params, limit, offset]
        )
        columns = [d[0] for d in cur.description]
        rows = []
        for r in cur.fetchall():
            row = {}
            for col in columns:
                val = r[col]
                # BLOB columns (embeddings) are not JSON-serializable; summarize.
                row[col] = f"<{len(val)} bytes>" if isinstance(val, (bytes, bytearray)) else val
            rows.append(row)
    except sqlite3.OperationalError as exc:
        raise HTTPException(400, f"query failed: {exc}")
    finally:
        conn.close()
    return {
        "table": table,
        "columns": columns,
        "total": total,
        "limit": limit,
        "offset": offset,
        "rows": rows,
    }


# A Run Record id as this server will ever emit one, checked before it goes
# into a download filename: no path separators, no quotes, no header breaks.
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


def _default_engine_name() -> str | None:
    """The Engine `config/models.yaml` declares as the default, or None when
    the registry cannot be read. Never raises: an interface that cannot learn
    the default still has to render."""
    from regcompass.config import load_models

    try:
        return load_models().default_engine
    except Exception:  # noqa: BLE001 - a broken registry is not a 500 here
        return None


def _export_files(result: ExportResult, out_dir: Path) -> list[ExportFile]:
    """The files an Evidence Export just wrote, as download names, workbook
    first because that is the one a reviewer came for.

    Only files that exist and sit directly in the output directory are listed:
    the download endpoint refuses anything else, so a link it could not serve
    would be worse than no link. Nothing here is a filesystem path, which is
    the whole point: on the container path the output directory is inside a
    named volume and a reviewer cannot reach it by name."""
    out_dir = Path(out_dir).resolve()
    wanted: list[tuple[Path | None, str, bool]] = [
        (result.xlsx_path, "the organizer's workbook", True),
        (result.csv_path, "the same rows as CSV", False),
        (
            result.submission_path,
            "the rows as JSON, with model identities and quote context",
            False,
        ),
        (
            result.supplementary_path,
            "derived scores, the battery result and every disclosure",
            False,
        ),
    ]
    files: list[ExportFile] = []
    for path, label, primary in wanted:
        if path is None:
            continue
        resolved = Path(path).resolve()
        if resolved.parent != out_dir or not resolved.is_file():
            continue
        files.append(ExportFile(name=resolved.name, label=label, primary=primary))
    # The primary flag marks the workbook; with no workbook the first file
    # written becomes the one the interface offers first.
    if files and not any(f.primary for f in files):
        files[0].primary = True
    return files


def _read_runs(
    db_path: str,
    *,
    run_id: str | None = None,
    economy: str | None = None,
    kind: str | None = None,
    status: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> list[dict]:
    """Run Records over the SAME read-only ``mode=ro`` connection the browser's
    database tab uses, so serving a page never writes to the working database.
    A database that does not exist yet, or one predating Run Records, lists
    nothing: an idle server is not an error."""
    from regcompass.contracts import RunRecord

    path = Path(db_path)
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.OperationalError:  # pragma: no cover - fs-dependent
        return []
    conn.row_factory = sqlite3.Row
    try:
        sql = "SELECT * FROM runs WHERE 1=1"
        params: list[object] = []
        for column, value in (
            ("run_id", run_id), ("economy", economy), ("kind", kind), ("status", status)
        ):
            if value is not None:
                sql += f" AND {column} = ?"
                params.append(value)
        sql += " ORDER BY started_at DESC, rowid DESC LIMIT ? OFFSET ?"
        params.append(max(1, min(int(limit), MAX_LIMIT)))
        params.append(max(0, int(offset)))
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        # no runs table on this database (or an unreadable file): nothing to
        # list, not a 500. An idle or half-built server still serves its page.
        return []
    finally:
        conn.close()
    records = [RunRecord.from_row(dict(r)).model_dump() for r in rows]
    for record in records:
        # The Run's recorded events, when the app recorded them: a file beside
        # the working database, named after the Run.
        rid = record["run_id"]
        if _RUN_ID_RE.match(rid) and events_path(path, rid).is_file():
            record["events_file"] = events_file_name(rid)
    return records


def _count_runs(
    db_path: str,
    *,
    economy: str | None = None,
    kind: str | None = None,
    status: str | None = None,
) -> int:
    """How many Run Records match, over the same read-only connection."""
    path = Path(db_path)
    if not path.exists():
        return 0
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.OperationalError:  # pragma: no cover - fs-dependent
        return 0
    try:
        sql = "SELECT COUNT(*) FROM runs WHERE 1=1"
        params: list[object] = []
        for column, value in (("economy", economy), ("kind", kind), ("status", status)):
            if value is not None:
                sql += f" AND {column} = ?"
                params.append(value)
        return int(conn.execute(sql, params).fetchone()[0])
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def _audit_clock(ts: str) -> str:
    """A Run Record's ...Z timestamp in the audit log's +00:00 spelling, so
    the two compare as strings."""
    return ts[:-1] + "+00:00" if ts.endswith("Z") else ts


def _read_stage_times(
    db_path: str, since: str | None = None, until: str | None = None
) -> list[dict]:
    """Per-stage wall time from the working DB's audit_log, over the SAME
    read-only ``mode=ro`` connection the browser uses. ``since`` (an ISO-8601
    UTC string, the current Run's started_at) restricts the sum to THIS Run's
    rows, so the trace panel never shows the db's lifetime totals under a
    per-run clock; audit timestamps are the same UTC ISO format, so string
    comparison is sound. Returns [] when the DB or the audit_log table is
    missing/empty; never opens the DB writable, never raises (a fresh server
    with no DB must not 500 /api/stats)."""
    path = Path(db_path)
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.OperationalError:  # pragma: no cover - fs-dependent
        return []
    conn.row_factory = sqlite3.Row
    sql = (
        "SELECT stage, SUM(duration_ms) AS ms, MIN(timestamp) AS first_ts"
        " FROM audit_log WHERE duration_ms IS NOT NULL"
    )
    params: tuple = ()
    if since:
        sql += " AND timestamp >= ?"
        params += (_audit_clock(since),)
    if until:
        sql += " AND timestamp <= ?"
        params += (_audit_clock(until),)
    sql += " GROUP BY stage"
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []  # table absent on an empty/foreign DB
    finally:
        conn.close()
    return [
        {"stage": r["stage"], "ms": int(r["ms"]), "first_ts": r["first_ts"]}
        for r in rows
        if r["stage"] is not None and r["ms"] is not None
    ]


def _count_export_rows(out_dir: Path) -> int | None:
    """Cheap data-row count of the submission CSV once a Run has finished (the
    export's headline artifact). None when it is not present or unreadable - we
    never invent a number."""
    path = out_dir / "submission.csv"
    if not path.is_file():
        return None
    try:
        with path.open(encoding="utf-8-sig", newline="") as fh:
            n = sum(1 for _ in csv.reader(fh))
    except OSError:  # pragma: no cover - fs-dependent
        return None
    return max(0, n - 1)  # minus the header row


def _engine_or_none(name: str | None):
    """The named Engine, or None for an idle server / a config problem. Never
    raises: /api/stats must not 500."""
    if not name:
        return None
    try:
        return resolve_engine(name)
    except Exception:  # noqa: BLE001 - config-dependent; an idle panel, not a 500
        return None


def _models_block(name: str | None) -> dict:
    """{engine, names}: the models this Run ACTUALLY runs. The fake Engine runs
    no model at all, so it names none: claiming a model beside a zero cost
    would be a lie."""
    engine = _engine_or_none(name)
    if engine is None or engine.provider == "fake":
        return {"engine": name, "names": []}
    names: list[str] = []
    try:
        from regcompass.config import load_models

        embedder_name = load_models().embedder.name
        if embedder_name:
            names.append(embedder_name)
    except Exception:  # pragma: no cover - config-dependent
        pass
    names.append(engine.display_name)
    return {"engine": name, "names": names}


def _pricing_block(name: str | None) -> dict | None:
    """The Engine's declared prices, the only prices a cost here is ever
    computed from. None for an idle server or the fake Engine, which runs no
    model and is billed nothing."""
    engine = _engine_or_none(name)
    if engine is None or engine.provider == "fake":
        return None
    embedder = None
    try:
        from regcompass.config import load_models

        embedder = load_models().embedder.name or None
    except Exception:  # pragma: no cover - config-dependent
        pass
    return {
        "engine": engine.name,
        "display_name": engine.display_name,
        "model": engine.litellm_model,
        "open_weights": engine.open_weights,
        "usd_per_million_input_tokens": engine.usd_per_million_input_tokens,
        "usd_per_million_output_tokens": engine.usd_per_million_output_tokens,
        # The shared embedder runs on this machine: it is never billed.
        "embedder": embedder,
    }


def _meter_block(meter: dict | None, name: str | None) -> dict | None:
    """What this session's Run has spent so far: the meter's token counts,
    priced at the Engine's declared rates, beside the provider's own figure
    when it sends one. None when no Run of this session has metered."""
    if meter is None:
        return None
    engine = _engine_or_none(name)
    cost = 0.0
    if engine is not None:
        from regcompass.engines import cost_usd_for

        cost = cost_usd_for(
            engine, meter["prompt_tokens"], meter["completion_tokens"]
        )
    return {
        "prompt_tokens": meter["prompt_tokens"],
        "completion_tokens": meter["completion_tokens"],
        "calls": meter["calls"],
        "cost_usd": cost,
        "provider_cost_usd": meter["provider_cost_usd"],
    }


def _last_run_record(db_path: str) -> dict | None:
    """The newest COMPLETED Run Record, or None on a server that has not
    finished one. What a Run cost is a measurement read back off the record,
    never a flat rate multiplied by a document count."""
    records = _read_runs(db_path, kind="run", status="completed", limit=1)
    return records[0] if records else None


def _corpus_size(db_path: str, economy: str) -> int:
    """How many Documents this Economy's Corpus holds, read straight off the
    working database. Read-only and tolerant: a database that does not exist
    yet is an empty Corpus, which is exactly what it means."""
    path = Path(db_path)
    if not path.is_file():
        return 0
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:  # pragma: no cover - defensive
        return 0
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE economy = ?"
            " AND (full_text IS NOT NULL OR local_path IS NOT NULL)",
            (economy,),
        ).fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def _removal_summary(report, *, done: bool) -> str:
    """What removing one Document takes, in the words the confirmation
    shows. Past Runs are named because they are the part a reviewer cannot
    see from the Corpus list."""
    head = (
        f"{report.title} {'was' if done else 'will be'} removed from the Corpus"
        f" of {_official_name(report.economy)}."
    )
    if report.mappings == 0:
        return f"{head} No Run has mapped it, so no Run's evidence changes."
    runs = f"{report.runs} past Run{'' if report.runs == 1 else 's'}"
    reviews = (
        f", with {report.reviews} Review Decision{'' if report.reviews == 1 else 's'},"
        if report.reviews
        else ""
    )
    return (
        f"{head} {report.mappings} Mapping{'' if report.mappings == 1 else 's'}"
        f" from {runs} quote{'s' if report.mappings == 1 else ''} it"
        f"{reviews} and {'were' if done else 'will be'} removed with it; the Run"
        " Records themselves stay, so their counts still describe the Run as it"
        " ran."
    )


def _official_name(economy: str) -> str:
    portal = economies().get(economy)
    return portal.official_name if portal is not None else economy


def create_app(
    db_path: str | Path = "data/regcompass.db",
    out_dir: str | Path = "out",
    data_dir: str | Path = "data",
    bundle_manifest: str | Path | None = None,
    ui_dir: Path | None = UI_DIST,
    auth_user: str | None = None,
    auth_password: str | None = None,
) -> FastAPI:
    """Build the one app.

    ``db_path`` is the working database a Run writes and the audit view reads;
    ``out_dir`` is where the export lands; ``data_dir`` is the root for a
    Corpus Document's stored bytes and MUST be the one Discovery fetched them
    with. ``bundle_manifest`` switches the audit lane to a frozen bundle (the
    judge's keyless path). ``ui_dir=None`` skips the frontend mount, which is
    what an API test wants: no built bundle needed. ``auth_user`` and
    ``auth_password`` turn the login on (see login_settings); left as None each
    reads its variable, and a half-set pair raises ValueError here, before
    anything is built."""
    login = login_settings(auth_user, auth_password)
    # A fresh signing key per build, so a restart signs every browser out.
    sessions = SessionTokens(login[0]) if login is not None else None
    failed_login_queue = asyncio.Lock()
    db_path = str(db_path)
    out_dir = Path(out_dir)
    crawl_data_dir = str(data_dir)
    manager = RunManager()

    bundle = AuditBundle(Path(bundle_manifest)) if bundle_manifest else None

    def sweep_interrupted_runs() -> int:
        """Mark every Run Record still reading `running` as interrupted, and
        say how many there were.

        A row still reading `running` in a database this process has only just
        opened belongs to a process that is gone: the one worker thread lives
        here, and it has not started anything yet. Saying so now is the
        difference between a Runs list that explains a restart and one that
        shows a Run going nowhere for the rest of the hour. A database that
        does not exist yet is left alone: the first upload is what creates it,
        and an empty file here would only be a working database with nothing in
        it.
        """
        if bundle is not None or not Path(db_path).is_file():
            return 0
        from regcompass.storage import Storage as _Storage

        sweeper = None
        try:
            sweeper = _Storage(db_path)
            sweeper.apply_schema()
            return sweeper.mark_interrupted_runs()
        except sqlite3.Error:  # pragma: no cover - an unwritable or foreign file
            return 0
        finally:
            if sweeper is not None:
                sweeper.close()

    @asynccontextmanager
    async def lifespan(started: FastAPI):
        """The sweep runs when the application STARTS, not when it is built.

        Building an application must open nothing: a test that builds one over
        a scratch database, and any process that merely imports this module,
        would otherwise write to whatever database the path names. A live Run
        in the database this process is about to serve is only knowably dead
        once this process is the one serving it.
        """
        started.state.interrupted_runs = sweep_interrupted_runs()
        yield

    app = FastAPI(
        title="RegCompass", docs_url=None, redoc_url=None, lifespan=lifespan,
    )
    app.state.manager = manager
    # Until the application starts, nothing has been swept and the count says
    # so: an application built but never started reports zero rather than
    # raising on a state attribute that was never set.
    app.state.interrupted_runs = 0

    # -- the audit source seam ----------------------------------------------

    @contextmanager
    def audit_source(run_id: str | None = None, economy: str | None = None):
        """The data source behind the four audit read endpoints, for the length
        of one request. A frozen bundle is one object for the process; the
        working database is a fresh connection per request, because FastAPI
        runs sync endpoints on a threadpool and a SQLite connection belongs to
        the thread that made it."""
        if bundle is not None:
            yield bundle
            return
        from regcompass.storage import Storage

        if not Path(db_path).is_file():
            yield None
            return
        storage = Storage(db_path)
        try:
            resolved = run_id
            if resolved is None:
                try:
                    resolved = storage.latest_run_id(economy)
                except sqlite3.Error:  # a database with no runs table
                    resolved = None
            yield DatabaseAuditSource(storage, resolved, data_dir=crawl_data_dir)
        finally:
            storage.close()

    def reviews_of(source) -> dict[str, Review]:
        """This Run's Review Decisions, keyed by Mapping. A frozen bundle has
        none: it is a reading room, and the 409 on POST says so."""
        if bundle is not None or source is None:
            return {}
        return source.storage.reviews_for_run(_review_run_id(source))

    @contextmanager
    def review_lane(run_id: str | None):
        """The working-database source a Review Decision or a Gloss approval is
        written against, refusing the bundle lane before anything else happens.

        The schema is applied on the way in, exactly as the Run path does it: an
        operator can point the server at a working database made by an earlier
        build, and a write lane must not fail because that database predates a
        table. Reads never do this (a missing table reads as no rows), so a
        Run in progress is never blocked by a reader taking a write lock."""
        if bundle is not None:
            raise HTTPException(409, BUNDLE_REVIEW_MESSAGE)
        with audit_source(run_id) as source:
            if source is None:
                raise HTTPException(404, "no working database yet: start a Run first")
            source.storage.apply_schema()
            yield source

    def _safe_output_path(name: str) -> Path:
        if not name or name != Path(name).name or name.startswith("."):
            raise HTTPException(400, "invalid file name")
        path = (out_dir / name).resolve()
        if path.parent != out_dir.resolve():
            raise HTTPException(400, "invalid file name")
        if not path.is_file():
            raise HTTPException(404, f"no such output file: {name}")
        return path

    # -- registries the interface builds its controls from -------------------

    @app.get("/api/status")
    def status() -> dict:
        out = manager.status()
        configured = economies()
        out.update(
            {
                "db": db_path,
                "out_dir": str(out_dir),
                # How many Run Records the start-up sweep found abandoned by a
                # dead process. Zero until this application has actually
                # started, because nothing is swept before then.
                "interrupted_runs": app.state.interrupted_runs,
                # The interface builds its Economy and Pillar selects from
                # these, so the registry is the single source and the
                # interface carries no hard-coded list of its own.
                "economies": list(configured),
                "economy_names": {
                    code: portal.official_name for code, portal in configured.items()
                },
                # "Add document" builds its Language control from these, and
                # closes its URL lane on a manual-only Economy.
                "economy_languages": {
                    code: list(portal.languages)
                    for code, portal in configured.items()
                },
                "manual_only": [
                    code for code, portal in configured.items() if portal.manual_only
                ],
                # Economies with no Discovery plan wired yet. NOT the same list
                # as manual_only: these keep the add-by-URL lane and may gain a
                # strategy, so the interface explains them differently.
                "no_discovery": [
                    code
                    for code, portal in configured.items()
                    if portal.strategy == MANUAL_STRATEGY and not portal.manual_only
                ],
                "pillars": list(valid_pillars()),
                "default_pillars": list(default_pillars()),
                # The registry's own choice of Engine, so the Run panel opens
                # on it rather than on whichever Engine happens to carry a key.
                # One click on Run must never spend on an Engine nobody chose.
                "default_engine": _default_engine_name(),
                "bundle_mode": bundle is not None,
                # Whether a login guards this server, never the login itself:
                # the header shows its "Log out" control only when it does.
                "login": login is not None,
            }
        )
        return out

    # -- the login page and its session (a hosted copy only) -----------------

    def _over_https(request: Request) -> bool:
        # Caddy ends the HTTPS and forwards plain http, saying so in this header
        forwarded = request.headers.get("x-forwarded-proto", "")
        return request.url.scheme == "https" or (
            forwarded.split(",")[0].strip().lower() == "https"
        )

    @app.get("/login", include_in_schema=False)
    def show_login_page() -> Response:
        if login is None:
            return RedirectResponse("/", status_code=303)
        return HTMLResponse(
            LOGIN_PAGE_HTML,
            headers={
                "Cache-Control": "no-store",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "same-origin",
            },
        )

    @app.post("/api/login")
    async def sign_in(request: Request) -> JSONResponse:
        """Check the user name and password and, when they match, hand the
        browser a signed session cookie. A wrong pair waits a moment before
        the 401, so guessing is slow."""
        if login is None:
            raise HTTPException(404, "login is off on this server")
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not (
            isinstance(body, dict)
            and isinstance(body.get("user"), str)
            and isinstance(body.get("password"), str)
        ):
            raise HTTPException(400, "send the user name and password as text")
        ok = credentials_match(
            body["user"].encode("utf-8", "surrogatepass"),
            body["password"].encode("utf-8", "surrogatepass"),
            login[0].encode("utf-8"),
            login[1].encode("utf-8"),
        )
        if not ok:
            # One wrong login at a time, each waiting its turn, so guesses sent
            # in parallel still arrive at about two a second in total. Nobody
            # is locked out: behind Caddy every visitor shares one address.
            async with failed_login_queue:
                await asyncio.sleep(LOGIN_FAIL_DELAY)
            return JSONResponse({"detail": LOGIN_FAIL_MESSAGE}, status_code=401)
        response = JSONResponse({"ok": True})
        response.set_cookie(
            SESSION_COOKIE,
            sessions.issue(login[0]),
            max_age=SESSION_SECONDS,
            path="/",
            httponly=True,
            samesite="lax",
            secure=_over_https(request),
        )
        return response

    @app.post("/api/logout")
    def sign_out(request: Request) -> JSONResponse:
        response = JSONResponse({"ok": True})
        response.delete_cookie(
            SESSION_COOKIE,
            path="/",
            httponly=True,
            samesite="lax",
            secure=_over_https(request),
        )
        return response

    @app.get("/api/engines")
    def engines() -> dict:
        """The Engine registry as the Run panel's Engine control needs it:
        display name, open-weights flag, declared prices, and WHETHER a key is
        set. Never the key itself."""
        from regcompass.config import load_models

        models = load_models()
        out = []
        for name, engine in models.engines.items():
            out.append(
                {
                    "name": name,
                    "display_name": engine.display_name,
                    "open_weights": engine.open_weights,
                    "usd_per_million_input_tokens": engine.usd_per_million_input_tokens,
                    "usd_per_million_output_tokens": engine.usd_per_million_output_tokens,
                    "api_key_env": engine.api_key_env,
                    "key_set": key_is_set(engine),
                    "default": name == models.default_engine,
                }
            )
        return {"engines": out, "default_engine": models.default_engine}

    @app.get("/api/indicators")
    def indicators(pillar: int | None = None) -> dict:
        """The Indicators of one Pillar, for the Run panel's optional
        narrowing control. The live test names one Pillar and two Indicators."""
        from regcompass.config import load_indicators

        known = valid_pillars()
        if pillar is not None and pillar not in known:
            raise HTTPException(
                400, f"unknown pillar {pillar}: expected one of {list(known)}"
            )
        defs = load_indicators()
        return {
            "pillar": pillar,
            "indicators": [
                {"id": ind_id, "name": d.name, "pillar": d.pillar}
                for ind_id, d in defs.items()
                if (pillar is None or d.pillar == pillar) and d.legislation_mapped
            ],
        }

    # -- Settings: a key for this server session, never on disk --------------

    @app.post("/api/settings/key")
    def set_key(req: SettingsKeyRequest) -> dict:
        """Hold an Engine's key in this process for as long as it runs. The
        value is never echoed back, never logged and never written anywhere;
        restarting the server forgets it."""
        env = req.env
        engine_name = req.engine
        if env is None:
            if not engine_name:
                raise HTTPException(400, "name the Engine or the variable this key fills")
            try:
                engine = resolve_engine(engine_name)
            except UnknownEngine as e:
                raise HTTPException(400, str(e))
            env = engine.api_key_env
            if not env:
                raise HTTPException(
                    400,
                    f"Engine '{engine.name}' ({engine.display_name}) needs no key",
                )
        try:
            set_session_key(env, req.key)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"engine": engine_name, "env": env, "key_set": True}

    @app.delete("/api/settings/key/{engine_name}")
    def forget_key(engine_name: str) -> dict:
        try:
            engine = resolve_engine(engine_name)
        except UnknownEngine as e:
            raise HTTPException(400, str(e))
        if engine.api_key_env:
            forget_session_key(engine.api_key_env)
        return {"engine": engine.name, "env": engine.api_key_env, "key_set": False}

    @app.get("/api/settings/politeness")
    def politeness() -> dict:
        """The crawler's politeness limits, read-only, for the Settings card:
        one row per Portal we can contact, built from the same Portal
        configuration and constants the crawler uses. Nothing here can be
        changed from the interface."""
        from regcompass.contracts import CONNECTIONS_PER_HOST

        rows = [
            {
                "economy": code,
                "name": portal.official_name,
                "host": portal.hosts[0],
                "hosts": list(portal.hosts),
                "min_interval_seconds": portal.min_interval_seconds,
                "connections_per_host": CONNECTIONS_PER_HOST,
                "robots_respected": True,
                "robots_unavailable_setting": portal.robots_unavailable_policy,
                "robots_unavailable_policy": robots_unavailable_words(portal),
            }
            for code, portal in economies().items()
            if portal.hosts
        ]
        return {
            "connections_per_host": CONNECTIONS_PER_HOST,
            "robots_respected": True,
            "crawl_delay_rule": CRAWL_DELAY_RULE,
            "robots_unreachable_rule": ROBOTS_UNREACHABLE_RULE,
            "portals": rows,
        }

    # -- the Run panel -------------------------------------------------------

    @app.get("/api/stats")
    def stats() -> dict:
        """Live Run telemetry for the verification funnel and the trace panel.
        Must never 500 on a fresh server with an empty DB (returns zeros)."""
        st = manager.status()
        tel = manager.telemetry_snapshot()
        engine_name = tel.get("engine")
        rows_exported = None
        if st["status"] == "done":
            rows_exported = _count_export_rows(out_dir)
        last_run = _last_run_record(db_path)
        # Only this session's current/last job is traced: without a started_at
        # there is none, and the db's lifetime totals under a per-run clock
        # would misread as one Run's times. A server restarted after a Run
        # still traces that Run, over its own recorded window.
        if st.get("started_at"):
            stages = _read_stage_times(db_path, since=st["started_at"])
            stages_scope = "session"
        elif last_run is not None:
            stages = _read_stage_times(
                db_path, since=last_run["started_at"], until=last_run["ended_at"]
            )
            stages_scope = "last_run"
        else:
            stages = []
            stages_scope = None
        return {
            "active": st["active"],
            "status": st["status"],
            "elapsed_s": manager.elapsed_s(),
            "run": {
                "economy": tel.get("economy"),
                "engine": engine_name,
                "mode": tel.get("mode"),
                "n_chunks": tel.get("n_chunks", 0),
                "n_pairs_considered": tel.get("n_pairs_considered", 0),
                "n_documents": tel.get("n_documents", 0),
                "n_pairs_gated": tel.get("n_pairs_gated", 0),
                "n_passed": tel.get("n_passed", 0),
                "n_no_evidence": tel.get("n_no_evidence", 0),
                "n_dropped": tel.get("n_dropped", 0),
                "n_groups": tel.get("n_groups", 0),
                "pairs_done": tel.get("pairs_done", 0),
                "pairs_total": tel.get("pairs_total", 0),
            },
            "rows_exported": rows_exported,
            "run_id": st.get("run_id"),
            "stages": stages,
            "stages_scope": stages_scope,
            "models": _models_block(engine_name),
            "pricing": _pricing_block(engine_name),
            # This session's Run's spend as it grows: measured tokens times the
            # declared prices, the same arithmetic the Run Record closes with.
            "meter": _meter_block(manager.meter_snapshot(), engine_name),
            # What a Run cost is READ OFF the last completed Run Record: tokens
            # counted from the models' own usage figures times the Engine's
            # declared prices. No flat rate, no estimate.
            "last_run": last_run,
            "last_run_pricing": _pricing_block(
                last_run["engine"] if last_run is not None else None
            ),
        }

    @app.post("/api/run")
    def run(req: RunRequest) -> dict:
        configured = economies()
        economy = (req.economy or "").upper()
        if economy not in configured:
            raise HTTPException(
                400,
                f"unknown economy '{req.economy}': expected one of"
                f" {', '.join(configured)}",
            )

        known_pillars = valid_pillars()
        if req.pillars is None:
            pillars: tuple[int, ...] = default_pillars()
        else:
            bad = [p for p in req.pillars if p not in known_pillars]
            if bad or not req.pillars:
                raise HTTPException(
                    400,
                    f"invalid pillars {req.pillars}: expected a subset of"
                    f" {list(known_pillars)}",
                )
            pillars = tuple(sorted(set(req.pillars)))

        # Indicator narrowing, checked against the Run's Pillars before anything
        # starts: the live test runs one Pillar and two Indicators, and a Run
        # that quietly ignored a typo would answer a different question.
        from regcompass.config import UnknownIndicator, narrow_indicators

        try:
            indicators = narrow_indicators(req.indicators, pillars)
        except UnknownIndicator as e:
            raise HTTPException(400, str(e))

        engine_name = (req.engine or "").lower() or None
        mode = (req.mode or "run").lower()
        if mode not in ("run", "e2e"):
            raise HTTPException(400, f"unknown mode '{req.mode}': expected 'run' or 'e2e'")

        # Mapping calls in flight. Clamped rather than refused (the same
        # treatment max_documents gets): the number is a speed dial, and the
        # Engine's own declared value is the one that applies when none is
        # given.
        concurrency = (
            None if req.concurrency is None
            else max(1, min(int(req.concurrency), MAX_CONCURRENCY))
        )

        try:
            engine = resolve_engine(engine_name)
        except UnknownEngine as e:
            raise HTTPException(400, str(e))
        # Fail fast on a missing key (mirrors the CLI): the Run would otherwise
        # burn the transport ladder on an unfixable error. The message points
        # at Settings, which is where a reviewer fixes it.
        try:
            preflight_key(engine)
        except ConfigError as e:
            raise HTTPException(400, str(e))

        if mode == "e2e":
            max_documents = max(1, min(int(req.max_documents or 1), 10))
            worker = _make_e2e_worker(
                economy, pillars, engine, db_path, crawl_data_dir, str(out_dir),
                max_documents, indicators, concurrency=concurrency,
            )
            meta = {
                "economy": economy, "pillars": list(pillars),
                "engine": engine.name, "mode": "e2e", "db": db_path,
                "max_documents": max_documents,
                "indicators": None if indicators is None else list(indicators),
                "concurrency": concurrency,
            }
        else:
            # Built below, once its Run Record exists: the worker adopts that
            # record instead of opening one of its own.
            worker = None
            meta = {
                "economy": economy, "pillars": list(pillars),
                "engine": engine.name, "mode": "run", "db": db_path,
                "indicators": None if indicators is None else list(indicators),
                "concurrency": concurrency,
            }

        # Does this Economy have a Corpus? A Run reads one and never fetches,
        # so an empty Corpus is a missing prerequisite, not a failure halfway
        # through. e2e starts WITH a Discovery, so an empty Corpus is its
        # normal starting state and it is allowed through.
        n_documents = _corpus_size(db_path, economy)
        if mode == "run" and n_documents == 0:
            raise HTTPException(
                409,
                {
                    "corpus_empty": True,
                    "economy": economy,
                    "message": (
                        f"no Documents in the Corpus for {_official_name(economy)};"
                        " run Discovery for this Economy first"
                    ),
                },
            )

        # The Run Record is opened HERE, on the request thread, before the
        # answer leaves. It used to be opened by the worker, a few hundred
        # milliseconds later and inside a thread, so a server killed between
        # "started" and the first model call left NO trace of a Run that had
        # certainly begun: the restart sweep had nothing to mark interrupted
        # and the Runs list simply lost it. Now the row exists the moment the
        # caller is told the Run started, and the worker closes it.
        opened_run_id: str | None = None
        if mode == "run":
            from regcompass.storage import new_run_id, utc_now_z

            # The same opening every other write lane takes, directory and
            # schema included: this is now the FIRST write of the Run, so on a
            # fresh clone it is the one that has to make room.
            record_storage = _open_for_write(db_path)
            opened_run_id = new_run_id("run")
            record_storage.run_start(
                run_id=opened_run_id, kind="run", economy=economy,
                pillars=list(pillars),
                indicators=None if indicators is None else list(indicators),
                engine=engine.name, started_at=utc_now_z(),
            )
            run_documents = _run_documents(record_storage, economy)
            record_storage.close()
            worker = _make_real_worker(
                economy, pillars, engine, db_path, crawl_data_dir, indicators,
                concurrency=concurrency, run_id=opened_run_id,
                documents=run_documents,
            )

        if not manager.start(worker, meta):
            if opened_run_id is not None:
                # The slot was taken between opening the record and asking for
                # it. Nothing ran, and an open record nobody will ever close
                # would read as a Run in progress, so it is closed here saying
                # exactly what happened.
                from regcompass.storage import utc_now_z

                record_storage = _open_for_write(db_path)
                record_storage.run_finish(
                    opened_run_id, status="failed", ended_at=utc_now_z(),
                    error="not started: another job held the single worker slot",
                )
                record_storage.close()
            raise HTTPException(409, "a Run is already active; wait for it to finish")
        return {
            "status": "started", "economy": economy, "pillars": list(pillars),
            "indicators": None if indicators is None else list(indicators),
            "engine": engine.name, "mode": mode,
            "corpus_empty": n_documents == 0, "corpus_documents": n_documents,
        }

    @app.post("/api/discover")
    def discover(req: DiscoverRequest) -> dict:
        """Fill one Economy's Corpus from its Portal. The only endpoint that
        touches the internet."""
        configured = economies()
        economy = (req.economy or "").upper()
        if economy not in configured:
            raise HTTPException(
                400,
                f"unknown economy '{req.economy}': expected one of"
                f" {', '.join(configured)}",
            )
        portal = configured[economy]
        # Two different refusals, and the interface has to be able to say which
        # one it is: a Portal whose rules do not permit automated collection
        # never gains a strategy and has no URL lane; an Economy whose plan is
        # simply not wired yet keeps both possibilities.
        if portal.manual_only:
            raise HTTPException(
                400,
                f"{portal.official_name} ({economy}) is manual-only: its"
                " Portal's own site rules do not permit automated collection,"
                " so Discovery never runs here. Upload each Document instead.",
            )
        if portal.strategy == MANUAL_STRATEGY:
            raise HTTPException(
                400,
                f"{portal.official_name} ({economy}) has no Discovery strategy"
                " configured, so Discovery fetches nothing for this Economy."
                " Add each Document by its official Source URL where the"
                " Portal host is whitelisted, or by uploading the file.",
            )

        worker = _make_discover_worker(economy, bool(req.refresh), db_path, crawl_data_dir)
        meta = {
            "economy": economy, "pillars": [], "engine": None,
            "mode": "discover", "db": db_path, "refresh": bool(req.refresh),
        }
        if not manager.start(worker, meta):
            raise HTTPException(409, "a Run is already active; wait for it to finish")
        return {
            "status": "started", "economy": economy, "mode": "discover",
            "refresh": bool(req.refresh),
            "corpus_documents": _corpus_size(db_path, economy),
        }

    # -- Starting empty: clear the downloads and the caches -------------------

    def _clear_scope(economy: str | None) -> str | None:
        """The Economy a clear is scoped to, or None for everything. An unknown
        code is a 404 rather than a silent whole-database clear."""
        if not economy:
            return None
        code = economy.upper()
        configured = economies()
        if code not in configured:
            raise HTTPException(
                404,
                f"unknown economy '{economy}': expected one of"
                f" {', '.join(configured)}",
            )
        return code

    @contextmanager
    def clear_lane():
        """The working database a clear reads or empties. A frozen bundle is a
        reading room and holds nothing to clear, and a database that does not
        exist yet is already empty."""
        from regcompass.storage import Storage

        if bundle is not None:
            raise HTTPException(409, BUNDLE_CLEAR_MESSAGE)
        if not Path(db_path).is_file():
            yield None
            return
        storage = Storage(db_path)
        try:
            yield storage
        finally:
            storage.close()

    @app.get("/api/clear/preview")
    def clear_preview(economy: str | None = None) -> dict:
        """What a clear of this scope would remove. Reads only: the interface
        shows these counts in the confirmation, and the reviewer agrees to
        exactly them."""
        from regcompass.storage import ClearReport

        scope = _clear_scope(economy)
        with clear_lane() as storage:
            if storage is None:
                return ClearReport(economy=scope).as_dict()
            return storage.clear_preview(scope, data_dir=crawl_data_dir).as_dict()

    @app.post("/api/clear")
    def clear(req: ClearRequest) -> dict:
        """Remove the downloaded Documents, the stored text and the Run Records
        of one Economy, or of all of them. The step a steward asks for before
        the clock starts: after it the Corpus and the Runs screens are empty for
        that scope, and the next Run fetches and extracts again."""
        from regcompass.storage import ClearReport

        scope = _clear_scope(req.economy)
        if not req.confirm:
            raise HTTPException(
                400,
                "a clear removes Documents, stored text and Run Records:"
                " send confirm=true to go ahead",
            )
        if manager.status()["active"]:
            raise HTTPException(
                409,
                "a job is already active; wait for it to finish before clearing",
            )
        with clear_lane() as storage:
            if storage is None:
                return ClearReport(economy=scope).as_dict()
            # An operator can point the server at a database an earlier build
            # wrote, and the clear has to know every table before it empties it.
            storage.apply_schema()
            return storage.clear(scope, data_dir=crawl_data_dir).as_dict()

    # -- "Add document": the other way a Document enters a Corpus ------------

    def _validated_add(economy: str, language: str | None):
        """The checks an add must clear BEFORE any Run Record is opened: a
        known Economy and a Language on the organizers' own list. Nothing is
        written until these pass, so a refused add leaves no trace to explain
        away."""
        from regcompass.contracts import ORGANIZER_LANGUAGES

        code = (economy or "").upper()
        configured = economies()
        if code not in configured:
            raise HTTPException(
                400,
                f"unknown economy '{economy}': expected one of"
                f" {', '.join(configured)}",
            )
        portal = configured[code]
        language = language or portal.languages[0]
        if language not in ORGANIZER_LANGUAGES:
            raise HTTPException(
                400,
                f"'{language}' is not on the organizers' Language list"
                f" ({', '.join(ORGANIZER_LANGUAGES)}); use 'Other' where the"
                " Document's language is not listed",
            )
        return code, portal, language

    def _manual_add(economy: str, language: str, source: str, add) -> dict:
        """Run one add under its own Run Record.

        The record is a Discovery record (kind 'discovery') carrying
        `manual: true`: an add is the same event a Discovery is, one Document
        entering a Corpus, and filing it the same way means the Runs list and
        the downloadable record need no second shape for it. Its engine is
        None, because no model was called."""
        from regcompass.corpus import (
            DuplicateDocumentError,
            HostNotAllowedError,
            ManualOnlyEconomyError,
        )
        from regcompass.storage import new_run_id, utc_now_z

        if manager.status()["active"]:
            raise HTTPException(
                409, "a Run is already active; wait for it to finish"
            )
        # The directory and the schema, exactly as every other write lane takes
        # them. An add is the first thing an assessor does on a fresh clone:
        # `data/` is gitignored, so neither the directory, the database nor the
        # tables exist yet, and without this the add reaches the browser as a
        # bare Internal Server Error before the app has said anything at all.
        storage = _open_for_write(db_path)
        run_id = new_run_id("discovery")
        storage.run_start(
            run_id=run_id, kind="discovery", economy=economy, pillars=[],
            indicators=None, engine=None, started_at=utc_now_z(),
        )
        details = {
            "strategy": "manual", "manual": True, "source": source,
            "language": language,
        }
        try:
            added = add(storage)
        except Exception as exc:
            # A refusal (these bytes are already here, a host off the
            # whitelist, a file we cannot read) is the add doing its job, not
            # the add breaking. The record says which, so Run history can
            # show "Refused" rather than "Stopped".
            refused = isinstance(
                exc, (DuplicateDocumentError, HostNotAllowedError, ManualOnlyEconomyError, ValueError)
            )
            storage.run_finish(
                run_id, status="failed", ended_at=utc_now_z(),
                documents_fetched=0,
                error=f"{type(exc).__name__}: {exc}"[:500],
                details={**details, "outcome": "refused" if refused else "failed"},
            )
            storage.close()
            if isinstance(exc, DuplicateDocumentError):
                raise HTTPException(409, str(exc)) from exc
            if isinstance(exc, (HostNotAllowedError, ManualOnlyEconomyError, ValueError)):
                raise HTTPException(400, str(exc)) from exc
            if type(exc).__name__ == "RobotsUnavailableError":
                # The Portal cannot show its rules right now, so we did not ask
                # it for anything. That is a "come back later", not our error
                # and not a bad request: it reads better as 503 with the
                # message than as a class name behind a 502.
                raise HTTPException(503, str(exc)) from exc
            raise HTTPException(502, f"{type(exc).__name__}: {exc}") from exc
        details.update({
            "source_url": added.source_url, "document_id": added.document_id,
        })
        if added.robots_note:
            # Same sentence, same keys, as a Discovery record carries: this add
            # went ahead without reading the Portal's rules, under RFC 9309's
            # 30-day rule or under the Portal's own configured policy, and the
            # record says which and what the Portal answered.
            details["robots"] = added.robots_note
        if added.robots_unavailable_status is not None:
            details["robots_unavailable_status"] = added.robots_unavailable_status
        if added.robots_unavailable_policy:
            details["robots_unavailable_policy"] = added.robots_unavailable_policy
        storage.run_finish(
            run_id, status="completed", ended_at=utc_now_z(),
            documents_fetched=1, details=details,
        )
        storage.close()
        answer = {
            "status": "added",
            "run_id": run_id,
            "document_id": added.document_id,
            "title": added.title,
            "economy": added.economy,
            "language": added.language,
            "source_url": added.source_url,
            "source_kind": added.source_kind,
            "n_pages": added.n_pages,
            "n_low_yield_pages": added.n_low_yield_pages,
            "ocr_applied": added.ocr_applied,
            "corpus_documents": _corpus_size(db_path, added.economy),
        }
        if added.warning:
            answer["warning"] = added.warning
        if added.robots_note:
            # The answer an operator reads says the same thing the record does:
            # this Document was fetched from a Portal whose rules we could not
            # read, and here is what it answered and what let us go ahead.
            answer["robots"] = added.robots_note
            if added.robots_unavailable_status is not None:
                answer["robots_unavailable_status"] = added.robots_unavailable_status
            if added.robots_unavailable_policy:
                answer["robots_unavailable_policy"] = added.robots_unavailable_policy
        return answer

    @app.post("/api/documents/upload")
    def upload_document(
        economy: str = Form(...),
        source_url: str | None = Form(None),
        language: str | None = Form(None),
        title: str | None = Form(None),
        file: UploadFile = File(...),
    ) -> dict:
        """Add a Document from a file the reviewer picked. Open to every
        Economy, including the ones whose Portal forbids automated
        collection: nothing is fetched here.

        `title` is the statute's own name. It is optional, and it is worth
        typing: without it the Document is named after the file, and that
        name is what the Evidence Export writes in the Law Name column.

        `source_url` is optional too. A reviewer working against the clock with
        a file they saved by hand can get it into the Corpus and Run it now;
        its rows then follow the local-copy rule, and the Evidence Export
        refuses them by name until the official address is recorded."""
        from regcompass.corpus import add_document

        code, _portal, language = _validated_add(economy, language)
        raw = file.file.read()
        if not raw:
            raise HTTPException(400, "the uploaded file is empty")
        return _manual_add(
            code, language, "upload",
            lambda storage: add_document(
                storage, crawl_data_dir, code, raw,
                source_url=source_url, language=language, title=title,
                filename_hint=file.filename,
            ),
        )

    @app.post("/api/documents/add-url")
    def add_document_by_url(req: AddUrlRequest) -> dict:
        """Add a Document by its official Source URL: ONE polite fetch, under
        the same robots check, politeness floor and identified user agent
        Discovery uses. Closed on a manual-only Economy, whose Portal rules do
        not distinguish one request from a crawl."""
        from regcompass.corpus import add_document_from_url, check_host_allowed

        code, portal, language = _validated_add(req.economy, req.language)
        if portal.manual_only:
            raise HTTPException(
                400,
                f"{portal.official_name} ({code}) is manual-only: its Portal's"
                " own site rules forbid automated collection, and adding by URL"
                " is still a request we would make. Upload the file instead.",
            )
        try:
            check_host_allowed(
                portal, req.source_url, allow_any_host=req.allow_any_host
            )
        except Exception as exc:
            raise HTTPException(400, str(exc)) from exc
        return _manual_add(
            code, language, "url",
            lambda storage: add_document_from_url(
                storage, crawl_data_dir, code, req.source_url,
                language=language, allow_any_host=req.allow_any_host,
                title=req.title,
            ),
        )

    @app.patch("/api/documents/{document_id}")
    def set_source_url(document_id: str, req: SetSourceUrlRequest) -> dict:
        """Record where an already-added Document is published.

        The other half of an optional Source URL on upload. Without this a
        Document added without an address could never gain one: adding it
        again by URL would FETCH the file a second time and put a second
        Document in the Corpus under a second id, which is not a correction,
        it is a duplicate. This edits the Document that is already there, so
        its rows stop following the local-copy rule and the Evidence Export
        stops refusing them."""
        url = (req.source_url or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            raise HTTPException(
                400,
                "a Source URL must be the http(s) address the Document is"
                f" published at; got {url!r}",
            )
        if bundle is not None:
            raise HTTPException(409, BUNDLE_REVIEW_MESSAGE)
        # A Run reads the Corpus while it works, and an ingest rewrites the
        # rows this touches, so the edit waits rather than racing either.
        if manager.status()["active"]:
            raise HTTPException(
                409, "a job is already active; wait for it to finish"
            )
        from regcompass.storage import Storage

        if not Path(db_path).is_file():
            raise HTTPException(404, f"no such Document: {document_id}")
        storage = Storage(db_path)
        try:
            previous = storage.set_document_source_url(document_id, url)
        except LookupError:
            raise HTTPException(404, f"no such Document: {document_id}") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        finally:
            storage.close()
        return {
            "document_id": document_id,
            "source_url": url,
            "previous_source_url": previous,
        }

    def _removal(document_id: str, *, remove: bool) -> dict:
        """Remove one Document, or say what removing it would take. The same
        guards every write lane takes: not in bundle mode, not while a job
        runs."""
        if bundle is not None:
            raise HTTPException(409, BUNDLE_REVIEW_MESSAGE)
        if remove and manager.status()["active"]:
            raise HTTPException(
                409, "a job is already active; wait for it to finish"
            )
        from regcompass.storage import Storage

        if not Path(db_path).is_file():
            raise HTTPException(404, f"no such Document: {document_id}")
        storage = Storage(db_path)
        try:
            storage.apply_schema()
            act = storage.remove_document if remove else storage.remove_document_preview
            report = act(document_id, data_dir=crawl_data_dir)
        except LookupError:
            raise HTTPException(404, f"no such Document: {document_id}") from None
        finally:
            storage.close()
        answer = report.as_dict()
        answer["corpus_documents"] = _corpus_size(db_path, report.economy)
        answer["summary"] = _removal_summary(report, done=remove)
        return answer

    @app.get("/api/documents/{document_id}/removal")
    def document_removal_preview(document_id: str) -> dict:
        """What removing this Document would take, in counts and in one plain
        sentence, before anything is removed."""
        return _removal(document_id, remove=False)

    @app.delete("/api/documents/{document_id}")
    def remove_document(document_id: str) -> dict:
        """Take one Document out of its Economy's Corpus: the way out of a bad
        add that does not clear the whole Economy and its Runs."""
        return _removal(document_id, remove=True)

    @app.get("/api/corpus")
    def corpus(economy: str) -> dict:
        """One Economy's CORPUS: the Documents a Run would read.

        Not the same list as /api/documents, and the difference is the point.
        That one answers "what did this Run map", so a Document nothing has
        mapped yet is invisible in it, and an upload therefore seemed to vanish
        until a Run finished. This answers "what is in the Corpus", which is
        what somebody who just added a file needs to see, immediately."""
        code = economy.upper()
        configured = economies()
        if code not in configured:
            raise HTTPException(
                404,
                f"unknown economy '{economy}': expected one of"
                f" {', '.join(configured)}",
            )
        from regcompass.storage import Storage

        if not Path(db_path).is_file():
            return {"economy": code, "n": 0, "documents": []}
        storage = Storage(db_path)
        try:
            rows = storage.corpus_documents(code)
        except sqlite3.Error:  # a database made before the documents table
            rows = []
        finally:
            storage.close()
        # Read through dicts, not row keys: a working database made by an older
        # build may predate a column, and a missing one must read as "not
        # recorded" rather than break the screen that lists the Corpus.
        documents = []
        for row in rows:
            d = dict(row)
            documents.append(
                {
                    "document_id": d["document_id"],
                    "title": d.get("title") or d["document_id"],
                    "source_kind": d.get("source_kind"),
                    "language": d.get("language"),
                    "source_url": d.get("source_url"),
                    "n_pages": d.get("n_pages"),
                    "ocr_applied": bool(d.get("ocr_applied")),
                    "added_at": d.get("created_at"),
                }
            )
        return {"economy": code, "n": len(documents), "documents": documents}

    @app.get("/api/corpus/summary")
    def corpus_summary() -> dict:
        """Every Economy's Corpus as it is now: how many Documents, and the
        languages those Documents are, most common first. What the Start a Run
        cards show, so neither the size of the Corpus at the last Run nor the
        Portal's declared languages stands in for it. A Document with no
        language recorded is read as its Portal's language where the Portal
        declares exactly one (a Singapore Act with no language stored is
        English, and saying "not recorded" there reads as a fault); where the
        Portal declares several, it is counted in "unrecorded", never guessed."""
        from regcompass.storage import Storage

        by_economy: dict[str, dict[str | None, int]] = {}
        if Path(db_path).is_file():
            storage = Storage(db_path)
            try:
                by_economy = storage.corpus_languages()
            except sqlite3.Error:  # a database made before the documents table
                by_economy = {}
            finally:
                storage.close()
        out = {}
        for code, portal in economies().items():
            counts = dict(by_economy.get(code, {}))
            if None in counts and len(portal.languages) == 1:
                only = portal.languages[0]
                counts[only] = counts.get(only, 0) + counts.pop(None)
            named = sorted(
                (lang for lang in counts if lang is not None),
                key=lambda lang: (-counts[lang], lang),
            )
            out[code] = {
                "n": sum(counts.values()),
                "languages": named,
                "unrecorded": counts.get(None, 0),
            }
        return {"economies": out}

    @app.get("/api/run/log")
    def run_log() -> dict:
        """The progress lines the last job wrote, still buffered.

        The live stream ends when the Run does, and the interface then moves
        the reviewer on to the evidence. Coming back to the Run panel and
        finding the placeholder text again reads as though the Run never
        happened, so the panel asks for these and shows them until the next
        Run clears the buffer."""
        state = manager.status()
        return {
            "lines": [line["msg"] for line in manager.lines_since(0)],
            # The typed events too, so a reload after the Run shows the same
            # picture of its Documents and Steps.
            "events": manager.events_since(0),
            "status": state["status"],
            "active": state["active"],
            "run_id": state.get("run_id"),
        }

    @app.get("/api/events")
    def events() -> StreamingResponse:
        return StreamingResponse(
            _sse(manager),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # -- the Runs list -------------------------------------------------------

    @app.get("/api/runs")
    def runs(
        economy: str | None = None,
        kind: str | None = None,
        status: str | None = None,
        limit: int = DEFAULT_LIMIT,
        offset: int = Query(0, ge=0),
    ) -> dict:
        """Past Run Records, newest first. The record of truth: the live
        narration is memory, this survives a restart.

        A page at a time, and `total` says how many match, so a list never
        silently loses its oldest rows: every add files a record, and a busy
        afternoon of adds used to push real Runs off a fixed newest-50 list.
        `kind` and `status` narrow it on the server, which is how a screen
        that needs every finished Run (the featured Economies, the Comparison
        pickers) asks for exactly those."""
        economy = None if economy is None else economy.upper()
        return {
            "runs": _read_runs(
                db_path, economy=economy, kind=kind, status=status,
                limit=limit, offset=offset,
            ),
            "total": _count_runs(db_path, economy=economy, kind=kind, status=status),
            "offset": offset,
        }

    @app.get("/api/audit/run")
    def audit_run(run_id: str | None = None, economy: str | None = None) -> dict:
        """WHICH Run the audit screens are showing right now.

        Every audit read falls back to the newest completed Run when no Run is
        named, and that fallback used to be invisible: the Evidence screen
        showed a Run id only when the reviewer had arrived from a Run panel or
        a Runs row, so a plain page load left them reading rows with no idea
        whose they were. This resolves the same way those reads do and hands
        back the Run Record, so the screen can always name it."""
        with audit_source(run_id, economy) as source:
            resolved = None if source is None else getattr(source, "run_id", None)
        found = (
            [] if resolved is None else _read_runs(db_path, run_id=resolved, limit=1)
        )
        return {
            "run_id": resolved,
            "record": found[0] if found else None,
            "bundle_mode": bundle is not None,
        }

    @app.get("/api/runs/{run_id}")
    def run_record(run_id: str) -> dict:
        found = _read_runs(db_path, run_id=run_id, limit=1)
        if not found:
            raise HTTPException(404, f"no Run Record '{run_id}'")
        return found[0]

    @app.get("/api/runs/{run_id}/replay")
    def run_replay(
        run_id: str,
        speed: float = DEFAULT_REPLAY_SPEED,
        from_seq: int = Query(0, ge=0),
    ) -> StreamingResponse:
        """Watch a Run again: its recorded events, in the live stream's own
        format, paced by the recorded times at `speed` times real time. The
        screen pauses by closing the stream and resumes by asking again from
        the next sequence number (`from_seq`), so it keeps one code path for a
        live Run and a recorded one."""
        if not valid_speed(speed):
            raise HTTPException(400, f"speed must be above 0 and at most 1000000, not {speed}")
        if not _RUN_ID_RE.match(run_id):
            raise HTTPException(404, f"no recorded events for Run '{run_id}'")
        path = events_path(db_path, run_id)
        if not path.is_file():
            raise HTTPException(404, f"no recorded events for Run '{run_id}'")
        try:
            recorded = read_events(path)
        except (OSError, ValueError) as exc:
            raise HTTPException(500, f"the recorded events of '{run_id}' cannot be read: {exc}")
        rest = [e for e in recorded if isinstance(e.get("seq"), int) and e["seq"] >= from_seq]
        return StreamingResponse(
            replay_stream(rest, speed, run_id=run_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/runs/{run_id}/download")
    def run_record_download(run_id: str) -> Response:
        """The Run Record as a JSON file: the organizers ask for a run record
        as a hand-in, so it has to leave the browser as a file."""
        found = _read_runs(db_path, run_id=run_id, limit=1)
        if not found:
            raise HTTPException(404, f"no Run Record '{run_id}'")
        if not _RUN_ID_RE.match(run_id):  # pragma: no cover - ids are ours
            raise HTTPException(400, "invalid Run Record id")
        return Response(
            content=json.dumps(found[0], indent=2, sort_keys=True),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{run_id}.json"'},
        )

    # -- the Comparison ------------------------------------------------------

    def _one_record(run_id: str):
        from regcompass.contracts import RunRecord

        found = _read_runs(db_path, run_id=run_id, limit=1)
        if not found:
            raise HTTPException(404, f"no Run Record '{run_id}'")
        return RunRecord(**found[0])

    def _auto_pair(economy: str, pillar: int):
        """The two Runs a reviewer means by "this Economy on both Engines": the
        newest COMPLETED Run of each Engine that covered this Pillar, over the
        SAME Pillar set. A Comparison is one Pillar set seen by two Engines, so
        a Pillar 6 and 7 Run never pairs with a Pillar 6 Run: of the Pillar
        sets both Engines have run, the one with the newest Run wins. Ordered
        by the Engine registry, so Engine A is always the left column and the
        screen does not swap sides between visits."""
        from regcompass.config import load_models
        from regcompass.contracts import RunRecord

        rows = _read_runs(
            db_path, economy=economy, kind="run", status="completed", limit=MAX_LIMIT
        )
        # Pillar set -> Engine -> that Engine's newest Run on exactly that set.
        by_set: dict[tuple[int, ...], dict[str, RunRecord]] = {}
        for row in rows:  # newest first
            record = RunRecord(**row)
            if record.engine and pillar in record.pillars:
                key = tuple(sorted(set(record.pillars)))
                by_set.setdefault(key, {}).setdefault(record.engine, record)
        engines_seen = {e for newest in by_set.values() for e in newest}
        pairable = {k: v for k, v in by_set.items() if len(v) >= 2}
        if not pairable:
            if len(engines_seen) >= 2:
                # Both Engines have run this Pillar, but never over the same
                # Pillars, and a Comparison of a two-Pillar Run against a
                # one-Pillar Run would set evidence beside a Pillar the other
                # side never searched.
                described = "; ".join(
                    f"{' and '.join(f'Pillar {p}' for p in key)} on"
                    f" {', '.join(sorted(newest))}"
                    for key, newest in sorted(by_set.items())
                )
                raise HTTPException(
                    404,
                    f"Run {_official_name(economy)} on the other Engine over the"
                    " same Pillars to compare. A Comparison puts two Engines side"
                    " by side on the same Pillars, and the completed Runs here"
                    f" cover different ones ({described}). Or pick two Runs"
                    " yourself.",
                )
            # What to DO, first: a reader who is told only that the count is 1
            # has been given a fact and no next step.
            raise HTTPException(
                404,
                f"Run {_official_name(economy)} Pillar {pillar} on the other"
                " Engine to compare. A Comparison puts two Engines side by side"
                " on the same Economy and Pillar, and this one has a completed"
                f" Run on {len(engines_seen) or 'no'} Engine so far.",
            )
        # The pair is chosen by the Engine REGISTRY, not by recency across
        # Engines: the declared Engines come first, so a newer rehearsal Run on
        # the fake Engine can never displace Engine A. Registry order is also
        # what puts Engine A in the left column on every visit.
        models = load_models()
        order = list(models.engines)

        def ranked(newest: dict[str, RunRecord]) -> list[RunRecord]:
            return sorted(
                newest.values(),
                key=lambda r: (
                    order.index(r.engine) if r.engine in order else len(order),
                    r.started_at,
                ),
            )[:2]

        def rank_of_set(item) -> tuple:
            pair = ranked(item[1])
            engines_rank = tuple(
                order.index(r.engine) if r.engine in order else len(order) for r in pair
            )
            newest_started = max(r.started_at for r in item[1].values())
            # Declared Engines first, then the set with the newest Run. The
            # timestamp sorts descending by being negated character-wise.
            return (engines_rank, tuple(-ord(c) for c in newest_started))

        _key, newest = min(pairable.items(), key=rank_of_set)
        chosen = ranked(newest)
        undeclared = [
            r.engine for r in chosen
            if r.engine in models.engines and models.engines[r.engine].provider == "fake"
        ]
        note = None
        if undeclared:
            note = (
                "this Comparison uses a non-declared Engine"
                f" ({', '.join(undeclared)}): fewer than two declared Engines have a"
                f" completed Run on {_official_name(economy)} Pillar {pillar}"
            )
        return chosen[0], chosen[1], note

    def _indicator_map(record_a, record_b, pillar: int | None) -> dict[str, str]:
        """Which Indicators the Comparison lists, in registry order. A Pillar's
        whole set, narrowed to what the two Runs actually ran when BOTH were
        narrowed: a two-Indicator live test must produce a two-row Comparison,
        not five rows with three empty pairs."""
        from regcompass.config import indicator_ids, load_indicators

        pillars = (
            tuple(sorted(set(record_a.pillars))) if pillar is None else (pillar,)
        )
        ids = indicator_ids(pillars)
        if record_a.indicators and record_b.indicators:
            wanted = set(record_a.indicators) | set(record_b.indicators)
            ids = tuple(i for i in ids if i in wanted)
        defs = load_indicators()
        return {i: (defs[i].name if i in defs else "") for i in ids}

    def _build_comparison(
        run_a: str | None, run_b: str | None, economy: str | None, pillar: int | None
    ):
        from regcompass.compare import ComparisonMismatch, compare_runs
        from regcompass.config import load_models
        from regcompass.contracts import RunRecord

        if run_a or run_b:
            if not (run_a and run_b):
                raise HTTPException(
                    400,
                    "a Comparison is two Runs: name run_a AND run_b, or name"
                    " economy and pillar and let the server pair them",
                )
            record_a, record_b = _one_record(run_a), _one_record(run_b)
            note = None
        elif economy and pillar is not None:
            economy = economy.upper()
            if economy not in economies():
                raise HTTPException(
                    400,
                    f"unknown economy '{economy}': expected one of"
                    f" {', '.join(economies())}",
                )
            if pillar not in valid_pillars():
                raise HTTPException(
                    400,
                    f"unknown pillar {pillar}: expected one of"
                    f" {list(valid_pillars())}",
                )
            record_a, record_b, note = _auto_pair(economy, pillar)
        else:
            raise HTTPException(
                400, "name run_a and run_b, or economy and pillar"
            )

        indicators = _indicator_map(record_a, record_b, pillar)

        from regcompass.storage import Storage

        if not Path(db_path).is_file():
            raise HTTPException(404, f"no database at {db_path}: start a Run first")
        storage = Storage(db_path)
        try:
            mappings_a = storage.load_mappings(run_id=record_a.run_id)
            mappings_b = storage.load_mappings(run_id=record_b.run_id)
            if pillar is not None:
                # A Comparison asked for one Pillar shows that Pillar, even when
                # both Runs covered more: the other Pillar's Indicators would
                # otherwise be appended as rows nobody asked to see.
                from regcompass.export import pillar_of

                mappings_a = [m for m in mappings_a if pillar_of(m.indicator_id) == pillar]
                mappings_b = [m for m in mappings_b if pillar_of(m.indicator_id) == pillar]
            document_ids = sorted(
                {m.document_id for m in mappings_a} | {m.document_id for m in mappings_b}
            )
            document_meta = storage.document_meta(document_ids)
            titles = {
                doc_id: (meta.get("title") or doc_id)
                for doc_id, meta in document_meta.items()
            }
            # The same rows carry each Document's Portal address and extractor,
            # which is what lets a Comparison row be followed to its source.
            document_sources = {
                doc_id: {
                    "source_url": meta.get("source_url"),
                    "extractor": meta.get("extractor"),
                }
                for doc_id, meta in document_meta.items()
            }
            gate_cosines = storage.load_gate_scores()
            # Each side carries ITS OWN Run's Review Decisions: the whole point
            # of keying them by (Run, Mapping) is that Engine A's accept says
            # nothing about the Mapping Engine B chose for the same Indicator.
            reviews_a = _review_statuses(storage, record_a.run_id)
            reviews_b = _review_statuses(storage, record_b.run_id)
        finally:
            storage.close()

        models = load_models()
        displays = {
            name: engine.display_name for name, engine in models.engines.items()
        }
        # "provider / model" as the organizers' sheet asks for it: the model id
        # without the routing prefix the provider already names.
        engine_models = {}
        for name, engine in models.engines.items():
            model_id = engine.litellm_model
            prefix = f"{engine.provider}/"
            if model_id.startswith(prefix):
                model_id = model_id[len(prefix):]
            engine_models[name] = f"{engine.provider} / {model_id}"
        # Every record on the Economy, Runs and Discoveries, so each pass can
        # count the documents its own Discovery fetched.
        economy_records = [
            RunRecord(**row)
            for row in _read_runs(db_path, economy=record_a.economy, limit=MAX_LIMIT)
        ]
        try:
            return compare_runs(
                record_a, record_b, mappings_a, mappings_b, indicators,
                engine_display_names=displays,
                document_titles=titles,
                document_sources=document_sources,
                gate_cosines=gate_cosines,
                reviews_a=reviews_a,
                reviews_b=reviews_b,
                note=note,
                economy_records=economy_records,
                engine_models=engine_models,
            )
        except ComparisonMismatch as e:
            raise HTTPException(400, str(e))

    @app.get("/api/compare")
    def compare(
        run_a: str | None = None,
        run_b: str | None = None,
        economy: str | None = None,
        pillar: int | None = None,
    ):
        """Two Runs on one Economy and Pillar, Indicator by Indicator, with the
        agreement mark between them. Either name both Runs or name the Economy
        and the Pillar and let the server pair the newest Run per Engine."""
        return _build_comparison(run_a, run_b, economy, pillar)

    @app.get("/api/compare/download")
    def compare_download(
        run_a: str | None = None,
        run_b: str | None = None,
        economy: str | None = None,
        pillar: int | None = None,
        format: str = "csv",
    ) -> Response:
        """The same Comparison as a file: the live test hands one in. CSV by
        default (a steward opens it beside the evidence workbook); format=json
        is the exact API payload, for the machine-readable trail; format=xlsx
        is the organizers' Engine Comparison sheet filled in, and
        format=sheet_csv is that sheet's content as a CSV."""
        from regcompass.compare import (
            comparison_csv,
            comparison_filename,
            engine_sheet_csv,
        )

        formats = ("csv", "json", "xlsx", "sheet_csv")
        fmt = (format or "csv").lower()
        if fmt not in formats:
            raise HTTPException(
                400, f"unknown format '{format}': expected one of {', '.join(formats)}"
            )
        comparison = _build_comparison(run_a, run_b, economy, pillar)
        if fmt == "xlsx":
            from regcompass.workbook import template_path, write_engine_comparison

            buffer = io.BytesIO()
            write_engine_comparison(template_path(), buffer, comparison)
            body, media = buffer.getvalue(), (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
            name = comparison_filename(comparison, "engine-comparison.xlsx")
        elif fmt == "sheet_csv":
            body, media = engine_sheet_csv(comparison), "text/csv; charset=utf-8"
            name = comparison_filename(comparison, "engine-comparison.csv")
        elif fmt == "json":
            name = comparison_filename(comparison, fmt)
            body, media = comparison.model_dump_json(indent=2), "application/json"
        else:
            name = comparison_filename(comparison, fmt)
            body, media = comparison_csv(comparison), "text/csv; charset=utf-8"
        return Response(
            content=body,
            media_type=media,
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    # -- the audit view ------------------------------------------------------

    @app.get("/api/documents", response_model=list[DocumentSummary])
    def documents(
        run_id: str | None = None, economy: str | None = None
    ) -> list[DocumentSummary]:
        with audit_source(run_id, economy) as source:
            if source is None:
                return []
            return source.document_summaries(reviews_of(source))

    @app.get("/api/documents/{document_id}/records", response_model=list[RecordSummary])
    def records(
        document_id: str, run_id: str | None = None, economy: str | None = None
    ) -> list[RecordSummary]:
        with audit_source(run_id, economy) as source:
            if source is None or not source.has_document(document_id):
                raise HTTPException(404, f"unknown document {document_id}")
            return source.record_summaries(document_id, reviews_of(source))

    @app.get("/api/documents/{document_id}/pdf")
    def pdf(
        document_id: str, run_id: str | None = None, economy: str | None = None
    ) -> FileResponse:
        with audit_source(run_id, economy) as source:
            path = None if source is None else source.pdf_path(document_id)
            if path is None:
                raise HTTPException(404, f"no PDF for {document_id}")
            # The stored bytes are not always a PDF (a Portal that publishes
            # HTML, a reviewer's own upload), and the row's "Open source (local
            # copy)" link opens THIS path in a browser tab. Serving everything
            # as application/pdf renders those as a broken document.
            media_type = source.media_type(document_id)
            # A Portal's stored page served from this app's own address would
            # otherwise run its scripts with this app's login. Sandboxed, it
            # still reads as the page; the PDF viewer is left alone.
            headers = {"X-Content-Type-Options": "nosniff"}
            if media_type != "application/pdf":
                headers["Content-Security-Policy"] = "sandbox"
            return FileResponse(path, media_type=media_type, headers=headers)

    @app.get("/api/records", response_model=ReviewQueue)
    def review_queue(
        run_id: str | None = None,
        economy: str | None = None,
        sort: str = "confidence",
        unreviewed: bool = False,
        corrected: bool = False,
    ) -> ReviewQueue:
        """The Review queue: every Mapping of one Run in one list, lowest
        Confidence first, with the counts the screen states above it.

        A sibling of the per-Document read rather than a widening of it. That
        one answers "what did this Document give me" and returns a bare list of
        one Document's rows; the queue spans the Run and has to carry counts,
        so bending it would have changed its shape for every caller."""
        with audit_source(run_id, economy) as source:
            if source is None:
                return ReviewQueue(
                    run_id=None, threshold=REVIEW_CONFIDENCE_THRESHOLD,
                    total=0, below_threshold=0, unreviewed=0, records=[],
                )
            try:
                return build_review_queue(
                    source,
                    reviews_of(source),
                    sort=sort,
                    unreviewed_only=unreviewed,
                    corrected_only=corrected,
                )
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc

    @app.get("/api/records/{mapping_id}", response_model=RecordDetail)
    def record(
        mapping_id: str, run_id: str | None = None, economy: str | None = None
    ) -> RecordDetail:
        with audit_source(run_id, economy) as source:
            detail = (
                None if source is None
                else source.record_detail(mapping_id, reviews_of(source))
            )
            if detail is None:
                raise HTTPException(404, f"unknown record {mapping_id}")
            return detail

    @app.get("/api/pieces/{piece_id}", response_model=PieceDetail)
    def piece(piece_id: str) -> PieceDetail:
        """One Piece as a Candidate panel shows it: its text is the Document's
        stored text at the Piece's offsets (Pieces keep offsets, never text of
        their own), with its page and its Document. The Gate's scores are not
        here: they live in the Run's recorded events, not in the database."""
        if bundle is not None:
            raise HTTPException(404, "a frozen bundle serves Mappings, not sections")
        from regcompass.storage import Storage

        if not Path(db_path).is_file():
            raise HTTPException(404, f"unknown section {piece_id}")
        storage = Storage(db_path)
        try:
            row = storage.chunk_row(piece_id)
            if row is None:
                raise HTTPException(404, f"unknown section {piece_id}")
            document_id = row["document_id"]
            full_text = storage.document_full_text(document_id)
            meta = storage.document_meta([document_id]).get(document_id) or {}
        finally:
            storage.close()
        start, end = int(row["char_start"]), int(row["char_end"])
        return PieceDetail(
            piece_id=piece_id,
            document_id=document_id,
            document_title=meta.get("title"),
            section=row["section_label"],
            page=row["page_start"],
            page_end=row["page_end"],
            char_start=start,
            char_end=end,
            text=full_text[start:end],
            format=format_for_extractor(meta.get("extractor")),
        )

    # -- Review Decisions ----------------------------------------------------

    @app.post("/api/reviews", response_model=Review)
    def add_review(req: ReviewRequest, run_id: str | None = None) -> Review:
        """Record the decision that stands for this Mapping of this Run. A
        second decision on the same Mapping REPLACES the first and carries the
        later time: what the reviewer thinks now is what the export obeys.
        Every decision also goes into the Mapping's history.

        A correction names the Indicator the Mapping belongs under (one of the
        Run's Pillars, never the Mapping's own) and a reason; anything else is
        refused with a 422 that says what to fix. The Mapping is not touched."""
        with review_lane(req.run_id or run_id) as source:
            record = source.mapping(req.mapping_id)
            if record is None:
                raise HTTPException(404, f"unknown record {req.mapping_id}")
            comment = req.comment
            corrected_to = None
            if req.review_status == "corrected" or req.corrected_indicator_id:
                problem = correction_problem(
                    status=req.review_status,
                    corrected_indicator_id=req.corrected_indicator_id,
                    comment=req.comment,
                    own_indicator_id=record.indicator_id,
                    choices=source.correction_choices_for(record),
                )
                if problem is not None:
                    raise HTTPException(422, problem)
                comment = (req.comment or "").strip()
                corrected_to = req.corrected_indicator_id
            return source.storage.review_set(
                run_id=_review_run_id(source, req.mapping_id),
                mapping_id=req.mapping_id,
                review_status=req.review_status,
                reviewer=req.reviewer,
                comment=comment,
                corrected_indicator_id=corrected_to,
            )

    @app.get("/api/reviews/history", response_model=ReviewHistory)
    def review_history(
        mapping_id: str, run_id: str | None = None, economy: str | None = None
    ) -> ReviewHistory:
        """Every decision ever written for one Mapping of one Run, oldest
        first, so a decision that was later changed is still on record. The
        frozen bundle takes no decisions, so its history is always empty."""
        if bundle is not None:
            if not bundle.has_record(mapping_id):
                raise HTTPException(404, f"unknown record {mapping_id}")
            return ReviewHistory(run_id=None, mapping_id=mapping_id, history=[])
        with audit_source(run_id, economy) as source:
            if source is None or not source.has_record(mapping_id):
                raise HTTPException(404, f"unknown record {mapping_id}")
            resolved = _review_run_id(source, mapping_id)
            rows = source.storage.review_history(resolved, mapping_id)
            return ReviewHistory(
                run_id=resolved,
                mapping_id=mapping_id,
                history=[ReviewHistoryEntry(**r) for r in rows],
            )

    @app.get("/api/reviews")
    def list_reviews(run_id: str | None = None, economy: str | None = None) -> dict:
        """Every Review Decision of one Run, one per Mapping."""
        if bundle is not None:
            return {"run_id": None, "reviews": []}
        with audit_source(run_id, economy) as source:
            if source is None:
                return {"run_id": run_id, "reviews": []}
            resolved = _review_run_id(source)
            reviews = source.storage.reviews_for_run(resolved)
            return {
                "run_id": resolved,
                "reviews": [reviews[k].model_dump(mode="json") for k in sorted(reviews)],
            }

    @app.post("/api/reviews/accept-all")
    def accept_all(req: AcceptAllRequest, run_id: str | None = None) -> dict:
        """Accept every verified Mapping of this Run that carries no decision
        yet, so an operator against the clock is never left with an empty file.
        Decisions already made are untouched: a rejection stays a rejection."""
        with review_lane(req.run_id or run_id) as source:
            resolved = _review_run_id(source)
            pending = source.storage.unreviewed_mapping_ids(resolved)
            for mapping_id in pending:
                source.storage.review_set(
                    run_id=resolved,
                    mapping_id=mapping_id,
                    review_status="accepted",
                    comment="accepted in bulk from the export preview",
                )
            return {
                "run_id": resolved,
                "n_newly_accepted": len(pending),
                **source.storage.review_counts(resolved),
            }

    # -- Glosses -------------------------------------------------------------

    @app.get("/api/glosses")
    def list_glosses(run_id: str | None = None, economy: str | None = None) -> dict:
        """Every Gloss of one Run, one per Mapping. A frozen bundle carries
        none: its Run finished before the Gloss lane existed for it."""
        if bundle is not None:
            return {"run_id": None, "glosses": []}
        with audit_source(run_id, economy) as source:
            if source is None:
                return {"run_id": run_id, "glosses": []}
            resolved = _review_run_id(source)
            glosses = source.storage.glosses_for_run(resolved)
            return {
                "run_id": resolved,
                "glosses": [glosses[k].model_dump(mode="json") for k in sorted(glosses)],
            }

    @app.post("/api/glosses/review", response_model=GlossRecord)
    def review_gloss(req: GlossReviewRequest, run_id: str | None = None) -> GlossRecord:
        """Approve a Gloss, with the text the reviewer approved and the name
        they approved it under. That name is what lets the Evidence Export ship
        the rendering without its AI label, so a blank one is refused here
        rather than quietly recorded as an anonymous approval."""
        with review_lane(req.run_id or run_id) as source:
            if not source.has_record(req.mapping_id):
                raise HTTPException(404, f"unknown record {req.mapping_id}")
            try:
                return source.storage.gloss_review(
                    run_id=_review_run_id(source, req.mapping_id),
                    mapping_id=req.mapping_id,
                    english=req.english,
                    reviewed_by=req.reviewed_by,
                )
            except ValueError as e:
                raise HTTPException(400, str(e))

    @app.get("/api/export/preview", response_model=ExportPreview)
    def export_preview(
        run_id: str | None = None, economy: str | None = None
    ) -> ExportPreview:
        """What the Evidence Export would contain right now, counted off the
        database and written nowhere. The reviewer sees the excluded Mappings
        before the file exists, not after."""
        with audit_source(run_id, economy) as source:
            if bundle is not None or source is None:
                n = 0 if source is None else len(source.all_records())
                return ExportPreview(
                    run_id=None, gated=False, n_verified=n, n_accepted=0,
                    n_rejected=0, n_flagged=0, n_unreviewed=n,
                )
            resolved = _review_run_id(source)
            counts = source.storage.review_counts(resolved)
            reviews = source.storage.reviews_for_run(resolved)
            return ExportPreview(
                run_id=resolved,
                gated=True,
                accepted_mapping_ids=sorted(
                    m for m, r in reviews.items() if r.review_status == "accepted"
                ),
                **counts,
            )

    @app.post("/api/export", response_model=ExportSummary)
    def export(run_id: str | None = None, economy: str | None = None) -> ExportSummary:
        """The Evidence Export over the Mappings on screen: the CSV and the
        organizers' filled workbook, written side by side into the output
        directory. On the working database only accepted Mappings enter them,
        so a Run nobody reviewed ships no substantive row. The frozen bundle
        carries no Review Decisions at all, so its export is ungated, exactly
        as it was in Round 1."""
        if run_id is not None and bundle is None:
            named = _read_runs(db_path, run_id=run_id, limit=1)
            if named and named[0].get("kind") == "discovery":
                # A Discovery fetched Documents and mapped none, so there is no
                # evidence to export, and no Engine to blame for that.
                raise HTTPException(
                    409,
                    "That is a Discovery, which collects Documents and maps none,"
                    " so it has nothing to export. Open a Run from Run history"
                    " to export its evidence.",
                )
        with audit_source(run_id, economy) as source:
            if source is None:
                raise HTTPException(404, "no working database yet: start a Run first")
            n_records_total = len(source.all_records())
            counts = {"n_accepted": 0, "n_rejected": 0, "n_flagged": 0,
                      "n_unreviewed": n_records_total}
            reviews: dict[str, str] | None = None
            corrections = None
            if bundle is None:
                resolved = _review_run_id(source)
                counts = source.storage.review_counts(resolved)
                counts.pop("n_verified", None)
                decisions = source.storage.reviews_for_run(resolved)
                reviews = {m: d.review_status for m, d in decisions.items()}
                # A corrected Mapping ships under the reviewer's Indicator, so
                # the export needs the whole decision, not the status alone.
                corrections = {
                    m: d for m, d in decisions.items() if d.review_status == "corrected"
                }
            if bundle is not None:
                try:
                    result = export_all(
                        out_dir,
                        source.all_records(),
                        chunk_text_lookup=source.chunk_text_lookup(),
                        gate_cosine_lookup=source.gate_cosine_lookup(),
                        coverage_stats=bundle.manifest.coverage_stats,
                        # Offline audit: the submission Run's own export does
                        # the live URL checks.
                        liveness_fn=lambda url: True,
                        reviews=reviews,
                    )
                except ExportGateError as e:
                    raise HTTPException(422, detail={"gate_failures": e.failures})
            else:
                from regcompass.pipeline import NoCompletedRunError, export_from_db

                try:
                    result = export_from_db(
                        source.storage, out_dir, run_id=source.run_id, reviews=reviews,
                        corrections=corrections,
                    )
                except ExportGateError as e:
                    raise HTTPException(422, detail={"gate_failures": e.failures})
                except NoCompletedRunError as e:
                    raise HTTPException(409, str(e))
                except RuntimeError as e:
                    raise HTTPException(422, str(e))
        # csv.reader, not a line count: verbatim quotes carry embedded newlines
        # inside quoted fields.
        with result.csv_path.open(encoding="utf-8-sig", newline="") as f:
            n_rows = sum(1 for _ in csv.reader(f)) - 1
        return ExportSummary(
            n_records_total=n_records_total,
            n_rows=n_rows,
            csv_path=str(result.csv_path),
            xlsx_path=str(result.xlsx_path) if result.xlsx_path else None,
            rows_cut=result.rows_cut,
            rows_collapsed=result.rows_collapsed,
            duplicate_collapse=duplicate_collapse_notice(result),
            supplementary_path=str(result.supplementary_path),
            files=_export_files(result, out_dir),
            **counts,
        )

    # -- the read-only database browser and the export directory -------------

    @app.get("/api/db/{table}")
    def db_table(
        table: str,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
        verification_status: str | None = None,
    ) -> dict:
        if table not in WHITELIST_TABLES:
            raise HTTPException(400, f"table '{table}' is not browsable; allowed: {', '.join(WHITELIST_TABLES)}")
        if verification_status is not None:
            if table != "mappings":
                raise HTTPException(400, "verification_status filter only applies to the mappings table")
            if verification_status not in VERIFICATION_STATUSES:
                raise HTTPException(400, f"unknown verification_status '{verification_status}'")
        limit = max(1, min(int(limit), MAX_LIMIT))
        offset = max(0, int(offset))
        return _read_table(db_path, table, limit, offset, verification_status)

    @app.get("/api/outputs")
    def outputs() -> dict:
        if not out_dir.is_dir():
            return {"dir": str(out_dir), "files": []}
        files = []
        for f in sorted(out_dir.iterdir(), key=lambda p: p.name):
            if not f.is_file():
                continue
            stat = f.stat()
            files.append(
                {
                    "name": f.name,
                    "size_bytes": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                }
            )
        return {"dir": str(out_dir), "files": files}

    @app.get("/api/outputs/preview")
    def preview(name: str) -> dict:
        path = _safe_output_path(name)
        suffix = path.suffix.lower()
        if suffix == ".csv":
            with path.open(encoding="utf-8-sig", newline="") as fh:
                reader = csv.reader(fh)
                all_rows = list(reader)
            if not all_rows:
                return {"kind": "csv", "name": name, "header": [], "rows": [], "n_rows_total": 0, "truncated": False}
            header, body = all_rows[0], all_rows[1:]
            shown = body[:CSV_PREVIEW_ROWS]
            return {
                "kind": "csv",
                "name": name,
                "header": header,
                "rows": shown,
                "n_rows_total": len(body),
                "truncated": len(body) > len(shown),
            }
        if suffix == ".json":
            if path.stat().st_size > JSON_PREVIEW_MAX_BYTES:
                return {"kind": "text", "name": name, "text": f"(JSON too large to pretty-print: {path.stat().st_size} bytes; download it instead)"}
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise HTTPException(400, f"not valid JSON: {exc}")
            return {"kind": "json", "name": name, "data": data}
        if suffix == ".xlsx":
            # A workbook is a zip; a text peek shows a screenful of noise.
            return {
                "kind": "text",
                "name": name,
                "text": (
                    "The organizers' filled workbook (Output Data, Indicator"
                    " Reference, Coverage Matrix and the live-hour sheets)."
                    " Download it to open in a spreadsheet; the same rows are"
                    " previewable in submission.csv."
                ),
            }
        # everything else: a bounded text peek
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[:5000]
        except OSError as exc:  # pragma: no cover - fs-dependent
            raise HTTPException(400, f"cannot read file: {exc}")
        return {"kind": "text", "name": name, "text": text}

    @app.get("/api/outputs/download")
    def download(name: str) -> FileResponse:
        path = _safe_output_path(name)
        media = _MEDIA.get(path.suffix.lower(), "application/octet-stream")
        return FileResponse(path, media_type=media, filename=name)

    if ui_dir is not None and Path(ui_dir).is_dir():
        # Every /api path above is matched FIRST; unmatched navigation paths
        # fall back to index.html, missing assets still 404 (fallback="auto").
        app.frontend("/", directory=ui_dir)

    if login is not None:
        app.add_middleware(
            LoginMiddleware, user=login[0], password=login[1], sessions=sessions,
        )
    app.state.login = login is not None
    app.state.sessions = sessions

    return app
