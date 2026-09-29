"""M10 crawl tests.

Offline fallback lanes, each with a test:
- manifest resume after a killed run: completed URLs not re-fetched, no gaps
- SHA-256 dedupe on identical bytes: one file on disk, duplicate row linked
- a 404/error URL recorded as failed, NOT retried on the next run
- forced ladder escalation: rung 1 blocked -> rung 2 fires
- tenacity transport-retry cap inside one fetch

Live lanes (opt-in + slow + online): one polite smoke fetch per portal proving
the ground-truth acts are reachable. They are OFF unless REGCOMPASS_LIVE=1 is
set, so `pytest -q` never touches a government portal; TestLiveLaneIsOptIn at
the bottom of this file is the gate on that gate. The full corpus run is
scripts/make_golden_m10.py.
"""

from __future__ import annotations

import json
import os
import re
import socket
import time
from datetime import date
from functools import partial
from pathlib import Path

import pytest

from conftest import LIVE_ENV_VAR, LIVE_OPT_IN_REASON, needs_live

# A base-tier install (no `live` extra) must SKIP this module, not error at
# collection: crawl.py itself needs httpx.
httpx = pytest.importorskip("httpx", reason="M10 crawl tests need the `live` extra (httpx)")

from regcompass import crawl  # noqa: E402
from regcompass.contracts import CrawlFamily, CrawlSeedsEconomy
from regcompass.config import CONFIG_DIR, load_crawl_seeds, load_portals
from regcompass.crawl import (
    CrawlReport,
    FetchResult,
    LadderExhaustedError,
    RateLimiter,
    crawl_economy,
    discover_au,
    discover_my,
    discover_sg,
    fetch_httpx,
    fetch_with_ladder,
    parse_crawl_delay,
    published_crawl_delay,
    robots_aware_limiter,
    _safe_name,
)
from regcompass.storage import Storage


def _online(host: str) -> bool:
    try:
        socket.getaddrinfo(host, 443)
        return True
    except OSError:
        return False


needs_network = pytest.mark.skipif(
    not _online("www.legislation.gov.au"), reason="no network / DNS"
)


def make_storage(tmp_path: Path) -> Storage:
    storage = Storage(tmp_path / "crawl.db")
    storage.apply_schema()
    return storage


def ok(url: str, content: bytes, *, method: str = "httpx", status: int = 200) -> FetchResult:
    return FetchResult(
        url=url, final_url=url, http_status=status, content=content,
        content_type="application/pdf", method=method,
    )


SG_SEEDS = CrawlSeedsEconomy(
    rate_limit_seconds=0.01,
    families={
        "telecommunications": CrawlFamily(acts=["TA1999"]),
        "data_protection": CrawlFamily(acts=["PDPA2012"]),
    },
)


class NullLimiter(RateLimiter):
    def __init__(self):
        super().__init__(0.0, clock=lambda: 0.0, sleep=lambda s: None)


# ---------------------------------------------------------------------------
# Rate limiter (politeness is a scored capability)
# ---------------------------------------------------------------------------


class TestRateLimiter:
    def test_second_hit_on_same_host_waits_the_floor(self):
        clock = iter([0.0, 1.0, 1.0, 11.0]).__next__
        slept: list[float] = []
        rl = RateLimiter(10.0, clock=clock, sleep=slept.append)
        rl.wait("https://sso.agc.gov.sg/Act/TA1999")
        rl.wait("https://sso.agc.gov.sg/Act/PDPA2012")
        assert slept == [pytest.approx(9.0)]

    def test_different_hosts_do_not_wait_on_each_other(self):
        clock = iter([0.0, 0.1, 0.1]).__next__
        slept: list[float] = []
        rl = RateLimiter(10.0, clock=clock, sleep=slept.append)
        rl.wait("https://sso.agc.gov.sg/Act/TA1999")
        rl.wait("https://lom.agc.gov.my/fess-proxy.php")
        assert slept == []


# ---------------------------------------------------------------------------
# robots.txt crawl-delay enforcement: the published
# ask is read at run time through the crawl's own fetch strategy, and
# max(config floor, published) wins - robots can raise politeness, never
# lower it, and a failed robots fetch falls back to the floor alone.
# ---------------------------------------------------------------------------


ROBOTS_SG = b"user-agent: *\ndisallow: /search\ncrawl-delay: 6\n"


class TestRobotsCrawlDelay:
    def test_parse_reads_the_published_delay(self):
        assert parse_crawl_delay(ROBOTS_SG.decode()) == 6.0

    def test_parse_is_case_insensitive_and_ignores_comments(self):
        text = "User-Agent: *\nCRAWL-DELAY: 7  # be nice\nCrawl-delay: 3\n"
        assert parse_crawl_delay(text) == 7.0  # strictest ask wins

    def test_parse_absent_or_malformed_is_none(self):
        assert parse_crawl_delay("user-agent: *\ndisallow: /\n") is None
        assert parse_crawl_delay("crawl-delay: soon\ncrawl-delay:\n") is None

    def test_published_delay_fetched_through_the_strategy(self):
        def fetch(url):
            assert url == "https://sso.agc.gov.sg/robots.txt"
            return ok(url, ROBOTS_SG, method="curl_cffi")

        assert published_crawl_delay("sso.agc.gov.sg", fetch) == 6.0

    def test_blocked_or_dead_robots_never_kills_the_crawl(self):
        def blocked(url):
            return ok(url, b"denied", status=403)

        assert published_crawl_delay("sso.agc.gov.sg", blocked) is None

        def dies(url):
            raise OSError("connection reset")

        assert published_crawl_delay("sso.agc.gov.sg", dies) is None

    def test_a_server_error_on_robots_stops_the_crawl_instead(self):
        """RFC 9309 section 2.3.1.4: a 5xx is "unavailable", not "no rules".
        A crawl that cannot read the rules does not get to choose its own
        spacing instead."""
        from regcompass.crawl import RobotsUnavailableError

        def broken(url):
            return ok(url, b"<h1>503</h1>", status=503)

        with pytest.raises(RobotsUnavailableError):
            published_crawl_delay("sso.agc.gov.sg", broken)

    def test_published_ask_larger_than_the_floor_wins(self, tmp_path):
        storage = make_storage(tmp_path)

        def fetch(url):
            return ok(url, b"crawl-delay: 9\n")

        rl = robots_aware_limiter("SG", SG_SEEDS, fetch, storage)
        assert rl.min_interval == 9.0
        decision = storage.conn.execute(
            "SELECT decision FROM audit_log WHERE method = 'SG:robots'"
        ).fetchone()[0]
        assert "published=9.0" in decision and "interval=9.0s" in decision

    def test_floor_holds_when_robots_asks_less_or_is_unreachable(self, tmp_path):
        storage = make_storage(tmp_path)
        seeds = CrawlSeedsEconomy(
            rate_limit_seconds=5.0, families={"x": CrawlFamily(acts=["TA1999"])}
        )

        def asks_less(url):
            return ok(url, b"crawl-delay: 1\n")

        assert robots_aware_limiter("SG", seeds, asks_less, storage).min_interval == 5.0

        def dies(url):
            raise OSError("no route")

        assert robots_aware_limiter("SG", seeds, dies, storage).min_interval == 5.0


# ---------------------------------------------------------------------------
# fetch_httpx: exact bytes + tenacity transport cap
# ---------------------------------------------------------------------------


class TestFetchHttpx:
    def test_exact_bytes_and_metadata(self):
        payload = b"%PDF-1.7 exact server bytes \x00\xff"
        transport = httpx.MockTransport(
            lambda req: httpx.Response(200, content=payload, headers={"content-type": "application/pdf"})
        )
        fr = fetch_httpx("https://example.gov/doc.pdf", transport=transport)
        assert fr.content == payload
        assert fr.http_status == 200
        assert fr.content_type == "application/pdf"
        assert fr.method == "httpx"

    def test_http_error_is_an_answer_not_an_exception(self):
        transport = httpx.MockTransport(lambda req: httpx.Response(404, content=b"gone"))
        fr = fetch_httpx("https://example.gov/missing", transport=transport)
        assert fr.http_status == 404

    def test_transport_error_retried_then_succeeds(self):
        calls = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 3:
                raise httpx.ConnectError("reset", request=req)
            return httpx.Response(200, content=b"ok")

        fr = fetch_httpx("https://example.gov/x", transport=httpx.MockTransport(handler), wait_max=0)
        assert fr.content == b"ok"
        assert calls["n"] == 3

    def test_transport_error_capped_never_forever(self):
        calls = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise httpx.ConnectError("dead", request=req)

        with pytest.raises(httpx.ConnectError):
            fetch_httpx("https://example.gov/x", transport=httpx.MockTransport(handler), wait_max=0)
        assert calls["n"] == 3  # the tenacity cap, not infinity


# ---------------------------------------------------------------------------
# The SG escalation ladder (forced escalation lane)
# ---------------------------------------------------------------------------


