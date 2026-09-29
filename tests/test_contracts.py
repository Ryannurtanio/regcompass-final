"""Contract round-trip and invariant tests (S0 exit criteria).

Every seam model must survive model_dump_json -> model_validate_json unchanged,
and the cheap structural invariants must fail at construction time.
"""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from regcompass.contracts import (
    BoundaryProposal,
    CanonicalText,
    Chunk,
    CrawlManifestEntry,
    CrosswalkConfig,
    DocumentMapping,
    Engine,
    GatedChunk,
    MappingRecord,
    OcrQuality,
    PageSpan,
    PipelineConfig,
    ReconciledGroup,
    Review,
    ShortlistRow,
    StageLogEntry,
    Timeline,
    TranslationGloss,
    WordBox,
)

NOW = datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc)
SHA = "a" * 64


def make_canonical(text: str = "Part I\ns. 1 Short title\nThis Act is the Test Act 2026.") -> CanonicalText:
    return CanonicalText(
        document_id="doc_001",
        source_sha256=SHA,
        extractor="pdfplumber",
        extractor_version="0.11.10",
        full_text=text,
        pages=[PageSpan(page_number=1, char_start=0, char_end=len(text))],
        words=[WordBox(text="Part", page=1, x0=0, y0=0, x1=10, y1=10, char_start=0, char_end=4)],
    )


def make_mapping(**overrides) -> MappingRecord:
    base = dict(
        mapping_id="map_001",
        document_id="doc_001",
        chunk_id="chunk_001",
        economy="MY",
        indicator_id="7.4",
        indicator_name="Data Protection Impact Assessment or Data Protection Officer requirements",
        section="s. 16",
        subsection="s. 16(1)(a)",
        verbatim_quote="a data user shall appoint a data protection officer",
        page_number=12,
        rdtii_score_contribution=1.0,
        verification_status="unverified",
        timeline=Timeline(adopted="2010", last_amended="2024"),
    )
    base.update(overrides)
    return MappingRecord(**base)


ROUND_TRIP_CASES = [
    make_canonical(),
    TranslationGloss(source_text="data peribadi", gloss_en="personal data", engine="opus-mt-mul-en-ct2-int8"),
    Chunk.from_stream(make_canonical(), "chunk_001", 7, 24, "s. 1"),
    BoundaryProposal(char_start=0, char_end=7, section_label="front matter", chunk_kind="front_matter"),
    GatedChunk(
        chunk=Chunk.from_stream(make_canonical(), "chunk_001", 7, 24, "s. 1"),
        indicator_id="6.1",
        cosine_pillar=0.42,
        bm25_indicator=3.1,
        gate_decision="passed",
    ),
    make_mapping(),
    DocumentMapping(
        economy="MY",
        law_name="Personal Data Protection Act 2010",
        law_number_ref="Act 709",
        document_id="doc_001",
        source_url="https://lom.agc.gov.my/act-709",
        mappings=[make_mapping()],
    ),
    ReconciledGroup(
        group_id="grp_SG_7.2",
        economy="SG",
        indicator_id="7.2",
        mapping_ids=["map_001", "map_002"],
        authoritative_mapping_id="map_001",
        relationships={"map_001": "supersedes", "map_002": "superseded_by"},
        reconciliation_notes="National Act controls over sector-only notice.",
    ),
    CrawlManifestEntry(
        url="https://sso.agc.gov.sg/Act/TA1999",
        economy="SG",
        source_family="telecommunications",
        status="fetched",
        http_status=200,
        method="curl_cffi",
        sha256=SHA,
        fetched_at=NOW,
        local_path="SG/raw/TA1999.html",
    ),
    ShortlistRow(
        rank=1,
        document_id="doc_001",
        title="Personal Data Protection Act 2010",
        source_url="https://lom.agc.gov.my/act-709",
        relevance_score=0.93,
        matched_keywords=["personal data", "data protection officer"],
    ),
    Review(
        review_id="rev_001",
        run_id="run_001",
        mapping_id="map_001",
        review_status="accepted",
        reviewed_at=NOW,
    ),
    StageLogEntry(
        stage="m1_extract",
        input_hash=SHA,
        output_hash=SHA,
        method="pdfplumber-0.11.10",
        decision="extracted",
        duration_ms=812.5,
        timestamp=NOW,
    ),
    OcrQuality(mean_word_confidence=0.94, dictionary_hit_rate=0.9, ocr_quality_cer=0.02),
    PipelineConfig(),
]


