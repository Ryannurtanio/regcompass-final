"""Discovery: the ONLY step that touches the internet.

Everything here runs offline against recorded Portal answers: a fake fetch that
returns committed bytes for a URL and counts its own calls, so "no request was
made" is a fact the test can assert rather than a claim. Discovery fills an
Economy's Corpus and writes its own Run Record with the count of Documents
fetched; a Run then reads that Corpus and never fetches (tests/test_corpus_run.py).
"""

from __future__ import annotations

from pathlib import Path

import pytest

# A base-tier install (no `live` extra) must SKIP this module, not error at
# collection: crawl.py, which discovery composes, needs httpx.
pytest.importorskip("httpx", reason="Discovery tests need the `live` extra (httpx)")

from regcompass.contracts import CrawlFamily, CrawlSeedsEconomy  # noqa: E402
from regcompass.crawl import IDENTIFIED_USER_AGENT, FetchResult, RateLimiter  # noqa: E402
from regcompass.discovery import (  # noqa: E402
    DiscoveryReport,
    ManualEconomyError,
    discover_economy,
)
from regcompass.storage import Storage  # noqa: E402

from forbidden_portal import FORBIDDEN, FORBIDDEN_NAME, forbidden_config  # noqa: E402

TA_URL = "https://sso.agc.gov.sg/Act/TA1999?ViewType=Pdf"
PDPA_URL = "https://sso.agc.gov.sg/Act/PDPA2012?ViewType=Pdf"
ROBOTS_URL = "https://sso.agc.gov.sg/robots.txt"

SG_SEEDS = CrawlSeedsEconomy(
    rate_limit_seconds=0.01,
    families={
        "telecommunications": CrawlFamily(acts=["TA1999"]),
        "data_protection": CrawlFamily(acts=["PDPA2012"]),
    },
)

MY_SEEDS = CrawlSeedsEconomy(
    rate_limit_seconds=0.01,
    families={"data_protection": CrawlFamily(queries=["personal data protection"])},
)

ACT_HTML = {
    TA_URL: (
        b"<html><body><h1>Telecommunications Act 1999</h1>"
        b"<p>5. A licensee shall protect the confidentiality of subscriber"
        b" information it holds under this Act.</p></body></html>"
    ),
    PDPA_URL: (
        b"<html><body><h1>Personal Data Protection Act 2012</h1>"
        b"<p>26. An organisation shall not transfer personal data outside"
        b" Singapore except in accordance with this Act.</p></body></html>"
    ),
}

ROBOTS_PERMISSIVE = b"User-agent: *\nCrawl-delay: 2\n"


class NullLimiter(RateLimiter):
    def __init__(self):
        super().__init__(0.0, clock=lambda: 0.0, sleep=lambda s: None)


def recorded_fetch(bodies: dict[str, bytes] | None = None, *, robots: bytes = ROBOTS_PERMISSIVE):
    """A fetch over recorded Portal answers. `calls` lists every Document URL
    asked for; robots.txt is tracked separately, because "Discovery made no
    request" is about Documents."""
    bodies = ACT_HTML if bodies is None else bodies
    calls: list[str] = []
    robots_calls: list[str] = []

    def fetch(url: str) -> FetchResult:
        if url.endswith("/robots.txt"):
            robots_calls.append(url)
            return FetchResult(url, url, 200, robots, "text/plain", "httpx")
        calls.append(url)
        body = bodies.get(url)
        if body is None:
            return FetchResult(url, url, 404, b"", "text/html", "httpx")
        return FetchResult(url, url, 200, body, "text/html", "httpx")

    fetch.calls = calls
    fetch.robots_calls = robots_calls
    return fetch


@pytest.fixture()
def storage(tmp_path):
    s = Storage(tmp_path / "discovery.db")
    s.apply_schema()
    yield s
    s.close()


def _discover(storage, tmp_path, fetch, **kwargs):
    kwargs.setdefault("seeds", SG_SEEDS)
    kwargs.setdefault("limiter", NullLimiter())
    return discover_economy(
        "SG", storage, data_dir=tmp_path / "data", fetch=fetch, **kwargs
    )