class TestLadder:
    def test_blocked_rung_escalates(self):
        rungs = (
            ("curl_cffi", lambda url: ok(url, b"blocked", method="curl_cffi", status=403)),
            ("playwright", lambda url: ok(url, b"<html>act</html>", method="playwright")),
        )
        fr = fetch_with_ladder("https://sso.agc.gov.sg/Act/TA1999", rungs)
        assert fr.method == "playwright"
        assert fr.content == b"<html>act</html>"

    def test_dead_rung_escalates(self):
        def rung1(url: str) -> FetchResult:
            raise ConnectionError("TLS handshake died")

        rungs = (("curl_cffi", rung1), ("playwright", lambda url: ok(url, b"x", method="playwright")))
        fr = fetch_with_ladder("https://sso.agc.gov.sg/Act/TA1999", rungs)
        assert fr.method == "playwright"

    def test_404_is_final_not_escalated(self):
        calls: list[str] = []

        def rung1(url: str) -> FetchResult:
            calls.append("curl_cffi")
            return ok(url, b"", method="curl_cffi", status=404)

        def rung2(url: str) -> FetchResult:
            calls.append("playwright")
            return ok(url, b"", method="playwright")

        fr = fetch_with_ladder("https://sso.agc.gov.sg/Act/NOPE", (("curl_cffi", rung1), ("playwright", rung2)))
        assert fr.http_status == 404
        assert calls == ["curl_cffi"]  # rung 2 never fired: 404 means the same everywhere

    def test_exhausted_ladder_names_the_next_rung(self):
        rungs = (
            ("curl_cffi", lambda url: ok(url, b"", method="curl_cffi", status=403)),
            ("playwright", lambda url: ok(url, b"", method="playwright", status=403)),
        )
        with pytest.raises(LadderExhaustedError, match="Patchright"):
            fetch_with_ladder("https://sso.agc.gov.sg/Act/TA1999", rungs)


# ---------------------------------------------------------------------------
# Discovery: seeds -> document URLs
# ---------------------------------------------------------------------------


class TestDiscoverSG:
    def test_codes_resolve_to_whole_act_pdf_urls(self):
        # ?ViewType=Pdf is load-bearing: the plain act page lazy-loads its
        # provisions and carries only front matter + TOC for large acts
        targets, misses = discover_sg(SG_SEEDS)
        assert misses == []
        by_url = {t.url: t for t in targets}
        assert "https://sso.agc.gov.sg/Act/TA1999?ViewType=Pdf" in by_url
        assert by_url["https://sso.agc.gov.sg/Act/TA1999?ViewType=Pdf"].source_family == "telecommunications"
        assert (
            by_url["https://sso.agc.gov.sg/Act/PDPA2012?ViewType=Pdf"].filename_hint
            == "sso_agc_gov_sg_Act_PDPA2012.pdf"
        )


AU_SEEDS = CrawlSeedsEconomy(
    rate_limit_seconds=0.01,
    families={"criminal_law": CrawlFamily(queries=["Criminal Code Act 1995"])},
)

# Trimmed from the real API answers recorded 5 Jul 2026 (M10 research probes).
AU_SEARCH_PAYLOAD = {
    "value": [
        {"id": "C2004A04868", "name": "Criminal Code Act 1995", "collection": "Act", "status": "InForce"},
        {
            "id": "F2011L02737",
            "name": "Criminal Code Act 1995 - Declaration - de-listing of a terrorist organisation",
            "collection": "LegislativeInstrument",
            "status": "Repealed",
        },
    ]
}
AU_VERSION_PAYLOAD = {
    "titleId": "C2004A04868",
    "start": "2026-06-30T00:00:00",
    "retrospectiveStart": "2026-06-30T00:00:00",
    "registerId": "C2026C00243",
    "compilationNumber": "174",
    "documents": [
        {"format": "Word", "volumeNumber": 1, "extension": ".docx", "sizeInBytes": 624257},
        {"format": "Pdf", "volumeNumber": 2, "extension": ".pdf", "sizeInBytes": 1715426},
        {"format": "Pdf", "volumeNumber": 1, "extension": ".pdf", "sizeInBytes": 2665631},
        {"format": "Pdf", "volumeNumber": 3, "extension": ".pdf", "sizeInBytes": 1460766},
    ],
}


def au_fake_get_json(url: str, params: dict | None = None) -> tuple[int, object]:
    if url.endswith("/titles"):
        return 200, AU_SEARCH_PAYLOAD
    if "versions/find" in url:
        return 200, AU_VERSION_PAYLOAD
    raise AssertionError(f"unexpected discovery URL: {url}")


class TestDiscoverAU:
    def test_exact_title_resolves_to_ordered_pdf_volumes(self):
        targets, misses = discover_au(AU_SEEDS, get_json=au_fake_get_json)
        assert misses == []
        assert [t.url for t in targets] == [
            f"https://www.legislation.gov.au/C2004A04868/2026-06-30/2026-06-30/text/original/pdf/{v}"
            for v in (1, 2, 3)
        ]
        assert [t.filename_hint for t in targets] == [
            "C2026C00243VOL01.pdf", "C2026C00243VOL02.pdf", "C2026C00243VOL03.pdf"
        ]
        assert all(t.source_family == "criminal_law" for t in targets)

    def test_no_exact_register_match_is_a_recorded_miss(self):
        seeds = CrawlSeedsEconomy(
            rate_limit_seconds=0.01,
            families={"x": CrawlFamily(queries=["Criminal Code"])},  # substring, not exact
        )
        targets, misses = discover_au(seeds, get_json=au_fake_get_json)
        assert targets == []
        assert len(misses) == 1 and "no exact register match" in misses[0]

    def test_odata_single_quotes_are_escaped(self):
        seen: list[dict] = []

        def spy(url: str, params: dict | None = None) -> tuple[int, object]:
            if url.endswith("/titles"):
                seen.append(params)
                return 200, {"value": []}
            raise AssertionError

        seeds = CrawlSeedsEconomy(
            rate_limit_seconds=0.01,
            families={"x": CrawlFamily(queries=["O'Reilly Act 2001"])},
        )
        discover_au(seeds, get_json=spy)
        assert "O''Reilly Act 2001" in seen[0]["$filter"]

    def test_single_volume_act_uses_literal_volume_zero(self):
        # Privacy Act 1988 regression (live 5 Jul 2026): single-volume acts
        # carry volumeNumber 0 and the download URL takes the literal 0;
        # coercing 0 -> 1 produced a 404.
        def single_vol(url: str, params: dict | None = None) -> tuple[int, object]:
            if url.endswith("/titles"):
                return 200, {"value": [{"id": "C2004A03712", "name": "Privacy Act 1988",
                                        "collection": "Act", "status": "InForce"}]}
            return 200, {
                "titleId": "C2004A03712",
                "start": "2026-06-04T00:00:00",
                "retrospectiveStart": "2026-06-04T00:00:00",
                "registerId": "C2026C00227",
                "documents": [{"format": "Pdf", "volumeNumber": 0, "sizeInBytes": 1920725}],
            }

        seeds = CrawlSeedsEconomy(
            rate_limit_seconds=0.01,
            families={"data_protection": CrawlFamily(queries=["Privacy Act 1988"])},
        )
        targets, misses = discover_au(seeds, get_json=single_vol)
        assert misses == []
        assert targets[0].url == (
            "https://www.legislation.gov.au/C2004A03712/2026-06-04/2026-06-04/text/original/pdf/0"
        )
        assert targets[0].filename_hint == "C2026C00227.pdf"

    def test_version_without_pdfs_is_a_miss(self):
        def no_pdf(url: str, params: dict | None = None) -> tuple[int, object]:
            if url.endswith("/titles"):
                return 200, AU_SEARCH_PAYLOAD
            return 200, {**AU_VERSION_PAYLOAD, "documents": [{"format": "Word", "volumeNumber": 1}]}

        targets, misses = discover_au(AU_SEEDS, get_json=no_pdf)
        assert targets == []
        assert "no PDF documents" in misses[0]


MY_SEEDS = CrawlSeedsEconomy(
    rate_limit_seconds=0.01,
    families={"data_protection": CrawlFamily(queries=["personal data protection"])},
)

# Trimmed from the real fess-proxy answer recorded 5 Jul 2026.
MY_DOC = {
    "actNo": "709",
    "titleBI": "PERSONAL DATA PROTECTION ACT 2010",
    "DOC2DOWNLOADBI": (
        '<a href="https://lom.agc.gov.my/ilims/upload/portal/akta/outputaktap/Act 709 ori.pdf"'
        ' target="_blank" class="eve">x</a>'
    ),
    "DOC2DOWNLOADBI_GENERATEPDF": json.dumps(
        [{"icon": "pdf-en-printed.png", "path": "/upload/portal/akta/outputaktap/", "docName": "Act 709 ori.pdf"}]
    ),
    "DOC2DOWNLOADBM_GENERATEPDF": json.dumps(
        [{"icon": "pdf-ms-printed.png", "path": "/upload/portal/akta/outputaktap/", "docName": "Akta 709 ori.pdf"}]
    ),
}


def my_fake_get_json(url: str, params: dict | None = None) -> tuple[int, object]:
    assert url == crawl.MY_FESS
    return 200, {"response": {"numFound": 1, "docs": [MY_DOC]}}


