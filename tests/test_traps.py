"""The Indicator Reference's scoring traps, checked on every exported row so a
live-hour export carries the same warnings as the submission workbook."""

from regcompass.traps import trap_flags, flag_traps


def row(**over):
    base = {
        "Law Name": "Personal Data Protection Act 2010",
        "Article / Section": "s. 129(1)",
        "Verbatim Snippet": "A data user shall not transfer any personal data of a data subject to any place outside Malaysia.",
        "Notes": "",
        "_repeal_status": "",
        "_economy_code": "MY",
    }
    base.update(over)
    return base


class TestDrafts:
    def test_draft_names_are_flagged(self):
        for name in (
            "Draft Personal Data Protection Bill 2025",
            "Personal Data Protection Bill",
            "Rancangan Undang-Undang Keamanan Siber",
            "RUU Perlindungan Data Pribadi",
            "网络数据安全管理条例（征求意见稿）",
            "个人信息保护法（草案）",
        ):
            assert any("draft" in f for f in trap_flags(row(**{"Law Name": name}), "6.1")), name

    def test_an_enacted_act_is_not_a_draft(self):
        assert trap_flags(row(), "6.2") == []


class TestRepealed:
    def test_repealed_in_the_name_or_the_status(self):
        assert any("repealed" in f for f in trap_flags(row(**{"Law Name": "Data Act 1998 (repealed)"}), "6.2"))
        assert any("repealed" in f for f in trap_flags(row(_repeal_status="repealed by Act 26 of 2023"), "6.2"))

    def test_in_force_wording_is_not_repealed(self):
        for status in ("not repealed", "unrepealed", "in force", "In force, not repealed",
                       "has not been repealed", "never repealed"):
            assert not any("repealed" in f for f in trap_flags(row(_repeal_status=status), "6.2")), status
        for name in ("Data Act 1998 (not repealed)", "Unrepealed Ordinance 1950"):
            assert not any("repealed" in f for f in trap_flags(row(**{"Law Name": name}), "6.2")), name

    def test_not_flagged_twice_when_the_export_already_notes_it(self):
        r = row(_repeal_status="repealed 2023", Notes="repealed-but-recorded: repealed 2023")
        assert trap_flags(r, "6.2") == []


class TestAmendingAct:
    def test_amending_acts(self):
        for name in (
            "Privacy Amendment (Notifiable Data Breaches) Act 2017",
            "Personal Data Protection (Amendment) Act 2024",
            "UU Nomor 19 Tahun 2016",
            "UU Nomor 1 Tahun 2024",
            "Undang-Undang tentang Perubahan atas UU Nomor 11 Tahun 2008",
            "中华人民共和国网络安全法修正案",
        ):
            assert any("amending act" in f for f in trap_flags(row(**{"Law Name": name}), "7.2")), name

    def test_the_principal_act_is_not_flagged(self):
        assert trap_flags(row(**{"Law Name": "UU Nomor 11 Tahun 2008"}), "7.2") == []


class TestIndicatorShapes:
    def test_7_3_without_a_duration(self):
        r = row(**{"Verbatim Snippet": "The operator shall retain the records for such period as may be prescribed."})
        assert any("7.3" in f for f in trap_flags(r, "7.3"))
        for snippet in (
            "shall retain the records for a period of not less than 7 years",
            "wajib menyimpan data paling singkat 5 (lima) tahun",
            "网络日志留存不少于六个月",
            "shall retain the logs for sixty days",
            "for a period of ninety (90) days",
            "retain for forty-five days",
            "retain for seventy two hours",
            "keep them for eighty days",
            "for twenty-four months",
            "not less than thirty-six months",
            "for one hundred and eighty days",
            "for a period of eleven years",
        ):
            assert trap_flags(row(**{"Verbatim Snippet": snippet}), "7.3") == [], snippet

    def test_6_1_with_a_condition(self):
        r = row(**{"Verbatim Snippet": "A data user shall not transfer personal data outside Malaysia unless the Minister so specifies."})
        assert any("6.4" in f for f in trap_flags(r, "6.1"))
        assert trap_flags(row(), "6.1") == []

    def test_elucidation(self):
        r = row(**{"Article / Section": "Elucidation of Art. 26"})
        assert any("Elucidation" in f for f in trap_flags(r, "6.4"))


def test_flag_traps_appends_to_notes():
    r = row(**{"Law Name": "Personal Data Protection Bill", "Notes": "provision has no internal subsection numbering"})
    flag_traps(r, "6.2")
    assert r["Notes"].startswith("provision has no internal subsection numbering; Check before submitting:")
    clean = row()
    flag_traps(clean, "6.2")
    assert clean["Notes"] == ""
