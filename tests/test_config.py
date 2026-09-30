"""Config file validation tests (S0): every committed config file loads through
its contract model, and the mechanical rules encoded in config hold."""

import pytest

from regcompass.config import (
    load_crosswalk,
    load_indicators,
    load_keywords,
    load_models,
    load_pillar_description,
    load_pipeline,
    load_portals,
)

ROUND1_INDICATORS = {"6.1", "6.2", "6.3", "6.4", "7.1", "7.2", "7.3", "7.4", "7.5"}

# The three Round 1 Economies plus the ten the organizers may draw on 15 Oct
# (the slide's eight, and Kazakhstan and Viet Nam from the Word template).
EXPECTED_ECONOMIES = {
    "SG", "AU", "MY", "ID", "TH", "LA", "VN", "CN", "IN", "KZ", "MN", "RU", "TL",
}


def test_portals_load_and_whitelist():
    portals = load_portals()
    assert set(portals) == EXPECTED_ECONOMIES
    assert portals["SG"].hosts == ["sso.agc.gov.sg"]
    assert "legislation.gov.au" in portals["AU"].hosts
    # lom (live), never lor (dead host printed in organizer materials)
    assert portals["MY"].hosts == ["lom.agc.gov.my"]
    assert all("lor.agc.gov.my" not in h for p in portals.values() for h in p.hosts)


def test_portals_carry_official_un_names():
    portals = load_portals()
    assert portals["SG"].official_name == "Singapore"
    assert portals["AU"].official_name == "Australia"
    assert portals["MY"].official_name == "Malaysia"


def test_models_transport_retries_all_zero():
    models = load_models()
    for engine in models.engines.values():
        assert engine.num_retries == 0
    assert models.embedder.num_retries == 0


def test_models_key_only_by_env_name():
    models = load_models()
    for spec in (*models.engines.values(), models.embedder):
        if spec.api_key_env is not None:
            # an env var NAME, not a key value
            assert spec.api_key_env.isupper()
            assert not spec.api_key_env.startswith("sk-")


def test_crosswalk_default_numeric_identity():
    """The numeric scheme is generated over the loaded registry, so every
    legislation-mapped Indicator emits as itself and none is left out."""
    cw = load_crosswalk()
    indicators = load_indicators()
    assert cw.emission_scheme == "numeric"
    expected = {i: i for i, d in indicators.items() if d.legislation_mapped}
    assert cw.schemes["numeric"] == expected
    assert ROUND1_INDICATORS <= set(expected)
    for text_id in ("4.01", "4.1", "12.01", "12.4.1", "12.4.7"):
        assert expected[text_id] == text_id


def test_crosswalk_p_code_positional_aliases():
    cw = load_crosswalk()
    for numeric, p_code in cw.schemes["p_code"].items():
        pillar, n = numeric.split(".")
        assert p_code == f"P{pillar}-I{n}"


def test_crosswalk_excludes_out_of_scope_65():
    cw = load_crosswalk()
    for scheme in cw.schemes.values():
        assert "6.5" not in scheme


@pytest.mark.parametrize("pillar,expected", [(6, {"6.1", "6.2", "6.3", "6.4"}), (7, {"7.1", "7.2", "7.3", "7.4", "7.5"})])
def test_keywords_cover_every_in_scope_indicator(pillar, expected):
    vocab = load_keywords(pillar)
    assert set(vocab) == expected
    for indicator, keywords in vocab.items():
        assert len(keywords) >= 5, f"{indicator} vocabulary too thin"
        assert all(isinstance(k, str) and k.strip() for k in keywords)


@pytest.mark.parametrize("pillar", [6, 7])
def test_keywords_include_malay_variants(pillar):
    """The Malaysian corpus gates in source language (never translate-then-gate)."""
    vocab = load_keywords(pillar)
    malay_markers = {"data peribadi", "pemindahan", "penyimpanan", "keselamatan siber",
                     "pegawai", "persetujuan", "pusat data", "pengekalan", "mahkamah",
                     "kerajaan", "pemintasan", "pelayan", "salinan", "kebenaran",
                     "infrastruktur", "jenayah", "menyimpan", "melantik", "penilaian",
                     "pendedahan", "larangan", "memindahkan", "disimpan", "syarat",
                     "perlindungan", "pemprosesan", "subjek", "penguatkuasaan", "perintah"}
    joined = " ".join(k for keywords in vocab.values() for k in keywords).lower()
    assert any(marker in joined for marker in malay_markers)


@pytest.mark.parametrize("pillar", [6, 7])
def test_pillar_descriptions_exist(pillar):
    assert len(load_pillar_description(pillar)) > 50


def test_pipeline_defaults_encode_hard_rules():
    p = load_pipeline()
    assert p.extraction_attempts == 3  # 1 initial + 2 stricter retries
    assert p.ocr_cer_max == 0.05  # OCR character-error-rate ceiling
    assert p.ocr_trigger_chars_per_page == 50
