"""M12 classify tests: closed-menu schema, 3-attempt budget, and EVERY branch
of the scoring rubric.

All offline: the model is a canned completion_fn. The rubric tests encode the
Guide's per-indicator criteria, never the Round 1 Database cell values."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from regcompass.classify import (
    ABSENCE_SCORE,
    ELIGIBLE_NATURES,
    ClassifyOutcome,
    ProvisionClassification,
    _parse_classification,
    _response_format,
    apply_classifications,
    build_classify_prompt,
    classify_record,
    derive_scores_v2,
    record_contribution,
)
from regcompass.contracts import IndicatorDef, MappingRecord

QUOTE = "shall not transfer personal data outside the jurisdiction unless conditions are met"
CHUNK = f"Section 26. Transfer limitation.\n(1) An organisation {QUOTE}.\n(2) Exceptions apply."

IND_DEF = IndicatorDef(
    pillar=6,
    name="Ban on cross-border data flows",
    definition="Bans or local processing.",
)


def make_record(
    indicator: str = "6.1",
    economy: str = "SG",
    mapping_id: str | None = None,
    chunk_id: str = "doc_x:c0001",
    document_id: str = "doc_x",
    section: str = "s. 26",
    quote: str = QUOTE,
    status: str = "passed",
) -> MappingRecord:
    return MappingRecord(
        mapping_id=mapping_id or f"{chunk_id}::{indicator}",
        document_id=document_id,
        chunk_id=chunk_id,
        economy=economy,
        indicator_id=indicator,
        indicator_name="test indicator",
        section=section,
        verbatim_quote=quote,
        verification_status=status,
    )


def cls(**overrides) -> ProvisionClassification:
    base = dict(
        measure_nature="transfer_ban",
        data_scope="personal",
        application="horizontal",
        government_data_only=False,
    )
    base.update(overrides)
    return ProvisionClassification(**base)


def valid_json(**overrides) -> str:
    base = dict(
        measure_nature="transfer_ban",
        data_scope="personal",
        application="horizontal",
        government_data_only=False,
        minimum_period_specified=None,
        dedicated_framework=None,
        comprehensive_framework=None,
        judicial_authorization_required=None,
    )
    base.update(overrides)
    return json.dumps(base)


# ---------------------------------------------------------------------------
# schema: closed menus, forbidden extras
# ---------------------------------------------------------------------------


class TestSchema:
    def test_valid_labels_parse(self):
        c = _parse_classification(valid_json())
        assert isinstance(c, ProvisionClassification)
        assert c.measure_nature == "transfer_ban"

    def test_extra_key_is_malformed(self):
        r = _parse_classification(valid_json()[:-1] + ', "confidence": 0.9}')
        assert isinstance(r, str) and "schema violation" in r

    def test_off_menu_value_is_malformed(self):
        r = _parse_classification(valid_json(measure_nature="data_ban"))
        assert isinstance(r, str) and "schema violation" in r

    def test_fenced_json_is_recovered(self):
        r = _parse_classification("```json\n" + valid_json() + "\n```")
        assert isinstance(r, ProvisionClassification)

    def test_response_format_is_strict_and_closed(self):
        schema = _response_format()["json_schema"]
        assert schema["strict"] is True
        inner = schema["schema"]
        assert inner["additionalProperties"] is False
        assert set(inner["required"]) == set(inner["properties"])
        # every model field except the code-set confirmation flag; the model
        # can never set confirmed (additionalProperties false at transport)
        assert set(ProvisionClassification.model_fields) - {"confirmed"} == set(inner["properties"])
        assert "not_data_measure" in inner["properties"]["measure_nature"]["enum"]


# ---------------------------------------------------------------------------
# the classifier loop: attempt budget, strict retry, unclassified lane
# ---------------------------------------------------------------------------


CONFIRM_YES = '{"claim_supported": true}'
CONFIRM_NO = '{"claim_supported": false}'


class TestClassifyRecord:
    def test_classifies_on_first_attempt(self):
        out = classify_record(
            make_record(),
            CHUNK,
            completion_fn=lambda p, s: valid_json(),
            confirm_completion_fn=lambda p, s: CONFIRM_YES,
            indicator_defs={"6.1": IND_DEF},
        )
        assert out.outcome == "classified" and out.attempts == 1
        assert out.classification.data_scope == "personal"
        assert out.classification.confirmed is True

    def test_garbage_then_valid_uses_strict_retry(self):
        calls: list[bool] = []

        def fn(prompt: str, strict: bool) -> str:
            calls.append(strict)
            return "no json here" if len(calls) == 1 else valid_json()

        out = classify_record(
            make_record(),
            CHUNK,
            completion_fn=fn,
            confirm_completion_fn=lambda p, s: CONFIRM_YES,
            indicator_defs={"6.1": IND_DEF},
        )
        assert out.outcome == "classified" and out.attempts == 2
        assert calls == [False, True]
        assert out.failures and "attempt 1" in out.failures[0]

    def test_three_failures_yield_unclassified_never_raise(self):
        out = classify_record(
            make_record(),
            CHUNK,
            completion_fn=lambda p, s: "{broken",
            confirm_completion_fn=lambda p, s: CONFIRM_YES,
            indicator_defs={"6.1": IND_DEF},
        )
        assert out.outcome == "unclassified" and out.classification is None
        assert out.attempts == 3 and len(out.failures) == 3

    def test_refuses_unverified_records(self):
        with pytest.raises(ValueError, match="unverified"):
            classify_record(
                make_record(status="unverified"),
                CHUNK,
                completion_fn=lambda p, s: valid_json(),
                confirm_completion_fn=lambda p, s: CONFIRM_YES,
                indicator_defs={"6.1": IND_DEF},
            )

    def test_prompt_carries_quote_menu_and_indicator(self):
        r = make_record()
        prompt = build_classify_prompt(r, CHUNK, IND_DEF, strict=False)
        assert QUOTE in prompt
        assert "INDICATOR 6.1" in prompt and IND_DEF.definition in prompt
        assert '"not_data_measure"' in prompt and '"single_economy"' in prompt
        assert QUOTE in build_classify_prompt(r, CHUNK, IND_DEF, strict=True)


class TestAdversarialConfirmation:
    """A score-eligible label must survive the skeptic question; refusal or
    unparseable output is fail-closed (confirmed=False, never drives a score)."""

    def _classify(self, confirm_fn, label_json=None):
        return classify_record(
            make_record(),
            CHUNK,
            completion_fn=lambda p, s: label_json or valid_json(),
            confirm_completion_fn=confirm_fn,
            indicator_defs={"6.1": IND_DEF},
        )

    def test_refused_label_is_kept_but_unconfirmed(self):
        out = self._classify(lambda p, s: CONFIRM_NO)
        assert out.outcome == "classified"
        assert out.classification.confirmed is False
        assert out.classification.measure_nature == "transfer_ban"  # audit trail intact
        assert any("confirmation refused" in f for f in out.failures)

    def test_refused_label_never_scores(self):
        out = self._classify(lambda p, s: CONFIRM_NO)
        r = make_record()
        assert record_contribution(r, out.classification) is None
        cell = derive_scores_v2([r], {r.mapping_id: out.classification})[("SG", "6.1")]
        assert cell.score == 0.0 and cell.controlling_mapping_id is None

    def test_ineligible_label_skips_confirmation(self):
        calls = []

        def confirm_fn(p, s):
            calls.append(p)
            return CONFIRM_YES

        out = self._classify(confirm_fn, label_json=valid_json(measure_nature="not_data_measure", data_scope="not_applicable", application="not_applicable"))
        assert out.outcome == "classified" and calls == []

    def test_unparseable_confirmation_fails_closed_after_strict_retry(self):
        calls: list[bool] = []

        def confirm_fn(p, s):
            calls.append(s)
            return "not json at all"

        out = self._classify(confirm_fn)
        assert calls == [False, True]
        assert out.classification.confirmed is False
        assert any("fail-closed" in f for f in out.failures)

    def test_confirm_prompt_is_skeptical_and_carries_the_quote(self):
        from regcompass.classify import CONFIRM_CLAIMS, build_confirm_prompt

        r = make_record()
        prompt = build_confirm_prompt(r, CHUNK, CONFIRM_CLAIMS["local_storage"], strict=False)
        assert QUOTE in prompt
        assert "kept or stored" in prompt and "within the jurisdiction" in prompt
        assert "answer false" in prompt
        assert "claim_supported" in prompt

    def test_every_eligible_nature_has_a_claim(self):
        from regcompass.classify import CONFIRM_CLAIMS

        for natures in ELIGIBLE_NATURES.values():
            for n in natures:
                assert n in CONFIRM_CLAIMS

    def test_confirm_response_format_is_strict(self):
        from regcompass.classify import _confirm_response_format

        schema = _confirm_response_format()["json_schema"]
        assert schema["strict"] is True
        assert schema["schema"]["additionalProperties"] is False
        assert schema["schema"]["required"] == ["claim_supported"]


# ---------------------------------------------------------------------------
# per-record contribution + apply_classifications (the M6 deferral landing)
# ---------------------------------------------------------------------------


class TestApply:
    def test_fills_measure_type_and_contribution(self):
        r = make_record()
        [updated] = apply_classifications([r], {r.mapping_id: cls()})
        assert updated.measure_type == "transfer_ban"
        assert updated.rdtii_score_contribution == 1.0
        assert r.measure_type is None  # original untouched

    def test_unclassified_record_stays_none(self):
        r = make_record()
        [updated] = apply_classifications([r], {})
        assert updated.measure_type is None and updated.rdtii_score_contribution is None

    def test_ineligible_nature_has_no_contribution_but_keeps_label(self):
        r = make_record(indicator="6.1")
        [updated] = apply_classifications(
            [r], {r.mapping_id: cls(measure_nature="not_data_measure")}
        )
        assert updated.measure_type == "not_data_measure"
        assert updated.rdtii_score_contribution is None

    def test_boundary_excluded_61_record_contributes_nothing(self):
        # the 6.1/6.4 boundary rule lands in apply_classifications too: a 6.1
        # record whose chunk also produced a 6.4 record keeps its label but
        # its contribution is None, exactly as the cell scores it
        r61 = make_record(indicator="6.1")
        r64 = make_record(indicator="6.4")  # same chunk doc_x:c0001
        updated = apply_classifications(
            [r61, r64],
            {
                r61.mapping_id: cls(),
                r64.mapping_id: cls(measure_nature="conditional_transfer"),
            },
        )
        by_id = {u.mapping_id: u for u in updated}
        assert by_id[r61.mapping_id].rdtii_score_contribution is None
        assert by_id[r61.mapping_id].measure_type == "transfer_ban"
        assert by_id[r64.mapping_id].rdtii_score_contribution is not None

    def test_government_only_excluded_on_pillar6(self):
        r = make_record(indicator="6.2")
        c = cls(measure_nature="local_storage", government_data_only=True)
        assert record_contribution(r, c) is None

    def test_government_flag_does_not_exclude_75(self):
        r = make_record(indicator="7.5")
        c = cls(
            measure_nature="government_access",
            government_data_only=True,
            judicial_authorization_required=False,
        )
        assert record_contribution(r, c) == 1.0


# ---------------------------------------------------------------------------
# the rubric, branch by branch
# ---------------------------------------------------------------------------


def one_cell(indicator: str, *pairs, economy: str = "SG"):
    """Run derive_scores_v2 over (record, classification) pairs, return the cell."""
    records = [r for r, _ in pairs]
    classifications = {r.mapping_id: c for r, c in pairs if c is not None}
    return derive_scores_v2(records, classifications)[(economy, indicator)]


class TestRubric61:
    def test_personal_ban_scores_1(self):
        r = make_record("6.1")
        cell = one_cell("6.1", (r, cls()))
        assert cell.score == 1.0 and cell.controlling_mapping_id == r.mapping_id
        assert "personal" in cell.basis

    def test_horizontal_nonpersonal_ban_scores_1(self):
        cell = one_cell("6.1", (make_record("6.1"), cls(data_scope="non_personal")))
        assert cell.score == 1.0

    def test_single_nonpersonal_ban_scores_half(self):
        cell = one_cell(
            "6.1", (make_record("6.1"), cls(data_scope="non_personal", application="sectoral"))
        )
        assert cell.score == 0.5

    def test_two_distinct_nonpersonal_requirements_score_1(self):
        weak = cls(data_scope="non_personal", application="sectoral")
        a = make_record("6.1", chunk_id="doc_a:c1", document_id="doc_a", section="s. 1")
        b = make_record("6.1", chunk_id="doc_b:c1", document_id="doc_b", section="s. 2")
        cell = one_cell("6.1", (a, weak), (b, weak))
        assert cell.score == 1.0 and "2 distinct" in cell.basis

    def test_same_section_twice_is_one_requirement(self):
        weak = cls(data_scope="non_personal", application="sectoral")
        a = make_record("6.1", chunk_id="doc_a:c1", mapping_id="doc_a:c1::6.1")
        b = make_record("6.1", chunk_id="doc_a:c2", mapping_id="doc_a:c2::6.1")
        cell = one_cell("6.1", (a, weak), (b, weak))
        assert cell.score == 0.5

    def test_not_data_measure_never_scores(self):
        cell = one_cell("6.1", (make_record("6.1"), cls(measure_nature="not_data_measure")))
        assert cell.score == 0.0 and cell.controlling_mapping_id is None

    def test_unclassified_never_drives(self):
        cell = one_cell("6.1", (make_record("6.1"), None))
        assert cell.score == 0.0 and "no rubric-eligible" in cell.basis

    def test_boundary_rule_64_claimed_chunk_never_ban_evidence(self):
        shared = "doc_x:c0001"
        r61 = make_record("6.1", chunk_id=shared)
        r64 = make_record("6.4", chunk_id=shared)
        cells = derive_scores_v2(
            [r61, r64],
            {
                r61.mapping_id: cls(),
                r64.mapping_id: cls(measure_nature="conditional_transfer"),
            },
        )
        assert cells[("SG", "6.1")].score == 0.0
        assert cells[("SG", "6.4")].score == 1.0

    def test_government_only_ban_scores_0(self):
        cell = one_cell("6.1", (make_record("6.1"), cls(government_data_only=True)))
        assert cell.score == 0.0


class TestRubric62to64:
    def test_62_personal_storage_scores_1(self):
        cell = one_cell("6.2", (make_record("6.2"), cls(measure_nature="local_storage")))
        assert cell.score == 1.0

    def test_62_specific_dataset_scores_half(self):
        cell = one_cell(
            "6.2",
            (
                make_record("6.2"),
                cls(
                    measure_nature="local_storage",
                    data_scope="specific_dataset",
                    application="sectoral",
                ),
            ),
        )
        assert cell.score == 0.5

    def test_62_specific_dataset_is_never_horizontal_coverage(self):
        # The Guide's 0.5 lane names "a specific data set" explicitly: a rule
        # binding all companies but covering ONE dataset (accounting records)
        # is 0.5, not 1 - entity spread is not data-coverage horizontality.
        cell = one_cell(
            "6.2",
            (
                make_record("6.2"),
                cls(
                    measure_nature="local_storage",
                    data_scope="specific_dataset",
                    application="horizontal",
                ),
            ),
        )
        assert cell.score == 0.5

    def test_63_any_infrastructure_scores_1(self):
        cell = one_cell(
            "6.3",
            (
                make_record("6.3"),
                cls(
                    measure_nature="local_infrastructure",
                    data_scope="non_personal",
                    application="sectoral",
                ),
            ),
        )
        assert cell.score == 1.0

    def test_63_none_scores_0(self):
        cell = one_cell("6.3", (make_record("6.3"), cls(measure_nature="other_data_measure")))
        assert cell.score == 0.0

    def test_64_personal_conditional_scores_1_even_sectoral(self):
        cell = one_cell(
            "6.4",
            (
                make_record("6.4"),
                cls(measure_nature="conditional_transfer", application="sectoral"),
            ),
        )
        assert cell.score == 1.0

    def test_64_sectoral_nonpersonal_scores_half(self):
        cell = one_cell(
            "6.4",
            (
                make_record("6.4"),
                cls(
                    measure_nature="conditional_transfer",
                    data_scope="non_personal",
                    application="sectoral",
                ),
            ),
        )
        assert cell.score == 0.5

    def test_64_has_no_two_requirement_lane(self):
        weak = cls(
            measure_nature="conditional_transfer", data_scope="non_personal", application="sectoral"
        )
        a = make_record("6.4", chunk_id="doc_a:c1", document_id="doc_a", section="s. 1")
        b = make_record("6.4", chunk_id="doc_b:c1", document_id="doc_b", section="s. 2")
        cell = one_cell("6.4", (a, weak), (b, weak))
        assert cell.score == 0.5


class TestRubricPillar7:
    def test_71_comprehensive_horizontal_framework_scores_0(self):
        cell = one_cell(
            "7.1",
            (
                make_record("7.1"),
                cls(measure_nature="protection_framework", comprehensive_framework=True),
            ),
        )
        assert cell.score == 0.0 and "comprehensive" in cell.basis

    def test_71_sectoral_framework_scores_half(self):
        cell = one_cell(
            "7.1",
            (
                make_record("7.1"),
                cls(
                    measure_nature="protection_framework",
                    application="sectoral",
                    comprehensive_framework=False,
                ),
            ),
        )
        assert cell.score == 0.5

    def test_71_no_framework_scores_1(self):
        cell = one_cell("7.1", (make_record("7.1"), cls(measure_nature="not_data_measure")))
        assert cell.score == 1.0

    def test_72_dedicated_horizontal_scores_0(self):
        cell = one_cell(
            "7.2",
            (
                make_record("7.2"),
                cls(measure_nature="protection_framework", dedicated_framework=True),
            ),
        )
        assert cell.score == 0.0

    def test_72_non_dedicated_scores_half(self):
        cell = one_cell(
            "7.2",
            (
                make_record("7.2"),
                cls(measure_nature="protection_framework", dedicated_framework=False),
            ),
        )
        assert cell.score == 0.5

    def test_73_minimum_specified_scores_1(self):
        cell = one_cell(
            "7.3",
            (
                make_record("7.3"),
                cls(measure_nature="retention_requirement", minimum_period_specified=True),
            ),
        )
        assert cell.score == 1.0

    def test_73_unspecified_duration_scores_0(self):
        cell = one_cell(
            "7.3",
            (
                make_record("7.3"),
                cls(measure_nature="retention_requirement", minimum_period_specified=False),
            ),
        )
        assert cell.score == 0.0

    def test_74_horizontal_dpo_scores_1(self):
        cell = one_cell("7.4", (make_record("7.4"), cls(measure_nature="dpo_requirement")))
        assert cell.score == 1.0

    def test_74_sectoral_dpo_scores_half(self):
        cell = one_cell(
            "7.4",
            (make_record("7.4"), cls(measure_nature="dpo_requirement", application="sectoral")),
        )
        assert cell.score == 0.5

    def test_74_dpia_only_scores_quarter(self):
        cell = one_cell("7.4", (make_record("7.4"), cls(measure_nature="dpia_only")))
        assert cell.score == 0.25

    def test_75_access_without_judicial_authorization_scores_1(self):
        cell = one_cell(
            "7.5",
            (
                make_record("7.5"),
                cls(measure_nature="government_access", judicial_authorization_required=False),
            ),
        )
        assert cell.score == 1.0

    def test_75_judicially_authorized_access_scores_0(self):
        cell = one_cell(
            "7.5",
            (
                make_record("7.5"),
                cls(measure_nature="government_access", judicial_authorization_required=True),
            ),
        )
        assert cell.score == 0.0


class TestDirectionAndShape:
    def test_inverse_direction_absence_scores(self):
        # 0 = open for every restriction indicator; missing framework = 1 for 7.1/7.2
        assert ABSENCE_SCORE["7.1"] == 1.0 and ABSENCE_SCORE["7.2"] == 1.0
        assert all(ABSENCE_SCORE[i] == 0.0 for i in ("6.1", "6.2", "6.3", "6.4", "7.3", "7.4", "7.5"))

    def test_every_indicator_gets_a_cell_per_economy(self):
        r = make_record("6.1")
        cells = derive_scores_v2([r], {r.mapping_id: cls()})
        assert {ind for (_, ind) in cells} == set(ELIGIBLE_NATURES)
        assert all(eco == "SG" for (eco, _) in cells)

    def test_controlling_record_is_deterministic(self):
        a = make_record("6.1", chunk_id="doc_a:c1", mapping_id="doc_a:c1::6.1")
        b = make_record("6.1", chunk_id="doc_b:c1", mapping_id="doc_b:c1::6.1")
        strong = cls()
        got = [
            derive_scores_v2(order, {a.mapping_id: strong, b.mapping_id: strong})[("SG", "6.1")]
            for order in ([a, b], [b, a])
        ]
        assert got[0] == got[1] and got[0].controlling_mapping_id == "doc_a:c1::6.1"
