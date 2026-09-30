"""M4 exit-criteria tests: deterministic section splitting on the real golden
streams (SG PDF, SSO HTML, MY, AU: three drafting styles plus the HTML lane),
the coverage partition + rejoin invariant on every fixture, the Niue volume as
a smoke stress test, and the LLM boundary fallback on the degraded OCR fixture
(offsets and labels ONLY: any text the model emits is discarded unread).

Fallback lanes: invalid JSON -> one stricter retry -> numbered passages
(coverage never breaks, and the Gate still reads every passage). The LiteLLM transport path is exercised
offline via mock_response; one slow test hits the real OpenRouter tier."""

import gzip
import json
import os
from pathlib import Path

import pytest

from conftest import needs_paid  # noqa: E402 - money is opt-in; see conftest
from regcompass.contracts import CanonicalText, Chunk, PageSpan, PipelineConfig
from regcompass.extract import extract
from regcompass.chunk import (
    ChunkingReport,
    _num_key,
    llm_boundaries,
    split_document,
    zh_numeral_to_int,
)

GOLDEN = Path(__file__).parent / "golden"


def load_golden(rel: str) -> CanonicalText:
    with gzip.open(GOLDEN / rel, "rt", encoding="utf-8") as f:
        return CanonicalText.model_validate_json(f.read())


@pytest.fixture(scope="module")
def sg():
    return load_golden("m1/sg_telecommunications_act_1999.json.gz")


@pytest.fixture(scope="module")
def sso():
    return load_golden("m1/sso_agc_gov_sg_Act_TA1999.json.gz")


@pytest.fixture(scope="module")
def my():
    return load_golden("m1/my_personal_data_protection_act_2010.json.gz")


@pytest.fixture(scope="module")
def au():
    return load_golden("m1/au_C2026C00098VOL01.json.gz")


@pytest.fixture(scope="module")
def pk_ocr():
    return load_golden("m2/pk_sro_221_2017_peca_powers.json.gz")


def assert_partition(chunks: list[Chunk], canonical: CanonicalText) -> None:
    """The M4 invariant: chunks are a contiguous, ordered, gapless partition of
    the canonical stream, and re-joining all slices reproduces it exactly."""
    assert chunks, "no chunks produced"
    assert chunks[0].char_start == 0
    assert chunks[-1].char_end == len(canonical.full_text)
    for prev, cur in zip(chunks, chunks[1:]):
        assert prev.char_end == cur.char_start, (
            f"gap/overlap between {prev.section_label!r} and {cur.section_label!r}"
        )
    assert "".join(c.text for c in chunks) == canonical.full_text
    for c in chunks:
        assert c.text == canonical.slice(c.char_start, c.char_end)
        assert c.document_id == canonical.document_id


def sections(chunks: list[Chunk]) -> list[Chunk]:
    return [c for c in chunks if c.chunk_kind == "section"]


def by_label(chunks: list[Chunk], label_part: str) -> Chunk:
    hits = [c for c in chunks if label_part == c.section_label or c.section_label.endswith(label_part)]
    assert hits, f"no chunk labelled {label_part!r}"
    return hits[0]


# ---------------------------------------------------------------------------
# SG Telecommunications Act 1999 (pdfplumber stream): "N." / "N.—(1)" starts,
# marginal-note headings, leading TOC, LEGISLATIVE HISTORY back matter
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def sg_result(sg):
    return split_document(sg)


class TestSingaporePdf:

    def test_partition_and_rejoin(self, sg_result, sg):
        chunks, _ = sg_result
        assert_partition(chunks, sg)

    def test_section_count_in_expected_band(self, sg_result):
        chunks, report = sg_result
        n = len(sections(chunks))
        assert 90 <= n <= 130, f"SG splitter found {n} sections"
        assert report.fallback_used is False

    def test_known_sections_land_where_the_act_says(self, sg_result):
        chunks, _ = sg_result
        s1 = by_label(chunks, "s. 1")
        assert "This Act is the Telecommunications Act 1999" in s1.text
        s3 = by_label(chunks, "s. 3")
        assert "exclusive privilege" in s3.text
        s53 = by_label(chunks, "s. 53")
        assert "Unlawful operation" in s53.text or "shall not" in s53.text

    def test_marginal_note_travels_with_its_section(self, sg_result):
        chunks, _ = sg_result
        s2 = by_label(chunks, "s. 2")
        assert s2.text.lstrip().startswith("Interpretation")

    def test_part_context_in_labels(self, sg_result):
        chunks, _ = sg_result
        s3 = by_label(chunks, "s. 3")
        assert "Part 2" in s3.section_label

    def test_toc_and_front_and_back_matter_accounted(self, sg_result):
        chunks, _ = sg_result
        kinds = {c.chunk_kind for c in chunks}
        assert "toc" in kinds
        assert "front_matter" in kinds
        back = [c for c in chunks if c.chunk_kind == "other"]
        assert any("LEGISLATIVE HISTORY" in c.text for c in back)

    def test_back_matter_lists_are_not_sections(self, sg_result):
        """The amendment-history numbered list at the tail must never be
        emitted as statute sections."""
        chunks, _ = sg_result
        for c in sections(chunks):
            assert "Act 10 of 2005" not in c.text[:80]

    def test_page_spans_populated(self, sg_result, sg):
        chunks, _ = sg_result
        for c in sections(chunks)[:20]:
            assert c.page_start is not None and c.page_end is not None
            assert 1 <= c.page_start <= c.page_end <= len(sg.pages)

    def test_deterministic(self, sg_result, sg):
        chunks, _ = sg_result
        again, _ = split_document(sg)
        assert [c.model_dump() for c in again] == [c.model_dump() for c in chunks]


# ---------------------------------------------------------------------------
# SSO HTML lane: section number alone on its own line ("1."), only s. 1-2
# rendered in the fixture (the page lazy-loads the rest)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def sso_result(sso):
    return split_document(sso)


class TestSingaporeHtml:

    def test_partition(self, sso_result, sso):
        chunks, _ = sso_result
        assert_partition(chunks, sso)

    def test_the_two_rendered_sections_found(self, sso_result):
        chunks, report = sso_result
        assert report.fallback_used is False
        s1 = by_label(chunks, "s. 1")
        assert "Telecommunications Act 1999" in s1.text
        s2 = by_label(chunks, "s. 2")
        assert "unless the context otherwise requires" in s2.text


# ---------------------------------------------------------------------------
# MY PDPA: "Section N. Title" headings, 146 sections exactly
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def my_result(my):
    return split_document(my)


class TestMalaysia:

    def test_partition(self, my_result, my):
        chunks, _ = my_result
        assert_partition(chunks, my)

    def test_all_146_sections(self, my_result):
        chunks, report = my_result
        assert len(sections(chunks)) == 146
        assert report.fallback_used is False

    def test_known_sections(self, my_result):
        chunks, _ = my_result
        s1 = by_label(chunks, "s. 1")
        assert "Personal Data Protection Act 2010" in s1.text
        s130 = by_label(chunks, "s. 130")
        # a page-number line may attach forward as leading noise; the heading
        # must sit within the chunk's first lines and the offence text inside
        assert "Section 130." in "\n".join(s130.text.split("\n")[:3])
        assert "Unlawful collecting" in s130.text

    def test_part_context(self, my_result):
        chunks, _ = my_result
        s5 = by_label(chunks, "s. 5")
        assert "Part" in s5.section_label and "II" in s5.section_label


# ---------------------------------------------------------------------------
# AU Criminal Code compilation vol 1: dot-leader TOC, bare Act sections 1-5,
# Schedule sections N.N, running headers/footers as noise
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def au_result(au):
    return split_document(au)


class TestAustralia:

    def test_partition(self, au_result, au):
        chunks, _ = au_result
        assert_partition(chunks, au)

    def test_section_count_band(self, au_result):
        chunks, report = au_result
        # vol 1 has 541 raw "N.N Title" head lines after the TOC, of which ~10
        # are wrapped-prose false positives, plus the 7 bare Act sections
        n = len(sections(chunks))
        assert 500 <= n <= 560, f"AU splitter found {n} sections"
        assert report.fallback_used is False

    def test_toc_region_is_one_toc_chunk_not_sections(self, au_result):
        chunks, _ = au_result
        tocs = [c for c in chunks if c.chunk_kind == "toc"]
        assert tocs, "AU dot-leader contents block not labelled toc"
        assert any("....." in c.text for c in tocs)
        # no section chunk may be a TOC fragment
        for c in sections(chunks):
            assert "..........." not in c.text[:200], f"TOC leaked into {c.section_label}"

    def test_act_sections_and_schedule_sections_both_found(self, au_result):
        chunks, _ = au_result
        s3 = by_label(chunks, "s. 3")
        assert "The Schedule has effect" in s3.text
        s11 = by_label(chunks, "s. 1.1")
        assert "The only offences against laws of the Commonwealth" in s11.text
        last = by_label(chunks, "s. 261.3")
        assert "Ancillary offences" in last.text

    def test_letter_suffix_sections(self, au_result):
        chunks, _ = au_result
        s3a = by_label(chunks, "s. 3A")
        assert "external Territory" in s3a.text

    def test_footers_never_become_sections(self, au_result):
        chunks, _ = au_result
        for c in sections(chunks):
            head = c.text.lstrip()[:60]
            assert not head.startswith("Compilation No."), c.section_label


