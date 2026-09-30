"""Discovery by Economy + Pillar + Indicators.

At the live hour the operator names an Economy, a Pillar and two Indicators.
Discovery then fetches the laws the 2025 baseline cites for those Indicators,
from official hosts only, adds what the Portal crawler finds for that Pillar,
stops at a cap, and lists every baseline law it did not fetch with a reason.

Offline throughout: a recorded fetch answers every request, and a small
baseline law list written per test stands in for config/baseline_laws.json.
"""

from __future__ import annotations

import json
import time

import pytest

pytest.importorskip("httpx", reason="Discovery tests need the `live` extra (httpx)")

from fastapi.testclient import TestClient  # noqa: E402

from regcompass.config import load_crawl_seeds  # noqa: E402
from regcompass.contracts import CrawlFamily, CrawlSeedsEconomy  # noqa: E402
from regcompass.discovery import discover_economy  # noqa: E402
from regcompass.server import create_app  # noqa: E402
from regcompass.storage import Storage  # noqa: E402

from test_discovery import (  # noqa: E402
    PDPA_URL,
    TA_URL,
    NullLimiter,
    recorded_fetch,
)

SSO = "https://sso.agc.gov.sg"
ETA_URL = f"{SSO}/Act/ETA2010?ViewType=Pdf"
COMP_URL = f"{SSO}/Act/CA2004?ViewType=Pdf"
NEWER_URL = f"{SSO}/Act/NA2020?ViewType=Pdf"
GONE_URL = f"{SSO}/Act/GONE2015?ViewType=Pdf"
FIRM_URL = "https://www.examplelawfirm.com/copies/sg-act.pdf"


def _page(title: str) -> bytes:
    body = (
        f"<html><body><h1>{title}</h1>"
        "<p>1. This Act may be cited as the Act named above and comes into"
        " operation on a date the Minister appoints by notification.</p>"
        "<p>2. A person who provides a service under this Act shall keep the"
        " records this Act requires and produce them on request.</p>"
        "</body></html>"
    )
    return body.encode()


BODIES = {
    ETA_URL: _page("Electronic Transactions Act 2010"),
    COMP_URL: _page("Competition Act 2004"),
    NEWER_URL: _page("Newer Act 2020"),
    TA_URL: _page("Telecommunications Act 1999"),
    PDPA_URL: _page("Personal Data Protection Act 2012"),
}


def _law(law, indicators, urls, pairing="single", year=None):
    return {
        "law": law, "law_key": law.lower(), "year": year,
        "indicators": indicators, "urls": urls, "url_pairing": pairing,
    }


SG_LAWS = [
    _law("Unrelated Act", ["6.1"], [f"{SSO}/Act/UN1999?ViewType=Pdf"], year=1999),
    _law("Competition Act 2004", ["4.3"], [COMP_URL], year=2004),
    _law("No Address Act", ["4.3"], [], pairing="none", year=2019),
    _law("Newer Act 2020", ["4.2"], [NEWER_URL], pairing="positional", year=2020),
    _law("Firm Copy Act", ["4.2"], [FIRM_URL], year=2018),
    _law("Gone Act 2015", ["4.2"], [GONE_URL], year=2015),
    _law("Electronic Transactions Act 2010", ["4.2", "4.3", "6.1"], [FIRM_URL, ETA_URL], year=2010),
]


