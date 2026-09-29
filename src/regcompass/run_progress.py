"""The Run's Steps, reported through one hook its caller supplies.

A Run prints its free-text lines for a person to read. Beside them it tells a
RunProgress what it is doing in a shape a program can follow: which Step of
which Document started, finished with what counts, how far Map has got, and
where a failure happened. The pipeline knows nothing about who listens: the
command line supplies no hook, the server's Run manager supplies one and turns
the calls into whatever its screen needs.

Every method is a no-op here, so a caller overrides only what it wants and a
Run with no hook at all behaves exactly as before.
"""

from __future__ import annotations

# The Steps, as the interface names them. Stable strings: a recorded Run is
# read back by them. Per Document in this order, then Reconcile once per Run.
READ = "read"
SCAN_CHECK = "scan_check"
CUT = "cut"
GATE = "gate"
MAP = "map"
PROVE = "prove"
GLOSS = "gloss"
RECONCILE = "reconcile"

DOCUMENT_STEPS = (READ, SCAN_CHECK, CUT, GATE, MAP, PROVE, GLOSS)
STEPS = DOCUMENT_STEPS + (RECONCILE,)

# What happened to one Candidate (one Piece and Indicator pair the Gate kept).
# Stable strings, like the Step names.
MAPPED = "mapped"  # the Engine picked a quote and it was proven word for word
NOT_APPLICABLE = "not_applicable"  # the Engine said the Piece does not answer it
DROPPED_BY_PROOF = "dropped_by_proof"  # a quote was picked but never proven
SKIPPED = "skipped"  # the call never came back (rate limit or transport)
CANDIDATE_OUTCOMES = (MAPPED, NOT_APPLICABLE, DROPPED_BY_PROOF, SKIPPED)

# How the Gate judged a Document: by meaning and keywords together, or by
# meaning alone (a language the keyword tier cannot read).
MEANING_AND_KEYWORDS = "meaning_and_keywords"
MEANING_ONLY = "meaning_only"


class RunProgress:
    """Receives a Run's Steps. Reconcile belongs to no one Document, so its
    document_id is None. Calls arrive on the Run's own thread, in order."""

    def step_started(self, document_id: str | None, step: str) -> None:
        pass

    def step_finished(self, document_id: str | None, step: str, counts: dict) -> None:
        pass

    def map_progress(self, document_id: str, done: int, total: int) -> None:
        pass

    def scan_flagged(self, document_id: str, reason: str) -> None:
        """The Scan check found this Document's text unreliable (a scan read
        with low confidence). The Run carries on and still maps it."""
        pass

    def candidate(
        self,
        document_id: str,
        piece_id: str,
        indicator: str,
        cosine: float,
        bm25: float,
        lane: str,
        outcome: str,
        section: str | None = None,
        page: int | None = None,
    ) -> None:
        """One Candidate is settled: the Gate's two scores for it (closeness
        of meaning to the Pillar, keyword score for the Indicator), the lane
        the Gate judged it in, and its outcome (one of CANDIDATE_OUTCOMES).
        Called once per Candidate, in Candidate order, during Map."""
        pass

    def mapping_added(
        self, document_id: str, mapping_id: str, indicator: str, page: int | None
    ) -> None:
        """A proven Mapping is saved and can be opened: its id and the page
        its Verbatim Quote is on."""
        pass

    def failed(self, document_id: str | None, step: str | None, message: str) -> None:
        pass


class StepTracker(RunProgress):
    """Passes every call on to the caller's hook and remembers where the Run
    is, so a failure can be reported against the Document and Step it
    happened in. The Step is the last one started: a failure in the
    bookkeeping just after a Step finished is reported against that Step."""

    def __init__(self, hook: RunProgress | None) -> None:
        self.hook = hook if hook is not None else RunProgress()
        self.document_id: str | None = None
        self.step: str | None = None

    def step_started(self, document_id: str | None, step: str) -> None:
        self.document_id, self.step = document_id, step
        self.hook.step_started(document_id, step)

    def step_finished(self, document_id: str | None, step: str, counts: dict) -> None:
        self.hook.step_finished(document_id, step, counts)

    def map_progress(self, document_id: str, done: int, total: int) -> None:
        self.hook.map_progress(document_id, done, total)

    def scan_flagged(self, document_id: str, reason: str) -> None:
        self.hook.scan_flagged(document_id, reason)

    def candidate(self, document_id: str, piece_id: str, indicator: str, cosine: float,
                  bm25: float, lane: str, outcome: str, section: str | None = None,
                  page: int | None = None) -> None:
        self.hook.candidate(
            document_id, piece_id, indicator, cosine, bm25, lane, outcome,
            section=section, page=page,
        )

    def mapping_added(
        self, document_id: str, mapping_id: str, indicator: str, page: int | None
    ) -> None:
        self.hook.mapping_added(document_id, mapping_id, indicator, page)

    def failed(self, document_id: str | None, step: str | None, message: str) -> None:
        self.hook.failed(document_id, step, message)

    def failed_here(self, exc: BaseException) -> None:
        self.failed(self.document_id, self.step, f"{type(exc).__name__}: {exc}")