class TestDiscoveryFillsTheCorpus:
    def test_documents_land_with_language_source_url_and_fetch_time(self, storage, tmp_path):
        fetch = recorded_fetch()
        report = _discover(storage, tmp_path, fetch)

        assert isinstance(report, DiscoveryReport)
        assert report.economy == "SG" and report.strategy == "curl_cffi_ladder"
        assert report.fetched == 2 and report.documents_stored == 2
        assert sorted(fetch.calls) == sorted([TA_URL, PDPA_URL])

        rows = storage.corpus_documents("SG")
        assert len(rows) == 2
        for row in rows:
            assert row["language"] == "English"  # the Portal's default Language
            assert row["source_url"] in ACT_HTML
            assert row["fetched_at"]
            assert row["full_text"]

    def test_the_discovery_record_carries_the_fetch_count(self, storage, tmp_path):
        report = _discover(storage, tmp_path, recorded_fetch())

        record = storage.run_get(report.run_id)
        assert record is not None
        assert record["kind"] == "discovery" and record["economy"] == "SG"
        assert record["status"] == "completed" and record["engine"] is None
        assert record["documents_fetched"] == report.fetched == 2
        assert record["details"] == {
            "strategy": "curl_cffi_ladder", "refresh": False,
            "skipped_existing": 0, "fetched": 2, "stored": 2,
            "escalated_to_impersonation": False,
        }
        assert record["started_at"].endswith("Z") and record["ended_at"].endswith("Z")
        assert storage.runs_list(kind="discovery")[0]["run_id"] == report.run_id

    def test_a_dead_url_is_recorded_as_failed_not_raised(self, storage, tmp_path):
        """One dead Document must not lose the rest of the Corpus."""

        def half_dead(url: str) -> FetchResult:
            if url.endswith("/robots.txt"):
                return FetchResult(url, url, 200, ROBOTS_PERMISSIVE, "text/plain", "httpx")
            if url == PDPA_URL:
                raise ConnectionError("the Portal dropped the connection")
            return FetchResult(url, url, 200, ACT_HTML[url], "text/html", "httpx")

        report = _discover(storage, tmp_path, half_dead)
        assert report.fetched == 1 and report.failed == 1
        assert [r["source_url"] for r in storage.corpus_documents("SG")] == [TA_URL]
        assert storage.run_get(report.run_id)["status"] == "completed"

    def test_a_failed_discovery_is_recorded_not_lost(self, storage, tmp_path):
        """A Portal search that dies takes the whole Discovery with it, and the
        Run Record says so instead of vanishing."""

        def dying_search(url, params=None):
            raise RuntimeError("the Portal search went away")

        with pytest.raises(RuntimeError, match="went away"):
            discover_economy(
                "MY", storage, data_dir=tmp_path / "data", seeds=MY_SEEDS,
                limiter=NullLimiter(), fetch=recorded_fetch({}), get_json=dying_search,
            )
        record = storage.runs_list(kind="discovery")[0]
        assert record["economy"] == "MY" and record["status"] == "failed"
        assert "went away" in record["error"]

    def test_the_record_opens_before_the_first_request(self, storage, tmp_path):
        """A Discovery that dies at once must still have said who it was."""
        seen: list[str] = []

        def watching_search(url, params=None):
            seen.append(storage.runs_list(kind="discovery")[0]["status"])
            raise RuntimeError("dead on the first search")

        with pytest.raises(RuntimeError):
            discover_economy(
                "MY", storage, data_dir=tmp_path / "data", seeds=MY_SEEDS,
                limiter=NullLimiter(), fetch=recorded_fetch({}), get_json=watching_search,
            )
        assert seen[0] == "running"


class TestRefresh:
    def test_a_second_discovery_asks_the_portal_for_nothing(self, storage, tmp_path):
        _discover(storage, tmp_path, recorded_fetch())
        again = recorded_fetch()
        report = _discover(storage, tmp_path, again)

        assert report.fetched == 0
        assert report.skipped_existing == len(storage.corpus_documents("SG")) == 2
        assert again.calls == [], "a Document already in the Corpus is never re-asked"
        assert again.robots_calls == [], "nothing to fetch means not even a robots request"

    def test_refresh_re_fetches_and_replaces(self, storage, tmp_path):
        _discover(storage, tmp_path, recorded_fetch())
        amended = dict(ACT_HTML)
        amended[TA_URL] = (
            b"<html><body><h1>Telecommunications Act 1999</h1>"
            b"<p>5. A licensee shall protect the confidentiality of subscriber"
            b" information, as amended in 2026, under this Act.</p></body></html>"
        )
        again = recorded_fetch(amended)
        report = _discover(storage, tmp_path, again, refresh=True)

        assert report.refresh is True
        assert report.fetched == 2 and report.skipped_existing == 0
        assert sorted(again.calls) == sorted([TA_URL, PDPA_URL])
        texts = {r["source_url"]: r["full_text"] for r in storage.corpus_documents("SG")}
        assert "as amended in 2026" in texts[TA_URL]
        assert len(storage.corpus_documents("SG")) == 2, "replaced, not duplicated"

    def test_refresh_retries_a_document_that_failed(self, storage, tmp_path):
        """Without refresh a failed row is history, never retried. Refresh is
        the deliberate way back: it resets EVERY target, not only the ones that
        made it into the Corpus, so a Portal that was down once is not written
        off forever."""
        dead = dict(ACT_HTML)
        first = recorded_fetch(dead)

        def half_dead(url: str) -> FetchResult:
            result = first(url)
            if url == PDPA_URL:
                return FetchResult(url, url, 503, b"", "text/html", "httpx")
            return result

        report = _discover(storage, tmp_path, half_dead)
        assert report.fetched == 1 and report.failed == 1
        assert storage.manifest_get(PDPA_URL)["status"] == "failed"

        # a second plain Discovery leaves the failed row alone
        plain = recorded_fetch()
        assert _discover(storage, tmp_path, plain).fetched == 0
        assert plain.calls == []

        again = recorded_fetch()
        report = _discover(storage, tmp_path, again, refresh=True)
        assert PDPA_URL in again.calls, "refresh must re-ask for the failed Document"
        assert report.fetched == 2
        assert {r["source_url"] for r in storage.corpus_documents("SG")} == set(ACT_HTML)

    def test_the_refresh_flag_reaches_the_discovery_record(self, storage, tmp_path):
        _discover(storage, tmp_path, recorded_fetch())
        report = _discover(storage, tmp_path, recorded_fetch(), refresh=True)
        assert storage.run_get(report.run_id)["details"]["refresh"] is True


