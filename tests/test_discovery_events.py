"""Discovery's typed events: which Portal it reads, what it found, fetched,
added and skipped (with the reason in plain words), and how it ended.

Discovery reports these through one optional hook
(regcompass.discovery_progress); the server turns each call into a named event
on the live stream, beside the text lines, which stay exactly as they were.

No network: every Portal answer here is a recorded one (tests/test_discovery.py),
and the server tests drive the real Discovery through that same recorded fetch.
"""

from __future__ import annotations

import functools
import json
import time

import pytest

pytest.importorskip("httpx", reason="Discovery tests need the `live` extra (httpx)")

from fastapi.testclient import TestClient  # noqa: E402

from regcompass.crawl import FetchResult  # noqa: E402
from regcompass.discovery import discover_economy  # noqa: E402
from regcompass.discovery_progress import (  # noqa: E402
    DISCOVERY_EVENT_NAMES,
    SKIP_REASONS,
    DiscoveryProgress,
)
from regcompass.server import create_app  # noqa: E402
from regcompass.storage import Storage  # noqa: E402

from test_discovery import (  # noqa: E402
    ACT_HTML,
    MY_SEEDS,
    PDPA_URL,
    ROBOTS_PERMISSIVE,
    SG_SEEDS,
    TA_URL,
    NullLimiter,
    recorded_fetch,
)


