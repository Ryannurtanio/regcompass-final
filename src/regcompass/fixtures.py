"""The legislation bundled with the source, and the offline seed that puts it
into a Corpus.

A Run reads the Corpus of its Economy and never fetches, which is
the right rule and which left the judge's keyless path with nothing to read: on
a clean Docker volume the documented demo exited 2 with "no Documents in the
Corpus" and pointed at a network Discovery. The three
legislation PDFs under tests/fixtures/ already ride into the image, so the
answer is to put them in the Corpus offline.

This list used to live in tests/corpus_fixtures.py, deliberately out of src/ so
that nothing shipped could pretend a baked-in list was a Corpus. That property
is kept where it matters: NOTHING here is read by a Run, a Discovery or an
export. The seed is a separate, explicit command, and every row it writes
carries source kind 'fixture' so a reader can tell a demo Corpus from a
collected one. tests/corpus_fixtures.py now reads this list rather than owning
a second copy, so the goldens and the judge's demo can never drift apart.

The bytes go in through regcompass.corpus.add_document, the same write path
Discovery and "Add document" use: exact bytes on disk, a manifest row over
them, then shortlist.ingest_economy. No network by any route.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from regcompass.config import CONFIG_DIR, load_portals
from regcompass.corpus import (
    FIXTURE_SOURCE_KIND,
    AddedDocument,
    DuplicateDocumentError,
    add_document,
)
from regcompass.storage import Storage

# src/regcompass/fixtures.py -> src/regcompass -> src -> the project root. The
# image installs the project editable (`pip install -e .`), so the source tree
# and its tests/fixtures directory are both present at /app and this resolves
# the same inside the container as it does in a checkout.
ROOT = Path(__file__).resolve().parents[2]
BORN_DIGITAL = ROOT / "tests/fixtures/sample_legislation/born_digital"

# The Economy the compose demo Runs. Australia, because its Run exports clean
# today; any bundled Economy works from the command line.
DEMO_ECONOMY = "AU"


class FixtureLegislationMissingError(RuntimeError):
    """Either this Economy has no bundled legislation, or the bundled file is
    not on disk. Both are the same kind of problem for a judge: the offline
    demo has nothing to read, and the message has to say which Economies do."""


@dataclass(frozen=True)
class BundledDocument:
    """One committed fixture Document, with the provenance a Discovery would
    have recorded for it. The filename hint is chosen so ingest derives the
    document id config/corpus.yaml already carries, which keeps the export
    battery running over these Documents exactly as before."""

    path: Path
    filename_hint: str
    source_url: str
    family: str


BUNDLED: dict[str, list[BundledDocument]] = {
    "SG": [
        BundledDocument(
            BORN_DIGITAL / "Telecommunications Act 1999.pdf",
            "telecommunications_act_1999.pdf",
            "https://sso.agc.gov.sg/Act/TA1999?ViewType=Pdf",
            "telecommunications",
        )
    ],
    "MY": [
        BundledDocument(
            BORN_DIGITAL / "PERSONAL DATA PROTECTION ACT 2010.pdf",
            "personal_data_protection_act_2010.pdf",
            "https://lom.agc.gov.my/ilims/upload/portal/akta/outputaktap/Act%20709%20ori.pdf",
            "data_protection",
        )
    ],
    "AU": [
        BundledDocument(
            BORN_DIGITAL / "C2026C00098VOL01.pdf",
            "C2026C00098VOL01.pdf",
            "https://www.legislation.gov.au/C2004A04868/2026-03-01/2026-03-01/text/original/pdf/1",
            "criminal_law",
        )
    ],
}


@dataclass(frozen=True)
class SeedResult:
    """What one seed did. `already_present` is not an error: seeding twice is
    what a judge's second `docker compose up` does, and the same bytes are one
    Document however many times they are offered."""

    economy: str
    language: str
    added: list[AddedDocument] = field(default_factory=list)
    already_present: list[str] = field(default_factory=list)


def seedable_economies() -> tuple[str, ...]:
    """The Economies that carry bundled legislation, in code order."""
    return tuple(sorted(BUNDLED))


def bundled_for(economy: str) -> list[BundledDocument]:
    """This Economy's bundled Documents, or a readable error naming the
    Economies that have some."""
    docs = BUNDLED.get(economy)
    if not docs:
        raise FixtureLegislationMissingError(
            f"no bundled fixture legislation for '{economy}': the Economies that"
            f" carry some are {', '.join(seedable_economies())}. Fill this"
            f" Economy's Corpus with `regcompass discover --economy {economy}`"
            " (network) or by adding a Document by hand."
        )
    missing = [doc.path for doc in docs if not doc.path.is_file()]
    if missing:
        raise FixtureLegislationMissingError(
            f"the bundled legislation for '{economy}' is not on disk:"
            f" {', '.join(str(p) for p in missing)}."
            " An installed copy of RegCompass that dropped tests/fixtures/"
            " cannot run the offline demo."
        )
    return docs


def seed_economy(
    storage: Storage,
    data_dir: Path,
    economy: str,
    *,
    language: str | None = None,
    config_dir=CONFIG_DIR,
) -> SeedResult:
    """Put this Economy's bundled legislation into its Corpus, offline.

    The Language is the Economy's first configured Portal Language unless the
    caller names one, which mirrors what an "Add document" with no Language
    chosen does. Documents already in the Corpus are reported, not re-added, so
    the command is safe to run on every start: the digest is looked up before
    the add rather than caught after it, because the same bytes under the same
    address are no longer a refusal. They are the Document that is already
    there, and an add hands it back, which a seed must report as present rather
    than announce as new."""
    data_dir = Path(data_dir)
    docs = bundled_for(economy)
    if language is None:
        language = load_portals(config_dir)[economy].languages[0]

    added: list[AddedDocument] = []
    already: list[str] = []
    for doc in docs:
        raw = doc.path.read_bytes()
        # The Document that already holds these bytes, asked of the same table
        # add_document asks, so the name reported is the stored one rather than
        # one parsed back out of an error message.
        existing = storage.conn.execute(
            "SELECT document_id FROM documents WHERE source_sha256 = ?",
            (hashlib.sha256(raw).hexdigest(),),
        ).fetchone()
        if existing is not None:
            already.append(existing["document_id"])
            continue
        try:
            added.append(
                add_document(
                    storage, data_dir, economy, raw,
                    source_url=doc.source_url, language=language,
                    filename_hint=doc.filename_hint, source_family=doc.family,
                    source_kind=FIXTURE_SOURCE_KIND, config_dir=config_dir,
                )
            )
        except DuplicateDocumentError:
            # These bytes are in the Corpus under ANOTHER address, which the
            # lookup above cannot see past: one Economy's bundled file is
            # another's, say. Still not an error, and still reported as
            # present rather than added.
            row = storage.conn.execute(
                "SELECT document_id FROM documents WHERE source_sha256 = ?",
                (hashlib.sha256(raw).hexdigest(),),
            ).fetchone()
            already.append(row["document_id"] if row is not None else doc.filename_hint)
    return SeedResult(
        economy=economy, language=language, added=added, already_present=already
    )
