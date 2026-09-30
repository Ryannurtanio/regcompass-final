"""The Discovery rehearsal script: one cell end to end over recorded Portal
answers, the guard that makes a paid call impossible, resume, and the table.

Offline throughout: a recorded fetch answers every request and the fake
embedder stands in for the local one."""

from __future__ import annotations

import sys
from functools import partial
from pathlib import Path

import pytest

pytest.importorskip("httpx", reason="Discovery tests need the `live` extra (httpx)")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import regcompass.engines as engines  # noqa: E402
import regcompass.map as map_module  # noqa: E402
import regcompass.pipeline as pipeline  # noqa: E402
import rehearse_discovery as rh  # noqa: E402
from regcompass.discovery import discover_economy  # noqa: E402

from test_discovery import NullLimiter, recorded_fetch  # noqa: E402
from test_discovery_by_pillar import (  # noqa: E402
    BODIES,
    ETA_URL,
    TAGGED_SG_SEEDS,
    _write_baseline,
)  # noqa: E402

GUARDED = [
    (engines, "make_completion"),
    (map_module, "map_gated_chunk"),
    (pipeline, "map_gated_chunk"),
    (pipeline, "map_and_verify_pairs"),
    (pipeline, "gloss_document"),
]


@pytest.fixture()
def guarded(monkeypatch):
    """The guard, installed for one test and undone after it."""
    for module, name in GUARDED:
        monkeypatch.setattr(module, name, getattr(module, name))
    try:
        import litellm

        monkeypatch.setattr(litellm, "completion", litellm.completion)
        monkeypatch.setattr(litellm, "acompletion", litellm.acompletion)
    except ImportError:
        pass
    env = {"OPENROUTER_API_KEY": "x", "OTHER_API_KEY": "y", "PATH": "/bin"}
    removed = rh.install_guard(env)
    return env, removed


def _fake_discover(tmp_path):
    fetch = recorded_fetch(BODIES)
    return partial(
        discover_economy, fetch=fetch, limiter=NullLimiter(),
        baseline_dir=_write_baseline(tmp_path), seeds=TAGGED_SG_SEEDS,
    )


class TestTheGuard:
    def test_keys_are_removed_and_every_completion_path_refuses(self, guarded):
        env, removed = guarded
        assert sorted(removed) == ["OPENROUTER_API_KEY", "OTHER_API_KEY"]
        assert env == {"PATH": "/bin"}
        with pytest.raises(rh.EngineCallRefused):
            engines.make_completion(engines.resolve_engine("engine-b"))
        with pytest.raises(rh.EngineCallRefused):
            map_module.map_gated_chunk()
        assert pipeline.map_and_verify_pairs is rh.record_candidates

    def test_a_cell_runs_discovery_and_the_gate_and_never_the_engine(
        self, guarded, tmp_path
    ):
        row = rh.rehearse_cell(
            "SG", 6, tmp_path / "work", discover=_fake_discover(tmp_path),
            prepare=partial(rh.prepare_documents, embed_fn=engines.fake_embed),
        )
        assert row["status"] == "ok"
        # The Electronic Transactions Act from the baseline, the
        # Telecommunications Act (tagged for Pillar 6) and the Personal Data
        # Protection Act (untagged, so every Pillar) from the crawler seeds.
        assert row["baseline_fetched"] == 1 and row["crawler_fetched"] == 2
        assert row["fetched"] == 3
        doc = next(d for d in row["documents"] if d["source_url"] == ETA_URL)
        assert doc["pieces"] >= 1 and doc["chars"] > 0 and not doc["garbage"]
        # Every Indicator of the Pillar has a count, zero or more.
        assert set(row["candidates"]) == set(row["indicators"])
        assert "unrelated act" in {s["law"].lower() for s in row["skipped_detail"]}
        assert row["skipped"] == {"unreachable": 1}
        assert row["dead_hosts"] == {"sso.agc.gov.sg": "HTTP 404"}
        assert row["crawler_failed"] == 0 and row["crawler_misses"] == []