# ---------------------------------------------------------------------------
# Non-English article headings: the profile that keeps a scanned Lao or Thai
# statute from degrading to one unusable chunk.
# ---------------------------------------------------------------------------


class TestArticleWordProfile:
    """Non-English drafting traditions head a provision with their own article
    word. Without a profile for them a scanned Lao or Thai statute degrades to
    one chunk of kind "other", which the Gate never looks at, so the whole
    non-English lane produces nothing."""

    ARTICLE_WORDS = {
        "Lao": "ມາດຕາ",
        "Thai": "มาตรา",
        "Bahasa Indonesia": "Pasal",
        "Russian": "Статья",
        "Vietnamese": "Điều",
    }
    BODY = (
        "The parties to an electronic transaction shall keep a record of the"
        " transaction for a period of five years and shall make it available to"
        " the authority on request, together with the means of verifying it."
    )

    HEADINGS = (
        "Scope of application", "Definitions", "Principles", "Duties of the parties",
        "Electronic signatures", "Record keeping",
    )

    def _canonical(self, word: str) -> CanonicalText:
        parts = [
            f"{word} {i} {heading}\n{self.BODY}\n"
            for i, heading in enumerate(self.HEADINGS, start=1)
        ]
        text = "".join(parts)
        return CanonicalText(
            document_id="doc_x_article",
            source_sha256="c" * 64,
            extractor="tesseract",
            extractor_version="5.5.2",
            full_text=text,
            pages=[PageSpan(page_number=1, char_start=0, char_end=len(text))],
            ocr_applied=True,
        )

    @pytest.mark.parametrize("language", sorted(ARTICLE_WORDS))
    def test_articles_become_section_chunks(self, language):
        canonical = self._canonical(self.ARTICLE_WORDS[language])
        chunks, report = split_document(canonical)
        assert report.style == "article_word", report.notes
        assert report.fallback_used is False
        assert len(sections(chunks)) == 6
        assert_partition(chunks, canonical)

    def test_the_profile_cannot_fire_on_an_english_act(self, sg_result):
        chunks, report = sg_result
        assert report.style != "article_word"
        assert chunks


# ---------------------------------------------------------------------------
# Chinese article headings: 第N条, the number written in Chinese numerals. Same
# purpose as the article-word profile above (China is one of the six chosen
# Economies and its statutes arrive as scans), with one extra problem: the
# number is not arabic, so it has to become an integer before anything can be
# ordered by it.
# ---------------------------------------------------------------------------


PIPL_EXCERPT = Path(__file__).parent / "fixtures/ocr_reference/pipl_cn_chapter1_excerpt.txt"


@pytest.fixture(scope="module")
def pipl() -> CanonicalText:
    text = PIPL_EXCERPT.read_text(encoding="utf-8")
    return CanonicalText(
        document_id="doc_cn_pipl",
        source_sha256="d" * 64,
        extractor="tesseract+rapidocr",
        extractor_version="5.5.2+3.9.1",
        full_text=text,
        pages=[PageSpan(page_number=1, char_start=0, char_end=len(text))],
        ocr_applied=True,
    )


@pytest.fixture(scope="module")
def pipl_result(pipl):
    return split_document(pipl)


class TestChineseNumerals:
    @pytest.mark.parametrize(
        ("numeral", "value"),
        [
            ("一", 1), ("九", 9), ("十", 10), ("十二", 12), ("二十", 20),
            ("二十一", 21), ("七十四", 74), ("一百", 100), ("一百零五", 105),
            ("一百二十三", 123), ("〇", 0), ("13", 13),
        ],
    )
    def test_a_numeral_becomes_its_integer(self, numeral, value):
        assert zh_numeral_to_int(numeral) == value

    @pytest.mark.parametrize(
        ("numeral", "value"),
        [("一千", 1000), ("一千零五", 1005), ("一千二百三十四", 1234), ("二千", 2000)],
    )
    def test_the_thousands_unit_converts(self, numeral, value):
        """An article number above 999 is written with 千; without it the
        converter read 第一千零五条 as 6 and put it at the front of the act."""
        assert zh_numeral_to_int(numeral) == value

    @pytest.mark.parametrize(
        ("numeral", "value"),
        [("一〇", 10), ("二〇", 20), ("一一", 11), ("一〇五", 105), ("二〇二一", 2021)],
    )
    def test_positional_zero_numbering_converts(self, numeral, value):
        """Some drafters number positionally rather than multiplicatively:
        第一〇条 is article 10, not article 1 followed by a zero. A string with
        no unit glyph and more than one character is positional digits, so the
        multiplicative reading (which returned 0 for 一〇) never applies."""
        assert zh_numeral_to_int(numeral) == value

    @pytest.mark.parametrize("numeral", ["一", "九", "〇", "零"])
    def test_a_single_glyph_is_still_itself(self, numeral):
        """The positional rule must not capture the one-character case, where
        both readings agree and the multiplicative one is the plain answer."""
        assert zh_numeral_to_int(numeral) == {"一": 1, "九": 9, "〇": 0, "零": 0}[numeral]

    @pytest.mark.parametrize("not_a_numeral", ["", "12A", "abc", "3.1", "条"])
    def test_anything_else_is_none(self, not_a_numeral):
        assert zh_numeral_to_int(not_a_numeral) is None

    @pytest.mark.parametrize("mixed", ["0一", "一0", "12三", "十2", "一百5"])
    def test_arabic_digits_mixed_with_chinese_are_rejected(self, mixed):
        """The heading pattern accepts either spelling of the number, so a
        scan can hand the converter both at once. Reading it as one number
        invents an article number that is in no act."""
        assert zh_numeral_to_int(mixed) is None

    def test_a_rejected_number_falls_back_to_a_plain_string_key(self):
        """_num_key must not turn an unreadable number into an integer label:
        the sentinel keeps it out of every increasing run instead."""
        assert _num_key("0一") == (10**9, 10**9, "0一")
        assert _num_key("一〇") == (10, -1, "")


class TestChineseArticleProfile:
    """The Personal Information Protection Law excerpt, verbatim OCR (see
    tests/fixtures/FIXTURES.md). Without this profile the whole statute is one
    chunk of kind "other", which the Gate never looks at."""

    def test_the_chinese_profile_wins_and_the_partition_holds(self, pipl_result, pipl):
        chunks, report = pipl_result
        assert report.style == "article_zh", report.notes
        assert report.fallback_used is False
        assert_partition(chunks, pipl)

    def test_every_article_of_the_excerpt_becomes_a_section(self, pipl_result):
        chunks, _ = pipl_result
        # articles 1, 2, 3, 5, 6, 7, 8, 9, 10, 12, 13; 4 and 11 are lost to the
        # scan (the OCR read their heading lines as "热采" and "校带护和护全采一")
        assert [c.section_label for c in sections(chunks)] == [
            "Chapter 1 s. 1", "Chapter 1 s. 2", "Chapter 1 s. 3", "Chapter 1 s. 5",
            "Chapter 1 s. 6", "Chapter 1 s. 7", "Chapter 1 s. 8", "Chapter 1 s. 9",
            "Chapter 1 s. 10", "Chapter 1 s. 12", "Chapter 2 s. 13",
        ]

    def test_the_numbers_sort_in_document_order_not_glyph_order(self, pipl_result):
        """Ten precedes twelve because 十 became 10, not because "十" sorts
        before "十二": a plain string key would also give that answer here, so
        the test reads the integers the labels carry."""
        chunks, _ = pipl_result
        numbers = [int(c.section_label.rsplit(". ", 1)[1]) for c in sections(chunks)]
        assert numbers == sorted(numbers)
        assert numbers == [1, 2, 3, 5, 6, 7, 8, 9, 10, 12, 13]

    def test_the_chapter_heading_travels_with_its_first_article(self, pipl_result):
        chunks, _ = pipl_result
        first = sections(chunks)[0]
        assert first.text.startswith("第一章 总 则\n第一条")
        thirteen = [c for c in sections(chunks) if c.section_label == "Chapter 2 s. 13"][0]
        assert thirteen.text.startswith("第二章 个人信息处理规则\n第一节 一般规定\n第十三条")

    def test_a_wrapped_body_line_stays_with_its_own_article(self, pipl_result):
        """The heading walk-back must not pull the tail of article 10 into the
        chunk for article 12: Chinese has no letter case, so the "line is all
        upper case, therefore a heading" rule says yes to every line."""
        chunks, _ = pipl_result
        ten = [c for c in sections(chunks) if c.section_label == "Chapter 1 s. 10"][0]
        assert "家安全、公共利益的个人信息处理活动。" in ten.text

    def test_the_profile_cannot_fire_on_an_english_act(self, sg_result):
        chunks, report = sg_result
        assert report.style != "article_zh"
        assert chunks


