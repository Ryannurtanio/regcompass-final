"""Lao PDR: the Official Gazette's legislation grid, replayed from recorded bytes.

Every test here reads the answers the Portal gave on 16 Sep 2026, committed under
tests/fixtures/portals/la/ with their URLs and their request count
(tests/fixtures/portals/la/README.md). Nothing in this file touches the network:
the fetch is a recorded one that raises for any URL that was not captured, so a
strategy that quietly asks the Portal for something else fails the test rather
than the Portal.

What the Lao lane has to get right, and what is proved below:
  * the Portal answers /robots.txt with its own homepage (a soft 404), which
    publishes NO rules rather than an accidental permission or refusal;
  * the page carries two look-alike tables, and only the legislation grid is
    results;
  * a row's Lao PDF is the Document and its English rendering is a note, never
    a Source URL;
  * the walk pages through a filter's results and stops at the configured bound.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# A base-tier install (no `live` extra) must SKIP this module, not error at
# collection: the Lao adapter parses HTML with bs4 and fetches with httpx.
pytest.importorskip("httpx", reason="the Lao Portal lane needs the `live` extra")
pytest.importorskip("bs4", reason="the Lao Portal lane needs the `live` extra")

from regcompass.config import load_crawl_seeds, load_portals  # noqa: E402
from regcompass.contracts import CrawlFamily, CrawlSeedsEconomy  # noqa: E402
from regcompass.crawl import (  # noqa: E402
    LA_ENGLISH_NOTE,
    FetchResult,
    LaoGridError,
    RateLimiter,
    RobotsPolicy,
    discover_la,
    la_listing_url,
    parse_la_listing,
    parse_robots,
    read_robots_policy,
)
from regcompass.discovery import discover_economy  # noqa: E402
from regcompass.engines import fake_completion, fake_embed, resolve_engine  # noqa: E402
from regcompass.pipeline import run_economy  # noqa: E402
from regcompass.storage import Storage  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures/portals/la"
GAZETTE = "https://laoofficialgazette.gov.la"
LISTING = f"{GAZETTE}/index.php?r=site/index"

# The URLs exactly as they went out on 16 Sep 2026 (the README lists all nine).
# They are spelled out rather than built, so a change in how the adapter encodes
# a filter shows up here as a missing recording.
ROBOTS_URL = f"{GAZETTE}/robots.txt"
ELECTRONIC = f"{LISTING}&Document%5Btitle%5D=%E0%BB%80%E0%BA%AD%E0%BB%80%E0%BA%A5%E0%BA%B1%E0%BA%81"
ELECTRONIC_P2 = f"{ELECTRONIC}&Document_page=2"
CYBER = f"{LISTING}&Document%5Btitle%5D=%E0%BB%84%E0%BA%8A%E0%BB%80%E0%BA%9A%E0%BA%B5"
CONSUMER = (
    f"{LISTING}&Document%5Btitle%5D="
    "%E0%BA%9C%E0%BA%B9%E0%BB%89%E0%BA%8A%E0%BA%BB%E0%BA%A1%E0%BB%83%E0%BA%8A%E0%BB%89"
)
TELECOM = f"{LISTING}&Document%5Btitle%5D=%E0%BB%82%E0%BA%97%E0%BA%A5%E0%BA%B0%E0%BA%84%E0%BA%BB%E0%BA%A1"
TELECOM_P2 = f"{TELECOM}&Document_page=2"
COMPUTER = (
    f"{LISTING}&Document%5Btitle%5D="
    "%E0%BA%84%E0%BA%AD%E0%BA%A1%E0%BA%9E%E0%BA%B4%E0%BA%A7%E0%BB%80%E0%BA%95%E0%BA%B5"
)
LAW_PAGE = f"{GAZETTE}/index.php?r=site/display&id=2023"

RECORDED_PAGES = {
    ROBOTS_URL: "robots_2026-09-16.html",
    ELECTRONIC: "listing_electronic_2026-09-16.html",
    ELECTRONIC_P2: "listing_electronic_page2_2026-09-16.html",
    CYBER: "listing_cyber_2026-09-16.html",
    CONSUMER: "listing_consumer_2026-09-16.html",
    TELECOM: "listing_telecom_2026-09-16.html",
    TELECOM_P2: "listing_telecom_page2_2026-09-16.html",
    COMPUTER: "listing_computer_2026-09-16.html",
    LAW_PAGE: "law_display_2023_2026-09-16.html",
}

# The three named instruments that these recordings reach.
ETL_PDF = f"{GAZETTE}/kcfinder/upload/files/31%E0%BA%AA%E0%BA%9E%E0%BA%8A2022.pdf"
CYBER_PDF = f"{GAZETTE}/kcfinder/upload/files/87.25.6.2025_0001.pdf"
CONSUMER_LAO_PDF = f"{GAZETTE}/kcfinder/upload/files/Protecting%20consumers%20Law%20.pdf"
CONSUMER_ENGLISH_PDF = f"{GAZETTE}/kcfinder/upload/files/Law%20on%20Consumer%20Protection.pdf"
DATA_PROTECTION_PDF = f"{GAZETTE}/kcfinder/upload/files/0918570.pdf"
CYBER_CRIME_PDF = f"{GAZETTE}/kcfinder/upload/files/1.%20Law%20on%20cyber%20crime.pdf"

FAKE_ENGINE = resolve_engine("fake")


def recorded(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def page(url: str) -> str:
    return recorded(RECORDED_PAGES[url]).decode("utf-8")


# ---------------------------------------------------------------------------
# test doubles
# ---------------------------------------------------------------------------


def _pdf(lines: list[str]) -> bytes:
    """A minimal one-page PDF with a real text layer, so a Document ingests
    through the text lane and no test needs tesseract. The Lao scans themselves
    are proved in tests/test_non_english_lane.py against the committed statute."""
    content = b"".join(
        b"BT /F1 12 Tf 72 %d Td (%s) Tj ET\n" % (720 - 20 * i, line.encode("ascii"))
        for i, line in enumerate(lines)
    )
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (
        len(objs) + 1, xref,
    )
    return bytes(out)


PROVISIONS = [
    "Section 5. A service provider shall keep the electronic records of every "
    "transaction it processes for five years.",
    "Section 6. A service provider shall protect the personal data of a user "
    "and shall not disclose it without consent.",
    "Section 7. A dispute over an electronic transaction may be settled by the "
    "authority named in this instrument.",
]


def recorded_fetch():
    """Every recorded Portal answer, and a text-layer PDF for each Document the
    grid points at. A URL that was never captured raises: the strategy must ask
    for what was recorded and nothing else."""
    calls: list[str] = []
    robots_calls: list[str] = []

    def fetch(url: str) -> FetchResult:
        if url.endswith("/robots.txt"):
            robots_calls.append(url)
            return FetchResult(
                url, url, 200, recorded(RECORDED_PAGES[ROBOTS_URL]), "text/html", "httpx"
            )
        calls.append(url)
        if url in RECORDED_PAGES:
            return FetchResult(
                url, url, 200, recorded(RECORDED_PAGES[url]), "text/html; charset=UTF-8", "httpx"
            )
        if url.lower().endswith(".pdf") and url.startswith(f"{GAZETTE}/kcfinder/"):
            # One Document, one body: identical bytes would be deduplicated by
            # the manifest, which is right for the Portal and wrong for a test
            # that needs each instrument to arrive as itself.
            name = "".join(c for c in url.rsplit("/", 1)[-1] if c.isalnum())
            return FetchResult(
                url, url, 200,
                _pdf([f"Instrument reference {name}.", *PROVISIONS]),
                "application/pdf", "httpx",
            )
        raise AssertionError(f"no recorded Portal answer for {url}")

    fetch.calls = calls
    fetch.robots_calls = robots_calls
    return fetch


class NullLimiter(RateLimiter):
    """No waiting, but every wait is recorded, so spacing is checkable."""

    def __init__(self):
        super().__init__(0.0, clock=lambda: 0.0, sleep=lambda s: None)
        self.waited: list[str] = []

    def wait(self, url: str) -> None:
        self.waited.append(url)
        super().wait(url)


def seeds_for(**families: str) -> CrawlSeedsEconomy:
    return CrawlSeedsEconomy(
        rate_limit_seconds=2,
        max_pages=10,
        families={name: CrawlFamily(queries=[word]) for name, word in families.items()},
    )


ELECTRONIC_SEEDS = seeds_for(electronic_transactions="ເອເລັກ")
THREE_TOPICS = seeds_for(
    cybersecurity="ໄຊເບີ", consumer_protection="ຜູ້ຊົມໃຊ້", telecommunications="ໂທລະຄົມ"
)


@pytest.fixture()
def storage(tmp_path):
    s = Storage(tmp_path / "lao.db")
    s.apply_schema()
    yield s
    s.close()


# ---------------------------------------------------------------------------
# what the Portal publishes, and what it does not
# ---------------------------------------------------------------------------


class TestTheSoftFourOhFourRobots:
    def test_the_homepage_served_as_robots_publishes_no_rules(self):
        """The recorded body is 135,911 bytes of the Portal's own homepage.
        Reading rules out of markup would invent both permissions and refusals;
        the honest answer is that this host publishes none."""
        body = recorded(RECORDED_PAGES[ROBOTS_URL]).decode("utf-8")
        assert "<html" in body.lower()
        assert parse_robots(body) == RobotsPolicy()
        assert parse_robots(body).allows(ETL_PDF)

    def test_the_one_robots_door_reads_the_soft_404_as_no_rules(self):
        """Two rules meet on this Portal and must not fight. RFC 9309 sorts the
        answer by STATUS (4xx no rules, 5xx refuse until the 30-day date), and
        this host answers 200, so the reading turns on the BODY: a web page is
        not a robots file. Read through the one door every lane uses."""
        reading = read_robots_policy(GAZETTE.removeprefix("https://"), recorded_fetch())
        assert reading.policy == RobotsPolicy()
        assert reading.note is None, "no 30-day rule was applied; the host answered"
        assert reading.policy.allows(ETL_PDF)

    def test_a_published_disallow_would_still_bind_this_host(self):
        """No rules today is not a licence forever: a real robots.txt from this
        host is read and obeyed exactly as Singapore's is."""
        policy = parse_robots("User-agent: *\nDisallow: /kcfinder/\nCrawl-delay: 4\n")
        assert policy.crawl_delay == 4.0
        assert not policy.allows(ETL_PDF)
        assert policy.allows(ELECTRONIC)

    def test_a_disallowed_listing_is_never_requested(self):
        fetch = recorded_fetch()
        targets, misses = discover_la(
            ELECTRONIC_SEEDS, "LA", fetch=fetch, limiter=NullLimiter(),
            robots=parse_robots("User-agent: *\nDisallow: /index.php\n"),
        )
        assert targets == []
        assert fetch.calls == [], "a refused listing page is never asked for"
        assert any("robots.txt disallows the listing" in m for m in misses)