class TestResumeAndTable:
    def _row(self, e, p, **kw):
        base = {
            "economy": e, "pillar": p, "status": "ok", "fetched": 2,
            "baseline_fetched": 1, "crawler_fetched": 1, "seconds": 10.0,
            "skipped": {}, "candidates": {"7.1": 3, "7.2": 1}, "documents": [],
            "sections": 40,
        }
        base.update(kw)
        return base

    def test_done_cells_are_skipped_and_failed_ones_redone_on_request(self, tmp_path):
        results = tmp_path / "results"
        rh.append_result(results, self._row("ID", 7))
        rh.append_result(results, self._row("ID", 6, status="crashed"))
        with open(results / "ID.jsonl", "a") as fh:
            fh.write('{"economy": "ID", "pil')  # a line cut short by a kill
        done = rh.load_results(results)
        assert set(done) == {("ID", 7), ("ID", 6)}
        assert rh.cells_to_run(["ID", "IN"], [6, 7], done) == {"IN": [6, 7]}
        assert rh.cells_to_run(["ID"], [6, 7], done, redo_failed=True) == {"ID": [6]}

    def test_an_official_source_list_counts_as_a_named_law(self, tmp_path):
        results = tmp_path / "results"
        found = [
            {"found_by": "official source list 7.1", "status": "fetched"},
            {"found_by": "portal crawler", "status": "fetched"},
            {"found_by": "baseline 7.2", "status": "already in the Corpus"},
        ]
        rh.append_result(results, self._row("VN", 7, found_by=found, baseline_fetched=0))
        row = rh.load_results(results)[("VN", 7)]
        assert (row["baseline_fetched"], row["crawler_fetched"]) == (1, 1)

    def test_the_last_line_for_a_cell_wins(self, tmp_path):
        results = tmp_path / "results"
        rh.append_result(results, self._row("ID", 7, status="crashed"))
        rh.append_result(results, self._row("ID", 7))
        assert rh.load_results(results)[("ID", 7)]["status"] == "ok"

    def test_verdicts_and_the_table(self):
        rows = {
            ("ID", 7): self._row("ID", 7),
            ("ID", 6): self._row("ID", 6, candidates={"6.1": 0, "6.2": 4}),
            ("VN", 7): self._row(
                "VN", 7, fetched=0, baseline_fetched=0, crawler_fetched=0,
                candidates={"7.1": 0}, notes=["no 2025 baseline exists for Viet Nam"],
            ),
            ("TH", 7): self._row(
                "TH", 7, fetched=0, baseline_fetched=0, crawler_fetched=0,
                skipped={"not_allowed_host": 3}, candidates={"7.1": 0},
            ),
        }
        assert rh.verdict(rows[("ID", 7)]) == "ok"
        assert rh.verdict(rows[("ID", 6)]) == "partial"
        assert rh.verdict(rows[("VN", 7)]) == "gap"
        assert "0 candidates: 6.1" in rh.why(rows[("ID", 6)])
        assert "not_allowed_host 3" in rh.why(rows[("TH", 7)])
        assert "no baseline law" in rh.why(rows[("VN", 7)])
        table = rh.render_table(rows, economies=("ID", "TH"), pillars=(6, 7))
        assert "| ID | 7 | ok | 2 (1+1) | y |" in table
        assert "| TH | 6 | not run |" in table
        summary = rh.render_summary(rows, economies=("ID",), pillars=(6, 7))
        assert "| ID | 1 | 1 | 0 | 0 |" in summary

    def test_the_markdown_carries_the_previous_pass_summary(self, tmp_path):
        now = {("ID", 7): self._row("ID", 7)}
        before = {("ID", 7): self._row("ID", 7, fetched=0, baseline_fetched=0)}
        out = tmp_path / "E.md"
        rh.write_markdown(out, now, before)
        text = out.read_text()
        assert "## Summary of the previous pass" in text
        assert "\u2014" not in text

    def test_unreachable_hosts_are_read_from_the_reason_not_the_law_list(self):
        detail = [
            {"code": "unreachable", "reason": "The host could not be reached or did not"
             " send the law (HTTP 403 from https://www.meity.gov.in/files/a.pdf)."},
            {"code": "unreachable", "reason": "The host could not be reached or did not"
             " send the law (peraturan.bpk.go.id did not answer earlier; not asked again)."},
            {"code": "no_url", "reason": "No address."},
        ]
        assert rh.unreachable_hosts(detail) == {
            "peraturan.bpk.go.id": "no answer", "www.meity.gov.in": "HTTP 403",
        }

    def test_a_refused_path_or_host_counts_as_unreachable(self):
        detail = [
            {"code": "unreachable", "reason": "The host could not be reached or did not"
             " send the law (peraturan.bpk.go.id refused /details/ addresses earlier;"
             " not asked again)."},
            {"code": "unreachable", "reason": "The host could not be reached or did not"
             " send the law (jdih.kominfo.go.id refused 3 kinds of addresses earlier;"
             " not asked again)."},
            {"code": "unreachable", "reason": "The host could not be reached or did not"
             " send the law (www.bi.go.id refused its robots.txt earlier; not asked again)."},
        ]
        assert rh.unreachable_hosts(detail) == {
            "jdih.kominfo.go.id": "refused 3 kinds of addresses",
            "peraturan.bpk.go.id": "refused /details/ addresses",
            "www.bi.go.id": "refused its robots.txt",
        }

    def test_the_markdown_names_the_cap_it_ran_with(self, tmp_path):
        out = tmp_path / "table.md"
        rh.write_markdown(out, {("ID", 7): self._row("ID", 7)}, cap=5)
        text = out.read_text()
        assert text.startswith("# Discovery rehearsal")
        assert "cap 5," in text and "cap 12" not in text

    def test_text_quality_flags_a_broken_layer(self):
        assert rh.text_quality("")["garbage"]
        assert rh.text_quality("(cid:12)(cid:40) " * 100)["garbage"]
        assert not rh.text_quality("Section 1. This Act applies to data. " * 50)["garbage"]
        # Thai vowel signs are combining marks, not letters to str.isalpha.
        thai = "มาตรา ๑ พระราชบัญญัตินี้เรียกว่า พระราชบัญญัติการประกอบธุรกิจของคนต่างด้าว " * 40
        assert not rh.text_quality(thai)["garbage"]
