"""Thailand: the Council of State's law library, replayed from recorded bytes.

Every test here reads the answers `searchlaw.ocs.go.th` gave on 22 Sep 2026,
committed under tests/fixtures/portals/th/ with their requests and their count
(tests/fixtures/portals/th/README.md). Nothing in this file touches the network.

The Thailand lane is NOT wired for Discovery, and these tests are the reason,
kept as bytes rather than as a claim:

  * the host printed in our own plan is dead and the live one's certificate
    verifies cleanly, so the planned certificate pinning was never needed and
    no source file disables TLS verification;
  * `/robots.txt` answers 404, which publishes no rules;
  * the Portal's BROWSE lane answers us and names the statutes we want;
  * every one of its SEARCH services fails, and both of its LAW-TEXT services
    reject the identifier the browse lane gives, so no Thai statute is
    reachable and there is nothing to discover.

The last two are pinned deliberately. The day the Portal answers differently,
these tests fail and say that Thailand is worth revisiting.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from regcompass.config import load_crawl_seeds, load_portals

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/portals/th"
SRC = ROOT / "src"

HOST = "searchlaw.ocs.go.th"
BASE = f"https://{HOST}"
# The application's own document route. A Source URL for Thailand is one of
# these: an address a person can open and see that law.
DOC_URL = f"{BASE}/council-of-state/#/public/doc/598"
OFF_HOST_URL = "https://www.krisdika.go.th/some-act.pdf"


def recorded(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def answer(name: str) -> dict:
    """One recorded API answer, parsed. Every one of them is HTTP 200 and says
    what happened in `respHeader.errorCode`, which is this API's convention."""
    return json.loads(recorded(name).decode("utf-8"))


def error_code(name: str) -> str:
    return answer(name)["respHeader"]["errorCode"]


# ---------------------------------------------------------------------------
# TLS: the planned pinning was not needed, and verification stays on
# ---------------------------------------------------------------------------


class TestVerificationIsNeverDisabled:
    """The plan for Thailand was to pin a broken certificate. The live host's
    certificate verifies cleanly against certifi, so nothing was pinned and
    nothing was loosened. This test is the guard on that, for every Portal at
    once: a lane that turns verification off would ship silently otherwise."""

    def test_no_source_file_turns_off_certificate_verification(self):
        offenders: list[str] = []
        pattern = re.compile(r"verify\s*=\s*False")
        for path in sorted(SRC.rglob("*.py")):
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            ):
                if pattern.search(line):
                    offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
        assert offenders == [], (
            "TLS verification is disabled in the source; a Portal's identity is"
            " what makes its bytes evidence:\n" + "\n".join(offenders)
        )


# ---------------------------------------------------------------------------
# what the Portal publishes
# ---------------------------------------------------------------------------


class TestRobotsIsAFourOhFour:
    def test_the_404_page_publishes_no_rules(self):
        """RFC 9309 reads a 4xx as no rules published, which is NOT the 5xx
        "unavailable" that stops India. Nothing is disallowed and nothing is
        permitted: our own floor is the whole promise."""
        from regcompass.crawl import RobotsPolicy, parse_robots

        body = recorded("robots_404_2026-09-22.html").decode("utf-8")
        assert "<html" in body.lower(), "the host answers with an HTML error page"
        assert parse_robots(body) == RobotsPolicy()
        assert parse_robots(body).allows(DOC_URL)

    def test_the_one_robots_door_reads_the_404_as_no_rules(self):
        """Read through the door every lane uses, with the status the host
        actually gave, so the add-by-URL lane and Discovery agree."""
        from regcompass.crawl import FetchResult, RobotsPolicy, read_robots_policy

        def fetch(url: str) -> FetchResult:
            assert url == f"{BASE}/robots.txt", f"unexpected request for {url}"
            return FetchResult(
                url, url, 404, recorded("robots_404_2026-09-22.html"), "text/html", "httpx"
            )

        reading = read_robots_policy(HOST, fetch)
        assert reading.policy == RobotsPolicy()
        assert reading.unavailable_status is None
        assert reading.note is None, "no grace rule was applied; the host answered"

    def test_a_published_disallow_would_still_bind_this_host(self):
        """No rules today is not a licence forever."""
        from regcompass.crawl import parse_robots

        policy = parse_robots("User-agent: *\nDisallow: /council-of-state/\nCrawl-delay: 5\n")
        assert policy.crawl_delay == 5.0
        assert not policy.allows(DOC_URL)


