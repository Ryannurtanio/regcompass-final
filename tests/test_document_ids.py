"""A Document id is unique per bytes, whatever script the file name is in.

The id is derived from the file name, and the derivation keeps only ASCII
name characters. A file named entirely in Lao or Chinese script therefore
sanitises to nothing and every such file in one Economy used to derive the
same id, so the ingest's upsert let the second file quietly replace the first:
37 files fetched, 36 Corpus rows, no error anywhere. These tests pin the two
halves of the fix. The id carries the bytes' own digest where the readable
name cannot tell two Documents apart, and Discovery counts the rows that
landed rather than the files it read.

Everything here is offline: tiny synthetic documents wired through a real
Storage and the real manifest, the same path tests/test_shortlist.py takes.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from regcompass.shortlist import document_id_for, ingest_economy, resolve_document_id
from regcompass.storage import Storage

# Two Instructions from the Lao official gazette, and two Chinese notices.
# Both pairs sanitise to an empty stem, which is the whole point of them.
LAO_NAMES = ("ຄຳສັ່ງແນະນຳ.pdf", "ຄຳແນະນຳ.pdf")
CHINESE_NAMES = ("网络安全法.pdf", "数据安全法.pdf")


def make_html(title: str, body: str) -> bytes:
    return (
        f"<html><head><title>{title}</title></head>"
        f"<body><p>{body}</p></body></html>"
    ).encode()


def add_fetched_doc(
    storage: Storage,
    data_dir: Path,
    economy: str,
    name: str,
    raw: bytes,
    *,
    url: str | None = None,
) -> str:
    """One synthetic document through the real manifest path. The stored file
    is named by digest, the way the fetch lane names it, so two files whose
    names are identical after sanitising still keep their own bytes on disk."""
    sha = hashlib.sha256(raw).hexdigest()
    url = url or f"https://example.test/{economy}/{name}"
    rel = f"{economy}/raw/{sha[:12]}_{name}"
    path = data_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    storage.manifest_add_pending(url, economy, filename_hint=name)
    storage.manifest_mark_fetched(
        url,
        http_status=200,
        method="httpx",
        sha256=sha,
        content_type="text/html",
        size_bytes=len(raw),
        local_path=rel,
    )
    return url


@pytest.fixture()
def env(tmp_path):
    storage = Storage(tmp_path / "ids.db")
    storage.apply_schema()
    yield storage, tmp_path / "data"
    storage.close()


def ids_in_corpus(storage: Storage, economy: str) -> set[str]:
    return {r["document_id"] for r in storage.documents_for_economy(economy)}


class TestNonLatinNamesKeepTheirOwnRow:
    """The live defect, in both scripts we crawl in."""

    @pytest.mark.parametrize(
        ("economy", "names"), [("LA", LAO_NAMES), ("CN", CHINESE_NAMES)]
    )
    def test_two_files_of_one_sanitised_name_are_two_documents(
        self, env, economy, names
    ):
        storage, data_dir = env
        first = add_fetched_doc(
            storage, data_dir, economy, names[0], make_html("A", "first law")
        )
        second = add_fetched_doc(
            storage, data_dir, economy, names[1], make_html("B", "second law")
        )

        results, excluded = ingest_economy(storage, data_dir, economy)

        assert excluded == []
        ids = [r.document_id for r in results]
        assert len(set(ids)) == 2, "one id for two files is the collision itself"
        rows = storage.documents_for_economy(economy)
        assert len(rows) == 2, "the second file must not replace the first"
        assert {r["source_url"] for r in rows} == {first, second}
        assert {r["full_text"].count("first law") for r in rows} == {0, 1}

    def test_the_id_carries_the_digest_of_its_own_bytes(self, env):
        storage, data_dir = env
        raw = make_html("A", "first law")
        add_fetched_doc(storage, data_dir, "LA", LAO_NAMES[0], raw)

        results, _ = ingest_economy(storage, data_dir, "LA")

        sha = hashlib.sha256(raw).hexdigest()
        assert results[0].document_id == f"doc_la_document_{sha[:12]}"

    def test_a_third_file_of_the_same_name_still_gets_its_own_row(self, env):
        """Ingested one at a time, which is how a resumed Discovery arrives."""
        storage, data_dir = env
        for i, name in enumerate((*LAO_NAMES, "ກົດໝາຍ.pdf")):
            add_fetched_doc(storage, data_dir, "LA", name, make_html("A", f"law {i}"))
            ingest_economy(storage, data_dir, "LA")
        assert len(storage.documents_for_economy("LA")) == 3


class TestReadableIdsAreUnchanged:
    """The AU, MY and SG Corpus and the goldens are keyed on these ids, so the
    digest suffix may only appear where the readable id cannot stand."""

    def test_a_latin_named_file_keeps_its_plain_id(self, env):
        storage, data_dir = env
        add_fetched_doc(
            storage, data_dir, "AU", "C2026C00243VOL01.pdf", make_html("Privacy Act 1988", "x")
        )
        results, _ = ingest_economy(storage, data_dir, "AU")
        assert [r.document_id for r in results] == ["doc_au_C2026C00243VOL01"]

    def test_the_derivation_itself_is_untouched(self):
        row = {
            "economy": "AU",
            "filename_hint": "C2026C00243VOL01.pdf",
            "local_path": "AU/raw/x.pdf",
            "url": "https://x",
        }
        assert document_id_for(row) == "doc_au_C2026C00243VOL01"

    def test_a_partly_latin_name_keeps_the_readable_part(self, env):
        storage, data_dir = env
        add_fetched_doc(
            storage, data_dir, "LA", "ກົດໝາຍ Law 33 2024.pdf", make_html("Law 33", "x")
        )
        results, _ = ingest_economy(storage, data_dir, "LA")
        assert [r.document_id for r in results] == ["doc_la_Law_33_2024"]

    def test_two_different_files_of_one_readable_name_are_told_apart(self, env):
        """Same name, different bytes, different addresses: the second is a
        different Document and may not land on the first one's row."""
        storage, data_dir = env
        add_fetched_doc(
            storage, data_dir, "SG", "act.html", make_html("A", "first"),
            url="https://example.test/SG/one/act.html",
        )
        add_fetched_doc(
            storage, data_dir, "SG", "act.html", make_html("B", "second"),
            url="https://example.test/SG/two/act.html",
        )
        results, excluded = ingest_economy(storage, data_dir, "SG")
        assert excluded == []
        assert len({r.document_id for r in results}) == 2
        assert len(storage.documents_for_economy("SG")) == 2
        assert "doc_sg_act" in ids_in_corpus(storage, "SG"), "the first id stands"


