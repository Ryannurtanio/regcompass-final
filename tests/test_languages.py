"""The Language lane: the tesseract string a Document's Language asks for, the
Latin-script test that decides whether the English OCR proxies mean anything,
and the keyword-tier test that decides whether the Gate's bm25 tier can read
the Document at all.

Every organizer Language is covered, including the two that collapse to
"Other" (Malay and Portuguese), because "Other" alone cannot pick a tesseract
string: it needs the Economy as a second key.
"""

from __future__ import annotations

import pytest

from regcompass.contracts import ORGANIZER_LANGUAGES
from regcompass.languages import (
    KEYWORD_TIER_LANGUAGES,
    TESSERACT_BY_LANGUAGE,
    is_latin_script_language,
    keyword_tier_applies,
    non_latin_share,
    tesseract_language_note,
    tesseract_languages,
)

LAO = "ມາດຕາ 5 ຫຼັກການກ່ຽວກັບວຽກງານທຸລະກໍາທາງເອເລັກໂຕຣນິກ"
THAI = "มาตรา ๕ ธุรกรรมทางอิเล็กทรอนิกส์"
ENGLISH = "Section 5. A licensee shall protect personal data held under this Act."
PORTUGUESE = "Artigo 5.º\nO presente diploma aplica-se aos prestadores de serviços da sociedade da informação."


class TestTesseractLanguages:
    @pytest.mark.parametrize(
        ("language", "economy", "expected"),
        [
            ("English", "AU", "eng"),
            ("English", "SG", "eng"),
            ("English", "LA", "eng"),
            # Malaysia is the one Economy whose "Other" means Malay, and its
            # acts are English-bodied with Malay provisions: unchanged string.
            ("English", "MY", "eng+msa"),
            ("Other", "MY", "eng+msa"),
            (None, "MY", "eng+msa"),
            ("Bahasa Indonesia", "ID", "ind+eng"),
            ("Thai", "TH", "tha+eng"),
            ("Lao", "LA", "lao+eng"),
            ("Russian", "RU", "rus+eng"),
            ("Vietnamese", "VN", "vie+eng"),
            ("Chinese", "CN", "chi_sim+eng"),
            ("Hindi", "IN", "hin+eng"),
            ("Kazakh", "KZ", "kaz+eng"),
            ("Mongolian", "MN", "mon+eng"),
            # Timor-Leste's "Other" is Portuguese (its Tetum is written in
            # the same alphabet), read with the Portuguese data.
            ("Other", "TL", "por+eng"),
            (None, "TL", "por+eng"),
            ("English", "TL", "eng"),
            # A Language with no vendored traineddata falls back to English
            # rather than crashing tesseract with a missing file.
            ("Other", "KH", "eng"),
            (None, "SG", "eng"),
        ],
    )
    def test_the_table(self, language, economy, expected):
        assert tesseract_languages(language, economy) == expected

    def test_every_organizer_language_resolves(self):
        for language in ORGANIZER_LANGUAGES:
            assert tesseract_languages(language, "ID")

    def test_an_unknown_language_falls_back_with_a_note(self):
        assert tesseract_languages("Klingon", "ID") == "eng"
        note = tesseract_language_note("Klingon", "ID")
        assert note is not None and "Klingon" in note and "eng" in note

    def test_a_mapped_language_has_no_note(self):
        assert tesseract_language_note("Lao", "LA") is None
        assert tesseract_language_note("Other", "MY") is None

    def test_every_organizer_language_but_other_has_its_own_data(self):
        """Every national script on the organizers' list is read in its own
        script; only "Other" outside Malaysia falls back to English."""
        for language in ORGANIZER_LANGUAGES:
            if language != "Other":
                assert tesseract_language_note(language, "XX") is None, language

    def test_every_mapped_code_is_vendored(self):
        from regcompass.ocr import VENDOR_TESSDATA

        codes = {c for s in TESSERACT_BY_LANGUAGE.values() for c in s.split("+")}
        codes.add("msa")
        missing = [c for c in sorted(codes) if not (VENDOR_TESSDATA / f"{c}.traineddata").is_file()]
        assert not missing, f"tesseract codes with no vendored traineddata: {missing}"