class TestPolitenessIsScored:
    def test_requests_to_one_host_are_spaced_at_or_above_the_floor(self, storage, tmp_path):
        """A fake clock and sleep: the limiter must hold the configured floor
        between two requests to the same host."""
        now = {"t": 0.0}
        slept: list[float] = []

        def clock() -> float:
            return now["t"]

        def sleep(seconds: float) -> None:
            slept.append(seconds)
            now["t"] += seconds

        limiter = RateLimiter(4.0, clock=clock, sleep=sleep)
        report = _discover(storage, tmp_path, recorded_fetch(), limiter=limiter)
        assert report.fetched == 2
        assert slept and all(s >= 4.0 for s in slept)

    def test_the_published_crawl_delay_raises_the_floor_and_never_lowers_it(
        self, storage, tmp_path
    ):
        no_wait = {"limiter": None, "clock": lambda: 0.0, "sleep": lambda s: None}
        report = _discover(
            storage, tmp_path, recorded_fetch(robots=b"User-agent: *\nCrawl-delay: 9\n"),
            **no_wait,
        )
        assert report.min_interval_seconds == 9.0

        storage.conn.execute("DELETE FROM documents")
        storage.conn.execute("DELETE FROM crawl_manifest")
        storage.conn.commit()
        report = _discover(
            storage, tmp_path, recorded_fetch(robots=b"User-agent: *\nCrawl-delay: 0.1\n"),
            **no_wait,
        )
        assert report.min_interval_seconds >= SG_SEEDS.rate_limit_seconds
        assert report.min_interval_seconds > 0.1

    def test_a_disallowed_url_is_skipped_and_counted(self, storage, tmp_path):
        fetch = recorded_fetch(robots=b"User-agent: *\nDisallow: /Act/PDPA2012\n")
        report = _discover(storage, tmp_path, fetch)

        assert report.disallowed == 1
        assert fetch.calls == [TA_URL], "the disallowed URL is never requested"
        assert any("robots.txt" in m and "PDPA2012" in m for m in report.misses)
        assert [r["source_url"] for r in storage.corpus_documents("SG")] == [TA_URL]

    def test_a_leftover_pending_row_is_gated_by_robots_too(self, storage, tmp_path):
        """The Disallow check binds where the request is made, not only where
        the target list is built: a pending row left by an earlier Discovery is
        refused just the same."""
        storage.manifest_add_pending(PDPA_URL, "SG", filename_hint="pdpa.pdf")
        fetch = recorded_fetch(robots=b"User-agent: *\nDisallow: /Act/PDPA2012\n")
        report = _discover(storage, tmp_path, fetch)

        assert PDPA_URL not in fetch.calls
        assert fetch.calls == [TA_URL]
        assert report.disallowed == 1
        assert [r["source_url"] for r in storage.corpus_documents("SG")] == [TA_URL]

    def test_every_request_goes_out_under_the_identified_user_agent(self, storage, tmp_path):
        import httpx

        seen: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req.headers["user-agent"])
            body = ROBOTS_PERMISSIVE if req.url.path == "/robots.txt" else ACT_HTML.get(
                str(req.url), b"<html>x</html>"
            )
            return httpx.Response(200, content=body)

        report = discover_economy(
            "SG", storage, data_dir=tmp_path / "data", seeds=SG_SEEDS,
            limiter=NullLimiter(), transport=httpx.MockTransport(handler),
        )
        assert report.fetched == 2
        assert seen and set(seen) == {IDENTIFIED_USER_AGENT}
        assert report.escalated_to_impersonation is False

    def test_a_discovery_that_impersonated_records_that_it_did(self, storage, tmp_path):
        """The browser-impersonating rung stays, as the documented escalation
        after the identified agent is refused, and a Discovery that reached it
        says so in its report instead of escalating silently. That the ladder
        only escalates AFTER a refusal is proved in tests/test_crawl.py
        (TestLadderOpensIdentified); here the fetch simply reports the rung it
        ended on."""

        def impersonating(url: str) -> FetchResult:
            if url.endswith("/robots.txt"):
                return FetchResult(url, url, 200, ROBOTS_PERMISSIVE, "text/plain", "httpx")
            return FetchResult(url, url, 200, ACT_HTML[url], "text/html", "curl_cffi")

        report = _discover(storage, tmp_path, impersonating)
        assert report.escalated_to_impersonation is True
        record = storage.run_get(report.run_id)
        assert record["status"] == "completed"
        # and it SURVIVES the process: a disclosure that lived only on the
        # in-memory report would be gone by the time anyone read the record.
        assert record["details"]["escalated_to_impersonation"] is True

    def test_a_discovery_that_stayed_identified_says_so(self, storage, tmp_path):
        report = _discover(storage, tmp_path, recorded_fetch())
        assert report.escalated_to_impersonation is False


class TestManualEconomies:
    def test_a_manual_only_economy_is_refused_by_name(self, storage, tmp_path):
        config_dir = forbidden_config(tmp_path)
        with pytest.raises(ManualEconomyError) as exc:
            discover_economy(
                FORBIDDEN, storage, config_dir=config_dir, data_dir=tmp_path / "data"
            )
        message = str(exc.value)
        assert FORBIDDEN_NAME in message and "manual" in message.lower()
        assert "Add document" in message
        assert storage.runs_list(kind="discovery") == [], "no record for work never started"


class TestTheStoredCountIsCorpusRows:
    """"Stored" is Documents the Corpus holds, never files the fetch read.

    The two used to be counted from the same list, so a file that was read and
    then lost on the way into the Corpus was still reported as stored: a live
    Lao Discovery read 37 files, filed 36 Documents and printed "stored 37".
    """

    LOST_URL = "https://sso.agc.gov.sg/Act/LOST2020?ViewType=Pdf"

    def _mark_fetched_without_bytes(self, storage, tmp_path):
        """A fetched manifest row whose file is not on disk. The ingest cannot
        turn it into a Document and records the reason, which is exactly the
        shape of a file that never reaches a Corpus row."""
        (tmp_path / "data" / "SG" / "raw").mkdir(parents=True, exist_ok=True)
        storage.manifest_add_pending(self.LOST_URL, "SG", filename_hint="lost.pdf")
        storage.manifest_mark_fetched(
            self.LOST_URL, http_status=200, method="httpx", sha256="0" * 64,
            content_type="application/pdf", size_bytes=3,
            local_path="SG/raw/lost.pdf",
        )

    def test_a_fetched_file_with_no_row_is_never_counted_as_stored(
        self, storage, tmp_path
    ):
        self._mark_fetched_without_bytes(storage, tmp_path)
        report = _discover(storage, tmp_path, recorded_fetch())

        assert report.fetched == 2
        assert report.documents_stored == 2 == len(storage.corpus_documents("SG"))
        assert report.fetched_without_a_row == 1
        assert any("lost.pdf" in m or "LOST2020" in m for m in report.misses)

    def test_the_record_carries_both_numbers(self, storage, tmp_path):
        self._mark_fetched_without_bytes(storage, tmp_path)
        report = _discover(storage, tmp_path, recorded_fetch())

        details = storage.run_get(report.run_id)["details"]
        assert details["fetched"] == 2 and details["stored"] == 2
        assert details["fetched_without_a_row"] == 1

    def test_an_ordinary_discovery_reports_nothing_missing(self, storage, tmp_path):
        report = _discover(storage, tmp_path, recorded_fetch())

        assert report.fetched_without_a_row == 0
        assert "fetched_without_a_row" not in storage.run_get(report.run_id)["details"]