@pytest.mark.parametrize("instance", ROUND_TRIP_CASES, ids=lambda m: type(m).__name__)
def test_json_round_trip(instance):
    restored = type(instance).model_validate_json(instance.model_dump_json())
    assert restored == instance


def test_extra_fields_forbidden():
    with pytest.raises(ValidationError):
        ShortlistRow(
            rank=1,
            document_id="d",
            title="t",
            source_url="u",
            relevance_score=0.5,
            surprise_field="nope",
        )


class TestCanonicalText:
    def test_page_span_beyond_stream_rejected(self):
        with pytest.raises(ValidationError, match="stream length"):
            CanonicalText(
                document_id="doc",
                source_sha256=SHA,
                extractor="pdfplumber",
                extractor_version="0.11.10",
                full_text="short",
                pages=[PageSpan(page_number=1, char_start=0, char_end=999)],
            )

    def test_bad_sha_rejected(self):
        with pytest.raises(ValidationError):
            make_canonical().model_copy(update={"source_sha256": "nothex"}).model_validate(
                {**make_canonical().model_dump(), "source_sha256": "nothex"}
            )

    def test_slice_is_identity(self):
        c = make_canonical()
        assert c.slice(0, len(c.full_text)) == c.full_text


class TestChunk:
    def test_from_stream_slice_identity(self):
        c = make_canonical()
        chunk = Chunk.from_stream(c, "ch1", 7, 24, "s. 1")
        assert chunk.text == c.full_text[7:24]

    def test_reemitted_text_rejected(self):
        """Chunk text that is not exactly the span's length cannot be a slice:
        the re-emission trap fails at construction."""
        with pytest.raises(ValidationError, match="never re-emitted"):
            Chunk(
                chunk_id="ch1",
                document_id="doc",
                char_start=0,
                char_end=10,
                text="this text was re-emitted by a model and is longer",
                section_label="s. 1",
            )


class TestMappingRecord:
    def test_discovery_tag_case_sensitive(self):
        with pytest.raises(ValidationError):
            make_mapping(discovery_tag="new")
        with pytest.raises(ValidationError):
            make_mapping(discovery_tag="Known")
        assert make_mapping(discovery_tag="NEW").discovery_tag == "NEW"
        assert make_mapping(discovery_tag="KNOWN").discovery_tag == "KNOWN"

    def test_indicator_id_is_dotted_text_not_a_p_code(self):
        """The SET of Indicators is config (all 12 pillars now), so the contract
        checks the SHAPE: dotted numeric text, two or three levels. A P-code is
        an emission format and is never an internal id; membership of the
        configured registry is checked at the boundaries that load it."""
        for good in ("6.1", "4.01", "12.01", "12.4.1", "1.4"):
            assert make_mapping(indicator_id=good).indicator_id == good
        for bad in ("P6-I1", "6", "6.1.2.3", "six.one", ""):
            with pytest.raises(ValidationError):
                make_mapping(indicator_id=bad)

    def test_score_direction_is_inverse(self):
        """RDTII direction: 0 = open, 1 = most restrictive.
        A most-restrictive measure carries 1.0 and the model accepts the full
        inverse range; anything outside [0, 1] is rejected."""
        most_restrictive = make_mapping(rdtii_score_contribution=1.0)
        fully_open = make_mapping(rdtii_score_contribution=0.0)
        assert most_restrictive.rdtii_score_contribution > fully_open.rdtii_score_contribution
        with pytest.raises(ValidationError):
            make_mapping(rdtii_score_contribution=1.5)
        with pytest.raises(ValidationError):
            make_mapping(rdtii_score_contribution=-0.1)

    def test_insufficient_evidence_never_passed(self):
        with pytest.raises(ValidationError, match="never be 'passed'"):
            make_mapping(insufficient_evidence=True, verification_status="passed")

    def test_substantive_mapping_requires_quote(self):
        with pytest.raises(ValidationError, match="verbatim_quote"):
            make_mapping(verbatim_quote="")

    def test_attempts_capped_at_three(self):
        with pytest.raises(ValidationError):
            make_mapping(extraction_attempts=4)


