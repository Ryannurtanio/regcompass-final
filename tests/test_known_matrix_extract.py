"""Section-reference parsing for the KNOWN matrix.

The old single-token SECTION_RE dropped every token after the first number of
a conjunction or range ("section 39 and 40" -> {39}, "Sections 15-24" -> {15}),
which shipped false NEW tags for provisions ESCAP's own database cites (SG 7.5
CPC s.40, MY 7.2 CSA s.24 among others). These tests gate parse_sections with
the VERBATIM comment strings from the ESCAP Round 1 Database, so the defect
cannot silently return; the committed config/known_matrix.json is regenerated
from the local-only xlsx whenever the parser changes.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from extract_known_matrix import parse_sections  # noqa: E402

# Verbatim from the ESCAP RDTII 2.1 Round 1 Database (Singapore sheet, 7.5;
# \xa0 normalized to space exactly as extract_known_matrix.py does).
SG_75_COMMENT = (
    "Under the Criminal Procedure Code, (section 39 and 40) police officers "
    "have the power to access computers and the power to access decrypted "
    "information. Moreover, (section 20) empowers the police to order the "
    '"production of any document or other thing" for the purposes of criminal '
    "investigations. Any person who obstructs the lawful exercise by a police "
    "officer or an authorised person shall be guilty of an offence and shall "
    "be liable on conviction to a fine not exceeding $10,000 or to "
    "imprisonment for a term not exceeding 3 years or to both. "
)

# Verbatim from the Malaysia sheet, 7.2 (the Cyber Security Act 2024 cell).
MY_72_COMMENT = (
    "Malaysia previous cybersecurity law was based on Computer Crimes Act "
    "(Act 563) 1997, it criminalizes various cybercrimes, such as "
    "unauthorized access to computers, modification of computer content, and "
    "other malicious cyber activities. However, it mainly addresses cyber "
    "offenses rather than a broader cybersecurity framework. The Cyber "
    "Security Act 2024 now provides a comprehensive framework to address "
    "national cybersecurity threats and regulate service providers.\n\n"
    "The new Cyber Security Act (Act 854) 2024 enhances Malaysia’s "
    "national cybersecurity efforts by:\n"
    "\t1. Establishing a National Cyber Security Committee, responsible for "
    "overseeing cybersecurity policies and national coordination (Part III - "
    "Duties and Powers of Chief Executive).\n"
    "\t2. Defining roles for critical information infrastructure entities "
    "and mandating them to comply with cybersecurity risk assessments, "
    "audits, and notifications of cybersecurity incidents. Sections 15-24 "
    "cover the responsibilities and duties of entities designated as "
    "national critical information infrastructure.\n"
    "\t3. Regulating cybersecurity service providers through a licensing "
    "regime to ensure that those offering services like managed security "
    "operation center monitoring and penetration testing adhere to national "
    "standards. Sections 27-34 focus on the licensing of cybersecurity "
    "service providers. \n"
    "\t4. Strengthening enforcement through powers of investigation, search, "
    "and seizure, allowing the government to respond effectively to "
    "cybersecurity threats (Sections 36-52), thus, enabling the enforcement "
    "of cybersecurity regulations and the management of cyber incidents.\n"
    "This new law positions Malaysia to better handle cyber risks and "
    "threats to its critical infrastructures."
)


class TestConjunctionsAndRanges:
    def test_sg_75_conjunction_yields_both_sections(self):
        # "section 39 and 40" must yield 39 AND 40; s. 40 shipped as a false
        # NEW beside its KNOWN sibling s. 39.
        assert parse_sections(SG_75_COMMENT) == ["20", "39", "40"]

    def test_my_72_ranges_expand_to_every_interior_section(self):
        got = set(parse_sections(MY_72_COMMENT))
        expected = {str(n) for n in range(15, 25)} | {
            str(n) for n in range(27, 35)
        } | {str(n) for n in range(36, 53)}
        assert got == expected

    def test_comma_and_list(self):
        assert parse_sections("see sections 3, 5 and 9 of the Act") == ["3", "5", "9"]

    def test_ampersand(self):
        assert parse_sections("Sections 12 & 14 apply") == ["12", "14"]

    def test_to_range(self):
        assert parse_sections("Sections 4 to 6 govern retention") == ["4", "5", "6"]


class TestConservatism:
    def test_no_keyword_no_capture(self):
        # "Regulations 2021" must not surface 2021: the \b guard keeps the
        # trailing s of a word from acting as the "s." keyword.
        assert parse_sections("under the Telecommunications Regulations 2021") == []

    def test_letter_suffix_and_subsection_tokens_kept_verbatim(self):
        assert parse_sections("Section 317ZH and section 45(2) apply") == [
            "317ZH",
            "45(2)",
        ]

    def test_letter_suffix_never_range_expanded(self):
        # A dash between letter-suffixed tokens is not an expandable range.
        assert parse_sections("sections 12A-12C") == ["12A", "12C"]

    def test_oversized_range_keeps_endpoints_only(self):
        got = parse_sections("sections 1-500 of the code")
        assert got == ["1", "500"]

    def test_empty_and_plain_prose(self):
        assert parse_sections("") == []
        assert parse_sections("no citation here at all") == []

    def test_single_reference_unchanged(self):
        assert parse_sections("Section 129 restricts transfer") == ["129"]


# ---------------------------------------------------------------------------
# Baseline Law List helpers (Round 1 and Round 2 Databases)
# ---------------------------------------------------------------------------

from extract_known_matrix import (  # noqa: E402
    build,
    extract_urls,
    law_year,
    pair_urls,
    reference_columns,
    split_laws,
)


class TestBaselineHelpers:
    def test_split_on_semicolons_and_blank_lines(self):
        cell = "Law No.11 on ITE 2008\n;\nLaw No.19 on Amendments 2016\n\nLaw No.1 2024 "
        assert split_laws(cell) == ["Law No.11 on ITE 2008", "Law No.19 on Amendments 2016", "Law No.1 2024"]

    def test_urls_in_order_deduplicated_without_tracking(self):
        cells = [
            "http://jdih.kkp.go.id/peraturan/pp-46-2014.pdf\n;\nhttps://peraturan.bpk.go.id/Details/5475",
            "https://farmalkes.kemkes.go.id/unduh/uu-17-2023/?utm_source=chatgpt.com",
            None,
            "https://peraturan.bpk.go.id/Details/5475",
        ]
        assert extract_urls(cells) == [
            "http://jdih.kkp.go.id/peraturan/pp-46-2014.pdf",
            "https://peraturan.bpk.go.id/Details/5475",
            "https://farmalkes.kemkes.go.id/unduh/uu-17-2023/",
        ]

    def test_pairing(self):
        assert pair_urls(["A"], ["u1", "u2"]) == [(["u1", "u2"], "single")]
        assert pair_urls(["A"], []) == [([], "none")]
        assert pair_urls(["A", "B"], ["u1", "u2"]) == [(["u1"], "positional"), (["u2"], "positional")]
        assert pair_urls(["A", "B", "C"], ["u1", "u2"]) == [([], "none")] * 3

    def test_year_from_title_else_timeframe(self):
        assert law_year("Law No.19 on Amendments to Law Number 11 of 2008 2016", "") == 2016
        assert law_year("Map Management Regulations 《地图管理条例》", "Since December 2015") == 2015
        assert law_year("Some Measures", "") is None

    def test_reference_columns_stop_at_the_next_named_column(self):
        header = ["Pillar_ID", "Indicator_ID", "Raw", "Act", "Cov", "Impact", "Time",
                  "References", None, None, None, None, "Note"]
        assert list(reference_columns(header)) == [7, 8, 9, 10, 11]

    def test_build_merges_one_law_across_indicators(self):
        rows = [
            {"indicator": "6.1", "acts_cell": "Law No.27 on Personal Data Protection 2022",
             "comment": "Article 56 applies", "timeframe": "", "urls": ["https://a/1"]},
            {"indicator": "7.2", "acts_cell": "Law A 2008;\nLaw No.27 on Personal Data Protection 2022",
             "comment": "", "timeframe": "", "urls": ["https://a/2", "https://a/3"]},
        ]
        database, economies = build({"ID": ("Indonesia", rows)})
        assert database["ID"]["6.1"][0]["sections"] == ["56"]
        uu27 = next(law for law in economies["ID"]["laws"] if law["law"].startswith("Law No.27"))
        assert uu27["indicators"] == ["6.1", "7.2"]
        assert uu27["urls"] == ["https://a/1", "https://a/3"]  # single-law row URLs first
        assert uu27["url_pairing"] == "single"
        assert uu27["year"] == 2022


class TestBaselineCleanup:
    def test_full_width_semicolons_split_and_trailing_punctuation_stripped(self):
        cell = "《网络安全法》；\n《数据安全法》;  Law on Cybersecurity,"
        assert split_laws(cell) == ["《网络安全法》", "《数据安全法》", "Law on Cybersecurity"]

    def test_buddhist_era_year_converted(self):
        assert law_year("Personal Data Protection Act B.E.2562 (พระราชบัญญัติ พ.ศ. 2562)", "") == 2019
        assert law_year("Some Act B.E. 2535", "") == 1992

    def test_timeframe_year_only_on_single_law_rows(self):
        assert law_year("Map Management Regulations", "Since December 2015", single=True) == 2015
        assert law_year("Map Management Regulations", "Since December 2015", single=False) is None

    def test_company_names_are_not_laws(self):
        from extract_known_matrix import is_legal_instrument

        for name in ("Lao Telecom (LaoTel)", "TPLUS Digital Co, Ltd", "UNITEL", "ETL",
                     "Mongolian Post JSC"):
            assert not is_legal_instrument(name), name
        for name in ("Mineral Law", "Penal Code No. 26/NA 2017", "Customs Law of Mongolia",
                     "《地图管理条例》", "Personal Data Protection Act 2010",
                     "Constitution of Mongolia", "WTO Anti-Dumping Agreement"):
            assert is_legal_instrument(name), name

    def test_positional_url_claimed_by_another_law_is_dropped(self):
        rows = [
            {"indicator": "5.1", "acts_cell": "Law on Telecommunications No.09/NA 2011",
             "comment": "", "timeframe": "", "urls": ["https://gazette/telecom.pdf"]},
            {"indicator": "5.2", "acts_cell": "Law on Investment Promotion 2016;Law on Telecommunications No.09/NA 2011",
             "comment": "", "timeframe": "", "urls": ["https://gazette/telecom.pdf", "https://x/other.pdf"]},
            {"indicator": "5.3", "acts_cell": "Lao Telecom (LaoTel);UNITEL",
             "comment": "", "timeframe": "", "urls": ["https://a/1", "https://a/2"]},
        ]
        database, economies = build({"LA": ("Lao PDR", rows)})
        laws = {law["law"]: law for law in economies["LA"]["laws"]}
        assert laws["Law on Investment Promotion 2016"]["urls"] == []
        assert laws["Law on Investment Promotion 2016"]["url_pairing"] == "none"
        # the row's order is off, so its other positional URL goes too
        assert laws["Law on Telecommunications No.09/NA 2011"]["urls"] == ["https://gazette/telecom.pdf"]
        assert "UNITEL" not in laws and "Lao Telecom (LaoTel)" not in laws
        assert all("unitel" not in e["law_key"] for e in database["LA"].get("5.3", []))

    def test_urls_deduplicated_across_schemes(self):
        rows = [{"indicator": "7.1", "acts_cell": "Law on Data 2020", "comment": "", "timeframe": "",
                 "urls": ["http://www.cac.gov.cn/a.htm", "https://www.cac.gov.cn/a.htm", "https://b/c"]}]
        _, economies = build({"CN": ("China", rows)})
        assert economies["CN"]["laws"][0]["urls"] == ["http://www.cac.gov.cn/a.htm", "https://b/c"]