class TestDiscoveryReportShape:
    def test_the_report_answers_what_the_interface_shows(self, storage, tmp_path):
        report = _discover(storage, tmp_path, recorded_fetch())
        assert report.economy == "SG"
        assert report.strategy == "curl_cffi_ladder"
        assert report.fetched == 2
        assert report.skipped_existing == 0
        assert report.refresh is False
        assert report.run_id.startswith("disc_")
        assert report.failed == 0 and report.disallowed == 0
        assert isinstance(report.misses, list)

    def test_the_run_id_is_stamped_with_the_start_time(self, storage, tmp_path):
        report = _discover(storage, tmp_path, recorded_fetch())
        stamp = report.run_id.split("_")
        assert len(stamp) == 3 and stamp[0] == "disc"
        assert len(stamp[1]) == len("20260916T101500Z") and stamp[1].endswith("Z")
        assert len(stamp[2]) == 6


class TestBytesAreExactAndResumable:
    def test_exact_portal_bytes_land_under_the_data_dir(self, storage, tmp_path):
        _discover(storage, tmp_path, recorded_fetch())
        for row in storage.corpus_documents("SG"):
            local = Path(tmp_path / "data") / row["local_path"]
            assert local.read_bytes() == ACT_HTML[row["source_url"]]


# ---------------------------------------------------------------------------
# The remaining live-test Economies.
#
# India Code is the one Portal of the five with a polite Discovery lane, so it
# is the one that runs here; Viet Nam, Kazakhstan, Mongolia and the Russian
# Federation are operator-supplied, and the test is that Discovery says so and
# the add lane still works. Why each is operator-supplied is recorded with the
# Portal's own recorded bytes in tests/test_crawl.py and docs/PORTALS.md.
# ---------------------------------------------------------------------------

import json  # noqa: E402
from datetime import date, timedelta  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

from regcompass.config import CONFIG_DIR as _CONFIG_DIR  # noqa: E402
from regcompass.contracts import CrawlFamily as _CrawlFamily  # noqa: E402

PORTAL_FIXTURES = _Path(__file__).resolve().parent / "fixtures/portals"
# The day India Code's robots.txt was found answering HTTP 500, and then 502 on
# a retry; it is the date recorded on the Portal in config/portals.yaml, and
# every test of the 30-day rule is measured from it rather than from today.
BROKE_ON = date(2026, 9, 16)

IN_SEEDS = CrawlSeedsEconomy(
    rate_limit_seconds=0.01,
    families={"data_protection": _CrawlFamily(
        queries=["The Digital Personal Data Protection Act, 2023"]
    )},
)
IN_PDF_URL = (
    "https://indiacode.gov.in/server/api/core/bitstreams"
    "/52f9ecbb-b927-4ba6-ae6f-ca2fac50d4df/content"
)
IN_SEARCH_URL = "https://indiacode.gov.in/server/api/discover/search/objects"
# A stand-in for the Act's own PDF. The recorded fixture of the real file is
# its first 64 KB, deliberately: a fixture capture does not pull whole statutes,
# and a truncated PDF cannot be read. So the bytes replayed here are a short
# readable document carrying the same Source URL and the same section numbering,
# enough for a Run to chunk and map. What is RECORDED, and therefore what is
# actually tested against the Portal, is its answer to the search.
IN_SECTIONS = (
    ("8", "A Data Fiduciary shall, irrespective of any agreement to the contrary or the"
          " failure of a Data Principal to carry out her duties under this Act, be"
          " responsible for complying with the provisions of this Act in respect of any"
          " processing undertaken by it or on its behalf by a Data Processor, and shall"
          " implement appropriate technical and organisational measures to ensure"
          " effective observance of the provisions of this Act."),
    ("9", "The Data Fiduciary shall, before processing any personal data of a child or of"
          " a person with disability who has a lawful guardian, obtain verifiable consent"
          " of the parent of such child or of such lawful guardian, and shall not undertake"
          " tracking or behavioural monitoring of children or targeted advertising directed"
          " at children."),
    ("10", "The Central Government may notify any Data Fiduciary or class of Data"
           " Fiduciaries as a Significant Data Fiduciary, on the basis of an assessment of"
           " relevant factors, including the volume and sensitivity of personal data"
           " processed, the risk to the rights of the Data Principal and the security of the"
           " State, and such a Data Fiduciary shall appoint a Data Protection Officer based"
           " in India."),
    ("16", "The Central Government may, by notification, restrict the transfer of personal"
           " data by a Data Fiduciary for processing to such country or territory outside"
           " India as may be so notified. Nothing in this section shall restrict the"
           " applicability of any law in force in India that provides for a higher degree of"
           " protection for, or restriction on, the transfer of personal data by a Data"
           " Fiduciary outside India."),
    ("25", "Any person aggrieved by an order of the Data Protection Board of India may"
           " prefer an appeal before the Appellate Tribunal within a period of sixty days"
           " from the date of receipt of the order, and the Board may inquire into a personal"
           " data breach and impose a monetary penalty in accordance with the Schedule to"
           " this Act."),
    ("33", "The Board may, after giving the person concerned a reasonable opportunity of"
           " being heard, impose a monetary penalty for a breach of the obligation of a Data"
           " Fiduciary to take reasonable security safeguards to prevent a personal data"
           " breach, and shall record the reasons for its decision in writing."),
)
IN_DOCUMENT = (
    "<html><body><h1>The Digital Personal Data Protection Act, 2023</h1>"
    "<p>test stand-in, not Portal bytes: written for this test suite, never"
    " fetched from India Code.</p>"
    + "".join(f"<p>{number}. {text}</p>" for number, text in IN_SECTIONS)
    + "</body></html>"
).encode("utf-8")


