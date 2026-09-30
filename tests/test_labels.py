"""Provision labels use the Economy's drafting word: Articles for civil-law
statutes, sections for common-law ones."""

from regcompass.labels import drafting_label


def test_chinese_and_indonesian_labels_read_as_articles():
    assert drafting_label("Chapter 3 s. 40", "CN") == "Chapter 3 Art. 40"
    assert drafting_label("s. 56(2)", "ID") == "Art. 56(2)"
    assert drafting_label("s. 26A", "ID") == "Art. 26A"


def test_other_article_economies():
    for eco in ("LA", "VN", "KZ", "RU", "MN", "TL"):
        assert drafting_label("s. 5", eco) == "Art. 5"


def test_common_law_and_thai_labels_keep_sections():
    for eco in ("AU", "SG", "MY", "IN", "TH"):
        assert drafting_label("Part II s. 43A(1)", eco) == "Part II s. 43A(1)"


def test_labels_without_a_section_word_are_unchanged():
    assert drafting_label("", "CN") == ""
    assert drafting_label("Elucidation of Article 26", "ID") == "Elucidation of Article 26"
    assert drafting_label("Schedule 1", "CN") == "Schedule 1"
    assert drafting_label("s. 40", None) == "s. 40"