# ---------------------------------------------------------------------------
# the grid
# ---------------------------------------------------------------------------


class TestTheLegislationGrid:
    def test_only_the_legislation_grid_is_results(self):
        """The page also carries a table of recent publications whose rows look
        identical. The cyber filter matched ONE instrument; a parser reading the
        page rather than the grid would report eleven."""
        rows, shown, total = parse_la_listing(page(CYBER))
        assert (shown, total) == (1, 1)
        assert len(rows) == 1
        assert "ໄຊເບີ" in rows[0].title
        assert rows[0].lao_pdf_url == CYBER_PDF

    def test_the_summary_line_says_how_many_results_the_filter_matched(self):
        rows, shown, total = parse_la_listing(page(ELECTRONIC))
        assert (len(rows), shown, total) == (10, 10, 17)
        rows, shown, total = parse_la_listing(page(ELECTRONIC_P2))
        assert (len(rows), shown, total) == (7, 17, 17)

    def test_the_lao_and_english_columns_are_read_by_their_headers(self):
        """The Consumer Protection law is the one recorded instrument published
        in both, and the two files are named the other way round
        ("Law on Consumer Protection.pdf" sits in the ENGLISH column). Position
        and header agree here; the header is what the parser trusts."""
        rows, _, _ = parse_la_listing(page(CONSUMER))
        law = next(r for r in rows if r.title.startswith("ກົດໝາຍ"))
        assert law.lao_pdf_url == CONSUMER_LAO_PDF
        assert law.english_pdf_url == CONSUMER_ENGLISH_PDF
        assert [r.english_pdf_url for r in rows if r is not law] == [None] * 4

    def test_a_page_without_the_grid_says_the_shape_changed(self):
        """The Malaysian lesson: an empty result set would blame the search
        terms for a change in the Portal."""
        with pytest.raises(LaoGridError) as exc:
            parse_la_listing("<html><body><p>maintenance</p></body></html>")
        assert "grid" in str(exc.value)

    def test_the_grid_url_is_the_portals_own(self):
        assert la_listing_url("ໄຊເບີ") == CYBER
        assert la_listing_url("ໂທລະຄົມ", 2) == TELECOM_P2