def in_recorded_answers(*, robots_status: int = 404, search: dict | None = None):
    """India Code as recorded on 16 Sep 2026: its answer to the search, verbatim.

    robots.txt is the one answer NOT replayed from the recording on the happy
    path. The Portal answered 500, and on a retry 502, and under RFC 9309 a 5xx
    stops a Discovery dead, so the recorded bytes cannot also exercise the lane
    behind them. The default here is therefore a SYNTHETIC HTTP 404 with an
    empty body, which fabricates nothing: a 404 carries no Portal content, and
    "no rules published" is a state this Portal could be in tomorrow. The real
    recorded 5xx answers are tested where they belong, on the policy that
    decides what an unavailable robots.txt means
    (TestIndiaUnderTheUnavailableRobotsPolicy)."""
    if search is None:
        search = json.loads(
            (PORTAL_FIXTURES / "in/search_dpdp_2026-09-16.json").read_text(encoding="utf-8")
        )
    robots_bodies = {
        404: b"",
        500: (PORTAL_FIXTURES / "in/robots_500_2026-09-16.html").read_bytes(),
        502: (PORTAL_FIXTURES / "in/robots_502_retry_2026-09-16.html").read_bytes(),
    }
    calls: list[str] = []
    robots_calls: list[str] = []

    def fetch(url: str) -> FetchResult:
        if url.endswith("/robots.txt"):
            robots_calls.append(url)
            return FetchResult(
                url, url, robots_status, robots_bodies[robots_status],
                "text/html", "httpx",
            )
        calls.append(url)
        if url == IN_PDF_URL:
            return FetchResult(url, url, 200, IN_DOCUMENT, "text/html", "httpx")
        return FetchResult(url, url, 404, b"", "text/html", "httpx")

    def get_json(url: str, params: dict | None = None):
        calls.append(url)
        return 200, search

    fetch.calls = calls
    fetch.robots_calls = robots_calls
    fetch.get_json = get_json
    return fetch


class TestIndiaDiscovery:
    def test_the_document_lands_with_its_language_and_source_url(
        self, storage, tmp_path
    ):
        fetch = in_recorded_answers()
        report = discover_economy(
            "IN", storage, data_dir=tmp_path / "data", seeds=IN_SEEDS,
            limiter=NullLimiter(), fetch=fetch, get_json=fetch.get_json,
        )
        assert report.strategy == "dspace_rest"
        assert report.fetched == 1 and report.documents_stored == 1
        assert report.misses == []
        assert report.escalated_to_impersonation is False, (
            "the new host answers the identified agent, so nothing escalates"
        )

        rows = storage.corpus_documents("IN")
        assert len(rows) == 1
        assert rows[0]["language"] == "English"
        assert rows[0]["source_url"] == IN_PDF_URL
        assert rows[0]["fetched_at"] and rows[0]["full_text"]

    def test_the_discovery_record_carries_the_strategy_and_the_fetch_count(
        self, storage, tmp_path
    ):
        fetch = in_recorded_answers()
        report = discover_economy(
            "IN", storage, data_dir=tmp_path / "data", seeds=IN_SEEDS,
            limiter=NullLimiter(), fetch=fetch, get_json=fetch.get_json,
        )
        record = storage.run_get(report.run_id)
        assert record["kind"] == "discovery" and record["economy"] == "IN"
        assert record["status"] == "completed"
        assert record["documents_fetched"] == 1
        assert record["details"]["strategy"] == "dspace_rest"
        assert len(fetch.calls) == 2, "one search, one Document, nothing else"
        assert fetch.robots_calls == ["https://indiacode.gov.in/robots.txt"]

    def test_the_portals_silence_does_not_lower_our_floor(self, storage, tmp_path):
        """A Portal that publishes no rules (404) has asked for nothing, and
        nothing is not permission to hurry: the configured 2s floor applies."""
        fetch = in_recorded_answers()
        report = discover_economy(
            "IN", storage, data_dir=tmp_path / "data", seeds=IN_SEEDS,
            fetch=fetch, get_json=fetch.get_json,
            clock=lambda: 0.0, sleep=lambda s: None,
        )
        assert report.min_interval_seconds >= 2.0

    def test_a_run_over_the_discovered_corpus_yields_mappings(
        self, storage, tmp_path
    ):
        from regcompass.engines import fake_completion, fake_embed, resolve_engine
        from regcompass.pipeline import run_economy

        fetch = in_recorded_answers()
        discover_economy(
            "IN", storage, data_dir=tmp_path / "data", seeds=IN_SEEDS,
            limiter=NullLimiter(), fetch=fetch, get_json=fetch.get_json,
        )
        report = run_economy(
            storage, "IN", (7,), resolve_engine("fake"),
            data_dir=tmp_path / "data",
            completion_fn=fake_completion, embed_fn=fake_embed,
        )
        assert report.documents, "the Run reads the Corpus Discovery just filled"
        assert storage.load_mappings(economy="IN")


