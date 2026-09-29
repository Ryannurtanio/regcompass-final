"""The prepared database: one download that fills an empty data folder.

A release ships its pre-run Runs as a separate file beside the code, because
the database and the Corpus bytes it points at are several hundred megabytes
and neither git nor the image build carries them. The file is a gzip tarball
laid out exactly like a data folder:

    regcompass.db             the working database, one self-contained file
    prepared-data.json        what is inside: Economies, Runs, counts, hashes
    <ECONOMY>/raw/<file>      the exact bytes each Document was read from

`scripts/pack_prepared_data.py` makes it; `regcompass load-data` (this module)
puts it in place. Loading is three promises, in order:

1. The bytes are the published bytes. The download is hashed as it streams
   and compared with the SHA-256 the release names, before a single member is
   read. A mismatch deletes the download and unpacks nothing.
2. The archive can only write where a data folder is written: the database,
   the manifest and `<ECONOMY>/raw/<file>`. Any other member (an absolute
   path, a `..`, a link, a device) refuses the whole archive.
3. Nothing of yours is overwritten silently. A database that already holds a
   Document or a Run, or a stored file with other bytes under the same name,
   refuses the load unless `force` is passed. A database with no rows at all
   (what the server leaves behind on its first start) is replaced, because
   there is nothing in it to lose.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable

import yaml

from regcompass.config import CONFIG_DIR

__all__ = [
    "ARCHIVE_DB_NAME",
    "BUNDLE_FORMAT",
    "CONFIG_FILE",
    "MANIFEST_NAME",
    "LoadReport",
    "PreparedDataError",
    "PreparedSource",
    "archive_member_kind",
    "configured_source",
    "load_prepared_data",
    "sha256_file",
]

ARCHIVE_DB_NAME = "regcompass.db"
MANIFEST_NAME = "prepared-data.json"
BUNDLE_FORMAT = "regcompass-prepared-data/1"
CONFIG_FILE = "prepared_data.yaml"

# `<ECONOMY>/raw/<file>`: the one place a Document's bytes live under a data
# folder (regcompass.paths). The Economy code is upper case letters, the file
# name a single path segment.
_RAW_MEMBER = re.compile(r"^[A-Z]{2,3}/raw/[^/]+$")
_DIR_MEMBER = re.compile(r"^[A-Z]{2,3}(/raw)?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CHUNK = 1 << 20


class PreparedDataError(RuntimeError):
    """A load that stopped before it changed anything a reviewer would miss.
    The message says what was wrong and what to do about it."""


@dataclass(frozen=True)
class PreparedSource:
    """Where the prepared database is published, and the digest it must have."""

    url: str
    sha256: str


@dataclass
class LoadReport:
    """What a load put in place."""

    db_path: Path
    data_dir: Path
    sha256: str
    archive_bytes: int
    raw_files_written: int
    raw_files_already_present: int
    documents: int
    runs: int
    replaced_database: bool
    manifest: dict = field(default_factory=dict)


def sha256_file(path: Path) -> str:
    sha = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(_CHUNK), b""):
            sha.update(block)
    return sha.hexdigest()


def configured_source(config_dir: Path | None = None) -> PreparedSource | None:
    """The address and digest a release writes into config/prepared_data.yaml,
    or None while either is still blank (a checkout between releases)."""
    path = Path(config_dir if config_dir is not None else CONFIG_DIR) / CONFIG_FILE
    if not path.is_file():
        return None
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    url = str(raw.get("url") or "").strip()
    sha = str(raw.get("sha256") or "").strip().lower()
    if not url or not sha:
        return None
    return PreparedSource(url=url, sha256=sha)


def archive_member_kind(name: str) -> str | None:
    """'db', 'manifest', 'raw' or 'dir' for a member a data folder may hold,
    None for anything else. Names are read as POSIX paths; a leading `./` (what
    `tar -C dir .` writes) is allowed and dropped."""
    posix = PurePosixPath(name)
    if posix.is_absolute() or ".." in posix.parts or "\\" in name:
        return None
    parts = [p for p in posix.parts if p != "."]
    if not parts:
        return "dir"
    clean = "/".join(parts)
    if clean == ARCHIVE_DB_NAME:
        return "db"
    if clean == MANIFEST_NAME:
        return "manifest"
    if _RAW_MEMBER.match(clean):
        return "raw"
    if _DIR_MEMBER.match(clean):
        return "dir"
    return None


def _clean_name(name: str) -> str:
    return "/".join(p for p in PurePosixPath(name).parts if p != ".")


def _open_url(url: str):
    """A readable stream for an http(s) or file URL, or for a plain local path
    (so a maintainer can load a bundle they just packed without a server)."""
    if "://" not in url:
        return Path(url).expanduser().open("rb")
    return urllib.request.urlopen(url, timeout=60)  # noqa: S310 - the URL is the operator's


def _download(
    url: str, into: Path, progress: Callable[[str], None]
) -> tuple[Path, str, int]:
    """Stream the file into a temporary file under `into`, hashing as it goes."""
    handle, tmp_name = tempfile.mkstemp(prefix=".prepared-download-", dir=into)
    tmp = Path(tmp_name)
    sha = hashlib.sha256()
    total = 0
    try:
        with os.fdopen(handle, "wb") as sink, _open_url(url) as source:
            length = getattr(source, "length", None) or 0
            next_report = 0.1
            for block in iter(lambda: source.read(_CHUNK), b""):
                sink.write(block)
                sha.update(block)
                total += len(block)
                if length and total / length >= next_report:
                    progress(f"  downloaded {total / 1e6:.0f} of {length / 1e6:.0f} MB")
                    next_report += 0.1
    except BaseException as e:
        tmp.unlink(missing_ok=True)
        if isinstance(e, (OSError, ValueError)):
            raise PreparedDataError(f"could not download {url}: {e}") from e
        raise
    return tmp, sha.hexdigest(), total


def _db_holds_rows(db_path: Path) -> bool:
    """Does this database hold a Document or a Run? An unreadable file counts
    as holding something: it is not ours to throw away."""
    if not db_path.exists():
        return False
    try:
        conn = sqlite3.connect(db_path)
        try:
            tables = {
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            for table in ("documents", "runs", "mappings"):
                if table in tables and conn.execute(
                    f"SELECT 1 FROM {table} LIMIT 1"
                ).fetchone():
                    return True
            return False
        finally:
            conn.close()
    except sqlite3.Error:
        return True


def _members(archive: tarfile.TarFile) -> list[tuple[tarfile.TarInfo, str]]:
    out: list[tuple[tarfile.TarInfo, str]] = []
    bad: list[str] = []
    for info in archive.getmembers():
        kind = archive_member_kind(info.name)
        if kind is None:
            bad.append(info.name)
        elif kind == "dir" and not info.isdir():
            bad.append(info.name)
        elif kind != "dir" and not info.isfile():
            bad.append(info.name)
        else:
            out.append((info, kind))
    if bad:
        shown = ", ".join(bad[:5]) + (" ..." if len(bad) > 5 else "")
        raise PreparedDataError(
            "the archive holds entries a data folder never holds, so nothing was"
            f" unpacked: {shown}"
        )
    kinds = [k for _, k in out]
    if kinds.count("db") != 1 or kinds.count("manifest") != 1:
        raise PreparedDataError(
            f"the archive must hold exactly one {ARCHIVE_DB_NAME} and one"
            f" {MANIFEST_NAME}; nothing was unpacked"
        )
    return out


def load_prepared_data(
    url: str,
    sha256: str,
    *,
    db_path: Path,
    data_dir: Path,
    force: bool = False,
    progress: Callable[[str], None] = lambda _msg: None,
) -> LoadReport:
    """Download, verify, and unpack the prepared database into a data folder.

    `db_path` is where the working database goes (the server's --db) and
    `data_dir` the root the Corpus bytes go under (its --data-dir); the two
    are separate because the command line lets them be. Every refusal happens
    before anything under `data_dir` is changed, and the download itself is
    removed on every path out."""
    expected = sha256.strip().lower()
    if not _SHA256.match(expected):
        raise PreparedDataError(
            f"'{sha256}' is not a SHA-256 (64 hexadecimal characters); copy it"
            " from the release notes"
        )
    db_path = Path(db_path)
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    replacing = db_path.exists()
    if replacing and _db_holds_rows(db_path) and not force:
        raise PreparedDataError(
            f"{db_path} already holds Documents or Runs. Loading would replace"
            " them, so nothing was downloaded. Pass --force to replace it, or"
            " point --db at another file."
        )

    progress(f"downloading {url}")
    download, actual, size = _download(url, data_dir, progress)
    staging: Path | None = None
    try:
        if actual != expected:
            raise PreparedDataError(
                f"checksum mismatch: expected {expected}, the download is {actual}."
                " This is not the published file (a partial download, or another"
                " release), so nothing was unpacked. Download it again, or check"
                " the SHA-256 against the release notes."
            )
        progress(f"SHA-256 verified ({size / 1e6:.0f} MB)")
        try:
            archive = tarfile.open(download, "r:*")
        except tarfile.TarError as e:
            raise PreparedDataError(f"the download is not a tar archive: {e}") from e
        with archive:
            members = _members(archive)
            raws = [(info, _clean_name(info.name)) for info, kind in members if kind == "raw"]
            clashes = []
            already = set()
            for info, name in raws:
                target = data_dir / name
                if not target.exists():
                    continue
                if target.is_file() and target.stat().st_size == info.size:
                    extracted = archive.extractfile(info)
                    digest = hashlib.sha256()
                    for block in iter(lambda: extracted.read(_CHUNK), b""):
                        digest.update(block)
                    if digest.hexdigest() == sha256_file(target):
                        already.add(name)
                        continue
                clashes.append(name)
            if clashes and not force:
                shown = ", ".join(clashes[:5]) + (" ..." if len(clashes) > 5 else "")
                raise PreparedDataError(
                    f"{len(clashes)} stored file(s) under {data_dir} have other bytes"
                    f" under the same name ({shown}); nothing was unpacked. Pass"
                    " --force to overwrite them."
                )
            staging = Path(tempfile.mkdtemp(prefix=".prepared-unpack-", dir=data_dir))
            archive.extractall(
                staging,
                members=[info for info, kind in members if kind != "dir"],
                filter="data",
            )

        manifest = json.loads((staging / MANIFEST_NAME).read_text(encoding="utf-8"))
        if manifest.get("format") != BUNDLE_FORMAT:
            raise PreparedDataError(
                f"the archive says it is {manifest.get('format')!r}, this install"
                f" reads {BUNDLE_FORMAT!r}; nothing was put in place"
            )

        written = 0
        for _info, name in raws:
            if name in already:
                continue
            target = data_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging / name, target)
            written += 1
        # The database goes last, so a failure above leaves the old one intact.
        # Its old write-ahead log belongs to the file being replaced; left
        # beside the new file, SQLite would replay it into the wrong database.
        for suffix in ("-wal", "-shm", "-journal"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)
        # shutil.move, not os.replace: --db may sit on another disk than the
        # data folder the archive was unpacked in.
        shutil.move(staging / ARCHIVE_DB_NAME, db_path)
        os.replace(staging / MANIFEST_NAME, data_dir / MANIFEST_NAME)
    finally:
        download.unlink(missing_ok=True)
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)

    from regcompass.storage import Storage

    storage = Storage(db_path)
    try:
        storage.apply_schema()
        documents = storage.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        runs = storage.conn.execute(
            "SELECT COUNT(*) FROM runs WHERE kind = 'run'"
        ).fetchone()[0]
    finally:
        storage.close()
    return LoadReport(
        db_path=db_path,
        data_dir=data_dir,
        sha256=actual,
        archive_bytes=size,
        raw_files_written=written,
        raw_files_already_present=len(already),
        documents=documents,
        runs=runs,
        replaced_database=replacing,
        manifest=manifest,
    )