class TestTheLawPageAddsNothing:
    def test_the_law_page_offers_the_same_single_lao_pdf(self):
        """Why the strategy reads the listing and stops: the instrument's own
        page (site/display&id=2023, the Law on Electronic Transactions) carries
        exactly the PDF its grid row already gave, so opening it would cost one
        request per instrument and add no reference."""
        import re

        body = page(LAW_PAGE)
        links = {
            f"{GAZETTE}{href}" for href in re.findall(r'href="(/kcfinder/[^"]+\.pdf)"', body)
        }
        assert len(links) == 1
        from urllib.parse import unquote

        assert unquote(links.pop()) == unquote(ETL_PDF)


# ---------------------------------------------------------------------------
# seeds to targets
# ---------------------------------------------------------------------------


class TestDiscoverLa:
    def test_every_row_of_the_filter_becomes_a_document_on_the_whitelist(self):
        fetch = recorded_fetch()
        targets, misses = discover_la(
            ELECTRONIC_SEEDS, "LA", fetch=fetch, limiter=NullLimiter()
        )
        assert misses == []
        assert len(targets) == 17, "both pages of the filter's 17 results"
        assert fetch.calls == [ELECTRONIC, ELECTRONIC_P2]
        assert all(t.url.startswith(f"{GAZETTE}/kcfinder/") for t in targets)
        assert all(t.economy == "LA" for t in targets)
        assert {t.source_family for t in targets} == {"electronic_transactions"}
        urls = [t.url for t in targets]
        assert ETL_PDF in urls, "the Law on Electronic Transactions (Amended)"
        assert DATA_PROTECTION_PDF in urls, "the Law on Electronic Data Protection"

    def test_the_english_rendering_is_a_note_never_a_source_url(self):
        targets, _ = discover_la(
            seeds_for(consumer_protection="ຜູ້ຊົມໃຊ້"), "LA",
            fetch=recorded_fetch(), limiter=NullLimiter(),
        )
        law = next(t for t in targets if t.url == CONSUMER_LAO_PDF)
        assert law.notes == LA_ENGLISH_NOTE + CONSUMER_ENGLISH_PDF
        assert CONSUMER_ENGLISH_PDF not in [t.url for t in targets]
        assert [t.notes for t in targets if t is not law] == [None] * 4

    def test_the_walk_stops_at_the_configured_bound(self):
        fetch = recorded_fetch()
        one_page = ELECTRONIC_SEEDS.model_copy(update={"max_pages": 1})
        targets, _ = discover_la(one_page, "LA", fetch=fetch, limiter=NullLimiter())
        assert len(targets) == 10 and fetch.calls == [ELECTRONIC]

    def test_one_instrument_matching_two_families_is_one_document(self):
        targets, _ = discover_la(
            seeds_for(electronic_transactions="ເອເລັກ", consumer_protection="ຜູ້ຊົມໃຊ້"),
            "LA", fetch=recorded_fetch(), limiter=NullLimiter(),
        )
        urls = [t.url for t in targets]
        assert len(urls) == len(set(urls))
        vat = f"{GAZETTE}/kcfinder/upload/files/0558%2014.2.2024.pdf"
        assert urls.count(vat) == 1, "the instruction both filters match"

    def test_every_listing_request_goes_through_the_limiter(self):
        limiter = NullLimiter()
        discover_la(ELECTRONIC_SEEDS, "LA", fetch=recorded_fetch(), limiter=limiter)
        assert limiter.waited == [ELECTRONIC, ELECTRONIC_P2]

    def test_a_filter_that_matches_nothing_is_a_miss(self):
        def empty(url: str) -> FetchResult:
            body = (
                '<div id="homelegal-grid" class="grid-view">'
                '<div class="summary">0</div><table class="items"><thead><tr>'
                "<th>ນິຕິກໍາ</th><th>PDF ອັງກິດ</th><th>PDF ລາວ</th></tr></thead>"
                '<tbody><tr class="empty"><td>ບໍ່ພົບ</td></tr></tbody></table></div>'
            )
            return FetchResult(url, url, 200, body.encode(), "text/html", "httpx")

        targets, misses = discover_la(
            seeds_for(cybersecurity="ບໍ່ມີ"), "LA", fetch=empty, limiter=NullLimiter()
        )
        assert targets == []
        assert misses == ["LA/cybersecurity: no Documents for 'ບໍ່ມີ'"]

    def test_a_row_offering_only_an_english_rendering_is_skipped_and_said(self):
        def english_only(url: str) -> FetchResult:
            body = (
                '<div id="homelegal-grid" class="grid-view">'
                '<div class="summary">ສະແດງ 1-1 ຂອງ 1</div>'
                '<table class="items"><thead><tr><th>ນິຕິກໍາ</th><th>ພາກສ່ວນ</th>'
                "<th>ວັນທີ</th><th>ເຜີຍແຜ່</th><th>ປະເພດ</th><th>ເນື້ອໃນ</th>"
                "<th>PDF ອັງກິດ</th><th>PDF ລາວ</th></tr></thead><tbody>"
                '<tr class="odd"><td>Draft law</td><td>x</td><td>1</td><td>2</td>'
                "<td>ກົດໝາຍ</td><td>y</td>"
                '<td><a href="/kcfinder/upload/files/draft-en.pdf">en</a></td>'
                "<td></td></tr></tbody></table></div>"
            )
            return FetchResult(url, url, 200, body.encode(), "text/html", "httpx")

        targets, misses = discover_la(
            seeds_for(cybersecurity="ໄຊເບີ"), "LA", fetch=english_only, limiter=NullLimiter()
        )
        assert targets == [], "an English rendering is never the quote source"
        assert any("only an English rendering" in m for m in misses)


