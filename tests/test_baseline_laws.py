"""The Baseline Law List and the Discovery Tag for every baseline Economy.

The committed config/baseline_laws.json (built by the local-only
scripts/extract_known_matrix.py from the RDTII 2.1 Round 1 and Round 2
Databases) names, per Economy, every law the 2025 baseline cites with its
Indicators and reference URLs. The KNOWN matrix covers the same ten
Economies, law names match in any script, a Document whose Source URL is a
baseline URL is recognised whatever its title, and Viet Nam and Kazakhstan,
which no baseline covers, are NEW by definition and say so."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from regcompass.config import load_baseline_laws, load_crosswalk, load_known_matrix
from regcompass.contracts import CorpusDoc, MappingRecord, Timeline
from regcompass.export import NO_BASELINE_NOTE, build_row, discovery_tag, norm_law, norm_url

CONFIG = Path(__file__).resolve().parents[1] / "config"
BASELINE_ECONOMIES = {"AU", "MY", "SG", "CN", "IN", "ID", "LA", "MN", "RU", "TH"}

MATRIX = load_known_matrix()


def rec(economy: str, indicator: str, section: str, document_id: str = "doc_x") -> MappingRecord:
    return MappingRecord(
        mapping_id=f"{document_id}:c0001::{indicator}",
        document_id=document_id,
        chunk_id=f"{document_id}:c0001",
        economy=economy,  # type: ignore[arg-type]
        indicator_id=indicator,  # type: ignore[arg-type]
        indicator_name="Indicator",
        section=section,
        subsection=None,
        verbatim_quote="A personal information handler shall not transfer personal information",
        page_number=1,
        impact="Restricts the transfer of personal information.",
        verification_status="passed",
        relationship_to_group="sole_source",
        controlling_evidence=True,
        timeline=Timeline(),
        extraction_attempts=1,
    )


def row_for(record: MappingRecord, law_name: str, source_url: str) -> dict:
    doc = CorpusDoc(
        economy=record.economy,
        law_name=law_name,
        source_url=source_url,
    )
    return build_row(
        record, doc, load_crosswalk(), MATRIX, {(record.chunk_id, record.indicator_id): 0.6},
        {record.chunk_id: 1},
    )


class TestBaselineLawList:
    raw = json.loads((CONFIG / "baseline_laws.json").read_text(encoding="utf-8"))

    def test_covers_the_ten_baseline_economies_and_not_vn_kz(self):
        assert set(self.raw["economies"]) == BASELINE_ECONOMIES
        assert "Viet Nam" in self.raw["_comment"] and "Kazakhstan" in self.raw["_comment"]
        assert "no 2025 baseline" in self.raw["_comment"].lower()
        assert self.raw["sources"]

    def test_every_economy_spans_all_twelve_pillars(self):
        for code, econ in self.raw["economies"].items():
            pillars = {int(i.split(".")[0]) for law in econ["laws"] for i in law["indicators"]}
            assert pillars == set(range(1, 13)), code

    def test_entries_follow_the_contract(self):
        for code, econ in self.raw["economies"].items():
            assert isinstance(econ["name"], str) and econ["name"]
            keys = [law["law_key"] for law in econ["laws"]]
            assert len(keys) == len(set(keys)), f"{code}: one entry per distinct law"
            for law in econ["laws"]:
                assert set(law) == {"law", "law_key", "year", "indicators", "urls", "url_pairing"}
                assert law["law_key"] == norm_law(law["law"])
                assert law["year"] is None or isinstance(law["year"], int)
                assert law["indicators"] and all("." in i for i in law["indicators"])
                assert all(u.startswith(("http://", "https://")) for u in law["urls"])
                assert law["url_pairing"] in {"single", "positional", "none"}
                assert (law["url_pairing"] == "none") == (not law["urls"])

    def test_pivotal_laws_with_their_urls(self):
        econ = load_baseline_laws()
        pipl = next(law for law in econ["CN"]["laws"] if "中华人民共和国个人信息保护法" in law["law"])
        assert {"6.2", "6.4", "7.1", "7.4"} <= set(pipl["indicators"])
        assert pipl["url_pairing"] == "single"
        uu27 = next(law for law in econ["ID"]["laws"] if law["law"] == "Law No.27 on Personal Data Protection 2022")
        assert uu27["year"] == 2022
        assert uu27["urls"][0] == "https://peraturan.bpk.go.id/Details/229798/uu-no-27-tahun-2022"


class TestKnownMatrixCoverage:
    def test_matrix_covers_the_ten_economies_and_records_vn_kz(self):
        assert set(MATRIX["database"]) == BASELINE_ECONOMIES
        assert MATRIX["no_baseline_economies"] == ["KZ", "TL", "VN"]


class TestUnicodeNormLaw:
    def test_latin_keys_unchanged(self):
        assert norm_law("Personal Data Protection Act (Act 709) 2010") == (
            "personal data protection act 2010"
        )
        assert norm_law("Telecommunications Act 1999 - Part 4; s.30") == (
            "telecommunications act 1999 part 4 s 30"
        )

    def test_non_latin_letters_survive(self):
        assert norm_law("《中华人民共和国个人信息保护法》") == "中华人民共和国个人信息保护法"
        # Thai vowel and tone marks are kept, not turned into gaps
        assert norm_law("พระราชบัญญัติคุ้มครองข้อมูลส่วนบุคคล พ.ศ. 2562") == (
            "พระราชบัญญัติคุ้มครองข้อมูลส่วนบุคคล พ ศ 2562"
        )
        assert norm_law("Федеральный закон «О персональных данных»") == (
            "федеральный закон о персональных данных"
        )

    def test_full_width_parentheticals_are_stripped(self):
        assert norm_law("人口健康信息的管理措施（试行）") == "人口健康信息的管理措施"


class TestDiscoveryTagAcrossEconomies:
    def test_chinese_titled_pipl_row_is_known(self):
        # The database cites PIPL Articles 38-40 under 6.4 and the Act as a
        # whole under 7.1; a Chinese-only title must match both.
        tag, _ = discovery_tag(rec("CN", "6.4", "Chapter 3 s. 38"), "中华人民共和国个人信息保护法", MATRIX)
        assert tag == "KNOWN"
        tag, _ = discovery_tag(rec("CN", "7.1", "Chapter 1 s. 1"), "中华人民共和国个人信息保护法", MATRIX)
        assert tag == "KNOWN"

    def test_english_titled_pipl_row_is_known(self):
        tag, _ = discovery_tag(
            rec("CN", "7.1", "Chapter 1 s. 1"),
            "Personal Information Protection Law of the People's Republic of China",
            MATRIX,
        )
        assert tag == "KNOWN"

    def test_indonesian_uu_27_2022_row_is_known(self):
        # Portal title in Indonesian, database title in English.
        tag, _ = discovery_tag(rec("ID", "6.4", "s. 56"), "UU Nomor 27 Tahun 2022", MATRIX)
        assert tag == "KNOWN"
        tag, scope = discovery_tag(rec("ID", "6.4", "s. 2"), "UU Nomor 27 Tahun 2022", MATRIX)
        assert (tag, scope) == ("NEW", "provision")

    def test_indonesian_identity_needs_the_same_number_and_year(self):
        tag, scope = discovery_tag(rec("ID", "6.4", "s. 56"), "UU Nomor 28 Tahun 2022", MATRIX)
        assert (tag, scope) == ("NEW", "law")

    def test_thai_native_title_is_known(self):
        # The database writes "Personal Data Protection Act B.E.2562
        # (พระราชบัญญัติคุ้มครองข้อมูลส่วนบุคคล พ.ศ. 2562)"; the portal title is the Thai half.
        tag, _ = discovery_tag(
            rec("TH", "6.2", "s. 28"), "พระราชบัญญัติคุ้มครองข้อมูลส่วนบุคคล พ.ศ.2562", MATRIX
        )
        assert tag == "KNOWN"
        tag, scope = discovery_tag(rec("TH", "6.2", "s. 28"), "พระราชบัญญัติสิทธิบัตร พ.ศ. 2522", MATRIX)
        assert (tag, scope) == ("NEW", "provision")

    def test_quoted_cyrillic_title_is_known(self):
        # Mongolia 10.1 cites a list by its English name and its quoted
        # Mongolian name; the Mongolian name alone must match.
        native = "Монгол Улсын хилээр нэвтрүүлэхийг хориглосон барааны кодлосон жагсаалт"
        entry = next(e for e in MATRIX["database"]["MN"]["10.1"] if native in e["law"])
        section = f"s. {entry['sections'][0]}" if entry["sections"] else "s. 1"
        tag, _ = discovery_tag(rec("MN", "10.1", section), native, MATRIX)
        assert tag == "KNOWN"

    def test_law_absent_from_the_baseline_is_new(self):
        tag, scope = discovery_tag(rec("CN", "6.4", "s. 3"), "中华人民共和国虚构数据法", MATRIX)
        assert (tag, scope) == ("NEW", "law")
        tag, scope = discovery_tag(rec("TH", "7.1", "s. 3"), "Totally Novel Data Act 2025", MATRIX)
        assert (tag, scope) == ("NEW", "law")

    def test_source_url_match_makes_known_whatever_the_title(self):
        record = rec("CN", "7.1", "Chapter 1 s. 1")
        # scheme, www. and a trailing slash do not matter
        url = "http://www.gov.cn/xinwen/2021-08/20/content_5632486.htm/"
        assert discovery_tag(record, "个人信息保护法全文", MATRIX)[0] == "NEW"
        assert discovery_tag(record, "个人信息保护法全文", MATRIX, url)[0] == "KNOWN"
        row = row_for(record, "个人信息保护法全文", url)
        assert row["Discovery Tag"] == "KNOWN"

    def test_spa_route_fragment_is_kept(self):
        a = norm_url("https://searchlaw.ocs.go.th/council-of-state/#/public/doc/abc")
        b = norm_url("https://searchlaw.ocs.go.th/council-of-state/#/public/doc/xyz")
        assert a != b and a.endswith("#/public/doc/abc")
        assert norm_url("https://gov.cn/a.htm#section2") == "gov.cn/a.htm"

    def test_portal_root_url_identifies_no_law(self):
        assert norm_url("https://www.legislation.gov.au/") == ""
        assert norm_url("https://www.gov.cn/xinwen/2021-08/20/content_5632486.htm") == (
            "gov.cn/xinwen/2021-08/20/content_5632486.htm"
        )

    def test_positional_url_is_tied_to_its_own_law(self):
        # ID 7.2 lists three Laws with three URLs (the database cites s. 27);
        # the second URL is UU 19/2016, spelled two ways across rows
        url = "https://peraturan.bpk.go.id/Details/37582/uu-no-19-tahun-2016"
        assert discovery_tag(rec("ID", "7.2", "s. 27"), "Dokumen", MATRIX)[0] == "NEW"
        assert discovery_tag(rec("ID", "7.2", "s. 27"), "Dokumen", MATRIX, url)[0] == "KNOWN"

    @pytest.mark.parametrize("economy", ["VN", "KZ", "TL"])
    def test_no_baseline_economy_rows_are_new_with_the_note(self, economy):
        record = rec(economy, "7.1", "Article 3")
        row = row_for(record, "Law on Personal Data Protection", "https://vbpl.vn/doc/1")
        assert row["Discovery Tag"] == "NEW"
        assert NO_BASELINE_NOTE in row["Notes"]
        assert NO_BASELINE_NOTE == "No 2025 baseline exists for this Economy"

    def test_baseline_economy_rows_carry_no_such_note(self):
        row = row_for(rec("CN", "7.1", "Chapter 1 s. 1"), "中华人民共和国个人信息保护法",
                      "https://www.cac.gov.cn/2021-08/20/c_1631050028355286.htm")
        assert NO_BASELINE_NOTE not in row["Notes"]
