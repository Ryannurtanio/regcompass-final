"""Indonesia: Portal, Discovery and the escalation that makes it possible.

Everything here runs offline. The recorded Portal answers under
tests/fixtures/portals/id/ are replayed through a fetch that counts its own
calls, and the Document bodies are small text-layer PDFs built in this file, so
"no request was made" is a fact these tests can assert.

The Indonesian Portal (peraturan.bpk.go.id) publishes rules that permit us and
then refuses the identified user agent at its Cloudflare edge. That is the same
shape Singapore has, and it gets the same answer: ask as ourselves first, climb
to the browser-impersonating rung only after the refusal, and record on the
Discovery record that we did.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from urllib.parse import urlsplit

import pytest

# A base-tier install (no `live` extra) must SKIP this module, not error at
# collection: crawl.py, which discovery composes, needs httpx.
pytest.importorskip("httpx", reason="Indonesian Portal tests need the `live` extra (httpx)")

from regcompass.config import CONFIG_DIR, load_crawl_seeds, load_portals  # noqa: E402
from regcompass.corpus import (  # noqa: E402
    HostNotAllowedError,
    add_document,
    check_host_allowed,
)
from regcompass.crawl import (  # noqa: E402
    ID_DOWNLOAD,
    STRATEGIES,
    FetchResult,
    RateLimiter,
    discover_id,
    fetch_with_ladder,
    parse_robots,
    read_robots_policy,
)
from regcompass.discovery import ManualEconomyError, discover_economy  # noqa: E402
from regcompass.engines import fake_completion, fake_embed, resolve_engine  # noqa: E402
from regcompass.pipeline import run_economy  # noqa: E402
from regcompass.storage import Storage  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "portals" / "id"
HOST = "peraturan.bpk.go.id"
ROBOTS_URL = f"https://{HOST}/robots.txt"

# Stamped into the text layer of every synthetic Document below, so the bytes
# themselves say what they are and nothing extracted from them can be mistaken
# for something the Portal served.
STAND_IN_MARK = "test stand-in, not Portal bytes"


# ---------------------------------------------------------------------------
# Synthetic Documents: real text-layer PDFs, Bahasa Indonesia, built here
# ---------------------------------------------------------------------------


def text_layer_pdf(lines: list[str]) -> bytes:
    """A minimal one-page PDF whose text layer is exactly `lines`.

    The Portal's own statute PDFs were never recorded (see the fixture
    README), so the Document bodies these tests fetch are stand-ins. Building
    them as REAL PDFs rather than HTML keeps the extraction lane under test:
    ingest sniffs `%PDF-`, pdfplumber reads the text layer, and OCR stays off
    because the text is there.

    Every one of them carries STAND_IN_MARK in its own text layer, so the
    disclaimer travels in the bytes: anything extracted from one of these, in a
    Corpus, a chunk or a quote, says on its face that it did not come from the
    Portal. A docstring cannot do that.
    """
    content = b"BT /F1 11 Tf 40 780 Td 14 TL\n"
    lines = [*lines, STAND_IN_MARK]
    for line in lines:
        escaped = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        content += f"({escaped}) Tj T*\n".encode("latin-1")
    content += b"ET\n"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842]"
        b" /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica"
        b" /Encoding /WinAnsiEncoding >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    start_xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{start_xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


# One body per seeded law, keyed by the download reference in
# config/crawl_seeds.yaml. The text is short Bahasa Indonesia statutory prose.
SEEDED_BODIES: dict[str, list[str]] = {
    "224884/UU Nomor 27 Tahun 2022.pdf": [
        "UNDANG-UNDANG REPUBLIK INDONESIA NOMOR 27 TAHUN 2022",
        "TENTANG PELINDUNGAN DATA PRIBADI",
        "Pasal 20",
        "Pengendali Data Pribadi wajib menjaga kerahasiaan Data Pribadi.",
        "Pasal 56",
        "Transfer Data Pribadi kepada penerima di luar wilayah hukum",
        "Republik Indonesia dilakukan sesuai dengan ketentuan Undang-Undang ini.",
    ],
    "26683/UU Nomor 11 Tahun 2008.pdf": [
        "UNDANG-UNDANG REPUBLIK INDONESIA NOMOR 11 TAHUN 2008",
        "TENTANG INFORMASI DAN TRANSAKSI ELEKTRONIK",
        "Pasal 5",
        "Informasi Elektronik dan Dokumen Elektronik merupakan alat bukti",
        "hukum yang sah dan merupakan perluasan dari alat bukti yang sah.",
    ],
    "26676/UU Nomor  19 Tahun 2016.pdf": [
        "UNDANG-UNDANG REPUBLIK INDONESIA NOMOR 19 TAHUN 2016",
        "TENTANG PERUBAHAN ATAS UNDANG-UNDANG NOMOR 11 TAHUN 2008",
        "Pasal 26",
        "Penyelenggara Sistem Elektronik wajib menghapus Informasi Elektronik",
        "yang tidak relevan atas permintaan orang yang bersangkutan.",
    ],
    "332870/UU Nomor 1 Tahun 2024.pdf": [
        "UNDANG-UNDANG REPUBLIK INDONESIA NOMOR 1 TAHUN 2024",
        "TENTANG PERUBAHAN KEDUA ATAS UNDANG-UNDANG NOMOR 11 TAHUN 2008",
        "Pasal 40",
        "Pemerintah melindungi kepentingan umum dari segala jenis gangguan",
        "sebagai akibat penyalahgunaan Informasi Elektronik.",
    ],
    "33784/UU Nomor 8 Tahun 1999.pdf": [
        "UNDANG-UNDANG REPUBLIK INDONESIA NOMOR 8 TAHUN 1999",
        "TENTANG PERLINDUNGAN KONSUMEN",
        "Pasal 4",
        "Hak konsumen adalah hak atas kenyamanan, keamanan, dan keselamatan",
        "dalam mengkonsumsi barang dan/atau jasa.",
    ],
    "33857/UU Nomor 36 Tahun 1999.pdf": [
        "UNDANG-UNDANG REPUBLIK INDONESIA NOMOR 36 TAHUN 1999",
        "TENTANG TELEKOMUNIKASI",
        "Pasal 42",
        "Penyelenggara jasa telekomunikasi wajib merahasiakan informasi yang",
        "dikirim dan atau diterima oleh pelanggan jasa telekomunikasi.",
    ],
    "112816/PP Nomor 71 Tahun 2019.pdf": [
        "PERATURAN PEMERINTAH REPUBLIK INDONESIA NOMOR 71 TAHUN 2019",
        "TENTANG PENYELENGGARAAN SISTEM DAN TRANSAKSI ELEKTRONIK",
        "Pasal 14",
        "Penyelenggara Sistem Elektronik wajib melakukan pengamanan terhadap",
        "komponen Sistem Elektronik yang dikelolanya.",
    ],
}

ROBOTS_BYTES = (FIXTURES / "robots_2026-09-16.txt").read_bytes()


def recorded_fetch(*, robots: bytes = ROBOTS_BYTES, method: str = "curl_cffi"):
    """A fetch over the recorded Portal answers.

    `method` is the rung the fetch reports it ended on. It defaults to the
    impersonating rung, because that is what this host actually answers, and
    the Discovery record must show it.
    """
    calls: list[str] = []
    robots_calls: list[str] = []

    def fetch(url: str) -> FetchResult:
        if url.endswith("/robots.txt"):
            robots_calls.append(url)
            return FetchResult(url, url, 200, robots, "text/plain", "httpx")
        calls.append(url)
        for reference, lines in SEEDED_BODIES.items():
            if url == ID_DOWNLOAD.format(ref=_quoted(reference)):
                return FetchResult(
                    url, url, 200, text_layer_pdf(lines), "application/pdf", method
                )
        return FetchResult(url, url, 404, b"", "text/html", method)

    fetch.calls = calls
    fetch.robots_calls = robots_calls
    return fetch


def _quoted(reference: str) -> str:
    from urllib.parse import quote

    return quote(reference, safe="/")


@pytest.fixture()
def storage(tmp_path):
    store = Storage(tmp_path / "indonesia.db")
    store.apply_schema()
    yield store
    store.close()


class NullLimiter(RateLimiter):
    def __init__(self):
        super().__init__(0.0, clock=lambda: 0.0, sleep=lambda s: None)


# ---------------------------------------------------------------------------
# The Portal configuration
# ---------------------------------------------------------------------------


class TestThePortalConfiguration:
    def test_indonesia_whitelists_the_portal_that_answered_and_the_one_that_did_not(self):
        portal = load_portals(CONFIG_DIR)["ID"]
        assert portal.official_name == "Indonesia"
        assert portal.hosts[0] == HOST, "the first host is the one robots.txt binds us to"
        assert "peraturan.go.id" in portal.hosts
        assert portal.languages[0] == "Bahasa Indonesia"
        assert portal.strategy == "curl_cffi_ladder"
        assert portal.min_interval_seconds >= 5.0
        assert portal.live_test_pool is True
        assert portal.manual_only is False

    def test_the_politeness_floor_is_at_least_five_seconds_in_both_files(self):
        portal = load_portals(CONFIG_DIR)["ID"]
        seeds = load_crawl_seeds(CONFIG_DIR)["ID"]
        assert max(portal.min_interval_seconds, seeds.rate_limit_seconds) >= 5.0

    def test_the_portal_answered_robots_so_it_carries_no_grace_date(self):
        """RFC 9309's second half, the 30-day grace after an unreachable
        robots.txt, is for a Portal that refuses to publish its rules. This one
        answered 200 on the first request of the capture, so it must not claim
        the grace."""
        portal = load_portals(CONFIG_DIR)["ID"]
        assert portal.robots_unreachable_since is None

    def test_the_recorded_robots_reads_as_a_published_permissive_policy(self):
        """Through the one reader every lane now shares, not through a parser
        this test picked for itself."""
        fetch = recorded_fetch()
        reading = read_robots_policy(HOST, fetch)
        assert fetch.robots_calls == [ROBOTS_URL]
        assert reading.note is None, "nothing was waived; the Portal answered"
        assert reading.policy.allows(ID_DOWNLOAD.format(ref="224884/x.pdf"))
        assert not reading.policy.allows(f"https://{HOST}/Admin/Dashboard")

    def test_the_published_rules_allow_every_route_discovery_takes(self):
        policy = parse_robots(ROBOTS_BYTES.decode())
        assert policy.allows(f"https://{HOST}/Search?keywords=telekomunikasi&jenis=8")
        assert policy.allows(f"https://{HOST}/Details/229798/uu-no-27-tahun-2022")
        assert policy.allows(ID_DOWNLOAD.format(ref="224884/x.pdf"))
        assert not policy.allows(f"https://{HOST}/Admin/Dashboard")
        assert policy.crawl_delay is None, "the Portal publishes no crawl delay"


class TestTheSeedsCameOffThePortal:
    def test_every_seeded_download_reference_appears_in_a_recorded_listing(self):
        """The one check that keeps a seed from being invented: each seeded
        download reference must be present, verbatim, in one of the recorded
        search listings."""
        listings = [
            path.read_text(encoding="utf-8", errors="replace")
            for path in sorted(FIXTURES.glob("search_*.html"))
        ]
        assert len(listings) == 5
        seeds = load_crawl_seeds(CONFIG_DIR)["ID"]
        references = [ref for family in seeds.families.values() for ref in family.acts]
        assert len(references) >= 5
        for reference in references:
            path = f"/Download/{_quoted(reference)}"
            assert any(path in listing for listing in listings), (
                f"seed '{reference}' is in no recorded listing, so nothing shows"
                " it was ever on the Portal"
            )

    def test_the_seeds_cover_at_least_three_source_families(self):
        seeds = load_crawl_seeds(CONFIG_DIR)["ID"]
        assert len(seeds.families) >= 3
        assert all(family.acts for family in seeds.families.values())


# ---------------------------------------------------------------------------
# Discovery: seeds to Document URLs
# ---------------------------------------------------------------------------


class TestDiscoverIdResolvesSeedsWithoutAsking:
    def test_a_seed_becomes_a_download_url_on_the_whitelisted_host(self):
        from regcompass.contracts import CrawlFamily, CrawlSeedsEconomy

        seeds = CrawlSeedsEconomy(
            rate_limit_seconds=5.0,
            families={
                "data_protection": CrawlFamily(acts=["224884/UU Nomor 27 Tahun 2022.pdf"])
            },
        )
        targets, misses = discover_id(seeds, "ID")
        assert misses == []
        assert len(targets) == 1
        target = targets[0]
        assert target.url == (
            "https://peraturan.bpk.go.id/Download/224884/UU%20Nomor%2027%20Tahun%202022.pdf"
        )
        assert target.economy == "ID"
        assert target.source_family == "data_protection"
        assert target.filename_hint == "UU Nomor 27 Tahun 2022.pdf"

    def test_a_double_space_in_a_portal_file_name_survives_encoding(self):
        """The Portal really does serve `UU Nomor  19 Tahun 2016.pdf` with two
        spaces. Normalising it would 404."""
        from regcompass.contracts import CrawlFamily, CrawlSeedsEconomy

        seeds = CrawlSeedsEconomy(
            rate_limit_seconds=5.0,
            families={
                "electronic_transactions": CrawlFamily(
                    acts=["26676/UU Nomor  19 Tahun 2016.pdf"]
                )
            },
        )
        targets, _ = discover_id(seeds, "ID")
        assert targets[0].url.endswith("UU%20Nomor%20%2019%20Tahun%202016.pdf")

    def test_the_real_seeds_resolve_to_at_least_five_documents(self):
        seeds = load_crawl_seeds(CONFIG_DIR)["ID"]
        targets, misses = discover_id(seeds, "ID")
        assert misses == []
        assert len(targets) >= 5
        hosts = {t.url.split("/")[2] for t in targets}
        assert hosts == {HOST}
        assert len({t.source_family for t in targets}) >= 3

    def test_every_target_clears_the_portal_whitelist_gate(self):
        """Discovery drops any target whose host is off the whitelist before it
        is queued. Every Indonesian target must survive that, or the Corpus
        would silently come up short."""
        portal = load_portals(CONFIG_DIR)["ID"]
        targets, _ = discover_id(load_crawl_seeds(CONFIG_DIR)["ID"], "ID")
        allowed = set(portal.hosts)
        assert all(urlsplit(t.url).netloc in allowed for t in targets)

    def test_the_adapter_takes_the_same_keywords_as_every_other_adapter(self):
        """One signature across the registry: a plan can hand its adapter the
        session fetch and the robots policy without knowing which Economy it
        is serving."""
        seeds = load_crawl_seeds(CONFIG_DIR)["ID"]
        targets, misses = discover_id(
            seeds, "ID", get_json=None, limiter=None, fetch=None, robots=None
        )
        assert targets and misses == []


class TestTheLadderPlanRoutesByEconomy:
    def test_indonesia_gets_its_own_plan_and_singapore_keeps_its_own(self):
        """One strategy name, two Economies: the ladder's fetch is shared, the
        seed resolution is not. Singapore must be untouched by Indonesia."""
        from regcompass.contracts import CrawlFamily, CrawlSeedsEconomy

        plan = STRATEGIES["curl_cffi_ladder"]
        assert plan.discovery_needs_network is False

        indonesia = CrawlSeedsEconomy(
            rate_limit_seconds=5.0,
            families={"data_protection": CrawlFamily(acts=["224884/x.pdf"])},
        )
        targets, _ = plan.discover(indonesia, "ID")
        assert targets[0].url.startswith(f"https://{HOST}/Download/")

        singapore = CrawlSeedsEconomy(
            rate_limit_seconds=6.0,
            families={"data_protection": CrawlFamily(acts=["PDPA2012"])},
        )
        targets, _ = plan.discover(singapore, "SG")
        assert targets[0].url == "https://sso.agc.gov.sg/Act/PDPA2012?ViewType=Pdf"


# ---------------------------------------------------------------------------
# Discovery end to end, over recorded answers
# ---------------------------------------------------------------------------


def _discover(storage, tmp_path, fetch, **kwargs):
    kwargs.setdefault("limiter", NullLimiter())
    return discover_economy(
        "ID", storage, data_dir=tmp_path / "data", fetch=fetch, **kwargs
    )


class TestDiscoveryFillsTheIndonesianCorpus:
    def test_five_documents_across_three_families_land_in_bahasa_indonesia(
        self, storage, tmp_path
    ):
        fetch = recorded_fetch()
        report = _discover(storage, tmp_path, fetch)

        assert report.economy == "ID" and report.strategy == "curl_cffi_ladder"
        assert report.fetched >= 5
        assert report.documents_stored >= 5

        rows = storage.corpus_documents("ID")
        assert len(rows) >= 5
        portal = load_portals(CONFIG_DIR)["ID"]
        for row in rows:
            assert row["language"] == "Bahasa Indonesia"
            assert row["source_url"].split("/")[2] in portal.hosts
            assert row["fetched_at"] and row["full_text"]
            assert row["ocr_applied"] == 0, "a text-layer PDF must not go to OCR"

        families = {
            row["source_family"]
            for row in storage.manifest_rows(economy="ID", status="fetched")
        }
        assert len(families) >= 3

    def test_every_synthetic_document_says_in_its_own_text_that_it_is_one(
        self, storage, tmp_path
    ):
        """No Document in this test Corpus came off the Portal, and each one
        says so in the text a quote would be cut from, not only in a comment."""
        _discover(storage, tmp_path, recorded_fetch())
        rows = storage.corpus_documents("ID")
        assert rows
        assert all(STAND_IN_MARK in row["full_text"] for row in rows)

    def test_the_discovery_record_reports_the_count_the_strategy_and_the_rung(
        self, storage, tmp_path
    ):
        report = _discover(storage, tmp_path, recorded_fetch())

        assert report.escalated_to_impersonation is True
        record = storage.run_get(report.run_id)
        assert record["kind"] == "discovery" and record["economy"] == "ID"
        assert record["status"] == "completed" and record["engine"] is None
        assert record["documents_fetched"] == report.fetched
        assert record["details"]["strategy"] == "curl_cffi_ladder"
        # The escalation is on the STORED record, read back from the database,
        # so docs/PORTALS.md's "recorded on the Discovery record" is a fact
        # anyone can check afterwards rather than a flag that died with the
        # process.
        assert record["details"]["escalated_to_impersonation"] is True

    def test_the_manifest_records_which_rung_each_document_came_down(
        self, storage, tmp_path
    ):
        """`escalated_to_impersonation` on the Discovery record says THAT we
        escalated; the manifest says which rung answered for each Document, so
        the disclosure is checkable Document by Document and not a single flag
        anyone has to take on trust."""
        _discover(storage, tmp_path, recorded_fetch())
        rows = storage.manifest_rows(economy="ID", status="fetched")
        assert rows and {row["method"] for row in rows} == {"curl_cffi"}

    def test_a_discovery_that_never_escalated_says_so(self, storage, tmp_path):
        report = _discover(storage, tmp_path, recorded_fetch(method="httpx"))
        assert report.escalated_to_impersonation is False

    def test_the_documents_carry_the_portal_file_names(self, storage, tmp_path):
        _discover(storage, tmp_path, recorded_fetch())
        ids = {row["document_id"] for row in storage.corpus_documents("ID")}
        assert "doc_id_UU_Nomor_27_Tahun_2022" in ids
        assert "doc_id_UU_Nomor_36_Tahun_1999" in ids

    def test_discovery_asks_the_portal_for_nothing_but_robots_and_the_documents(
        self, storage, tmp_path
    ):
        """Seeding is by source family, so resolving a seed costs no request.
        The Portal sees robots.txt once and then one request per Document, and
        nothing else: no search, no listing walk, no detail page."""
        fetch = recorded_fetch()
        report = _discover(storage, tmp_path, fetch)
        assert fetch.robots_calls == [ROBOTS_URL]
        assert len(fetch.calls) == report.fetched
        assert all("/Download/" in url for url in fetch.calls)

    def test_a_second_discovery_asks_for_nothing_at_all(self, storage, tmp_path):
        _discover(storage, tmp_path, recorded_fetch())
        again = recorded_fetch()
        report = _discover(storage, tmp_path, again)
        assert report.fetched == 0
        assert report.skipped_existing == len(storage.corpus_documents("ID"))
        assert again.calls == [] and again.robots_calls == []


class TestPolitenessOnTheIndonesianPortal:
    def test_the_identified_agent_is_tried_first_and_the_rung_only_after_a_refusal(self):
        """The ladder's whole justification: the Portal's published rules
        permit us, its edge refuses us, and we only put on a browser's identity
        after being refused, never before."""
        asked: list[str] = []
        url = ID_DOWNLOAD.format(ref="224884/x.pdf")

        def identified(target: str) -> FetchResult:
            asked.append("httpx")
            return FetchResult(target, target, 403, b"Just a moment...", "text/html", "httpx")

        def impersonating(target: str) -> FetchResult:
            asked.append("curl_cffi")
            return FetchResult(target, target, 200, b"%PDF-1.4 ", "application/pdf", "curl_cffi")

        result = fetch_with_ladder(
            url, rungs=(("httpx", identified), ("curl_cffi", impersonating))
        )
        assert asked == ["httpx", "curl_cffi"], "the identified agent asks first"
        assert result.method == "curl_cffi"

    def test_a_portal_that_answers_us_is_never_escalated_past(self):
        url = ID_DOWNLOAD.format(ref="224884/x.pdf")
        asked: list[str] = []

        def identified(target: str) -> FetchResult:
            asked.append("httpx")
            return FetchResult(target, target, 200, b"%PDF-1.4 ", "application/pdf", "httpx")

        def impersonating(target: str) -> FetchResult:  # pragma: no cover - must not run
            asked.append("curl_cffi")
            raise AssertionError("the ladder escalated without a refusal")

        result = fetch_with_ladder(
            url, rungs=(("httpx", identified), ("curl_cffi", impersonating))
        )
        assert asked == ["httpx"] and result.method == "httpx"

    def test_robots_is_read_before_the_first_document_request(self, storage, tmp_path):
        fetch = recorded_fetch()
        _discover(storage, tmp_path, fetch)
        assert fetch.robots_calls == [ROBOTS_URL]

    def test_requests_are_spaced_at_or_above_the_five_second_floor(self, storage, tmp_path):
        now = {"t": 0.0}
        slept: list[float] = []

        def clock() -> float:
            return now["t"]

        def sleep(seconds: float) -> None:
            slept.append(seconds)
            now["t"] += seconds

        report = discover_economy(
            "ID", storage, data_dir=tmp_path / "data", fetch=recorded_fetch(),
            clock=clock, sleep=sleep,
        )
        assert report.min_interval_seconds >= 5.0
        assert slept and all(seconds >= 5.0 for seconds in slept)


# ---------------------------------------------------------------------------
# A Run over the Indonesian Corpus
# ---------------------------------------------------------------------------


class TestARunOverTheIndonesianCorpus:
    def test_the_fake_engine_produces_verified_mappings_for_pillar_six(
        self, storage, tmp_path
    ):
        _discover(storage, tmp_path, recorded_fetch())
        report = run_economy(
            storage, "ID", (6,), resolve_engine("fake"), data_dir=tmp_path / "data",
            completion_fn=fake_completion, embed_fn=fake_embed,
        )
        assert report.documents, "the Run must read the Corpus Discovery filled"
        assert report.n_passed > 0
        assert storage.load_mappings(economy="ID", verification_status="passed")

    def test_the_gate_runs_the_meaning_only_rule_on_bahasa_indonesia(
        self, storage, tmp_path
    ):
        """The keyword tier is English end to end (tokenizer, stopwords and
        Indicator vocabularies), so a Bahasa Indonesia Document must be
        shortlisted by meaning alone. Both halves are read back from the audit
        record: the method says the keyword index was never built, the decision
        says which rule ran."""
        _discover(storage, tmp_path, recorded_fetch())
        run_economy(
            storage, "ID", (6,), resolve_engine("fake"), data_dir=tmp_path / "data",
            completion_fn=fake_completion, embed_fn=fake_embed,
        )
        rows = list(
            storage.conn.execute(
                "SELECT method, decision FROM audit_log WHERE stage = 'm5_gate'"
            )
        )
        assert rows
        assert {row["method"] for row in rows} == {"bge-m3"}, (
            "bge-m3+bm25 would mean the English keyword tier was built on an"
            " Indonesian Document"
        )
        gated = [row for row in rows if "of 0 pairs" not in row["decision"]]
        assert gated, "at least one Document must have reached the Gate"
        assert all("mode=meaning_only" in row["decision"] for row in gated)


# ---------------------------------------------------------------------------
# The hosts that are not fetched, and the operator's way in
# ---------------------------------------------------------------------------


class TestOnBytesThePortalActuallyServed:
    """One test that uses no synthetic Document at all.

    The Personal Data Protection Act's detail page is real recorded Portal
    output (request 8 of the capture). It goes into the Corpus through the same
    write path Discovery uses, its Bahasa Indonesia text comes out of the real
    bytes, and that text is embedded and gated. What the test supplies is the
    section framing: a detail page is metadata and an abstract, with no
    numbered articles, so the chunker finds no section in it. The Portal's own
    words are what reaches the Gate."""

    DETAIL_URL = "https://peraturan.bpk.go.id/Details/229798/uu-no-27-tahun-2022"
    DETAIL_PAGE = FIXTURES / "details_uu-no-27-tahun-2022_2026-09-16.html"

    def test_the_recorded_detail_page_ingests_as_a_bahasa_indonesia_document(
        self, storage, tmp_path
    ):
        added = add_document(
            storage, tmp_path / "data", "ID", self.DETAIL_PAGE.read_bytes(),
            source_url=self.DETAIL_URL, filename_hint="uu-no-27-tahun-2022.html",
        )
        assert added.language == "Bahasa Indonesia"
        assert added.ocr_applied is False, "a text page needs no OCR"
        text = storage.corpus_documents("ID")[0]["full_text"]
        assert STAND_IN_MARK not in text, "this Document is real Portal output"
        assert "Pelindungan Data Pribadi" in text
        assert "Bahasa Indonesia" in text, "the Portal states the Language itself"

    def test_the_portals_own_words_reach_the_gate_with_candidates(
        self, storage, tmp_path
    ):
        from regcompass.contracts import Chunk
        from regcompass.gate import gate_document

        added = add_document(
            storage, tmp_path / "data", "ID", self.DETAIL_PAGE.read_bytes(),
            source_url=self.DETAIL_URL, filename_hint="uu-no-27-tahun-2022.html",
        )
        text = storage.corpus_documents("ID")[0]["full_text"]
        start = text.index("UU ini mengatur")
        end = text.index("METADATA")
        chunk = Chunk(
            chunk_id="ch_id_detail_1", document_id=added.document_id,
            char_start=start, char_end=end, text=text[start:end],
            section_label="Materi Pokok Peraturan", chunk_kind="section",
        )
        gated, report = gate_document(
            [chunk], embed_fn=fake_embed, pillars=(6,), keyword_tier=False
        )
        assert report.n_candidates == 1, "the Gate must have something to score"
        assert report.gate_mode == "meaning_only"
        assert gated, "every candidate and Indicator pair is emitted, never dropped"
        assert any(row.gate_decision == "passed" for row in gated)
        assert "pemrosesan data pribadi" in chunk.text


class TestTheOtherWhitelistedHost:
    def test_a_document_from_the_unreachable_portal_still_clears_the_host_check(self):
        """peraturan.go.id never answered from here, so Discovery does not point
        at it. It stays on the whitelist because it IS an official Indonesian
        Portal: a Document a reviewer adds from it must pass the host check the
        Evidence Export applies."""
        portal = load_portals(CONFIG_DIR)["ID"]
        host = check_host_allowed(
            portal, "https://peraturan.go.id/details/1234/uu-no-27-tahun-2022"
        )
        assert host == "peraturan.go.id"

    def test_an_aggregator_is_still_refused(self):
        portal = load_portals(CONFIG_DIR)["ID"]
        with pytest.raises(HostNotAllowedError):
            check_host_allowed(portal, "https://hukumonline.com/pusatdata/uu-27-2022")


class TestTheManualFallback:
    """The veto path. If the escalation is not wanted on this Portal, Indonesia
    becomes a manual Economy with one line of configuration, and the operator's
    add still works."""

    @pytest.fixture()
    def manual_config(self, tmp_path):
        config_dir = tmp_path / "config"
        shutil.copytree(CONFIG_DIR, config_dir)
        portals_file = config_dir / "portals.yaml"
        text = portals_file.read_text(encoding="utf-8")
        marker = "    strategy: curl_cffi_ladder\n    min_interval_seconds: 5"
        assert marker in text, "the Indonesian block moved; fix this fixture"
        portals_file.write_text(
            text.replace(marker, "    strategy: manual\n    min_interval_seconds: 5"),
            encoding="utf-8",
        )
        return config_dir

    def test_discovery_is_refused_with_the_operator_message(
        self, storage, tmp_path, manual_config
    ):
        with pytest.raises(ManualEconomyError) as exc:
            discover_economy(
                "ID", storage, data_dir=tmp_path / "data", config_dir=manual_config
            )
        message = str(exc.value)
        assert "Indonesia" in message and "Add document" in message
        assert storage.runs_list(kind="discovery") == []

    def test_the_operator_can_still_add_a_document_by_hand(
        self, storage, tmp_path, manual_config
    ):
        added = add_document(
            storage,
            tmp_path / "data",
            "ID",
            text_layer_pdf(SEEDED_BODIES["224884/UU Nomor 27 Tahun 2022.pdf"]),
            source_url=ID_DOWNLOAD.format(ref=_quoted("224884/UU Nomor 27 Tahun 2022.pdf")),
            filename_hint="UU Nomor 27 Tahun 2022.pdf",
            config_dir=manual_config,
        )
        assert added.economy == "ID"
        assert added.language == "Bahasa Indonesia"
        assert added.ocr_applied is False
        assert storage.corpus_documents("ID")