# ---------------------------------------------------------------------------
# Discovery: the Corpus, the record, and a Run over it
# ---------------------------------------------------------------------------


class TestDiscoveryFillsTheLaoCorpus:
    def test_five_documents_across_three_topics_in_lao(self, storage, tmp_path):
        fetch = recorded_fetch()
        limiter = NullLimiter()
        report = discover_economy(
            "LA", storage, data_dir=tmp_path / "data", seeds=THREE_TOPICS,
            fetch=fetch, limiter=limiter,
        )

        assert report.strategy == "httpx"
        assert report.fetched >= 5 and report.documents_stored == report.fetched
        rows = storage.corpus_documents("LA")
        assert len(rows) >= 5
        for row in rows:
            assert row["language"] == "Lao"
            assert row["source_url"].startswith(f"{GAZETTE}/kcfinder/")
            assert row["fetched_at"] and row["full_text"]
        families = {
            r["source_family"] for r in storage.manifest_rows(economy="LA", status="fetched")
        }
        assert families == {"cybersecurity", "consumer_protection", "telecommunications"}

        # The record carries the count, robots was read once, and EVERY request
        # went through the limiter (the floor itself is the next test).
        record = storage.run_get(report.run_id)
        assert record["kind"] == "discovery" and record["economy"] == "LA"
        assert record["documents_fetched"] == report.fetched
        assert fetch.robots_calls == [ROBOTS_URL]
        assert limiter.waited[0] in (CYBER, CONSUMER, TELECOM)
        assert len(limiter.waited) == len(fetch.calls)

    def test_the_english_rendering_reaches_the_corpus_row(self, storage, tmp_path):
        discover_economy(
            "LA", storage, data_dir=tmp_path / "data",
            seeds=seeds_for(consumer_protection="ຜູ້ຊົມໃຊ້"),
            fetch=recorded_fetch(), limiter=NullLimiter(),
        )
        notes = {
            r["source_url"]: r["notes"] for r in storage.corpus_documents("LA")
        }
        assert notes[CONSUMER_LAO_PDF] == LA_ENGLISH_NOTE + CONSUMER_ENGLISH_PDF
        assert CONSUMER_ENGLISH_PDF not in notes, "never a Document of its own"
        assert [v for v in notes.values() if v is None] == [None] * 4

    def test_the_min_interval_is_the_portals_floor_when_robots_is_silent(
        self, storage, tmp_path
    ):
        report = discover_economy(
            "LA", storage, data_dir=tmp_path / "data",
            seeds=seeds_for(cybersecurity="ໄຊເບີ"), fetch=recorded_fetch(),
            clock=lambda: 0.0, sleep=lambda s: None,
        )
        assert report.min_interval_seconds == 2.0

    def test_a_run_over_the_lao_corpus_produces_verified_mappings(self, storage, tmp_path):
        discover_economy(
            "LA", storage, data_dir=tmp_path / "data",
            seeds=seeds_for(cybersecurity="ໄຊເບີ"), fetch=recorded_fetch(),
            limiter=NullLimiter(),
        )
        report = run_economy(
            storage, "LA", (6,), FAKE_ENGINE, data_dir=tmp_path / "data",
            completion_fn=fake_completion, embed_fn=fake_embed,
        )
        assert report.documents, "the Run reads the Corpus Discovery filled"
        assert report.n_passed > 0, "the verbatim-quote lane must pass in the Lao lane"
        assert storage.load_mappings(economy="LA", verification_status="passed")

    def test_the_export_battery_is_green_on_a_lao_row(self, storage, tmp_path):
        """The metadata seam: Economy spelling, Language of Source, and the
        Source URL's host all have to satisfy the export's own gate battery.
        Whole-Run exports are covered elsewhere; this is one Lao Run through the
        same gate."""
        from regcompass.pipeline import export_from_db

        discover_economy(
            "LA", storage, data_dir=tmp_path / "data",
            seeds=seeds_for(cybersecurity="ໄຊເບີ"), fetch=recorded_fetch(),
            limiter=NullLimiter(),
        )
        run_economy(
            storage, "LA", (6,), FAKE_ENGINE, data_dir=tmp_path / "data",
            completion_fn=fake_completion, embed_fn=fake_embed,
        )
        result = export_from_db(storage, tmp_path / "out", check_pointers=False)
        assert result.battery_failures == []
        lao = [r for r in result.rows if r["Economy"] == "Lao PDR"]
        assert lao, "the Coverage Matrix spelling is what the export writes"
        assert all(r["Language of Source"] == "Lao" for r in lao)
        assert all(r["Source URL"].startswith(GAZETTE) for r in lao)
        # The Cyber Security law has no English rendering, so nothing is added.
        assert all(LA_ENGLISH_NOTE not in r["Notes"] for r in lao)

    def test_the_english_rendering_reaches_the_submissions_notes(self, storage, tmp_path):
        """The whole point of recording the English URL: a reader of the
        submission can find the Portal's own rendering of a Lao instrument, on
        the row whose evidence came from that instrument and on no other."""
        from regcompass.pipeline import export_from_db

        discover_economy(
            "LA", storage, data_dir=tmp_path / "data",
            seeds=seeds_for(consumer_protection="ຜູ້ຊົມໃຊ້"), fetch=recorded_fetch(),
            limiter=NullLimiter(),
        )
        run_economy(
            storage, "LA", (6,), FAKE_ENGINE, data_dir=tmp_path / "data",
            completion_fn=fake_completion, embed_fn=fake_embed,
        )
        result = export_from_db(storage, tmp_path / "out", check_pointers=False)
        assert result.battery_failures == []
        noted = [r for r in result.rows if LA_ENGLISH_NOTE in r["Notes"]]
        assert noted, "the Consumer Protection law's English URL must reach Notes"
        for row in noted:
            assert row["Notes"].endswith(CONSUMER_ENGLISH_PDF), "the reference comes last"
            assert row["Source URL"] == CONSUMER_LAO_PDF, "the Lao file stays the source"
        # and only there: the other four instruments carry no rendering
        assert [r["Source URL"] for r in noted] == [CONSUMER_LAO_PDF] * len(noted)


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


