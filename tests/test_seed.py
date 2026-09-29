"""The offline seed: a Corpus a judge can Run against without a network.

The second defect of the Docker work was that the keyless demo exited 2 with
"no Documents in the Corpus" on a clean volume, which sent the judge to a
network Discovery. `regcompass seed` is the offline answer: the fixture
legislation the image already carries goes into the Corpus through the SAME
write path Discovery and "Add document" use, marked with its own source kind so
nobody mistakes a demo Corpus for a collected one.

Every test here runs under the network guard, because "offline" is the whole
claim being made.
"""

from __future__ import annotations

import socket
import sys

import pytest

from regcompass.corpus import FIXTURE_SOURCE_KIND
from regcompass.fixtures import (
    BUNDLED,
    DEMO_ECONOMY,
    FixtureLegislationMissingError,
    seed_economy,
    seedable_economies,
)
from regcompass.storage import Storage

# The modules that ARE the network lane, mirrored from tests/test_corpus_run.py.
NETWORK_MODULES = ("regcompass.crawl", "regcompass.discovery")


@pytest.fixture()
def no_network(monkeypatch):
    """Every outbound connection raises, AND importing the network lane raises.
    A seed that completes under this guard made no request by any route."""

    def refuse(*args, **kwargs):
        raise AssertionError("a seed must not touch the network")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)

    class RefuseTheNetworkLane:
        def find_spec(self, name, path=None, target=None):
            if name in NETWORK_MODULES:
                raise AssertionError(f"a seed must not import {name}")
            return None

    for name in NETWORK_MODULES:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(sys, "meta_path", [RefuseTheNetworkLane(), *sys.meta_path])
    return refuse


@pytest.fixture(scope="module")
def seeded_sg(tmp_path_factory):
    """One seeded SG Corpus, shared by the read-only assertions below. SG is the
    smallest bundled Document, so the shared fixture costs one ingest."""
    root = tmp_path_factory.mktemp("seed")
    storage = Storage(root / "seed.db")
    storage.apply_schema()
    result = seed_economy(storage, root / "data", "SG")
    return storage, root / "data", result


class TestWhatTheSeedPutsInTheCorpus:
    def test_the_bundled_document_lands_as_a_corpus_row(self, seeded_sg):
        storage, _, result = seeded_sg
        assert [a.document_id for a in result.added] == [
            "doc_sg_telecommunications_act_1999"
        ]
        rows = storage.corpus_documents("SG")
        assert [r["document_id"] for r in rows] == [a.document_id for a in result.added]

    def test_the_row_is_marked_as_fixture_legislation(self, seeded_sg):
        """Not 'discovery' and not 'manual': these bytes came out of the image,
        so the row says so and the disclosure can follow the row."""
        storage, _, _ = seeded_sg
        rows = storage.corpus_documents("SG")
        assert {r["source_kind"] for r in rows} == {FIXTURE_SOURCE_KIND}
        assert FIXTURE_SOURCE_KIND == "fixture"

    def test_the_row_carries_the_economys_configured_language(self, seeded_sg):
        storage, _, _ = seeded_sg
        rows = storage.corpus_documents("SG")
        assert rows[0]["language"] == "English"

    def test_the_row_carries_the_official_source_url(self, seeded_sg):
        """A judge reading the export must still be able to reach the real Act:
        the bytes are bundled, the provenance is not invented."""
        storage, _, _ = seeded_sg
        assert storage.corpus_documents("SG")[0]["source_url"].startswith(
            "https://sso.agc.gov.sg/"
        )

    def test_the_stored_bytes_land_under_the_data_dir(self, seeded_sg):
        _, data_dir, _ = seeded_sg
        assert list((data_dir / "SG" / "raw").glob("*.pdf"))

    def test_an_explicit_language_overrides_the_portal_default(self, tmp_path, no_network):
        storage = Storage(tmp_path / "lang.db")
        storage.apply_schema()
        seed_economy(storage, tmp_path / "data", "SG", language="Malay")
        assert storage.corpus_documents("SG")[0]["language"] == "Malay"


class TestTheSeedIsOffline:
    def test_seeding_makes_no_request_by_any_route(self, tmp_path, no_network):
        storage = Storage(tmp_path / "offline.db")
        storage.apply_schema()
        result = seed_economy(storage, tmp_path / "data", "SG")
        assert len(result.added) == 1


class TestSeedingTwiceAddsNothing:
    def test_the_second_seed_reports_the_documents_already_present(
        self, tmp_path, no_network
    ):
        """Same bytes twice is one Document, not two. The add path already
        refuses it; the seed catches that and says 'already seeded' rather than
        failing a judge's second `docker compose up`."""
        storage = Storage(tmp_path / "twice.db")
        storage.apply_schema()
        first = seed_economy(storage, tmp_path / "data", "SG")
        second = seed_economy(storage, tmp_path / "data", "SG")
        assert [a.document_id for a in first.added] == second.already_present
        assert second.added == []
        assert len(storage.corpus_documents("SG")) == 1


class TestWhichEconomiesCanBeSeeded:
    def test_the_bundled_economies_are_the_seedable_ones(self):
        assert seedable_economies() == tuple(sorted(BUNDLED))
        assert set(seedable_economies()) == {"AU", "MY", "SG"}

    def test_the_demo_economy_is_bundled(self):
        """The compose demo Runs this Economy, so a missing fixture here is a
        broken judge path, not a missing extra."""
        assert DEMO_ECONOMY in BUNDLED

    def test_an_economy_with_no_bundled_legislation_says_which_ones_have_some(
        self, tmp_path
    ):
        storage = Storage(tmp_path / "none.db")
        storage.apply_schema()
        with pytest.raises(FixtureLegislationMissingError) as exc:
            seed_economy(storage, tmp_path / "data", "LA")
        assert "AU" in str(exc.value) and "SG" in str(exc.value)

    def test_every_bundled_file_is_committed(self):
        """The seed reads these off disk inside the image, so a renamed or
        dropped fixture must fail here and not in front of a judge."""
        for economy, docs in BUNDLED.items():
            for doc in docs:
                assert doc.path.is_file(), f"{economy}: {doc.path} is missing"


class TestTheTestSeedAndTheShippedSeedStayInStep:
    def test_the_test_helper_reads_the_same_bundled_list(self):
        """tests/corpus_fixtures.py used to own this list. It now reads the
        shipped one, so the goldens and the judge's demo can never drift onto
        different bytes."""
        from corpus_fixtures import BUNDLED as TEST_BUNDLED

        assert TEST_BUNDLED is BUNDLED

    def test_the_test_helper_still_marks_its_rows_as_discovered(self, tmp_path):
        """seed_corpus stands in for a Discovery in the existing suite and the
        goldens; the new source kind must not leak into it."""
        from corpus_fixtures import seed_corpus

        storage = Storage(tmp_path / "helper.db")
        storage.apply_schema()
        seed_corpus(storage, tmp_path / "data", "SG")
        assert storage.corpus_documents("SG")[0]["source_kind"] == "discovery"
