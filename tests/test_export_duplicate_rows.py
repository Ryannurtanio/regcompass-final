"""A real Run always exports, because duplicate provision rows are
collapsed before the battery judges them.

Every other export test feeds frozen golden m8 records, so nothing caught what
a Run actually produces: the Gate shortlists two neighbouring chunks of one
Document, the Engine quotes a passage in each, and the quote-anchored section
label repair in `export_from_db` re-derives BOTH labels to the same nearest
heading. Two honest Mappings then share one (Economy, Indicator, provision)
key and the battery's duplicate guard, rightly, refuses the file.

The label-repair fix then removed that cause at source: a quote taken from the text ABOVE
a chunk's own section heading is no longer relabelled to the section that ended
before the chunk began, so no fixture Economy's fake-Engine Run manufactures a
duplicate any more (measured below). The
collapse stays as the final guard, because a long section split across two
chunks and quoted in both still yields two Mappings on one provision whatever
the Engine is; the tests that exercise it give one provision a second accepted
Mapping explicitly.

These tests run the fake Engine over each fixture Economy's Corpus, accept
every Mapping the way the interface's accept-all does, and export. Nothing
here touches the network: a socket guard is installed over each Run.
"""

from __future__ import annotations

import shutil
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from corpus_fixtures import seed_corpus  # noqa: E402

from regcompass.corpus import add_document  # noqa: E402
from regcompass.engines import fake_completion, fake_embed, resolve_engine  # noqa: E402
from regcompass.export import (  # noqa: E402
    ABSENCE_MARKER,
    collapse_duplicate_provisions,
    run_gate_battery,
)
from regcompass.pipeline import export_from_db, run_economy  # noqa: E402
from regcompass.storage import Storage  # noqa: E402

LAO_SLICE = ROOT / "tests/fixtures/derived/lao_electronic_transactions_p05_p06.pdf"
LAO_URL = "https://laoofficialgazette.gov.la/etl.pdf"

needs_tesseract = pytest.mark.skipif(
    shutil.which("tesseract") is None, reason="tesseract binary not on PATH"
)

# What a fake-Engine Pillar 7 Run over each fixture Corpus actually produces:
# the (Indicator, provision) keys two Mappings end up sharing once their labels
# are repaired. Measured 16 Sep 2026 as Australia 0, Malaysia 3 (7.2, 7.4, 7.5
# on Part I s. 4), Singapore 4 (7.2 on Part 5B s. 44; 7.4 and 7.5 on Part 6
# s. 57; 7.4 on Part 8 s. 79), Lao PDR 0; every one of those pairs was a
# backwards relabel, and re-measured after the label-repair fix all four Economies
# collapse nothing. Australia's chunks never collided in the first place, and
# the Lao slice is one short scan.
EXPECTED_COLLAPSE: dict[str, set[tuple[str, str]]] = {
    "AU": set(),
    "MY": set(),
    "SG": set(),
    "LA": set(),
}


# ---------------------------------------------------------------------------
# one fake-Engine Run per Economy, shared by the tests below
# ---------------------------------------------------------------------------


def _no_network(monkeypatch) -> None:
    def refuse(*args, **kwargs):
        raise AssertionError("a Run must not touch the network")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


def run_and_accept_all(tmp_path: Path, economy: str, monkeypatch) -> tuple[Storage, str, dict]:
    """Seed this Economy's Corpus, run the fake Engine over Pillar 7, and
    accept every verified Mapping exactly as POST /api/reviews/accept-all does
    (unreviewed ids -> review_set 'accepted'). Returns the Storage, the Run id
    and the {mapping_id: review_status} map the export takes."""
    storage = Storage(tmp_path / "run.db")
    storage.apply_schema()
    data_dir = tmp_path / "data"
    if economy == "LA":
        # The Lao lane has no fixture Corpus entry: the scan enters the Corpus
        # the way a reviewer adds it, with its Language recorded as Lao.
        add_document(
            storage, data_dir, "LA", LAO_SLICE.read_bytes(),
            source_url=LAO_URL, language="Lao",
            filename_hint="lao_electronic_transactions.pdf",
        )
    else:
        seed_corpus(storage, data_dir, economy)
    _no_network(monkeypatch)
    report = run_economy(
        storage, economy, (7,), resolve_engine("fake"), data_dir=data_dir,
        completion_fn=fake_completion, embed_fn=fake_embed,
    )
    # The guard covered the Run, which is the promise worth proving. It comes
    # off here so a test may drive the interface afterwards: TestClient opens a
    # local socketpair of its own, and that is not the network.
    monkeypatch.undo()
    run_id = report.run_id
    for mapping_id in storage.unreviewed_mapping_ids(run_id):
        storage.review_set(
            run_id=run_id, mapping_id=mapping_id, review_status="accepted",
            comment="accepted in bulk from the export preview",
        )
    reviews = {m: r.review_status for m, r in storage.reviews_for_run(run_id).items()}
    assert reviews, "the Run produced no Mapping to review"
    return storage, run_id, reviews