# ---------------------------------------------------------------------------
# Chinese statutes published as web pages. The regulator's pages print each
# article number in bold, <strong>第一条</strong>, and the HTML lane breaks the
# stream at every tag, so the number stands ALONE on its line with the body on
# the next one. The heading rule must accept that shape too, or the statute
# degrades to one chunk the Gate never reads.
# ---------------------------------------------------------------------------


CAC_PIPL_HTML = Path(__file__).parent / "fixtures/html/cac_pipl_articles_excerpt.htm"


@pytest.fixture(scope="module")
def cac_pipl() -> CanonicalText:
    return extract(CAC_PIPL_HTML.read_bytes(), "html", "doc_cn_cac_pipl")


class TestChineseArticlesOnAWebPage:
    def test_the_article_number_stands_alone_on_its_line(self, cac_pipl):
        """The shape the fix is for, read from the real page: if the HTML lane
        ever joins the bold number to its body, this test says so."""
        lines = cac_pipl.full_text.split("\n")
        first = lines.index("第一条")
        assert lines[first + 1].startswith("\u3000为了保护个人信息权益")

    def test_every_article_becomes_a_section(self, cac_pipl):
        chunks, report = split_document(cac_pipl)
        assert report.style == "article_zh", report.notes
        assert report.fallback_used is False
        assert [c.section_label for c in sections(chunks)] == [
            *(f"Chapter 1 s. {n}" for n in range(1, 13)),
            "Chapter 2 s. 13", "Chapter 2 s. 14",
        ]
        assert_partition(chunks, cac_pipl)

    def test_each_article_carries_its_own_body(self, cac_pipl):
        chunks, _ = split_document(cac_pipl)
        by = {c.section_label: c for c in sections(chunks)}
        assert by["Chapter 1 s. 1"].text.startswith("第一章\u3000总\u3000\u3000则\n第一条\n")
        assert "根据宪法，制定本法。" in by["Chapter 1 s. 1"].text
        assert "第二条" not in by["Chapter 1 s. 1"].text
        assert by["Chapter 2 s. 13"].text.startswith(
            "第二章\u3000个人信息处理规则\n第一节\u3000一般规定\n第十三条\n"
        )
        assert "取得个人的同意" in by["Chapter 2 s. 13"].text

    def test_the_table_of_contents_is_not_a_run_of_articles(self, cac_pipl):
        """The contents list names chapters and sections, never articles, so
        everything above the first article is front matter."""
        chunks, _ = split_document(cac_pipl)
        assert chunks[0].chunk_kind == "front_matter"
        assert "目" in chunks[0].text and "第八章" in chunks[0].text


# ---------------------------------------------------------------------------
# Indonesian statutes: the heading is "Pasal N" alone on its line. Two other
# lines share that opening and are not headings: a sentence that wraps just
# before a cross-reference ("Pasal 13 ayat (1) dan ayat (2) dikecualikan
# untuk:"), and the catchword the printer puts at the foot of each page to
# announce the next page's first line ("Pasal 18. ."). Either one taken as a
# heading splits an article and gives its body another article's number.
# ---------------------------------------------------------------------------


UU27_EXCERPT = Path(__file__).parent / "fixtures/ocr_reference/uu27_2022_pasal1_19_excerpt.txt"


@pytest.fixture(scope="module")
def uu27() -> CanonicalText:
    text = UU27_EXCERPT.read_text(encoding="utf-8")
    return CanonicalText(
        document_id="doc_id_uu27",
        source_sha256="e" * 64,
        extractor="pdfplumber",
        extractor_version="0.11.4",
        full_text=text,
        pages=[PageSpan(page_number=1, char_start=0, char_end=len(text))],
    )


