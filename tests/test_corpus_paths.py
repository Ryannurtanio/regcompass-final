"""A Corpus row must survive the data folder moving.

The defect this file exists for: the working database built here in July held
39 rows whose stored path was this laptop's absolute path, and one whose path
carried a stray `data/` prefix. Inside the image, or on any other machine,
every Run over those rows would have failed with the file missing, and one did
on 23 September 2026. Two things have to be true for that to stay fixed.

Every writer records `<ECONOMY>/raw/<file>`, relative to the data folder, even
when the data folder is handed to it as an absolute path (which is how the
pre-run job passes it). And every reader forgives a row already written the
wrong way: it looks for the same BYTES under the data folder, verifies the
digest the row carries, and only then uses what it found.

Nothing here touches the network or spends anything.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

from regcompass.corpus import add_document
from regcompass.engines import fake_completion, fake_embed, resolve_engine
from regcompass.fixtures import BUNDLED, seed_economy
from regcompass.paths import (
    AS_STORED,
    BY_NAME,
    WITHOUT_DATA_PREFIX,
    resolve_stored_path,
    storable_local_path,
)
from regcompass.pipeline import corpus_document_path, corpus_for_run, run_economy
from regcompass.storage import Storage

from corpus_fixtures import seed_corpus  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import pre_run  # noqa: E402

FAKE_ENGINE = resolve_engine("fake")

# A path from a machine that is not this one. It never exists here, which is
# the whole point: it is what the shipped database actually held.
OTHER_MACHINE = "/Users/someone-else/work/regcompass/data"


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def put(data_dir: Path, economy: str, name: str, raw: bytes) -> Path:
    """Bytes where a Discovery would have left them."""
    raw_dir = data_dir / economy / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / name
    path.write_bytes(raw)
    return path


def row(**fields) -> dict:
    """A documents row as the resolver reads it."""
    base = {
        "document_id": "doc_sg_thing",
        "economy": "SG",
        "local_path": "SG/raw/thing.pdf",
        "source_sha256": None,
    }
    base.update(fields)
    return base


def stored_paths(storage: Storage, economy: str) -> list[str]:
    return [r["local_path"] for r in storage.corpus_documents(economy)]


# ---------------------------------------------------------------------------
# the spelling a row records
# ---------------------------------------------------------------------------


class TestWhatARowWritesDown:
    def test_a_file_under_the_data_folder_is_written_relative(self, tmp_path):
        data_dir = (tmp_path / "data").resolve()
        path = put(data_dir, "SG", "act.pdf", b"bytes")
        assert storable_local_path(path, data_dir) == "SG/raw/act.pdf"

    def test_a_relative_data_folder_gives_the_same_answer(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        put(tmp_path / "data", "SG", "act.pdf", b"bytes")
        assert storable_local_path(Path("data/SG/raw/act.pdf"), Path("data")) == (
            "SG/raw/act.pdf"
        )

    def test_the_two_spellings_mixed_still_reduce(self, tmp_path, monkeypatch):
        """An absolute file beside a relative data folder, which is how a job
        started from the project root with a resolved --data-dir looked."""
        monkeypatch.chdir(tmp_path)
        path = put(tmp_path / "data", "SG", "act.pdf", b"bytes")
        assert storable_local_path(path, Path("data")) == "SG/raw/act.pdf"

    def test_a_file_outside_the_data_folder_keeps_its_own_path(self, tmp_path):
        outside = tmp_path / "downloads" / "one.pdf"
        outside.parent.mkdir()
        outside.write_bytes(b"bytes")
        assert storable_local_path(outside, tmp_path / "data") == outside.as_posix()


# ---------------------------------------------------------------------------
# reading a row back
# ---------------------------------------------------------------------------


class TestReadingAStoredPathBack:
    def test_the_ordinary_relative_row_opens_as_stored(self, tmp_path):
        data_dir = tmp_path / "data"
        path = put(data_dir, "SG", "act.pdf", b"bytes")
        found = resolve_stored_path("SG/raw/act.pdf", data_dir, economy="SG")
        assert found is not None
        assert found.path == path and found.form == AS_STORED
        assert not found.repaired

    def test_another_machines_absolute_path_resolves_under_the_data_folder(
        self, tmp_path
    ):
        data_dir = tmp_path / "data"
        raw = b"the act as published"
        path = put(data_dir, "AU", "abc123_act.pdf", raw)
        found = resolve_stored_path(
            f"{OTHER_MACHINE}/AU/raw/abc123_act.pdf",
            data_dir, economy="AU", sha256=sha(raw),
        )
        assert found is not None
        assert found.path == path and found.form == BY_NAME
        assert found.repaired

    def test_a_stray_data_prefix_resolves(self, tmp_path):
        data_dir = tmp_path / "data"
        raw = b"the act as published"
        path = put(data_dir, "SG", "abc123_act.pdf", raw)
        found = resolve_stored_path(
            "data/SG/raw/abc123_act.pdf", data_dir, economy="SG", sha256=sha(raw)
        )
        assert found is not None and found.path == path
        assert found.form in (BY_NAME, WITHOUT_DATA_PREFIX)
        assert found.repaired

    def test_a_stray_data_prefix_resolves_without_an_economy_to_help(self, tmp_path):
        data_dir = tmp_path / "data"
        raw = b"the act as published"
        path = put(data_dir, "SG", "abc123_act.pdf", raw)
        found = resolve_stored_path("data/SG/raw/abc123_act.pdf", data_dir, sha256=sha(raw))
        assert found is not None
        assert found.path == path and found.form == WITHOUT_DATA_PREFIX

    def test_a_file_of_the_right_name_and_the_wrong_bytes_is_refused(self, tmp_path):
        """The digest is the Document's identity. A namesake is not it."""
        data_dir = tmp_path / "data"
        put(data_dir, "AU", "abc123_act.pdf", b"some other document entirely")
        assert resolve_stored_path(
            f"{OTHER_MACHINE}/AU/raw/abc123_act.pdf",
            data_dir, economy="AU", sha256=sha(b"the act as published"),
        ) is None

    def test_the_rows_own_path_with_the_wrong_bytes_is_not_trusted_either(
        self, tmp_path
    ):
        """A swapped file at the stored place is a namesake too. The digest
        decides, and the by-name place holding the real bytes wins."""
        data_dir = tmp_path / "data"
        raw = b"the act as published"
        swapped = tmp_path / "elsewhere" / "act.pdf"
        swapped.parent.mkdir()
        swapped.write_bytes(b"some other document entirely")
        real = put(data_dir, "AU", "act.pdf", raw)
        found = resolve_stored_path(swapped, data_dir, economy="AU", sha256=sha(raw))
        assert found is not None
        assert found.path == real and found.form == BY_NAME

    def test_wrong_bytes_everywhere_is_refused(self, tmp_path):
        data_dir = tmp_path / "data"
        put(data_dir, "AU", "act.pdf", b"some other document entirely")
        assert resolve_stored_path(
            "AU/raw/act.pdf", data_dir, economy="AU", sha256=sha(b"the act as published")
        ) is None

    def test_nothing_on_disk_at_all_is_not_resolved(self, tmp_path):
        assert resolve_stored_path("SG/raw/gone.pdf", tmp_path / "data", economy="SG") is None

    def test_a_path_outside_the_data_folder_that_is_really_there_still_opens(
        self, tmp_path
    ):
        outside = tmp_path / "elsewhere" / "one.pdf"
        outside.parent.mkdir()
        outside.write_bytes(b"bytes")
        found = resolve_stored_path(outside, tmp_path / "data", economy="SG")
        assert found is not None and found.path == outside


