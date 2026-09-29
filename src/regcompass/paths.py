"""Where a Document's stored bytes are, and how a row writes that down.

One rule, in one place. A Document's bytes live under the data folder, so the
row that points at them records where they are RELATIVE to it:
`<ECONOMY>/raw/<file>`. Written that way, one database works inside the image,
on a judge's machine and on ours. Written as one machine's absolute path, every
Run anywhere else fails with the file missing, which is exactly what a database
built here in July would have done in the shipped image.

The reader is forgiving, because databases outlive the fix. A row that already
holds an absolute path, or one carrying a stray `data/` prefix, is resolved by
looking for the same bytes where they would be now. Same BYTES, not same name:
the digest on the row is the Document's identity, so a candidate whose file
does not hash to it is some other file with the same name and is refused.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

__all__ = [
    "AS_STORED",
    "BY_NAME",
    "WITHOUT_DATA_PREFIX",
    "ResolvedPath",
    "fallback_places",
    "resolve_stored_path",
    "storable_local_path",
]

# How a stored path was read back. The first is the ordinary case; the other
# two are repairs, and a repair is worth saying out loud because it means the
# row is written in a spelling that will not travel.
AS_STORED = "as stored"
BY_NAME = "by file name under the data folder"
WITHOUT_DATA_PREFIX = "with the stray 'data/' prefix dropped"

RAW_DIR = "raw"


def storable_local_path(path: Path | str, data_dir: Path | str) -> str:
    """The spelling a row records for these bytes: relative to the data folder
    when they live under it, POSIX-separated so the string a Mac writes is the
    string a Linux container reads. A file genuinely outside the data folder
    (an operator's own PDF mapped where it lies) keeps its full path, because
    there is no data folder to hang it off.

    Callers pass whatever they have. A relative path beside a relative data
    folder, an absolute beside an absolute, and the two mixed all reduce to the
    same `<ECONOMY>/raw/<file>`.
    """
    path = Path(path)
    data_dir = Path(data_dir)
    pairs = [(path, data_dir)]
    try:
        pairs.append((path.resolve(), data_dir.resolve()))
    except OSError:  # pragma: no cover - defensive; resolve() is non-strict
        pass
    for candidate, base in pairs:
        try:
            return candidate.relative_to(base).as_posix()
        except ValueError:
            continue
    return path.as_posix()


@dataclass(frozen=True)
class ResolvedPath:
    """A stored path read back: the file itself, and how it was found."""

    path: Path
    form: str
    stored: str

    @property
    def repaired(self) -> bool:
        """Was the row's own spelling wrong? True means the bytes were found
        somewhere else under the data folder, so the row points at a place this
        machine happens to have and another machine will not."""
        return self.form != AS_STORED


def _posix(stored: Path | str) -> PurePosixPath:
    return PurePosixPath(str(stored).replace("\\", "/"))


def fallback_places(
    stored: Path | str, data_dir: Path | str, economy: str | None = None
) -> list[tuple[Path, str]]:
    """The places a stored path that does not open is looked for, in order:
    under this Economy's own raw folder by file name, then with a leading
    `data/` dropped. Deduplicated, because for a `data/<ECONOMY>/raw/<file>`
    row the two are the same place.
    """
    data_dir = Path(data_dir)
    name = _posix(stored).name
    places: list[tuple[Path, str]] = []
    if economy and name:
        places.append((data_dir / economy / RAW_DIR / name, BY_NAME))
    parts = _posix(stored).parts
    if len(parts) > 1 and parts[0] == "data":
        places.append((data_dir.joinpath(*parts[1:]), WITHOUT_DATA_PREFIX))
    seen: set[Path] = set()
    unique: list[tuple[Path, str]] = []
    for place, form in places:
        if place not in seen:
            seen.add(place)
            unique.append((place, form))
    return unique


def _digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            sha.update(block)
    return sha.hexdigest()


def resolve_stored_path(
    stored: Path | str | None,
    data_dir: Path | str,
    *,
    economy: str | None = None,
    sha256: str | None = None,
) -> ResolvedPath | None:
    """Open what this row points at, or None when nothing that could be it is
    on disk.

    The row's own spelling is tried first, absolute or relative, which keeps a
    Document mapped from outside the data folder working. Every candidate has
    to prove itself the same way, the row's own spelling included: its bytes
    must hash to `sha256`, the digest the row already carries, because the
    bytes are the Document's identity and a namesake with other bytes is not
    it. Only when the row's own file is missing or is not the Document do the
    fallbacks run. Without a digest to check there is nothing better to do
    than take the file, so it is taken.
    """
    if stored is None or not str(stored):
        return None
    text = str(stored).replace("\\", "/")
    data_dir = Path(data_dir)
    first = Path(text)
    if not first.is_absolute():
        first = data_dir / first
    if first.is_file() and not (sha256 and _digest(first) != sha256):
        return ResolvedPath(first, AS_STORED, text)
    for place, form in fallback_places(text, data_dir, economy):
        if not place.is_file():
            continue
        if sha256 and _digest(place) != sha256:
            continue
        return ResolvedPath(place, form, text)
    return None