class TestScriptTest:
    def test_latin_and_non_latin_share(self):
        assert non_latin_share(ENGLISH) == 0.0
        assert non_latin_share(LAO) > 0.9
        assert non_latin_share(THAI) > 0.9
        assert non_latin_share("") == 0.0
        assert non_latin_share("12345 -- (2)(a)") == 0.0  # no letters at all

    def test_vietnamese_diacritics_count_as_latin(self):
        assert non_latin_share("Điều 5. Giao dịch điện tử được thực hiện") < 0.05

    def test_latin_script_languages(self):
        assert is_latin_script_language("English")
        assert is_latin_script_language("Bahasa Indonesia")
        assert is_latin_script_language("Vietnamese")
        assert is_latin_script_language("Other")  # Malay, Portuguese, Tetum
        assert is_latin_script_language(None)
        for language in ("Thai", "Lao", "Russian", "Chinese", "Hindi", "Kazakh", "Mongolian"):
            assert not is_latin_script_language(language)


class TestKeywordTier:
    def test_english_keeps_both_tiers(self):
        assert keyword_tier_applies("English", ENGLISH)
        assert keyword_tier_applies(None, ENGLISH)

    def test_other_stays_on_the_english_path(self):
        """Malaysia's acts are English-bodied; "Other" must not pull MY into
        the meaning-only lane."""
        assert keyword_tier_applies("Other", ENGLISH)
        assert KEYWORD_TIER_LANGUAGES == frozenset({"English", "Other"})

    @pytest.mark.parametrize(
        "language",
        ["Lao", "Thai", "Russian", "Chinese", "Vietnamese", "Mongolian", "Kazakh", "Hindi",
         "Bahasa Indonesia"],
    )
    def test_non_english_languages_drop_the_keyword_tier(self, language):
        assert not keyword_tier_applies(language, ENGLISH)

    def test_timor_leste_portuguese_is_shortlisted_by_meaning(self):
        """Timor-Leste's "Other" is Portuguese, which the English vocabulary
        scores zero, so its Documents take the meaning-only lane; Malaysia's
        "Other" and Timor-Leste's own English texts keep both tiers."""
        assert not keyword_tier_applies("Other", PORTUGUESE, economy="TL")
        assert not keyword_tier_applies(None, PORTUGUESE, economy="TL")
        assert keyword_tier_applies("English", ENGLISH, economy="TL")
        assert keyword_tier_applies("Other", ENGLISH, economy="MY")

    def test_a_mislabelled_document_is_caught_by_the_script_test(self):
        """A Document labelled English whose text is Lao still routes to the
        meaning-only Gate: the text wins over the label."""
        assert not keyword_tier_applies("English", LAO * 20)

    def test_a_few_non_latin_characters_do_not_flip_an_english_document(self):
        assert keyword_tier_applies("English", ENGLISH * 40 + LAO)


class TestOcrLanguagesForAGarbageLayer:
    """A text layer judged garbage is read by OCR in every script the Document
    could be in: what the layer still shows, its own Language, and the
    Languages its Economy publishes in."""

    def test_a_hindi_gazette_filed_as_english_is_read_with_hindi(self):
        from regcompass.languages import garbage_ocr_languages

        dropped = "] 417\n, ;\n1. (1) , ,\n(2) ,\n"
        assert garbage_ocr_languages("English", "IN", dropped, ("English", "Hindi")) == (
            "hin+eng", "Hindi")

    def test_the_script_left_in_the_layer_counts(self):
        from regcompass.languages import garbage_ocr_languages

        thai = "มาตรา ๑ พระราชบัญญัตินี้เรียกว่า พระราชบัญญัติคุ้มครองข้อมูลส่วนบุคคล " * 3
        assert garbage_ocr_languages("English", "XX", thai, ()) == ("tha+eng", "Thai")

    def test_cyrillic_follows_the_economys_languages(self):
        from regcompass.languages import garbage_ocr_languages

        cyr = "Осы Заң дербес деректерді жинауға байланысты қатынастарды реттейді " * 3
        langs, reading = garbage_ocr_languages("Russian", "KZ", cyr, ("Russian", "Kazakh"))
        assert set(langs.split("+")) == {"rus", "kaz", "eng"} and langs.endswith("+eng")
        assert reading == "Russian"

    def test_lao_stays_lao(self):
        from regcompass.languages import garbage_ocr_languages

        assert garbage_ocr_languages("Lao", "LA", "iimijCSC;Jn1nsun", ("Lao", "English")) == (
            "lao+eng", "Lao")
