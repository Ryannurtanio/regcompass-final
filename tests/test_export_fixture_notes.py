"""A seeded Corpus discloses itself in the Export.

`regcompass seed` fills a Corpus offline from the
legislation bundled with the install, so the keyless demo has something to Run
against on a clean machine. Those bytes are the real Act, but they arrived from
the repository rather than from the Portal on the day of the Run, and a reader
of the submission file has no other way to learn that. So every row built on a
Document whose source kind is 'fixture' says so in Notes, exactly as a manually
added Document's rows do.

The disclosure rides the shared row composer, which is what makes a
provision row and its Economy's absence rows carry the same sentence. It is a
NOTE and nothing more: a fixture Document is a curated corpus entry with its
official Portal URL, so it earns no whitelist exemption.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from corpus_fixtures import seed_corpus  # noqa: E402

from regcompass.contracts import CorpusDoc  # noqa: E402
from regcompass.corpus import (  # noqa: E402
    DISCOVERY_SOURCE_KIND,
    FIXTURE_SOURCE_KIND,
    MANUAL_SOURCE_KIND,
)
from regcompass.engines import fake_completion, fake_embed, resolve_engine  # noqa: E402
from regcompass.export import (  # noqa: E402
    ABSENCE_MARKER,
    ALLOW_ANY_HOST_NOTE,
    FIXTURE_ADD_NOTE,
    MANUAL_ADD_NOTE,
    SyntheticDoc,
    synthetic_notes,
)
from regcompass.fixtures import seed_economy  # noqa: E402
from regcompass.pipeline import export_from_db, run_economy  # noqa: E402
from regcompass.storage import Storage  # noqa: E402

# Singapore: the Economy the README's second demo block names, and the one
# whose Run needed the duplicate collapse before it could export at all.
DEMO_ECONOMY = "SG"


def _no_network(monkeypatch) -> None:
    def refuse(*args, **kwargs):
        raise AssertionError("a Run must not touch the network")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


def _run_and_accept_all(tmp_path: Path, economy: str, monkeypatch, *, seeder):
    """Fill this Economy's Corpus with the given seeder, Run the fake Engine
    over Pillar 7 under a network guard, and accept every Mapping the way the
    interface's accept-all does."""
    storage = Storage(tmp_path / "run.db")
    storage.apply_schema()
    data_dir = tmp_path / "data"
    seeder(storage, data_dir, economy)
    _no_network(monkeypatch)
    report = run_economy(
        storage, economy, (7,), resolve_engine("fake"), data_dir=data_dir,
        completion_fn=fake_completion, embed_fn=fake_embed,
    )
    monkeypatch.undo()
    for mapping_id in storage.unreviewed_mapping_ids(report.run_id):
        storage.review_set(
            run_id=report.run_id, mapping_id=mapping_id, review_status="accepted",
            comment="accepted in bulk from the export preview",
        )
    reviews = {m: r.review_status for m, r in storage.reviews_for_run(report.run_id).items()}
    assert reviews, "the Run produced no Mapping to review"
    return storage, report.run_id, reviews


@pytest.fixture(scope="module")
def seeded_export(tmp_path_factory):
    """One fixture-seeded Singapore Run, exported once. Module scope because
    the Run is the expensive part and every assertion below reads the same
    rows."""
    monkeypatch = pytest.MonkeyPatch()
    tmp_path = tmp_path_factory.mktemp("fixture-export")
    try:
        storage, run_id, reviews = _run_and_accept_all(
            tmp_path, DEMO_ECONOMY, monkeypatch, seeder=seed_economy
        )
    finally:
        monkeypatch.undo()
    return export_from_db(storage, tmp_path / "out", run_id=run_id, reviews=reviews)


@pytest.fixture(scope="module")
def discovered_export(tmp_path_factory):
    """The same Economy, the same bytes, entered as a DISCOVERY. The control:
    whatever the fixture rows gain, these rows must not."""
    monkeypatch = pytest.MonkeyPatch()
    tmp_path = tmp_path_factory.mktemp("discovered-export")
    try:
        storage, run_id, reviews = _run_and_accept_all(
            tmp_path, DEMO_ECONOMY, monkeypatch, seeder=seed_corpus
        )
    finally:
        monkeypatch.undo()
    return export_from_db(storage, tmp_path / "out", run_id=run_id, reviews=reviews)