class TestDiscoverMY:
    def test_hit_resolves_to_encoded_english_pdf(self):
        targets, misses = discover_my(MY_SEEDS, get_json=my_fake_get_json)
        assert misses == []
        assert [t.url for t in targets] == [
            "https://lom.agc.gov.my/ilims/upload/portal/akta/outputaktap/Act%20709%20ori.pdf"
        ]
        assert targets[0].filename_hint == "Act 709 ori.pdf"

    def test_bm_fallback_when_no_english_pdf(self):
        doc = {**MY_DOC, "DOC2DOWNLOADBI": "", "DOC2DOWNLOADBI_GENERATEPDF": ""}
        targets, _ = discover_my(
            MY_SEEDS, get_json=lambda u, p=None: (200, {"response": {"docs": [doc]}})
        )
        assert targets[0].url.endswith("Akta%20709%20ori.pdf")

    def test_portal_anchor_beats_the_doubled_generatepdf_path(self):
        # ACT 588 regression (live 5 Jul 2026): the GENERATEPDF path field
        # carries a doubled prefix that 500s; the portal's own anchor href is
        # correct and must win.
        doc = {
            "titleBI": "COMMUNICATIONS AND MULTIMEDIA ACT 1998",
            "DOC2DOWNLOADBI": (
                '<a href="https://lom.agc.gov.my/ilims/upload/portal/akta/outputaktap/600_BI/ACT 588.pdf"'
                ' target="_blank">x</a>'
            ),
            "DOC2DOWNLOADBI_GENERATEPDF": json.dumps(
                [{"icon": "i", "path": "/upload/portal/akta/outputaktap/upload/portal/akta/outputaktap/600_BI/",
                  "docName": "ACT 588.pdf"}]
            ),
        }
        targets, _ = discover_my(
            MY_SEEDS, get_json=lambda u, p=None: (200, {"response": {"docs": [doc]}})
        )
        assert targets[0].url == (
            "https://lom.agc.gov.my/ilims/upload/portal/akta/outputaktap/600_BI/ACT%20588.pdf"
        )
        assert targets[0].filename_hint == "ACT 588.pdf"

    def test_no_hits_is_a_recorded_miss(self):
        targets, misses = discover_my(
            MY_SEEDS, get_json=lambda u, p=None: (200, {"response": {"docs": []}})
        )
        assert targets == []
        assert "no portal hits" in misses[0]

    def test_hits_without_pdf_links_are_a_miss(self):
        doc = {"titleBI": "X", "DOC2DOWNLOADBI_GENERATEPDF": "", "DOC2DOWNLOADBM_GENERATEPDF": ""}
        targets, misses = discover_my(
            MY_SEEDS, get_json=lambda u, p=None: (200, {"response": {"docs": [doc]}})
        )
        assert targets == []
        assert "carry no PDF links" in misses[0]


# ---------------------------------------------------------------------------
# crawl_economy: manifest, resume, dedupe, failure recording
# ---------------------------------------------------------------------------


def counting_fetcher(bodies: dict[str, bytes | int]):
    """bodies[url] = bytes (200) or int (an HTTP status to fail with)."""
    calls: list[str] = []

    def fetch(url: str) -> FetchResult:
        calls.append(url)
        body = bodies[url]
        if isinstance(body, int):
            return ok(url, b"", status=body)
        return ok(url, body)

    return fetch, calls


TA_URL = "https://sso.agc.gov.sg/Act/TA1999?ViewType=Pdf"
PDPA_URL = "https://sso.agc.gov.sg/Act/PDPA2012?ViewType=Pdf"


class TestCrawlEconomy:
    def test_happy_path_exact_bytes_on_disk(self, tmp_path):
        storage = make_storage(tmp_path)
        bodies = {TA_URL: b"<html>TA bytes \x00</html>", PDPA_URL: b"<html>PDPA</html>"}
        fetch, calls = counting_fetcher(bodies)
        report = crawl_economy(
            "SG", storage, tmp_path / "data", SG_SEEDS, fetcher=fetch, limiter=NullLimiter()
        )
        assert report.fetched == 2 and report.failed == 0
        for row in storage.manifest_rows(economy="SG"):
            assert row["status"] == "fetched"
            stored = (tmp_path / "data" / row["local_path"]).read_bytes()
            assert stored == bodies[row["url"]]  # EXACT server bytes
            assert row["sha256"] and row["size_bytes"] == len(stored)
        audit = storage.conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE stage = 'm10_crawl'"
        ).fetchone()[0]
        assert audit == 2

    def test_resume_skips_completed_urls_no_refetch_no_gaps(self, tmp_path):
        storage = make_storage(tmp_path)
        bodies = {TA_URL: b"a", PDPA_URL: b"b"}
        fetch, calls = counting_fetcher(bodies)
        # a killed run: only one document completed
        crawl_economy(
            "SG", storage, tmp_path / "data", SG_SEEDS,
            fetcher=fetch, limiter=NullLimiter(), max_documents=1,
        )
        assert len(calls) == 1
        # resume: the completed URL is not re-fetched, the gap is closed
        report = crawl_economy(
            "SG", storage, tmp_path / "data", SG_SEEDS, fetcher=fetch, limiter=NullLimiter()
        )
        assert len(calls) == 2  # exactly one more call, not three
        assert report.skipped_resume == 1
        statuses = {r["url"]: r["status"] for r in storage.manifest_rows(economy="SG")}
        assert set(statuses.values()) == {"fetched"}  # no gaps

    def test_identical_bytes_deduplicated_one_file(self, tmp_path):
        storage = make_storage(tmp_path)
        same = b"%PDF-1.7 identical statute bytes"
        fetch, _ = counting_fetcher({TA_URL: same, PDPA_URL: same})
        report = crawl_economy(
            "SG", storage, tmp_path / "data", SG_SEEDS, fetcher=fetch, limiter=NullLimiter()
        )
        assert report.fetched == 1 and report.deduplicated == 1
        raw = list((tmp_path / "data" / "SG" / "raw").iterdir())
        assert len(raw) == 1  # one copy on disk
        rows = storage.manifest_rows(economy="SG")
        dupes = [r for r in rows if r["is_duplicate_of"]]
        assert len(dupes) == 1
        assert dupes[0]["local_path"] == raw[0].relative_to(tmp_path / "data").as_posix()

    def test_http_error_recorded_failed_and_never_retried(self, tmp_path):
        storage = make_storage(tmp_path)
        fetch, calls = counting_fetcher({TA_URL: 404, PDPA_URL: b"fine"})
        report = crawl_economy(
            "SG", storage, tmp_path / "data", SG_SEEDS, fetcher=fetch, limiter=NullLimiter()
        )
        assert report.failed == 1 and report.fetched == 1
        row = storage.manifest_get(TA_URL)
        assert row["status"] == "failed" and row["error"] == "HTTP 404" and row["attempts"] == 1
        # re-run: the failed row is recorded history, not a retry queue
        n = len(calls)
        crawl_economy("SG", storage, tmp_path / "data", SG_SEEDS, fetcher=fetch, limiter=NullLimiter())
        assert len(calls) == n
        assert storage.manifest_get(TA_URL)["attempts"] == 1

    def test_dead_transport_recorded_failed(self, tmp_path):
        storage = make_storage(tmp_path)

        def dying(url: str) -> FetchResult:
            raise ConnectionError("portal unreachable")

        report = crawl_economy(
            "SG", storage, tmp_path / "data", SG_SEEDS, fetcher=dying, limiter=NullLimiter()
        )
        assert report.failed == 2
        for row in storage.manifest_rows(economy="SG", status="failed"):
            assert "ConnectionError" in row["error"]

    def test_forced_ladder_escalation_records_the_rung(self, tmp_path):
        storage = make_storage(tmp_path)
        rungs = (
            ("curl_cffi", lambda url: ok(url, b"", method="curl_cffi", status=403)),
            ("playwright", lambda url: ok(url, b"<html>via browser</html>", method="playwright")),
        )
        report = crawl_economy(
            "SG", storage, tmp_path / "data", SG_SEEDS,
            fetcher=lambda url: fetch_with_ladder(url, rungs), limiter=NullLimiter(),
        )
        assert report.fetched >= 1
        for row in storage.manifest_rows(economy="SG", status="fetched"):
            assert row["method"] == "playwright"

    def test_pending_rows_never_demote_fetched_rows(self, tmp_path):
        storage = make_storage(tmp_path)
        fetch, calls = counting_fetcher({TA_URL: b"a", PDPA_URL: b"b"})
        crawl_economy("SG", storage, tmp_path / "data", SG_SEEDS, fetcher=fetch, limiter=NullLimiter())
        assert storage.manifest_add_pending(TA_URL, "SG") is False
        assert storage.manifest_get(TA_URL)["status"] == "fetched"


class TestSafeName:
    def test_spaces_and_specials_sanitized(self):
        assert _safe_name("Act 709 ori.pdf", "u") == "Act_709_ori.pdf"
        assert _safe_name("P.U. (B) 50_2023 (Pelantikan).pdf", "u").endswith(".pdf")

    def test_url_fallback(self):
        assert _safe_name(None, "https://x.gov/path/TA1999.html") == "TA1999.html"


class TestSeedsConfig:
    def test_seeds_cover_every_prepared_economy_and_nothing_stray(self):
        """Seeds are required where Discovery can run: a prepared Economy on a
        non-manual strategy. A live-test Economy we have not crawled yet
        legitimately has none, and must not make check-config red."""
        seeds = load_crawl_seeds()
        portals = load_portals()
        needs = {c for c, p in portals.items() if p.prepared and p.strategy != "manual"}
        assert needs and set(seeds) >= needs
        assert set(seeds) <= set(portals)

    def test_au_politeness_floor_honors_robots(self):
        assert load_crawl_seeds()["AU"].rate_limit_seconds >= 10

    def test_empty_family_rejected(self):
        with pytest.raises(Exception, match="at least one"):
            CrawlFamily()


# ---------------------------------------------------------------------------
# Live smoke lanes: one polite request per portal (exit criteria: the
# ground-truth acts are reachable). Full corpus run = scripts/make_golden_m10.py.
# ---------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.slow
@needs_live
@needs_network
class TestLiveSG:
    def test_ladder_fetches_the_telecommunications_act_pdf(self):
        fr = fetch_with_ladder("https://sso.agc.gov.sg/Act/TA1999?ViewType=Pdf")
        assert fr.http_status == 200
        assert len(fr.content) > 100_000
        assert fr.content.startswith(b"%PDF")  # the whole-act consolidation
        # rung 1 asks as ourselves; "curl_cffi"/"playwright" mean the WAF
        # refused the identified agent and the documented escalation fired
        assert fr.method in ("httpx", "curl_cffi", "playwright")
        time.sleep(5)  # politeness before any subsequent SG test


