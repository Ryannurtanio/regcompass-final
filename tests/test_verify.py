"""M7 verify: the mechanical gate that makes zero hallucination a code property.
Checks are stdlib string/regex only: (1) verbatim_quote is a byte-for-byte
substring of the chunk text; (2) every cited subsection component actually
appears in the chunk. Forced failures that MUST be rejected:
a mutated quote (one char), a paraphrase, normalized whitespace, a fabricated
subsection, and an empty/trivial quote. Failure triggers the stricter-retry
escalation (a re-map with mechanical feedback), extraction_attempts max 3
total, then a logged DROP; the escalation is observable in the audit log.

The offline golden sweep proves every mapped golden M6 record's quote passes
the byte check, and names the exact five pre-verify subsection offenders the
M7 escalation repairs or drops; the live golden M7 generation
(scripts/make_golden_m7.py) produces the final verified records for M8. The
shipped sweep then re-verifies every reconciled (M8) and shipped (M9) record
against its own chunk, so a silent regression in shipped output is impossible.
"""

from __future__ import annotations

import gzip
import json
from functools import lru_cache
from pathlib import Path

import pytest

from regcompass.contracts import (
    CanonicalText,
    Chunk,
    GatedChunk,
    MappingRecord,
    PipelineConfig,
)
from regcompass.storage import Storage
from regcompass.verify import VerifyOutcome, verify_record, verify_with_retry

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_M4 = ROOT / "tests/golden/m4"
GOLDEN_M5 = ROOT / "tests/golden/m5"
GOLDEN_M6 = ROOT / "tests/golden/m6"
GOLDEN_M7 = ROOT / "tests/golden/m7"
GOLDEN_M8 = ROOT / "tests/golden/m8"
GOLDEN_M9 = ROOT / "tests/golden/m9"

SLUGS = [
    "sg_telecommunications_act_1999",
    "my_personal_data_protection_act_2010",
    "au_C2026C00098VOL01",
]

# ---------------------------------------------------------------------------
# offline fixtures (mirrors test_map's tiny provision)
# ---------------------------------------------------------------------------

LAW_TEXT = (
    "Section 12. Transfer of customer data abroad\n"
    "(1) A licensee must not transfer customer data to a place outside the\n"
    "economy unless the customer has given express consent in writing.\n"
    "(2) The Authority may exempt any class of transfers from subsection (1).\n"
)
GOOD_QUOTE = (
    "must not transfer customer data to a place outside the\n"
    "economy unless the customer has given express consent in writing"
)


def tiny_chunk() -> Chunk:
    return Chunk(
        chunk_id="doc_test:c0001",
        document_id="doc_test",
        char_start=0,
        char_end=len(LAW_TEXT),
        text=LAW_TEXT,
        section_label="s. 12",
        chunk_kind="section",
        page_start=3,
    )


def tiny_gated(indicator: str = "6.4") -> GatedChunk:
    return GatedChunk(
        chunk=tiny_chunk(),
        indicator_id=indicator,  # type: ignore[arg-type]
        cosine_pillar=0.5,
        bm25_indicator=1.0,
        gate_decision="passed",
    )


def make_record(
    quote: str = GOOD_QUOTE,
    subsection: str | None = "(1)",
    insufficient: bool = False,
    attempts: int = 1,
) -> MappingRecord:
    return MappingRecord(
        mapping_id="doc_test:c0001::6.4",
        document_id="doc_test",
        chunk_id="doc_test:c0001",
        economy="SG",
        indicator_id="6.4",
        indicator_name="Conditional flow regimes",
        section="s. 12",
        subsection=subsection,
        verbatim_quote=quote,
        insufficient_evidence=insufficient,
        extraction_attempts=attempts,
    )


def selection(maps: bool, quote: str = "", subsection=None, impact=None) -> str:
    return json.dumps(
        {
            "maps_to_indicator": maps,
            "verbatim_quote": quote,
            "subsection": subsection,
            "impact": impact,
        }
    )