class TestIndonesianArticleHeadings:
    def test_each_article_is_one_section_under_its_own_number(self, uu27):
        chunks, report = split_document(uu27)
        assert report.style == "article_word", report.notes
        # "Pasal I" (article 1) and "Pasal ." (article 7's catchword gone
        # wrong) carry no readable number in this text layer, so article 1
        # stays in the front matter and article 6 runs into article 7's
        # catchword line; every number that IS readable is right and unique.
        assert [c.section_label for c in sections(chunks)] == [
            f"s. {n}" for n in range(2, 20)
        ]
        assert_partition(chunks, uu27)

    def test_a_cross_reference_at_a_line_start_stays_in_its_article(self, uu27):
        chunks, _ = split_document(uu27)
        fifteen = next(c for c in sections(chunks) if c.section_label == "s. 15")
        assert fifteen.text.startswith("Pasal 15\n")
        assert "Pasal 13 ayat (1) dan ayat (2) dikecualikan untuk:" in fifteen.text
        assert "kepentingan statistik dan penelitian ilmiah." in fifteen.text

    def test_a_page_foot_catchword_is_not_a_heading(self, uu27):
        chunks, _ = split_document(uu27)
        seventeen = next(c for c in sections(chunks) if c.section_label == "s. 17")
        eighteen = next(c for c in sections(chunks) if c.section_label == "s. 18")
        assert "Pasal 18. ." in seventeen.text
        # the running page head above the real heading travels with it, as
        # upper-case heading lines do in every style
        assert "\nPasal 18\n(1) Pemrosesan Data Pribadi" in eighteen.text
        assert "Pasal 18. ." not in eighteen.text

    # The tail of an Indonesian statute as its text layer reads (UU 27/2022,
    # OCR spellings kept): the last articles, then the official Elucidation
    # (Penjelasan), whose article-by-article part heads each note "Pasal N"
    # exactly as the body heads the article itself.
    ELUCIDATION_TAIL = (
        "Pasal 75\n"
        "Pada saat Undang-Undang ini mulai berlaku, semua peraturan\n"
        "perundang-undangan yang mengatur mengenai Pelindungan Data\n"
        "Pribadi, dinyatakan masih tetap berlaku.\n"
        "Pasal 76\n"
        "Undang-Undang ini mulai berlaku pada tanggal diundangkan.\n"
        "PENJEI,ASAN\n"
        "ATAS\n"
        "UNDANG-UNDANG REPUBUK INDONESI,A\n"
        "NOMOR 27 TAHVN 2022\n"
        "I UMUM\n"
        "Ferkembangan teknologi informasi dan komurikasi yang melaju dengan\n"
        "pesat telah menimbulkan berbagai peluang dan tantangan.\n"
        "II.\n"
        "PASALDEMIPASAL\n"
        "Pasal 2\n"
        "Cukupjelas.\n"
        "Pasal 3\n"
        "Hurufa\n"
        'Yang dimaksud dengan "asas pelindungan" adalah bahwa setiap\n'
        "pemrosesan Data Pribadi dilakukan dengan memberikan pelindungan.\n"
    )

    def _tail(self) -> CanonicalText:
        text = self.ELUCIDATION_TAIL
        return CanonicalText(
            document_id="doc_id_tail",
            source_sha256="e" * 64,
            extractor="pdfplumber",
            extractor_version="0.11.4",
            full_text=text,
            pages=[PageSpan(page_number=1, char_start=0, char_end=len(text))],
        )

    def test_elucidation_notes_are_not_the_articles_themselves(self):
        """A quote from the note on article 3 cited as "s. 3" sends the
        reader to article 3, where the quote is not."""
        chunks, _ = split_document(self._tail())
        assert [c.section_label for c in sections(chunks)] == [
            "s. 75", "s. 76", "Elucidation s. 2", "Elucidation s. 3",
        ]

    def test_the_general_elucidation_is_its_own_chunk(self):
        doc = self._tail()
        chunks, _ = split_document(doc)
        general = next(c for c in chunks if c.text.startswith("PENJEI,ASAN\n"))
        assert general.chunk_kind == "other"
        assert "Ferkembangan teknologi" in general.text
        seventy_six = next(c for c in chunks if c.section_label == "s. 76")
        assert "PENJEI,ASAN" not in seventy_six.text
        assert_partition(chunks, doc)

    def test_a_bare_catchword_before_the_page_head_is_not_a_heading(self):
        """The same catchword with its dots on the line above (UU 27/2022,
        page 32): two headings with one number a page head apart are one
        article announced and then printed."""
        text = (
            "Pasal 70\n"
            "Pidana tambahan dapat dijatuhkan kepada Korporasi berupa\n"
            "perampasan keuntungan yang diperoleh dari tindak pidana.\n"
            "...\n"
            "Pasal 71\n"
            "SK No 155232 A\n"
            "PRESIDEN\n"
            "REPIIBLIK INDONESIA\n"
            "-32-\n"
            "Pasal 71\n"
            "(1) Dalam hal pengadilan menjatuhkan putusan pidana\n"
            "denda, terpidana diberikan jangka waktu 1 (satu)\n"
            "bulan sejak putusan telah berkekuatan hukum tetap.\n"
            "Pasal 72\n"
            "Dalam hal Korporasi tidak dapat membayar pidana denda.\n"
        )
        doc = CanonicalText(
            document_id="doc_id_x",
            source_sha256="e" * 64,
            extractor="pdfplumber",
            extractor_version="0.11.4",
            full_text=text,
            pages=[PageSpan(page_number=1, char_start=0, char_end=len(text))],
        )
        chunks, _ = split_document(doc)
        assert [c.section_label for c in sections(chunks)] == ["s. 70", "s. 71", "s. 72"]
        seventy_one = sections(chunks)[1]
        assert "(1) Dalam hal pengadilan" in seventy_one.text

    @staticmethod
    def _id_doc(text: str) -> CanonicalText:
        return CanonicalText(
            document_id="doc_id_y",
            source_sha256="e" * 64,
            extractor="pdfplumber",
            extractor_version="0.11.4",
            full_text=text,
            pages=[PageSpan(page_number=1, char_start=0, char_end=len(text))],
        )

    def test_a_sentence_ending_on_a_cross_reference_is_not_a_heading(self):
        """UU 36/1999: "... tidak merupakan pelanggaran\nPasal 40." ends a
        sentence of article 43 on a line of its own."""
        text = (
            "Pasal 42\n"
            "Penyelenggara jasa telekomunikasi wajib merahasiakan informasi.\n"
            "Pasal 43\n"
            "Pemberian rekaman informasi oleh penyelenggara jasa telekomunikasi\n"
            "dimaksud dalam Pasal 42 ayat (2), tidak merupakan pelanggaran\n"
            "Pasal 40.\n"
            "Pasal 44\n"
            "Selain Penyidik Pejabat Polisi Negara Republik Indonesia.\n"
        )
        chunks, _ = split_document(self._id_doc(text))
        assert [c.section_label for c in sections(chunks)] == ["s. 42", "s. 43", "s. 44"]

    def test_an_article_number_split_by_the_text_layer_is_joined(self):
        """PP 71/2019 reads "Pasal 41" as "Pasal 4 1"."""
        text = (
            "Pasal 40\n"
            "Pemerintah berwenang melakukan pemutusan akses Informasi Elektronik.\n"
            "Pasal 4 1\n"
            "(1)\n"
            "Penyelenggaraan Transaksi Elektronik dapat dilakukan dalam lingkup publik.\n"
            "Pasal 42\n"
            "Penyelenggaraan Transaksi Elektronik dalam lingkup publik meliputi.\n"
        )
        chunks, _ = split_document(self._id_doc(text))
        assert [c.section_label for c in sections(chunks)] == ["s. 40", "s. 41", "s. 42"]

    @pytest.mark.parametrize(
        ("heading", "label"),
        [
            # all read from the text layers of UU 27/2022 and PP 71/2019
            ("Pasal 1O", "s. 10"), ("Pasa1 11", "s. 11"), ("Pasal t2", "s. 12"),
            ("Pasal L4", "s. 14"), ("PasaJ22", "s. 22"), ("Pasal24", "s. 24"),
            ("Pasal2T", "s. 27"), ("Pasal T2", "s. 72"), ("Pasal 4OA", "s. 40A"),
        ],
    )
    def test_a_misread_heading_keeps_its_number(self, heading, label):
        """The gazette text layers misread the heading word and its digits
        (O for 0; I, l, L, t for 1; T for 7). A heading alone on its line is
        read through those confusions; a missed heading hands its article's
        body to the article above."""
        body = "Pengendali Data Pribadi wajib melakukan pemrosesan secara sah.\n"
        text = f"Pasal 8\n{body}{heading}\n{body}Pasal 99\n{body}"
        chunks, _ = split_document(self._id_doc(text))
        assert [c.section_label for c in sections(chunks)] == ["s. 8", label, "s. 99"]

    def test_a_roman_article_of_an_amending_act_is_not_read_as_a_digit(self):
        """An amending act numbers its own articles I and II; with no digit in
        the number there is nothing to read as a misread arabic numeral."""
        body = "Beberapa ketentuan dalam Undang-Undang diubah sebagai berikut.\n"
        text = f"Pasal I\n{body}Pasal 26\n{body}Pasal 27\n{body}Pasal II\n{body}"
        chunks, _ = split_document(self._id_doc(text))
        assert [c.section_label for c in sections(chunks)] == ["s. 26", "s. 27"]

    def test_a_titled_lao_heading_still_counts(self):
        """Lao and Thai print the article title on the heading line and have
        no letter case, so the cross-reference rule must leave them alone."""
        body = "ລັດຖະບານ ຄຸ້ມຄອງ ລະບົບ ຂໍ້ມູນ ຂ່າວສານ ທົ່ວປະເທດ.\n"
        text = "".join(f"ມາດຕາ {i} ຫົວຂໍ້ {i}\n{body}" for i in range(1, 5))
        doc = CanonicalText(
            document_id="doc_la_x",
            source_sha256="f" * 64,
            extractor="tesseract",
            extractor_version="5.5.2",
            full_text=text,
            pages=[PageSpan(page_number=1, char_start=0, char_end=len(text))],
            ocr_applied=True,
        )
        chunks, report = split_document(doc)
        assert report.style == "article_word"
        assert [c.section_label for c in sections(chunks)] == ["s. 1", "s. 2", "s. 3", "s. 4"]


# ---------------------------------------------------------------------------
# Niue volume (stress smoke): 683 pages, ~50 acts, drop-cap noise. Exit here is
# invariants + no crash, not label accuracy (not an accuracy fixture).
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestNiueSmoke:
    def test_partition_holds_on_the_monster(self):
        niue = load_golden("m1/niue_legislation_volume_1.json.gz")
        chunks, report = split_document(niue)
        assert_partition(chunks, niue)
        assert len(sections(chunks)) >= 100
        assert report.fallback_used is False
        # 90 scheduled convention articles never outrank the volume's sections
        assert report.style == "num_title"


# ---------------------------------------------------------------------------
# Degraded lane: the scanned S.R.O. notification has no statute structure.
# The deterministic splitter must refuse and the LLM boundary fallback fires.
# ---------------------------------------------------------------------------


def fake_llm(payload: object):
    """Build a completion_fn returning canned content (a JSON payload or str)."""
    calls: list[dict] = []

    def completion(prompt: str, strict: bool) -> str:
        calls.append({"prompt": prompt, "strict": strict})
        return payload if isinstance(payload, str) else json.dumps(payload)

    completion.calls = calls  # type: ignore[attr-defined]
    return completion