class TestReIngestAndRefresh:
    def test_a_re_ingest_of_known_bytes_is_a_no_op(self, env):
        storage, data_dir = env
        for name in LAO_NAMES:
            add_fetched_doc(storage, data_dir, "LA", name, make_html("A", name))

        first, _ = ingest_economy(storage, data_dir, "LA")
        before = ids_in_corpus(storage, "LA")
        second, _ = ingest_economy(storage, data_dir, "LA")

        assert len(first) == 2 and second == []
        assert ids_in_corpus(storage, "LA") == before

    def test_a_refresh_replaces_the_document_rather_than_adding_one(self, env):
        """A refresh re-fetches the SAME address and the bytes change. That is
        not a collision: the Document keeps its id and its single row."""
        storage, data_dir = env
        url = add_fetched_doc(storage, data_dir, "SG", "act.html", make_html("A", "old text"))
        ingest_economy(storage, data_dir, "SG")

        new_raw = make_html("A", "amended text")
        add_fetched_doc(storage, data_dir, "SG", "act.html", new_raw, url=url)
        results, _ = ingest_economy(storage, data_dir, "SG")

        rows = storage.documents_for_economy("SG")
        assert [r.document_id for r in results] == ["doc_sg_act"]
        assert len(rows) == 1 and "amended text" in rows[0]["full_text"]

    def test_a_refreshed_non_latin_document_keeps_its_id(self, env):
        storage, data_dir = env
        url = add_fetched_doc(storage, data_dir, "LA", LAO_NAMES[0], make_html("A", "old"))
        first, _ = ingest_economy(storage, data_dir, "LA")

        add_fetched_doc(storage, data_dir, "LA", LAO_NAMES[0], make_html("A", "new"), url=url)
        second, _ = ingest_economy(storage, data_dir, "LA")

        assert [r.document_id for r in second] == [first[0].document_id]
        assert len(storage.documents_for_economy("LA")) == 1

    def test_refreshing_one_of_two_non_latin_documents_leaves_the_other_alone(self, env):
        storage, data_dir = env
        urls = [
            add_fetched_doc(storage, data_dir, "LA", name, make_html("A", name))
            for name in LAO_NAMES
        ]
        ingest_economy(storage, data_dir, "LA")
        before = ids_in_corpus(storage, "LA")

        add_fetched_doc(
            storage, data_dir, "LA", LAO_NAMES[0], make_html("A", "amended"), url=urls[0]
        )
        ingest_economy(storage, data_dir, "LA")

        assert ids_in_corpus(storage, "LA") == before
        assert len(storage.documents_for_economy("LA")) == 2


class TestResolverIsHonestAboutWhatItReads:
    def test_it_answers_the_plain_id_when_nothing_holds_it(self, env):
        storage, data_dir = env
        url = add_fetched_doc(storage, data_dir, "SG", "act.html", make_html("A", "x"))
        row = storage.manifest_get(url)
        assert resolve_document_id(storage, row) == "doc_sg_act"

    def test_it_is_stable_across_calls(self, env):
        storage, data_dir = env
        url = add_fetched_doc(storage, data_dir, "LA", LAO_NAMES[0], make_html("A", "x"))
        row = storage.manifest_get(url)
        assert resolve_document_id(storage, row) == resolve_document_id(storage, row)