class TestTheComposerKnowsTheSourceKind:
    def test_a_fixture_document_with_no_synthetic_entry_still_earns_the_note(self):
        """A fixture Document is IN config/corpus.yaml, so it never becomes a
        SyntheticDoc. Before this the composer had nothing to say about it."""
        assert synthetic_notes(None, FIXTURE_SOURCE_KIND) == [FIXTURE_ADD_NOTE]

    def test_a_discovered_document_earns_nothing(self):
        assert synthetic_notes(None, DISCOVERY_SOURCE_KIND) == []
        assert synthetic_notes(None, None) == []

    def test_the_manual_lane_is_untouched(self):
        doc = SyntheticDoc(
            CorpusDoc(economy="LA", law_name="x", source_url="https://x/y.pdf", url_is_direct=True),
            allow_any_host=True, manual_added=True,
        )
        notes = synthetic_notes(doc, MANUAL_SOURCE_KIND)
        assert ALLOW_ANY_HOST_NOTE in notes and MANUAL_ADD_NOTE in notes
        assert FIXTURE_ADD_NOTE not in notes

    def test_a_fixture_note_never_arrives_twice(self):
        doc = SyntheticDoc(
            CorpusDoc(economy="SG", law_name="x", source_url="https://x/y.pdf", url_is_direct=True)
        )
        assert synthetic_notes(doc, FIXTURE_SOURCE_KIND).count(FIXTURE_ADD_NOTE) == 1


class TestASeededRunExportsAndDisclosesItself:
    def test_the_battery_passes_and_the_collapse_is_reported(self, seeded_export):
        """Singapore needed the duplicate collapse to export at all: its Run
        produced 4 duplicate provision rows, every one of them a quote taken
        from the text above a chunk's own heading and relabelled to the section
        that ended before that chunk. The label-repair fix stopped that at source, so the
        seeded Run now collapses nothing and still exports, which is the proof
        behind the README's Singapore demo block."""
        assert seeded_export.csv_path.exists()
        assert seeded_export.rows_collapsed == 0, seeded_export.rows_collapsed
        assert seeded_export.rows

    def test_every_provision_row_discloses_the_fixture_corpus(self, seeded_export):
        provisions = [
            r for r in seeded_export.rows if r["Article / Section"] != ABSENCE_MARKER
        ]
        assert provisions
        for row in provisions:
            assert FIXTURE_ADD_NOTE in row["Notes"], row["Article / Section"]

    def test_every_absence_row_discloses_it_too(self, seeded_export):
        """An earned zero rests on the same Document, so it owes the same
        sentence: one composer serves both lanes."""
        absences = [
            r for r in seeded_export.rows if r["Article / Section"] == ABSENCE_MARKER
        ]
        assert absences
        for row in absences:
            assert FIXTURE_ADD_NOTE in row["Notes"], row["Indicator ID"]

    def test_the_note_says_what_a_reader_needs_to_know(self):
        assert "fixture legislation seeded from the install, demo only" == FIXTURE_ADD_NOTE

    def test_a_fixture_document_earns_no_whitelist_exemption(self, seeded_export):
        """The note is a disclosure, not a licence: these Documents carry their
        official Portal URL and are checked against the whitelist like any
        other."""
        for row in seeded_export.rows:
            assert ALLOW_ANY_HOST_NOTE not in row["Notes"]


class TestADiscoveredRunIsUntouched:
    def test_no_row_carries_the_fixture_note(self, discovered_export):
        for row in discovered_export.rows:
            assert FIXTURE_ADD_NOTE not in row["Notes"]

    def test_the_two_runs_produce_the_same_rows_apart_from_the_note(
        self, seeded_export, discovered_export
    ):
        """The disclosure is the ONLY difference the source kind makes: same
        provisions, same Indicators, same quotes."""
        def keys(result):
            return sorted(
                (r["Economy"], r["Indicator ID"], r["Article / Section"])
                for r in result.rows
            )

        assert keys(seeded_export) == keys(discovered_export)
        assert seeded_export.rows_collapsed == discovered_export.rows_collapsed