class TestTheCorpusResolverSpeaks:
    def test_a_repair_is_announced_with_the_form_it_was_found_in(self, tmp_path):
        data_dir = tmp_path / "data"
        raw = b"the act as published"
        put(data_dir, "AU", "abc123_act.pdf", raw)
        lines: list[str] = []
        path = corpus_document_path(
            row(
                document_id="doc_au_act", economy="AU",
                local_path=f"{OTHER_MACHINE}/AU/raw/abc123_act.pdf",
                source_sha256=sha(raw),
            ),
            data_dir, lines.append,
        )
        assert path.is_file()
        assert len(lines) == 1
        assert "doc_au_act" in lines[0] and BY_NAME in lines[0]
        assert "digest verified" in lines[0]

    def test_an_ordinary_row_says_nothing(self, tmp_path):
        data_dir = tmp_path / "data"
        put(data_dir, "SG", "act.pdf", b"bytes")
        lines: list[str] = []
        corpus_document_path(row(local_path="SG/raw/act.pdf"), data_dir, lines.append)
        assert lines == []

    def test_the_refusal_names_every_place_it_looked(self, tmp_path):
        data_dir = tmp_path / "data"
        with pytest.raises(RuntimeError) as exc:
            corpus_document_path(
                row(
                    document_id="doc_au_gone", economy="AU",
                    local_path=f"{OTHER_MACHINE}/AU/raw/gone.pdf",
                ),
                data_dir,
            )
        message = str(exc.value)
        assert "Looked in:" in message
        assert f"{OTHER_MACHINE}/AU/raw/gone.pdf" in message
        assert str(data_dir / "AU" / "raw" / "gone.pdf") in message