class TestTheApplicationShell:
    def test_the_law_library_is_a_javascript_application(self):
        """Why there is no HTML lane here at all: the page a person opens is a
        1.3 KB shell, and every law it shows arrives through the JSON API."""
        shell = recorded("app_shell_2026-09-22.html").decode("utf-8")
        assert "<app-root>" in shell
        assert re.search(r"main\.[0-9a-f]+\.js", shell), "an Angular bundle"
        assert "<a " not in shell, "no law URL is reachable without the API"


# ---------------------------------------------------------------------------
# the browse lane: it answers, and it names the statutes we want
# ---------------------------------------------------------------------------


class TestTheBrowseLaneAnswers:
    def test_the_subject_tags_come_back(self):
        body = answer("browse_tags_1B_2026-09-22.json")
        assert body["respHeader"]["errorCode"] == "SUCCESS"
        assert body["respHeader"]["serviceName"] == "getPublicTags"
        tags = body["respBody"]["data"]
        assert len(tags) == 43
        by_value = {t["value"]: t["label"] for t in tags}
        # The tag the digital-trade statutes sit under.
        assert by_value["26"] == "วิทยาศาสตร์ และเทคโนโลยี"
        assert all(t["totalItms"] >= 0 for t in tags)

    def test_the_acts_under_the_technology_tag_are_the_ones_we_want(self):
        """The evidence that this Portal holds the right law, and that the
        browse lane reaches it. What it does NOT give is a document address:
        `indexId` is not the `timelineId` a document URL is built from."""
        body = answer("browse_folders_tag26_2026-09-22.json")
        assert body["respHeader"]["errorCode"] == "SUCCESS"
        folders = body["respBody"]["data"]
        assert len(folders) == 23
        by_index = {f["indexId"]: f["indexName"] for f in folders}
        assert "ธุรกรรมทางอิเล็กทรอนิกส์" in by_index[598], "Electronic Transactions Act"
        assert "ไซเบอร์" in by_index[199], "Cybersecurity Act"
        assert "คอมพิวเตอร์" in by_index[499], "Computer-related Crime Act"
        assert "ดิจิทัลเพื่อเศรษฐกิจและสังคม" in by_index[188], "Digital Economy Act"
        assert "ดิจิทัล" in by_index[627], "Digital Government Act"
        assert all("timelineId" not in f for f in folders)

    def test_the_years_service_fails_even_with_the_expected_req_by(self):
        """The application sends the literal string `unknow` for an anonymous
        visitor. The recorded answer carries that value back and still fails
        with the generic processing error, so getPublicYears is a third browse
        service that does not answer (the earlier empty-reqBy refusal,
        PYC_INVALID_FIELD_REQUIED, was not kept; see the fixture README)."""
        header = answer("browse_years_error_2026-09-22.json")["respHeader"]
        assert header["errorCode"] == "PYC_9998"
        assert header["reqBy"] == "unknow", "the retry that got past the field check"


# ---------------------------------------------------------------------------
# why Discovery is not wired: the two lanes that would build a Document
# ---------------------------------------------------------------------------