@pytest.mark.live
@pytest.mark.slow
@needs_live
@needs_network
class TestLiveAU:
    def test_api_discovery_finds_the_criminal_code_volumes(self):
        limiter = RateLimiter(10.0)
        targets, misses = discover_au(AU_SEEDS, limiter=limiter)
        assert misses == []
        assert len(targets) >= 3  # the compilation ships in 3 PDF volumes
        assert all(t.url.startswith("https://www.legislation.gov.au/C2004A04868/") for t in targets)

    def test_volume_download_is_exact_pdf_bytes(self):
        limiter = RateLimiter(10.0)
        targets, _ = discover_au(AU_SEEDS, limiter=limiter)
        limiter.wait(targets[0].url)
        fr = fetch_httpx(targets[0].url)
        assert fr.http_status == 200
        assert fr.content.startswith(b"%PDF")
        assert len(fr.content) > 1_000_000


@pytest.mark.live
@pytest.mark.slow
@needs_live
@needs_network
class TestLiveMY:
    def test_portal_search_finds_the_pdpa_pdf(self):
        limiter = RateLimiter(5.0)
        targets, misses = discover_my(MY_SEEDS, limiter=limiter)
        assert any("709" in (t.filename_hint or "") for t in targets), (targets, misses)
        pdpa = next(t for t in targets if "709" in (t.filename_hint or ""))
        limiter.wait(pdpa.url)
        fr = fetch_httpx(pdpa.url)
        assert fr.http_status == 200
        assert fr.content.startswith(b"%PDF")


# ---------------------------------------------------------------------------
# The gate on the gate: the live lane must stay opt-in. Defined after the live
# classes so it can introspect them; itself fully offline.
# ---------------------------------------------------------------------------


class TestLiveLaneIsOptIn:
    """A DNS probe alone is not a gate: DNS resolves on any online machine, so
    before REGCOMPASS_LIVE existed these three classes fired on a plain
    `pytest -q` and in CI. These checks introspect the marks; they never run
    the lane and never touch the network."""

    LIVE_CLASSES = (TestLiveSG, TestLiveAU, TestLiveMY)

    @staticmethod
    def _skipif_reasons(cls) -> set[str]:
        return {
            m.kwargs.get("reason")
            for m in getattr(cls, "pytestmark", [])
            if m.name == "skipif"
        }

    def test_every_live_class_carries_the_opt_in_and_the_markers(self):
        for cls in self.LIVE_CLASSES:
            names = {m.name for m in getattr(cls, "pytestmark", [])}
            assert {"live", "slow", "skipif"} <= names, (cls.__name__, names)
            reasons = self._skipif_reasons(cls)
            assert LIVE_OPT_IN_REASON in reasons, (cls.__name__, reasons)
            # the DNS probe survives alongside it, so an opted-in but offline
            # machine still skips instead of erroring
            assert "no network / DNS" in reasons, (cls.__name__, reasons)

    def test_the_opt_in_condition_tracks_the_env_var(self):
        # the marker condition is evaluated once at import time, so assert on
        # it directly rather than re-importing conftest under a patched env
        opted_in = bool(os.environ.get(LIVE_ENV_VAR))
        for cls in self.LIVE_CLASSES:
            gate = next(
                m
                for m in cls.pytestmark
                if m.name == "skipif" and m.kwargs.get("reason") == LIVE_OPT_IN_REASON
            )
            assert gate.args[0] is not opted_in, cls.__name__

    def test_the_real_lane_is_skipped_when_the_env_var_is_unset(self, pytester, monkeypatch):
        """End to end against THIS file, in a child pytest session with
        REGCOMPASS_LIVE cleared: the three real live classes must collect and
        report as skipped for the opt-in reason, never run. A synthetic stand-in
        would pass even if the real classes lost their gate, so the child session
        targets the real file. TestLiveLaneIsOptIn is deselected: it is this
        class, and selecting it would make the child spawn its own child."""
        monkeypatch.delenv(LIVE_ENV_VAR, raising=False)
        result = pytester.runpytest_subprocess(
            str(Path(__file__).resolve()),
            "-k",
            "TestLive and not OptIn",
            "-q",
            "-rs",
            "-p",
            "no:cacheprovider",
        )
        result.assert_outcomes(skipped=4, passed=0, failed=0, errors=0)
        # -rs prints one line per skip: the opt-in, not the DNS probe, is what
        # stopped every one of the four
        reasons = [line for line in result.outlines if LIVE_OPT_IN_REASON in line]
        assert len(reasons) == 4, result.outlines

    def test_no_ungated_live_class_can_sneak_in(self):
        """A new live class must join the gated tuple above, not slip past it."""
        live_named = {
            name
            for name, obj in globals().items()
            if isinstance(obj, type)
            and name.startswith("TestLive")
            and name != "TestLiveLaneIsOptIn"
        }
        assert live_named == {c.__name__ for c in self.LIVE_CLASSES}


# ---------------------------------------------------------------------------
# The identified user agent and robots.txt Disallow. Politeness is a scored
# capability: we say who we are, we read what the Portal asks, and the
# browser-impersonating rungs are an ESCALATION after a refusal, never the
# opening move.
# ---------------------------------------------------------------------------


ROBOTS_WITH_DISALLOW = b"""
User-agent: *
Disallow: /search
Disallow: /private/
Crawl-delay: 6

User-agent: RegCompass
Disallow: /admin
Allow: /admin/public
"""


class TestIdentifiedUserAgent:
    def test_the_agent_names_us_and_carries_a_contact_url(self):
        from regcompass import __version__
        from regcompass.crawl import IDENTIFIED_USER_AGENT, USER_AGENT_TOKEN

        assert IDENTIFIED_USER_AGENT.startswith(f"{USER_AGENT_TOKEN}/{__version__}")
        assert "https://github.com/Ryannurtanio/regcompass" in IDENTIFIED_USER_AGENT
        # never a browser lie
        assert "Mozilla" not in IDENTIFIED_USER_AGENT

    def test_fetch_httpx_sends_the_identified_agent(self):
        from regcompass.crawl import IDENTIFIED_USER_AGENT

        seen: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req.headers["user-agent"])
            return httpx.Response(200, content=b"ok")

        fetch_httpx("https://example.gov/x", transport=httpx.MockTransport(handler))
        assert seen == [IDENTIFIED_USER_AGENT]

    def test_json_discovery_sends_the_identified_agent_too(self):
        from regcompass.crawl import IDENTIFIED_USER_AGENT, _default_get_json

        seen: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req.headers["user-agent"])
            return httpx.Response(200, json={"ok": True})

        from regcompass.crawl import one_connection

        with one_connection(transport=httpx.MockTransport(handler)) as client:
            _default_get_json("https://example.gov/api", None, client=client)
        assert seen == [IDENTIFIED_USER_AGENT]


class TestRobotsDisallow:
    def test_our_own_group_wins_over_the_star_group(self):
        from regcompass.crawl import parse_robots

        policy = parse_robots(ROBOTS_WITH_DISALLOW.decode())
        assert policy.disallow == ("/admin",)
        assert policy.allow == ("/admin/public",)

    def test_the_star_group_binds_when_we_are_not_named(self):
        from regcompass.crawl import parse_robots

        policy = parse_robots("User-agent: *\nDisallow: /search\nDisallow: /private/\n")
        assert policy.disallow == ("/search", "/private/")

    def test_a_disallowed_path_is_refused_and_a_sibling_is_allowed(self):
        from regcompass.crawl import parse_robots

        policy = parse_robots("User-agent: *\nDisallow: /search\n")
        assert policy.allows("https://portal.gov/Act/TA1999") is True
        assert policy.allows("https://portal.gov/search?q=data") is False

    def test_the_longest_matching_rule_wins(self):
        from regcompass.crawl import parse_robots

        policy = parse_robots("User-agent: *\nDisallow: /docs\nAllow: /docs/public\n")
        assert policy.allows("https://portal.gov/docs/private.pdf") is False
        assert policy.allows("https://portal.gov/docs/public/a.pdf") is True

    def test_disallow_everything_and_the_empty_disallow_escape_hatch(self):
        from regcompass.crawl import parse_robots

        assert parse_robots("User-agent: *\nDisallow: /\n").allows("https://p.gov/x") is False
        # an empty Disallow value means "nothing is disallowed" (RFC 9309)
        assert parse_robots("User-agent: *\nDisallow:\n").allows("https://p.gov/x") is True

    def test_no_robots_file_means_no_published_ask_not_a_block(self):
        from regcompass.crawl import fetch_robots_policy

        def dies(url):
            raise OSError("HTTP 500 is what this Portal serves")

        policy = fetch_robots_policy("lom.agc.gov.my", dies)
        assert policy.disallow == () and policy.crawl_delay is None
        assert policy.allows("https://lom.agc.gov.my/ilims/x.pdf") is True

    def test_the_policy_carries_the_published_crawl_delay(self):
        from regcompass.crawl import fetch_robots_policy

        def fetch(url):
            assert url == "https://sso.agc.gov.sg/robots.txt"
            return ok(url, ROBOTS_WITH_DISALLOW, method="curl_cffi")

        assert fetch_robots_policy("sso.agc.gov.sg", fetch).crawl_delay == 6.0


class TestLadderOpensIdentified:
    def test_the_first_rung_is_the_identified_agent_not_impersonation(self):
        from regcompass.crawl import SG_LADDER, fetch_httpx as real_fetch_httpx

        assert SG_LADDER[0][1] is real_fetch_httpx
        assert [name for name, _ in SG_LADDER] == ["httpx", "curl_cffi", "playwright"]

    def test_impersonation_fires_only_after_the_identified_agent_is_refused(self):
        from regcompass.crawl import IMPERSONATING_METHODS

        tried: list[str] = []

        def identified(url):
            tried.append("httpx")
            return ok(url, b"<html>act</html>", method="httpx")

        def impersonating(url):  # pragma: no cover - must never be reached here
            tried.append("curl_cffi")
            return ok(url, b"", method="curl_cffi")

        rungs = (("httpx", identified), ("curl_cffi", impersonating))
        fr = fetch_with_ladder("https://sso.agc.gov.sg/Act/TA1999", rungs)
        assert fr.method == "httpx" and tried == ["httpx"]
        assert fr.method not in IMPERSONATING_METHODS

        tried.clear()

        def refused(url):
            tried.append("httpx")
            return ok(url, b"", method="httpx", status=403)

        fr = fetch_with_ladder(
            "https://sso.agc.gov.sg/Act/TA1999", (("httpx", refused), ("curl_cffi", impersonating))
        )
        assert tried == ["httpx", "curl_cffi"]
        assert fr.method in IMPERSONATING_METHODS


