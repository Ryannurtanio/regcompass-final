"""Resume-safety gates for scripts/run_repro.py's JSONL checkpoints: a run
killed mid-write leaves a partial final line with no
newline; the append path must SEAL that line off so the recomputed pair lands
on its own line, instead of welding onto the partial bytes and losing both
forever. Also the per-stage audit-trail shape check for the local repro
checkpoints: the JSONL trail IS the audit trail for the
repro driver's paid stages, per the project audit contract, so its shape is enforced
here whenever data/repro exists (skips on CI, which has no corpus).
"""

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("httpx", reason="run_repro imports httpx (live extra)")
pytest.importorskip("litellm", reason="run_repro imports the mapper (live extra)")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from run_repro import REPRO, load_jsonl, open_checkpoint_append  # noqa: E402

COMPLETE = {"chunk_id": "c0001", "indicator_id": "6.1", "outcome": "no_evidence"}
RECOMPUTED = {"chunk_id": "c0002", "indicator_id": "6.2", "outcome": "mapped"}


def write_killed_mid_write(path: Path) -> None:
    """One complete line, then a partial record with NO trailing newline -
    exactly what a SIGKILL between write() and the final newline leaves."""
    partial = json.dumps(RECOMPUTED)[: len(json.dumps(RECOMPUTED)) // 2]
    path.write_text(json.dumps(COMPLETE) + "\n" + partial, encoding="utf-8")


def test_partial_line_is_dropped_on_load(tmp_path):
    ckpt = tmp_path / "doc.jsonl"
    write_killed_mid_write(ckpt)
    rows = load_jsonl(ckpt)
    assert rows == [COMPLETE]


def test_resume_append_after_kill_preserves_the_recomputed_pair(tmp_path):
    """The failure chain, mechanically: kill mid-write, resume
    recomputes the dropped pair and appends it. Without the seal, the new
    record welds onto the partial bytes and BOTH vanish on the next load."""
    ckpt = tmp_path / "doc.jsonl"
    write_killed_mid_write(ckpt)
    assert load_jsonl(ckpt) == [COMPLETE]  # resume sees the pair as missing

    with open_checkpoint_append(ckpt) as f:
        f.write(json.dumps(RECOMPUTED) + "\n")

    rows = load_jsonl(ckpt)
    keys = {(r["chunk_id"], r["indicator_id"]) for r in rows}
    assert (RECOMPUTED["chunk_id"], RECOMPUTED["indicator_id"]) in keys
    assert (COMPLETE["chunk_id"], COMPLETE["indicator_id"]) in keys
    # and the file has converged: every line now parses
    lines = ckpt.read_text(encoding="utf-8").splitlines()
    parsed = sum(1 for ln in lines if _parses(ln))
    assert parsed == len(rows)


def test_healthy_file_gains_no_blank_lines(tmp_path):
    ckpt = tmp_path / "doc.jsonl"
    ckpt.write_text(json.dumps(COMPLETE) + "\n", encoding="utf-8")
    with open_checkpoint_append(ckpt) as f:
        f.write(json.dumps(RECOMPUTED) + "\n")
    lines = ckpt.read_text(encoding="utf-8").splitlines()
    assert lines == [json.dumps(COMPLETE), json.dumps(RECOMPUTED)]


def test_fresh_and_empty_files_are_fine(tmp_path):
    fresh = tmp_path / "fresh.jsonl"
    with open_checkpoint_append(fresh) as f:
        f.write(json.dumps(COMPLETE) + "\n")
    assert load_jsonl(fresh) == [COMPLETE]

    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    with open_checkpoint_append(empty) as f:
        f.write(json.dumps(COMPLETE) + "\n")
    assert load_jsonl(empty) == [COMPLETE]


def _parses(line: str) -> bool:
    try:
        json.loads(line)
        return True
    except json.JSONDecodeError:
        return False


# -- local audit-trail shape check (project audit contract, repro stages) --

PAIR_KEYS = {"chunk_id", "indicator_id", "outcome", "attempts"}
M12_KEYS = {"mapping_id", "indicator_id", "outcome", "attempts"}


@pytest.mark.skipif(not REPRO.exists(), reason="no local data/repro corpus (CI)")
def test_local_repro_checkpoint_trail_is_well_formed():
    """Every checkpoint line in the local repro run parses and carries the
    audit fields the project audit contract names as the trail for the paid repro stages.
    This automates a full scan of the checkpoint trail."""
    checked = 0
    for stage, required in (("m6", PAIR_KEYS), ("m7", PAIR_KEYS), ("m12", M12_KEYS)):
        for ckpt in sorted((REPRO / stage).glob("*.jsonl")):
            for i, line in enumerate(ckpt.read_text(encoding="utf-8").splitlines(), 1):
                row = json.loads(line)  # a malformed SHIPPED line is a failure
                missing = required - set(row)
                assert not missing, f"{stage}/{ckpt.name}:{i} missing {missing}"
                checked += 1
    assert checked > 0, "data/repro exists but holds no checkpoint lines"
