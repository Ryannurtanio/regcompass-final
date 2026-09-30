"""Add a Document to an Economy's Corpus by hand.

Discovery is one way bytes become a Document; this is the other. A reviewer
uploads a PDF, or names an official Source URL, and the Document enters the
Corpus through the SAME path a discovered one takes: exact bytes on disk, a
crawl_manifest row over them, then shortlist.ingest_economy, which extracts,
OCRs where the text layer is thin, derives the title and writes the row. The
next Run reads it like any other Document, which is the whole point: an
Economy whose Portal forbids automated collection is not a second-class
Economy, it just has a different front door.

Two things mark the row afterwards. `source_kind` is 'manual', which the
Document list shows and the Evidence Export discloses in Notes; and the add
files its own Run Record of kind 'discovery' carrying `manual: true`, so a
steward reading the Runs list can see exactly what arrived by hand.

A manual upload's manifest row is keyed by its address AND its own content
digest (`<url>#sha256=<digest>`, or `local-upload:<digest>` where the operator
named no address), never by the address alone. The manifest's URL key is right
for Discovery, where a URL fetched once must never be fetched again; it is
wrong here, because one ministry landing page publishes many statutes, and
keying on it alone had each upload land on the row the previous upload had
claimed. Both key shapes, and the reading that recovers the address from one,
are in regcompass.contracts (manifest_key_for_upload, manifest_address), where
the ingest can reach them without importing this module's extraction stack.

This module never imports the network lane at module scope. The upload lane
touches no network at all, and the URL lane imports regcompass.crawl inside
the call, so a Run (which imports the Corpus, never the crawler) stays
provably fetch-free.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlsplit

from regcompass.config import CONFIG_DIR, load_portals
from regcompass.extract import OCR_TRIGGER_CHARS
from regcompass.paths import storable_local_path
from regcompass.contracts import (
    DISCOVERY_SOURCE_KIND,
    FIXTURE_SOURCE_KIND,
    MANUAL_SOURCE_KIND,
    PortalConfig,
    manifest_key_for_upload,
)
from regcompass.shortlist import (
    IngestResult,
    document_id_for,
    ingest_economy,
    resolve_document_id,
)
from regcompass.storage import Storage

# The three ways bytes become a Document. The strings live in contracts.py
# because the Export reads them too and must not import this module's
# extraction stack to learn them; the names stay here, where the add lane uses
# them. FIXTURE is `regcompass seed`: the legislation bundled with the install,
# a demonstration Corpus rather than a collection (see regcompass.fixtures).
FIXTURE_DISCLOSURE = "fixture legislation, demo only"

#: How an upload is filed in the fetch manifest: by its address AND its own
#: digest, so several statutes published under one landing page each keep a row
#: of their own, and by the digest alone when the operator named no address.
#: Both key shapes, and the reverse reading an ingest does to recover the
#: address, live in regcompass.contracts (manifest_key_for_upload,
#: manifest_address), because the Export and the ingest read them too and must
#: not import this module's extraction stack to learn them.


class ManualOnlyEconomyError(RuntimeError):
    """This Economy's Portal forbids automated collection, so nothing may be
    fetched for it, an operator-supplied URL included: an add-by-URL is still a
    request we would be making. The upload lane stays open."""


class HostNotAllowedError(RuntimeError):
    """The URL's host is not on this Economy's Portal whitelist. The operator
    can still vouch for it (allow_any_host), which is a decision they make
    explicitly and which the export then discloses on every row."""


class DuplicateDocumentError(RuntimeError):
    """These exact bytes are already in this Economy's Corpus UNDER ANOTHER
    ADDRESS. Adding them again would give one Document two places it is
    published, so the add is refused and names the Document that already holds
    them. The same bytes under the same address are not this: that is one
    Document uploaded twice, and the add hands back the Document that exists."""


class NameCollisionError(ValueError):
    """Two different files whose names reduce to one Document id.

    The id is derived from the file name, so `act.pdf` uploaded twice with
    different contents would have the second file's bytes land on the first
    file's row: one law's text under another law's title, which is the worst
    thing this Corpus can do. Refused, with the fix in the message."""


class UnreadableFileError(ValueError):
    """Bytes RegCompass cannot read as legislation: a Word or spreadsheet
    file, an archive, an image, anything binary that is not a PDF. Treating
    them as a web page (which every non-PDF used to be) put the zip's raw
    bytes into the Corpus as the Document's "text"."""