class TestOneConnection:
    def test_the_pool_is_capped_at_one_connection(self, monkeypatch):
        import regcompass.crawl as crawl_mod
        from regcompass.crawl import one_connection

        captured: dict = {}
        real_client = httpx.Client

        def spy(**kwargs):
            captured.update(kwargs)
            return real_client(**kwargs)

        monkeypatch.setattr(crawl_mod.httpx, "Client", spy)
        with one_connection(transport=httpx.MockTransport(lambda r: httpx.Response(200))):
            pass
        assert captured["limits"].max_connections == 1
        assert captured["limits"].max_keepalive_connections == 1

    def test_the_session_fetch_sends_every_request_down_that_one_client(self):
        from regcompass.crawl import one_connection, session_fetch, strategy_for_economy

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"ok")

        with one_connection(transport=httpx.MockTransport(handler)) as client:
            fetch = session_fetch(strategy_for_economy("MY"), client)
            assert fetch.func is crawl.fetch_httpx
            assert fetch.keywords == {"client": client}
            for url in ("https://lom.agc.gov.my/a.pdf", "https://lom.agc.gov.my/b.pdf"):
                assert fetch(url).content == b"ok"

    def test_the_singapore_ladder_binds_only_its_identified_rung_to_the_client(self):
        from regcompass.crawl import (
            fetch_curl_cffi,
            fetch_httpx,
            one_connection,
            session_fetch,
            strategy_for_economy,
        )

        with one_connection(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as client:
            bound = session_fetch(strategy_for_economy("SG"), client)
            rungs = bound.keywords["rungs"]
        assert [name for name, _ in rungs] == ["httpx", "curl_cffi", "playwright"]
        assert rungs[0][1].func is fetch_httpx and rungs[0][1].keywords == {"client": client}
        assert rungs[1][1] is fetch_curl_cffi  # impersonation owns its own transport


# ---------------------------------------------------------------------------
# The Malaysian Portal's current answer, recorded. The search returns zero hits
# today because the Portal now wraps its payload in an encrypted envelope: the
# Solr `response.docs` shape the adapter parses is no longer on the wire. The
# adapter must SAY that rather than report a misleading empty result set.
# ---------------------------------------------------------------------------


MY_RECORDED = (
    Path(__file__).resolve().parent
    / "fixtures/portals/my/fess_search_personal_data_protection_2026-09-16.json"
)


class TestMalaysianPortalToday:
    def test_the_recorded_answer_is_an_encrypted_envelope(self):
        """Recorded live on 16 Sep 2026, one request, identified user agent."""
        body = json.loads(MY_RECORDED.read_text(encoding="utf-8"))
        assert body["encrypted"] is True
        assert isinstance(body["data"], str) and body["data"]
        assert "response" not in body, "the Solr envelope is gone from the wire"

    def test_the_parser_names_the_encrypted_envelope_instead_of_reporting_no_hits(self):
        body = json.loads(MY_RECORDED.read_text(encoding="utf-8"))
        targets, misses = discover_my(MY_SEEDS, get_json=lambda u, p=None: (200, body))
        assert targets == []
        assert len(misses) == 1
        miss = misses[0]
        assert "encrypted" in miss.lower(), miss
        assert "no portal hits" not in miss, "silence about the real cause is the bug"
        assert "personal data protection" in miss

    def test_a_plain_solr_answer_still_parses(self):
        """The repair is additive: the Portal's older shape must keep working,
        because the recorded corpus was fetched through it."""
        targets, misses = discover_my(MY_SEEDS, get_json=my_fake_get_json)
        assert misses == []
        assert targets[0].filename_hint == "Act 709 ori.pdf"


# ---------------------------------------------------------------------------
# robots.txt Disallow binds the FETCH LOOP, not just the target list. A pending
# manifest row can outlive the call that created it (a max_documents bound, a
# crashed Discovery, a Document added by hand), so the refusal has to be checked
# where the request is actually about to be made.
# ---------------------------------------------------------------------------


class TestDisallowBindsEveryPendingRow:
    def test_a_leftover_pending_row_is_never_fetched_when_robots_forbids_it(
        self, tmp_path
    ):
        from regcompass.crawl import ROBOTS_DISALLOWED_ERROR, parse_robots

        storage = make_storage(tmp_path)
        # a pending row from an earlier call, for a URL robots now forbids
        storage.manifest_add_pending(PDPA_URL, "SG", filename_hint="pdpa.pdf")
        fetch, calls = counting_fetcher({TA_URL: b"a", PDPA_URL: b"b"})

        report = crawl_economy(
            "SG", storage, tmp_path / "data", SG_SEEDS,
            fetcher=fetch, limiter=NullLimiter(),
            robots=parse_robots("User-agent: *\nDisallow: /Act/PDPA2012\n"),
        )
        assert calls == [TA_URL], "a disallowed URL must never be requested"
        assert report.disallowed == 1
        assert report.fetched == 1
        assert report.failed == 0, "a refusal we chose to honour is not a failure"

        row = storage.manifest_get(PDPA_URL)
        assert row["status"] != "fetched"
        assert ROBOTS_DISALLOWED_ERROR in row["error"]
        assert any("robots.txt" in m and "PDPA2012" in m for m in report.misses)

    def test_it_is_counted_once_even_when_it_is_also_a_fresh_target(self, tmp_path):
        from regcompass.crawl import parse_robots

        storage = make_storage(tmp_path)
        storage.manifest_add_pending(PDPA_URL, "SG", filename_hint="pdpa.pdf")
        fetch, calls = counting_fetcher({TA_URL: b"a", PDPA_URL: b"b"})
        report = crawl_economy(
            "SG", storage, tmp_path / "data", SG_SEEDS,
            fetcher=fetch, limiter=NullLimiter(),
            robots=parse_robots("User-agent: *\nDisallow: /Act/PDPA2012\n"),
        )
        assert report.disallowed == 1
        assert len([m for m in report.misses if "PDPA2012" in m]) == 1

    def test_a_refused_row_is_not_retried_on_the_next_call(self, tmp_path):
        from regcompass.crawl import parse_robots

        storage = make_storage(tmp_path)
        fetch, calls = counting_fetcher({TA_URL: b"a", PDPA_URL: b"b"})
        robots = parse_robots("User-agent: *\nDisallow: /Act/PDPA2012\n")
        crawl_economy(
            "SG", storage, tmp_path / "data", SG_SEEDS,
            fetcher=fetch, limiter=NullLimiter(), robots=robots,
        )
        n = len(calls)
        report = crawl_economy(
            "SG", storage, tmp_path / "data", SG_SEEDS,
            fetcher=fetch, limiter=NullLimiter(), robots=robots,
        )
        assert len(calls) == n, "nothing re-requested"
        assert report.disallowed == 0, "a settled refusal is history, not a new count"

    def test_no_policy_means_no_gate(self, tmp_path):
        """A crawl with no robots policy behaves exactly as it always has."""
        storage = make_storage(tmp_path)
        fetch, calls = counting_fetcher({TA_URL: b"a", PDPA_URL: b"b"})
        report = crawl_economy(
            "SG", storage, tmp_path / "data", SG_SEEDS,
            fetcher=fetch, limiter=NullLimiter(),
        )
        assert report.fetched == 2 and report.disallowed == 0
        assert sorted(calls) == sorted([TA_URL, PDPA_URL])


# ---------------------------------------------------------------------------
# The remaining live-test Portals, as they answered on 16 Sep 2026.
#
# India Code is the one Portal of the five that offers a polite Discovery lane,
# and it does so from a NEW HOST: www.indiacode.nic.in now serves a migration
# notice pointing at indiacode.gov.in, which runs DSpace 7 and publishes the
# read-only REST API this adapter reads. Every byte below is a recorded live
# answer under the identified user agent (tests/fixtures/portals/in/README.md).
# ---------------------------------------------------------------------------


IN_FIXTURES = Path(__file__).resolve().parent / "fixtures/portals/in"
IN_SEARCH_RECORDED = IN_FIXTURES / "search_dpdp_2026-09-16.json"
IN_DPDP_PDF = (
    "https://indiacode.gov.in/server/api/core/bitstreams"
    "/52f9ecbb-b927-4ba6-ae6f-ca2fac50d4df/content"
)

IN_SEEDS = CrawlSeedsEconomy(
    rate_limit_seconds=0.01,
    families={"data_protection": CrawlFamily(
        queries=["The Digital Personal Data Protection Act, 2023"]
    )},
)


def in_recorded_get_json(url: str, params: dict | None = None) -> tuple[int, object]:
    """The recorded India Code answer, with the request the adapter must make
    asserted on the way past."""
    from regcompass.crawl import IN_SEARCH

    assert url == IN_SEARCH
    assert params is not None
    assert params["dsoType"] == "item"
    assert params["embed"] == "bundles/bitstreams", "one request per seed, not three"
    return 200, json.loads(IN_SEARCH_RECORDED.read_text(encoding="utf-8"))


class TestDiscoverIN:
    def test_the_exact_title_resolves_to_the_official_pdf(self):
        from regcompass.crawl import discover_in

        targets, misses = discover_in(IN_SEEDS, get_json=in_recorded_get_json)
        assert misses == []
        assert [t.url for t in targets] == [IN_DPDP_PDF]
        assert targets[0].filename_hint == "a2023-22.pdf"
        assert targets[0].economy == "IN"
        assert targets[0].source_family == "data_protection"

    def test_the_source_url_host_is_on_the_portal_whitelist(self):
        from urllib.parse import urlsplit

        from regcompass.crawl import discover_in

        hosts = load_portals()["IN"].hosts
        targets, _ = discover_in(IN_SEEDS, get_json=in_recorded_get_json)
        assert urlsplit(targets[0].url).netloc in hosts

    def test_section_items_of_the_same_act_are_not_documents(self):
        """India Code indexes every SECTION as its own item ("Application of
        Act.", "Right to nominate."). Only the whole-act item carries the
        official PDF, and only an exact title match may become a Document."""
        from regcompass.crawl import discover_in

        targets, _ = discover_in(IN_SEEDS, get_json=in_recorded_get_json)
        assert len(targets) == 1, [t.url for t in targets]

    def test_a_near_miss_title_is_recorded_not_guessed(self):
        from regcompass.crawl import discover_in

        seeds = CrawlSeedsEconomy(
            rate_limit_seconds=0.01,
            families={"data_protection": CrawlFamily(
                queries=["Digital Personal Data Protection Rules"]
            )},
        )
        targets, misses = discover_in(
            seeds,
            get_json=lambda u, p=None: (
                200, json.loads(IN_SEARCH_RECORDED.read_text(encoding="utf-8"))
            ),
        )
        assert targets == []
        assert misses and "no exact India Code title match" in misses[0]
        assert "Digital Personal Data Protection Rules" in misses[0]

    def test_the_trailing_period_india_code_prints_is_not_a_mismatch(self):
        """The Portal's own title is "The Digital Personal Data Protection
        Act, 2023." with a full stop; a seed written without it is the same
        act, and nothing else in the answer becomes eligible."""
        from regcompass.crawl import discover_in

        with_stop = CrawlSeedsEconomy(
            rate_limit_seconds=0.01,
            families={"data_protection": CrawlFamily(
                queries=["The Digital Personal Data Protection Act, 2023."]
            )},
        )
        targets, misses = discover_in(with_stop, get_json=in_recorded_get_json)
        assert misses == [] and [t.url for t in targets] == [IN_DPDP_PDF]

    def test_an_item_without_an_original_pdf_is_a_recorded_miss(self):
        from regcompass.crawl import discover_in

        body = json.loads(IN_SEARCH_RECORDED.read_text(encoding="utf-8"))
        objects = body["_embedded"]["searchResult"]["_embedded"]["objects"]
        objects[0]["_embedded"]["indexableObject"]["_embedded"]["bundles"][
            "_embedded"
        ]["bundles"] = []
        targets, misses = discover_in(IN_SEEDS, get_json=lambda u, p=None: (200, body))
        assert targets == []
        assert misses and "carries no PDF" in misses[0]

    def test_a_dead_search_is_a_recorded_miss(self):
        from regcompass.crawl import discover_in

        targets, misses = discover_in(
            IN_SEEDS, get_json=lambda u, p=None: (503, {})
        )
        assert targets == []
        assert misses and "HTTP 503" in misses[0]

    def test_the_seed_query_is_spaced_by_the_limiter(self):
        from regcompass.crawl import discover_in

        waited: list[str] = []

        class Watching(NullLimiter):
            def wait(self, url: str) -> None:
                waited.append(url)

        discover_in(IN_SEEDS, get_json=in_recorded_get_json, limiter=Watching())
        assert waited, "a Portal search is a request and waits its turn"

    def test_the_strategy_registry_points_india_at_this_adapter(self):
        from regcompass.crawl import STRATEGIES, discover_in

        portal = load_portals()["IN"]
        assert portal.strategy == "dspace_rest"
        assert STRATEGIES[portal.strategy].discover is discover_in
        assert STRATEGIES[portal.strategy].fetch is fetch_httpx

    def test_india_is_prepared_because_its_corpus_was_fetched(self):
        # `prepared` is a fact about the CORPUS, not about the adapter: the
        # operator's supervised live Discovery of 23 Sep 2026 fetched and
        # checked India's Corpus (4 Documents from indiacode.gov.in at the 2 s
        # floor, under the configured unavailable-robots policy).
        assert load_portals()["IN"].prepared is True


class TestIndiaCodeMovedHost:
    def test_the_old_host_answers_with_a_migration_notice(self):
        """Recorded 16 Sep 2026 through the documented escalation rung, after
        the identified agent was refused 403 by the old host's Akamai edge."""
        body = (IN_FIXTURES / "migration_notice_2026-09-16.html").read_text(
            encoding="utf-8"
        )
        assert "indiacode.gov.in" in body
        assert "migrated" in body.lower()

    @pytest.mark.parametrize(
        "status,fixture",
        [(500, "robots_500_2026-09-16.html"), (502, "robots_502_retry_2026-09-16.html")],
    )
    def test_the_new_hosts_robots_is_unavailable_so_nothing_is_assumed(
        self, status, fixture
    ):
        """indiacode.gov.in answered /robots.txt with HTTP 500 on 16 Sep 2026
        and, on a retry the same day, HTTP 502. Neither says "there are no
        rules": they say the Portal cannot show them. RFC 9309 asks a crawler
        to assume complete disallow while that lasts, so we stop rather than
        read the silence as a yes."""
        from regcompass.crawl import RobotsUnavailableError, fetch_robots_policy

        body = (IN_FIXTURES / fixture).read_bytes()

        def fetch(url: str) -> FetchResult:
            assert url == "https://indiacode.gov.in/robots.txt"
            return FetchResult(url, url, status, body, "text/html", "httpx")

        with pytest.raises(RobotsUnavailableError) as exc:
            fetch_robots_policy("indiacode.gov.in", fetch)
        assert exc.value.status == status
        assert str(status) in str(exc.value)
        assert "complete disallow" in str(exc.value)

    def test_a_404_on_robots_means_no_rules_and_the_floor_stands(self):
        """The other half of the same rule: a 4xx IS "no rules published", and
        no published ask is not permission to hurry."""
        from regcompass.crawl import fetch_robots_policy

        def fetch(url: str) -> FetchResult:
            return FetchResult(url, url, 404, b"not found", "text/html", "httpx")

        policy = fetch_robots_policy("indiacode.gov.in", fetch)
        assert policy.disallow == () and policy.crawl_delay is None
        assert policy.allows("https://indiacode.gov.in/server/api/core/bitstreams/x/content")
        assert load_portals()["IN"].min_interval_seconds >= 2.0

    def test_the_portal_documents_its_own_read_interface(self):
        """The recorded API root is the Portal's own statement of what it
        serves: a HAL index naming `discover`, `bitstreams`, `bundles` and the
        rest. It is why this adapter reads an API instead of scraping pages."""
        root = json.loads((IN_FIXTURES / "api_root_2026-09-16.json").read_text())
        links = root.get("_links", {})
        for name in ("discover", "bitstreams", "bundles", "items"):
            assert name in links, name

    def test_the_discovered_url_answers_with_a_pdf(self):
        """The first 64 KB of the official Act PDF, recorded from the very URL
        discover_in emits. Only the head was taken: a whole statute is not
        something a fixture capture needs to pull."""
        head = (IN_FIXTURES / "dpdp_original_head_2026-09-16.pdf").read_bytes()
        assert head.startswith(b"%PDF-")
        assert len(head) == 65536


# ---------------------------------------------------------------------------
# The four Portals that ARE reachable but publish no polite route to their
# texts, read from their own recorded bytes. These tests are the evidence
# behind `strategy: manual` in config/portals.yaml, so that the decision can be
# re-checked rather than taken on trust.
# ---------------------------------------------------------------------------


PORTAL_FIXTURES = Path(__file__).resolve().parent / "fixtures/portals"


class TestPublishedRulesOfTheLiveTestPortals:
    def test_viet_nam_forbids_the_only_route_to_its_document_list(self):
        from regcompass.crawl import parse_robots

        policy = parse_robots(
            (PORTAL_FIXTURES / "vn/robots_2026-09-16.txt").read_text(encoding="utf-8")
        )
        assert "/api/" in policy.disallow and "/Pages/" in policy.disallow
        assert not policy.allows("https://vbpl.vn/api/van-ban?page=1")
        assert not policy.allows("https://vbpl.vn/Pages/home.aspx")
        assert policy.allows("https://vbpl.vn/van-ban/trung-uong")

    def test_the_viet_nam_sitemap_names_four_static_pages_and_no_law(self):
        """The sitemap its robots.txt points at is titled "Trang tinh" (static
        pages): the whole index is one sub-sitemap of four navigation URLs, so
        sitemap-driven Discovery has nothing to find here."""
        index = (PORTAL_FIXTURES / "vn/sitemap_2026-09-16.xml").read_text(encoding="utf-8")
        pages = (PORTAL_FIXTURES / "vn/sitemap_0_2026-09-16.xml").read_text(encoding="utf-8")
        assert index.count("<sitemap>") == 1
        assert "https://vbpl.vn/sitemap/0.xml" in index
        locs = re.findall(r"<loc>([^<]+)</loc>", pages)
        assert locs == [
            "https://vbpl.vn/",
            "https://vbpl.vn/gioi-thieu",
            "https://vbpl.vn/van-ban/trung-uong",
            "https://vbpl.vn/van-ban/dia-phuong",
        ]

    def test_kazakhstan_forbids_search_and_api_and_allows_act_pages(self):
        from regcompass.crawl import parse_robots

        policy = parse_robots(
            (PORTAL_FIXTURES / "kz/robots_2026-09-16.txt").read_text(encoding="utf-8")
        )
        assert not policy.allows("https://adilet.zan.kz/search?q=personal+data")
        assert not policy.allows("https://adilet.zan.kz/advanced-search")
        assert not policy.allows("https://adilet.zan.kz/api/internal-export")
        assert policy.allows("https://adilet.zan.kz/rus/docs/Z1300000094")

    def test_we_read_the_group_that_binds_us_not_the_ai_training_group(self):
        """adilet.zan.kz closes the whole site to model-training crawlers by
        name (GPTBot, CCBot and others) while leaving act pages open
        to everyone else. RegCompass is not named, so the `*` group binds it.
        Reading the wrong group here would either block a Discovery that is
        allowed or, far worse, ignore a rule that applies."""
        from regcompass.crawl import parse_robots

        text = (PORTAL_FIXTURES / "kz/robots_2026-09-16.txt").read_text(encoding="utf-8")
        assert parse_robots(text).allows("https://adilet.zan.kz/rus/docs/Z1300000094")
        assert not parse_robots(text, agent_token="GPTBot").allows(
            "https://adilet.zan.kz/rus/docs/Z1300000094"
        )
        assert not parse_robots(text, agent_token="CCBot").allows(
            "https://adilet.zan.kz/rus/docs/Z1300000094"
        )

    def test_the_kazakh_act_page_withholds_the_text_from_non_browser_clients(self):
        """The act route answers 2.7 KB of shell whose own comment says the
        full text is withheld on purpose, so that the system is cited rather
        than drained. A headless browser could render around that; honouring it
        is the reason Kazakhstan is operator-supplied."""
        shell = (PORTAL_FIXTURES / "kz/act_Z1300000094_2026-09-16.html").read_text(
            encoding="utf-8"
        )
        assert "<!--SEO_BODY-->" in shell
        assert "Полного текста здесь нет намеренно" in shell
        assert len(shell) < 4000

    def test_mongolia_publishes_no_rules_and_serves_no_result_list(self):
        """legalinfo.mn answers /robots.txt with its 404 page, so nothing is
        published. Its search page echoes the query back and then hands the
        results to a client-side Google widget: the server HTML carries the
        category navigation and no document at all. The Portal's own list
        endpoint is an undocumented POST, which is not a published API."""
        from regcompass.crawl import parse_robots

        not_robots = (PORTAL_FIXTURES / "mn/robots_404_2026-09-16.html").read_text(
            encoding="utf-8", errors="replace"
        )
        policy = parse_robots(not_robots)
        assert policy.disallow == () and policy.allow == ()

        results = (PORTAL_FIXTURES / "mn/search_personal_data_2026-09-16.html").read_text(
            encoding="utf-8", errors="replace"
        )
        assert 'value="хувийн мэдээлэл"' in results, "the query reached the server"
        assert "Google хайлт" in results, "and the server delegates the search"
        assert not re.search(r'href="[^"]+\.pdf"', results, re.I)
        assert set(re.findall(r'href="https://legalinfo\.mn/mn/([\w-]+)', results)) <= {
            "about", "advsearch", "book", "community", "compilation",
            "discussion", "dislaw", "ecommunity", "entity", "esearch",
            "knowledge", "latestlaw", "law", "news", "overlaw", "translate",
        }, "every link on a results page is navigation, never a document"


# ---------------------------------------------------------------------------
# The headless rung, exercised against a recorded page served from localhost.
# No Portal is touched: the bytes are the ones committed under fixtures/portals.
# ---------------------------------------------------------------------------


class TestHeadlessRungReadsRawBytes:
    def test_playwright_returns_the_servers_own_bytes(self, tmp_path):
        pytest.importorskip("playwright", reason="the headless rung needs the browser")
        import http.server
        import threading

        from regcompass.crawl import fetch_playwright

        body = b"<html><body><h1>Act</h1><p>3. Recorded, not rendered.</p></body></html>"
        root = tmp_path / "www"
        root.mkdir()
        (root / "act.html").write_bytes(body)

        handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/act.html"
            try:
                fr = fetch_playwright(url, timeout_ms=30_000)
            except Exception as exc:  # no browser binary in this environment
                pytest.skip(f"headless browser unavailable: {type(exc).__name__}: {exc}")
        finally:
            server.shutdown()

        assert fr.http_status == 200
        assert fr.content == body, "response.body() is the server's bytes, not the DOM"
        assert fr.method == "playwright"


class TestTheSingleUrlLaneHonoursTheSameRule:
    def test_an_unavailable_robots_stops_a_named_url_too(self):
        """"Add document" by URL makes a request, so it reads the rules first,
        and a Portal that cannot show them stops that request as surely as it
        stops a crawl: one request is not a licence the rules did not give.
        Singapore is the ordinary Portal here, carrying neither a recorded
        unreachable date nor an operator policy."""
        from regcompass.crawl import RobotsUnavailableError, fetch_one

        asked: list[str] = []

        def fetch(url: str) -> FetchResult:
            asked.append(url)
            if url.endswith("/robots.txt"):
                return FetchResult(url, url, 503, b"busy", "text/html", "httpx")
            return ok(url, b"%PDF-1.4 ...")

        with pytest.raises(RobotsUnavailableError) as exc:
            fetch_one(
                "https://sso.agc.gov.sg/Act/TA1999?ViewType=Pdf",
                "SG", fetch=fetch, limiter=NullLimiter(),
            )
        assert exc.value.status == 503
        assert asked == ["https://sso.agc.gov.sg/robots.txt"], (
            "the Document itself was never requested"
        )

    def test_a_404_on_robots_leaves_the_single_url_lane_open(self, tmp_path):
        from regcompass.crawl import fetch_one

        def fetch(url: str) -> FetchResult:
            if url.endswith("/robots.txt"):
                return FetchResult(url, url, 404, b"", "text/html", "httpx")
            return ok(url, b"%PDF-1.4 ...")

        fr = fetch_one(
            "https://indiacode.gov.in/server/api/core/bitstreams/x/content",
            "IN", fetch=fetch, limiter=NullLimiter(),
        )
        assert fr.http_status == 200 and fr.content.startswith(b"%PDF")


# ---------------------------------------------------------------------------
# RFC 9309 section 2.3.1.4 has two halves, and this is the second one: a
# robots.txt that answers 5xx asks for complete disallow, but a Portal that has
# STAYED that way for 30 days may be treated as publishing no rules. The clock
# starts from a date a person wrote down in config/portals.yaml after checking
# the Portal by hand, so the code never decides on its own that a Portal has
# been broken long enough.
# ---------------------------------------------------------------------------


def robots_answering(status: int, body: bytes = b"<h1>error</h1>"):
    """A fetch whose robots.txt answers `status`; any other URL is a document."""
    asked: list[str] = []

    def fetch(url: str) -> FetchResult:
        asked.append(url)
        if url.endswith("/robots.txt"):
            return FetchResult(url, url, status, body, "text/html", "httpx")
        return ok(url, b"%PDF-1.4 ...")

    fetch.asked = asked
    return fetch


DEFAULT_POLICY_LINE = "    robots_unavailable_policy: proceed"


def config_with_the_default_policy(tmp_path: Path) -> Path:
    """A copy of config/ with India's operator policy line taken out, which is
    every Portal back on the default: an unavailable robots.txt refuses. It is
    the one-line change the configuration comment describes, done to a copy so
    the refusal stays tested after the shipped configuration stopped carrying
    it."""
    import shutil

    config_dir = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, config_dir)
    portals_file = config_dir / "portals.yaml"
    text = portals_file.read_text(encoding="utf-8")
    assert DEFAULT_POLICY_LINE in text, "the India policy line moved; fix this helper"
    portals_file.write_text(
        "\n".join(
            line for line in text.splitlines()
            if not line.startswith(DEFAULT_POLICY_LINE)
        ) + "\n",
        encoding="utf-8",
    )
    return config_dir


