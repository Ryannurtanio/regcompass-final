"""Official sources for the Economies with no Portal crawler (TH, VN, RU, MN,
KZ): the allowed hosts, the addresses seeded per law, plain-http hosts for
the Russian Federation, and what a Discovery by Pillar does with them. Also
Indonesia's statute PDFs, seeded in place of the summary pages the baseline
gives.

Offline throughout: every Portal answer is recorded.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest

pytest.importorskip("httpx", reason="Discovery tests need the `live` extra (httpx)")

from regcompass.config import load_baseline_laws, load_crawl_seeds, load_portals  # noqa: E402
from regcompass.contracts import CrawlFamily, CrawlSeedsEconomy, PortalConfig  # noqa: E402
from regcompass.discovery import discover_economy  # noqa: E402
from regcompass.storage import Storage  # noqa: E402

from test_discovery import NullLimiter  # noqa: E402
from test_discovery_by_pillar import _answering, _law, _page, _write_baseline  # noqa: E402

ID_DETAILS = (
    Path(__file__).resolve().parent / "fixtures" / "portals" / "id"
    / "details_downloads_2026-09-29.json"
)
ID_PORTAL = "peraturan.bpk.go.id"
TH_PDPA_EN = "https://mdes.go.th/law/detail/3577-Personal-Data-Protection-Act-B-E--2562--2019-"
KZ_PD = "https://natlex.ilo.org/dyn/natlex2/natlex2/files/download/96710/KAZ-96710.pdf"
RU_KREMLIN = "http://kremlin.ru/acts/bank/24154/print"
RU_PRAVO = "http://pravo.gov.ru/proxy/ips/?doc_itself=&nd=102108261&page=1"


@pytest.fixture()
def storage(tmp_path):
    s = Storage(tmp_path / "sources.db")
    s.apply_schema()
    yield s
    s.close()


def _documents(storage):
    rows = storage.conn.execute("SELECT * FROM documents").fetchall()
    return [dict(r) for r in rows]


def _seeds(**families):
    return CrawlSeedsEconomy(
        rate_limit_seconds=0.01,
        families={name: CrawlFamily(**f) for name, f in families.items()},
    )


class TestTheCommittedConfiguration:
    def test_each_economy_has_its_official_hosts(self):
        portals = load_portals()
        assert {"www.mdes.go.th", "mdes.go.th"} <= set(portals["TH"].hosts)
        assert {"congbao.chinhphu.vn", "congbaocdn.chinhphu.vn"} <= set(portals["VN"].hosts)
        assert "vanban.chinhphu.vn" not in portals["VN"].hosts
        assert portals["RU"].hosts == ["pravo.gov.ru", "kremlin.ru", "eec.eaeunion.org"]
        assert "legalinfo.mn" in portals["MN"].hosts
        assert "natlex.ilo.org" in portals["KZ"].hosts
        assert "eec.eaeunion.org" in portals["KZ"].hosts
        # The Union's legal portal answers robots.txt with a 500: a refusal.
        assert not any("docs.eaeunion.org" in p.hosts for p in portals.values())
        assert "ILO NATLEX, intergovernmental repository of official texts" in (
            portals["KZ"].notes
        )

    def test_plain_http_is_allowed_for_the_two_russian_hosts_only(self):
        portals = load_portals()
        allowed = {e: p.http_hosts for e, p in portals.items() if p.http_hosts}
        assert allowed == {"RU": ["pravo.gov.ru", "kremlin.ru"]}
        assert portals["RU"].fetch_timeout_seconds == 60

    def test_every_seeded_address_is_on_an_allowed_host(self):
        from urllib.parse import urlsplit

        portals = load_portals()
        seeds = load_crawl_seeds()
        for economy in ("TH", "VN", "RU", "MN", "KZ"):
            families = [f for f in seeds[economy].families.values() if f.urls]
            assert families, economy
            for family in families:
                for url in family.urls:
                    host = urlsplit(url).netloc
                    assert host in portals[economy].hosts, url
                    # Plain http only where the Portal entry allows it.
                    assert url.startswith("https://") or host in portals[economy].http_hosts, url

    def test_every_seed_serves_indicators_of_the_framework(self):
        from regcompass.config import load_indicators

        known = set(load_indicators())
        for economy, seeds in load_crawl_seeds().items():
            for name, family in seeds.families.items():
                assert set(family.indicators) <= known, (economy, name, family.indicators)

    def test_no_viet_nam_seed_is_an_instrument_no_longer_in_force(self):
        """Law No. 116/2025/QH15, Article 44(2), ended the Law on Cybersecurity
        No. 24/2018/QH14; Decree No. 356/2025/ND-CP, Article 42(2), ended
        Decree No. 13/2023/ND-CP. A repealed provision is no evidence."""
        families = load_crawl_seeds()["VN"].families.values()
        laws = " ".join(f.law for f in families)
        assert "24/2018/QH14" not in laws and "13/2023/ND-CP" not in laws
        serving = {i: {f.law for f in families if i in f.indicators} for i in ("6.2", "7.1", "7.2")}
        assert any("116/2025/QH15" in law for law in serving["6.2"] | serving["7.2"])
        assert any("91/2025/QH15" in law for law in serving["7.1"])

    def test_every_baseline_law_named_by_a_seed_exists(self):
        from regcompass.discovery import _fold

        baseline = load_baseline_laws()
        for economy, seeds in load_crawl_seeds().items():
            for family in seeds.families.values():
                if not family.baseline_law:
                    continue
                names = [_fold(law["law"]) for law in baseline[economy]["laws"]]
                assert any(n.startswith(_fold(family.baseline_law)) for n in names), (
                    economy, family.baseline_law,
                )

    def test_translations_are_seeded_as_english_and_say_so(self):
        seeds = load_crawl_seeds()
        kz = seeds["KZ"].families["data_protection"]
        assert kz.language == "English" and "translation" in kz.law
        for family in seeds["KZ"].families.values():
            if any("natlex.ilo.org" in url for url in family.urls):
                assert family.language == "English" and "translation" in family.law
        for family in seeds["TH"].families.values():
            assert family.language == "English" and "translation" in family.law

    def test_kazakhstan_offers_english_for_a_translation_added_by_hand(self):
        """Add document offers an Economy's Languages, the first as the
        default. Russian stays the default, English is offered for a NATLEX
        translation, and a Russian or Kazakh scan is read exactly as before."""
        from regcompass.languages import garbage_ocr_languages

        languages = load_portals()["KZ"].languages
        assert languages[0] == "Russian" and "English" in languages[1:]
        cyr = "Осы Заң дербес деректерді жинауға байланысты қатынастарды реттейді " * 3
        for language in ("Russian", "Kazakh", None):
            assert garbage_ocr_languages(language, "KZ", cyr, languages) == (
                garbage_ocr_languages(language, "KZ", cyr, ("Russian", "Kazakh"))
            )

    def test_the_seeds_serve_the_right_pillars(self):
        seeds = load_crawl_seeds()
        th = seeds["TH"].families
        assert th["data_protection"].covers(7) and th["data_protection"].covers(6)
        assert th["computer_crime"].covers(7) and not th["computer_crime"].covers(6)
        assert th["electronic_transactions"].covers(12)
        assert not th["electronic_transactions"].covers(7)
        assert seeds["KZ"].families["data_protection"].covers(7)


class TestIndonesianStatutePdfs:
    """The baseline gives most Indonesian laws a /Details/ page of the
    Portal: metadata and an abstract, not the articles. Each is seeded with
    the statute PDF that page links, so a Verbatim Quote is a provision and
    not the Portal's summary."""

    # Baseline laws with no seed, and why (the seeds file says the same).
    UNSEEDED = {
        # PDFs of page images: OCR end to end, 739, 909 and 155 pages.
        "Regulation of the Government of the Republic of Indonesia No.5 on Implementation",
        "Regulation of the Government of the Republic of Indonesia No.5 on Risk-Based",
        "Regulation of Minister of Communications and Informatics No.5 on Telecommunications",
        "Regulation of the Minister of Communication and Informatics of the Republic of Indonesia No.13",
        # The /Details/ page given is a regency regulation, not this law.
        "Regulation of the Government of the Republic of Indonesia No.34 on the Employment",
        "Ministerial Regulation No.14 on Electronic Systems",
        # The file is under a path jdih.komdigi.go.id's robots.txt disallows.
        "Regulation of the Government of the Republic of Indonesia No. 17 on the Governance",
        # The first baseline address is already the statute PDF.
        "Regulation of the Government of the Republic of Indonesia No.46 on Health",
    }

    def _statute_seeds(self):
        return [f for f in load_crawl_seeds()["ID"].families.values() if f.urls]

    def _laws(self):
        return load_baseline_laws()["ID"]["laws"]

    def _recorded(self):
        return json.loads(ID_DETAILS.read_text(encoding="utf-8"))["pages"]

    @staticmethod
    def _summary_page(url: str) -> bool:
        return "/Details/" in url or "/produk_hukum/view/" in url

    def test_every_seed_is_a_statute_pdf_over_https_on_the_portal(self):
        hosts = load_portals()["ID"].hosts
        seeds = self._statute_seeds()
        assert len(seeds) >= 50
        for family in seeds:
            (url,) = family.urls
            parts = urlsplit(url)
            assert url.startswith("https://") and parts.netloc in hosts, url
            assert parts.netloc == ID_PORTAL and parts.path.startswith("/Download/"), url
            assert parts.path.endswith(".pdf"), url
            assert not self._summary_page(url), url

    def test_each_seed_names_exactly_one_baseline_law(self):
        from regcompass.discovery import _fold

        names = [_fold(law["law"]) for law in self._laws()]
        served = []
        for family in self._statute_seeds():
            assert family.baseline_law, family.law
            matches = [n for n in names if n.startswith(_fold(family.baseline_law))]
            assert len(matches) == 1, (family.baseline_law, matches)
            served.append(matches[0])
        assert len(served) == len(set(served)), "two seeds serve one baseline law"

    def test_each_seed_is_the_statute_link_of_the_page_the_baseline_gives(self):
        """Read off the Portal, not composed: the download id is not the
        /Details/ id. And no seed is a scan the OCR lane would read end to
        end."""
        from regcompass.discovery import _fold

        recorded = self._recorded()
        downloads = {page.get("download") for page in recorded.values()}
        for family in self._statute_seeds():
            law = next(
                law for law in self._laws()
                if _fold(law["law"]).startswith(_fold(family.baseline_law))
            )
            path = urlsplit(family.urls[0]).path
            cited = [
                m.group(1) for u in law["urls"]
                for m in [re.search(r"peraturan\.bpk\.go\.id/(?:Home/)?Details/(\d+)", u)] if m
            ]
            if cited:
                assert path in {recorded[i].get("download") for i in cited}, family.law
            else:
                assert path in downloads, family.law
            page = next(p for p in recorded.values() if p.get("download") == path)
            assert page["read_by_ocr"] is False and page["text_chars"] > 4000, family.law
            assert set(family.indicators) == set(law["indicators"]), family.law

    def test_every_summary_page_the_baseline_gives_is_seeded_or_says_why_not(self):
        from regcompass.discovery import _fold

        keys = [_fold(f.baseline_law) for f in self._statute_seeds()]
        unseeded = [_fold(u) for u in self.UNSEEDED]
        for law in self._laws():
            if not any(self._summary_page(u) and "kominfo.go.id" not in u for u in law["urls"]):
                continue
            name = _fold(law["law"])
            seeded = any(name.startswith(k) for k in keys)
            excused = any(name.startswith(k) for k in unseeded)
            assert seeded != excused, law["law"]

    def test_a_discovery_fetches_the_statute_and_never_the_summary(self, storage, tmp_path):
        """Pillar 7, Indicator 7.1, on the committed baseline and seeds: the
        Personal Data Protection Law comes in as its statute PDF."""
        seeds = load_crawl_seeds()["ID"]
        pdfs = {f.urls[0]: _page(f.law) for f in seeds.families.values() if f.urls}
        fetch = _answering(pdfs)
        report = discover_economy(
            "ID", storage, data_dir=tmp_path / "data", fetch=fetch,
            limiter=NullLimiter(), pillar=7, indicators=["7.1"],
        )
        assert not any("/Details/" in u for u in fetch.seen)
        uu27 = seeds.families["uu_27_2022"].urls[0]
        assert uu27.endswith("/Download/224884/UU%20Nomor%2027%20Tahun%202022.pdf")
        assert uu27 in {d["source_url"] for d in _documents(storage)}
        assert report.fetched >= 2


    def test_a_summary_page_is_never_fetched_and_a_law_with_only_one_is_a_gap(
        self, storage, tmp_path
    ):
        details = f"https://{ID_PORTAL}/Details/229798/uu-no-27-tahun-2022"
        home = f"https://{ID_PORTAL}/Home/Details/37536/uu-no-13-tahun-2016"
        view = "https://jdih.komdigi.go.id/produk_hukum/view/id/965/t/pp+17+2025"
        pdf = f"https://{ID_PORTAL}/Download/224884/UU%20Nomor%2027%20Tahun%202022.pdf"
        laws = [
            _law("Law No.27 on Personal Data Protection 2022", ["7.1"], [details, pdf]),
            _law("Law No.13 on Patent 2016", ["7.1"], [home]),
            _law("Regulation No. 17 on Child Protection 2025", ["7.1"], [view]),
        ]
        fetch = _answering({pdf: _page("Undang-Undang Nomor 27 Tahun 2022")})
        report = discover_economy(
            "ID", storage, data_dir=tmp_path / "data", fetch=fetch,
            limiter=NullLimiter(),
            baseline_dir=_write_baseline(tmp_path, {"ID": {"name": "Indonesia", "laws": laws}}),
            pillar=7, indicators=["7.1"],
            seeds=_seeds(other={"law": "X", "indicators": ["1.4"], "urls": [pdf]}),
        )
        assert not {details, home, view} & set(fetch.seen)
        assert [d["source_url"] for d in _documents(storage)] == [pdf]
        skipped = {s["law"]: s for s in report.baseline_skipped}
        for name in ("Law No.13 on Patent 2016", "Regulation No. 17 on Child Protection 2025"):
            assert skipped[name]["code"] == "summary_page"
            assert "only the Portal's summary page" in skipped[name]["reason"]

    def test_the_regency_look_alikes_are_gaps_not_documents(self, storage, tmp_path):
        """The baseline's /Details/ pages for PP 34/2021 and for "Ministerial
        Regulation No.14 ... 2018" are regency regulations of Jepara and
        Lumajang. Neither is asked for any more."""
        seeds = load_crawl_seeds()["ID"]
        pdfs = {f.urls[0]: _page(f.law) for f in seeds.families.values() if f.urls}
        codes = {}
        for pillar in (3, 12):
            fetch = _answering(pdfs)
            report = discover_economy(
                "ID", storage, data_dir=tmp_path / f"data{pillar}", fetch=fetch,
                limiter=NullLimiter(), pillar=pillar,
            )
            assert not any(_is_summary(u) for u in fetch.seen), pillar
            codes.update({s["law"]: s["code"] for s in report.baseline_skipped})
        assert codes[
            "Regulation of the Government of the Republic of Indonesia No.34 on the"
            " Employment of Foreign Workers in Indonesia 2021"
        ] == "summary_page"
        assert codes["Ministerial Regulation No.14 on Electronic Systems and Transactions 2018"] == (
            "summary_page"
        )
        assert not any("lumajang" in d["source_url"] for d in _documents(storage))