class TooLittleTextError(ValueError):
    """A web page (or plain text) that read to almost no text: an error page,
    a login or search page, a page that builds its text with scripts. A Run
    over it would find nothing and say so as if the law had been searched, so
    it never enters the Corpus. A PDF is not refused this way: a scan OCR read
    badly is still that law, its OCR verdict is recorded on the row, and the
    add warns instead (AddedDocument.warning)."""


# Below this many characters of text a Document is not a law we can map. The
# per-page OCR trigger (OCR_TRIGGER_CHARS, 50) is the same idea applied to one
# page; a whole Document gets twice that. The smallest statute in the shipped
# Corpus runs to thousands of characters, and the error pages the adds met
# ("Invalid request", "India Code") to fifteen and ten.
MIN_DOCUMENT_CHARS = 2 * OCR_TRIGGER_CHARS

# One Chinese or Japanese character carries about a word, so it weighs as
# much as a short English word does in characters: a Chinese statute of a
# hundred characters is not an error page.
_DENSE_SCRIPT = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")
_DENSE_WEIGHT = 4


def _text_weight(text: str) -> int:
    """How much text a Document holds, for the almost-empty check: its
    characters, with a dense-script character counted as a word."""
    text = text.strip()
    return len(text) + (_DENSE_WEIGHT - 1) * len(_DENSE_SCRIPT.findall(text))


# What each readable kind of file is filed as in the fetch manifest.
_CONTENT_TYPES = {"pdf": "application/pdf", "html": "text/html", "text": "text/plain"}


@dataclass(frozen=True)
class AddedDocument:
    """What one add produced, in the words the interface shows: the Document's
    id and title, what extraction found, and the facts the reviewer supplied."""

    document_id: str
    title: str
    economy: str
    language: str
    # None for a file a reviewer uploaded without naming where it is published.
    # The Document is in the Corpus and a Run reads it; its rows follow the
    # local-copy rule until somebody records the official address.
    source_url: str | None
    source_kind: str
    n_pages: int
    n_low_yield_pages: int
    ocr_applied: bool
    # Set only when the Portal's robots.txt could not be read and something
    # other than its published rules let this one request past it: RFC 9309's
    # 30-day rule, or the Portal's own configured policy. The caller records
    # it. Where it was the policy, the status the Portal answered with and the
    # policy itself are carried beside the sentence, so the answer an operator
    # sees states both rather than leaving them to be read out of prose.
    robots_note: str | None = None
    robots_unavailable_status: int | None = None
    robots_unavailable_policy: str | None = None
    # A plain sentence the interface shows beside the add: set when a PDF read
    # to almost no text and was kept, because a scan is still the law.
    warning: str | None = None


def portal_for(economy: str, config_dir=CONFIG_DIR) -> PortalConfig:
    """One Economy's Portal configuration, or a readable error naming the
    Economies that are configured."""
    portals = load_portals(config_dir)
    if economy not in portals:
        raise ValueError(
            f"unknown economy '{economy}': configured economies are {sorted(portals)}"
        )
    return portals[economy]


def default_language(economy: str, config_dir=CONFIG_DIR) -> str:
    """The Language an add assumes when the reviewer names none: the Portal's
    first configured Language. Per-Document detection is out of scope, so the
    Economy's own default is the honest answer and the reviewer corrects it in
    the control when it is wrong."""
    return portal_for(economy, config_dir).languages[0]


def check_host_allowed(
    portal: PortalConfig, source_url: str, *, allow_any_host: bool = False
) -> str:
    """The URL's host, once it has cleared this Economy's Portal whitelist.

    An Economy with no verified host (an empty whitelist) can only be added to
    with the operator's explicit override, which is the honest shape: we have
    not checked that Portal, so only a person can vouch for the URL."""
    host = urlsplit(source_url).netloc
    if not host or not urlsplit(source_url).scheme.startswith("http"):
        raise ValueError(f"'{source_url}' is not an http(s) Source URL")
    from regcompass.contracts import is_never_requested

    if is_never_requested(host):
        raise HostNotAllowedError(
            f"'{host}' is never requested by RegCompass. Upload the file"
            " instead, with its Source URL."
        )
    if allow_any_host or host in portal.hosts:
        return host
    raise HostNotAllowedError(
        f"'{host}' is not on {portal.official_name}'s Portal whitelist"
        f" ({portal.hosts or 'no host has been verified for this Economy yet'})."
        " Tick 'official source outside the configured Portal' to vouch for it;"
        " the Evidence Export then discloses that on every row from it."
    )