class TestFallbackLane:
    def test_deterministic_splitter_refuses_the_sro(self, pk_ocr):
        """No profile may claim the notification's numbered lists as statute
        sections; split_document without a completion_fn splits it into
        numbered passages instead."""
        chunks, report = split_document(pk_ocr, completion_fn=None)
        assert report.fallback_used is True
        assert report.fallback_succeeded is False  # no LLM available -> passages
        assert report.unstructured is True
        assert report.style == "passages"
        assert [c.section_label for c in chunks] == [
            f"Passage {i}" for i in range(1, len(chunks) + 1)]
        assert {c.chunk_kind for c in chunks} == {"section"}
        assert_partition(chunks, pk_ocr)

    def test_fallback_offsets_and_labels_only(self, pk_ocr):
        """The LLM returns line positions + labels; any text it emits is
        poison and must never reach a chunk."""
        lines = pk_ocr.full_text.split("\n")
        assert len(lines) > 50
        payload = [
            {"start_line": 1, "label": "gazette header", "kind": "front_matter",
             "text": "FABRICATED TEXT THAT MUST NEVER APPEAR"},
            {"start_line": 6, "label": "S.R.O. 221(I)/2017", "kind": "section",
             "text": "MORE FABRICATION"},
            {"start_line": 47, "label": "S.R.O. 220(I)/2017", "kind": "section"},
        ]
        fn = fake_llm(payload)
        chunks, report = split_document(pk_ocr, completion_fn=fn)
        assert report.fallback_used is True and report.fallback_succeeded is True
        assert report.fallback_attempts == 1
        assert_partition(chunks, pk_ocr)
        assert len(chunks) == 3
        assert [c.section_label for c in chunks] == [
            "gazette header", "S.R.O. 221(I)/2017", "S.R.O. 220(I)/2017"]
        assert [c.chunk_kind for c in chunks] == ["front_matter", "section", "section"]
        for c in chunks:
            assert "FABRICATED" not in c.text and "FABRICATION" not in c.text
            assert c.text == pk_ocr.slice(c.char_start, c.char_end)
        # boundary chars: chunk 2 starts exactly at line 6 of the stream
        line6_offset = sum(len(l) + 1 for l in pk_ocr.full_text.split("\n")[:5])
        assert chunks[1].char_start == line6_offset

    def test_garbage_then_valid_uses_stricter_retry(self, pk_ocr):
        responses = iter([
            "I think the document has three parts, roughly speaking.",  # not JSON
            json.dumps([{"start_line": 1, "label": "whole notification", "kind": "section"}]),
        ])
        calls = []

        def fn(prompt: str, strict: bool) -> str:
            calls.append(strict)
            return next(responses)

        chunks, report = split_document(pk_ocr, completion_fn=fn)
        assert report.fallback_attempts == 2
        assert report.fallback_succeeded is True
        assert calls == [False, True]  # second attempt is the stricter one
        assert_partition(chunks, pk_ocr)

    def test_garbage_twice_falls_back_to_passages(self, pk_ocr):
        fn = fake_llm("not json at all")
        chunks, report = split_document(pk_ocr, completion_fn=fn)
        assert report.fallback_used is True
        assert report.fallback_succeeded is False
        assert report.fallback_attempts == PipelineConfig().chunk_fallback_attempts
        assert report.unstructured is True
        assert chunks[0].section_label == "Passage 1"
        assert {c.chunk_kind for c in chunks} == {"section"}
        assert_partition(chunks, pk_ocr)

    def test_invalid_offsets_rejected(self, pk_ocr):
        for bad in (
            [{"start_line": 0, "label": "x", "kind": "section"}],  # 1-based
            [{"start_line": 999999, "label": "x", "kind": "section"}],  # out of range
            [{"start_line": 10, "label": "a", "kind": "section"},
             {"start_line": 5, "label": "b", "kind": "section"}],  # non-monotonic
            [{"start_line": 1, "label": "x", "kind": "chapter"}],  # unknown kind
        ):
            fn = fake_llm(bad)
            chunks, report = split_document(pk_ocr, completion_fn=fn)
            assert report.fallback_succeeded is False, f"accepted invalid payload {bad}"
            assert report.style == "passages"
            assert_partition(chunks, pk_ocr)

    def test_code_fenced_json_accepted(self, pk_ocr):
        content = "```json\n" + json.dumps(
            [{"start_line": 1, "label": "notification", "kind": "section"}]) + "\n```"
        fn = fake_llm(content)
        chunks, report = split_document(pk_ocr, completion_fn=fn)
        assert report.fallback_succeeded is True
        assert len(chunks) == 1 and chunks[0].chunk_kind == "section"


class TestLiteLlmTransport:
    def test_default_path_goes_through_litellm(self, pk_ocr, monkeypatch):
        """Exercise the real litellm client offline via mock_response, proving
        the default completion path (num_retries=0) is wired."""
        pytest.importorskip("litellm")  # live extra; absent on a base-tier install
        import regcompass.chunk as chunk_mod

        mock = json.dumps([{"start_line": 1, "label": "notification", "kind": "section"}])
        monkeypatch.setattr(chunk_mod, "_LITELLM_MOCK_RESPONSE", mock)
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-never-used-offline")
        proposals = llm_boundaries(pk_ocr)
        assert len(proposals) == 1
        assert proposals[0].section_label == "notification"
        assert proposals[0].char_start == 0
        assert proposals[0].char_end == len(pk_ocr.full_text)

    @pytest.mark.slow
    @pytest.mark.paid
    @needs_paid
    @pytest.mark.skipif(
        not os.environ.get("OPENROUTER_API_KEY"),
        reason="live OpenRouter test needs OPENROUTER_API_KEY",
    )
    def test_live_openrouter_boundary_call(self, pk_ocr):
        """One real call to the 30B mapper tier: the S.R.O. text is 6k chars.
        Asserts structural validity only (model output varies)."""
        proposals = llm_boundaries(pk_ocr)
        assert proposals, "live fallback returned nothing"
        assert proposals[0].char_start == 0
        assert proposals[-1].char_end == len(pk_ocr.full_text)
        for a, b in zip(proposals, proposals[1:]):
            assert a.char_end == b.char_start


class TestReport:
    def test_report_fields(self, my):
        chunks, report = split_document(my)
        assert isinstance(report, ChunkingReport)
        assert report.style == "my_section_word"
        assert report.n_sections == 146
        assert report.fallback_used is False
        assert report.coverage_chars == len(my.full_text)


# ---------------------------------------------------------------------------
# Degenerate-run drop: SSO whole-act PDFs render the arrangement-of-sections
# TOC and schedule index lists in the exact body "N. Title" shape, so they
# match the ambiguous profiles; each forms its own increasing run (body
# numbering restarts) whose median span is tiny and must be dropped instead of
# dragging the global median under the gate (found on Banking Act 1970).
# ---------------------------------------------------------------------------