class TestTheUnavailableRobotsPolicy:
    """A Portal may carry an operator's decision about what an unavailable
    robots.txt means for IT: refuse (the default, and RFC 9309's own reading) or
    proceed, which treats the 5xx as no rules published for that Portal alone
    and says so on every record. Every other politeness rule is untouched."""

    def test_only_india_carries_proceed_and_every_other_portal_refuses(self):
        portals = load_portals()
        assert portals["IN"].robots_unavailable_policy == "proceed"
        assert [
            code for code, p in portals.items()
            if p.robots_unavailable_policy != "refuse"
        ] == ["IN"]

    def test_proceed_reads_an_unavailable_robots_txt_as_no_rules(self):
        from regcompass.crawl import read_robots_policy

        reading = read_robots_policy(
            "indiacode.gov.in",
            robots_answering(500),
            unreachable_since=date(2026, 9, 16),
            today=date(2026, 9, 17),
            unavailable_policy="proceed",
        )
        assert reading.policy.disallow == () and reading.policy.allow == ()
        assert reading.policy.crawl_delay is None
        assert reading.unavailable_status == 500
        assert reading.unavailable_policy == "proceed"
        assert reading.note == (
            "unavailable (HTTP 500), and this Portal is configured"
            " robots_unavailable_policy: proceed, so it is read as publishing"
            " no rules and the Portal's own spacing floor applies"
        )

    def test_the_same_seam_refuses_on_the_default(self):
        """Same Portal, same day, same answer: without the policy the refusal
        is exactly the one that was there before."""
        from regcompass.crawl import RobotsUnavailableError, read_robots_policy

        with pytest.raises(RobotsUnavailableError) as exc:
            read_robots_policy(
                "indiacode.gov.in",
                robots_answering(500),
                unreachable_since=date(2026, 9, 16),
                today=date(2026, 9, 17),
            )
        assert exc.value.lifts_on == date(2026, 10, 17)

    def test_proceed_never_touches_the_published_rules(self):
        """A Portal that CAN show its rules is bound by them, policy or not:
        proceed says what an unavailable robots.txt means, nothing else."""
        from regcompass.crawl import read_robots_policy

        def serving(url: str) -> FetchResult:
            return FetchResult(
                url, url, 200, b"User-agent: *\nDisallow: /server/api/discover\n",
                "text/plain", "httpx",
            )

        reading = read_robots_policy(
            "indiacode.gov.in", serving, unavailable_policy="proceed",
        )
        assert reading.note is None and reading.unavailable_status is None
        assert not reading.policy.allows(
            "https://indiacode.gov.in/server/api/discover/search/objects"
        )

    def test_a_4xx_is_still_simply_no_rules_under_proceed(self):
        from regcompass.crawl import read_robots_policy

        reading = read_robots_policy(
            "indiacode.gov.in", robots_answering(404), unavailable_policy="proceed",
        )
        assert reading.note is None and reading.unavailable_status is None

    def test_an_unknown_policy_value_is_rejected_when_the_config_loads(
        self, tmp_path
    ):
        from regcompass.config import load_portals as load

        config_dir = config_with_the_default_policy(tmp_path)
        portals_file = config_dir / "portals.yaml"
        portals_file.write_text(
            portals_file.read_text(encoding="utf-8").replace(
                "    strategy: dspace_rest",
                "    strategy: dspace_rest\n    robots_unavailable_policy: ignore",
            ),
            encoding="utf-8",
        )
        with pytest.raises(Exception) as exc:
            load(config_dir)
        message = str(exc.value)
        assert "robots_unavailable_policy" in message
        assert "'ignore'" in message and "refuse" in message and "proceed" in message