def add_document(
    storage: Storage,
    data_dir: Path,
    economy: str,
    raw: bytes,
    *,
    source_url: str | None,
    language: str | None = None,
    filename_hint: str | None = None,
    title: str | None = None,
    source_kind: str = MANUAL_SOURCE_KIND,
    source_family: str | None = None,
    config_dir=CONFIG_DIR,
    evidence_root: Path | None = None,
) -> AddedDocument:
    """Put one Document's exact bytes into an Economy's Corpus.

    The bytes land under data_dir/<ECONOMY>/raw/ named by their own sha256, a
    manifest row records where they came from, and ingest_economy does the rest
    (extraction, OCR when the text layer is thin, the title, the row). The OCR
    language data follows the Document's Language through
    regcompass.languages, so a Lao or Thai upload is not read as English.

    `title` is the operator's own statute name, and it wins over the one the
    ingest derives. It matters because that string is what the Evidence Export
    ships in the organizer's Law Name column: derived from a file called
    `upload sample.pdf` it reads "upload sample", which is not the name of any
    law. Left out, the derived title stands exactly as before.

    `source_url` may be None on the upload lane: a reviewer who saved the file
    by hand can get it into the Corpus and Run it without first hunting down
    where it is published. The Corpus row then records no address, so every row
    from this Document follows the local-copy rule, and the Evidence Export's
    battery refuses it by name (Source URL is a REQUIRED column) until somebody
    records the real one. Getting the Document in is the urgent thing; the
    address is checkable later, and nothing ships quietly without it.

    SEVERAL Documents may share one `source_url`, and they must: a ministry
    landing page publishes a whole collection, and that page is the honest
    address for every statute on it. Each upload keeps its own file, its own
    text and its own row. Two cases are not that. The same bytes under the same
    address are one Document uploaded twice, and the Document that already
    exists is handed back untouched; the same bytes under a SECOND address are
    refused, because only a person can say where a law is published. Two
    different files whose NAMES reduce to one Document id are refused too
    (NameCollisionError): the id is derived from the file name, so the second
    would otherwise land on the first one's row.

    No network by any route: this is the upload lane, and it is also the lane
    add_document_from_url finishes in once it has the bytes."""
    if not raw:
        raise ValueError("an empty file is not a Document")
    kind = readable_kind(raw)
    portal = portal_for(economy, config_dir)
    language = language or portal.languages[0]
    data_dir = Path(data_dir)
    sha = hashlib.sha256(raw).hexdigest()

    given_url = (source_url or "").strip() or None
    hint = _filename_hint(filename_hint, given_url)
    # The manifest's key is a URL, which is right for Discovery and wrong for
    # an upload: several statutes are published under one landing page, and an
    # operator pasting that page for each of them filed every upload on ONE
    # row. So an upload is keyed by its address AND its own digest. The Corpus
    # row below still records the address alone; the digest is how the manifest
    # tells two Documents apart, not part of where either one is published.
    manifest_key = manifest_key_for_upload(given_url, sha)
    upload_row = {
        "filename_hint": hint,
        "local_path": hint,
        "economy": economy,
        "url": manifest_key,
        "sha256": sha,
    }
    plain_id = document_id_for(upload_row)

    # These exact bytes are already here. Whether that is a mistake depends on
    # the address beside them: the same file under the same address (or under
    # none, twice) is one Document somebody uploaded twice, and the honest
    # answer is the Document they already have. A second, DIFFERENT address for
    # the same bytes is a claim only a person can settle, so it is still
    # refused by name.
    existing = storage.conn.execute(
        "SELECT * FROM documents WHERE source_sha256 = ?", (sha,)
    ).fetchone()
    if existing is not None:
        if existing["economy"] == economy and existing["source_url"] == given_url:
            return _already_added(existing)
        raise DuplicateDocumentError(
            f"these exact bytes are already in the Corpus as"
            f" '{existing['document_id']}'"
        )

    # Two different files whose names reduce to one Document id. The id comes
    # from the file name, so `act.pdf` twice would have the second file's bytes
    # land on the first file's row and leave one law's text under another law's
    # title. Refused, naming the fix: the operator renames the file. An upload
    # always has a readable name to rename: _filename_hint above sanitises the
    # one it was given and falls back to "document.pdf", so the digest-named
    # ids the ingest derives for an unreadable name are a Discovery matter.
    clash = storage.conn.execute(
        "SELECT document_id, title FROM documents WHERE document_id = ?",
        (plain_id,),
    ).fetchone()
    if clash is not None:
        raise NameCollisionError(
            f"'{hint}' would be filed as '{plain_id}', which is already"
            f" {clash['title'] or 'another Document'} in this Corpus. Rename"
            " the file so the two Documents can be told apart, then upload it"
            " again."
        )

    # The id the ingest below will give these bytes, asked of the ingest's own
    # resolver rather than derived a second time here, so the upload lane and
    # the Discovery lane can never drift into two ids for one file. Read before
    # the manifest row is written, which changes nothing it looks at, and after
    # the refusal above, which is what leaves the readable id free for it.
    expected_id = resolve_document_id(storage, upload_row)

    raw_dir = data_dir / economy / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    local = raw_dir / f"{sha[:12]}_{hint}"
    local.write_bytes(raw)

    storage.manifest_add_pending(
        manifest_key, economy, source_family=source_family, filename_hint=hint
    )
    storage.manifest_mark_fetched(
        manifest_key, http_status=200, method="manual", sha256=sha,
        content_type=_CONTENT_TYPES[kind], size_bytes=len(raw),
        local_path=storable_local_path(local, data_dir),
    )

    results, excluded = ingest_economy(
        storage, data_dir, economy, language=language, evidence_root=evidence_root
    )
    mine = [r for r in results if r.document_id == expected_id]
    if not mine:
        reasons = "; ".join(e["reason"] for e in excluded) or "the ingest produced no row"
        raise RuntimeError(
            f"'{given_url or hint}' could not be added to the Corpus: {reasons}"
        )

    result: IngestResult = mine[0]
    warning = None
    thin = _text_weight(storage.document_full_text(result.document_id)) < MIN_DOCUMENT_CHARS
    chars = f"{result.n_chars} character{'' if result.n_chars == 1 else 's'}"
    if thin and kind != "pdf":
        # Refused, and undone: the row, the stored stream, the manifest rows
        # and the file all go, so a refused add leaves nothing to explain.
        storage.remove_document(result.document_id, data_dir=data_dir)
        where = "page at this address" if given_url else "file"
        raise TooLittleTextError(
            f"The {where} holds almost no text ({chars}), so it is not a law a"
            " Run can read. It may be an error page, a login or search page, or"
            " a page that builds its text with scripts. Add the statute's own"
            " PDF or its full-text page instead."
        )
    if thin:
        warning = (
            f"This PDF gave almost no text ({chars}"
            f"{', even after OCR' if result.ocr_applied else ''}). A Run will"
            " find little or nothing in it. Check it is the statute and not a"
            " cover page or an unreadable scan; Remove it from the Corpus list"
            " if not."
        )
    # The fields ingest does not know: HOW this Document arrived, and the
    # statute name the operator typed. A thin re-upsert overwrites only the
    # fields it is given, so the row ingest just wrote keeps everything else.
    given_title = (title or "").strip() or None
    derived_title = result.title
    if given_title is None:
        # No name typed, so the derived one is what the Evidence Export will
        # write as the Law Name: cleaned here, on the add lane, for this new
        # Document only. Titles already stored are never rewritten.
        derived_title = clean_added_title(
            result.title, raw=raw, kind=kind, economy=economy, hint=hint
        )
    # The Source URL is the operator's own address, exactly as they typed it:
    # never the manifest key, which carries the digest beside it, and None
    # where they named no address at all. Saying None is what makes the
    # local-copy link and the export battery's refusal both read correctly.
    extra: dict[str, object] = {"source_kind": source_kind, "source_url": given_url}
    if given_title is not None:
        extra["title"] = given_title
    elif derived_title != result.title:
        extra["title"] = derived_title
    storage.upsert_document(result.document_id, economy, sha, **extra)
    return AddedDocument(
        document_id=result.document_id,
        title=given_title or derived_title,
        economy=economy,
        language=language,
        source_url=given_url,
        source_kind=source_kind,
        n_pages=result.n_pages,
        n_low_yield_pages=result.n_low_yield_pages,
        ocr_applied=result.ocr_applied,
        warning=warning,
    )


