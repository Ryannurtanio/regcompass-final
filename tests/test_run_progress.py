"""The Run's progress: the free-text lines and the Step hook beside them.

Nothing here calls a paid model or touches the network: every Run goes through
the fake Engine over a Corpus seeded from the committed fixture Documents.

The free-text lines are pinned by snapshot, as the terminal and /api/events
print them. The only things masked are the ones that differ between two
identical Runs: a Step's wall time and the Run id. Regenerate with
REGCOMPASS_UPDATE_SNAPSHOTS=1 only when a line is meant to change.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from regcompass.engines import fake_completion, fake_embed, resolve_engine
from regcompass.pipeline import run_economy
from regcompass.run_progress import DOCUMENT_STEPS, RECONCILE, RunProgress
from regcompass.server import create_app
from regcompass.storage import Storage

from corpus_fixtures import BUNDLED, seed_corpus  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOTS = ROOT / "tests/snapshots"


def _masked(lines: list[str]) -> str:
    out = []
    for line in lines:
        line = re.sub(r"\b\d+\.\d+s\)", "Ns)", line)
        line = re.sub(r"run_\d{8}T\d{6}Z_[0-9a-f]+", "run_ID", line)
        out.append(line)
    return "\n".join(out) + "\n"


def _matches_snapshot(name: str, text: str) -> None:
    snapshot = SNAPSHOTS / name
    if os.environ.get("REGCOMPASS_UPDATE_SNAPSHOTS"):
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text(text, encoding="utf-8")
    assert snapshot.exists(), f"missing snapshot {snapshot}"
    assert text == snapshot.read_text(encoding="utf-8"), (
        f"the Run's free-text lines changed ({name});"
        " regenerate with REGCOMPASS_UPDATE_SNAPSHOTS=1 only on purpose"
    )


def _sg_storage(tmp_path: Path) -> tuple[Storage, Path]:
    storage = Storage(tmp_path / "progress.db")
    storage.apply_schema()
    data_dir = tmp_path / "data"
    seed_corpus(storage, data_dir, "SG")
    return storage, data_dir


def _run(storage, data_dir, **kwargs):
    return run_economy(
        storage, "SG", (6, 7), resolve_engine("fake"), data_dir=data_dir,
        completion_fn=fake_completion, embed_fn=fake_embed, **kwargs,
    )


class TestTheFreeTextLinesDoNotMove:
    def test_the_terminal_lines_of_a_fresh_and_a_reused_run(self, tmp_path, monkeypatch):
        """First Run reads the Document afresh, the second reuses its stored
        extraction: both branches of the Read Step print here."""
        monkeypatch.chdir(ROOT)
        storage, data_dir = _sg_storage(tmp_path)
        storage.conn.execute("DELETE FROM extractions")
        storage.conn.commit()
        lines: list[str] = []
        _run(storage, data_dir, progress=lines.append)
        lines.append("-- second Run --")
        _run(storage, data_dir, progress=lines.append)
        _matches_snapshot("run_progress_sg.txt", _masked(lines))

    def test_the_api_events_lines_of_a_fake_engine_run(self, tmp_path, monkeypatch):
        db = tmp_path / "web.db"
        storage = Storage(db)
        storage.apply_schema()
        data = tmp_path / "data"
        seed_corpus(storage, data, "SG")
        storage.conn.close()
        out = tmp_path / "out"
        out.mkdir()
        c = TestClient(create_app(db_path=db, out_dir=out, data_dir=data, ui_dir=None))

        started = c.post("/api/run", json={"economy": "SG", "pillars": [7], "engine": "fake"})
        assert started.status_code == 200, started.text
        deadline = time.time() + 180
        while time.time() < deadline:
            st = c.get("/api/status").json()
            if not st["active"] and st["status"] in ("done", "error"):
                break
            time.sleep(0.02)
        assert st["status"] == "done", st

        msgs: list[str] = []
        with c.stream("GET", "/api/events") as resp:
            for line in resp.iter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    obj = json.loads(line[len("data: "):])
                except json.JSONDecodeError:
                    continue
                if "msg" in obj:
                    msgs.append(obj["msg"])
        _matches_snapshot("api_events_sg_p7.txt", _masked(msgs))


class Recorder(RunProgress):
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def step_started(self, document_id, step):
        self.calls.append(("started", document_id, step))

    def step_finished(self, document_id, step, counts):
        self.calls.append(("finished", document_id, step, dict(counts)))

    def map_progress(self, document_id, done, total):
        self.calls.append(("map", document_id, done, total))

    def failed(self, document_id, step, message):
        self.calls.append(("failed", document_id, step, message))

    def steps(self) -> list[tuple]:
        return [c[:3] for c in self.calls if c[0] in ("started", "finished")]

    def finished(self, step: str) -> list[dict]:
        return [c[3] for c in self.calls if c[0] == "finished" and c[2] == step]


def _two_document_storage(tmp_path: Path) -> tuple[Storage, Path, list[str]]:
    """SG's Document plus MY's bytes filed under SG: two Documents, one Run."""
    from regcompass.corpus import add_document

    storage, data_dir = _sg_storage(tmp_path)
    my = BUNDLED["MY"][0]
    add_document(
        storage, data_dir, "SG", my.path.read_bytes(), source_url=my.source_url,
        language="en", filename_hint=my.filename_hint,
    )
    ids = [r["document_id"] for r in storage.corpus_documents("SG")]
    assert len(ids) == 2
    return storage, data_dir, ids


class TestTheStepHook:
    @pytest.fixture(scope="class")
    def recorded(self, tmp_path_factory):
        with pytest.MonkeyPatch.context() as mp:
            mp.chdir(ROOT)
            storage, data_dir, ids = _two_document_storage(
                tmp_path_factory.mktemp("recorded")
            )
            hook = Recorder()
            report = _run(storage, data_dir, hook=hook)
        return hook, report, ids

    def test_every_document_walks_the_steps_in_order_then_reconcile_once(self, recorded):
        hook, report, ids = recorded
        expected = []
        for doc in report.documents:
            for step in DOCUMENT_STEPS:
                expected += [("started", doc, step), ("finished", doc, step)]
        expected += [("started", None, RECONCILE), ("finished", None, RECONCILE)]
        assert hook.steps() == expected
        assert sorted(report.documents) == sorted(ids)
        assert not [c for c in hook.calls if c[0] == "failed"]

    def test_the_counts_add_up_to_the_run_report(self, recorded):
        hook, report, _ = recorded
        assert all(c["pages"] > 0 and c["chars"] > 0 for c in hook.finished("read"))
        assert sum(c["pieces"] for c in hook.finished("cut")) == report.n_chunks
        gate = hook.finished("gate")
        assert sum(c["pairs"] for c in gate) == report.n_pairs_considered
        assert sum(c["candidates"] for c in gate) == report.n_pairs_gated
        assert [c["pieces"] for c in gate] == [c["pieces"] for c in hook.finished("cut")]
        mapped = hook.finished("map")
        assert [c["total"] for c in mapped] == [c["candidates"] for c in gate]
        prove = hook.finished("prove")
        assert sum(c["proven"] for c in prove) == report.n_passed
        assert sum(c["no_evidence"] for c in prove) == report.n_no_evidence
        assert sum(c["dropped"] for c in prove) == report.n_dropped
        assert sum(c["glossed"] for c in hook.finished("gloss")) == report.n_glossed
        (reconcile,) = hook.finished(RECONCILE)
        assert reconcile["groups"] == report.n_groups
        assert reconcile["passed"] == report.n_passed

    def test_map_progress_is_monotonic_and_ends_at_the_total(self, recorded):
        hook, report, _ = recorded
        for doc in report.documents:
            ticks = [(c[2], c[3]) for c in hook.calls if c[0] == "map" and c[1] == doc]
            assert ticks, doc
            done = [d for d, _ in ticks]
            assert done == sorted(done) and len(set(done)) == len(done)
            assert {t for _, t in ticks} == {ticks[-1][1]}
            assert done[-1] == ticks[-1][1]

    def test_map_ticks_fall_between_map_started_and_map_finished(self, recorded):
        hook, report, _ = recorded
        for doc in report.documents:
            mine = [c for c in hook.calls if c[1] == doc]
            kinds = [(c[0], c[2] if c[0] != "map" else "map") for c in mine]
            start = kinds.index(("started", "map"))
            end = kinds.index(("finished", "map"))
            ticks = [i for i, k in enumerate(kinds) if k == ("map", "map")]
            assert ticks and start < min(ticks) and max(ticks) < end

    def test_the_text_lines_are_the_same_with_or_without_a_hook(self, tmp_path, monkeypatch):
        monkeypatch.chdir(ROOT)
        runs = []
        for name, hook in (("bare", None), ("hooked", Recorder())):
            (tmp_path / name).mkdir()
            storage, data_dir, _ = _two_document_storage(tmp_path / name)
            lines: list[str] = []
            kwargs = {} if hook is None else {"hook": hook}
            report = _run(storage, data_dir, progress=lines.append, **kwargs)
            runs.append((
                _masked(lines),
                (report.n_chunks, report.n_pairs_gated, report.n_passed,
                 report.n_no_evidence, report.n_dropped, report.n_groups),
            ))
        assert runs[0] == runs[1]


class TestAFailureIsReportedWhereItHappened:
    def test_a_failing_gate_names_the_document_and_the_step(self, tmp_path, monkeypatch):
        import regcompass.pipeline as pipeline_mod

        monkeypatch.chdir(ROOT)
        storage, data_dir = _sg_storage(tmp_path)

        def boom(*args, **kwargs):
            raise RuntimeError("the embedder fell over")

        monkeypatch.setattr(pipeline_mod, "gate_document", boom)
        hook = Recorder()
        with pytest.raises(RuntimeError, match="the embedder fell over"):
            _run(storage, data_dir, hook=hook)
        (doc,) = [r["document_id"] for r in storage.corpus_documents("SG")]
        failed = [c for c in hook.calls if c[0] == "failed"]
        assert failed == [
            ("failed", doc, "gate", "RuntimeError: the embedder fell over")
        ]
        assert hook.calls[-1] == failed[0]
        assert ("started", doc, "gate") in hook.steps()
        assert ("finished", doc, "gate") not in hook.steps()

    def test_a_failing_reconcile_names_no_document(self, tmp_path, monkeypatch):
        import regcompass.pipeline as pipeline_mod

        from regcompass.map import ConfigError

        monkeypatch.chdir(ROOT)
        storage, data_dir = _sg_storage(tmp_path)

        # A configuration error skips the transport retry ladder.
        def boom(*args, **kwargs):
            raise ConfigError("no ladder")

        monkeypatch.setattr(pipeline_mod, "reconcile_records", boom)
        hook = Recorder()
        with pytest.raises(ConfigError):
            _run(storage, data_dir, hook=hook)
        assert [c for c in hook.calls if c[0] == "failed"] == [
            ("failed", None, RECONCILE, "ConfigError: no ladder")
        ]

    def test_a_failing_run_without_a_hook_fails_as_before(self, tmp_path, monkeypatch):
        import regcompass.pipeline as pipeline_mod

        monkeypatch.chdir(ROOT)
        storage, data_dir = _sg_storage(tmp_path)

        def boom(*args, **kwargs):
            raise RuntimeError("the embedder fell over")

        monkeypatch.setattr(pipeline_mod, "gate_document", boom)
        with pytest.raises(RuntimeError, match="the embedder fell over"):
            _run(storage, data_dir)


class TestTheServerIsAHook:
    def test_map_progress_moves_the_funnel_bar(self):
        from regcompass.server import RunManager, RunTelemetry

        manager = RunManager()
        assert isinstance(manager, RunProgress)
        manager._telemetry = RunTelemetry()
        manager.map_progress("doc_a", 3, 10)
        manager.map_progress("doc_b", 1, 4)
        assert (manager._telemetry.pairs_done, manager._telemetry.pairs_total) == (4, 14)
        assert manager._telemetry.n_documents == 2