def collapsed_keys(result) -> set[tuple[str, str]]:
    return {(c.indicator_id, c.article_section) for c in result.collapsed}


def duplicate_one_provision(storage: Storage, run_id: str) -> tuple[str, str, str]:
    """Give one provision a second accepted Mapping, the shape that survives
    the label-repair fix: a long section split across two chunks, quoted in both, so two
    honest Mappings carry the same (Economy, Indicator, Law, provision) key.

    Returns (twin mapping id, Indicator id, Article / Section). The twin's id
    extends the original's, so it sorts later and the ORIGINAL is the row the
    collapse keeps."""
    passed = sorted(
        (
            r
            for r in storage.load_mappings(run_id=run_id)
            if r.verification_status == "passed"
        ),
        key=lambda r: r.mapping_id,
    )
    assert passed, "the Run produced no verified Mapping to duplicate"
    original = passed[0]
    twin = original.model_copy(update={"mapping_id": f"{original.mapping_id}::twin"})
    storage.upsert_mappings([twin], run_id=run_id)
    storage.review_set(
        run_id=run_id,
        mapping_id=twin.mapping_id,
        review_status="accepted",
        comment="accepted in bulk from the export preview",
    )
    return twin.mapping_id, original.indicator_id, original.section


def reviews_of(storage: Storage, run_id: str) -> dict:
    return {m: r.review_status for m, r in storage.reviews_for_run(run_id).items()}


# ---------------------------------------------------------------------------
# Box 1: every fixture Economy exports green after accept-all
# ---------------------------------------------------------------------------