class TestTheSearchLaneFails:
    """All three search services answer the same 6013 code under their own
    service prefix, for every body tried. The bodies are the application's
    own, and the browse lane answered SUCCESS to the same envelope on the same
    connection minutes earlier, so this is the Portal, not our client."""

    @pytest.mark.parametrize(
        "fixture, service, code",
        [
            ("search_law_error_2026-09-22.json", "searchPublicLaw", "PSL_6013"),
            ("search_by_tag_error_2026-09-22.json", "searchPublicByTag", "PST_6013"),
            ("suggest_error_2026-09-22.json", "suggest", "NON_6013"),
        ],
    )
    def test_every_search_service_answers_the_same_failure(self, fixture, service, code):
        header = answer(fixture)["respHeader"]
        assert header["serviceName"] == service
        assert header["errorCode"] == code
        assert "respBody" not in answer(fixture), "no result set came back"

    def test_no_search_answer_carries_a_document_identifier(self):
        """The whole blocker in one assertion: a document address needs a
        timelineId, a search result is the only public thing that issues one,
        and no recorded search answer carries a single result."""
        for fixture in (
            "search_law_error_2026-09-22.json",
            "search_by_tag_error_2026-09-22.json",
            "suggest_error_2026-09-22.json",
        ):
            assert answer(fixture).get("respBody") is None


class TestTheLawTextLaneFails:
    """Both services that return the text of a law answer a cipher error for
    the bodies the application itself sends. The application performs no
    client-side encryption on either call, so there is nothing on our side to
    add, and retrying with the application's own UUID v4 session shape changed
    nothing (tests/fixtures/portals/th/README.md)."""

    @pytest.mark.parametrize(
        "fixture, service, code",
        [
            ("law_doc_cipher_error_2026-09-22.json", "getPublicLawDoc",
             "PDL_CIPHER_EXCEPTION"),
            ("print_html_cipher_error_2026-09-22.json", "printPublicLawAsHtml",
             "PPH_CIPHER_EXCEPTION"),
        ],
    )
    def test_the_document_services_reject_the_browse_lanes_identifier(
        self, fixture, service, code
    ):
        header = answer(fixture)["respHeader"]
        assert header["serviceName"] == service
        assert header["errorCode"] == code
        assert answer(fixture).get("respBody") is None


# ---------------------------------------------------------------------------
# configuration: the host is whitelisted, Discovery is not wired
# ---------------------------------------------------------------------------


class TestTheThailandPortalIsConfigured:
    def test_the_portal_declares_the_live_host_the_language_and_the_floor(self):
        portal = load_portals()["TH"]
        assert portal.official_name == "Thailand"
        assert portal.hosts[0] == HOST, "the live library, not the dead placeholder"
        assert not any(
            h.endswith("law.go.th") or "ratchakitcha" in h for h in portal.hosts
        ), "never requested"
        assert portal.languages[0] == "Thai"
        assert portal.min_interval_seconds == 2.0
        assert portal.live_test_pool is True

    def test_discovery_is_not_wired_but_this_is_not_a_permanent_refusal(self):
        """`manual` here means no strategy has been wired, which is the nine
        Economies' position. It is NOT `manual_only`, which says the Portal's
        own rules forbid automated collection forever."""
        from regcompass.contracts import MANUAL_STRATEGY

        portal = load_portals()["TH"]
        assert portal.strategy == MANUAL_STRATEGY
        assert portal.manual_only is False
        assert portal.prepared is False, "no Corpus has been fetched or checked"

    def test_the_dead_placeholder_host_is_not_whitelisted(self):
        """www.krisdika.go.th serves a self-signed certificate and 404s every
        path. Whitelisting it would let a Document in from a host we cannot
        verify the identity of."""
        assert "www.krisdika.go.th" not in load_portals()["TH"].hosts

    def test_the_excluded_hosts_are_on_no_portals_whitelist(self):
        """The Royal Gazette blocks bots and www.law.go.th answers 403; neither
        was requested and neither may become a Source URL without the
        reviewer's explicit outside-the-Portal disclosure. They are NAMED in
        the Thailand notes, which is where an exclusion belongs, and that is
        the difference this test keeps: documented, never whitelisted."""
        whitelisted = {host for p in load_portals().values() for host in p.hosts}
        for host in ("ratchakitcha.soc.go.th", "www.law.go.th", "www.krisdika.go.th"):
            assert host not in whitelisted
        assert "www.law.go.th" in (load_portals()["TH"].notes or "")

    def test_thailand_carries_no_crawler_seeds(self):
        """A seed that cannot resolve to a Document would be a claim the
        Portal does not honour. Thailand's only seeds are fixed MDES
        addresses, each fetched once when it was seeded; no search or act
        code is seeded for a Portal with no crawler."""
        families = load_crawl_seeds()["TH"].families.values()
        assert all(f.urls and not f.acts and not f.queries for f in families)
        assert all(f.urls[0].startswith("https://mdes.go.th/law/detail/") for f in families)