def _is_summary(url: str) -> bool:
    return "/Details/" in url or "/produk_hukum/view/" in url

class TestTheContracts:
    def test_an_http_host_must_be_one_of_the_portal_hosts(self):
        with pytest.raises(ValueError, match="http_hosts"):
            PortalConfig(
                economy="RU", official_name="Russian Federation", languages=["Russian"],
                hosts=["pravo.gov.ru"], http_hosts=["kremlin.ru"], strategy="manual",
            )

    def test_a_family_of_urls_names_its_law_and_indicators(self):
        with pytest.raises(ValueError, match="names its law"):
            CrawlFamily(urls=["https://mdes.go.th/law/detail/1-"])

    def test_a_seed_language_is_an_organizer_language(self):
        with pytest.raises(ValueError, match="organizer"):
            CrawlFamily(
                urls=["https://mdes.go.th/law/detail/1-"], law="X", indicators=["7.1"],
                language="Malay",
            )


class TestSeededAddressesInADiscoveryByPillar:
    def test_a_seed_is_tried_first_for_its_baseline_law_in_english(self, storage, tmp_path):
        gazette = "http://www.ratchakitcha.soc.go.th/DATA/PDF/2562/A/069/T_0052.PDF"
        laws = [_law("Personal Data Protection Act B.E.2562 (พระราชบัญญัติ)", ["7.1"], [gazette])]
        fetch = _answering({TH_PDPA_EN: _page("Personal Data Protection Act B.E. 2562")})
        report = discover_economy(
            "TH", storage, data_dir=tmp_path / "data", fetch=fetch,
            limiter=NullLimiter(), baseline_dir=_write_baseline(tmp_path, {"TH": {"name": "Thailand", "laws": laws}}),
            pillar=7, indicators=["7.1"],
            seeds=_seeds(pdpa={
                "law": "Personal Data Protection Act B.E. 2562 (2019), unofficial English translation",
                "baseline_law": "Personal Data Protection Act B.E.2562",
                "indicators": ["7.1"], "language": "English", "urls": [TH_PDPA_EN],
            }),
        )
        assert not any("ratchakitcha" in u for u in fetch.seen)
        assert TH_PDPA_EN in fetch.seen
        (found,) = report.found_by
        assert found["found_by"] == "baseline 7.1"
        (doc,) = _documents(storage)
        assert doc["language"] == "English"
        assert "English translation" in found["title"]

    def test_a_seed_with_no_baseline_is_its_own_law(self, storage, tmp_path):
        fetch = _answering({KZ_PD: _page("On Personal Data and their Protection")})
        report = discover_economy(
            "KZ", storage, data_dir=tmp_path / "data", fetch=fetch,
            limiter=NullLimiter(), baseline_dir=_write_baseline(tmp_path, {}),
            pillar=7, indicators=["7.1", "7.2"],
            seeds=_seeds(pd={
                "law": "Law No. 94-V On Personal Data, unofficial English translation",
                "indicators": ["7.1"], "language": "English", "urls": [KZ_PD],
            }),
        )
        assert "https://natlex.ilo.org/robots.txt" in fetch.seen
        (found,) = report.found_by
        assert found["found_by"] == "official source list 7.1"
        (doc,) = _documents(storage)
        assert doc["language"] == "English" and doc["source_url"] == KZ_PD

    def test_a_seed_for_another_pillar_is_not_fetched(self, storage, tmp_path):
        fetch = _answering({KZ_PD: _page("On Personal Data")})
        report = discover_economy(
            "KZ", storage, data_dir=tmp_path / "data", fetch=fetch,
            limiter=NullLimiter(), baseline_dir=_write_baseline(tmp_path, {}),
            pillar=5, seeds=_seeds(pd={"law": "PD", "indicators": ["7.1"], "urls": [KZ_PD]}),
        )
        assert KZ_PD not in fetch.seen
        assert report.fetched == 0

    def test_nothing_fetched_says_upload_by_hand(self, storage, tmp_path):
        fetch = _answering({KZ_PD: 404})
        report = discover_economy(
            "KZ", storage, data_dir=tmp_path / "data", fetch=fetch,
            limiter=NullLimiter(), baseline_dir=_write_baseline(tmp_path, {}),
            pillar=7, indicators=["7.1"],
            seeds=_seeds(pd={"law": "PD", "indicators": ["7.1"], "urls": [KZ_PD]}),
        )
        assert report.fetched == 0
        assert any("upload the laws by hand" in n for n in report.notes)

    def test_the_crawler_stage_leaves_url_families_alone(self, storage, tmp_path):
        from regcompass.discovery import _seeds_for_pillar, DiscoveryReport

        report = DiscoveryReport(economy="SG", strategy="curl_cffi_ladder", run_id="r", refresh=False)
        report.pillar = 7
        seeds = _seeds(
            acts={"acts": ["PDPA2012"]},
            fixed={"law": "X", "indicators": ["7.1"], "urls": ["https://sso.agc.gov.sg/Act/X"]},
        )
        portal = load_portals()["SG"]
        chosen = _seeds_for_pillar(report, portal, True, seeds, None, 5)
        assert list(chosen.families) == ["acts"]