def scripted(responses: list[str]):
    calls: list[tuple[str, bool]] = []

    def fn(prompt: str, strict: bool) -> str:
        calls.append((prompt, strict))
        return responses[min(len(calls) - 1, len(responses) - 1)]

    fn.calls = calls  # type: ignore[attr-defined]
    return fn


# ---------------------------------------------------------------------------
# pure checks: the forced-failure suite
# ---------------------------------------------------------------------------


class TestVerifyRecord:
    def test_clean_record_passes(self):
        assert verify_record(make_record(), tiny_chunk()) == []

    def test_mutated_quote_one_char_fails(self):
        bad = make_record(quote=GOOD_QUOTE.replace("consent", "consnet"))
        assert any("byte-for-byte" in f for f in verify_record(bad, tiny_chunk()))

    def test_paraphrased_quote_fails(self):
        bad = make_record(quote="licensees are forbidden from sending customer data overseas")
        assert verify_record(bad, tiny_chunk()) != []

    def test_normalized_whitespace_fails(self):
        # byte-for-byte means byte-for-byte: a reflowed newline is a failure
        bad = make_record(quote=" ".join(GOOD_QUOTE.split()))
        assert any("byte-for-byte" in f for f in verify_record(bad, tiny_chunk()))

    def test_empty_quote_cannot_even_construct(self):
        # the contract layer already refuses a substantive record with no quote
        with pytest.raises(Exception, match="verbatim_quote"):
            make_record(quote="")

    def test_trivial_quotes_fail(self):
        for q in ("   ", "must not", "a\nb"):
            bad = make_record(quote=q)
            assert any("trivial" in f or "short" in f for f in verify_record(bad, tiny_chunk())), q

    def test_fabricated_subsection_fails(self):
        bad = make_record(subsection="(9)")
        fails = verify_record(bad, tiny_chunk())
        assert any("(9)" in f and "subsection" in f for f in fails)

    def test_unparseable_subsection_fails(self):
        bad = make_record(subsection="133.1")
        assert any("unparseable" in f for f in verify_record(bad, tiny_chunk()))

    def test_nested_subsection_components_all_checked(self):
        assert verify_record(make_record(subsection="(2)(1)"), tiny_chunk()) == []
        bad = make_record(subsection="(2)(z)")
        assert any("(z)" in f for f in verify_record(bad, tiny_chunk()))

    def test_null_subsection_is_fine(self):
        assert verify_record(make_record(subsection=None), tiny_chunk()) == []

    def test_multiple_failures_all_reported(self):
        bad = make_record(quote="paraphrased words entirely", subsection="(9)")
        fails = verify_record(bad, tiny_chunk())
        assert len(fails) == 2

    def test_insufficient_evidence_record_is_a_caller_error(self):
        with pytest.raises(ValueError, match="insufficient"):
            verify_record(make_record(quote="", insufficient=True), tiny_chunk())

    def test_mismatched_chunk_is_a_caller_error(self):
        other = tiny_chunk().model_copy(update={"chunk_id": "doc_test:c0999"})
        with pytest.raises(ValueError, match="chunk"):
            verify_record(make_record(), other)

    def test_canonical_slice_corruption_raises(self):
        canonical = CanonicalText(
            document_id="doc_test",
            source_sha256="0" * 64,
            extractor="test",
            extractor_version="0",
            full_text="X" * len(LAW_TEXT),
        )
        with pytest.raises(ValueError, match="slice"):
            verify_record(make_record(), tiny_chunk(), canonical=canonical)

    def test_canonical_slice_identity_accepted(self):
        canonical = CanonicalText(
            document_id="doc_test",
            source_sha256="0" * 64,
            extractor="test",
            extractor_version="0",
            full_text=LAW_TEXT,
        )
        assert verify_record(make_record(), tiny_chunk(), canonical=canonical) == []