def seeded_family(economy: str, source_url: str, config_dir=CONFIG_DIR):
    """The source-list entry (config/crawl_seeds.yaml) whose official
    addresses include this one, or None. Such an address is a known law with
    a known name and Language."""
    from regcompass.config import load_crawl_seeds

    try:
        seeds = load_crawl_seeds(config_dir).get(economy)
    except Exception:
        return None
    if seeds is None:
        return None
    wanted = source_url.strip()
    return next(
        (f for f in seeds.families.values() if wanted in f.urls), None
    )


def add_document_from_url(
    storage: Storage,
    data_dir: Path,
    economy: str,
    source_url: str,
    *,
    language: str | None = None,
    allow_any_host: bool = False,
    title: str | None = None,
    source_kind: str = MANUAL_SOURCE_KIND,
    config_dir=CONFIG_DIR,
    evidence_root: Path | None = None,
    fetch: Callable | None = None,
    limiter=None,
    robots=None,
) -> AddedDocument:
    """Fetch ONE operator-supplied URL politely, then add what came back.

    The whitelist is checked before anything is requested, and an Economy whose
    Portal forbids automated collection refuses this lane outright: its site
    rules do not distinguish a crawl from a single request, and neither do we.
    `fetch`, `limiter` and `robots` pass straight through to crawl.fetch_one so
    a test drives the lane over a recorded answer."""
    portal = portal_for(economy, config_dir)
    if portal.manual_only:
        raise ManualOnlyEconomyError(
            f"{portal.official_name} ({economy}) is manual-only: its Portal's"
            " own site rules forbid automated collection, and adding by URL is"
            " still a request we would make. Upload the file instead."
        )
    check_host_allowed(portal, source_url, allow_any_host=allow_any_host)
    # An address the source list names is a known law: with nothing typed,
    # it takes that law's name and Language rather than its file name and
    # the Portal's default Language.
    seed = seeded_family(economy, source_url, config_dir)
    if seed is not None:
        title = title or seed.law
        language = language or seed.language

    # Imported HERE, never at module scope: a Run imports the Corpus and must
    # stay unable to reach the network lane at all (tests/test_corpus_run.py).
    from regcompass.crawl import fetch_one

    readings: list = []
    result = fetch_one(
        source_url, economy,
        fetch=fetch, limiter=limiter, robots=robots, config_dir=config_dir,
        robots_reading=readings.append,
        # A redirect may only lead to another host of this Portal, unless the
        # operator vouched for any host; never to a never-requested one.
        allowed_hosts=None if allow_any_host else list(portal.hosts),
    )
    added = add_document(
        storage, data_dir, economy, result.content,
        source_url=source_url, language=language,
        filename_hint=url_filename_hint(storage, economy, source_url, title),
        title=title,
        source_kind=source_kind, config_dir=config_dir,
        evidence_root=evidence_root,
    )
    # This lane reads robots.txt through the same place Discovery does, so when
    # the 30-day rule or the Portal's own policy lets a request past an
    # unreadable robots.txt the add says so on its own record, in the same
    # words and with the same two facts beside them.
    if not readings:
        return added
    reading = readings[0]
    return replace(
        added,
        robots_note=reading.note,
        robots_unavailable_status=reading.unavailable_status,
        robots_unavailable_policy=reading.unavailable_policy,
    )


