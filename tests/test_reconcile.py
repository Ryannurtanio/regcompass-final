"""M8 reconcile: verified records grouped by (economy, indicator); the LLM
proposes relationships, the legal-hierarchy ladder is enforced in CODE
(Acts > Regulations > Notices/Guidelines > Codes/Advisories, hierarchy beats
recency). Lanes: the SG 7.2 regression (Cybersecurity Act 2018
controls over a newer sector-only MAS Notice, the Notice stays recorded), LLM
disagreement -> the rule wins, conflicting -> flagged for the human reviewer
(never auto-resolved away), single-record groups skip the LLM, malformed
proposals retry then fall back to the deterministic ladder (a group always
reconciles: scoring needs one controlling source).
"""

from __future__ import annotations

import gzip
import json
import os
from pathlib import Path

import pytest

from regcompass.classify import ProvisionClassification
from regcompass.contracts import MappingRecord, ReconciledGroup
from regcompass.reconcile import (
    NO_FIT_NOTE,
    build_group_prompt,
    instrument_rank,
    reconcile_group,
    reconcile_records,
)
from regcompass.storage import Storage

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_M7 = ROOT / "tests/golden/m7"

from conftest import needs_paid  # noqa: E402 - money is opt-in; see conftest

needs_openrouter = pytest.mark.skipif(
    not os.environ.get("OPENROUTER_API_KEY"),
    reason="live OpenRouter test needs OPENROUTER_API_KEY",
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def mk_rec(
    n: int,
    document_id: str = "doc_sg_cybersecurity_act_2018",
    indicator: str = "7.2",
    economy: str = "SG",
    last_amended: str | None = None,
    subsection: str | None = "(1)",
) -> MappingRecord:
    return MappingRecord(
        mapping_id=f"{document_id}:c{n:04d}::{indicator}",
        document_id=document_id,
        chunk_id=f"{document_id}:c{n:04d}",
        economy=economy,  # type: ignore[arg-type]
        indicator_id=indicator,  # type: ignore[arg-type]
        indicator_name="Lack of dedicated legal framework for cybersecurity",
        section=f"s. {n}",
        subsection=subsection,
        verbatim_quote=f"the owner of a critical information infrastructure must comply {n}",
        verification_status="passed",
        timeline={"last_amended": last_amended},  # type: ignore[arg-type]
        extraction_attempts=1,
    )


LAW_NAMES = {
    "doc_sg_cybersecurity_act_2018": "Cybersecurity Act 2018 (No. 9 of 2018)",
    "doc_sg_mas_notice_655": "MAS Notice 655 Cyber Hygiene",
    "doc_sg_telecommunications_act_1999": "Telecommunications Act 1999",
    "doc_my_personal_data_protection_act_2010": "Personal Data Protection Act 2010",
    "doc_au_C2026C00098VOL01": "Criminal Code Act 1995",
}


def proposal(authoritative: str, rels: dict[str, str], notes: str = "n") -> str:
    return json.dumps(
        {
            "authoritative_mapping_id": authoritative,
            "relationships": [
                {"mapping_id": k, "relationship": v} for k, v in rels.items()
            ],
            "notes": notes,
        }
    )


def scripted(responses: list[str]):
    calls: list[tuple[str, bool]] = []

    def fn(prompt: str, strict: bool) -> str:
        calls.append((prompt, strict))
        return responses[min(len(calls) - 1, len(responses) - 1)]

    fn.calls = calls  # type: ignore[attr-defined]
    return fn


def load_golden_group(slug: str, economy: str, indicator: str) -> list[MappingRecord]:
    with gzip.open(GOLDEN_M7 / f"{slug}.verified.json.gz", "rt", encoding="utf-8") as f:
        return [
            MappingRecord.model_validate(o["record"])
            for o in json.load(f)["outcomes"]
            if o["outcome"] == "passed" and o["indicator_id"] == indicator
        ]


# ---------------------------------------------------------------------------
# the hierarchy ladder (plain code)
# ---------------------------------------------------------------------------


class TestLadder:
    def test_ranks(self):
        act = instrument_rank("Cybersecurity Act 2018 (No. 9 of 2018)")
        code_act = instrument_rank("Criminal Code Act 1995")
        reg = instrument_rank("Personal Data Protection (Transfer) Regulations 2020")
        notice = instrument_rank("MAS Notice 655 Cyber Hygiene")
        cop = instrument_rank("Personal Data Protection Code of Practice for Banking")
        assert act == code_act < reg < notice < cop

    def test_code_of_practice_is_not_an_act(self):
        # 'Practice' contains the letters 'act'; the ladder must use word
        # boundaries, and check code-of-practice before statute patterns
        assert instrument_rank("Code of Practice for Licensees") > instrument_rank(
            "Telecommunications Act 1999"
        )

    def test_unknown_instrument_ranks_below_everything(self):
        assert instrument_rank("Mystery Document 2024") > instrument_rank(
            "Some Advisory 2020"
        )


# ---------------------------------------------------------------------------
# single-record short-circuit
# ---------------------------------------------------------------------------


class TestSingleRecord:
    def test_no_llm_sole_source_authoritative(self):
        fn = scripted(["MUST NOT BE CALLED"])
        rec = mk_rec(1)
        group, updated = reconcile_group([rec], LAW_NAMES, completion_fn=fn)
        assert fn.calls == []
        assert isinstance(group, ReconciledGroup)
        assert group.authoritative_mapping_id == rec.mapping_id
        assert group.relationships[rec.mapping_id] == "sole_source"
        [u] = updated
        assert u.relationship_to_group == "sole_source"
        assert u.controlling_evidence is True


# ---------------------------------------------------------------------------
# the fit-filtered ladder
# ---------------------------------------------------------------------------


def cls(nature: str, confirmed: bool = True, **kw) -> ProvisionClassification:
    return ProvisionClassification(
        measure_nature=nature,  # type: ignore[arg-type]
        data_scope=kw.pop("data_scope", "non_personal"),
        application=kw.pop("application", "horizontal"),
        government_data_only=kw.pop("government_data_only", False),
        confirmed=confirmed,
        **kw,
    )


class TestFitFilteredLadder:
    """With classifications, the controlling record must EVIDENCE its
    indicator; hierarchy breaks ties within the fit subset only. The shipped
    defect: a warrant-consent sentence (government_access) controlled AU 6.2
    while the on-point localization provision sat demoted in the same group."""

    def _group(self, indicator="6.2"):
        act = mk_rec(1, "doc_sg_cybersecurity_act_2018", indicator=indicator)
        reg = mk_rec(
            2, "doc_sg_pdp_transfer_regulations_2020", indicator=indicator
        )
        names = dict(
            LAW_NAMES,
            doc_sg_pdp_transfer_regulations_2020=(
                "Personal Data Protection (Transfer) Regulations 2020"
            ),
        )
        return act, reg, names

    def test_fit_member_beats_higher_hierarchy_misfit(self):
        act, reg, names = self._group()
        classifications = {
            act.mapping_id: cls("government_access"),  # misfit for 6.2
            reg.mapping_id: cls("local_storage"),  # fits 6.2
        }
        fn = scripted(
            [proposal(act.mapping_id, {act.mapping_id: "complementary", reg.mapping_id: "complementary"})]
        )
        group, updated = reconcile_group(
            [act, reg], names, completion_fn=fn, classifications=classifications
        )
        # the Act outranks the Regulation, but it does not evidence 6.2: the
        # fit-filtered ladder must control from the Regulation
        assert group.authoritative_mapping_id == reg.mapping_id
        by_id = {u.mapping_id: u for u in updated}
        assert by_id[reg.mapping_id].controlling_evidence is True
        assert by_id[act.mapping_id].controlling_evidence is False
        assert NO_FIT_NOTE not in (group.reconciliation_notes or "")

    def test_confirmation_refused_member_is_not_fit(self):
        act, reg, names = self._group()
        classifications = {
            act.mapping_id: cls("local_storage", confirmed=False),  # refused
            reg.mapping_id: cls("local_storage"),
        }
        fn = scripted(
            [proposal(act.mapping_id, {act.mapping_id: "complementary", reg.mapping_id: "complementary"})]
        )
        group, _ = reconcile_group(
            [act, reg], names, completion_fn=fn, classifications=classifications
        )
        assert group.authoritative_mapping_id == reg.mapping_id

    def test_hierarchy_still_breaks_ties_within_the_fit_subset(self):
        act, reg, names = self._group()
        classifications = {
            act.mapping_id: cls("local_storage"),
            reg.mapping_id: cls("local_storage"),
        }
        fn = scripted(
            [proposal(reg.mapping_id, {act.mapping_id: "complementary", reg.mapping_id: "complementary"})]
        )
        group, _ = reconcile_group(
            [act, reg], names, completion_fn=fn, classifications=classifications
        )
        # both fit: the Act wins on hierarchy exactly as before
        assert group.authoritative_mapping_id == act.mapping_id

    def test_no_fit_falls_back_to_full_ladder_with_the_flag(self):
        act, reg, names = self._group()
        classifications = {
            act.mapping_id: cls("government_access"),
            reg.mapping_id: cls("government_access"),
        }
        fn = scripted(
            [proposal(reg.mapping_id, {act.mapping_id: "complementary", reg.mapping_id: "complementary"})]
        )
        group, updated = reconcile_group(
            [act, reg], names, completion_fn=fn, classifications=classifications
        )
        assert group.authoritative_mapping_id == act.mapping_id  # full ladder
        assert NO_FIT_NOTE in (group.reconciliation_notes or "")
        assert sum(1 for u in updated if u.controlling_evidence) == 1

    def test_unclassified_member_is_not_fit_but_remains_fallback(self):
        act, reg, names = self._group()
        classifications = {reg.mapping_id: cls("local_storage")}  # act missing
        fn = scripted(
            [proposal(act.mapping_id, {act.mapping_id: "complementary", reg.mapping_id: "complementary"})]
        )
        group, _ = reconcile_group(
            [act, reg], names, completion_fn=fn, classifications=classifications
        )
        assert group.authoritative_mapping_id == reg.mapping_id

    def test_single_record_no_fit_group_is_flagged(self):
        rec = mk_rec(1, indicator="6.2")
        group, updated = reconcile_group(
            [rec],
            LAW_NAMES,
            completion_fn=scripted(["MUST NOT BE CALLED"]),
            classifications={rec.mapping_id: cls("government_access")},
        )
        assert group.authoritative_mapping_id == rec.mapping_id
        assert NO_FIT_NOTE in (group.reconciliation_notes or "")
        assert updated[0].controlling_evidence is True

    def test_prompt_names_the_fit_subset(self):
        act, reg, names = self._group()
        classifications = {
            act.mapping_id: cls("government_access"),
            reg.mapping_id: cls("local_storage"),
        }
        fn = scripted(
            [proposal(reg.mapping_id, {act.mapping_id: "complementary", reg.mapping_id: "complementary"})]
        )
        reconcile_group([act, reg], names, completion_fn=fn, classifications=classifications)
        prompt = fn.calls[0][0]
        assert "MUST be one of these members" in prompt
        assert reg.mapping_id in prompt.split("MUST be one of these members")[1].split("\n")[0]

    def test_score_aligned_member_beats_higher_hierarchy_same_nature(self):
        # 7.2 is the shared-nature trap: a sectoral banking-secrecy framework
        # (contribution 0.5) and a dedicated horizontal cybersecurity
        # framework (contribution 0.0) are BOTH protection_framework. The
        # cell scores 0.0 from the dedicated one, so the dedicated one must
        # control even from a lower-hierarchy instrument.
        act = mk_rec(1, "doc_sg_banking_act_1970", indicator="7.2")
        notice = mk_rec(2, "doc_sg_mas_notice_655", indicator="7.2")
        names = dict(LAW_NAMES, doc_sg_banking_act_1970="Banking Act 1970")
        classifications = {
            act.mapping_id: cls("protection_framework", application="sectoral"),
            notice.mapping_id: cls(
                "protection_framework", dedicated_framework=True, application="horizontal"
            ),
        }
        fn = scripted(
            [proposal(act.mapping_id, {act.mapping_id: "complementary", notice.mapping_id: "complementary"})]
        )
        group, _ = reconcile_group(
            [act, notice], names, completion_fn=fn, classifications=classifications
        )
        assert group.authoritative_mapping_id == notice.mapping_id

    def test_restriction_indicator_aligns_on_the_strong_member(self):
        # 6.2: a personal-data storage rule (1.0) establishes the cell over a
        # specific-document rule (0.5); the 0.5 member cannot control even
        # from a stronger instrument.
        act = mk_rec(1, "doc_sg_cybersecurity_act_2018", indicator="6.2")
        reg = mk_rec(2, "doc_sg_pdp_transfer_regulations_2020", indicator="6.2")
        names = dict(
            LAW_NAMES,
            doc_sg_pdp_transfer_regulations_2020=(
                "Personal Data Protection (Transfer) Regulations 2020"
            ),
        )
        classifications = {
            act.mapping_id: cls("local_storage", data_scope="specific_dataset", application="sectoral"),
            reg.mapping_id: cls("local_storage", data_scope="personal"),
        }
        fn = scripted(
            [proposal(act.mapping_id, {act.mapping_id: "complementary", reg.mapping_id: "complementary"})]
        )
        group, _ = reconcile_group(
            [act, reg], names, completion_fn=fn, classifications=classifications
        )
        assert group.authoritative_mapping_id == reg.mapping_id

    def test_boundary_excluded_61_member_is_not_fit(self):
        # the 6.1/6.4 boundary rule: a 6.1 record whose CHUNK also produced a
        # 6.4 record is conditional-transfer evidence and can neither score
        # nor control the 6.1 cell. reconcile_records computes the exclusions
        # across groups and threads them into each group's fit filter.
        r61 = mk_rec(5, indicator="6.1")
        r61b = mk_rec(6, "doc_sg_telecommunications_act_1999", indicator="6.1")
        r64 = mk_rec(5, indicator="6.4")  # same chunk c0005 as r61
        classifications = {
            r61.mapping_id: cls("transfer_ban", data_scope="personal"),
            r61b.mapping_id: cls("government_access"),  # misfit anyway
            r64.mapping_id: cls("conditional_transfer", data_scope="personal"),
        }
        fn = scripted(
            [
                proposal(
                    r61.mapping_id,
                    {r61.mapping_id: "complementary", r61b.mapping_id: "complementary"},
                )
            ]
        )
        groups, _ = reconcile_records(
            [r61, r61b, r64], LAW_NAMES, completion_fn=fn, classifications=classifications
        )
        g61 = next(g for g in groups if g.indicator_id == "6.1")
        # r61 is boundary-excluded, r61b is a misfit: the 6.1 group has no
        # fitting member and must say so
        assert NO_FIT_NOTE in (g61.reconciliation_notes or "")
        g64 = next(g for g in groups if g.indicator_id == "6.4")
        assert g64.authoritative_mapping_id == r64.mapping_id
        assert NO_FIT_NOTE not in (g64.reconciliation_notes or "")

    def test_without_classifications_behavior_is_unchanged(self):
        act, reg, names = self._group()
        fn = scripted(
            [proposal(reg.mapping_id, {act.mapping_id: "complementary", reg.mapping_id: "complementary"})]
        )
        group, _ = reconcile_group([act, reg], names, completion_fn=fn)
        assert group.authoritative_mapping_id == act.mapping_id  # pure ladder
        assert "MUST be one of these members" not in fn.calls[0][0]


# ---------------------------------------------------------------------------
# the SG 7.2 regression + ladder-beats-LLM
# ---------------------------------------------------------------------------


def sg72_group() -> tuple[MappingRecord, MappingRecord]:
    act = mk_rec(17, "doc_sg_cybersecurity_act_2018", last_amended="2018")
    notice = mk_rec(4, "doc_sg_mas_notice_655", last_amended="2024")
    return act, notice


class TestSG72Regression:
    def test_act_controls_over_newer_notice_even_when_llm_disagrees(self):
        act, notice = sg72_group()
        # the LLM proposes the NEWER sector Notice as controlling: rule must win
        fn = scripted(
            [
                proposal(
                    notice.mapping_id,
                    {act.mapping_id: "complementary", notice.mapping_id: "complementary"},
                )
            ]
        )
        group, updated = reconcile_group([act, notice], LAW_NAMES, completion_fn=fn)
        assert group.authoritative_mapping_id == act.mapping_id
        assert "hierarchy" in (group.reconciliation_notes or "").lower()
        by_id = {u.mapping_id: u for u in updated}
        assert by_id[act.mapping_id].controlling_evidence is True
        # the loser stays RECORDED, never dropped
        assert by_id[notice.mapping_id].controlling_evidence is False
        assert by_id[notice.mapping_id].relationship_to_group is not None
        assert by_id[notice.mapping_id].verification_status == "passed"

    def test_llm_agreeing_with_the_ladder_is_not_overridden(self):
        act, notice = sg72_group()
        fn = scripted(
            [
                proposal(
                    act.mapping_id,
                    {act.mapping_id: "supersedes", notice.mapping_id: "superseded_by"},
                    notes="act is the horizontal framework",
                )
            ]
        )
        group, updated = reconcile_group([act, notice], LAW_NAMES, completion_fn=fn)
        assert group.authoritative_mapping_id == act.mapping_id
        assert "override" not in (group.reconciliation_notes or "").lower()
        by_id = {u.mapping_id: u for u in updated}
        assert by_id[notice.mapping_id].relationship_to_group == "superseded_by"

    def test_hierarchy_beats_recency_in_pure_ladder_fallback(self):
        act, notice = sg72_group()
        fn = scripted(["garbage"] * 3)
        group, _ = reconcile_group([act, notice], LAW_NAMES, completion_fn=fn)
        assert group.authoritative_mapping_id == act.mapping_id
        assert "fallback" in (group.reconciliation_notes or "").lower()

    def test_recency_breaks_ties_within_the_same_rank(self):
        old = mk_rec(1, "doc_sg_cybersecurity_act_2018", last_amended="2018")
        new = mk_rec(2, "doc_sg_other_act", last_amended="2023")
        law_names = {**LAW_NAMES, "doc_sg_other_act": "Other Cybersecurity Act 2023"}
        fn = scripted(["garbage"] * 3)
        group, _ = reconcile_group([old, new], law_names, completion_fn=fn)
        assert group.authoritative_mapping_id == new.mapping_id


# ---------------------------------------------------------------------------
# proposal validation and fallback lanes
# ---------------------------------------------------------------------------


class TestProposalLanes:
    def test_malformed_then_valid_counts_calls_and_escalates(self):
        act, notice = sg72_group()
        good = proposal(
            act.mapping_id,
            {act.mapping_id: "complementary", notice.mapping_id: "complementary"},
        )
        fn = scripted(["not json", good])
        group, _ = reconcile_group([act, notice], LAW_NAMES, completion_fn=fn)
        assert group.authoritative_mapping_id == act.mapping_id
        assert [s for _, s in fn.calls] == [False, True]

    @pytest.mark.parametrize(
        "bad_factory",
        [
            # missing a member
            lambda a, n: proposal(a, {a: "complementary"}),
            # unknown member
            lambda a, n: proposal(a, {a: "complementary", n: "complementary", "ghost": "complementary"}),
            # authoritative not in the group
            lambda a, n: proposal("ghost", {a: "complementary", n: "complementary"}),
            # sole_source inside a multi-record group
            lambda a, n: proposal(a, {a: "sole_source", n: "complementary"}),
            # the controlling record cannot be the superseded one
            lambda a, n: proposal(a, {a: "superseded_by", n: "supersedes"}),
        ],
    )
    def test_incoherent_proposals_are_malformed(self, bad_factory):
        act, notice = sg72_group()
        bad = bad_factory(act.mapping_id, notice.mapping_id)
        good = proposal(
            act.mapping_id,
            {act.mapping_id: "complementary", notice.mapping_id: "complementary"},
        )
        fn = scripted([bad, good])
        group, _ = reconcile_group([act, notice], LAW_NAMES, completion_fn=fn)
        assert len(fn.calls) == 2  # the bad one was rejected and retried
        assert group.authoritative_mapping_id == act.mapping_id

    def test_all_malformed_falls_back_to_deterministic_ladder(self):
        act, notice = sg72_group()
        fn = scripted(["{", "[]", "nope"])
        group, updated = reconcile_group([act, notice], LAW_NAMES, completion_fn=fn)
        assert len(fn.calls) == 3
        assert group.authoritative_mapping_id == act.mapping_id
        assert sum(1 for u in updated if u.controlling_evidence) == 1

    def test_conflicting_members_are_flagged_for_human_review(self):
        act, notice = sg72_group()
        fn = scripted(
            [
                proposal(
                    act.mapping_id,
                    {act.mapping_id: "complementary", notice.mapping_id: "conflicting"},
                )
            ]
        )
        group, updated = reconcile_group([act, notice], LAW_NAMES, completion_fn=fn)
        by_id = {u.mapping_id: u for u in updated}
        assert "contradictory-sources" in by_id[notice.mapping_id].uncertainty_flags
        assert "review" in (group.reconciliation_notes or "").lower()
        # never auto-resolved away: still recorded, still exactly one controlling
        assert sum(1 for u in updated if u.controlling_evidence) == 1

    def test_unverified_records_are_refused(self):
        bad = mk_rec(1).model_copy(update={"verification_status": "unverified"})
        with pytest.raises(ValueError, match="passed"):
            reconcile_group([bad], LAW_NAMES, completion_fn=scripted([]))

    def test_mixed_group_keys_are_refused(self):
        a = mk_rec(1, indicator="7.2")
        b = mk_rec(2, indicator="7.5")
        with pytest.raises(ValueError, match="group"):
            reconcile_group([a, b], LAW_NAMES, completion_fn=scripted([]))


# ---------------------------------------------------------------------------
# prompt contract
# ---------------------------------------------------------------------------


class TestPrompt:
    def test_prompt_lists_members_and_vocabulary(self):
        act, notice = sg72_group()
        p = build_group_prompt("SG", "7.2", [(act, LAW_NAMES[act.document_id]), (notice, LAW_NAMES[notice.document_id])], strict=False)
        assert act.mapping_id in p and notice.mapping_id in p
        assert "Cybersecurity Act 2018" in p and "MAS Notice 655" in p
        for word in ("complementary", "superseded_by", "supersedes", "conflicting"):
            assert word in p
        assert "sole_source" not in p  # not a valid choice inside a multi group
        assert "authoritative_mapping_id" in p


# ---------------------------------------------------------------------------
# whole-dataset grouping + audit
# ---------------------------------------------------------------------------


class TestReconcileRecords:
    def test_groups_singles_and_multis_across_indicators(self):
        act, notice = sg72_group()
        single = mk_rec(9, indicator="7.5")
        good = proposal(
            act.mapping_id,
            {act.mapping_id: "complementary", notice.mapping_id: "complementary"},
        )
        groups, updated = reconcile_records(
            [act, notice, single], LAW_NAMES, completion_fn=scripted([good])
        )
        assert {g.group_id for g in groups} == {"SG:7.2", "SG:7.5"}
        assert len(updated) == 3
        assert sum(1 for u in updated if u.controlling_evidence) == 2  # one per group

    def test_real_golden_group_reconciles_offline(self):
        records = load_golden_group("my_personal_data_protection_act_2010", "MY", "7.5")
        assert len(records) >= 5
        rels = {r.mapping_id: "complementary" for r in records}
        fn = scripted([proposal(records[0].mapping_id, rels)])
        group, updated = reconcile_group(records, LAW_NAMES, completion_fn=fn)
        assert group.authoritative_mapping_id == records[0].mapping_id
        assert sum(1 for u in updated if u.controlling_evidence) == 1
        assert all(u.relationship_to_group is not None for u in updated)

    def test_audit_log_entries(self, tmp_path):
        s = Storage(tmp_path / "a.db")
        s.apply_schema()
        act, notice = sg72_group()
        fn = scripted(["garbage"] * 3)
        reconcile_group([act, notice], LAW_NAMES, completion_fn=fn, storage=s)
        rows = [
            r["decision"]
            for r in s.conn.execute("SELECT decision FROM audit_log WHERE stage = 'm8_reconcile'")
        ]
        assert rows and any("fallback" in d for d in rows)


class TestGoldenM8:
    """The reconciled golden dataset (scripts/make_golden_m8.py) must hold the
    exactly-one-controlling invariant in every group, with every record
    classified and every group contract-valid."""

    def test_golden_m8_invariants(self):
        golden = ROOT / "tests/golden/m8"
        if not golden.exists():
            pytest.skip("golden m8 not generated yet")
        for slug in (
            "sg_telecommunications_act_1999",
            "my_personal_data_protection_act_2010",
            "au_C2026C00098VOL01",
        ):
            with gzip.open(golden / f"{slug}.reconciled.json.gz", "rt", encoding="utf-8") as f:
                payload = json.load(f)
            groups = [ReconciledGroup.model_validate(g) for g in payload["groups"]]
            records = [MappingRecord.model_validate(r) for r in payload["records"]]
            by_group: dict[str, list[MappingRecord]] = {}
            for r in records:
                by_group.setdefault(f"{r.economy}:{r.indicator_id}", []).append(r)
            assert len(groups) == len(by_group)
            for g in groups:
                members = by_group[g.group_id]
                assert sorted(m.mapping_id for m in members) == sorted(g.mapping_ids)
                assert sum(1 for m in members if m.controlling_evidence) == 1
                assert all(m.relationship_to_group is not None for m in members)
                winner = next(m for m in members if m.controlling_evidence)
                assert winner.mapping_id == g.authoritative_mapping_id
                if len(members) == 1:
                    assert g.relationships[winner.mapping_id] == "sole_source"


# ---------------------------------------------------------------------------
# live (skips without key)
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.paid
@needs_paid
@needs_openrouter
class TestLive30B:
    def test_live_group_reconciles(self):
        records = load_golden_group("sg_telecommunications_act_1999", "SG", "7.3")
        assert len(records) == 3
        group, updated = reconcile_group(records, LAW_NAMES)
        assert group.authoritative_mapping_id in {r.mapping_id for r in records}
        assert sum(1 for u in updated if u.controlling_evidence) == 1
        assert all(u.relationship_to_group is not None for u in updated)


# ---------------------------------------------------------------------------
# a hung Engine call
# ---------------------------------------------------------------------------


class TestAnExpiredCallDeadlineLeavesTheStage:
    """reconcile_group catches nothing around its completion callable: an
    exception from the Engine travels straight out of the stage, and the
    transport ladder at the M8 call site in pipeline.py is what retries it.

    A call that blew its wall-clock deadline must behave exactly like the
    dropped connection it stands in for. Asserted as a PARITY property, so the
    stage keeps whatever policy it has rather than growing a second one.
    """

    def _two_records(self):
        return [mk_rec(5), mk_rec(9, document_id="doc_sg_mas_notice_655")]

    def test_a_dropped_connection_travels_out_of_the_stage(self):
        def dead(prompt: str, strict: bool) -> str:
            raise ConnectionError("connection reset by peer")

        with pytest.raises(ConnectionError):
            reconcile_group(self._two_records(), LAW_NAMES, completion_fn=dead)

    def test_an_expired_deadline_travels_out_the_same_way(self):
        from regcompass.engines import EngineCallDeadline, call_with_deadline

        def hung(prompt: str, strict: bool) -> str:
            def never_answers() -> str:
                import threading

                threading.Event().wait(5)
                return "too late"

            return call_with_deadline(never_answers, 0.2, "engine-b")

        with pytest.raises(EngineCallDeadline, match="deadline"):
            reconcile_group(self._two_records(), LAW_NAMES, completion_fn=hung)