# ---------------------------------------------------------------------------
# retry escalation
# ---------------------------------------------------------------------------


class TestRetryEscalation:
    def test_clean_record_passes_without_any_model_call(self):
        fn = scripted(["MUST NOT BE CALLED"])
        out = verify_with_retry(make_record(), tiny_gated(), "SG", completion_fn=fn)
        assert out.outcome == "passed"
        assert out.record.verification_status == "passed"
        assert out.attempts == 1
        assert fn.calls == []

    def test_bad_subsection_retries_with_feedback_then_passes(self):
        fn = scripted([selection(True, GOOD_QUOTE, "(1)", "x")])
        out = verify_with_retry(
            make_record(subsection="(9)"), tiny_gated(), "SG", completion_fn=fn
        )
        assert out.outcome == "passed"
        assert out.record.subsection == "(1)"
        assert out.record.verification_status == "passed"
        assert out.attempts == 2
        assert out.record.extraction_attempts == 2
        prompt, strict = fn.calls[0]
        assert strict is True
        assert "(9)" in prompt  # the failure feedback names the bogus reference
        assert "not present" in prompt

    def test_bad_quote_retry_hint_asks_for_shorter_span(self):
        fn = scripted([selection(True, GOOD_QUOTE, "(1)", "x")])
        out = verify_with_retry(
            make_record(quote=" ".join(GOOD_QUOTE.split())), tiny_gated(), "SG", completion_fn=fn
        )
        assert out.outcome == "passed"
        assert "SHORTER" in fn.calls[0][0]

    def test_budget_already_exhausted_drops_without_model_call(self):
        fn = scripted(["MUST NOT BE CALLED"])
        out = verify_with_retry(
            make_record(subsection="(9)", attempts=3), tiny_gated(), "SG", completion_fn=fn
        )
        assert out.outcome == "dropped"
        assert out.record.verification_status == "dropped"
        assert out.attempts == 3
        assert fn.calls == []

    def test_persistent_failure_drops_after_three_total_attempts(self):
        fn = scripted([selection(True, GOOD_QUOTE, "(9)", "x")])  # keeps citing (9)
        out = verify_with_retry(
            make_record(subsection="(9)"), tiny_gated(), "SG", completion_fn=fn
        )
        assert out.outcome == "dropped"
        assert out.attempts == 3
        assert len(fn.calls) == 2  # attempts 2 and 3
        assert len(out.failures) >= 3

    def test_retry_returning_no_evidence_is_final(self):
        fn = scripted([selection(False)])
        out = verify_with_retry(
            make_record(subsection="(9)"), tiny_gated(), "SG", completion_fn=fn
        )
        assert out.outcome == "no_evidence"
        assert out.record.insufficient_evidence is True
        assert out.record.verification_status == "unverified"

    def test_malformed_retries_consume_budget_then_drop(self):
        fn = scripted(["garbage", "more garbage"])
        out = verify_with_retry(
            make_record(subsection="(9)"), tiny_gated(), "SG", completion_fn=fn
        )
        assert out.outcome == "dropped"
        assert out.attempts == 3

    def test_insufficient_evidence_record_passes_through(self):
        fn = scripted(["MUST NOT BE CALLED"])
        out = verify_with_retry(
            make_record(quote="", insufficient=True), tiny_gated(), "SG", completion_fn=fn
        )
        assert out.outcome == "no_evidence"
        assert out.record.verification_status == "unverified"
        assert fn.calls == []


# ---------------------------------------------------------------------------
# subsection markers in other scripts and styles
# ---------------------------------------------------------------------------