def _already_added(row) -> AddedDocument:
    """The Document these bytes are already in the Corpus as.

    A re-upload of a file that is already here, under the address it is already
    filed under, is not a second Document and not an error: it is an operator
    who lost track of what they had uploaded, which during a timed assessment
    is the most ordinary thing in the world. They get back the Document they
    have, and nothing about it is touched."""
    return AddedDocument(
        document_id=row["document_id"],
        title=row["title"] or row["document_id"],
        economy=row["economy"],
        language=row["language"],
        source_url=row["source_url"],
        source_kind=row["source_kind"],
        n_pages=row["n_pages"] or 0,
        n_low_yield_pages=row["n_low_yield_pages"] or 0,
        ocr_applied=bool(row["ocr_applied"]),
    )


def _filename_hint(hint: str | None, source_url: str | None) -> str:
    """A filename for the stored bytes. The reviewer's own filename wins (it is
    what they will recognise in the Document list); otherwise the last path
    segment of the Source URL, and failing that a flat 'document.pdf', because
    the hint is what the document id is derived from and it can never be
    empty."""
    # The URL's last segment is percent-encoded ("Act%20709%20ori.pdf"), and a
    # document id built off the encoding reads as nonsense; decode first. A
    # trailing slash ("/Acts-Supp/40-2020/") does not end the name: the
    # segment before it is the law's.
    path = urlsplit(source_url or "").path.rstrip("/")
    base = hint or unquote(path.rsplit("/", 1)[-1]) or "document.pdf"
    base = re.sub(r"[^A-Za-z0-9._()-]+", "_", base).strip("._") or "document"
    if "." not in base:
        base = f"{base}.pdf"
    return base[:120]


