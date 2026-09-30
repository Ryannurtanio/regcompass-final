"""The rationale's plain wording: "rung N" is the mapping prompt's word for the
Nth line of an Indicator's RDTII scoring criteria, and a reviewer reading the
export or the Evidence detail should see the RDTII's own words instead."""

from regcompass.wording import plain_rationale


class TestNumberedRung:
    def test_each_rung_names_its_criterion_and_score(self):
        # 1.4 carries five score levels: 1, 0.75, 0.50, 0.25, 0
        expected = {1: "1", 2: "0.75", 3: "0.50", 4: "0.25", 5: "0"}
        for n, score in expected.items():
            assert plain_rationale(f"Three measures, matching rung {n}.", "1.4") == (
                f"Three measures, matching RDTII criterion {n} (score {score})."
            )

    def test_the_china_example(self):
        text = "Requires ride-hailing platforms to file vehicle data, a sector-specific restriction, matching rung 2."
        assert plain_rationale(text, "6.1") == (
            "Requires ride-hailing platforms to file vehicle data, a sector-specific"
            " restriction, matching RDTII criterion 2 (score 0.5)."
        )

    def test_rung_of_the_scoring_ladder_is_one_phrase(self):
        assert plain_rationale(
            "Conditions transfers on consent, matching rung 1 of the scoring ladder for indicator 6.4.", "6.4"
        ) == "Conditions transfers on consent, matching RDTII criterion 1 (score 1)."
        assert plain_rationale(
            "A comprehensive framework, matching rung 3 of the scoring ladder.", "7.1"
        ) == "A comprehensive framework, matching RDTII criterion 3 (score 0)."

    def test_leading_and_parenthesised_rungs(self):
        assert plain_rationale("Rung 2: rules for one sector's records.", "6.2") == (
            "RDTII criterion 2 (score 0.5): rules for one sector's records."
        )
        assert plain_rationale("A sectoral law (rung 2).", "7.1") == (
            "A sectoral law (RDTII criterion 2 (score 0.5))."
        )
        assert plain_rationale("Fits the sectoral-law rung (2).", "7.1") == (
            "Fits the sectoral-law RDTII criterion 2 (score 0.5)."
        )

    def test_indicator_named_before_the_rung_is_not_doubled(self):
        assert plain_rationale("RDTII 7.3 rung 1: minimum retention of 5 years.", "7.3") == (
            "RDTII 7.3 criterion 1 (score 1): minimum retention of 5 years."
        )
        assert plain_rationale("Matching RDTII rung 3.", "7.4") == "Matching RDTII criterion 3 (score 0)."

    def test_indicator_without_score_levels_shows_no_score(self):
        # 6.5 is treaty participation: indicators.json gives it no score levels
        assert plain_rationale("Joins a treaty, matching rung 1.", "6.5") == (
            "Joins a treaty, matching RDTII criterion 1."
        )

    def test_a_rung_past_the_indicator_levels_shows_no_score(self):
        assert plain_rationale("Matches rung 4.", "7.3") == "Matches RDTII criterion 4."

    def test_unknown_indicator_shows_no_score(self):
        assert plain_rationale("Matches rung 1.", "99.9") == "Matches RDTII criterion 1."


class TestLadderAndBareRung:
    def test_scoring_ladder(self):
        assert plain_rationale("Meets no line of the scoring ladder.", "6.1") == (
            "Meets no line of the RDTII scoring criteria."
        )
        assert plain_rationale("Fits the indicator's ladder.", "6.1") == (
            "Fits the indicator's RDTII scoring criteria."
        )
        assert plain_rationale("Fits RDTII 6.4's scoring ladder.", "6.4") == (
            "Fits RDTII 6.4's scoring criteria."
        )

    def test_rung_of_the_ladder(self):
        assert plain_rationale("Does not meet any rung of the scoring ladder.", "6.1") == (
            "Does not meet any RDTII scoring criterion."
        )

    def test_bare_rung(self):
        assert plain_rationale("Matches the 'infrastructure requirement' rung.", "6.3") == (
            "Matches the 'infrastructure requirement' criterion."
        )
        assert plain_rationale("Meets the indicator's first rung.", "7.5") == (
            "Meets the indicator's first criterion."
        )
        assert plain_rationale("Rungs one and two both apply.", "6.1") == (
            "Criteria one and two both apply."
        )


class TestUnchanged:
    def test_rationale_without_rung_is_unchanged(self):
        assert plain_rationale("Requires personal data to be stored in Indonesia.", "6.2") == (
            "Requires personal data to be stored in Indonesia."
        )
        # a word that only contains the letters is not the jargon
        assert plain_rationale("Wrung from the operator; see Brungs v. State.", "6.2") == (
            "Wrung from the operator; see Brungs v. State."
        )

    def test_empty_and_none(self):
        assert plain_rationale(None, "6.1") is None
        assert plain_rationale("", "6.1") == ""

    def test_mixed_script_rationale_is_left_as_it_is(self):
        text = "Requires网约车平台公司 to报备车辆相关信息 to the local taxi authority."
        assert plain_rationale(text, "6.1") == text