class TestAHomePageIsNoLawsAddress:
    def test_a_baseline_home_page_address_is_never_fetched(self, storage, tmp_path):
        """The 2025 baseline gives Decision No. 115 of the Eurasian Economic
        Commission the Commission's front page as an address. Fetched, that
        page would be filed under the Decision's name."""
        home = "https://eec.eaeunion.org/"
        mirror = "https://www.alta.ru/tamdoc/12kr0049/"
        laws = [_law("Decision of the Eurasian Economic Commission No. 115", ["1.4"], [mirror, home])]
        fetch = _answering({home: _page("Eurasian Economic Commission")})
        report = discover_economy(
            "RU", storage, data_dir=tmp_path / "data", fetch=fetch,
            limiter=NullLimiter(),
            baseline_dir=_write_baseline(tmp_path, {"RU": {"name": "Russian Federation", "laws": laws}}),
            pillar=1, indicators=["1.4"],
            seeds=_seeds(pd={"law": "PD", "indicators": ["7.1"], "urls": [RU_KREMLIN]}),
        )
        assert home not in fetch.seen
        assert report.fetched == 0 and not _documents(storage)
        (skip,) = report.baseline_skipped
        assert skip["code"] == "not_allowed_host" and "www.alta.ru" in skip["reason"]


class TestPlainHttpForTheRussianHosts:
    def _ru(self, storage, tmp_path, fetch, urls, laws=None):
        return discover_economy(
            "RU", storage, data_dir=tmp_path / "data", fetch=fetch,
            limiter=NullLimiter(),
            baseline_dir=_write_baseline(tmp_path, {"RU": {"name": "Russian Federation", "laws": laws or []}}),
            pillar=7, indicators=["7.1"],
            seeds=_seeds(pd={"law": "Federal Law No. 152-FZ On Personal Data", "indicators": ["7.1"], "urls": urls}),
        )

    def test_robots_is_read_over_http_and_the_record_says_so(self, storage, tmp_path):
        fetch = _answering({RU_KREMLIN: _page("Федеральный закон О персональных данных")})
        report = self._ru(storage, tmp_path, fetch, [RU_KREMLIN, RU_PRAVO])
        assert "http://kremlin.ru/robots.txt" in fetch.seen
        assert not any(u.startswith("https://") for u in fetch.seen)
        assert report.fetched == 1
        assert any("plain http" in n and "kremlin.ru" in n for n in report.notes)

    def test_a_windows_1251_pravo_page_is_read_as_russian(self, storage, tmp_path):
        page = (
            '<!DOCTYPE html><html><head><title>О персональных данных</title>'
            '<meta http-equiv="Content-Type" content="text/html; charset=windows-1251" />'
            "</head><body><p>Федеральный закон О персональных данных</p>"
            "<p>Статья 1. Сфера действия настоящего Федерального закона</p>"
            "<p>Настоящим Федеральным законом регулируются отношения, связанные"
            " с обработкой персональных данных.</p></body></html>"
        ).encode("windows-1251")
        fetch = _answering({RU_KREMLIN: 404, RU_PRAVO: page})
        report = self._ru(storage, tmp_path, fetch, [RU_KREMLIN, RU_PRAVO])
        assert "http://pravo.gov.ru/robots.txt" in fetch.seen
        assert report.fetched == 1
        (doc,) = _documents(storage)
        assert "Статья 1. Сфера действия" in doc["full_text"]
        assert doc["language"] == "Russian"

    def test_an_http_address_on_any_other_host_reads_robots_over_https(self):
        from regcompass.crawl import fetch_one

        other = "http://www.gprocurement.go.th/act.pdf"
        fetch = _answering({other: _page("Some Act B.E. 2560")})
        fetch_one(other, "TH", fetch=fetch, limiter=NullLimiter())
        assert fetch.seen[0] == "https://www.gprocurement.go.th/robots.txt"

    def test_add_by_url_reads_robots_over_http_for_a_russian_host(self):
        from regcompass.crawl import fetch_one

        fetch = _answering({RU_KREMLIN: _page("Федеральный закон")})
        fetch_one(RU_KREMLIN, "RU", fetch=fetch, limiter=NullLimiter())
        assert fetch.seen[0] == "http://kremlin.ru/robots.txt"

    def test_the_russian_hosts_get_the_longer_timeout(self, storage, tmp_path, monkeypatch):
        import httpx

        timeouts = []

        def handler(request):
            timeouts.append(request.extensions.get("timeout", {}).get("read"))
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nDisallow:\n")
            return httpx.Response(200, content=_page("Федеральный закон О персональных данных"))

        discover_economy(
            "RU", storage, data_dir=tmp_path / "data", transport=httpx.MockTransport(handler),
            limiter=NullLimiter(),
            baseline_dir=_write_baseline(tmp_path, {}), pillar=7, indicators=["7.1"],
            seeds=_seeds(pd={"law": "152-FZ", "indicators": ["7.1"], "urls": [RU_KREMLIN]}),
        )
        assert timeouts and set(timeouts) == {60}