# ---------------------------------------------------------------------------
# what an add can read
# ---------------------------------------------------------------------------

# Markup a web page opens with, looked for near the top once any byte-order
# mark and leading space are gone. Case does not matter in HTML.
_HTML_OPENERS = re.compile(
    rb"<(?:!doctype\s+html|html|head|body|meta|title|div|p|table|article|main|section)\b",
    re.I,
)

# Container formats that are never a PDF or a page, named in the refusal so
# the reviewer knows what they picked. Word, Excel and PowerPoint files since
# 2007 are zip archives; the older ones are OLE compound files.
_BINARY_SIGNATURES = (
    (b"PK\x03\x04", "a Word, Excel or PowerPoint file, or a zip archive"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "an older Word or Excel file"),
    (b"\x89PNG", "an image"),
    (b"\xff\xd8\xff", "an image"),
    (b"GIF8", "an image"),
    (b"{\\rtf", "a rich-text (RTF) file"),
)


def readable_kind(raw: bytes) -> str:
    """'pdf', 'html' or 'text' for bytes an add can read; UnreadableFileError,
    with a plain reason, for anything else.

    A PDF carries %PDF- in its first 1024 bytes (the same test the extractor
    makes). A web page opens with markup. Plain text is text that decodes as
    UTF-8 with no NUL bytes; it takes the web-page lane, which keeps its line
    breaks. Everything else is refused before a byte is stored."""
    if b"%PDF-" in raw[:1024]:
        return "pdf"
    what = next((name for sig, name in _BINARY_SIGNATURES if raw.startswith(sig)), None)
    if what is None:
        head = raw[:4096].lstrip(b"\xef\xbb\xbf \t\r\n")
        if _HTML_OPENERS.search(head):
            return "html"
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        if text is not None and "\x00" not in text:
            return "text"
    raise UnreadableFileError(
        f"This file is {what or 'not a PDF, a web page or plain text'}, and"
        " RegCompass cannot read it as legislation. Save the law as a PDF (or"
        " give its official web page) and add that instead."
    )


# ---------------------------------------------------------------------------
# the id of a Document added by URL
# ---------------------------------------------------------------------------

# Last path segments that name how a Portal serves a file, not which law it
# is. Federal Register PDFs end in /pdf/0 and India Code's in /content, so
# every add from either Portal derived the SAME id and the second one was
# refused with advice (rename the file) the URL lane cannot follow.
_GENERIC_SEGMENTS = frozenset(
    "0 1 content download file files view text pdf html htm index document"
    " documents original latest print get show details detail act-detail"
    " default page".split()
)
_DATE_SEGMENT = re.compile(r"\d{4}-\d{2}-\d{2}")
_OPAQUE_SEGMENT = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\d+", re.I)


# A file extension inside a name ("ประกาศฯ.pdf.aspx" keeps ".pdf" in its stem).
_INNER_EXTENSION = re.compile(r"\.(?:pdf|html?|aspx?|docx?|php)\b", re.I)


def _is_meaningful(segment: str) -> bool:
    stem = segment.rsplit(".", 1)[0].lower()
    return bool(
        stem
        and stem not in _GENERIC_SEGMENTS
        and not _DATE_SEGMENT.fullmatch(stem)
        and not _OPAQUE_SEGMENT.fullmatch(stem)
        and (stem.isascii() or _names_in_ascii(stem))
    )


