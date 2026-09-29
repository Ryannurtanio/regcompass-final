"""Discovery's progress, reported through one hook its caller supplies.

Discovery prints free-text lines for a person to read. Beside them it tells a
DiscoveryProgress what it is doing in a shape a program can follow: which
Portal it reads, each Document it found there, fetched, added to the Corpus or
skipped (with the reason in plain words), and how it ended. Discovery knows
nothing about who listens: the command line supplies no hook, the server's Run
manager supplies one and turns the calls into named events on its stream:

    discovery_portal    economy, name, hosts, strategy, refresh, run_id
    discovery_found     url, name
    discovery_fetched   url, size_bytes, method
    discovery_added     url, document_id, title, n_pages, ocr_applied
    discovery_skipped   url, code, reason, title (the Corpus title, when known)
    discovery_finished  counts
    discovery_failed    message

Every method is a no-op here, so a caller overrides only what it wants and a
Discovery with no hook at all behaves exactly as before.
"""

from __future__ import annotations

DISCOVERY_EVENT_NAMES = (
    "discovery_portal",
    "discovery_found",
    "discovery_fetched",
    "discovery_added",
    "discovery_skipped",
    "discovery_finished",
    "discovery_failed",
)
DISCOVERY_FINAL_EVENTS = frozenset({"discovery_finished", "discovery_failed"})

# Why a Document was not added, in the words the screen shows. The code is the
# stable name a recorded stream is read back by; the sentence is for a person.
# http_error and fetch_error carry what the Portal answered, so they are built
# by skip_reason() rather than read from here as they stand.
SKIP_REASONS = {
    "in_corpus": "Already in the Corpus, so it was not asked for again.",
    "off_whitelist": (
        "Its address is not on the Portal's list of official hosts, so it was"
        " not fetched."
    ),
    "robots": "The Portal's robots.txt asks crawlers not to fetch it, so it was not fetched.",
    "http_error": "The Portal did not send the file.",
    "fetch_error": "The request for the file failed.",
    "duplicate": (
        "The same file as another address already fetched, so it is kept once."
    ),
    "unchanged": (
        "Fetched again, and the file matches the Document already in the"
        " Corpus, so nothing changed."
    ),
    "not_added": "Fetched, but its text could not be read, so it was not added.",
    "limit": "Not fetched: this Discovery was limited to fewer Documents.",
    "failed_before": "Failed on an earlier Discovery; not asked again.",
    "not_fetched": "Not fetched in this Discovery.",
}

# How an earlier refusal by robots.txt is recorded on the address's row.
_ROBOTS_MARK = "robots.txt disallows"


def earlier_failure(error: str | None) -> str:
    """The plain sentence for an address that failed on an earlier Discovery
    and is not asked for again, naming what went wrong when that is known."""
    if error and error.startswith(_ROBOTS_MARK):
        return (
            "Refused by the Portal's robots.txt on an earlier Discovery;"
            " not asked again."
        )
    if error:
        cause = error.split(":", 1)[0] if not error.startswith("HTTP ") else error
        return f"Failed on an earlier Discovery ({cause}); not asked again."
    return SKIP_REASONS["failed_before"]


def skip_reason(code: str, detail: str | None = None) -> str:
    """The plain sentence for a skip, with what the Portal answered where
    that is the whole reason (an HTTP status, a dropped connection)."""
    if code == "http_error" and detail:
        return f"The Portal answered {detail}, so nothing was saved."
    if code == "fetch_error" and detail:
        return f"The request for the file failed ({detail}), so nothing was saved."
    if code == "not_added" and detail:
        return f"Fetched, but its text could not be read ({detail}), so it was not added."
    return SKIP_REASONS[code]


class DiscoveryProgress:
    """Receives a Discovery's progress. Calls arrive on the Discovery's own
    thread, in order: portal first, then found, fetched, added and skipped as
    they happen, then exactly one of finished or failed."""

    def portal(
        self, *, economy: str, name: str, hosts: list[str], strategy: str,
        refresh: bool, run_id: str,
    ) -> None:
        pass

    def found(self, url: str, name: str | None) -> None:
        pass

    def fetched(self, url: str, size_bytes: int, method: str) -> None:
        pass

    def added(
        self, url: str, document_id: str, title: str, n_pages: int, ocr_applied: bool
    ) -> None:
        pass

    def skipped(
        self, url: str, code: str, reason: str, title: str | None = None
    ) -> None:
        pass

    def finished(self, counts: dict) -> None:
        pass

    def failed(self, message: str) -> None:
        pass