@pytest.fixture()
def default_policy_config(tmp_path):
    """A copy of config/ with India's operator policy line taken out: every
    Portal back on the default, where an unavailable robots.txt refuses. It is
    the one-line change the configuration comment names, made to a copy, so the
    default refusal stays tested now that the shipped file carries the policy."""
    import shutil

    config_dir = tmp_path / "default-config"
    shutil.copytree(_CONFIG_DIR, config_dir)
    portals_file = config_dir / "portals.yaml"
    text = portals_file.read_text(encoding="utf-8")
    line = "    robots_unavailable_policy: proceed"
    assert line in text, "the India policy line moved; fix this fixture"
    portals_file.write_text(
        "\n".join(r for r in text.splitlines() if not r.startswith(line)) + "\n",
        encoding="utf-8",
    )
    return config_dir


class TestIndiaUnderTheUnavailableRobotsPolicy:
    """What the Portal actually answered on 16 Sep 2026: HTTP 500, then HTTP
    502 on a retry the same day. Under RFC 9309 that is "unavailable", not "no
    rules", and the default is to stop. India carries the operator's decision of
    17 Sep 2026 to proceed for this Portal alone, so Discovery runs and the
    record says on whose decision, beside the status the Portal gave."""

    @pytest.mark.parametrize("status", [500, 502])
    def test_discovery_proceeds_and_the_law_pages_land_as_documents(
        self, storage, tmp_path, status
    ):
        fetch = in_recorded_answers(robots_status=status)
        report = discover_economy(
            "IN", storage, data_dir=tmp_path / "data", seeds=IN_SEEDS,
            limiter=NullLimiter(), fetch=fetch, get_json=fetch.get_json,
            today=BROKE_ON,
        )
        assert report.fetched == 1 and report.documents_stored == 1
        assert report.robots_unavailable_status == status
        assert report.robots_unavailable_policy == "proceed"
        rows = storage.corpus_documents("IN")
        assert len(rows) == 1 and rows[0]["source_url"] == IN_PDF_URL
        assert rows[0]["full_text"]

    def test_the_discovery_record_carries_the_status_and_the_policy(
        self, storage, tmp_path
    ):
        fetch = in_recorded_answers(robots_status=500)
        report = discover_economy(
            "IN", storage, data_dir=tmp_path / "data", seeds=IN_SEEDS,
            limiter=NullLimiter(), fetch=fetch, get_json=fetch.get_json,
            today=BROKE_ON,
        )
        record = storage.run_get(report.run_id)
        assert record["status"] == "completed"
        assert record["details"]["robots_unavailable_status"] == 500
        assert record["details"]["robots_unavailable_policy"] == "proceed"
        assert record["details"]["robots"] == report.robots_note
        assert "proceed" in report.robots_note and "500" in report.robots_note

    def test_a_run_over_the_corpus_it_filled_yields_mappings(self, storage, tmp_path):
        """The whole point of proceeding: a steward presses Discover on India
        and the Documents that land are Run-able, here on the fake Engine."""
        from regcompass.engines import fake_completion, fake_embed, resolve_engine
        from regcompass.pipeline import run_economy

        fetch = in_recorded_answers(robots_status=500)
        discover_economy(
            "IN", storage, data_dir=tmp_path / "data", seeds=IN_SEEDS,
            limiter=NullLimiter(), fetch=fetch, get_json=fetch.get_json,
            today=BROKE_ON,
        )
        report = run_economy(
            storage, "IN", (7,), resolve_engine("fake"),
            data_dir=tmp_path / "data",
            completion_fn=fake_completion, embed_fn=fake_embed,
        )
        assert report.documents, "the Run reads the Corpus Discovery just filled"
        assert storage.load_mappings(economy="IN")

    def test_proceeding_does_not_lower_the_portals_spacing_floor(
        self, storage, tmp_path
    ):
        """The policy is about the rules we could not read, never about hurry:
        India's configured 2 s floor is exactly what it was."""
        fetch = in_recorded_answers(robots_status=500)
        report = discover_economy(
            "IN", storage, data_dir=tmp_path / "data", seeds=IN_SEEDS,
            fetch=fetch, get_json=fetch.get_json, today=BROKE_ON,
            clock=lambda: 0.0, sleep=lambda s: None,
        )
        assert report.min_interval_seconds >= 2.0

    def test_an_off_whitelist_download_is_still_dropped_under_the_policy(
        self, storage, tmp_path
    ):
        """Every other politeness rule stands. The Portal whitelist binds what
        a Portal ANSWERS, and proceeding past an unreadable robots.txt is no
        licence to fetch from a host nobody verified."""
        search = json.loads(
            (PORTAL_FIXTURES / "in/search_dpdp_2026-09-16.json").read_text(encoding="utf-8")
        )
        item = search["_embedded"]["searchResult"]["_embedded"]["objects"][0][
            "_embedded"
        ]["indexableObject"]
        stream = item["_embedded"]["bundles"]["_embedded"]["bundles"][0]["_embedded"][
            "bitstreams"
        ]["_embedded"]["bitstreams"][0]
        stream["_links"]["content"]["href"] = "https://cdn.example.net/mirror/a.pdf"

        fetch = in_recorded_answers(robots_status=500, search=search)
        report = discover_economy(
            "IN", storage, data_dir=tmp_path / "data", seeds=IN_SEEDS,
            limiter=NullLimiter(), fetch=fetch, get_json=fetch.get_json,
            today=BROKE_ON,
        )
        assert report.skipped_off_host == 1 and report.fetched == 0
        assert storage.corpus_documents("IN") == []

    @pytest.mark.parametrize("status", [500, 502])
    def test_the_default_stops_before_it_asks_for_a_document(
        self, storage, tmp_path, status, default_policy_config
    ):
        """Take the policy line out and the refusal is exactly the one that was
        there before: the rule stays the default for every Portal."""
        from regcompass.crawl import RobotsUnavailableError

        fetch = in_recorded_answers(robots_status=status)
        with pytest.raises(RobotsUnavailableError) as exc:
            discover_economy(
                "IN", storage, data_dir=tmp_path / "data", seeds=IN_SEEDS,
                limiter=NullLimiter(), fetch=fetch, get_json=fetch.get_json,
                today=BROKE_ON, config_dir=default_policy_config,
            )
        assert exc.value.status == status
        assert fetch.calls == [], "not one Document was requested"
        assert "Add document" in str(exc.value), "the refusal names the way in"
        assert "2026-10-17" in str(exc.value), "and the date the refusal lifts"

    def test_the_default_record_carries_the_status_and_the_failure(
        self, storage, tmp_path, default_policy_config
    ):
        from regcompass.crawl import RobotsUnavailableError

        fetch = in_recorded_answers(robots_status=500)
        with pytest.raises(RobotsUnavailableError):
            discover_economy(
                "IN", storage, data_dir=tmp_path / "data", seeds=IN_SEEDS,
                limiter=NullLimiter(), fetch=fetch, get_json=fetch.get_json,
                today=BROKE_ON, config_dir=default_policy_config,
            )
        record = storage.runs_list(kind="discovery")[0]
        assert record["status"] == "failed"
        assert record["details"]["robots_unavailable_status"] == 500
        assert "robots_unavailable_policy" not in record["details"]
        assert "robots.txt" in record["error"]
        assert storage.corpus_documents("IN") == []

    def test_after_thirty_days_the_default_runs_and_the_record_says_why(
        self, storage, tmp_path, default_policy_config
    ):
        """RFC 9309's second half, untouched: on the default policy, once the
        Portal has been unreadable for more than 30 days the crawl goes ahead,
        and the reason it was allowed to is written onto the Discovery record
        rather than left to be inferred."""
        fetch = in_recorded_answers(robots_status=502)
        report = discover_economy(
            "IN", storage, data_dir=tmp_path / "data", seeds=IN_SEEDS,
            limiter=NullLimiter(), fetch=fetch, get_json=fetch.get_json,
            today=BROKE_ON + timedelta(days=31), config_dir=default_policy_config,
        )
        assert report.fetched == 1
        assert report.robots_note == (
            "unreachable since 2026-09-16, treated as no restrictions per"
            " RFC 9309 after 30 days"
        )
        record = storage.run_get(report.run_id)
        assert record["details"]["robots"] == report.robots_note
        assert "robots_unavailable_status" not in record["details"]
        assert "robots_unavailable_policy" not in record["details"]