class TestReconciledGroup:
    def test_authoritative_must_be_member(self):
        with pytest.raises(ValidationError, match="member"):
            ReconciledGroup(
                group_id="g",
                economy="SG",
                indicator_id="7.2",
                mapping_ids=["map_001"],
                authoritative_mapping_id="map_999",
            )

    def test_relationship_keys_must_be_members(self):
        with pytest.raises(ValidationError, match="unknown mapping_ids"):
            ReconciledGroup(
                group_id="g",
                economy="SG",
                indicator_id="7.2",
                mapping_ids=["map_001"],
                authoritative_mapping_id="map_001",
                relationships={"map_404": "complementary"},
            )


class TestCrawlManifestEntry:
    def test_fetched_requires_bytes_evidence(self):
        with pytest.raises(ValidationError, match="sha256 and local_path"):
            CrawlManifestEntry(url="https://x", economy="SG", status="fetched", fetched_at=NOW)

    def test_fetched_requires_timestamp(self):
        with pytest.raises(ValidationError, match="fetched_at"):
            CrawlManifestEntry(
                url="https://x", economy="SG", status="fetched", sha256=SHA, local_path="SG/raw/x"
            )

    def test_failed_needs_no_hash(self):
        entry = CrawlManifestEntry(
            url="https://x", economy="SG", status="failed", http_status=404, error="HTTP 404"
        )
        assert entry.sha256 is None

    def test_pending_is_the_default(self):
        entry = CrawlManifestEntry(url="https://x", economy="MY")
        assert entry.status == "pending"


class TestGloss:
    def test_gloss_is_permanently_nonauthoritative(self):
        with pytest.raises(ValidationError):
            TranslationGloss(
                source_text="x",
                gloss_en="y",
                engine="e",
                non_authoritative=False,
            )


class TestEngine:
    def test_transport_retries_pinned_zero(self):
        with pytest.raises(ValidationError):
            Engine(
                name="bad",
                display_name="Bad Engine",
                litellm_model="openrouter/x",
                provider="openrouter",
                api_key_env="OPENROUTER_API_KEY",
                usd_per_million_input_tokens=1,
                usd_per_million_output_tokens=1,
                open_weights=False,
                structured_output="json_schema",
                concurrency=4,
                num_retries=2,
            )

    def test_prices_cannot_be_negative(self):
        with pytest.raises(ValidationError):
            Engine(
                name="bad",
                display_name="Bad Engine",
                litellm_model="openrouter/x",
                provider="openrouter",
                api_key_env="OPENROUTER_API_KEY",
                usd_per_million_input_tokens=-1,
                usd_per_million_output_tokens=1,
                open_weights=False,
                structured_output="json_schema",
                concurrency=4,
            )


class TestCrosswalkConfig:
    def test_empty_scheme_rejected(self):
        """A scheme no longer has to cover a fixed id set (the numeric scheme is
        generated over the loaded registry by load_crosswalk), but an empty one
        would emit nothing at all."""
        with pytest.raises(ValidationError, match="is empty"):
            CrosswalkConfig(emission_scheme="numeric", schemes={"numeric": {}})

    def test_partial_scheme_is_allowed(self):
        cw = CrosswalkConfig(emission_scheme="numeric", schemes={"numeric": {"6.1": "6.1"}})
        assert cw.schemes["numeric"] == {"6.1": "6.1"}

    def test_emission_scheme_must_exist_in_schemes(self):
        full = {i: i for i in ["6.1", "6.2", "6.3", "6.4", "7.1", "7.2", "7.3", "7.4", "7.5"]}
        with pytest.raises(ValidationError, match="not defined"):
            CrosswalkConfig(emission_scheme="p_code", schemes={"numeric": full})

    def test_unknown_emission_scheme_rejected(self):
        with pytest.raises(ValidationError, match="not defined"):
            CrosswalkConfig(emission_scheme="roman", schemes={"numeric": {"6.1": "6.1"}})