ZH_TEXT = (
    "第十一条 关键信息基础设施的运营者应当履行下列安全保护义务：\n"
    "（一）设置专门安全管理机构和安全管理负责人；\n"
    "（十二）法律、行政法规规定的其他义务。\n"
)
TH_TEXT = (
    "มาตรา ๒๖ ผู้ควบคุมข้อมูลส่วนบุคคลต้องดำเนินการดังต่อไปนี้\n"
    "(๑) จัดให้มีมาตรการรักษาความมั่นคงปลอดภัยที่เหมาะสม\n"
)
RU_TEXT = (
    "Статья 18. Обязанности оператора при сборе персональных данных\n"
    "а) обеспечить запись, систематизацию и хранение данных\n"
)
ID_TEXT = (
    "Pasal 20\n"
    "Pemrosesan Data Pribadi dilakukan berdasarkan:\n"
    "a. persetujuan yang sah secara eksplisit dari Subjek Data Pribadi;\n"
    "1) pemenuhan kewajiban perjanjian dengan Subjek Data Pribadi.\n"
)


def text_chunk(text: str) -> Chunk:
    return Chunk(
        chunk_id="doc_test:c0001",
        document_id="doc_test",
        char_start=0,
        char_end=len(text),
        text=text,
        section_label="s. 1",
        chunk_kind="section",
    )


def text_record(quote: str, subsection: str) -> MappingRecord:
    return make_record(quote=quote, subsection=subsection)


ZH_QUOTE = "设置专门安全管理机构和安全管理负责人"
TH_QUOTE = "จัดให้มีมาตรการรักษาความมั่นคงปลอดภัยที่เหมาะสม"
RU_QUOTE = "обеспечить запись, систематизацию и хранение данных"
ID_QUOTE = "persetujuan yang sah secara eksplisit dari Subjek Data Pribadi"


class TestUnicodeSubsections:
    """A quote that really is in the text keeps its Mapping when the cited
    subsection is written the way the law writes it: full-width brackets,
    Chinese numerals, Thai digits, Cyrillic letters, or the "a)" and "a."
    styles. The quote check itself stays byte-exact."""

    @pytest.mark.parametrize(
        "text, quote, subsection",
        [
            (ZH_TEXT, ZH_QUOTE, "第十一条（一）"),
            (ZH_TEXT, ZH_QUOTE, "（一）"),
            (ZH_TEXT, ZH_QUOTE, "(1)"),
            (ZH_TEXT, ZH_QUOTE, "(12)"),
            (TH_TEXT, TH_QUOTE, "(๑)"),
            (TH_TEXT, TH_QUOTE, "(1)"),
            (RU_TEXT, RU_QUOTE, "(а)"),
            (RU_TEXT, RU_QUOTE, "а)"),
            (ID_TEXT, ID_QUOTE, "a)"),
            (ID_TEXT, ID_QUOTE, "a."),
            (ID_TEXT, ID_QUOTE, "(a)"),
            (ID_TEXT, ID_QUOTE, "1)"),
            (ID_TEXT, ID_QUOTE, "Ayat (1)"),
        ],
        ids=[
            "zh-article-and-item", "zh-item", "zh-as-digit", "zh-twelve", "th-digit",
            "th-as-digit", "ru-bracketed", "ru-paren", "id-a-paren", "id-a-dot",
            "id-a-bracketed", "id-1-paren", "id-ayat",
        ],
    )
    def test_marker_written_in_the_law_s_own_style_is_accepted(self, text, quote, subsection):
        assert verify_record(text_record(quote, subsection), text_chunk(text)) == []

    @pytest.mark.parametrize(
        "text, quote, subsection, missing",
        [
            (ZH_TEXT, ZH_QUOTE, "第十一条（二）", "（二）"),
            (ZH_TEXT, ZH_QUOTE, "(2)", "(2)"),
            (TH_TEXT, TH_QUOTE, "(๒)", "(๒)"),
            (RU_TEXT, RU_QUOTE, "(б)", "(б)"),
            (ID_TEXT, ID_QUOTE, "b.", "b."),
            (ID_TEXT, ID_QUOTE, "2)", "2)"),
        ],
        ids=["zh-item", "zh-as-digit", "th-digit", "ru-letter", "id-b-dot", "id-2-paren"],
    )
    def test_marker_truly_absent_from_the_chunk_is_rejected(self, text, quote, subsection, missing):
        fails = verify_record(text_record(quote, subsection), text_chunk(text))
        assert fails == [f"subsection component(s) {missing} not present in the chunk"]

    def test_letters_keep_their_case(self):
        fails = verify_record(text_record(ID_QUOTE, "A."), text_chunk(ID_TEXT))
        assert any("subsection" in f for f in fails)

    def test_a_letter_ending_a_word_is_not_a_marker(self):
        # "Pribadi." ends in "i." but no line carries an "i." marker
        fails = verify_record(text_record(ID_QUOTE, "i."), text_chunk(ID_TEXT))
        assert any("subsection" in f for f in fails)

    def test_digit_like_non_decimal_marker_does_not_crash(self):
        text = ZH_TEXT + "(፩) 附则。\n"
        assert verify_record(text_record(ZH_QUOTE, "(1)"), text_chunk(text)) == []
        assert verify_record(text_record(ZH_QUOTE, "(፩)"), text_chunk(text)) == []

    @pytest.mark.parametrize("subsection", ["", "   "])
    def test_empty_subsection_means_none_cited(self, subsection):
        assert verify_record(text_record(ZH_QUOTE, subsection), text_chunk(ZH_TEXT)) == []

    def test_decimal_numbers_stay_unparseable(self):
        fails = verify_record(text_record(ZH_QUOTE, "133.1"), text_chunk(ZH_TEXT))
        assert fails == ["unparseable subsection reference '133.1'"]

    def test_quote_check_stays_exact_with_a_good_marker(self):
        # full-width punctuation is NOT normalised for the quote
        fails = verify_record(
            text_record("设置专门安全管理机构和安全管理负责人;", "（一）"), text_chunk(ZH_TEXT)
        )
        assert fails == ["quote is not a byte-for-byte substring of the chunk text"]