def _names_in_ascii(stem: str) -> bool:
    """Does a name in another script still name something once the id's
    ASCII spelling is all that is left of it? "ประกาศฯ" leaves nothing and
    "Приложение 9_ред 131 (6)" leaves "9_131_(6)": neither is a name, so the
    typed law name is used instead."""
    ascii_only = re.sub(r"[^A-Za-z0-9()-]+", "_", _INNER_EXTENSION.sub("", stem))
    return bool(re.search(r"[A-Za-z]", ascii_only))


def _numbered_in_ascii(segment: str) -> bool:
    """Does a file name in another script keep its number once only ASCII is
    left? "05ສພຊ2021.pdf" keeps "05_2021", the Lao Gazette's own numbering,
    which beats the host. "ประกาศฯ.PDF" keeps nothing but its extension."""
    stem = _INNER_EXTENSION.sub("", segment.rsplit(".", 1)[0])
    return bool(re.search(r"\d", re.sub(r"[^A-Za-z0-9()-]+", "_", stem)))


def _is_identifier(segment: str) -> bool:
    """A path segment that reads as a register number ("C2006A00088"): letters
    and digits both, and not a digest. A bare word ("bitstreams", "api") says
    how the Portal is built, not which law this is."""
    return (
        _is_meaningful(segment)
        and bool(re.search(r"[A-Za-z]", segment))
        and bool(re.search(r"\d", segment))
    )


def url_filename_hint(
    storage: Storage, economy: str, source_url: str, title: str | None = None
) -> str:
    """The name a Document fetched from `source_url` is filed under, which is
    what its id is read from.

    The URL's last segment where it names the law ("Act%20709%20ori.pdf"),
    exactly as before, so every id the lane has already given stays what it
    is. Where that segment only says how the Portal serves files ("0",
    "content"), the law name the reviewer typed, else the nearest segment that
    reads as a register number ("C2006A00088"), else the host. And where the id that
    gives is already another Document's, a short digest of the URL goes on the
    end: one address always gives one id, and two addresses never share one."""
    parts = urlsplit(source_url)
    segments = [unquote(s) for s in parts.path.split("/") if s]
    if segments and _is_meaningful(segments[-1]):
        hint = _filename_hint(None, source_url)
    else:
        # A dot in a law name ("No.57-FZ", "B.E.2544") is a word break, not
        # the start of a file extension that would cut the name short.
        typed = re.sub(r"\s+", "_", (title or "").replace(".", " ").strip())
        if not re.search(r"[A-Za-z0-9]", typed):
            typed = ""  # a name in another script sanitises to nothing
        named = next((s for s in reversed(segments) if _is_identifier(s)), None)
        host = (parts.hostname or "").removeprefix("www.").split(".")[0]
        last = segments[-1] if segments else ""
        if not (typed or named) and not last.isascii() and _numbered_in_ascii(last):
            hint = _filename_hint(None, source_url)
        else:
            hint = _filename_hint(typed or named or host or None, None)
    plain = document_id_for({"economy": economy, "filename_hint": hint, "local_path": hint})
    held = storage.conn.execute(
        "SELECT source_url FROM documents WHERE document_id = ?", (plain,)
    ).fetchone()
    if held is None or held["source_url"] == source_url:
        return hint
    digest = hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:8]
    stem, dot, ext = hint.rpartition(".")
    return f"{stem}_{digest}{dot}{ext}" if dot else f"{hint}_{digest}"


# ---------------------------------------------------------------------------
# the Law Name of a Document added without one
# ---------------------------------------------------------------------------

# Separators a site puts between a page's own name and the site's ("Spam Act
# 2003 - Federal Register of Legislation", "...条例_中央网络安全和信息化委员会办公室").
_SITE_SEPARATOR = re.compile(r"\s+[-|\u2013\u2014]\s+|\s*_\s*|\s*\uff5c\s*")
_SMALL_WORDS = frozenset("a an and as at by for from in of on or the to under with".split())
_LETTERS = re.compile(r"[^\W\d_]")
_LATIN = re.compile(r"[A-Za-z]")
_CJK = re.compile(r"[㐀-鿿]")


def _is_broken_case(word: str) -> bool:
    """A word whose capitals fall mid-word more than once, or after a lower
    case start: 'eLectrOnIc', 'cOMMerce'. Text-layer damage, not a name."""
    letters = [c for c in word if c.isalpha()]
    if len(letters) < 3:
        return False
    flips = sum(1 for a, b in zip(letters, letters[1:]) if a.islower() and b.isupper())
    return flips >= 2 or (letters[0].islower() and any(c.isupper() for c in letters[1:]))