class TestMalaysiaUnderTheThirtyDayRule:
    def test_a_long_broken_portal_is_crawled_and_the_record_says_on_what_basis(
        self, storage, tmp_path
    ):
        """Malaysia's robots.txt has answered HTTP 500 since 6 Jul 2026, which
        is the date recorded on its Portal. By 16 Sep that is 72 days, past the
        RFC's 30, so Discovery runs; what it must not do is run quietly, so the
        Discovery record carries the sentence that explains it."""
        def fetch(url: str) -> FetchResult:
            if url.endswith("/robots.txt"):
                return FetchResult(url, url, 500, b"<h1>500</h1>", "text/html", "httpx")
            return FetchResult(url, url, 200, b"%PDF-1.4 ...", "application/pdf", "httpx")

        report = discover_economy(
            "MY", storage, data_dir=tmp_path / "data", seeds=MY_SEEDS,
            limiter=NullLimiter(), fetch=fetch,
            get_json=lambda u, p=None: (200, {"response": {"docs": []}}),
            today=date(2026, 9, 16),
        )
        assert report.robots_note == (
            "unreachable since 2026-07-06, treated as no restrictions per"
            " RFC 9309 after 30 days"
        )
        record = storage.run_get(report.run_id)
        assert record["status"] == "completed"
        assert record["details"]["robots"] == report.robots_note


class TestOffWhitelistTargets:
    def test_a_download_url_on_another_host_is_dropped_and_counted(
        self, storage, tmp_path
    ):
        """The Portal whitelist binds what a Portal ANSWERS, not only what an
        operator types. The adapter reads download URLs out of India Code's own
        JSON; if that JSON ever named a third-party host, fetching it and
        filing the result as official evidence is exactly the failure the
        whitelist exists to prevent."""
        search = json.loads(
            (PORTAL_FIXTURES / "in/search_dpdp_2026-09-16.json").read_text(encoding="utf-8")
        )
        item = search["_embedded"]["searchResult"]["_embedded"]["objects"][0][
            "_embedded"
        ]["indexableObject"]
        stream = item["_embedded"]["bundles"]["_embedded"]["bundles"][0]["_embedded"][
            "bitstreams"
        ]["_embedded"]["bitstreams"][0]
        stream["_links"]["content"]["href"] = (
            "https://cdn.example.net/mirror/a2023-22.pdf"
        )

        fetch = in_recorded_answers(search=search)
        report = discover_economy(
            "IN", storage, data_dir=tmp_path / "data", seeds=IN_SEEDS,
            limiter=NullLimiter(), fetch=fetch, get_json=fetch.get_json,
        )
        assert report.skipped_off_host == 1
        assert report.fetched == 0
        assert fetch.calls == [IN_SEARCH_URL], (
            "the Portal's own search is asked; the off-whitelist download is not"
        )
        assert any("not on the Portal whitelist" in m for m in report.misses)
        assert storage.corpus_documents("IN") == []
        record = storage.run_get(report.run_id)
        assert record["details"]["skipped_off_host"] == 1

    def test_an_on_whitelist_url_is_untouched_by_the_check(self, storage, tmp_path):
        fetch = in_recorded_answers()
        report = discover_economy(
            "IN", storage, data_dir=tmp_path / "data", seeds=IN_SEEDS,
            limiter=NullLimiter(), fetch=fetch, get_json=fetch.get_json,
        )
        assert report.skipped_off_host == 0 and report.fetched == 1
        assert "skipped_off_host" not in storage.run_get(report.run_id)["details"]