class TestReverifyScript:
    """scripts/reverify_mappings.py re-checks the stored Mappings of a database
    copy with today's rules and reports, per Economy and Engine, how many
    previously dropped Mappings now pass. It only reads: the file is unchanged."""

    def _db(self, tmp_path) -> Path:
        path = tmp_path / "copy.db"
        s = Storage(path)
        s.apply_schema()
        s.upsert_document("doc_cn", "CN", "sha", full_text=ZH_TEXT)
        s.upsert_chunks([text_chunk(ZH_TEXT).model_copy(update={"document_id": "doc_cn",
                                                                "chunk_id": "doc_cn:c0001"})])
        s.run_start(run_id="run_b", kind="run", economy="CN", pillars=[6], indicators=None,
                    engine="engine-b", started_at="2026-09-23T00:00:00+00:00")

        def rec(mid: str, subsection: str, status: str) -> MappingRecord:
            return make_record(quote=ZH_QUOTE, subsection=subsection).model_copy(update={
                "mapping_id": mid, "document_id": "doc_cn", "chunk_id": "doc_cn:c0001",
                "economy": "CN", "verification_status": status,
            })

        s.upsert_mappings([
            rec("m1", "(1)", "dropped"),       # now passes: （一） is in the chunk
            rec("m2", "（一）", "dropped"),     # now passes
            rec("m3", "(7)", "dropped"),       # truly absent: still dropped
            rec("m4", "（十二）", "passed"),     # already passed, still passes
        ], run_id="run_b")
        s.close()
        return path

    def test_reports_recovered_drops_per_economy_and_engine(self, tmp_path):
        import subprocess
        import sys

        db = self._db(tmp_path)
        before = db.read_bytes()
        out = subprocess.run(
            [sys.executable, str(ROOT / "scripts/reverify_mappings.py"), str(db), "--json"],
            capture_output=True, text=True, check=True,
        )
        rows = json.loads(out.stdout)
        assert rows == [{
            "economy": "CN", "engine": "engine-b", "runs": 1,
            "dropped": 3, "now_pass": 2, "still_dropped": 1,
            "passed": 1, "passed_now_fail": 0,
        }]
        assert db.read_bytes() == before