class TestTheThirtyDayRule:
    def test_a_portal_broken_for_months_is_read_as_publishing_no_rules(self):
        """Malaysia's robots.txt has answered HTTP 500 since 6 Jul 2026. By
        16 Sep that is 72 days, well past the RFC's 30, so the crawl runs and
        the record says on what basis."""
        from datetime import date

        from regcompass.crawl import read_robots_policy

        reading = read_robots_policy(
            "lom.agc.gov.my",
            robots_answering(500),
            unreachable_since=date(2026, 7, 6),
            today=date(2026, 9, 16),
        )
        assert reading.policy.disallow == ()
        assert reading.policy.allows("https://lom.agc.gov.my/ilims/x.pdf")
        assert reading.note == (
            "unreachable since 2026-07-06, treated as no restrictions per"
            " RFC 9309 after 30 days"
        )

    def test_a_portal_that_broke_today_is_refused_with_the_date_the_rule_lifts(self):
        """India Code answered 500, then 502 on a retry, on 16 Sep 2026. That
        is nothing like 30 days, so Discovery refuses and says when it would
        stop refusing."""
        from datetime import date

        from regcompass.crawl import RobotsUnavailableError, read_robots_policy

        with pytest.raises(RobotsUnavailableError) as exc:
            read_robots_policy(
                "indiacode.gov.in",
                robots_answering(502),
                unreachable_since=date(2026, 9, 16),
                today=date(2026, 9, 16),
            )
        message = str(exc.value)
        assert exc.value.status == 502
        assert exc.value.since == date(2026, 9, 16)
        assert exc.value.lifts_on == date(2026, 10, 17)
        assert "unreachable since 2026-09-16" in message
        assert "2026-10-17" in message and "30 days later" in message

    def test_the_rule_lifts_the_day_after_thirty_days(self):
        from datetime import date

        from regcompass.crawl import RobotsUnavailableError, read_robots_policy

        since = date(2026, 9, 16)
        with pytest.raises(RobotsUnavailableError):
            read_robots_policy(
                "indiacode.gov.in", robots_answering(500),
                unreachable_since=since, today=date(2026, 10, 16),
            )
        reading = read_robots_policy(
            "indiacode.gov.in", robots_answering(500),
            unreachable_since=since, today=date(2026, 10, 17),
        )
        assert reading.note and reading.policy.disallow == ()

    def test_a_portal_with_no_recorded_date_is_refused_and_told_what_to_do(self):
        """Singapore carries no date, which is the ordinary case, and the
        refusal then asks the operator to check the Portal and write one
        down rather than guessing how long it has been broken."""
        from regcompass.crawl import RobotsUnavailableError, read_robots_policy

        assert load_portals()["SG"].robots_unreachable_since is None
        with pytest.raises(RobotsUnavailableError) as exc:
            read_robots_policy("sso.agc.gov.sg", robots_answering(503))
        message = str(exc.value)
        assert "No robots_unreachable_since date is recorded" in message
        assert "config/portals.yaml" in message
        assert "30 days after that date" in message

    def test_a_4xx_is_still_simply_no_rules(self):
        from datetime import date

        from regcompass.crawl import read_robots_policy

        reading = read_robots_policy(
            "indiacode.gov.in", robots_answering(404),
            unreachable_since=date(2026, 9, 16), today=date(2026, 9, 16),
        )
        assert reading.note is None and reading.policy.disallow == ()

    def test_the_published_rules_still_win_when_the_portal_can_serve_them(self):
        from datetime import date

        from regcompass.crawl import read_robots_policy

        def serving(url: str) -> FetchResult:
            return FetchResult(
                url, url, 200, b"User-agent: *\nDisallow: /search\n",
                "text/plain", "httpx",
            )

        reading = read_robots_policy(
            "adilet.zan.kz", serving,
            unreachable_since=date(2020, 1, 1), today=date(2026, 9, 16),
        )
        assert reading.note is None
        assert not reading.policy.allows("https://adilet.zan.kz/search?q=x")