# ---------------------------------------------------------------------------
# every writer, with the data folder passed absolute
# ---------------------------------------------------------------------------


class TestEveryWriterStoresTheRelativeSpelling:
    def test_an_upload_stores_economy_raw_file(self, tmp_path):
        data_dir = (tmp_path / "data").resolve()
        storage = Storage(tmp_path / "upload.db")
        storage.apply_schema()
        doc = BUNDLED["SG"][0]
        add_document(
            storage, data_dir, "SG", doc.path.read_bytes(),
            source_url=doc.source_url, filename_hint=doc.filename_hint,
        )
        paths = stored_paths(storage, "SG")
        assert paths and all(p.startswith("SG/raw/") for p in paths), paths
        assert all(not Path(p).is_absolute() for p in paths)

    def test_a_seed_stores_economy_raw_file(self, tmp_path):
        data_dir = (tmp_path / "data").resolve()
        storage = Storage(tmp_path / "seed.db")
        storage.apply_schema()
        result = seed_economy(storage, data_dir, "AU")
        assert result.added
        paths = stored_paths(storage, "AU")
        assert paths and all(p.startswith("AU/raw/") for p in paths), paths

    def test_the_manifest_rows_are_relative_too(self, tmp_path):
        """The Corpus row is copied from the manifest row, so a manifest row
        holding an absolute path would put one back into the Corpus."""
        data_dir = (tmp_path / "data").resolve()
        storage = Storage(tmp_path / "manifest.db")
        storage.apply_schema()
        seed_economy(storage, data_dir, "MY")
        stored = [
            r["local_path"] for r in storage.manifest_rows(economy="MY")
            if r["local_path"]
        ]
        assert stored and all(p.startswith("MY/raw/") for p in stored), stored

    def test_a_run_does_not_rewrite_the_rows_absolute(self, tmp_path):
        """The regression itself. A Run used to write back the path it had just
        resolved, which turned every relative row it read into a local one."""
        data_dir = (tmp_path / "data").resolve()
        storage = Storage(tmp_path / "run.db")
        storage.apply_schema()
        seed_corpus(storage, data_dir, "MY")
        before = stored_paths(storage, "MY")
        run_economy(
            storage, "MY", (7,), FAKE_ENGINE, data_dir=data_dir,
            completion_fn=fake_completion, embed_fn=fake_embed,
        )
        after = stored_paths(storage, "MY")
        assert after == before
        assert all(p.startswith("MY/raw/") for p in after), after

    def test_a_run_repairs_a_row_that_arrived_absolute(self, tmp_path):
        """A database that already holds the bad spelling heals as it is read,
        so the next machine to open it finds rows that travel."""
        data_dir = (tmp_path / "data").resolve()
        storage = Storage(tmp_path / "heal.db")
        storage.apply_schema()
        seed_corpus(storage, data_dir, "MY")
        for stored in stored_paths(storage, "MY"):
            storage.conn.execute(
                "UPDATE documents SET local_path = ? WHERE local_path = ?",
                (f"{OTHER_MACHINE}/{stored}", stored),
            )
        storage.conn.commit()
        assert all(p.startswith(OTHER_MACHINE) for p in stored_paths(storage, "MY"))

        lines: list[str] = []
        run_economy(
            storage, "MY", (7,), FAKE_ENGINE, data_dir=data_dir,
            completion_fn=fake_completion, embed_fn=fake_embed,
            progress=lines.append,
        )
        after = stored_paths(storage, "MY")
        assert all(p.startswith("MY/raw/") for p in after), after
        assert any("does not open here" in line for line in lines)

    def test_a_run_reads_a_row_with_a_stray_data_prefix(self, tmp_path):
        data_dir = (tmp_path / "data").resolve()
        storage = Storage(tmp_path / "prefix.db")
        storage.apply_schema()
        seed_corpus(storage, data_dir, "MY")
        for stored in stored_paths(storage, "MY"):
            storage.conn.execute(
                "UPDATE documents SET local_path = ? WHERE local_path = ?",
                (f"data/{stored}", stored),
            )
        storage.conn.commit()
        corpus = corpus_for_run(storage, "MY", data_dir)
        assert corpus and all(path.is_file() for _, path in corpus)


