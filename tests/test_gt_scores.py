"""The 23/27 golden baseline, made self-checking.

README.md, tests/golden/repro/GT_MATRIX.md and tests/golden/repro/REPORT.md all
state that our derived scores agree with the ESCAP RDTII 2.1 Round 1 Database on
23 of 27 cells. Until now that number was stated in three prose documents and
computed by no test: scripts/make_gt_matrix.py can only run locally, because it
reads an xlsx that is not in this repo.

These checks recompute the number offline from two committed artifacts: the
hand transcription in regcompass.round1_database and the shipped
tests/golden/repro/supplementary.json. They also pin the v1/v2 gap, because
supplementary.json carries BOTH the rubric scores (derived_scores) and the older
presence scores (presence_scores_v1), and only the rubric scores reach 23/27.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from regcompass.round1_database import ROUND1_DATABASE_SCORES

ROOT = Path(__file__).resolve().parents[1]
SUPPLEMENTARY = ROOT / "tests/golden/repro/supplementary.json"

ECONOMIES = ("AU", "MY", "SG")
INDICATORS = ("6.1", "6.2", "6.3", "6.4", "7.1", "7.2", "7.3", "7.4", "7.5")

# Frozen expectations, recomputed from the committed artifacts (not copied from
# the prose). v2 rubric scores agree on 23 of 27; the four that differ are
# argued cell by cell in tests/golden/repro/GT_MATRIX.md.
V2_AGREEMENT = 23
V2_MISMATCHES = {("AU", "6.1"), ("AU", "6.2"), ("AU", "7.2"), ("MY", "6.2")}
# the presence-only v1 scores agree on 16 of 27: the gap the rubric closes
V1_AGREEMENT = 16


@pytest.fixture(scope="module")
def supplementary() -> dict:
    return json.loads(SUPPLEMENTARY.read_text(encoding="utf-8"))


def _compare(scores: dict[str, dict[str, float]]) -> set[tuple[str, str]]:
    """Returns the set of (economy, indicator) cells that DISAGREE with the
    Database transcription. Raises if a cell is missing, so a truncated score
    payload cannot pass by comparing nothing."""
    mismatches = set()
    for economy in ECONOMIES:
        for indicator in INDICATORS:
            if scores[economy][indicator] != ROUND1_DATABASE_SCORES[economy][indicator]:
                mismatches.add((economy, indicator))
    return mismatches


class TestTranscription:
    def test_the_transcription_is_a_complete_27_cell_matrix(self):
        assert sorted(ROUND1_DATABASE_SCORES) == sorted(ECONOMIES)
        cells = 0
        for economy in ECONOMIES:
            assert sorted(ROUND1_DATABASE_SCORES[economy]) == sorted(INDICATORS)
            for value in ROUND1_DATABASE_SCORES[economy].values():
                assert value in (0.0, 0.5, 1.0)  # the RDTII 2.1 scoring scale
                cells += 1
        assert cells == 27

    def test_the_script_uses_the_same_single_copy(self):
        """scripts/make_gt_matrix.py must import the constant, not keep a second
        transcription that can drift from this one."""
        source = (ROOT / "scripts/make_gt_matrix.py").read_text(encoding="utf-8")
        assert "from regcompass.round1_database import ROUND1_DATABASE_SCORES" in source


class TestGoldenBaseline:
    def test_derived_scores_reproduce_the_23_of_27_agreement(self, supplementary):
        mismatches = _compare(supplementary["derived_scores"])
        assert mismatches == V2_MISMATCHES
        assert 27 - len(mismatches) == V2_AGREEMENT

    def test_singapore_agrees_on_every_cell(self, supplementary):
        """The economy with no mismatches: a cheap, named regression canary."""
        mismatches = _compare(supplementary["derived_scores"])
        assert not any(e == "SG" for e, _ in mismatches)

    def test_the_published_documents_state_the_number_this_test_computes(
        self, supplementary
    ):
        computed = 27 - len(_compare(supplementary["derived_scores"]))
        matrix = (ROOT / "tests/golden/repro/GT_MATRIX.md").read_text(encoding="utf-8")
        report = (ROOT / "tests/golden/repro/REPORT.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        assert f"Agreement: {computed}/27" in matrix
        assert f"{computed}/27 agree" in report
        assert f"{computed}/27 agree" in readme


class TestPresenceScoreGap:
    def test_v1_presence_scores_agree_on_only_16_of_27(self, supplementary):
        mismatches = _compare(supplementary["presence_scores_v1"])
        assert 27 - len(mismatches) == V1_AGREEMENT

    def test_the_rubric_strictly_improves_on_presence(self, supplementary):
        v2 = _compare(supplementary["derived_scores"])
        v1 = _compare(supplementary["presence_scores_v1"])
        assert len(v2) < len(v1)
        # every cell v2 still gets wrong was already wrong under v1: the rubric
        # fixes cells, it does not trade one error for another
        assert v2 <= v1
