"""A Run reads the Corpus and never fetches.

The proof is not a comment: a network guard makes every outbound socket raise
for the length of the Run, and the Run still produces Mappings. That is what
makes "Documents fetched: 0" on the second Engine's Run Record a checkable
fact rather than something a judge has to take on trust.

Deliberately free of httpx: this file must run on a base-tier install too, so
the zero-fetch property is proved on every install, not only where the live
extra is present.
"""

from __future__ import annotations

import socket
import sys

import pytest

from regcompass.engines import fake_completion, fake_embed, resolve_engine
from regcompass.pipeline import EmptyCorpusError, run_economy
from regcompass.storage import Storage

from corpus_fixtures import seed_corpus  # noqa: E402

FAKE_ENGINE = resolve_engine("fake")


# The modules that ARE the network lane. A Run that never imports them cannot
# have fetched anything, whatever the sockets say.
NETWORK_MODULES = ("regcompass.crawl", "regcompass.discovery")


@pytest.fixture()
def no_network(monkeypatch):
    """Every outbound connection raises, AND importing the network lane raises.
    httpx, curl_cffi and playwright all reach the network through these socket
    entry points, so a Run that completes under this guard made no request by
    any route; the import blocker closes the question one step earlier, at
    whether the Run can even reach the code that would make one."""

    def refuse(*args, **kwargs):
        raise AssertionError("a Run must not touch the network")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)

    class RefuseTheNetworkLane:
        def find_module(self, name, path=None):  # pragma: no cover - legacy hook
            return None

        def find_spec(self, name, path=None, target=None):
            if name in NETWORK_MODULES:
                raise AssertionError(f"a Run must not import {name}")
            return None

    # Another test may have imported the lane already, so evict it for the
    # duration: without that, an import inside the Run would be served from the
    # cache and the blocker would never fire.
    for name in NETWORK_MODULES:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(sys, "meta_path", [RefuseTheNetworkLane(), *sys.meta_path])
    return refuse


@pytest.fixture(scope="module")
def sg_corpus(tmp_path_factory):
    """One SG Corpus, seeded once, shared by the Run tests in this module."""
    root = tmp_path_factory.mktemp("corpus")
    storage = Storage(root / "corpus.db")
    storage.apply_schema()
    doc_ids = seed_corpus(storage, root / "data", "SG")
    return storage, root / "data", doc_ids


class TestTheCorpusIsWhatARunReads:
    def test_the_seeded_corpus_carries_language_and_source_url(self, sg_corpus):
        storage, _, doc_ids = sg_corpus
        rows = storage.corpus_documents("SG")
        assert [r["document_id"] for r in rows] == doc_ids
        assert rows[0]["language"] == "English"
        assert rows[0]["source_url"].startswith("https://sso.agc.gov.sg/")

    def test_a_run_over_the_corpus_produces_mappings_with_no_network(
        self, sg_corpus, tmp_path, no_network
    ):
        storage, data_dir, doc_ids = sg_corpus
        report = run_economy(
            storage, "SG", (7,), FAKE_ENGINE, data_dir=data_dir,
            completion_fn=fake_completion, embed_fn=fake_embed,
        )
        assert report.documents == doc_ids
        assert report.n_passed > 0, "the verbatim-quote lane must pass"
        assert storage.load_mappings(economy="SG", verification_status="passed")

    def test_the_run_reads_the_corpus_not_a_bundled_fixture_list(self, tmp_path):
        """A Corpus of one Document maps that one Document. Nothing is added
        from a list baked into the code."""
        storage = Storage(tmp_path / "one.db")
        storage.apply_schema()
        seed_corpus(storage, tmp_path / "data", "MY")
        report = run_economy(
            storage, "MY", (7,), FAKE_ENGINE, data_dir=tmp_path / "data",
            completion_fn=fake_completion, embed_fn=fake_embed,
        )
        assert report.documents == ["doc_my_personal_data_protection_act_2010"]


class TestAnEmptyCorpusSaysWhatToDo:
    def test_a_run_on_an_empty_corpus_names_the_economy_and_the_next_command(
        self, tmp_path
    ):
        storage = Storage(tmp_path / "empty.db")
        storage.apply_schema()
        with pytest.raises(EmptyCorpusError) as exc:
            run_economy(
                storage, "SG", (7,), FAKE_ENGINE, data_dir=tmp_path / "data",
                completion_fn=fake_completion, embed_fn=fake_embed,
            )
        message = str(exc.value)
        assert "no Documents in the Corpus for Singapore" in message
        assert "regcompass discover --economy SG" in message
        assert exc.value.economy == "SG"

    def test_a_thin_document_row_is_not_a_corpus(self, tmp_path):
        """A row written only so chunks could attach is not a Document: the
        Run must still say the Corpus is empty rather than map nothing."""
        storage = Storage(tmp_path / "thin.db")
        storage.apply_schema()
        storage.upsert_document("doc_sg_thin", "SG", "a" * 64)
        with pytest.raises(EmptyCorpusError):
            run_economy(
                storage, "SG", (7,), FAKE_ENGINE, data_dir=tmp_path / "data",
                completion_fn=fake_completion, embed_fn=fake_embed,
            )


class TestBytesMustBeWhereTheManifestSaid:
    def test_a_database_reused_with_another_data_dir_says_so(self, tmp_path):
        storage = Storage(tmp_path / "moved.db")
        storage.apply_schema()
        seed_corpus(storage, tmp_path / "data", "SG")
        with pytest.raises(RuntimeError, match="--data-dir"):
            run_economy(
                storage, "SG", (7,), FAKE_ENGINE, data_dir=tmp_path / "elsewhere",
                completion_fn=fake_completion, embed_fn=fake_embed,
            )