def _title_case(title: str) -> str:
    words = title.split(" ")
    out = []
    for i, word in enumerate(words):
        lower = word.lower()
        core = lower.strip("()[],.;:'\"")
        if i > 0 and core in _SMALL_WORDS:
            out.append(lower)
        else:
            # The first letter of the word itself, past any opening bracket.
            j = next((k for k, c in enumerate(lower) if c.isalpha()), None)
            out.append(lower if j is None else lower[:j] + lower[j].upper() + lower[j + 1:])
    return " ".join(out)


def normalise_case(title: str) -> str:
    """Title case for a Latin-script name set wholly in capitals or damaged by
    the text layer; any other name exactly as it stands. Tokens with digits
    ("A1743", "2006") keep their spelling either way."""
    letters = _LETTERS.findall(title)
    if not letters or not all(_LATIN.match(c) for c in letters):
        return title
    words = [w for w in title.split() if _LETTERS.search(w) and not any(c.isdigit() for c in w)]
    all_caps = bool(words) and all(w.isupper() for w in words) and len(words) >= 2
    if not all_caps and not any(_is_broken_case(w) for w in words):
        return title
    fixed = _title_case(title)
    # A token with digits is an identifier and keeps its own spelling.
    return " ".join(
        orig if any(c.isdigit() for c in orig) else new
        for orig, new in zip(title.split(" "), fixed.split(" "))
    )


def strip_site_name(title: str) -> str:
    """The page's own name without the site's name after it. The first part
    stands when it reads as a name (four characters of CJK, or two words);
    otherwise the title is left whole rather than cut to a fragment."""
    parts = [p.strip() for p in _SITE_SEPARATOR.split(title) if p.strip()]
    if len(parts) < 2:
        return title
    first = parts[0]
    if len(_CJK.findall(first)) >= 4 or len(first.split()) >= 2:
        return first
    return title


def _usable_metadata_title(text: str | None) -> str | None:
    """A PDF's own Title field, where it names something: not a digest, not
    a page-range stamp, not a two-letter code ("BI", "5306gi")."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text or _OPAQUE_SEGMENT.search(text) and len(text.split()) < 3:
        return None
    if len(_CJK.findall(text)) >= 4:
        return text
    words = [w for w in text.split() if _LETTERS.search(w)]
    return text if len(words) >= 2 and sum(len(w) for w in words) >= 8 else None


def _pdf_metadata_title(raw: bytes) -> str | None:
    try:
        import io

        import pdfplumber

        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            value = (pdf.metadata or {}).get("Title")
    except Exception:  # noqa: BLE001 - a title is a nicety, never a refusal
        return None
    return value if isinstance(value, str) else None


def clean_added_title(
    title: str, *, raw: bytes, kind: str, economy: str, hint: str
) -> str:
    """The Law Name an add derived, made fit to ship.

    Three repairs, each mechanical and each only where the damage is plain:

    - a title that is only the file's name ("ID UU Nomor 35 Tahun 2014") gives
      way to the PDF's own Title field where that names something, and
      otherwise loses the Economy code a reviewer prefixed to the file;
    - a site's name after the page's ("..._中央网络安全和信息化委员会办公室",
      "... - Federal Register of Legislation") goes;
    - a Latin name set wholly in capitals, or with capitals scattered through
      its words by the text layer ("eLectrOnIc cOMMerce Act 2006"), is set in
      title case.

    Never model-generated, and applied to new adds only: a stored title is
    never rewritten here."""
    stem = re.sub(r"[_\s]+", " ", hint.rsplit(".", 1)[0]).strip()
    cleaned = title
    if title == stem:
        meta = _usable_metadata_title(_pdf_metadata_title(raw)) if kind == "pdf" else None
        if meta:
            cleaned = strip_site_name(meta)
        else:
            prefix = f"{economy} "
            if cleaned.upper().startswith(prefix) and len(cleaned) > len(prefix):
                cleaned = cleaned[len(prefix):].strip()
    elif kind == "html":
        # A page's <title> carries the site's name; a statute's own heading
        # line does not, and a dash inside one is part of the name.
        cleaned = strip_site_name(cleaned)
    return normalise_case(cleaned) or title