class Recorder(DiscoveryProgress):
    """Every hook call, as (name, fields), in order."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def portal(self, **fields) -> None:
        self.calls.append(("portal", fields))

    def found(self, url, name) -> None:
        self.calls.append(("found", {"url": url, "name": name}))

    def fetched(self, url, size_bytes, method) -> None:
        self.calls.append(("fetched", {"url": url, "size_bytes": size_bytes, "method": method}))

    def added(self, url, document_id, title, n_pages, ocr_applied) -> None:
        self.calls.append(("added", {
            "url": url, "document_id": document_id, "title": title,
            "n_pages": n_pages, "ocr_applied": ocr_applied,
        }))

    def skipped(self, url, code, reason, title=None) -> None:
        self.calls.append(
            ("skipped", {"url": url, "code": code, "reason": reason, "title": title})
        )

    def finished(self, counts) -> None:
        self.calls.append(("finished", dict(counts)))

    def failed(self, message) -> None:
        self.calls.append(("failed", {"message": message}))

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]

    def of(self, name: str) -> list[dict]:
        return [f for n, f in self.calls if n == name]


@pytest.fixture()
def storage(tmp_path):
    s = Storage(tmp_path / "discovery.db")
    s.apply_schema()
    yield s
    s.close()


def _discover(storage, tmp_path, fetch, hook, **kwargs):
    kwargs.setdefault("seeds", SG_SEEDS)
    kwargs.setdefault("limiter", NullLimiter())
    return discover_economy(
        "SG", storage, data_dir=tmp_path / "data", fetch=fetch, hook=hook, **kwargs
    )


class TestTheHook:
    def test_no_hook_is_the_old_behaviour(self, storage, tmp_path):
        report = discover_economy(
            "SG", storage, data_dir=tmp_path / "data", fetch=recorded_fetch(),
            seeds=SG_SEEDS, limiter=NullLimiter(),
        )
        assert report.fetched == 2

    def test_the_default_hook_does_nothing(self):
        hook = DiscoveryProgress()
        hook.portal(economy="SG", name="x", hosts=[], strategy="s", refresh=False, run_id="r")
        hook.found("u", None)
        hook.fetched("u", 1, "httpx")
        hook.added("u", "d", "t", 1, False)
        hook.skipped("u", "in_corpus", "why")
        hook.finished({})
        hook.failed("m")

    def test_a_first_discovery_finds_fetches_and_adds_each_document(self, storage, tmp_path):
        hook = Recorder()
        report = _discover(storage, tmp_path, recorded_fetch(), hook)

        assert hook.names()[0] == "portal"
        portal = hook.of("portal")[0]
        assert portal["economy"] == "SG"
        assert portal["name"] == "Singapore"
        assert portal["hosts"] and all(isinstance(h, str) for h in portal["hosts"])
        assert portal["strategy"] == report.strategy
        assert portal["run_id"] == report.run_id
        assert portal["refresh"] is False

        assert sorted(f["url"] for f in hook.of("found")) == sorted([TA_URL, PDPA_URL])
        assert sorted(f["url"] for f in hook.of("fetched")) == sorted([TA_URL, PDPA_URL])
        for f in hook.of("fetched"):
            assert f["size_bytes"] == len(ACT_HTML[f["url"]])
            assert f["method"]
        added = hook.of("added")
        assert sorted(a["url"] for a in added) == sorted([TA_URL, PDPA_URL])
        ids = {r["document_id"] for r in storage.corpus_documents("SG")}
        assert {a["document_id"] for a in added} == ids
        for a in added:
            assert a["title"] and a["n_pages"] >= 1 and a["ocr_applied"] is False
        assert hook.of("skipped") == []

        # Each Document is found before it is fetched, and fetched before added.
        order = [(n, f.get("url")) for n, f in hook.calls]
        for url in (TA_URL, PDPA_URL):
            assert order.index(("found", url)) < order.index(("fetched", url))
            assert order.index(("fetched", url)) < order.index(("added", url))

        assert hook.names()[-1] == "finished"
        counts = hook.of("finished")[0]
        assert counts["found"] == 2 and counts["fetched"] == 2
        assert counts["added"] == 2 and counts["skipped"] == 0
        assert counts["run_id"] == report.run_id

    def test_a_second_discovery_skips_what_the_corpus_holds_in_plain_words(
        self, storage, tmp_path
    ):
        _discover(storage, tmp_path, recorded_fetch(), None)
        hook = Recorder()
        fetch = recorded_fetch()
        _discover(storage, tmp_path, fetch, hook)

        assert fetch.calls == []
        assert hook.of("fetched") == [] and hook.of("added") == []
        skipped = hook.of("skipped")
        assert sorted(s["url"] for s in skipped) == sorted([TA_URL, PDPA_URL])
        for s in skipped:
            assert s["code"] == "in_corpus"
            assert s["reason"] == SKIP_REASONS["in_corpus"]
            assert "Corpus" in s["reason"]
        counts = hook.of("finished")[0]
        assert counts["skipped"] == 2 and counts["fetched"] == 0 and counts["found"] == 2

    def test_a_refresh_with_unchanged_bytes_says_nothing_changed(self, storage, tmp_path):
        _discover(storage, tmp_path, recorded_fetch(), None)
        hook = Recorder()
        _discover(storage, tmp_path, recorded_fetch(), hook, refresh=True)

        assert len(hook.of("fetched")) == 2
        assert hook.of("added") == []
        assert {s["code"] for s in hook.of("skipped")} == {"unchanged"}
        assert len(hook.of("skipped")) == 2

    def test_a_dead_url_is_skipped_with_what_the_portal_answered(self, storage, tmp_path):
        hook = Recorder()
        _discover(storage, tmp_path, recorded_fetch({TA_URL: ACT_HTML[TA_URL]}), hook)

        (skip,) = hook.of("skipped")
        assert skip["url"] == PDPA_URL and skip["code"] == "http_error"
        assert "404" in skip["reason"]
        assert [a["url"] for a in hook.of("added")] == [TA_URL]
        assert hook.of("finished")[0]["failed"] == 1

    def test_a_dropped_connection_is_skipped_and_named(self, storage, tmp_path):
        def half_dead(url: str) -> FetchResult:
            if url.endswith("/robots.txt"):
                return FetchResult(url, url, 200, ROBOTS_PERMISSIVE, "text/plain", "httpx")
            if url == PDPA_URL:
                raise ConnectionError("the Portal dropped the connection")
            return FetchResult(url, url, 200, ACT_HTML[url], "text/html", "httpx")

        hook = Recorder()
        _discover(storage, tmp_path, half_dead, hook)
        (skip,) = hook.of("skipped")
        assert skip["code"] == "fetch_error" and "ConnectionError" in skip["reason"]

    def test_a_robots_refusal_is_skipped_and_says_so(self, storage, tmp_path):
        hook = Recorder()
        fetch = recorded_fetch(robots=b"User-agent: *\nDisallow: /Act/PDPA2012\n")
        _discover(storage, tmp_path, fetch, hook)

        (skip,) = hook.of("skipped")
        assert skip["url"] == PDPA_URL and skip["code"] == "robots"
        assert "robots.txt" in skip["reason"]

    def test_two_addresses_with_the_same_bytes_are_kept_once(self, storage, tmp_path):
        hook = Recorder()
        same = {TA_URL: ACT_HTML[TA_URL], PDPA_URL: ACT_HTML[TA_URL]}
        _discover(storage, tmp_path, recorded_fetch(same), hook)

        (skip,) = hook.of("skipped")
        assert skip["code"] == "duplicate"
        assert len(hook.of("added")) == 1

    def test_a_leftover_pending_row_is_found_too(self, storage, tmp_path):
        storage.manifest_add_pending(
            "https://sso.agc.gov.sg/Act/CA2018?ViewType=Pdf", "SG", filename_hint="ca.pdf"
        )
        hook = Recorder()
        _discover(storage, tmp_path, recorded_fetch(), hook)
        found = {f["url"] for f in hook.of("found")}
        assert "https://sso.agc.gov.sg/Act/CA2018?ViewType=Pdf" in found
        assert len(hook.of("found")) == 3

    def test_a_limit_leaves_the_rest_skipped_not_silent(self, storage, tmp_path):
        hook = Recorder()
        _discover(storage, tmp_path, recorded_fetch(), hook, max_documents=1)
        assert len(hook.of("fetched")) == 1
        (skip,) = hook.of("skipped")
        assert skip["code"] == "limit"

    def test_a_discovery_that_dies_reports_failed_and_nothing_after(self, storage, tmp_path):
        def dying_search(url, params=None):
            raise RuntimeError("the Portal search went away")

        hook = Recorder()
        with pytest.raises(RuntimeError):
            discover_economy(
                "MY", storage, data_dir=tmp_path / "data", seeds=MY_SEEDS,
                limiter=NullLimiter(), fetch=recorded_fetch({}), get_json=dying_search,
                hook=hook,
            )
        assert hook.names()[0] == "portal"
        assert hook.names()[-1] == "failed"
        assert "went away" in hook.of("failed")[0]["message"]
        assert "finished" not in hook.names()

    def test_an_address_that_failed_before_is_skipped_with_the_earlier_cause(
        self, storage, tmp_path
    ):
        """A second Discovery does not ask again for an address that failed on
        an earlier one, and says so rather than leaving it unexplained."""
        _discover(storage, tmp_path, recorded_fetch({TA_URL: ACT_HTML[TA_URL]}), None)
        hook = Recorder()
        fetch = recorded_fetch({TA_URL: ACT_HTML[TA_URL]})
        _discover(storage, tmp_path, fetch, hook)

        assert fetch.calls == []
        skipped = {s["url"]: s for s in hook.of("skipped")}
        assert skipped[PDPA_URL]["code"] == "failed_before"
        assert skipped[PDPA_URL]["reason"] == (
            "Failed on an earlier Discovery (HTTP 404); not asked again."
        )
        assert skipped[TA_URL]["code"] == "in_corpus"
        counts = hook.of("finished")[0]
        assert counts["found"] == 2
        assert counts["found"] == counts["added"] + counts["skipped"]

    def test_a_robots_refusal_before_is_named_as_such(self, storage, tmp_path):
        robots = b"User-agent: *\nDisallow: /Act/PDPA2012\n"
        _discover(storage, tmp_path, recorded_fetch(robots=robots), None)
        hook = Recorder()
        _discover(storage, tmp_path, recorded_fetch(robots=robots), hook)
        skipped = {s["url"]: s for s in hook.of("skipped")}
        assert skipped[PDPA_URL]["code"] == "failed_before"
        assert "robots.txt" in skipped[PDPA_URL]["reason"]

    def test_a_document_already_in_the_corpus_carries_its_corpus_title(
        self, storage, tmp_path
    ):
        _discover(storage, tmp_path, recorded_fetch(), None)
        titles = {r["source_url"]: r["title"] for r in storage.corpus_documents("SG")}
        hook = Recorder()
        _discover(storage, tmp_path, recorded_fetch(), hook)
        for s in hook.of("skipped"):
            assert s["title"] == titles[s["url"]] and s["title"]

    @pytest.mark.parametrize(
        "first, second",
        [
            ({"bodies": {TA_URL: ACT_HTML[TA_URL]}}, {}),
            ({"robots": b"User-agent: *\nDisallow: /Act/PDPA2012\n"}, {}),
            ({"bodies": {TA_URL: ACT_HTML[TA_URL], PDPA_URL: ACT_HTML[TA_URL]}}, {}),
            ({}, {"refresh": True}),
            (None, {"max_documents": 1}),
        ],
    )
    def test_every_listed_document_ends_added_or_skipped_with_a_reason(
        self, storage, tmp_path, first, second
    ):
        if first is not None:
            _discover(storage, tmp_path, recorded_fetch(**first), None)
        hook = Recorder()
        fetch_args = first or {}
        _discover(storage, tmp_path, recorded_fetch(**fetch_args), hook, **second)

        final: dict[str, str] = {}
        for name, f in hook.calls:
            if name in ("added", "skipped"):
                assert f["url"] not in final, f"{f['url']} settled twice"
                final[f["url"]] = name
                if name == "skipped":
                    assert f["reason"] and f["reason"].endswith(".")
        found = {f["url"] for f in hook.of("found")}
        assert found <= set(final), f"left without a state: {found - set(final)}"
        counts = hook.of("finished")[0]
        assert counts["found"] == len(set(final))
        assert counts["found"] == counts["added"] + counts["skipped"]

    def test_every_skip_reason_is_a_plain_sentence(self):
        for code, reason in SKIP_REASONS.items():
            assert reason[0].isupper() and reason.endswith(".")
            assert "M10" not in reason and "manifest" not in reason.lower()


# ---------------------------------------------------------------------------
# The server seam: the real Discovery, the recorded Portal, the live stream.
# ---------------------------------------------------------------------------


def _wait_done(client, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = client.get("/api/status").json()
        if not st["active"] and st["status"] in ("done", "error"):
            return st
        time.sleep(0.02)
    raise AssertionError("the job did not finish in time")


def _read_stream(client):
    text: list[str] = []
    typed: list[dict] = []
    name = None
    with client.stream("GET", "/api/events") as resp:
        for line in resp.iter_lines():
            if line.startswith("event: "):
                name = line[len("event: "):]
            elif line.startswith("data: "):
                obj = json.loads(line[len("data: "):])
                if name is None:
                    text.append(obj["msg"])
                elif name in DISCOVERY_EVENT_NAMES:
                    assert obj["type"] == name
                    typed.append(obj)
            elif line == "":
                name = None
    return text, typed


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    import regcompass.discovery as discovery_mod

    db = tmp_path / "web.db"
    out = tmp_path / "out"
    out.mkdir()

    def offline(fetch):
        """The real Discovery, over recorded Portal answers."""
        real = discovery_mod.discover_economy
        monkeypatch.setattr(
            discovery_mod, "discover_economy",
            functools.partial(real, fetch=fetch, seeds=SG_SEEDS, limiter=NullLimiter()),
        )

    app = create_app(db_path=db, out_dir=out, data_dir=tmp_path / "data", ui_dir=None)
    return TestClient(app), offline


class TestTheServerStream:
    def test_a_discovery_streams_its_events_beside_its_lines(self, app_client):
        client, offline = app_client
        fetch = recorded_fetch({TA_URL: ACT_HTML[TA_URL]})
        offline(fetch)
        assert client.post("/api/discover", json={"economy": "SG"}).status_code == 200
        assert _wait_done(client)["status"] == "done"
        text, events = _read_stream(client)

        assert [e["seq"] for e in events] == list(range(len(events)))
        assert all(e["type"] in DISCOVERY_EVENT_NAMES and e["ts"] for e in events)
        assert all("msg" not in e for e in events), "old readers take `msg` as a line"
        types = [e["type"] for e in events]
        assert types[0] == "discovery_portal" and types[-1] == "discovery_finished"
        assert events[0]["economy"] == "SG" and events[0]["name"] == "Singapore"
        assert types.count("discovery_found") == 2
        assert [e["url"] for e in events if e["type"] == "discovery_fetched"] == [TA_URL]
        assert [e["url"] for e in events if e["type"] == "discovery_added"] == [TA_URL]
        (skip,) = [e for e in events if e["type"] == "discovery_skipped"]
        assert skip["url"] == PDPA_URL and "404" in skip["reason"]
        assert events[-1]["counts"]["added"] == 1

        # The text lines are the ones Discovery always wrote.
        assert text[0] == "M0 start | discover economy=SG refresh=False"
        assert text[-1].startswith("M10 done | SG: fetched 1,")
        assert not any(line.startswith("{") for line in text)
        assert client.get("/api/run/log").json()["lines"] == text

    def test_the_log_endpoint_hands_the_events_back_for_a_reload(self, app_client):
        client, offline = app_client
        offline(recorded_fetch())
        client.post("/api/discover", json={"economy": "SG"})
        _wait_done(client)
        _, streamed = _read_stream(client)
        logged = client.get("/api/run/log").json()["events"]
        assert logged == streamed

    def test_a_discovery_that_dies_ends_with_one_failed_event(self, app_client, monkeypatch):
        import regcompass.discovery as discovery_mod

        client, _ = app_client

        def boom(url, params=None):
            raise RuntimeError("the Portal search went away")

        real = discovery_mod.discover_economy
        monkeypatch.setattr(
            discovery_mod, "discover_economy",
            functools.partial(
                real, fetch=recorded_fetch({}), seeds=MY_SEEDS,
                limiter=NullLimiter(), get_json=boom,
            ),
        )
        client.post("/api/discover", json={"economy": "MY"})
        assert _wait_done(client)["status"] == "error"
        _, events = _read_stream(client)
        assert events[0]["type"] == "discovery_portal"
        assert [e["type"] for e in events].count("discovery_failed") == 1
        assert events[-1]["type"] == "discovery_failed"
        assert "went away" in events[-1]["message"]

    def test_a_discovery_that_never_called_the_hook_still_ends(self, app_client, monkeypatch):
        """A Discovery that reports nothing (an older caller) still closes its
        stream with a finished event built from its report."""
        import regcompass.discovery as discovery_mod
        from regcompass.discovery import DiscoveryReport

        client, _ = app_client
        monkeypatch.setattr(
            discovery_mod, "discover_economy",
            lambda economy, storage, **kw: DiscoveryReport(
                economy=economy, strategy="httpx", run_id="disc_z", fetched=3,
                documents_stored=3,
            ),
        )
        client.post("/api/discover", json={"economy": "SG"})
        _wait_done(client)
        _, events = _read_stream(client)
        assert [e["type"] for e in events] == ["discovery_finished"]
        assert events[0]["counts"]["fetched"] == 3
        assert events[0]["counts"]["added"] == 3