class TestDiscoveryStoresTheRelativeSpelling:
    def test_a_discovery_over_recorded_answers_stores_economy_raw_file(self, tmp_path):
        pytest.importorskip(
            "httpx", reason="the M10 crawl lane needs the `live` extra (httpx)"
        )
        from regcompass.contracts import CrawlFamily, CrawlSeedsEconomy
        from regcompass.crawl import FetchResult, RateLimiter, crawl_economy

        class NullLimiter(RateLimiter):
            def __init__(self):
                super().__init__(0.0, clock=lambda: 0.0, sleep=lambda s: None)

        seeds = CrawlSeedsEconomy(
            rate_limit_seconds=0.01,
            families={"telecommunications": CrawlFamily(acts=["TA1999"])},
        )

        def fetch(url: str) -> FetchResult:
            return FetchResult(
                url=url, final_url=url, http_status=200, content=b"<html>TA</html>",
                content_type="application/pdf", method="httpx",
            )

        data_dir = (tmp_path / "data").resolve()
        storage = Storage(tmp_path / "crawl.db")
        storage.apply_schema()
        report = crawl_economy(
            "SG", storage, data_dir, seeds, fetcher=fetch, limiter=NullLimiter()
        )
        assert report.fetched
        stored = [
            r["local_path"] for r in storage.manifest_rows(economy="SG") if r["local_path"]
        ]
        assert stored and all(p.startswith("SG/raw/") for p in stored), stored

    def test_an_add_by_url_stores_economy_raw_file(self, tmp_path):
        pytest.importorskip(
            "httpx", reason="the add-by-URL lane reaches crawl.fetch_one (httpx)"
        )
        from regcompass.corpus import add_document_from_url
        from regcompass.crawl import FetchResult, RateLimiter, parse_robots

        class NullLimiter(RateLimiter):
            def __init__(self):
                super().__init__(0.0, clock=lambda: 0.0, sleep=lambda s: None)

        doc = BUNDLED["SG"][0]
        raw = doc.path.read_bytes()

        def fetch(url: str) -> FetchResult:
            return FetchResult(
                url=url, final_url=url, http_status=200, content=raw,
                content_type="application/pdf", method="httpx",
            )

        data_dir = (tmp_path / "data").resolve()
        storage = Storage(tmp_path / "by_url.db")
        storage.apply_schema()
        add_document_from_url(
            storage, data_dir, "SG", doc.source_url,
            fetch=fetch, limiter=NullLimiter(),
            robots=parse_robots("User-agent: *\n"),
        )
        paths = stored_paths(storage, "SG")
        assert paths and all(p.startswith("SG/raw/") for p in paths), paths


# ---------------------------------------------------------------------------
# the check before the image ships
# ---------------------------------------------------------------------------