class TestAFakeEngineRunExports:
    @pytest.mark.parametrize(
        "economy",
        [
            "AU",
            "MY",
            "SG",
            pytest.param("LA", marks=needs_tesseract),
        ],
    )
    def test_the_battery_passes_and_the_report_names_what_it_collapsed(
        self, tmp_path, monkeypatch, economy
    ):
        storage, run_id, reviews = run_and_accept_all(tmp_path, economy, monkeypatch)
        result = export_from_db(storage, tmp_path / "out", run_id=run_id, reviews=reviews)

        expected = EXPECTED_COLLAPSE[economy]
        assert collapsed_keys(result) == expected
        assert result.rows_collapsed == len(expected)
        assert result.csv_path.exists() and result.xlsx_path is not None
        keys = [
            (r["Economy"], r["Indicator ID"], r["Law Name"], r["Article / Section"])
            for r in result.rows
            if r["Article / Section"] != ABSENCE_MARKER
        ]
        assert len(keys) == len(set(keys)), "the written rows still carry a duplicate"

        import json

        supplementary = json.loads(result.supplementary_path.read_text(encoding="utf-8"))
        assert ("duplicate_collapse" in supplementary) is bool(expected), (
            "an export that collapsed nothing must not grow a disclosure block"
        )

    def test_the_collapse_leaves_every_review_decision_in_the_database(
        self, tmp_path, monkeypatch
    ):
        storage, run_id, _reviews = run_and_accept_all(tmp_path, "MY", monkeypatch)
        duplicate_one_provision(storage, run_id)
        result = export_from_db(
            storage, tmp_path / "out", run_id=run_id, reviews=reviews_of(storage, run_id)
        )
        dropped = [m for c in result.collapsed for m in c.dropped_mapping_ids]
        assert dropped, "the duplicated provision must have collapsed"
        after = storage.reviews_for_run(run_id)
        assert {m: after[m].review_status for m in dropped} == {
            m: "accepted" for m in dropped
        }, "a collapsed row must not change the reviewer's decision"

    def test_the_export_report_says_what_was_collapsed(self, tmp_path, monkeypatch):
        """What a reviewer reads: the supplementary JSON block the API and the
        CLI both draw on, and the one sentence they print."""
        import json

        from regcompass.export import duplicate_collapse_notice

        storage, run_id, _reviews = run_and_accept_all(tmp_path, "MY", monkeypatch)
        twin_id, indicator, _stored_section = duplicate_one_provision(storage, run_id)
        result = export_from_db(
            storage, tmp_path / "out", run_id=run_id, reviews=reviews_of(storage, run_id)
        )

        block = json.loads(result.supplementary_path.read_text(encoding="utf-8"))[
            "duplicate_collapse"
        ]
        assert block["rows_collapsed"] == result.rows_collapsed == 1
        entry = block["collapsed"][0]
        assert entry["indicator_id"] == indicator
        assert entry["kept_mapping_id"] and entry["kept_mapping_id"] != twin_id
        assert entry["dropped_mapping_ids"] == [twin_id]
        # The provision named is the label the export SHIPS, which the
        # quote-anchored repair may have refined (a Part prefix) since the
        # Mapping was stored.
        assert entry["article_section"].endswith(_stored_section)

        notice = duplicate_collapse_notice(result)
        assert notice is not None
        assert "1 row(s) collapsed" in notice
        assert entry["article_section"] in notice

    def test_two_exports_of_the_same_database_are_byte_identical(
        self, tmp_path, monkeypatch
    ):
        storage, run_id, reviews = run_and_accept_all(tmp_path, "SG", monkeypatch)
        first = export_from_db(storage, tmp_path / "a", run_id=run_id, reviews=reviews)
        second = export_from_db(storage, tmp_path / "b", run_id=run_id, reviews=reviews)
        assert first.csv_path.read_bytes() == second.csv_path.read_bytes()
        assert first.rows_collapsed == second.rows_collapsed


# ---------------------------------------------------------------------------
# Box 2: which row survives a collapse
# ---------------------------------------------------------------------------


def _row(mapping_id: str, confidence: str, **over) -> dict:
    row = {
        "Economy": "Malaysia",
        "Law Name": "Personal Data Protection Act 2010",
        "Indicator ID": "7.2",
        "Article / Section": "Part I s. 4",
        "Confidence": confidence,
        "_mapping_id": mapping_id,
    }
    row.update(over)
    return row


class TestWhichRowSurvives:
    def test_the_highest_confidence_row_is_the_one_kept(self):
        rows = [_row("m_b", "0.42"), _row("m_a", "0.77"), _row("m_c", "0.13")]
        kept, collapsed = collapse_duplicate_provisions(rows)
        assert [r["_mapping_id"] for r in kept] == ["m_a"]
        assert len(collapsed) == 1
        assert collapsed[0].kept_mapping_id == "m_a"
        assert collapsed[0].dropped_mapping_ids == ("m_b", "m_c")
        assert collapsed[0].indicator_id == "7.2"
        assert collapsed[0].article_section == "Part I s. 4"

    def test_a_controlling_row_outranks_a_better_scoring_subordinate(self):
        """Confidence is a mechanical anchoring composite; Controlling Evidence
        is the reconciler's finding about which instrument governs. Collapsing
        away the only controlling row would leave the Indicator evidenced by a
        subordinate instrument, whatever the arithmetic says."""
        rows = [
            _row("m_sub", "0.90", **{"Controlling Evidence": "false"}),
            _row("m_ctrl", "0.31", **{"Controlling Evidence": "true"}),
        ]
        kept, collapsed = collapse_duplicate_provisions(rows)
        assert [r["_mapping_id"] for r in kept] == ["m_ctrl"]
        assert collapsed[0].kept_mapping_id == "m_ctrl"
        assert collapsed[0].dropped_mapping_ids == ("m_sub",)

    def test_the_earlier_mapping_id_breaks_a_confidence_tie(self):
        rows = [_row("m_z", "0.50"), _row("m_a", "0.50")]
        kept, collapsed = collapse_duplicate_provisions(rows)
        assert [r["_mapping_id"] for r in kept] == ["m_a"]
        assert collapsed[0].dropped_mapping_ids == ("m_z",)

    def test_two_laws_sharing_a_section_label_are_not_one_provision(self):
        rows = [
            _row("m_a", "0.90"),
            _row("m_b", "0.10", **{"Law Name": "Communications and Multimedia Act 1998"}),
        ]
        kept, collapsed = collapse_duplicate_provisions(rows)
        assert [r["_mapping_id"] for r in kept] == ["m_a", "m_b"]
        assert collapsed == []

    def test_absence_rows_are_never_collapsed(self):
        rows = [
            _row("m_a", "", **{"Article / Section": ABSENCE_MARKER}),
            _row("m_b", "", **{"Article / Section": ABSENCE_MARKER}),
        ]
        kept, collapsed = collapse_duplicate_provisions(rows)
        assert len(kept) == 2 and collapsed == []