# ---------------------------------------------------------------------------
# audit observability
# ---------------------------------------------------------------------------


class TestAuditLog:
    def _storage(self, tmp_path) -> Storage:
        s = Storage(tmp_path / "audit.db")
        s.apply_schema()
        return s

    def _decisions(self, s: Storage) -> list[str]:
        return [
            r["decision"]
            for r in s.conn.execute(
                "SELECT decision FROM audit_log WHERE stage = 'm7_verify' ORDER BY rowid"
            )
        ]

    def test_pass_is_logged(self, tmp_path):
        s = self._storage(tmp_path)
        verify_with_retry(make_record(), tiny_gated(), "SG", completion_fn=scripted([]), storage=s)
        assert any("passed" in d for d in self._decisions(s))

    def test_retry_escalation_is_observable(self, tmp_path):
        s = self._storage(tmp_path)
        fn = scripted([selection(True, GOOD_QUOTE, "(9)", "x")])
        verify_with_retry(
            make_record(subsection="(9)"), tiny_gated(), "SG", completion_fn=fn, storage=s
        )
        ds = self._decisions(s)
        assert any(d.startswith("retry attempt 2") for d in ds)
        assert any(d.startswith("retry attempt 3") for d in ds)
        assert any(d.startswith("dropped after 3") for d in ds)


# ---------------------------------------------------------------------------
# golden sweep: every mapped golden M6 quote passes the byte check (offline)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=None)
def _chunks_by_pair(slug: str) -> dict[tuple[str, str], Chunk]:
    with gzip.open(GOLDEN_M5 / f"{slug}.gated.json.gz", "rt", encoding="utf-8") as f:
        return {
            (r["chunk"]["chunk_id"], r["indicator_id"]): Chunk.model_validate(r["chunk"])
            for r in json.load(f)["passed"]
        }


# The exact m6 pre-verify offenders, frozen by mapping_id and failure text.
# m6 is RAW mapper output: these five records are the committed evidence that
# the M7 stricter-retry escalation works (four are repaired to a marker that is
# really in the chunk, the fifth is dropped). Repairing them in the fixture
# would delete that evidence, so the expectation is exact rather than a
# tolerance: a sixth offender fails this test, and so does a repaired fixture.
M6_KNOWN_OFFENDERS = {
    "doc_sg_telecommunications_act_1999:c0066::7.3": [
        "subsection component(s) (1) not present in the chunk"
    ],
    "doc_my_personal_data_protection_act_2010:c0122::7.5": [
        "subsection component(s) (1) not present in the chunk"
    ],
    "doc_au_C2026C00098VOL01:c0202::7.2": [
        "subsection component(s) (1) not present in the chunk"
    ],
    "doc_au_C2026C00098VOL01:c0203::7.2": [
        "subsection component(s) (1) not present in the chunk"
    ],
    "doc_au_C2026C00098VOL01:c0490::7.2": ["unparseable subsection reference '133.1'"],
}


def _m6_outcomes(slug: str) -> list[dict]:
    with gzip.open(GOLDEN_M6 / f"{slug}.mapped.json.gz", "rt", encoding="utf-8") as f:
        return json.load(f)["outcomes"]


def _m7_outcomes(slug: str) -> list[dict]:
    with gzip.open(GOLDEN_M7 / f"{slug}.verified.json.gz", "rt", encoding="utf-8") as f:
        return json.load(f)["outcomes"]