# ---------------------------------------------------------------------------
# the lane Thailand actually has: a reviewer adds a Document by URL
# ---------------------------------------------------------------------------


class TestAddingAThaiDocumentByUrl:
    """The host is whitelisted precisely so this works without the "official
    source outside the configured Portal" tick. The bytes are a committed
    born-digital PDF standing in for the statute: what is under test is the
    lane's treatment of a Thailand Source URL, not the Portal's text, which it
    will not serve."""

    @pytest.fixture()
    def storage(self, tmp_path):
        from regcompass.storage import Storage

        st = Storage(tmp_path / "th.db")
        st.apply_schema()
        yield st
        st.close()

    def _fetch(self):
        from regcompass.crawl import FetchResult

        pdf = (
            ROOT / "tests/fixtures/sample_legislation/born_digital"
            / "PERSONAL DATA PROTECTION ACT 2010.pdf"
        ).read_bytes()
        calls: list[str] = []
        robots_calls: list[str] = []

        def fetch(url: str) -> FetchResult:
            if url.endswith("/robots.txt"):
                robots_calls.append(url)
                return FetchResult(
                    url, url, 404,
                    recorded("robots_404_2026-09-22.html"), "text/html", "httpx",
                )
            calls.append(url)
            return FetchResult(url, url, 200, pdf, "application/pdf", "httpx")

        fetch.calls = calls  # type: ignore[attr-defined]
        fetch.robots_calls = robots_calls  # type: ignore[attr-defined]
        return fetch

    class _Limiter:
        min_interval = 0.0

        def __init__(self):
            self.waited: list[str] = []

        def wait(self, url: str) -> None:
            self.waited.append(url)

    def test_a_document_address_on_the_official_host_needs_no_override(
        self, storage, tmp_path
    ):
        from regcompass.corpus import add_document_from_url

        fetch = self._fetch()
        limiter = self._Limiter()
        added = add_document_from_url(
            storage, tmp_path / "data", "TH", DOC_URL,
            language="Thai", fetch=fetch, limiter=limiter,
        )
        assert fetch.robots_calls == [f"{BASE}/robots.txt"], "the rules are read first"
        assert fetch.calls == [DOC_URL]
        assert limiter.waited == [DOC_URL], "the floor applies to a single request too"
        assert added.document_id
        rows = storage.corpus_documents("TH")
        assert len(rows) == 1
        assert rows[0]["source_url"] == DOC_URL
        assert rows[0]["language"] == "Thai"

    def test_the_dead_placeholder_host_is_refused_without_the_tick(
        self, storage, tmp_path
    ):
        from regcompass.corpus import HostNotAllowedError, add_document_from_url

        fetch = self._fetch()
        with pytest.raises(HostNotAllowedError) as exc:
            add_document_from_url(
                storage, tmp_path / "data", "TH", OFF_HOST_URL,
                language="Thai", fetch=fetch, limiter=self._Limiter(),
            )
        assert "krisdika" in str(exc.value)
        assert fetch.calls == [], "nothing is requested from an off-whitelist host"
