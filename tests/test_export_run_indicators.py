"""An Evidence Export answers only for the Indicators its Run searched.

A Run can be narrowed to a few Indicators of one Pillar. Its absence rows
say "No provision found" after candidates were screened, so they may only be
written for the Indicators the Gate was actually asked about. The Pillar alone
is not enough: a Run over 7.3 and 7.4 never screened 7.1, 7.2 or 7.5.
"""

from __future__ import annotations

import csv
import re
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from corpus_fixtures import seed_corpus  # noqa: E402

from regcompass.config import load_corpus, load_crosswalk  # noqa: E402
from regcompass.engines import fake_completion, fake_embed, resolve_engine  # noqa: E402
from regcompass.export import build_absence_rows  # noqa: E402
from regcompass.pipeline import export_from_db, run_economy  # noqa: E402
from regcompass.storage import Storage  # noqa: E402

SEARCHED = ["7.3", "7.4"]


def _no_network(monkeypatch) -> None:
    def refuse(*args, **kwargs):
        raise AssertionError("a Run must not touch the network")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


def _export(
    tmp_path: Path, indicators, *, earlier_whole_pillar: bool = False,
    storage_out=None, decision: str = "accepted",
):
    monkeypatch = pytest.MonkeyPatch()
    storage = Storage(tmp_path / "run.db")
    storage.apply_schema()
    data_dir = tmp_path / "data"
    seed_corpus(storage, data_dir, "SG")
    try:
        _no_network(monkeypatch)
        if earlier_whole_pillar:
            # Gate scores are kept for every Run on the database.
            run_economy(
                storage, "SG", (7,), resolve_engine("fake"), data_dir=data_dir,
                completion_fn=fake_completion, embed_fn=fake_embed,
            )
        report = run_economy(
            storage, "SG", (7,), resolve_engine("fake"), data_dir=data_dir,
            completion_fn=fake_completion, embed_fn=fake_embed,
            indicators=indicators,
        )
    finally:
        monkeypatch.undo()
    if storage_out is not None:
        storage_out.append(storage)
    for mapping_id in storage.unreviewed_mapping_ids(report.run_id):
        storage.review_set(
            run_id=report.run_id, mapping_id=mapping_id, review_status=decision,
            comment=f"{decision} in bulk",
        )
    reviews = {m: r.review_status for m, r in storage.reviews_for_run(report.run_id).items()}
    result = export_from_db(storage, tmp_path / "out", run_id=report.run_id, reviews=reviews)
    with open(result.csv_path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


@pytest.fixture(scope="module")
def narrowed_rows(tmp_path_factory):
    return _export(tmp_path_factory.mktemp("narrowed"), SEARCHED)


class TestANarrowedRunExportsOnlyWhatItSearched:
    def test_no_row_names_an_indicator_the_run_never_searched(self, narrowed_rows):
        assert narrowed_rows, "the export wrote no rows at all"
        assert {r["Indicator ID"] for r in narrowed_rows} <= set(SEARCHED)

    def test_every_searched_indicator_still_earns_a_row(self, narrowed_rows):
        assert {r["Indicator ID"] for r in narrowed_rows} == set(SEARCHED)

    def test_the_absence_builder_takes_the_run_indicator_list(self):
        rows = build_absence_rows(
            [], load_corpus(), load_crosswalk(),
            {"SG": {"law": "Telecommunications Act 1999", "sections": 98, "pairs_gated": 4}},
            run_pillars=(7,), run_indicators=("7.3",),
        )
        assert {r["Indicator ID"] for r in rows} == {"7.3"}


class TestTheScreenedCountIsNarrowedToo:
    def test_notes_count_only_the_searched_indicators_pairs(self, tmp_path):
        """A Run narrowed to one Indicator, after a whole-Pillar Run on the
        same database: its Notes count the pairs of that Indicator alone."""
        kept = []
        # Rejected in review, so every row is a zero stating its count.
        rows = _export(
            tmp_path, ["7.5"], earlier_whole_pillar=True, storage_out=kept,
            decision="rejected",
        )
        cosines = kept[0].load_gate_scores()
        searched = sum(1 for (_, ind) in cosines if ind == "7.5")
        everything = sum(1 for (_, ind) in cosines if ind.startswith("7."))
        assert searched < everything, "the earlier Run left no other Gate scores"
        counts = {
            int(m.group(1))
            for r in rows
            for m in [re.search(r"(\d+) gate-passed", r["Notes"])] if m
        }
        assert counts, "no row states a screened count"
        assert counts == {searched}


class TestAWholePillarRunIsUnchanged:
    def test_no_indicator_list_keeps_every_indicator_of_the_pillar(self):
        rows = build_absence_rows(
            [], load_corpus(), load_crosswalk(),
            {"SG": {"law": "Telecommunications Act 1999", "sections": 98, "pairs_gated": 4}},
            run_pillars=(7,), run_indicators=None,
        )
        assert {r["Indicator ID"] for r in rows} == {"7.1", "7.2", "7.3", "7.4", "7.5"}