class TestGoldenSweep:
    def test_every_mapped_golden_quote_is_byte_verified(self):
        """m6 is pre-verify mapper output. Every quote must already pass the
        byte check (M6's anchoring guarantees that); subsection hygiene is M7's
        retry problem, and the offenders it repairs or drops are named exactly."""
        checked = quote_failures = 0
        offenders: dict[str, list[str]] = {}
        for slug in SLUGS:
            chunks = _chunks_by_pair(slug)
            for o in _m6_outcomes(slug):
                if o["outcome"] != "mapped":
                    continue
                record = MappingRecord.model_validate(o["record"])
                fails = verify_record(record, chunks[(o["chunk_id"], o["indicator_id"])])
                checked += 1
                quote_failures += sum(1 for f in fails if "byte" in f or "short" in f)
                if fails:
                    offenders[record.mapping_id] = fails
        assert checked >= 90
        assert quote_failures == 0
        assert offenders == M6_KNOWN_OFFENDERS

    def test_m6_offenders_are_all_repaired_or_dropped_in_m7(self):
        """Ties the fixture to the behaviour it proves: each named offender is
        either passed in m7 with a subsection marker that really appears in its
        chunk, or dropped after the attempt budget is exhausted."""
        seen: dict[str, str] = {}
        for slug in SLUGS:
            chunks = _chunks_by_pair(slug)
            for o in _m7_outcomes(slug):
                mapping_id = (o["record"] or {}).get("mapping_id")
                if mapping_id not in M6_KNOWN_OFFENDERS:
                    continue
                seen[mapping_id] = o["outcome"]
                if o["outcome"] == "passed":
                    record = MappingRecord.model_validate(o["record"])
                    chunk = chunks[(o["chunk_id"], o["indicator_id"])]
                    assert record.subsection == "(a)"
                    assert record.subsection in chunk.text
                    assert verify_record(record, chunk) == []
                    assert record.extraction_attempts > 1  # it took a retry
                else:
                    assert o["outcome"] == "dropped"
        assert set(seen) == set(M6_KNOWN_OFFENDERS)
        # the unparseable reference can never be repaired, so it is the drop
        assert seen["doc_au_C2026C00098VOL01:c0490::7.2"] == "dropped"
        assert sorted(k for k, v in seen.items() if v == "passed") == sorted(
            set(M6_KNOWN_OFFENDERS) - {"doc_au_C2026C00098VOL01:c0490::7.2"}
        )


# Vacuity guard for the shipped sweeps. Observed passed/reconciled/shipped
# record counts per slug are SG 25, MY 46, AU 22 at every one of m7, m8 and m9;
# 20 is that floor rounded down, so a truncated or empty golden fails loudly
# instead of sweeping nothing and reporting green.
MIN_RECORDS_PER_SLUG = 20
MIN_RECORDS_PER_STAGE = 90


# NOT marked slow on purpose: this is the gate that makes a silent regression in
# shipped output impossible, so it has to run on a plain `pytest -q`. It reads
# three small gzipped fixtures and finishes in well under a second.
class TestGoldenM7:
    """The verified golden records (produced live by scripts/make_golden_m7.py)
    are the pipeline's shipped output for M8: 100% mechanically verified."""

    def test_golden_m7_records_all_pass(self):
        golden = GOLDEN_M7
        if not golden.exists():
            pytest.skip("golden m7 not generated yet")
        total = 0
        for slug in SLUGS:
            chunks = _chunks_by_pair(slug)
            with gzip.open(golden / f"{slug}.verified.json.gz", "rt", encoding="utf-8") as f:
                payload = json.load(f)
            passed = 0
            for o in payload["outcomes"]:
                record = MappingRecord.model_validate(o["record"]) if o["record"] else None
                if o["outcome"] == "passed":
                    assert record is not None
                    assert record.verification_status == "passed"
                    assert verify_record(record, chunks[(o["chunk_id"], o["indicator_id"])]) == []
                    passed += 1
                elif o["outcome"] == "no_evidence":
                    assert record is not None and record.insufficient_evidence
                else:
                    assert o["outcome"] == "dropped"
            assert passed >= MIN_RECORDS_PER_SLUG, (slug, passed)
            total += passed
        assert total >= MIN_RECORDS_PER_STAGE