class TestTheThaiPdpaBrokenTextLayer:
    """mdes.go.th/law/detail/3541 (the Thai PDPA) is a Word export whose text
    layer reads every sara aa as sara am, never spells the section word and
    carries the watermark about 2,000 times. It must be read by OCR."""

    def test_the_broken_pattern_is_judged_garbage(self):
        from regcompass.contracts import CanonicalText, PageSpan
        from regcompass.shortlist import garbage_text_layer

        watermark = "สำนักงำนคณะกรรมกำรกฤษฎีกำ"
        body = (
            "พระรำชบัญญัติคุ้มครองข้อมูลส่วนบุคคล พ.ศ. ๒๕๖๒ "
            "มำตรำ ๑ พระรำชบัญญัตินี้เรียกว่ำ พระรำชบัญญัติคุ้มครองข้อมูลส่วนบุคคล "
            "ข้อมูลส่วนบุคคล หมำยควำมว่ำ ข้อมูลเกี่ยวกับบุคคลซึ่งทำให้สำมำรถระบุตัวบุคคลนั้นได้"
        )
        lines = []
        for i in range(2002):
            lines.append(watermark)
            if i % 4 == 0:
                lines.append(body)
        text = "\n".join(lines)
        assert "มาตรา" not in text
        pages = 37
        step = len(text) // pages
        layer = CanonicalText(
            document_id="th_pdpa", source_sha256="0" * 64, extractor="pdfplumber",
            extractor_version="x", full_text=text,
            pages=[
                PageSpan(page_number=i + 1, char_start=i * step,
                         char_end=len(text) if i == pages - 1 else (i + 1) * step)
                for i in range(pages)
            ],
        )
        assert garbage_text_layer(layer, "Thai") is not None