class TestTheOperatorSuppliedEconomies:
    @pytest.mark.parametrize(
        "economy,name",
        [("VN", "Viet Nam"), ("KZ", "Kazakhstan"),
         ("MN", "Mongolia"), ("RU", "Russian Federation"), ("TL", "Timor-Leste")],
    )
    def test_discovery_refuses_and_names_the_add_lane(
        self, storage, tmp_path, economy, name
    ):
        with pytest.raises(ManualEconomyError) as exc:
            discover_economy(economy, storage, data_dir=tmp_path / "data")
        message = str(exc.value)
        assert name in message and "Add document" in message
        assert storage.runs_list(kind="discovery") == [], "no record for work never started"

    @pytest.mark.parametrize("economy", ["VN", "KZ", "MN", "RU", "CN", "TL"])
    def test_the_refusal_says_no_strategy_is_configured_not_that_it_is_forbidden(
        self, storage, tmp_path, economy
    ):
        """An Economy waiting for a Discovery plan and an Economy whose Portal
        rules forbid collection are two different statements, and the refusal
        has to make that visible: one may gain a strategy, the other never
        will, and only the second closes the add-by-URL lane."""
        with pytest.raises(ManualEconomyError) as exc:
            discover_economy(economy, storage, data_dir=tmp_path / "data")
        message = str(exc.value)
        assert "no Discovery strategy configured" in message
        assert "Source URL" in message and "upload" in message.lower()
        assert "manual-only" not in message
        assert "do not permit" not in message

    def test_a_manual_only_portal_says_it_does_not_permit_automated_collection(
        self, storage, tmp_path
    ):
        config_dir = forbidden_config(tmp_path)
        with pytest.raises(ManualEconomyError) as exc:
            discover_economy(
                FORBIDDEN, storage, config_dir=config_dir, data_dir=tmp_path / "data"
            )
        message = str(exc.value)
        assert FORBIDDEN_NAME in message and "manual-only" in message
        assert "do not permit automated collection" in message
        assert "upload" in message.lower()
        assert "no Discovery strategy configured" not in message

    @pytest.mark.parametrize("economy", ["VN", "KZ", "MN", "CN", "TL"])
    def test_their_hosts_are_whitelisted_so_an_operator_needs_no_override(
        self, economy
    ):
        """Verifying the host is the part of Discovery that CAN be done for
        these Portals, and it is what spares the operator the "official source
        outside the configured Portal" tick on 15 Oct."""
        from regcompass.config import load_portals

        portal = load_portals()[economy]
        assert portal.hosts, f"{economy} has no verified host"
        assert portal.manual_only is False, "these are waiting, not forbidden"
        assert portal.min_interval_seconds >= 2.0

    def test_the_russian_portal_lists_its_two_plain_http_hosts(self):
        """publication.pravo.gov.ru (image scans, amendments only) is never
        whitelisted; the consolidated texts on pravo.gov.ru and the acts bank
        on kremlin.ru are, over plain http because port 443 is closed on our
        path; the Eurasian Economic Commission's own PDFs are, over https.
        Discovery by Pillar fetches their seeded addresses."""
        from regcompass.config import load_portals

        portal = load_portals()["RU"]
        assert portal.hosts == ["pravo.gov.ru", "kremlin.ru", "eec.eaeunion.org"]
        assert "publication.pravo.gov.ru" not in portal.hosts
        assert portal.http_hosts == ["pravo.gov.ru", "kremlin.ru"]
        assert portal.strategy == "manual"

    def test_an_operator_supplied_document_reaches_the_corpus(
        self, storage, tmp_path
    ):
        from regcompass.corpus import add_document

        added = add_document(
            storage, tmp_path / "data", "VN",
            b"<html><body><h1>Luat An ninh mang 2018</h1>"
            b"<p>test stand-in, not Portal bytes: written for this test suite,"
            b" never fetched from vbpl.vn.</p>"
            b"<p>Article 26. Enterprises shall store in Viet Nam data on"
            b" personal information of service users in Viet Nam.</p></body></html>",
            source_url="https://vbpl.vn/van-ban/trung-uong/luat-an-ninh-mang-2018",
            filename_hint="luat_an_ninh_mang_2018.html",
        )
        assert added.economy == "VN" and added.language == "Vietnamese"
        rows = storage.corpus_documents("VN")
        assert [r["source_url"] for r in rows] == [
            "https://vbpl.vn/van-ban/trung-uong/luat-an-ninh-mang-2018"
        ]

    def test_the_url_lane_honours_the_rules_the_portal_publishes(self, tmp_path):
        """The add lane makes a request, so it reads robots.txt first. Viet Nam
        forbids /api/ and Kazakhstan forbids /search: an operator who pastes
        one of those is told no, before anything is asked of the Portal."""
        from regcompass.corpus import add_document_from_url
        from regcompass.crawl import RobotsDisallowedError, parse_robots

        storage = Storage(tmp_path / "refused.db")
        storage.apply_schema()
        asked: list[str] = []

        def fetch(url: str) -> FetchResult:
            asked.append(url)
            return FetchResult(url, url, 200, b"<html>x</html>", "text/html", "httpx")

        for economy, robots_file, url in (
            ("VN", "vn/robots_2026-09-16.txt", "https://vbpl.vn/api/van-ban/1"),
            ("KZ", "kz/robots_2026-09-16.txt", "https://adilet.zan.kz/search?q=data"),
        ):
            policy = parse_robots(
                (PORTAL_FIXTURES / robots_file).read_text(encoding="utf-8")
            )
            with pytest.raises(RobotsDisallowedError):
                add_document_from_url(
                    storage, tmp_path / "data", economy, url,
                    fetch=fetch, limiter=NullLimiter(), robots=policy,
                )
        assert asked == [], "a refused URL is never requested"
        storage.close()