class TestTheOperatorsCheck:
    def test_a_corpus_that_resolves_reports_nothing(self, tmp_path):
        data_dir = (tmp_path / "data").resolve()
        db = tmp_path / "ok.db"
        storage = Storage(db)
        storage.apply_schema()
        seed_corpus(storage, data_dir, "SG")
        storage.conn.close()
        assert pre_run.missing_corpus_files(db, ("SG",), data_dir) == []

    def test_a_row_whose_file_is_gone_is_named_with_its_economy_and_id(self, tmp_path):
        data_dir = (tmp_path / "data").resolve()
        db = tmp_path / "gone.db"
        storage = Storage(db)
        storage.apply_schema()
        seed_corpus(storage, data_dir, "SG")
        stored = stored_paths(storage, "SG")[0]
        (data_dir / stored).unlink()
        storage.conn.close()

        missing = pre_run.missing_corpus_files(db, ("SG",), data_dir)
        assert len(missing) == 1
        assert missing[0].economy == "SG"
        assert missing[0].document_id.startswith("doc_sg_")
        assert missing[0].stored == stored
        assert "doc_sg_" in str(missing[0])

    def test_a_row_the_run_would_repair_is_not_reported(self, tmp_path):
        data_dir = (tmp_path / "data").resolve()
        db = tmp_path / "repairable.db"
        storage = Storage(db)
        storage.apply_schema()
        seed_corpus(storage, data_dir, "SG")
        for stored in stored_paths(storage, "SG"):
            storage.conn.execute(
                "UPDATE documents SET local_path = ? WHERE local_path = ?",
                (f"{OTHER_MACHINE}/{stored}", stored),
            )
        storage.conn.commit()
        storage.conn.close()
        assert pre_run.missing_corpus_files(db, ("SG",), data_dir) == []

    def test_another_economys_broken_row_is_not_this_jobs_problem(self, tmp_path):
        data_dir = (tmp_path / "data").resolve()
        db = tmp_path / "other.db"
        storage = Storage(db)
        storage.apply_schema()
        seed_corpus(storage, data_dir, "SG")
        (data_dir / stored_paths(storage, "SG")[0]).unlink()
        storage.conn.close()
        assert pre_run.missing_corpus_files(db, ("MY",), data_dir) == []

    def test_the_dry_run_prints_the_missing_rows_and_runs_nothing(
        self, tmp_path, capsys
    ):
        data_dir = (tmp_path / "data").resolve()
        db = tmp_path / "dry.db"
        storage = Storage(db)
        storage.apply_schema()
        seed_corpus(storage, data_dir, "SG")
        doc_id = stored_paths(storage, "SG")[0]
        (data_dir / doc_id).unlink()
        storage.conn.close()

        def never(*args, **kwargs):  # pragma: no cover - the point is it is not called
            raise AssertionError("a dry run must run nothing")

        code = pre_run.main(
            [
                "--dry-run", "--ceiling-usd", "1", "--engines", "fake",
                "--pillars", "7", "--economies", "SG", "--db", str(db),
                "--data-dir", str(data_dir), "--ledger", str(tmp_path / "ledger.tsv"),
            ],
            execute=never,
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "CORPUS FILES MISSING" in out
        assert "doc_sg_" in out
        assert "--dry-run: nothing was run." in out

    def test_the_dry_run_says_so_when_everything_opens(self, tmp_path, capsys):
        data_dir = (tmp_path / "data").resolve()
        db = tmp_path / "clean.db"
        storage = Storage(db)
        storage.apply_schema()
        seed_corpus(storage, data_dir, "SG")
        storage.conn.close()

        code = pre_run.main(
            [
                "--dry-run", "--ceiling-usd", "1", "--engines", "fake",
                "--pillars", "7", "--economies", "SG", "--db", str(db),
                "--data-dir", str(data_dir), "--ledger", str(tmp_path / "ledger.tsv"),
            ],
            execute=lambda *a, **k: 0,
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "every Document of the planned Economies opens" in out
        assert "CORPUS FILES MISSING" not in out
