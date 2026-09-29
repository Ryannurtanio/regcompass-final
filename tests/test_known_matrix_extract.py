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
