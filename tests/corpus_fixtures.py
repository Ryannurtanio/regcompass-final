"""Seed a test Corpus from the bundled fixture Documents.

The three committed legislation PDFs used to be a constant inside the pipeline
(the old DEMO_CORPUS) that a Run read directly. A Run now reads the Corpus of
its Economy, so a test that needs a Corpus loads the fixture Documents into its
own temp database through the SAME write path Discovery and "Add document" use:
regcompass.corpus.add_document, which puts exact bytes on disk, files the
manifest rows and runs shortlist.ingest_economy.

The list itself moved to regcompass.fixtures when the keyless demo
needed the same bytes inside the image (`regcompass seed`). This module reads
that one list rather than keeping a second copy, so the goldens and the judge's
demo can never drift onto different bytes. What stays HERE is the test-only
choice of source kind: seed_corpus stands in for a DISCOVERY, so its rows are
marked 'discovery', while the shipped seed marks its rows 'fixture'.

The filename hints are chosen so ingest derives the document ids the curated
config/corpus.yaml already carries, which keeps the M9 export battery running
over them exactly as before.
"""

from __future__ import annotations

from pathlib import Path

from regcompass.config import CONFIG_DIR, load_portals
from regcompass.corpus import DISCOVERY_SOURCE_KIND, add_document
from regcompass.fixtures import BORN_DIGITAL, BUNDLED, ROOT, BundledDocument
from regcompass.storage import Storage

__all__ = ["BORN_DIGITAL", "BUNDLED", "ROOT", "BundledDocument", "seed_corpus"]


def seed_corpus(
    storage: Storage,
    data_dir: Path,
    economy: str,
    *,
    config_dir=CONFIG_DIR,
    language: str | None = None,
) -> list[str]:
    """Put this Economy's bundled fixture Documents into the Corpus exactly as
    a Discovery would have, and return their document ids."""
    data_dir = Path(data_dir)
    if language is None:
        language = load_portals(config_dir)[economy].languages[0]
    added = [
        add_document(
            storage, data_dir, economy, doc.path.read_bytes(),
            source_url=doc.source_url, language=language,
            filename_hint=doc.filename_hint, source_family=doc.family,
            # These stand in for DISCOVERED Documents, so they carry the source
            # kind a Discovery would have written, not "manual".
            source_kind=DISCOVERY_SOURCE_KIND, config_dir=config_dir,
        )
        for doc in BUNDLED[economy]
    ]
    return [a.document_id for a in added]