def sso_pdf_shaped(n: int = 12, body_chars: int = 400) -> CanonicalText:
    toc = "".join(f"{i}. Heading for section number {i}\n" for i in range(1, n + 1))
    body = "".join(
        f"{i}. Heading for section number {i}\n"
        + f"the operative text of section {i} follows here. " * (body_chars // 45)
        + "\n"
        for i in range(1, n + 1)
    )
    return CanonicalText(
        document_id="doc_test_sso_pdf",
        source_sha256="0" * 64,
        extractor="test",
        extractor_version="0",
        full_text="AN ACT to test degenerate runs.\n" + toc + body,
    )


class TestDegenerateRunDrop:
    def test_toc_run_dropped_body_sections_kept(self):
        doc = sso_pdf_shaped()
        chunks, report = split_document(doc)
        assert report.fallback_used is False, "degenerate TOC run forced the fallback"
        assert report.n_sections == 12
        assert_partition(chunks, doc)
        for c in sections(chunks):
            assert "operative text" in c.text, "a TOC entry was emitted as a section"

    def test_all_degenerate_runs_still_fall_back(self):
        """A document that is ONLY tiny-span lists has no credible structure;
        the fallback lane must still fire (here: numbered passages)."""
        toc_only = "".join(f"{i}. Heading for section number {i}\n" for i in range(1, 30))
        doc = CanonicalText(
            document_id="doc_test_lists_only",
            source_sha256="0" * 64,
            extractor="test",
            extractor_version="0",
            full_text=toc_only,
        )
        chunks, report = split_document(doc)
        assert report.fallback_used is True
        assert report.style == "passages"
        assert all(c.section_label.startswith("Passage ") for c in chunks)


# ---------------------------------------------------------------------------
# More drafting traditions: Mongolian and Kazakh put the number BEFORE the
# article word, English translations head each provision "Article N", and the
# Lao text layers glue the number to the title. Every fragment below is real
# statute text (shortened); without these profiles each of these Documents
# became one chunk the Gate never read.
# ---------------------------------------------------------------------------


def _doc(text: str, document_id: str = "doc_x_fragment") -> CanonicalText:
    return CanonicalText(
        document_id=document_id,
        source_sha256="d" * 64,
        extractor="test",
        extractor_version="0",
        full_text=text,
        pages=[PageSpan(page_number=1, char_start=0, char_end=len(text))],
    )


def _section_labels(chunks: list[Chunk]) -> list[str]:
    return [c.section_label for c in sections(chunks)]


# Law of Mongolia on Personal Data Protection (2021), articles 1 to 3.
MONGOLIAN = (
    "ХУВЬ ХҮНИЙ МЭДЭЭЛЭЛ ХАМГААЛАХ ТУХАЙ\n"
    "НЭГДҮГЭЭР БҮЛЭГ\n"
    "НИЙТЛЭГ ҮНДЭСЛЭЛ\n"
    "1 дүгээр зүйл.Хуулийн зорилт\n"
    "1.1.Энэ хуулийн зорилт нь хүний эрх, эрх чөлөөг хангах үүднээс хувь хүний мэдээллийг\n"
    "цуглуулах, боловсруулах, ашиглах, түүний аюулгүй байдлыг хангахтай холбогдсон\n"
    "харилцааг зохицуулахад оршино.\n"
    "2 дугаар зүйл.Хувь хүний мэдээлэл хамгаалах хууль тогтоомж\n"
    "2.1.Хувь хүний мэдээлэл хамгаалах хууль тогтоомж нь Монгол Улсын Үндсэн хууль, Хүний\n"
    "эрхийн тухай хууль, энэ хууль болон эдгээр хуультай нийцүүлэн гаргасан хууль\n"
    "тогтоомжийн бусад актаас бүрдэнэ.\n"
    "3 дугаар зүйл.Хуулийн үйлчлэх хүрээ\n"
    "3.1.Энэ хуулиар төрийн байгууллага, хуулийн этгээд болон хувь хүн хувь хүний мэдээлэл\n"
    "цуглуулах, боловсруулах, ашиглахтай холбогдсон харилцааг зохицуулна. Энэ хуулийн\n"
    "8 дугаар зүйлд заасан журмын дагуу мэдээллийг боловсруулна.\n"
)

# Law of the Republic of Kazakhstan on Personal Data and their Protection
# (No. 94-V), articles 1 to 3.
KAZAKH = (
    "1-тарау. ЖАЛПЫ ЕРЕЖЕЛЕР\n"
    "1-бап. Осы Заңда пайдаланылатын негізгі ұғымдар\n"
    "Осы Заңда мынадай негізгі ұғымдар пайдаланылады:\n"
    "1) биометриялық деректер – дербес деректер субъектісінің физиологиялық және\n"
    "биологиялық ерекшеліктерін сипаттайтын, оның негізінде оның жеке басын анықтауға\n"
    "болатын дербес деректер;\n"
    "2-бап. Қазақстан Республикасының дербес деректер және оларды қорғау туралы заңнамасы\n"
    "1. Қазақстан Республикасының дербес деректер және оларды қорғау туралы заңнамасы\n"
    "Қазақстан Республикасының Конституциясына негізделеді.\n"
    "3-бап. Осы Заңның қолданылу аясы\n"
    "1. Осы Заң дербес деректерді жинауға, өңдеуге және қорғауға байланысты қатынастарды\n"
    "реттейді. Осы Заңның 5-бабында көзделген жағдайларды қоспағанда.\n"
)

# Law on Cybersecurity of Viet Nam (No. 24/2018/QH14), English translation.
ENGLISH_ARTICLES = (
    "LAW ON CYBERSECURITY\n"
    "Chapter I\n"
    "GENERAL PROVISIONS\n"
    "Article 1. Scope of regulation\n"
    "This Law provides for activities of protecting national security and ensuring social\n"
    "order and safety in cyberspace; and the responsibilities of relevant agencies,\n"
    "organizations and individuals.\n"
    "Article 2. Interpretation of terms\n"
    "In this Law, the terms below are construed as follows:\n"
    "1. Cyberspace means the connected network of information technology infrastructure.\n"
    "2. Cybersecurity means the assurance that activities in cyberspace do not harm\n"
    "national security, social order and safety.\n"
    "ARTICLE 3. State policies on cybersecurity\n"
    "1. To prioritize the protection of cybersecurity in national defense and security.\n"
    "Article 26 of this Law applies to the protection of children in cyberspace.\n"
)

# The same law in Vietnamese, articles 1 to 3.
VIETNAMESE = (
    "Điều 1. Phạm vi điều chỉnh\n"
    "Luật này quy định về hoạt động bảo vệ an ninh quốc gia và bảo đảm trật tự, an toàn\n"
    "xã hội trên không gian mạng; trách nhiệm của cơ quan, tổ chức, cá nhân có liên quan.\n"
    "Điều 2. Giải thích từ ngữ\n"
    "Trong Luật này, các từ ngữ dưới đây được hiểu như sau:\n"
    "Điều 3. Chính sách của Nhà nước về an ninh mạng\n"
    "Ưu tiên bảo vệ an ninh mạng trong quốc phòng, an ninh, phát triển kinh tế - xã hội.\n"
)

# Lao Instruction No. 0144 (Official Gazette), the OCR stream of articles 3 to
# 6 as stored. Article 4's number is glued to its title ("ມາດຕາ 4ການ..."), and
# the numbered items inside article 4 are the ambiguous "N. text" shape.
LAO_0144 = (
    "ມາດຕາ 3 ຂອບເຂດນໍາໃຊ້\n"
    "ຄໍາແນະນໍາສະບັບນີ ນໍາໃຊ້ຢູ່ບັນດາອົງການຈັດຕັ້ງພັກ-ລັດ, ກະຊວງ, ແຂວງ ແລະ ນະຄອນຫຼວງວຽງ\n"
    "ຈັນ ໃນຂອບເຂດທົວປະເທດ ທີ່ຄຸ້ມຄອງ ແລະ ນໍາໃຊ້ລະບົບຂໍ່ມູນຂ່າວສານຄຸ້ມຄອງຊັບສິນແຫ່ງລັດ ແບບເອ\n"
    "ເລັກໂຕຣນິກ (AMIS).\n"
    "ບນວດທີ 2\n"
    "ການຄຸ້ມຄອງ ແລະ ນໍາໃຊ້, ຂັນຕອນ ແລະ ກົນໄກການຄຸ້ມຄອງ\n"
    "ມາດຕາ 4ການຄຸ້ນຄອງ ແລະ ນໍາໃຊ້ ລະບົບຂໍ້ມູນຂ່າວສານຄຸ້ມຄອງຊັບສິນແຫ່ງລັດແບບເອເລັກ\n"
    "ໂຕຣນິກ (AMIS)\n"
    "1. ລັດຖະບານ ມອບສິດໃຫ້ກະຊວງການເງິນ ໃນການຄຸ້ມຄອງຊັບສິນ ທີ່ລັດລົງທຶນສ້າງຂຶ້ນ ຫຼື ໄດ້ມາ\n"
    "ດ້ວຍຄວາມຊອບທໍາຕາມກົດຫນາຍ ແລະ ລະບຽບການ ເຊິ່ງປະກອບດ້ວຍສັງຫາລິມະຊັບ ແລະ ອະສັງຫາລິມະ\n"
    "ຊັບ ທີ່ມອບໃຫ້ການຈັດຕັ້ງ, ບຸກຄົນ ແລະ ນິຕິບຸກຄົນ ເປັນຜູ້ຄຸ້ມຄອງນໍາໃຊ້ ແບບລວມສູນ ດ້ວຍການຂຶ້ນບັນ\n"
    "2. ນໍາໃຊ້ລະບົບທີ່ທັນສະໄຫນ: (1) ເພື່ອຄຸ້ມຄອງ ແລະ ສັງລວມຂໍ້ມູນຊັບສິນຂອງລັດ ໃຫ້ຄົບຖ້ວນ\n"
    "ແລະ ຊັດເຈນ ໃນຂອບເຂດທົ່ວປະເທດ ແລະ ຄຸ້ມຄອງຊັບສິນຂອງລັດບໍ່ໃຫ້ຕົກເຮ່ຍເສຍຫາຍ:; (2) ເພື່ອຄຸ້ມ\n"
    "ຄອງຖານລາຍຮັບຈາກຊັບສິນຂອງລັດໃຫ້ໄດ້ຄົບຖ້ວນ, ຖືກຕ້ອງຕາມກໍານົດເວລາ, ມີຄວາມໂປ່ງໃສ, ສະດວກ\n"
    "3. ການຂຶ້ນທະບຽນຊັບສິນ ແບບເອເລັກໂຕຣນິກ ສາມາດດໍາເນີນໄດ້ ຜ່ານການປ້ອນຂໍ້ມູນເຂົ້າລະບົບຂໍ້\n"
    "ມູນຂ່າວສານຄຸ້ມຄອງຊັບສິນແຫ່ງລັດ ໃນຮູບແບບອອນໄລນ໌; ລະບົບຈະກໍານົດການໃສ່ລະຫັດໃຫ້ຊັບສິນ ຫຼື\n"
    "ການໃສ່ເລກປະຈໍາຕົວໃຫ້ຊັບສິນແບບອັດຕະໂນມັດ (Automatic) ໂດຍອີງຕາມລາຍການຈັດລໍາດັບຂອງສູນ\n"
    "4. ການຂຶ້ນບັນຊີຊັບສິນ ແບບເອເລັກໂຕຣນິກ ແມ່ນການລວບລວມເອົາຂໍ້ມູນທັງຫົດ ຂອງຊັບສິນທີ່\n"
    "ໄດ້ຂຶ້ນທະບຽນແລ້ວບັນທຶກເຂົ້າໄວ້ໃນຖານຂໍ້ມູນ ແລະ ສາມາດສ້າງຕາຕະລາງສັງລວມລາຍງານຕາມລໍາດັບ\n"
    "ແລະ ແຍກຕາມແຕ່ລະບນວດ ແລະ ແຕ່ລະປະເພດຂອງຊັບສິນ;\n"
    "5. ການຄຸ້ມຄອງຖານລາຍຮັບຈາກຊັບສິນແຫ່ງລັດ ແມ່ນການປ້ອນຂໍ້ມູນເຂົ້າໃນລະບົບ ຕາມແບບຟອມ\n"
    "ກໍານົດ ເພື່ອບັນທຶກບັນດາຂໍ້ມູນທີ່ດິນ-ເຮືອນ, ສິ່ງປຸກສ້າງ, ຕຶກອາຄານ, ພາຫະນະ, ບັນດາສັນຍາສໍາປະທານ,\n"
    "ສັນຍາເຊົາຊັບສິນຂອງລັດ ແລະ ອື່ນໆ;\n"
    "6.ການດໍາເນີນວຽກງານຄຸ້ມຄອງຊັບສິນແຫ່ງລັດ ໃນກໍລະນີປະຕິບັດທາງເອເລັກໂຕຣນິກ ແມ່ນ\n"
    "ປະຕິບັດຕາມຂັ້ນຕອນຄືກັນກັບການປ້ອນຂໍ້ມູນເຂົ້າໃນແບບຟອມ Excel, ການຫັນເປັນທັນສະໄຫນ ຕ້ອງຜ່ານ\n"
    "ມາດຕາ 5 ຂັ້ນຕອນ ແລະ ກົນໄກການຄຸ້ມຄອງນໍາໃຊ້ລະບົບຂໍ້ມູນຂ່າວສານຄຸ້ມຄອງຊັບສິນແຫ່ງລັດ\n"
    "1. ຂັ້ນຕອນການຄຸ້ມຄອງນໍາໃຊ້ລະບົບ ໃຫ້ປະຕິບັດຕາມຄໍາແນະນໍາຂອງກະຊວງການເງິນ.\n"
    "ມາດຕາ 6 ຫາທຮບຜດຊອບຂອງແຕລະພາກສວນ\n"
    "ກະຊວງການເງິນ ເປັນຜູ້ຄົ້ນຄວ້າ ກໍານົດ ແຜ່ນກາຫນາຍການນໍາໃຊ້ຊັບສິນຂອງລັດ ປະເພດຕ່າງໆ.\n"
)


# A Timor-Leste Decree-Law as the Jornal da República prints it: the article
# word and number alone on the line ("Artigo 1.º"), the title on the next.
PORTUGUESE = (
    "Assim,\n"
    "O Governo decreta, nos termos do n.o 3 do artigo 115.º da\n"
    "Constituição da República, para valer como lei, o seguinte:\n"
    "Artigo 1.º\n"
    "Objeto\n"
    "O presente diploma cria o subsídio de risco para os\n"
    "trabalhadores que exercem funções no Instituto de Gestão de\n"
    "Equipamentos e Apoio ao Desenvolvimento de Infraestruturas.\n"
    "Artigo 2.º\n"
    "Subsídio de risco\n"
    "1. O subsídio de risco criado pelo artigo anterior constitui\n"
    "uma prestação pecuniária com natureza de suplemento\n"
    "remuneratório.\n"
    "Artigo 3.º\n"
    "Beneficiários\n"
    "São beneficiários do subsídio de risco os trabalhadores que\n"
    "exerçam funções no IGEADI, nos termos do\n"
    "Artigo 2.º da Lei n.º 8/2004, de 16 de junho.\n"
)


class TestMoreDraftingTraditions:
    def test_mongolian_articles(self):
        doc = _doc(MONGOLIAN)
        chunks, report = split_document(doc)
        assert report.fallback_used is False
        assert _section_labels(chunks) == ["s. 1", "s. 2", "s. 3"]
        # the chapter heading travels with article 1; the cross-reference
        # "8 дугаар зүйлд заасан" wrapped onto a new line is not a heading
        assert "НЭГДҮГЭЭР БҮЛЭГ\nНИЙТЛЭГ ҮНДЭСЛЭЛ\n1 дүгээр" in sections(chunks)[0].text
        assert "8 дугаар зүйлд" in sections(chunks)[2].text
        assert_partition(chunks, doc)

    @pytest.mark.parametrize(
        "heading",
        ["{n} дүгээр зүйл.", "{n} дугаар зүйл.", "{n}-р зүйл.", "{n}-р зүйл ", "Зүйл {n}."],
    )
    def test_every_mongolian_numbering_form(self, heading):
        body = "Энэ хуулийн зорилт нь хувь хүний мэдээллийг хамгаалахад оршино.\n"
        text = "".join(f"{heading.format(n=n)}Зорилт\n{body}" for n in range(1, 4))
        chunks, _ = split_document(_doc(text))
        assert _section_labels(chunks) == ["s. 1", "s. 2", "s. 3"]

    def test_kazakh_articles(self):
        doc = _doc(KAZAKH)
        chunks, report = split_document(doc)
        assert report.fallback_used is False
        assert _section_labels(chunks) == ["s. 1", "s. 2", "s. 3"]
        assert "5-бабында" in sections(chunks)[2].text
        assert_partition(chunks, doc)

    def test_kazakh_heading_without_the_hyphen(self):
        body = "Осы Заң дербес деректерді қорғауға байланысты қатынастарды реттейді.\n"
        text = "".join(f"{n} бап. Ұғымдар\n{body}" for n in range(1, 4))
        chunks, _ = split_document(_doc(text))
        assert _section_labels(chunks) == ["s. 1", "s. 2", "s. 3"]

    @pytest.mark.parametrize(
        "heading",
        ["Статья {n}. Понятия", "Article {n}. Definitions", "{n}-бап. Ұғымдар"],
    )
    def test_a_hyphenated_article_number_is_its_own_article(self, heading):
        """An article inserted by amendment ("Статья 8-1", "8-1-бап") is an
        article of its own, not the tail of article 8."""
        body = "Осы Заң дербес деректерді қорғауға байланысты қатынастарды реттейді.\n"
        text = "".join(f"{heading.format(n=n)}\n{body}" for n in ("7", "8", "8-1", "9"))
        doc = _doc(text)
        chunks, report = split_document(doc)
        assert report.fallback_used is False
        assert _section_labels(chunks) == ["s. 7", "s. 8", "s. 8-1", "s. 9"]
        assert_partition(chunks, doc)

    def test_portuguese_articles(self):
        doc = _doc(PORTUGUESE)
        chunks, report = split_document(doc)
        assert report.fallback_used is False
        assert _section_labels(chunks) == ["s. 1", "s. 2", "s. 3"]
        # a sentence wrapped just before a cross-reference is not a heading
        assert "Artigo 2.º da Lei n.º 8/2004" in sections(chunks)[2].text
        assert_partition(chunks, doc)

    @pytest.mark.parametrize(
        "heading",
        ["Artigo {n}.º", "Artigo {n}º", "Artigo {n}.o", "Artigo {n}.°", "ARTIGO {n}.º",
         "Artigo {n}", "Artigu {n}.º", "Artigo {n}.º\u00a0"],
    )
    def test_every_portuguese_and_tetum_numbering_form(self, heading):
        body = "O presente diploma aplica-se aos prestadores de serviços.\n"
        text = "".join(f"{heading.format(n=n)}\nObjeto\n{body}" for n in range(1, 4))
        chunks, report = split_document(_doc(text))
        assert report.fallback_used is False
        assert _section_labels(chunks) == ["s. 1", "s. 2", "s. 3"]

    def test_a_portuguese_article_inserted_by_amendment_is_its_own_article(self):
        """"Artigo 6.º-A" sits between articles 6 and 7."""
        body = "O município tem centro administrativo na vila.\n"
        text = "".join(f"Artigo {n}\nMunicípio\n{body}" for n in ("5.º", "6.º", "6.º-A", "7.º"))
        doc = _doc(text)
        chunks, _ = split_document(doc)
        assert _section_labels(chunks) == ["s. 5", "s. 6", "s. 6A", "s. 7"]
        assert_partition(chunks, doc)

    def test_a_title_on_the_heading_line_is_kept(self):
        body = "O presente diploma aplica-se aos prestadores de serviços.\n"
        text = "".join(f"Artigo {n}.º Objeto\n{body}" for n in range(1, 4))
        chunks, _ = split_document(_doc(text))
        assert _section_labels(chunks) == ["s. 1", "s. 2", "s. 3"]

    def test_a_hyphenated_number_in_prose_is_not_an_article(self):
        body = "The operator shall keep a record of the processing.\n"
        text = (
            "".join(f"Article {n}. Definitions\n{body}" for n in ("7", "8"))
            + "Article 8-10 The operator shall notify the authority.\n"
            + f"Article 9. Scope\n{body}"
        )
        chunks, _ = split_document(_doc(text))
        assert "s. 8-10" not in _section_labels(chunks)

    def test_english_article_headings(self):
        doc = _doc(ENGLISH_ARTICLES)
        chunks, report = split_document(doc)
        assert report.style == "article_en"
        # "Article 26 of this Law ..." is a sentence, not a heading
        assert _section_labels(chunks) == ["s. 1", "s. 2", "s. 3"]
        assert "Article 26 of this Law" in sections(chunks)[2].text
        assert_partition(chunks, doc)

    def test_a_convention_scheduled_to_an_english_act_does_not_take_over(self):
        """An English act that schedules a convention carries "Article N"
        headings AFTER its own sections. Its sections stay the structure."""
        body = "The Minister may, by legislative instrument, make rules for the purposes. " * 4
        act = "".join(f"{n} Heading of section {n}\n{body}\n" for n in range(1, 13))
        convention = "Schedule 1\n" + "".join(
            f"Article {n} Obligations of the parties\n{body}\n" for n in range(1, 4)
        )
        doc = _doc(act + convention)
        chunks, report = split_document(doc)
        assert report.style == "num_title"
        assert "s. 12" in _section_labels(chunks)

    def test_decomposed_vietnamese_splits_after_nfc(self):
        import unicodedata

        from regcompass.chunk import ARTICLE_WORD_RE

        decomposed = unicodedata.normalize("NFD", VIETNAMESE)
        assert decomposed != VIETNAMESE
        # the heading word as some PDFs store it cannot match the pattern ...
        assert ARTICLE_WORD_RE.match(decomposed.split("\n")[0]) is None
        doc = _doc(decomposed)
        # ... and the canonical stream is composed once, at the stream itself
        assert doc.full_text == VIETNAMESE
        chunks, report = split_document(doc)
        assert report.style == "article_word"
        assert _section_labels(chunks) == ["s. 1", "s. 2", "s. 3"]

    def test_lao_glued_numbers_and_articles_over_numbered_items(self):
        doc = _doc(LAO_0144)
        chunks, report = split_document(doc)
        assert report.style == "article_word"
        assert _section_labels(chunks) == ["s. 3", "s. 4", "s. 5", "s. 6"]
        assert_partition(chunks, doc)

    def test_lao_article_word_as_the_ocr_misreads_it(self):
        body = "ຄໍາແນະນໍາສະບັບນີ ນໍາໃຊ້ຢູ່ບັນດາອົງການຈັດຕັ້ງພັກ-ລັດ.\n"
        text = f"ມາດຕາ 11 ການຄໍ້າປະກັນ\n{body}ນາດຕາ 12 ການຖອນວົງເງິນຄ້າປະ ກັນ\n{body}"
        chunks, _ = split_document(_doc(text))
        assert _section_labels(chunks) == ["s. 11", "s. 12"]


class TestTheStreamIsComposedOnce:
    def test_page_and_word_offsets_follow_the_composed_text(self):
        import unicodedata

        from regcompass.contracts import WordBox

        pages = [unicodedata.normalize("NFD", "Điều 1. Phạm vi"), unicodedata.normalize("NFD", "Điều 2. Giải thích")]
        text = "\n".join(pages)
        second = len(pages[0]) + 1
        words = [
            WordBox(text=w, page=p, x0=0, y0=0, x1=1, y1=1, char_start=s, char_end=s + len(w))
            for w, p, s in (
                (unicodedata.normalize("NFD", "Phạm"), 1, pages[0].index(unicodedata.normalize("NFD", "Phạm"))),
                (unicodedata.normalize("NFD", "thích"), 2, second + pages[1].index(unicodedata.normalize("NFD", "thích"))),
            )
        ]
        doc = CanonicalText(
            document_id="d", source_sha256="0" * 64, extractor="t", extractor_version="0",
            full_text=text,
            pages=[
                PageSpan(page_number=1, char_start=0, char_end=len(pages[0])),
                PageSpan(page_number=2, char_start=second, char_end=second + len(pages[1])),
            ],
            words=words,
        )
        assert doc.full_text == "Điều 1. Phạm vi\nĐiều 2. Giải thích"
        assert [doc.slice(p.char_start, p.char_end) for p in doc.pages] == [
            "Điều 1. Phạm vi", "Điều 2. Giải thích"]
        assert [doc.slice(w.char_start, w.char_end) for w in doc.words] == ["Phạm", "thích"]
        assert [w.text for w in doc.words] == ["Phạm", "thích"]

    def test_composed_text_is_left_exactly_as_it_was(self, sg):
        again = CanonicalText.model_validate_json(sg.model_dump_json())
        assert again.full_text == sg.full_text
        assert again.pages == sg.pages
        assert again.words == sg.words


# ---------------------------------------------------------------------------
# The passage fallback: a Document with no credible structure is split into
# numbered passages of about a section's size, so the Gate still reads it.
# ---------------------------------------------------------------------------

NOTICE_PARAGRAPH = (
    "The Ministry reminds all service providers that personal information collected in the\n"
    "course of providing an online service shall be stored on servers located within the\n"
    "territory and shall not be transferred abroad without the approval of the Ministry.\n"
    "Providers shall notify the Ministry of any breach within seventy-two hours of discovery.\n"
)


class TestPassageFallback:
    def test_an_unstructured_document_becomes_numbered_passages(self):
        from regcompass.chunk import PASSAGE_CHARS

        text = "".join(NOTICE_PARAGRAPH + ("\n" if i % 3 == 2 else "") for i in range(24))
        doc = _doc(text, "doc_x_notice")
        chunks, report = split_document(doc)
        assert report.style == "passages"
        assert report.unstructured is True
        assert report.fallback_used is True
        assert len(chunks) > 3
        assert [c.section_label for c in chunks] == [f"Passage {i}" for i in range(1, len(chunks) + 1)]
        assert {c.chunk_kind for c in chunks} == {"section"}
        assert report.n_sections == len(chunks)
        for c in chunks[:-1]:
            assert PASSAGE_CHARS // 2 <= len(c.text) <= PASSAGE_CHARS * 3 // 2
            assert c.text.endswith("\n"), "a passage ends on a line boundary"
        assert_partition(chunks, doc)

    def test_a_short_document_is_one_passage(self):
        doc = _doc(NOTICE_PARAGRAPH)
        chunks, report = split_document(doc)
        assert [(c.section_label, c.chunk_kind) for c in chunks] == [("Passage 1", "section")]
        assert report.unstructured is True

    def test_a_structured_document_is_not_marked_unstructured(self, my):
        _, report = split_document(my)
        assert report.unstructured is False

    def test_a_document_with_no_line_breaks_still_splits(self):
        text = NOTICE_PARAGRAPH.replace("\n", " ") * 12
        doc = _doc(text)
        chunks, _ = split_document(doc)
        assert len(chunks) > 1
        assert_partition(chunks, doc)

    def test_an_empty_document_has_no_chunks(self):
        chunks, report = split_document(_doc(""))
        assert chunks == []
        assert report.coverage_chars == 0


def _translated_statute(clauses: int, articles: int = 40) -> str:
    """An English translation in the Vietnamese drafting style: every article
    numbers its own clauses from 1 again."""
    clause = (
        "agencies, organizations and individuals shall store the personal data of"
        " users in the territory for the period prescribed by the Government and"
        " shall provide it to the competent authority on request, within the time"
        " limit and in the form that the Ministry of Public Security prescribes"
        " for the verification, investigation and handling of violations."
    )
    return "".join(
        f"Article {a}. Duties of enterprises providing service type {a}\n"
        + "".join(f"{c}. Under this clause {c} of article {a}, {clause}\n" for c in range(1, clauses + 1))
        for a in range(1, articles + 1)
    )


class TestArticlesOverRestartingClauses:
    @pytest.mark.parametrize("clauses", [3, 5, 8])
    def test_a_translation_splits_on_its_articles(self, clauses):
        doc = _doc(_translated_statute(clauses))
        chunks, report = split_document(doc)
        assert report.style == "article_en"
        assert _section_labels(chunks) == [f"s. {a}" for a in range(1, 41)]
        assert_partition(chunks, doc)

    @pytest.mark.parametrize("clauses", [3, 5, 8])
    def test_the_label_index_reads_the_article_not_the_clause(self, clauses):
        from regcompass.chunk import SectionLabelIndex

        text = _translated_statute(clauses)
        pos = text.index("Article 17.")
        pos = text.index("3. Under", pos)
        assert SectionLabelIndex(text).label_at(pos) == "s. 17"