# ---------------------------------------------------------------------------
# shipped-record sweep: every reconciled (m8) and shipped (m9) record verifies
# ---------------------------------------------------------------------------


@lru_cache(maxsize=None)
def _chunks_by_id(slug: str) -> dict[str, Chunk]:
    """M8 and M9 records carry no indicator-keyed gate payload, so their chunks
    come from the M4 chunk set, keyed by chunk_id alone."""
    with gzip.open(GOLDEN_M4 / f"{slug}.chunks.json.gz", "rt", encoding="utf-8") as f:
        payload = json.load(f)
    chunks = payload["chunks"] if isinstance(payload, dict) else payload
    return {c["chunk_id"]: Chunk.model_validate(c) for c in chunks}


def _slug_for_document(document_id: str) -> str:
    slug = document_id.removeprefix("doc_")
    assert slug in SLUGS, document_id
    return slug


class TestShippedGoldenSweep:
    """m6 and m7 are pipeline checkpoints; m8 (reconciled) and m9 (the shipped
    mapping JSON the judges read) are the product. Every substantive record in
    both must still verify byte-for-byte against its own chunk, or the zero
    hallucination claim is not true of what actually shipped."""

    def test_every_reconciled_m8_record_verifies(self):
        total = 0
        for slug in SLUGS:
            chunks = _chunks_by_id(slug)
            with gzip.open(GOLDEN_M8 / f"{slug}.reconciled.json.gz", "rt", encoding="utf-8") as f:
                records = json.load(f)["records"]
            checked = 0
            for r in records:
                record = MappingRecord.model_validate(r)
                if record.insufficient_evidence:
                    continue
                assert verify_record(record, chunks[record.chunk_id]) == [], record.mapping_id
                checked += 1
            assert checked >= MIN_RECORDS_PER_SLUG, (slug, checked)
            total += checked
        assert total >= MIN_RECORDS_PER_STAGE

    def test_the_shipped_sg_7_3_record_verifies(self):
        """The named verification case, by mapping_id. The m6 fixture for this pair
        cites a subsection '(1)' that is not in its chunk; the record that
        actually shipped cites '(a)', which is, and it verifies clean."""
        mapping_id = "doc_sg_telecommunications_act_1999:c0066::7.3"
        payload = json.loads(
            (GOLDEN_M9 / "mapping_sg_sg_telecommunications_act_1999_7.json").read_text(
                encoding="utf-8"
            )
        )
        shipped = [m for m in payload["mappings"] if m["mapping_id"] == mapping_id]
        assert len(shipped) == 1, mapping_id
        record = MappingRecord.model_validate(shipped[0])
        chunk = _chunks_by_id("sg_telecommunications_act_1999")[record.chunk_id]
        assert record.subsection == "(a)"
        assert record.subsection in chunk.text
        assert record.verbatim_quote in chunk.text
        assert verify_record(record, chunk) == []

    def test_every_shipped_m9_record_verifies(self):
        by_economy: dict[str, int] = {}
        for path in sorted(GOLDEN_M9.glob("mapping_*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            for m in payload["mappings"]:
                record = MappingRecord.model_validate(m)
                if record.insufficient_evidence:
                    continue
                chunks = _chunks_by_id(_slug_for_document(record.document_id))
                assert verify_record(record, chunks[record.chunk_id]) == [], record.mapping_id
                by_economy[record.economy] = by_economy.get(record.economy, 0) + 1
        assert sorted(by_economy) == ["AU", "MY", "SG"]
        assert min(by_economy.values()) >= MIN_RECORDS_PER_SLUG, by_economy
        assert sum(by_economy.values()) >= MIN_RECORDS_PER_STAGE