class TestTheSingleUrlLaneFollowsTheSameRule:
    def test_the_add_lane_is_let_past_by_the_same_thirty_day_rule(self, tmp_path):
        """Malaysia's date is old enough, so the operator's URL is fetched and
        the lane hands back the reading for the record."""
        from regcompass.crawl import fetch_one

        readings: list = []
        fetch = robots_answering(500)
        fr = fetch_one(
            "https://lom.agc.gov.my/ilims/upload/portal/akta/Act%20709.pdf",
            "MY", fetch=fetch, limiter=NullLimiter(), robots_reading=readings.append,
        )
        assert fr.http_status == 200
        assert readings and readings[0].note.startswith("unreachable since 2026-07-06")
        assert readings[0].unavailable_policy is None, (
            "the 30-day rule let this past, not an operator policy"
        )
        assert fetch.asked[0].endswith("/robots.txt")

    def test_the_add_lane_is_refused_where_discovery_would_be(self, tmp_path):
        """A Portal on the default policy whose 5xx is recent refuses the add
        lane exactly as it refuses Discovery: one request is not a licence the
        rules did not give."""
        from regcompass.crawl import RobotsUnavailableError, fetch_one

        fetch = robots_answering(502)
        with pytest.raises(RobotsUnavailableError):
            fetch_one(
                "https://indiacode.gov.in/server/api/core/bitstreams/x/content",
                "IN", fetch=fetch, limiter=NullLimiter(),
                config_dir=config_with_the_default_policy(tmp_path),
            )
        assert fetch.asked == ["https://indiacode.gov.in/robots.txt"], (
            "the Document itself was never requested"
        )

    def test_the_add_lane_proceeds_where_the_portal_carries_the_policy(self):
        """India carries the operator policy, so the same answer on the same
        day lets the one request through, and the lane hands back both facts:
        the status robots.txt gave, and the policy that allowed it."""
        from regcompass.crawl import fetch_one

        readings: list = []
        fetch = robots_answering(502)
        fr = fetch_one(
            "https://indiacode.gov.in/server/api/core/bitstreams/x/content",
            "IN", fetch=fetch, limiter=NullLimiter(), robots_reading=readings.append,
        )
        assert fr.http_status == 200
        assert readings[0].unavailable_status == 502
        assert readings[0].unavailable_policy == "proceed"
        assert "proceed" in readings[0].note and "502" in readings[0].note

    def test_the_policy_does_not_lower_the_portals_spacing_floor(self):
        """Proceeding is about the rules we could not read, never about hurry:
        India's configured 2 s floor is what the lane waits out."""
        from regcompass.crawl import fetch_one

        class Spy:
            min_interval = 0.0

            def __init__(self):
                self.waited: list[str] = []

            def wait(self, url: str) -> None:
                self.waited.append(url)

        limiter = Spy()
        fetch_one(
            "https://indiacode.gov.in/server/api/core/bitstreams/x/content",
            "IN", fetch=robots_answering(500), limiter=limiter,
        )
        assert limiter.waited == [
            "https://indiacode.gov.in/server/api/core/bitstreams/x/content"
        ]
        assert load_portals()["IN"].min_interval_seconds == 2.0


class TestTheUploadLaneNeverAsksAboutRobots:
    def test_uploading_a_file_reads_no_robots_txt_at_all(self, tmp_path, monkeypatch):
        """An upload makes no request, so there is no ask to honour and no
        Portal to consult. A Portal whose robots.txt is unreadable must not
        block the one lane that never touches it."""
        import regcompass.crawl as crawl_mod
        from regcompass.corpus import add_document
        from regcompass.storage import Storage

        def explode(*args, **kwargs):
            raise AssertionError("the upload lane must not read robots.txt")

        monkeypatch.setattr(crawl_mod, "read_robots_policy", explode)
        monkeypatch.setattr(crawl_mod, "fetch_robots_policy", explode)

        storage = Storage(tmp_path / "upload.db")
        storage.apply_schema()
        added = add_document(
            storage, tmp_path / "data", "IN",
            b"<html><body><h1>The Digital Personal Data Protection Act, 2023</h1>"
            b"<p>test stand-in, not Portal bytes: written for this test suite.</p>"
            b"<p>16. The Central Government may restrict the transfer of personal"
            b" data outside India by notification.</p></body></html>",
            source_url="https://indiacode.gov.in/handle/123456789/496508",
            filename_hint="dpdp.html",
        )
        assert added.economy == "IN" and added.robots_note is None
        storage.close()