class TestTheLaoPortalIsConfigured:
    def test_the_portal_declares_the_host_the_language_and_the_floor(self):
        portal = load_portals()["LA"]
        assert portal.official_name == "Lao PDR"
        assert portal.hosts[0] == "laoofficialgazette.gov.la"
        assert portal.strategy == "httpx" and portal.manual_only is False
        assert portal.languages[0] == "Lao"
        assert portal.min_interval_seconds == 2.0
        assert portal.live_test_pool is True
        # `prepared` is a fact about the CORPUS, not about the adapter: the
        # Portal and its seeds are verified here, and the operator's supervised
        # live Discovery of 23 Sep 2026 fetched and checked the Corpus itself
        # (37 Documents from the Gazette at the 2 s floor, no refusal).
        assert portal.prepared is True

    def test_the_seeds_are_lao_words_for_the_portals_own_filter(self):
        seeds = load_crawl_seeds()["LA"]
        assert seeds.rate_limit_seconds == 2
        assert seeds.max_pages == 10
        assert set(seeds.families) == {
            "electronic_transactions", "cybersecurity",
            "consumer_protection", "telecommunications",
        }
        for family in seeds.families.values():
            assert family.queries and not family.acts
            assert all(not q.isascii() for q in family.queries)

    def test_the_configured_seeds_reach_the_named_statutes(self):
        """Config and code together, over the recorded pages: the four seed
        words find the Electronic Transactions, Electronic Data Protection,
        Cyber Security and Consumer Protection laws."""
        targets, misses = discover_la(
            load_crawl_seeds()["LA"], "LA",
            fetch=recorded_fetch(), limiter=NullLimiter(),
        )
        urls = {t.url for t in targets}
        assert misses == []
        assert {
            ETL_PDF,            # Law on Electronic Transactions (Amended)
            DATA_PROTECTION_PDF,  # Law on Electronic Data Protection
            CYBER_PDF,          # Law on Cyber Security
            CYBER_CRIME_PDF,    # Law on Prevention and Combating Cyber Crime
            CONSUMER_LAO_PDF,   # Law on Consumer Protection
        } <= urls
        assert len({t.source_family for t in targets}) == 4

    def test_the_two_cyber_vocabularies_are_both_seeded(self):
        """The Portal titles the 2025 security law with ໄຊເບີ and the crime law
        with ອາຊະຍາກຳທາງລະບົບຄອມພິວເຕີ, so one word reaches only one of them."""
        fetch = recorded_fetch()
        configured = load_crawl_seeds()["LA"]
        cyber_only = configured.model_copy(
            update={"families": {"cybersecurity": configured.families["cybersecurity"]}}
        )
        targets, misses = discover_la(
            cyber_only, "LA", fetch=fetch, limiter=NullLimiter()
        )
        assert misses == []
        assert fetch.calls == [CYBER, COMPUTER]
        assert {CYBER_PDF, CYBER_CRIME_PDF} <= {t.url for t in targets}