# ---------------------------------------------------------------------------
# Box 3: the battery keeps its guard
# ---------------------------------------------------------------------------


class TestTheGuardStays:
    def test_a_hand_built_duplicate_pair_still_fails_the_battery(self):
        """The collapse feeds the battery, it does not replace it: a duplicate
        pair that reaches run_gate_battery is still refused by name."""
        from regcompass.config import load_portals
        from regcompass.export import COLUMNS, EXTRA_COLUMNS

        def full_row(mapping_id: str) -> dict:
            row = {c: "x" for c in COLUMNS}
            row.update(
                {
                    "Economy": "Malaysia",
                    "Law Name": "Personal Data Protection Act 2010",
                    "Indicator ID": "7.2",
                    "Article / Section": "Part I s. 4",
                    "Discovery Tag": "NEW",
                    "Verbatim Snippet": "A data user shall not process personal data",
                    "Source URL": "https://lom.agc.gov.my/act709.pdf",
                    "Confidence": "0.50",
                    "Notes": "",
                    "Mapping Rationale": "",
                }
            )
            row.update({c: "" for c in EXTRA_COLUMNS})
            row.update(
                {
                    "_chunk_id": "c1",
                    "_document_id": "doc_my",
                    "_mapping_id": mapping_id,
                    "_repeal_status": "",
                    "_economy_code": "MY",
                }
            )
            return row

        rows = [full_row("m_a"), full_row("m_b")]
        failures = run_gate_battery(
            rows,
            {"c1": "A data user shall not process personal data without consent."},
            load_portals(),
            lambda url: True,
        )
        assert any("duplicate provision-indicator row" in f for f in failures)


# ---------------------------------------------------------------------------
# Box 4: the operator is told, on both surfaces
# ---------------------------------------------------------------------------


class TestWhatTheOperatorSees:
    def test_the_cli_export_prints_the_collapse_line(self, tmp_path, monkeypatch):
        from typer.testing import CliRunner

        from regcompass.cli import app

        storage, run_id, _reviews = run_and_accept_all(tmp_path, "MY", monkeypatch)
        _twin_id, _indicator, stored_section = duplicate_one_provision(storage, run_id)
        db = Path(storage.conn.execute("PRAGMA database_list").fetchone()["file"])
        storage.close()

        result = CliRunner().invoke(
            app, ["export", "--db", str(db), "--out", str(tmp_path / "cliout")]
        )
        assert result.exit_code == 0, result.output
        assert "1 row(s) collapsed" in result.output
        assert stored_section in result.output

    def test_the_export_endpoint_reports_the_collapse(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        from regcompass.server import create_app

        storage, run_id, _reviews = run_and_accept_all(tmp_path, "MY", monkeypatch)
        _twin_id, _indicator, stored_section = duplicate_one_provision(storage, run_id)
        db = Path(storage.conn.execute("PRAGMA database_list").fetchone()["file"])
        storage.close()

        out = tmp_path / "apiout"
        out.mkdir()
        client = TestClient(
            create_app(db_path=db, out_dir=out, data_dir=tmp_path / "data", ui_dir=None)
        )
        response = client.post("/api/export")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["rows_collapsed"] == 1
        assert "1 row(s) collapsed" in body["duplicate_collapse"]
        assert stored_section in body["duplicate_collapse"]
