"""The frozen 27-cell ground-truth comparison: tests/golden/repro/GT_MATRIX.md
must exist, cover all 27 cells, and justify every mismatch. Bytes
are pinned by EVIDENCE.sha256; this checks the CONTENT contract so a future
regeneration cannot quietly ship an unjustified mismatch."""

import re
from pathlib import Path

ARTIFACT = Path(__file__).parent / "golden/repro/GT_MATRIX.md"

ROW_RE = re.compile(
    r"^\| (AU|MY|SG) \| (\d\.\d) \| ([0-9.]+) \| ([0-9.]+) \| (agree|MISMATCH) \|"
)


def _rows() -> dict[tuple[str, str], tuple[float, float, str]]:
    rows = {}
    for line in ARTIFACT.read_text(encoding="utf-8").splitlines():
        m = ROW_RE.match(line)
        if m:
            eco, ind, want, got, verdict = m.groups()
            rows[(eco, ind)] = (float(want), float(got), verdict)
    return rows


def test_all_27_cells_recorded():
    rows = _rows()
    assert len(rows) == 27
    assert {e for e, _ in rows} == {"AU", "MY", "SG"}
    assert {i for _, i in rows} == {
        "6.1", "6.2", "6.3", "6.4", "7.1", "7.2", "7.3", "7.4", "7.5"
    }


def test_verdicts_are_consistent_with_the_scores():
    for (eco, ind), (want, got, verdict) in _rows().items():
        assert (verdict == "agree") == (want == got), (eco, ind)


def test_acceptance_cells_agree():
    rows = _rows()
    # SG 6.3 reverted to 0
    assert rows[("SG", "6.3")] == (0.0, 0.0, "agree")
    # the 8 original headline spots all still hold
    for eco, ind, want in [
        ("AU", "6.4", 1.0), ("AU", "7.3", 1.0), ("SG", "6.1", 0.0),
        ("SG", "7.2", 0.0), ("SG", "6.2", 0.5), ("MY", "6.1", 0.0),
        ("MY", "7.4", 1.0), ("MY", "7.5", 1.0),
    ]:
        assert rows[(eco, ind)] == (want, want, "agree"), (eco, ind)


def test_every_mismatch_is_justified_in_the_artifact():
    text = ARTIFACT.read_text(encoding="utf-8")
    mismatches = [(e, i) for (e, i), (_, _, v) in _rows().items() if v == "MISMATCH"]
    for eco, ind in mismatches:
        assert f"### {eco} {ind}:" in text, f"mismatch {eco} {ind} lacks a justification section"


def test_in_sample_caveat_is_stated():
    assert "In-sample caveat" in ARTIFACT.read_text(encoding="utf-8")