def _write_baseline(tmp_path, economies=None):
    path = tmp_path / "baseline_laws.json"
    path.write_text(
        json.dumps(
            {
                "_comment": "test fixture",
                "sources": {},
                "economies": economies
                if economies is not None
                else {"SG": {"name": "Singapore", "laws": SG_LAWS}},
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


UNTAGGED_SG_SEEDS = CrawlSeedsEconomy(
    rate_limit_seconds=0.01,
    families={
        "telecommunications": CrawlFamily(acts=["TA1999"]),
        "data_protection": CrawlFamily(acts=["PDPA2012"]),
    },
)

TAGGED_SG_SEEDS = CrawlSeedsEconomy(
    rate_limit_seconds=0.01,
    families={
        "telecommunications": CrawlFamily(acts=["TA1999"], pillars=[4, 6]),
        "data_protection": CrawlFamily(acts=["PDPA2012"]),
    },
)


# Seeds with no fixed official addresses, so an Economy without a crawler
# (Thailand) runs on the test's own baseline alone, not the committed seeds.
NO_URL_SEEDS = UNTAGGED_SG_SEEDS


@pytest.fixture()
def storage(tmp_path):
    s = Storage(tmp_path / "discovery.db")
    s.apply_schema()
    yield s
    s.close()


def _discover(storage, tmp_path, fetch, economy="SG", **kwargs):
    kwargs.setdefault("seeds", UNTAGGED_SG_SEEDS)
    kwargs.setdefault("limiter", NullLimiter())
    if "baseline_dir" not in kwargs:
        kwargs["baseline_dir"] = _write_baseline(tmp_path)
    return discover_economy(
        economy, storage, data_dir=tmp_path / "data", fetch=fetch, **kwargs
    )


def _by_law(report):
    return {s["law"]: s for s in report.baseline_skipped}


class TestTheBaselineStage:
    def test_a_pillar_4_draw_fetches_the_baseline_laws_in_rank_order(
        self, storage, tmp_path
    ):
        fetch = recorded_fetch(BODIES)
        report = _discover(
            storage, tmp_path, fetch, pillar=4, indicators=["4.2", "4.3"]
        )

        # Two drawn Indicators cited first; then single-law pairing before
        # positional, newest first. The law firm copy is never asked for, and
        # the Pillar 6 law is not part of this draw.
        assert fetch.calls == [ETA_URL, GONE_URL, COMP_URL, NEWER_URL]
        assert report.fetched == 3 and report.documents_stored == 3
        assert [(d["url"], d["found_by"]) for d in report.found_by] == [
            (ETA_URL, "baseline 4.2, 4.3"),
            (COMP_URL, "baseline 4.3"),
            (NEWER_URL, "baseline 4.2"),
        ]
        rows = {r["source_url"]: r for r in storage.corpus_documents("SG")}
        assert set(rows) == {ETA_URL, COMP_URL, NEWER_URL}
        assert rows[ETA_URL]["title"] == "Electronic Transactions Act 2010"
        assert rows[ETA_URL]["source_kind"] == "discovery"

    def test_unofficial_hosts_missing_urls_and_dead_pages_are_listed_with_reasons(
        self, storage, tmp_path
    ):
        report = _discover(
            storage, tmp_path, recorded_fetch(BODIES), pillar=4,
            indicators=["4.2", "4.3"],
        )
        skipped = _by_law(report)
        assert set(skipped) == {"Firm Copy Act", "Gone Act 2015", "No Address Act"}
        assert skipped["Firm Copy Act"]["code"] == "not_allowed_host"
        assert "www.examplelawfirm.com" in skipped["Firm Copy Act"]["reason"]
        assert skipped["Gone Act 2015"]["code"] == "unreachable"
        assert "404" in skipped["Gone Act 2015"]["reason"]
        assert skipped["No Address Act"]["code"] == "no_url"
        assert skipped["Firm Copy Act"]["indicators"] == ["4.2"]

    def test_the_cap_stops_discovery_and_the_rest_are_over_the_cap(
        self, storage, tmp_path
    ):
        fetch = recorded_fetch(BODIES)
        report = _discover(
            storage, tmp_path, fetch, pillar=4, indicators=["4.2", "4.3"],
            max_documents=2,
        )
        assert fetch.calls == [ETA_URL, GONE_URL, COMP_URL]
        assert report.fetched == 2
        skipped = _by_law(report)
        assert skipped["Newer Act 2020"]["code"] == "over_cap"
        # A reason that would stand whatever the cap keeps its own words.
        assert skipped["No Address Act"]["code"] == "no_url"
        assert skipped["Firm Copy Act"]["code"] == "not_allowed_host"

    def test_no_indicators_means_every_indicator_of_the_pillar(self, storage, tmp_path):
        fetch = recorded_fetch(BODIES)
        _discover(storage, tmp_path, fetch, pillar=4)
        assert fetch.calls == [ETA_URL, GONE_URL, COMP_URL, NEWER_URL]

    def test_a_stored_law_is_not_fetched_again_and_the_report_says_so(
        self, storage, tmp_path
    ):
        _discover(storage, tmp_path, recorded_fetch(BODIES), pillar=4, indicators=["4.3"])
        fetch = recorded_fetch(BODIES)
        report = _discover(storage, tmp_path, fetch, pillar=4, indicators=["4.3"])
        assert fetch.calls == []
        assert report.fetched == 0
        assert {d["url"]: d["status"] for d in report.found_by} == {
            ETA_URL: "already in the Corpus",
            COMP_URL: "already in the Corpus",
        }

    def test_the_discovery_record_carries_pillar_indicators_and_found_by(
        self, storage, tmp_path
    ):
        report = _discover(
            storage, tmp_path, recorded_fetch(BODIES), pillar=4,
            indicators=["4.2", "4.3"],
        )
        record = storage.run_get(report.run_id)
        assert record["pillars"] == [4]
        assert record["indicators"] == ["4.2", "4.3"]
        assert record["documents_fetched"] == 3
        details = record["details"]
        assert details["pillar"] == 4 and details["indicators"] == ["4.2", "4.3"]
        assert [d["found_by"] for d in details["found_by"]] == [
            "baseline 4.2, 4.3", "baseline 4.3", "baseline 4.2",
        ]
        assert all(d["document_id"] for d in details["found_by"])
        assert {s["code"] for s in details["baseline_skipped"]} == {
            "not_allowed_host", "unreachable", "no_url",
        }

    def test_an_economy_without_a_crawler_still_discovers_from_the_baseline(
        self, storage, tmp_path
    ):
        cac = "https://www.cac.gov.cn/2021-08/20/c_1631050028355286.htm"
        path = _write_baseline(
            tmp_path,
            {"CN": {"name": "China", "laws": [
                _law("个人信息保护法", ["7.1"], [cac], year=2021),
            ]}},
        )
        fetch = recorded_fetch({cac: _page("中华人民共和国个人信息保护法 " * 20)})
        report = discover_economy(
            "CN", storage, data_dir=tmp_path / "data", fetch=fetch,
            limiter=NullLimiter(), baseline_dir=path, pillar=7,
            indicators=["7.1"],
        )
        assert fetch.calls == [cac]
        assert report.found_by[0]["found_by"] == "baseline 7.1"
        assert any("no Portal crawler" in n for n in report.notes)

    def test_an_economy_with_no_baseline_says_so(self, storage, tmp_path):
        path = _write_baseline(tmp_path, {})
        report = _discover(
            storage, tmp_path, recorded_fetch(BODIES), pillar=4,
            baseline_dir=path,
        )
        assert report.fetched == 0
        assert any("no 2025 baseline" in n for n in report.notes)


class TestAPositionalLinkMustBeThatLaw:
    """A positional URL was paired with its law by position in a multi-law
    baseline row, so it can point at another law. What it fetches is kept only
    when the text plausibly is that law."""

    CYBER_URL = f"{SSO}/Act/CSA2018?ViewType=Pdf"

    def _baseline(self, tmp_path, urls):
        return _write_baseline(
            tmp_path,
            {"SG": {"name": "Singapore", "laws": [
                _law("Cybersecurity Act 2018", ["4.2"], urls, pairing="positional", year=2018),
            ]}},
        )

    def test_a_link_to_a_different_law_is_discarded_and_the_next_is_tried(
        self, storage, tmp_path
    ):
        fetch = recorded_fetch({
            COMP_URL: _page("Competition Act 2004"),
            self.CYBER_URL: _page("Cybersecurity Act 2018"),
        })
        report = _discover(
            storage, tmp_path, fetch, pillar=4, indicators=["4.2"],
            baseline_dir=self._baseline(tmp_path, [COMP_URL, self.CYBER_URL]),
        )
        assert fetch.calls == [COMP_URL, self.CYBER_URL]
        assert [d["url"] for d in report.found_by] == [self.CYBER_URL]
        assert report.fetched == 1 and report.documents_stored == 1
        assert {r["source_url"] for r in storage.corpus_documents("SG")} == {self.CYBER_URL}
        assert report.baseline_skipped == []

    def test_only_a_wrong_link_skips_the_law_with_that_reason(self, storage, tmp_path):
        report = _discover(
            storage, tmp_path, recorded_fetch({COMP_URL: _page("Competition Act 2004")}),
            pillar=4, indicators=["4.2"],
            baseline_dir=self._baseline(tmp_path, [COMP_URL]),
        )
        (skip,) = report.baseline_skipped
        assert skip["code"] == "wrong_law"
        assert "different law" in skip["reason"]
        assert report.fetched == 0
        assert storage.corpus_documents("SG") == []

    def test_a_non_latin_title_matches_its_own_text(self):
        from regcompass.discovery import plausibly_the_law

        assert plausibly_the_law(
            "中华人民共和国个人信息保护法", "中华人民共和国个人信息保护法 第一条 为了保护个人信息权益"
        )
        assert not plausibly_the_law(
            "中华人民共和国个人信息保护法", "中华人民共和国反垄断法 第一条 为了预防和制止垄断行为"
        )
        assert plausibly_the_law("Law No. 27 of 2022", "UNDANG-UNDANG NOMOR 27 TAHUN 2022")
        assert plausibly_the_law("พระราชบัญญัติ ๒๕๖๒", "พระราชบัญญัติคุ้มครองข้อมูล พ.ศ. 2562")


class TestTheCrawlerStage:
    def test_seeds_are_filtered_by_pillar_and_untagged_means_6_and_7(
        self, storage, tmp_path
    ):
        absent = tmp_path / "missing"
        fetch = recorded_fetch(BODIES)
        report = _discover(
            storage, tmp_path, fetch, pillar=4, seeds=TAGGED_SG_SEEDS,
            baseline_dir=absent,
        )
        assert fetch.calls == [TA_URL]
        assert [d["found_by"] for d in report.found_by] == ["portal crawler"]
        assert any("no baseline law list" in n for n in report.notes)

        fetch6 = recorded_fetch(BODIES)
        _discover(
            storage, tmp_path, fetch6, pillar=6, seeds=TAGGED_SG_SEEDS,
            baseline_dir=absent,
        )
        # TA1999 is already stored; the untagged family counts for Pillar 6.
        assert fetch6.calls == [PDPA_URL]

    def test_no_seed_for_the_pillar_runs_no_crawler(self, storage, tmp_path):
        fetch = recorded_fetch(BODIES)
        report = _discover(
            storage, tmp_path, fetch, pillar=12,
            baseline_dir=tmp_path / "absent",
        )
        assert fetch.calls == [] and report.fetched == 0
        assert any("Pillar 12" in n for n in report.notes)

    def test_baseline_first_then_crawler_under_one_cap(self, storage, tmp_path):
        fetch = recorded_fetch(BODIES)
        report = _discover(
            storage, tmp_path, fetch, pillar=4, indicators=["4.3"],
            seeds=TAGGED_SG_SEEDS, max_documents=3,
        )
        assert fetch.calls == [ETA_URL, COMP_URL, TA_URL]
        assert [d["found_by"] for d in report.found_by] == [
            "baseline 4.3", "baseline 4.3", "portal crawler",
        ]
        assert report.fetched == 3

    def test_the_committed_seeds_still_load_and_default_to_6_and_7(self):
        seeds = load_crawl_seeds()
        for economy in seeds.values():
            for family in economy.families.values():
                assert family.covers(6) or family.covers(7) or family.pillars or family.indicators


class TestTheAllowedHosts:
    NEVER = (
        "flk.npc.gov.cn", "www.npc.gov.cn", "www.law.go.th", "ratchakitcha.soc.go.th",
        "www.ratchakitcha.soc.go.th", "indiankanoon.org", "base.garant.ru",
        "www.consultant.ru", "lawinfochina.com", "www.bakermckenzie.com",
        "peraturanpedia.id", "cyrilla.org",
    )

    def test_no_forbidden_or_unofficial_host_is_ever_allowed(self):
        from regcompass.config import load_portals

        for portal in load_portals().values():
            assert not set(self.NEVER) & set(portal.hosts), portal.economy
            assert len(portal.hosts) == len(set(portal.hosts)), portal.economy

    def test_the_portal_host_stays_first(self):
        """robots_host reads the first host, so the additions come after it."""
        from regcompass.config import load_portals

        first = {k: p.hosts[0] for k, p in load_portals().items() if p.hosts}
        assert first["CN"] == "www.cac.gov.cn"
        assert first["IN"] == "indiacode.gov.in"
        assert first["ID"] == "peraturan.bpk.go.id"
        assert first["TH"] == "searchlaw.ocs.go.th"
        assert first["LA"] == "laoofficialgazette.gov.la"
        assert first["MN"] == "legalinfo.mn"


class TestHostsNeverRequested:
    GAZETTE = "https://ratchakitcha.soc.go.th/DATA/PDF/2562/A/069/T_0052.PDF"
    SEARCHLAW = "https://searchlaw.ocs.go.th/council-of-state/#/public/doc/abc"

    def test_a_law_whose_only_url_is_the_royal_gazette_is_skipped_unasked(
        self, storage, tmp_path
    ):
        path = _write_baseline(
            tmp_path,
            {"TH": {"name": "Thailand", "laws": [
                _law("Personal Data Protection Act B.E. 2562", ["7.1"], [self.GAZETTE], year=2019),
            ]}},
        )
        fetch = recorded_fetch({self.GAZETTE: _page("never")})
        report = discover_economy(
            "TH", storage, data_dir=tmp_path / "data", fetch=fetch,
            limiter=NullLimiter(), baseline_dir=path, pillar=7, indicators=["7.1"],
            seeds=NO_URL_SEEDS,
        )
        assert fetch.calls == [] and fetch.robots_calls == []
        (skip,) = report.baseline_skipped
        assert skip["code"] == "not_allowed_host"
        assert "ratchakitcha.soc.go.th" in skip["reason"]

    def test_the_gazette_is_passed_over_for_the_official_library(
        self, storage, tmp_path
    ):
        path = _write_baseline(
            tmp_path,
            {"TH": {"name": "Thailand", "laws": [
                _law("Personal Data Protection Act B.E. 2562", ["7.1"],
                     [self.GAZETTE, self.SEARCHLAW], year=2019),
            ]}},
        )
        fetch = recorded_fetch({self.SEARCHLAW: _page("Personal Data Protection Act 2562")})
        discover_economy(
            "TH", storage, data_dir=tmp_path / "data", fetch=fetch,
            limiter=NullLimiter(), baseline_dir=path, pillar=7, indicators=["7.1"],
            seeds=NO_URL_SEEDS,
        )
        assert fetch.calls == [self.SEARCHLAW]
        assert all("ratchakitcha" not in u for u in fetch.robots_calls)

    def test_a_forbidden_host_is_never_asked_even_if_it_were_listed(
        self, storage, tmp_path, monkeypatch
    ):
        from regcompass.config import load_portals as real_load

        def with_gazette(config_dir=None):
            portals = real_load(config_dir)
            th = portals["TH"]
            portals["TH"] = th.model_copy(
                update={"hosts": [*th.hosts, "ratchakitcha.soc.go.th"]}
            )
            return portals

        import regcompass.discovery as discovery_mod

        monkeypatch.setattr(discovery_mod, "load_portals", with_gazette)
        path = _write_baseline(
            tmp_path,
            {"TH": {"name": "Thailand", "laws": [
                _law("Personal Data Protection Act B.E. 2562", ["7.1"], [self.GAZETTE], year=2019),
            ]}},
        )
        fetch = recorded_fetch({self.GAZETTE: _page("never")})
        report = discover_economy(
            "TH", storage, data_dir=tmp_path / "data", fetch=fetch,
            limiter=NullLimiter(), baseline_dir=path, pillar=7, indicators=["7.1"],
            seeds=NO_URL_SEEDS,
        )
        assert fetch.calls == [] and fetch.robots_calls == []
        assert report.baseline_skipped[0]["code"] == "not_allowed_host"


class TestTheCommittedBaseline:
    def test_the_committed_list_loads_and_ranks_for_a_pool_economy(self):
        from regcompass.discovery import _baseline_economies, _rank_laws
        from regcompass.config import CONFIG_DIR

        economies = _baseline_economies(CONFIG_DIR, None)
        assert economies is not None and "TH" in economies
        ranked = _rank_laws(economies["TH"]["laws"], ["7.1", "7.2"])
        assert ranked, "the baseline cites Thai laws for 7.1 and 7.2"

    def test_an_absent_list_is_none(self, tmp_path):
        from regcompass.discovery import _baseline_economies

        assert _baseline_economies(tmp_path, None) is None


class TestUnchangedWithoutAPillar:
    def test_no_pillar_is_todays_discovery(self, storage, tmp_path):
        fetch = recorded_fetch(BODIES)
        report = _discover(storage, tmp_path, fetch)
        assert sorted(fetch.calls) == sorted([TA_URL, PDPA_URL])
        record = storage.run_get(report.run_id)
        assert record["pillars"] == [] and record["indicators"] is None
        assert "found_by" not in record["details"]


# ---------------------------------------------------------------------------
# Through the server


def _wait_done(client, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = client.get("/api/status").json()
        if not st["active"] and st["status"] in ("done", "error"):
            return st
        time.sleep(0.02)
    raise AssertionError("the job did not finish in time")


@pytest.fixture()
def app(tmp_path, monkeypatch):
    import regcompass.discovery as discovery_mod

    db = tmp_path / "web.db"
    out = tmp_path / "out"
    out.mkdir()
    fetch = recorded_fetch(BODIES)
    calls: list[dict] = []
    real = discovery_mod.discover_economy

    def offline(economy, storage, **kwargs):
        calls.append(dict(kwargs))
        return real(
            economy, storage, fetch=fetch, seeds=UNTAGGED_SG_SEEDS,
            limiter=NullLimiter(), baseline_dir=_write_baseline(tmp_path),
            **kwargs,
        )

    monkeypatch.setattr(discovery_mod, "discover_economy", offline)
    client = TestClient(
        create_app(db_path=db, out_dir=out, data_dir=tmp_path / "data", ui_dir=None)
    )
    return client, fetch, calls, db


class TestTheServer:
    def test_discover_takes_pillar_indicators_and_a_cap(self, app):
        client, fetch, calls, db = app
        r = client.post(
            "/api/discover",
            json={"economy": "SG", "pillar": 4, "indicators": ["4.3", "4.2"],
                  "max_documents": 2},
        )
        assert r.status_code == 200, r.text
        assert r.json()["pillar"] == 4
        st = _wait_done(client)
        assert st["status"] == "done"
        assert calls[0]["pillar"] == 4
        assert calls[0]["indicators"] == ["4.2", "4.3"]
        assert calls[0]["max_documents"] == 2
        assert fetch.calls == [ETA_URL, GONE_URL, COMP_URL]
        discovery = st["discovery"]
        assert discovery["pillar"] == 4
        assert {s["law"]: s["code"] for s in discovery["baseline_skipped"]} == {
            "Firm Copy Act": "not_allowed_host",
            "Gone Act 2015": "unreachable",
            "No Address Act": "no_url",
            "Newer Act 2020": "over_cap",
        }
        assert [d["found_by"] for d in discovery["found_by"]] == [
            "baseline 4.2, 4.3", "baseline 4.3",
        ]

    def test_the_cap_defaults_to_12_and_is_bounded(self, app):
        client, _, calls, _ = app
        client.post("/api/discover", json={"economy": "SG", "pillar": 4})
        _wait_done(client)
        client.post(
            "/api/discover", json={"economy": "SG", "pillar": 4, "max_documents": 500}
        )
        _wait_done(client)
        assert [c["max_documents"] for c in calls] == [12, 30]

    def test_indicators_must_belong_to_the_pillar(self, app):
        client, _, calls, _ = app
        r = client.post(
            "/api/discover", json={"economy": "SG", "pillar": 4, "indicators": ["6.1"]}
        )
        assert r.status_code == 400 and "6.1" in r.text
        r = client.post("/api/discover", json={"economy": "SG", "indicators": ["4.2"]})
        assert r.status_code == 400
        r = client.post("/api/discover", json={"economy": "SG", "pillar": 99})
        assert r.status_code == 400
        assert calls == []

    def test_an_economy_with_no_crawler_can_discover_by_pillar(self, app):
        client, _, calls, _ = app
        assert client.post("/api/discover", json={"economy": "CN"}).status_code == 400
        r = client.post("/api/discover", json={"economy": "CN", "pillar": 7})
        assert r.status_code == 200, r.text
        _wait_done(client)
        assert calls[0]["pillar"] == 7

    def test_no_pillar_passes_nothing_new(self, app):
        client, _, calls, _ = app
        client.post("/api/discover", json={"economy": "SG"})
        _wait_done(client)
        assert calls[0].get("pillar") is None
        assert calls[0].get("max_documents") is None

    def test_e2e_by_pillar_hands_the_draw_to_discovery(
        self, tmp_path, monkeypatch
    ):
        import regcompass.pipeline as pipeline_mod
        from regcompass.pipeline import E2EReport, RunReport

        called = {}

        def fake_run_e2e(storage, economy, pillars, engine, data_dir, outdir, **kwargs):
            called.update(kwargs, pillars=list(pillars))
            rep = E2EReport(economy=economy, crawl_fetched=1, documents_mapped=["d"])
            rep.run = RunReport(economy=economy, engine=engine.name, n_passed=1, n_groups=1)
            return rep

        monkeypatch.setattr(pipeline_mod, "run_e2e", fake_run_e2e)
        out = tmp_path / "out"
        out.mkdir()
        client = TestClient(
            create_app(db_path=tmp_path / "e.db", out_dir=out,
                       data_dir=tmp_path / "data", ui_dir=None)
        )
        r = client.post(
            "/api/run",
            json={"economy": "SG", "pillars": [4], "indicators": ["4.2", "4.3"],
                  "engine": "fake", "mode": "e2e", "discover_by_pillar": True},
        )
        assert r.status_code == 200, r.text
        assert _wait_done(client)["status"] == "done"
        assert called["discover_by_pillar"] is True
        assert called["discovery_hook"] is not None
        assert called["max_documents"] == 12
        assert called["indicators"] == ["4.2", "4.3"]

        r = client.post(
            "/api/run",
            json={"economy": "SG", "pillars": [4, 6], "engine": "fake",
                  "mode": "e2e", "discover_by_pillar": True},
        )
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Bounded and guarded: redirects, dead hosts, the time budget, the tries


def _answering(answers: dict, *, robots=b"User-agent: *\n", robots_by_host=None):
    """A recorded fetch where an answer is bytes (200), an int status, a
    ("redirect", target) pair or an exception to raise. Every request, robots
    included, is logged in `seen`."""
    import httpx

    seen: list[str] = []

    def fetch(url):
        from regcompass.crawl import FetchResult

        seen.append(url)
        if url.endswith("/robots.txt"):
            host = url.split("/")[2]
            body = (robots_by_host or {}).get(host, robots)
            if isinstance(body, Exception):
                raise body
            if isinstance(body, int):
                return FetchResult(url, url, body, b"", "text/plain", "httpx")
            return FetchResult(url, url, 200, body, "text/plain", "httpx")
        answer = answers.get(url, 404)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, tuple):
            return FetchResult(url, url, 302, b"", "text/html", "httpx", location=answer[1])
        if isinstance(answer, int):
            return FetchResult(url, url, answer, b"", "text/html", "httpx")
        return FetchResult(url, url, 200, answer, "text/html", "httpx")

    fetch.seen = seen
    fetch.httpx = httpx
    return fetch


def _one_law_baseline(tmp_path, economy, laws):
    return _write_baseline(tmp_path, {economy: {"name": economy, "laws": laws}})


class TestRedirects:
    DFT = "https://www.dft.go.th/law/1"

    def _th(self, storage, tmp_path, fetch, laws):
        return discover_economy(
            "TH", storage, data_dir=tmp_path / "data", fetch=fetch,
            limiter=NullLimiter(), baseline_dir=_one_law_baseline(tmp_path, "TH", laws),
            pillar=7, indicators=["7.1"], seeds=NO_URL_SEEDS,
        )

    def test_a_redirect_to_the_thai_law_portal_is_never_requested(self, storage, tmp_path):
        fetch = _answering({self.DFT: ("redirect", "https://www.law.go.th/x.pdf")})
        report = self._th(
            storage, tmp_path, fetch, [_law("Some Act B.E. 2562", ["7.1"], [self.DFT])]
        )
        assert not any("law.go.th/" in u and "dft" not in u for u in fetch.seen)
        (skip,) = report.baseline_skipped
        assert skip["code"] == "redirected_off"
        assert "redirected off the official hosts" in skip["reason"]

    def test_a_redirect_to_an_unlisted_host_is_not_followed(self, storage, tmp_path):
        fetch = _answering({self.DFT: ("redirect", "https://files.example.com/a.pdf")})
        report = self._th(
            storage, tmp_path, fetch, [_law("Some Act B.E. 2562", ["7.1"], [self.DFT])]
        )
        assert all("example.com" not in u for u in fetch.seen)
        assert report.baseline_skipped[0]["code"] == "redirected_off"

    def test_a_redirect_to_another_official_host_is_followed(self, storage, tmp_path):
        target = "https://www.mdes.go.th/law/detail/2562"
        fetch = _answering({
            self.DFT: ("redirect", target),
            target: _page("Personal Data Protection Act 2562"),
        })
        report = self._th(
            storage, tmp_path, fetch, [_law("Some Act B.E. 2562", ["7.1"], [self.DFT])]
        )
        assert target in fetch.seen
        assert report.fetched == 1 and report.baseline_skipped == []

    def test_the_http_client_refuses_a_redirect_to_a_never_requested_host(self):
        import httpx

        from regcompass.crawl import NeverRequestedHostError, fetch_httpx

        asked: list[str] = []

        def handler(request):
            asked.append(str(request.url))
            if request.url.host == "www.dft.go.th":
                return httpx.Response(302, headers={"location": "https://www.law.go.th/x"})
            return httpx.Response(200, content=b"never")

        with pytest.raises(NeverRequestedHostError):
            fetch_httpx(self.DFT, transport=httpx.MockTransport(handler), wait_max=0)
        assert asked == [self.DFT]

    def test_add_by_url_does_not_follow_a_redirect_to_a_never_requested_host(
        self, storage, tmp_path
    ):
        from regcompass.corpus import add_document_from_url
        from regcompass.crawl import RedirectOffHostError

        fetch = _answering({self.DFT: ("redirect", "https://ratchakitcha.soc.go.th/a.pdf")})
        with pytest.raises(RedirectOffHostError):
            add_document_from_url(
                storage, tmp_path / "data", "TH", self.DFT,
                fetch=fetch, limiter=NullLimiter(), allow_any_host=True,
            )
        assert all("ratchakitcha" not in u for u in fetch.seen)

    def test_a_never_requested_host_cannot_be_vouched_for(self):
        from regcompass.config import load_portals
        from regcompass.corpus import HostNotAllowedError, check_host_allowed

        with pytest.raises(HostNotAllowedError):
            check_host_allowed(
                load_portals()["TH"], "https://www.law.go.th/a.pdf", allow_any_host=True
            )


class TestBounds:
    A = f"{SSO}/Act/A2001?ViewType=Pdf"
    B = f"{SSO}/Act/B2002?ViewType=Pdf"
    C = f"{SSO}/Act/C2003?ViewType=Pdf"
    MDES = "https://www.mdes.go.th/law/a"

    def test_a_host_that_did_not_answer_is_not_asked_again(self, storage, tmp_path):
        import httpx

        laws = [
            _law("First Act 2001", ["7.1"], [self.MDES], year=2001),
            _law("Second Act 2002", ["7.1"], ["https://www.mdes.go.th/law/b"], year=2000),
        ]
        fetch = _answering(
            {}, robots_by_host={"www.mdes.go.th": httpx.ConnectError("no route")}
        )
        report = discover_economy(
            "TH", storage, data_dir=tmp_path / "data", fetch=fetch,
            limiter=NullLimiter(), baseline_dir=_one_law_baseline(tmp_path, "TH", laws),
            pillar=7, indicators=["7.1"], seeds=NO_URL_SEEDS,
        )
        assert fetch.seen == ["https://www.mdes.go.th/robots.txt"]
        assert [s["code"] for s in report.baseline_skipped] == ["unreachable", "unreachable"]

    def test_a_document_fetch_that_dies_marks_the_host_too(self, storage, tmp_path):
        import httpx

        laws = [
            _law("First Act 2001", ["4.2"], [self.A], year=2001),
            _law("Second Act 2002", ["4.2"], [self.B], year=2000),
        ]
        fetch = _answering({self.A: httpx.ReadTimeout("slow")})
        report = _discover(
            storage, tmp_path, fetch, pillar=4, indicators=["4.2"],
            baseline_dir=_one_law_baseline(tmp_path, "SG", laws),
        )
        assert self.B not in fetch.seen
        assert [s["code"] for s in report.baseline_skipped] == ["unreachable", "unreachable"]

    def test_the_stage_stops_at_its_time_budget(self, storage, tmp_path):
        ticks = iter(range(0, 100000, 400))
        laws = [
            _law("First Act 2001", ["4.2"], [self.A], year=2003),
            _law("Second Act 2002", ["4.2"], [self.B], year=2002),
            _law("Third Act 2003", ["4.2"], [self.C], year=2001),
        ]
        fetch = _answering({
            self.A: _page("First Act 2001"), self.B: _page("Second Act 2002"),
            self.C: _page("Third Act 2003"),
        })
        report = _discover(
            storage, tmp_path, fetch, pillar=4, indicators=["4.2"],
            baseline_dir=_one_law_baseline(tmp_path, "SG", laws),
            clock=lambda: next(ticks),
        )
        assert self.C not in fetch.seen
        codes = {s["law"]: s["code"] for s in report.baseline_skipped}
        assert codes["Third Act 2003"] == "time_limit"
        assert "10 minutes" in report.baseline_skipped[-1]["reason"]

    def test_addresses_tried_are_bounded_at_twice_the_cap(self, storage, tmp_path):
        urls = [f"{SSO}/Act/X{i}?ViewType=Pdf" for i in range(5)]
        laws = [_law("Gone Act", ["4.2"], urls, year=2001)]
        fetch = _answering({})
        report = _discover(
            storage, tmp_path, fetch, pillar=4, indicators=["4.2"], max_documents=1,
            baseline_dir=_one_law_baseline(tmp_path, "SG", laws),
        )
        assert [u for u in fetch.seen if "robots" not in u] == urls[:2]
        assert report.baseline_skipped[0]["code"] == "attempt_limit"


class TestRobotsPerHost:
    def test_the_portal_proceed_policy_binds_its_own_host_only(self, storage, tmp_path):
        meity = "https://www.meity.gov.in/act.pdf"
        laws = [_law("Information Technology Act 2000", ["7.2"], [meity], year=2000)]
        fetch = _answering(
            {meity: _page("Information Technology Act 2000")},
            robots_by_host={"www.meity.gov.in": 500},
        )
        report = discover_economy(
            "IN", storage, data_dir=tmp_path / "data", fetch=fetch,
            limiter=NullLimiter(), baseline_dir=_one_law_baseline(tmp_path, "IN", laws),
            pillar=7, indicators=["7.2"], seeds=UNTAGGED_SG_SEEDS.model_copy(
                update={"families": {"x": CrawlFamily(queries=["q"], pillars=[1])}}
            ),
        )
        assert meity not in fetch.seen
        assert report.baseline_skipped[0]["code"] == "robots"

    def test_the_title_search_failing_is_unreachable_not_no_address(self, storage, tmp_path):
        laws = [_law("The Telecommunications Act, 2023", ["7.2"], ["https://example.org/t"], year=2023)]

        def boom(url, params=None):
            raise RuntimeError("search is down")

        report = discover_economy(
            "IN", storage, data_dir=tmp_path / "data", fetch=_answering({}),
            get_json=boom, limiter=NullLimiter(),
            baseline_dir=_one_law_baseline(tmp_path, "IN", laws),
            pillar=7, indicators=["7.2"], seeds=UNTAGGED_SG_SEEDS.model_copy(
                update={"families": {"x": CrawlFamily(queries=["q"], pillars=[1])}}
            ),
        )
        (skip,) = report.baseline_skipped
        assert skip["code"] == "unreachable"
        assert "title search failed" in skip["reason"]


class TestACrawlerFailureIsNotFatal:
    def test_the_baseline_documents_stand_and_the_discovery_completes(
        self, storage, tmp_path, monkeypatch
    ):
        import regcompass.discovery as discovery_mod

        def broken(*args, **kwargs):
            raise RuntimeError("Portal search went away")

        monkeypatch.setattr(discovery_mod, "_discover", broken)
        report = _discover(
            storage, tmp_path, recorded_fetch(BODIES), pillar=4, indicators=["4.3"],
            seeds=TAGGED_SG_SEEDS,
        )
        assert report.fetched == 2
        assert any("the Portal crawler failed" in n for n in report.notes)
        record = storage.run_get(report.run_id)
        assert record["status"] == "completed" and record["documents_fetched"] == 2


class TestPlausibility:
    def test_the_civil_code_is_kept(self):
        from regcompass.discovery import plausibly_the_law

        assert plausibly_the_law(
            "Civil Code of the People's Republic of China《中华人民共和国民法典》",
            "中华人民共和国民法典 第一编 总则 第一条 为了保护民事主体的合法权益",
        )

    def test_an_indonesian_regulation_with_another_number_is_dropped(self):
        from regcompass.discovery import plausibly_the_law

        name = "Regulation of the Government of the Republic of Indonesia No.29 on Industrial Empowerment 2018"
        assert plausibly_the_law(
            name, "PERATURAN PEMERINTAH REPUBLIK INDONESIA NOMOR 29 TAHUN 2018 TENTANG PEMBERDAYAAN INDUSTRI"
        )
        assert not plausibly_the_law(
            name, "PERATURAN PEMERINTAH REPUBLIK INDONESIA NOMOR 28 TAHUN 2021 TENTANG PENYELENGGARAAN BIDANG PERINDUSTRIAN"
        )
        assert not plausibly_the_law("Law No. 28 of 2021", "UNDANG-UNDANG NOMOR 1 TAHUN 2028")

    def test_another_act_of_the_same_economy_is_dropped(self):
        from regcompass.discovery import plausibly_the_law

        assert not plausibly_the_law(
            "Personal Data Protection Act 2012",
            "Cybersecurity Act 2018. An Act to require or authorise the taking of measures",
        )

    def test_a_name_with_nothing_distinctive_is_kept(self):
        from regcompass.discovery import plausibly_the_law

        assert plausibly_the_law("The Law of the Republic", "Any text at all")


class TestTheCrawlerStageIsBounded:
    def test_a_silent_portal_is_asked_once_and_its_rows_stay_pending(
        self, storage, tmp_path
    ):
        import httpx

        fetch = _answering(
            dict(BODIES),
            robots_by_host={"sso.agc.gov.sg": httpx.ConnectTimeout("silent")},
        )
        report = _discover(
            storage, tmp_path, fetch, pillar=6, seeds=TAGGED_SG_SEEDS,
            baseline_dir=tmp_path / "absent",
        )
        assert fetch.seen == ["https://sso.agc.gov.sg/robots.txt"]
        assert report.fetched == 0
        pending = {r["url"] for r in storage.manifest_rows(economy="SG", status="pending")}
        assert pending == {TA_URL, PDPA_URL}, "not asked is not failed"

    def test_one_deadline_covers_both_stages(self, storage, tmp_path):
        ticks = iter(range(0, 10**6, 400))
        fetch = _answering(dict(BODIES))
        report = _discover(
            storage, tmp_path, fetch, pillar=4, indicators=["4.3"],
            seeds=TAGGED_SG_SEEDS, clock=lambda: next(ticks),
        )
        assert [u for u in fetch.seen if "robots" not in u] == []
        assert report.fetched == 0
        assert {s["code"] for s in report.baseline_skipped} == {"time_limit", "no_url"}
        assert storage.manifest_rows(economy="SG", status="failed") == []
        assert TA_URL not in fetch.seen

    def test_a_host_refusing_every_rung_is_not_asked_again(self, storage, tmp_path):
        from regcompass.crawl import FetchResult

        seen: list[str] = []

        def fetch(url):
            seen.append(url)
            if url.endswith("/robots.txt"):
                return FetchResult(url, url, 404, b"", "text/plain", "httpx")
            return FetchResult(url, url, 403, b"", "text/html", "curl_cffi")

        laws = [
            _law("First Act 2001", ["4.2"], [TestBounds.A], year=2001),
            _law("Second Act 2002", ["4.2"], [TestBounds.B], year=2000),
        ]
        report = _discover(
            storage, tmp_path, fetch, pillar=4, indicators=["4.2"],
            baseline_dir=_one_law_baseline(tmp_path, "SG", laws),
        )
        assert TestBounds.B not in seen
        assert [s["code"] for s in report.baseline_skipped] == ["unreachable", "unreachable"]


class TestTheLadder:
    def test_a_refusal_of_ours_is_never_escalated(self):
        from regcompass.crawl import (
            NeverRequestedHostError,
            RedirectOffHostError,
            fetch_with_ladder,
        )

        for refusal in (
            NeverRequestedHostError("no"),
            RedirectOffHostError("https://a.go.th/x", "https://www.law.go.th/y"),
        ):
            climbed: list[str] = []

            def first(url, refusal=refusal):
                raise refusal

            rungs = (("httpx", first), ("playwright", lambda url: climbed.append(url)))
            with pytest.raises(type(refusal)):
                fetch_with_ladder("https://a.go.th/x", rungs)
            assert climbed == []

    def test_a_timeout_stops_a_bounded_ladder(self):
        import httpx

        from regcompass.crawl import fetch_with_ladder

        climbed: list[str] = []

        def slow(url):
            raise httpx.ReadTimeout("silent")

        rungs = (("httpx", slow), ("curl_cffi", lambda url: climbed.append(url)))
        with pytest.raises(httpx.ReadTimeout):
            fetch_with_ladder("https://a.go.th/x", rungs, stop_on_timeout=True)
        assert climbed == []

    def test_the_browser_aborts_a_redirect_to_a_never_requested_host(self):
        from regcompass.crawl import _route_guard

        class Response:
            status = 302
            headers = {"location": "https://www.law.go.th/x.pdf"}

        class Route:
            def __init__(self, url):
                self.request = type("R", (), {"url": url})()
                self.done: list[str] = []

            def fetch(self, max_redirects=None):
                assert max_redirects == 0
                self.done.append("fetch")
                return Response()

            def abort(self):
                self.done.append("abort")

            def fulfill(self, response=None):
                self.done.append("fulfill")

        route = Route("https://www.dft.go.th/law/1")
        _route_guard(route)
        assert route.done == ["fetch", "abort"]
        direct = Route("https://sub.ratchakitcha.soc.go.th/a")
        _route_guard(direct)
        assert direct.done == ["abort"]

    def test_any_subdomain_of_a_never_requested_host_is_refused(self):
        from regcompass.contracts import is_never_requested

        assert is_never_requested("law.go.th")
        assert is_never_requested("www.law.go.th.")
        assert is_never_requested("m.ratchakitcha.soc.go.th")
        assert is_never_requested("FLK.NPC.GOV.CN")
        assert not is_never_requested("searchlaw.ocs.go.th")
        assert not is_never_requested("flaw.go.th")


class TestPlausibilityCases:
    def test_same_year_another_law_is_dropped(self):
        from regcompass.discovery import plausibly_the_law

        assert not plausibly_the_law(
            "The Digital Personal Data Protection Act, 2023",
            "THE TELECOMMUNICATIONS ACT, 2023 NO. 44 OF 2023 An Act to amend and"
            " consolidate the law relating to telecommunication services and"
            " the Digital Bharat Nidhi",
        )
        assert plausibly_the_law(
            "The Digital Personal Data Protection Act, 2023",
            "THE DIGITAL PERSONAL DATA PROTECTION ACT, 2023 NO. 22 OF 2023",
        )

    def test_the_amending_act_is_not_the_act(self):
        from regcompass.discovery import plausibly_the_law

        assert not plausibly_the_law(
            "The Information Technology Act, 2000",
            "THE INFORMATION TECHNOLOGY (AMENDMENT) ACT, 2008 NO. 10 OF 2009 An Act"
            " further to amend the Information Technology Act, 2000",
        )
        assert not plausibly_the_law(
            "Law No. 11 of 2008 on Electronic Information and Transactions",
            "UNDANG-UNDANG REPUBLIK INDONESIA NOMOR 19 TAHUN 2016 TENTANG PERUBAHAN"
            " ATAS UNDANG-UNDANG NOMOR 11 TAHUN 2008 TENTANG INFORMASI DAN"
            " TRANSAKSI ELEKTRONIK",
        )
        assert plausibly_the_law(
            "Law No. 11 of 2008 on Electronic Information and Transactions",
            "UNDANG-UNDANG REPUBLIK INDONESIA NOMOR 11 TAHUN 2008 TENTANG INFORMASI"
            " DAN TRANSAKSI ELEKTRONIK",
        )

    def test_leading_zeros_do_not_matter(self):
        from regcompass.discovery import plausibly_the_law

        assert plausibly_the_law("Decree No. 7 of 2019", "DECREE NO. 07/2019 ON STANDARDS")


class TestIndiaCodeSearchAnswers:
    def _run(self, storage, tmp_path, get_json):
        laws = [_law("The Telecommunications Act, 2023", ["7.2"], ["https://example.org/t"], year=2023)]
        return discover_economy(
            "IN", storage, data_dir=tmp_path / "data", fetch=_answering({}),
            get_json=get_json, limiter=NullLimiter(),
            baseline_dir=_one_law_baseline(tmp_path, "IN", laws),
            pillar=7, indicators=["7.2"], seeds=UNTAGGED_SG_SEEDS.model_copy(
                update={"families": {"x": CrawlFamily(queries=["q"], pillars=[1])}}
            ),
        )

    def test_an_error_answer_is_unreachable(self, storage, tmp_path):
        report = self._run(storage, tmp_path, lambda url, params=None: (500, {}))
        (skip,) = report.baseline_skipped
        assert skip["code"] == "unreachable" and "HTTP 500" in skip["reason"]

    def test_no_exact_title_is_its_own_reason(self, storage, tmp_path):
        answer = {"_embedded": {"searchResult": {"_embedded": {"objects": [
            {"_embedded": {"indexableObject": {"name": "Application of Act."}}}
        ]}}}}
        report = self._run(storage, tmp_path, lambda url, params=None: (200, answer))
        (skip,) = report.baseline_skipped
        assert skip["code"] == "no_title_match"


class TestPlausibilityRealShapes:
    """Real openings of correct laws that must be kept, and the wrong-law
    cases that must still be dropped."""

    KEEP = [
        (
            "Foreign Acquisitions and Takeovers Act 1975",
            "Authorised Version C2025C00012 registered 03/01/2025\n"
            "Foreign Acquisitions and Takeovers Act 1975\nNo. 92, 1975\n"
            "Compilation No. 41\nCompilation date: 1 January 2025",
        ),
        (
            "Criminal Code Act 1995",
            "Authorised Version C2024C00123 registered 12/04/2024\nCriminal Code Act 1995\n"
            "No. 12, 1995\nCompilation No. 160\nIncludes amendments up to: Act No. 3, 2024",
        ),
        (
            "Customs (Prohibited Exports) Regulations 1958",
            "Authorised Version F2024C00456 registered 01/05/2024\n"
            "Customs (Prohibited Exports) Regulations 1958\nStatutory Rules No. 5, 1958 as"
            " amended\nCompilation No. 80\nF1996B00341",
        ),
        (
            "Personal Data Protection Act 2012 (PDPA)",
            "Current version as at 29 Sep 2026\nPERSONAL DATA PROTECTION ACT 2012\n"
            "2020 REVISED EDITION\nThis revised edition incorporates all amendments",
        ),
        (
            "Patents Act 1994",
            "2020 REVISED EDITION\nPATENTS ACT 1994\nAn Act relating to patents",
        ),
        (
            "Countervailing and Anti-Dumping Duties Act (Act 504) P.U.(A) 233/94 1993",
            "LAWS OF MALAYSIA\nREPRINT\nAct 504\nCOUNTERVAILING AND ANTI-DUMPING DUTIES"
            " ACT 1993\nIncorporating all amendments up to 1 January 2006",
        ),
        (
            "The Digital Personal Data Protection Act, 2023",
            "REGISTERED NO. DL-(N)04/0007/2003-23\nTHE GAZETTE OF INDIA EXTRAORDINARY\n"
            "MINISTRY OF LAW AND JUSTICE\nTHE DIGITAL PERSONAL DATA PROTECTION ACT, 2023\n"
            "NO. 22 OF 2023\n[11th August, 2023.]",
        ),
        (
            "Telecommunications (Interception and Access) Authorisation Rules 2019",
            "Telecommunications (Interception and Access) Authorization Rules 2019\n"
            "made under the Telecommunications Act",
        ),
    ]
    DROP = [
        (
            "The Digital Personal Data Protection Act, 2023",
            "THE TELECOMMUNICATIONS ACT, 2023 NO. 44 OF 2023 An Act to amend and"
            " consolidate the law relating to telecommunication services and the"
            " Digital Bharat Nidhi",
        ),
        (
            "The Information Technology Act, 2000",
            "THE INFORMATION TECHNOLOGY (AMENDMENT) ACT, 2008 NO. 10 OF 2009 An Act"
            " further to amend the Information Technology Act, 2000",
        ),
        (
            "Law No. 11 of 2008 on Electronic Information and Transactions",
            "UNDANG-UNDANG REPUBLIK INDONESIA NOMOR 19 TAHUN 2016 TENTANG PERUBAHAN"
            " ATAS UNDANG-UNDANG NOMOR 11 TAHUN 2008 TENTANG INFORMASI DAN"
            " TRANSAKSI ELEKTRONIK",
        ),
    ]

    @pytest.mark.parametrize("name,text", KEEP, ids=[k[0][:40] for k in KEEP])
    def test_the_right_law_is_kept(self, name, text):
        from regcompass.discovery import plausibly_the_law

        assert plausibly_the_law(name, text)

    @pytest.mark.parametrize("name,text", DROP, ids=[d[0][:40] for d in DROP])
    def test_the_wrong_law_is_dropped(self, name, text):
        from regcompass.discovery import plausibly_the_law

        assert not plausibly_the_law(name, text)


class TestNothingRunsPastTheDeadline:
    A = TestBounds.A
    B = TestBounds.B

    def test_a_politeness_wait_that_would_pass_the_deadline_is_not_slept(
        self, storage, tmp_path
    ):
        from regcompass.crawl import RateLimiter

        slept: list[float] = []
        limiter = RateLimiter(1000.0, clock=lambda: 0.0, sleep=slept.append)
        laws = [
            _law("First Act 2001", ["4.2"], [self.A], year=2001),
            _law("Second Act 2002", ["4.2"], [self.B], year=2000),
        ]
        fetch = _answering({self.A: _page("First Act 2001"), self.B: _page("Second Act 2002")})
        report = discover_economy(
            "SG", storage, data_dir=tmp_path / "data", fetch=fetch, limiter=limiter,
            clock=lambda: 0.0, seeds=UNTAGGED_SG_SEEDS,
            baseline_dir=_one_law_baseline(tmp_path, "SG", laws), pillar=4,
            indicators=["4.2"],
        )
        assert slept == []
        assert self.B not in fetch.seen
        assert report.fetched == 1
        assert report.baseline_skipped[0]["code"] == "time_limit"

    def test_a_law_fetched_as_time_runs_out_is_not_read(self, storage, tmp_path):
        now = [0.0]

        def fetch(url):
            answer = _answering({self.A: _page("First Act 2001")})(url)
            if url == self.A:
                now[0] = 10_000.0
            return answer

        laws = [_law("First Act 2001", ["4.2"], [self.A], year=2001)]
        report = _discover(
            storage, tmp_path, fetch, pillar=4, indicators=["4.2"],
            baseline_dir=_one_law_baseline(tmp_path, "SG", laws), clock=lambda: now[0],
        )
        assert storage.corpus_documents("SG") == []
        assert report.baseline_skipped[0]["code"] == "time_limit"

    def test_crawler_files_fetched_after_the_deadline_are_not_read(
        self, storage, tmp_path
    ):
        now = [0.0]
        inner = _answering(dict(BODIES))

        def fetch(url):
            answer = inner(url)
            if url == TA_URL:
                now[0] = 10_000.0
            return answer

        report = _discover(
            storage, tmp_path, fetch, pillar=4, seeds=TAGGED_SG_SEEDS,
            baseline_dir=tmp_path / "absent", clock=lambda: now[0],
        )
        assert storage.corpus_documents("SG") == []
        assert [r["url"] for r in storage.manifest_rows(economy="SG", status="fetched")] == [TA_URL]
        assert any("were not read" in n for n in report.notes)

    def test_the_impersonating_rungs_check_the_deadline_first(self):
        from regcompass.crawl import DeadlineReachedError, fetch_curl_cffi, fetch_playwright

        def expired(url):
            raise DeadlineReachedError("late")

        with pytest.raises(DeadlineReachedError):
            fetch_curl_cffi("https://sso.agc.gov.sg/x", before_request=expired)
        with pytest.raises(DeadlineReachedError):
            fetch_playwright("https://sso.agc.gov.sg/x", before_request=expired)


NO_CRAWLER_SEEDS = UNTAGGED_SG_SEEDS.model_copy(
    update={"families": {"x": CrawlFamily(queries=["q"], pillars=[1])}}
)


def _refusing(answers: dict, refused: set[str]):
    """_answering, except that every address in `refused` answers 403 on the
    impersonating rung too (a bot challenge)."""
    from regcompass.crawl import FetchResult

    inner = _answering(answers)

    def fetch(url):
        if url in refused:
            inner.seen.append(url)
            return FetchResult(url, url, 403, b"", "text/html", "curl_cffi")
        return inner(url)

    fetch.seen = inner.seen
    return fetch


class TestARefusalIsRememberedPerPath:
    """A host that refuses one kind of page (a bot challenge on its preview
    pages) may still serve another (its PDF downloads). A refusal is kept per
    host and first path segment; a host that does not answer at all is still
    not asked again anywhere."""

    BPK = "https://peraturan.bpk.go.id"
    PAGE_A = f"{BPK}/Read/1/uu-no-1-tahun-2010"
    PAGE_B = f"{BPK}/Read/2/uu-no-2-tahun-2009"
    DOWNLOAD = f"{BPK}/Download/9/UU%20Nomor%203%20Tahun%202008.pdf"

    LAWS = [
        _law("Law No. 1 of 2010", ["7.1"], [PAGE_A], year=2010),
        _law("Law No. 2 of 2009", ["7.1"], [PAGE_B], year=2009),
        _law("Law No. 3 of 2008", ["7.1"], [DOWNLOAD], year=2008),
    ]

    def _id(self, storage, tmp_path, fetch):
        return discover_economy(
            "ID", storage, data_dir=tmp_path / "data", fetch=fetch,
            limiter=NullLimiter(),
            baseline_dir=_one_law_baseline(tmp_path, "ID", self.LAWS),
            pillar=7, indicators=["7.1"], seeds=NO_CRAWLER_SEEDS,
        )

    def test_a_refused_path_does_not_close_the_host(self, storage, tmp_path):
        fetch = _refusing({self.DOWNLOAD: _page("Law No. 3 of 2008")}, {self.PAGE_A})
        report = self._id(storage, tmp_path, fetch)
        assert self.DOWNLOAD in fetch.seen
        assert report.fetched == 1
        # The refused path is not asked again, even for another law.
        assert self.PAGE_B not in fetch.seen
        skipped = _by_law(report)
        assert skipped["Law No. 1 of 2010"]["code"] == "unreachable"
        assert skipped["Law No. 2 of 2009"]["code"] == "unreachable"
        assert "refused" in skipped["Law No. 2 of 2009"]["reason"]

    def test_a_host_that_does_not_answer_is_still_closed(self, storage, tmp_path):
        import httpx

        fetch = _answering({
            self.PAGE_A: httpx.ConnectTimeout("silent"),
            self.DOWNLOAD: _page("Law No. 3 of 2008"),
        })
        report = self._id(storage, tmp_path, fetch)
        assert self.DOWNLOAD not in fetch.seen and self.PAGE_B not in fetch.seen
        assert report.fetched == 0

    def test_a_refusal_is_not_retried(self, storage, tmp_path):
        fetch = _refusing({}, {self.PAGE_A})
        self._id(storage, tmp_path, fetch)
        assert fetch.seen.count(self.PAGE_A) == 1

    def test_the_crawler_stage_may_ask_another_path_of_a_refusing_host(
        self, storage, tmp_path
    ):
        from regcompass.crawl import LadderExhaustedError

        details = f"{SSO}/Details/X2010"

        def fetch(url):
            if url == details:
                fetch.seen.append(url)
                raise LadderExhaustedError(
                    f"all rungs blocked for {url}", statuses=(403, 403)
                )
            return inner(url)

        inner = _answering(dict(BODIES))
        fetch.seen = inner.seen
        laws = [_law("Refused Act 2010", ["4.3"], [details], year=2010)]
        report = _discover(
            storage, tmp_path, fetch, pillar=4, indicators=["4.3"],
            seeds=TAGGED_SG_SEEDS, baseline_dir=_one_law_baseline(tmp_path, "SG", laws),
        )
        assert TA_URL in fetch.seen
        assert [d["found_by"] for d in report.found_by] == ["portal crawler"]

    def _answering_status(self, answers: dict, statuses: dict[str, int]):
        """_answering, except that an address in `statuses` answers that
        status on the impersonating rung too."""
        from regcompass.crawl import FetchResult

        inner = _answering(answers)

        def fetch(url):
            if url in statuses:
                inner.seen.append(url)
                return FetchResult(url, url, statuses[url], b"", "text/html", "curl_cffi")
            return inner(url)

        fetch.seen = inner.seen
        return fetch

    def _id_laws(self, storage, tmp_path, fetch, laws):
        return discover_economy(
            "ID", storage, data_dir=tmp_path / "data", fetch=fetch,
            limiter=NullLimiter(),
            baseline_dir=_one_law_baseline(tmp_path, "ID", laws),
            pillar=7, indicators=["7.1"], seeds=NO_CRAWLER_SEEDS,
        )

    def test_a_host_refusing_a_third_kind_of_address_is_closed(self, storage, tmp_path):
        preview = f"{self.BPK}/Read/1/a"
        ruling = f"{self.BPK}/DownloadUjiMateri/2/b"
        search = f"{self.BPK}/Search?keywords=c"
        laws = [
            _law("Law No. 1 of 2010", ["7.1"], [preview], year=2010),
            _law("Law No. 2 of 2009", ["7.1"], [ruling], year=2009),
            _law("Law No. 3 of 2008", ["7.1"], [search], year=2008),
            _law("Law No. 4 of 2007", ["7.1"], [self.DOWNLOAD], year=2007),
        ]
        fetch = _refusing(
            {self.DOWNLOAD: _page("Law No. 4 of 2007")}, {preview, ruling, search}
        )
        report = self._id_laws(storage, tmp_path, fetch, laws)
        assert self.DOWNLOAD not in fetch.seen
        assert report.fetched == 0
        reason = _by_law(report)["Law No. 4 of 2007"]["reason"]
        assert "peraturan.bpk.go.id refused 3 kinds of addresses" in reason

    def test_two_refused_kinds_leave_the_host_open(self, storage, tmp_path):
        preview = f"{self.BPK}/Read/1/a"
        ruling = f"{self.BPK}/DownloadUjiMateri/2/b"
        laws = [
            _law("Law No. 1 of 2010", ["7.1"], [preview], year=2010),
            _law("Law No. 2 of 2009", ["7.1"], [ruling], year=2009),
            _law("Law No. 4 of 2007", ["7.1"], [self.DOWNLOAD], year=2007),
        ]
        fetch = _refusing({self.DOWNLOAD: _page("Law No. 4 of 2007")}, {preview, ruling})
        report = self._id_laws(storage, tmp_path, fetch, laws)
        assert self.DOWNLOAD in fetch.seen and report.fetched == 1

    def test_a_429_closes_only_its_path(self, storage, tmp_path):
        fetch = self._answering_status(
            {self.DOWNLOAD: _page("Law No. 3 of 2008")}, {self.PAGE_A: 429}
        )
        report = self._id(storage, tmp_path, fetch)
        assert self.PAGE_B not in fetch.seen
        assert self.DOWNLOAD in fetch.seen and report.fetched == 1

    def test_a_503_on_every_rung_closes_the_host(self, storage, tmp_path):
        fetch = self._answering_status(
            {self.DOWNLOAD: _page("Law No. 3 of 2008")}, {self.PAGE_A: 503}
        )
        report = self._id(storage, tmp_path, fetch)
        assert self.DOWNLOAD not in fetch.seen and self.PAGE_B not in fetch.seen
        assert report.fetched == 0

    def test_a_503_ladder_closes_the_host_for_the_crawler_stage_too(
        self, storage, tmp_path
    ):
        from regcompass.crawl import LadderExhaustedError

        details = f"{SSO}/Details/X2010"

        def fetch(url):
            if url == details:
                fetch.seen.append(url)
                raise LadderExhaustedError(
                    f"all rungs blocked for {url}", statuses=(503, 503)
                )
            return inner(url)

        inner = _answering(dict(BODIES))
        fetch.seen = inner.seen
        laws = [_law("Refused Act 2010", ["4.3"], [details], year=2010)]
        report = _discover(
            storage, tmp_path, fetch, pillar=4, indicators=["4.3"],
            seeds=TAGGED_SG_SEEDS, baseline_dir=_one_law_baseline(tmp_path, "SG", laws),
        )
        assert TA_URL not in fetch.seen
        assert report.found_by == []

    def test_a_refused_robots_txt_closes_the_host(self, storage, tmp_path):
        robots = f"{self.BPK}/robots.txt"
        fetch = self._answering_status(
            {self.DOWNLOAD: _page("Law No. 3 of 2008")}, {robots: 403}
        )
        report = self._id(storage, tmp_path, fetch)
        assert fetch.seen == [robots]
        assert report.fetched == 0
        assert {s["code"] for s in report.baseline_skipped} == {"unreachable"}
        assert "robots.txt" in _by_law(report)["Law No. 3 of 2008"]["reason"]


class TestTheLadderSaysWhyItStopped:
    def test_every_rung_refusing_is_a_refusal(self):
        from regcompass.crawl import FetchResult, LadderExhaustedError, fetch_with_ladder

        def refuse(url):
            return FetchResult(url, url, 403, b"", "text/html", "httpx")

        with pytest.raises(LadderExhaustedError) as caught:
            fetch_with_ladder("https://a.go.id/Details/1", (("a", refuse), ("b", refuse)))
        assert caught.value.statuses == (403, 403)
        assert caught.value.refused

    def test_a_rung_that_died_is_not_a_refusal(self):
        from regcompass.crawl import FetchResult, LadderExhaustedError, fetch_with_ladder

        def refuse(url):
            return FetchResult(url, url, 403, b"", "text/html", "httpx")

        def dead(url):
            raise ConnectionError("reset")

        with pytest.raises(LadderExhaustedError) as caught:
            fetch_with_ladder("https://a.go.id/Details/1", (("a", refuse), ("b", dead)))
        assert not caught.value.refused


IT_ACT_PDF = "https://indiacode.gov.in/server/api/core/bitstreams/it2000/content"
TELEGRAPH_PDF = "https://indiacode.gov.in/server/api/core/bitstreams/tg1885/content"


def _india_code_search(titles: dict[str, str]):
    """A fake India Code search: an exact title -> one item whose ORIGINAL
    bundle holds `titles[title]`. Every query is logged in `queries`."""
    queries: list[str] = []

    def get_json(url, params=None):
        query = (params or {}).get("query", "")
        queries.append(query)
        objects = [
            {"_embedded": {"indexableObject": {
                "name": title,
                "_embedded": {"bundles": {"_embedded": {"bundles": [{
                    "name": "ORIGINAL",
                    "_embedded": {"bitstreams": {"_embedded": {"bitstreams": [{
                        "name": "act.pdf", "_links": {"content": {"href": href}},
                    }]}}},
                }]}}},
            }}}
            for title, href in titles.items()
        ]
        return 200, {"_embedded": {"searchResult": {"_embedded": {"objects": objects}}}}

    get_json.queries = queries
    return get_json


class TestIndiaCodeMoved:
    """India Code moved from www.indiacode.nic.in to indiacode.gov.in, and the
    old host now refuses. A baseline link on the old host goes to the Portal's
    exact title search instead of being fetched, and an RDTII citation name is
    put in India Code's own title form before that search."""

    LEGACY = "https://www.indiacode.nic.in/bitstream/123456789/13115/1/indiantelegraphact_1885.pdf"

    def _in(self, storage, tmp_path, laws, get_json, answers):
        fetch = _answering(answers)
        report = discover_economy(
            "IN", storage, data_dir=tmp_path / "data", fetch=fetch,
            get_json=get_json, limiter=NullLimiter(),
            baseline_dir=_one_law_baseline(tmp_path, "IN", laws),
            pillar=7, indicators=["7.2"], seeds=NO_CRAWLER_SEEDS,
        )
        return report, fetch

    def test_a_legacy_link_goes_to_the_title_search(self, storage, tmp_path):
        search = _india_code_search({"The Indian Telegraph Act, 1885": TELEGRAPH_PDF})
        report, fetch = self._in(
            storage, tmp_path,
            [_law("Indian Telegraph Act No. 13 1885", ["7.2"], [self.LEGACY], year=1885)],
            search, {TELEGRAPH_PDF: _page("The Indian Telegraph Act, 1885")},
        )
        assert self.LEGACY not in fetch.seen
        assert search.queries == ["The Indian Telegraph Act, 1885"]
        assert TELEGRAPH_PDF in fetch.seen
        assert report.fetched == 1

    def test_a_citation_name_is_put_in_the_title_form(self, storage, tmp_path):
        search = _india_code_search({"The Information Technology Act, 2000": IT_ACT_PDF})
        report, _ = self._in(
            storage, tmp_path,
            [_law("Government of India, Information Technology Act No.21 2000", ["7.2"], [], pairing="none", year=2000)],
            search, {IT_ACT_PDF: _page("The Information Technology Act, 2000")},
        )
        assert search.queries == ["The Information Technology Act, 2000"]
        assert report.fetched == 1

    def test_a_legacy_link_already_in_the_corpus_is_not_searched(
        self, storage, tmp_path
    ):
        from regcompass.corpus import add_document_from_url
        from regcompass.discovery import IN_CORPUS_STATUS

        add_document_from_url(
            storage, tmp_path / "data", "IN", self.LEGACY,
            fetch=_answering({self.LEGACY: _page("The Indian Telegraph Act, 1885")}),
            limiter=NullLimiter(),
        )
        search = _india_code_search({"The Indian Telegraph Act, 1885": TELEGRAPH_PDF})
        report, fetch = self._in(
            storage, tmp_path,
            [_law("Indian Telegraph Act No. 13 1885", ["7.2"], [self.LEGACY], year=1885)],
            search, {TELEGRAPH_PDF: _page("The Indian Telegraph Act, 1885")},
        )
        assert search.queries == []
        assert [u for u in fetch.seen if not u.endswith("/robots.txt")] == []
        assert report.fetched == 0
        assert [(d["url"], d["status"]) for d in report.found_by] == [
            (self.LEGACY, IN_CORPUS_STATUS)
        ]

    def test_other_links_of_the_law_are_still_tried(self, storage, tmp_path):
        other = "https://www.meity.gov.in/rules.pdf"
        name = "Information Technology (Reasonable Security Practices) Rules 2011"
        search = _india_code_search({})
        report, fetch = self._in(
            storage, tmp_path,
            [_law(name, ["7.2"], [other, self.LEGACY], pairing="single", year=2011)],
            search, {other: _page(name)},
        )
        assert self.LEGACY not in fetch.seen
        assert other in fetch.seen
        assert report.fetched == 1

    @pytest.mark.parametrize(
        "cited, title",
        [
            ("Government of India, Information Technology Act No.21 2000",
             "The Information Technology Act, 2000"),
            ("Indian Telegraph Act No. 13 1885", "The Indian Telegraph Act, 1885"),
            ("The Central Goods and Services Tax Act No. 12 2017",
             "The Central Goods and Services Tax Act, 2017"),
            ("Indian Penal Code 1860", "The Indian Penal Code, 1860"),
            ("The Telecommunications Act, 2023", "The Telecommunications Act, 2023"),
            ("Order XXXIX of the Civil Procedure Code, 1908 (CPC)",
             "Order XXXIX of the Civil Procedure Code, 1908 (CPC)"),
            ("IPR Policy 2016", "IPR Policy 2016"),
            ("The Cigarettes and Other Tobacco Products (Prohibition of Advertisement"
             " and Regulation of Trade and Commerce, Production, Supply and"
             " Distribution) Act No. 34 2003",
             "The Cigarettes and Other Tobacco Products (Prohibition of Advertisement"
             " and Regulation of Trade and Commerce, Production, Supply and"
             " Distribution) Act, 2003"),
            ("The Code Of Civil Procedure (Amendment) Act No. 22 2002",
             "The Code Of Civil Procedure (Amendment) Act, 2002"),
        ],
    )
    def test_citation_names(self, cited, title):
        from regcompass.crawl import in_citation_title

        assert in_citation_title(cited) == title


DPDP_PDF = "https://indiacode.gov.in/server/api/core/bitstreams/dpdp2023/content"


class TestIndiaCodeWhenEveryLinkFails:
    """A baseline law whose every link was refused or did not answer is
    looked up once by exact title on India Code, inside the same limits."""

    MEITY = "https://www.meity.gov.in/writereaddata/files/DPDP2023.pdf"
    NAME = "Digital Personal Data Protection Act No. 22 2023"

    def _in(self, storage, tmp_path, answers, laws=None, get_json=None, **kwargs):
        fetch = _answering(answers)
        search = get_json or _india_code_search(
            {"The Digital Personal Data Protection Act, 2023": DPDP_PDF}
        )
        report = discover_economy(
            "IN", storage, data_dir=tmp_path / "data", fetch=fetch,
            get_json=search, limiter=NullLimiter(),
            baseline_dir=_one_law_baseline(
                tmp_path, "IN",
                laws or [_law(self.NAME, ["7.2"], [self.MEITY], year=2023)],
            ),
            pillar=7, indicators=["7.2"], seeds=NO_CRAWLER_SEEDS, **kwargs,
        )
        return report, fetch, search

    def test_a_refused_link_falls_back_to_the_title_search(self, storage, tmp_path):
        report, fetch, search = self._in(storage, tmp_path, {
            self.MEITY: 403,
            DPDP_PDF: _page("The Digital Personal Data Protection Act, 2023"),
        })
        assert self.MEITY in fetch.seen and DPDP_PDF in fetch.seen
        assert search.queries == ["The Digital Personal Data Protection Act, 2023"]
        assert report.fetched == 1 and report.baseline_skipped == []

    def test_a_second_discovery_does_not_fetch_the_found_law_again(
        self, storage, tmp_path
    ):
        from regcompass.discovery import IN_CORPUS_STATUS

        answers = {
            self.MEITY: 403,
            DPDP_PDF: _page("The Digital Personal Data Protection Act, 2023"),
        }
        first, _, _ = self._in(storage, tmp_path, answers)
        assert first.fetched == 1
        second, fetch, search = self._in(storage, tmp_path, answers)
        assert DPDP_PDF not in fetch.seen, "the bitstream is not requested again"
        assert len(search.queries) == 1
        assert second.fetched == 0 and second.baseline_skipped == []
        assert [(d["url"], d["status"]) for d in second.found_by] == [
            (DPDP_PDF, IN_CORPUS_STATUS)
        ]
        assert second.skipped_existing == 1

    def test_a_link_that_did_not_answer_falls_back_too(self, storage, tmp_path):
        import httpx

        report, _, search = self._in(storage, tmp_path, {
            self.MEITY: httpx.ConnectTimeout("silent"),
            DPDP_PDF: _page("The Digital Personal Data Protection Act, 2023"),
        })
        assert len(search.queries) == 1 and report.fetched == 1

    def test_at_most_one_search_per_law(self, storage, tmp_path):
        report, fetch, search = self._in(storage, tmp_path, {
            self.MEITY: 403, DPDP_PDF: 403,
        })
        assert len(search.queries) == 1
        assert fetch.seen.count(DPDP_PDF) == 1
        (skip,) = report.baseline_skipped
        assert skip["code"] == "unreachable"

    def test_a_law_already_searched_is_not_searched_again(self, storage, tmp_path):
        legacy = "https://www.indiacode.nic.in/bitstream/1/2/dpdp.pdf"
        report, _, search = self._in(
            storage, tmp_path, {self.MEITY: 403, DPDP_PDF: 403},
            laws=[_law(self.NAME, ["7.2"], [self.MEITY, legacy], year=2023)],
        )
        assert len(search.queries) == 1

    def test_a_search_with_no_match_keeps_the_links_own_reason(
        self, storage, tmp_path
    ):
        report, _, search = self._in(
            storage, tmp_path, {self.MEITY: 403},
            get_json=_india_code_search({}),
            laws=[_law(self.NAME, ["7.2"], [self.MEITY], year=2023)],
        )
        # Searched once, nothing found: the link's own failure is reported.
        assert len(search.queries) == 1
        assert report.baseline_skipped[0]["code"] == "unreachable"
        assert "403" in report.baseline_skipped[0]["reason"]

    def test_the_attempt_limit_holds(self, storage, tmp_path):
        urls = [f"https://www.meity.gov.in/x{i}.pdf" for i in range(3)]
        report, fetch, search = self._in(
            storage, tmp_path, {u: 403 for u in urls},
            laws=[_law(self.NAME, ["7.2"], urls, year=2023)], max_documents=1,
        )
        assert search.queries == []
        assert report.baseline_skipped[0]["code"] == "attempt_limit"

    def test_a_robots_refusal_does_not_fall_back(self, storage, tmp_path):
        fetch = _answering(
            {DPDP_PDF: _page("x")}, robots_by_host={"www.meity.gov.in": b"User-agent: *\nDisallow: /\n"}
        )
        search = _india_code_search({"The Digital Personal Data Protection Act, 2023": DPDP_PDF})
        report = discover_economy(
            "IN", storage, data_dir=tmp_path / "data", fetch=fetch,
            get_json=search, limiter=NullLimiter(),
            baseline_dir=_one_law_baseline(
                tmp_path, "IN", [_law(self.NAME, ["7.2"], [self.MEITY], year=2023)]
            ),
            pillar=7, indicators=["7.2"], seeds=NO_CRAWLER_SEEDS,
        )
        assert search.queries == []
        assert report.baseline_skipped[0]["code"] == "robots"


class TestOneLawUnderAnotherAddress:
    """The baseline cites sso.agc.gov.sg/Act/CoA1967 where the Corpus already
    holds /Act/CoA1967?ViewType=Pdf. That is one law: Discovery reports it as
    held and never adds it again under a second id."""

    HELD = f"{SSO}/Act/CoA1967?ViewType=Pdf"
    NAME = "Companies Act 1967"

    def _run(self, storage, tmp_path, urls, answers, name=NAME):
        fetch = _answering(answers)
        report = discover_economy(
            "SG", storage, data_dir=tmp_path / "data", fetch=fetch,
            limiter=NullLimiter(), seeds=NO_CRAWLER_SEEDS, pillar=6,
            indicators=["6.2"],
            baseline_dir=_one_law_baseline(
                tmp_path, "SG", [_law(name, ["6.2"], urls, year=1967)]
            ),
        )
        return report, fetch

    def test_the_bare_act_address_finds_the_pdf_form_already_held(
        self, storage, tmp_path
    ):
        from regcompass.discovery import IN_CORPUS_STATUS

        first, _ = self._run(
            storage, tmp_path, [self.HELD], {self.HELD: _page(self.NAME)}
        )
        assert first.fetched == 1
        before = [r["document_id"] for r in storage.corpus_documents("SG")]

        bare = f"{SSO}/Act/CoA1967"
        cited = f"{SSO}/Act/CoA1967?ProvIds=P1-#pr2-"
        second, fetch = self._run(
            storage, tmp_path, [bare, cited],
            {bare: _page("Companies Act 1967 (web page)")},
        )
        assert bare not in fetch.seen and cited not in fetch.seen
        assert second.fetched == 0 and second.skipped_existing == 1
        assert [(d["url"], d["status"]) for d in second.found_by] == [
            (self.HELD, IN_CORPUS_STATUS)
        ]
        assert [r["document_id"] for r in storage.corpus_documents("SG")] == before

    def test_a_different_query_is_still_a_different_address(self):
        from regcompass.discovery import same_law_key

        assert same_law_key(f"{SSO}/Act/CoA1967/") == same_law_key(
            "https://SSO.agc.gov.sg/Act/CoA1967?ViewType=Pdf#pr1-"
        )
        assert same_law_key("https://example.gov/law?id=1") != same_law_key(
            "https://example.gov/law?id=2"
        )

    def test_every_federal_register_form_of_one_act_is_one_law(self):
        from regcompass.discovery import same_law_key

        held = same_law_key(
            "https://www.legislation.gov.au/C2004A03712/2026-06-04/2026-06-04/text/original/pdf/0"
        )
        for cited in (
            "https://www.legislation.gov.au/C2004A03712",
            "https://www.legislation.gov.au/C2004A03712/latest/versions",
            "https://www.legislation.gov.au/Details/C2004A03712",
        ):
            assert same_law_key(cited) == held, cited
        assert same_law_key("https://www.legislation.gov.au/C2004A04868") != held
        # Only that Register: another host's first segment is not a series id.
        assert same_law_key("https://example.gov/C2004A03712/a") != same_law_key(
            "https://example.gov/C2004A03712/b"
        )

    def test_a_malformed_address_is_its_own_key_never_an_error(self):
        from regcompass.discovery import same_law_key

        assert same_law_key("http://[::1/law") == "http://[::1/law"

    def test_an_address_ending_in_a_slash_names_the_document_by_its_last_segment(
        self, storage, tmp_path
    ):
        url = f"{SSO}/Acts-Supp/40-2020/"
        name = "Personal Data Protection (Amendment) Act 2020"
        report, _ = self._run(storage, tmp_path, [url], {url: _page(name)}, name=name)
        assert report.fetched == 1
        [row] = storage.corpus_documents("SG")
        assert row["document_id"] == "doc_sg_40-2020"
        assert row["title"] == name
