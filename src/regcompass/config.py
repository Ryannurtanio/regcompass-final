"""Typed loaders for config/. Every file is validated through its contract model
at load time so a config typo fails at startup, not mid-pipeline."""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from regcompass.contracts import (
    INDICATOR_ID_PATTERN,
    MANUAL_STRATEGY,
    CorpusDoc,
    CrawlSeedsEconomy,
    CrosswalkConfig,
    IndicatorDef,
    LawMetadata,
    ModelsConfig,
    PillarDef,
    PipelineConfig,
    PortalConfig,
)

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"

_INDICATOR_ID_RE = re.compile(INDICATOR_ID_PATTERN)


def _dir(config_dir: Path | None) -> Path:
    """Resolve the config directory at CALL time. A `config_dir=CONFIG_DIR`
    default would bind the module value once at import, so nothing could ever
    point the loaders at another directory."""
    return Path(config_dir) if config_dir is not None else CONFIG_DIR


class ConfigInvalid(ValueError):
    """A committed config file is internally inconsistent. Raised at load time
    with the file and the offending key named, so `regcompass check-config`
    can print something a human can act on."""


def _read_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_portals(config_dir: Path | None = None) -> dict[str, PortalConfig]:
    """The Economy registry. Every CLI choice list, every server validation and
    every interface dropdown is built from this, so adding an Economy is an
    edit to portals.yaml and nothing else. The Discovery strategy name is
    validated by PortalConfig against contracts.STRATEGY_NAMES (crawl.STRATEGIES
    binds the same names to functions; the two are asserted equal in tests, and
    the vocabulary lives in contracts so config loading needs no live extra)."""
    raw = _read_yaml(_dir(config_dir) / "portals.yaml")
    return {code: PortalConfig(economy=code, **entry) for code, entry in raw["portals"].items()}


def economy_codes(config_dir: Path | None = None) -> tuple[str, ...]:
    """Every configured Economy code, in the order portals.yaml lists them."""
    return tuple(load_portals(config_dir))


def load_models(config_dir: Path | None = None) -> ModelsConfig:
    return ModelsConfig(**_read_yaml(_dir(config_dir) / "models.yaml"))


def load_crosswalk(config_dir: Path | None = None) -> CrosswalkConfig:
    """The emission crosswalk. The `numeric` scheme is GENERATED here as the
    identity over every legislation-mapped Indicator, so adding an Indicator
    never means editing the crosswalk file; a scheme the file DOES list (p_code)
    stays scoped to the Indicators it names, and every one of them must exist."""
    raw = json.loads((_dir(config_dir) / "indicator_id_crosswalk.json").read_text(encoding="utf-8"))
    config = {k: v for k, v in raw.items() if not k.startswith("_")}
    indicators = load_indicators(config_dir)
    schemes = dict(config.get("schemes") or {})
    schemes["numeric"] = {
        i: i for i, d in indicators.items() if d.legislation_mapped
    }
    for name, mapping in schemes.items():
        unknown = sorted(set(mapping) - set(indicators))
        if unknown:
            raise ConfigInvalid(
                f"indicator_id_crosswalk.json scheme '{name}' names indicators that"
                f" are not in indicators.json: {unknown}"
            )
    config["schemes"] = schemes
    return CrosswalkConfig(**config)


def load_pipeline(config_dir: Path | None = None) -> PipelineConfig:
    path = _dir(config_dir) / "pipeline.yaml"
    if not path.exists():
        return PipelineConfig()
    return PipelineConfig(**_read_yaml(path))


def load_corpus(config_dir: Path | None = None) -> dict[str, CorpusDoc]:
    """Document-level export metadata keyed by document_id (M9)."""
    raw = _read_yaml(_dir(config_dir) / "corpus.yaml")
    return {doc_id: CorpusDoc(**meta) for doc_id, meta in raw["documents"].items()}


def load_law_metadata(config_dir: Path | None = None) -> dict[str, LawMetadata]:
    """Law Number / Ref and Last Amended for Documents outside corpus.yaml,
    keyed by document id. Optional file: a Document with no entry keeps both
    columns blank, which is what the export did before the file existed."""
    path = _dir(config_dir) / "law_metadata.yaml"
    if not path.exists():
        return {}
    raw = _read_yaml(path)
    return {doc_id: LawMetadata(**meta) for doc_id, meta in (raw.get("documents") or {}).items()}


def load_crawl_seeds(config_dir: Path | None = None) -> dict[str, CrawlSeedsEconomy]:
    """Per-Economy Discovery seeding (M10): source families + politeness floor.

    Seeds are required only where Discovery can actually run: an Economy that is
    `prepared` and is not on the `manual` strategy. An Economy in the live-test
    pool that we have not crawled yet legitimately has no seeds, and must not
    make `check-config` red."""
    raw = _read_yaml(_dir(config_dir) / "crawl_seeds.yaml")
    seeds = {code: CrawlSeedsEconomy(**entry) for code, entry in raw["seeds"].items()}
    portals = load_portals(config_dir)
    needs_seeds = {
        code
        for code, p in portals.items()
        if p.prepared and p.strategy != MANUAL_STRATEGY
    }
    missing = needs_seeds - set(seeds)
    if missing:
        raise ConfigInvalid(
            f"crawl_seeds.yaml has no seeds for prepared economies: {sorted(missing)}"
        )
    stray = set(seeds) - set(portals)
    if stray:
        raise ConfigInvalid(
            f"crawl_seeds.yaml seeds economies with no portal: {sorted(stray)}"
        )
    return seeds


def load_known_matrix(config_dir: Path | None = None) -> dict:
    """The provision-level KNOWN matrix (M9 Discovery Tag). Keys: 'database'
    (economy -> indicator -> [{law, law_key, sections}]) and
    'inventory_law_keys' (economy -> [normalized law keys])."""
    raw = json.loads((_dir(config_dir) / "known_matrix.json").read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def load_review_drops(config_dir: Path | None = None) -> dict[str, str]:
    """Human review-rejected records excluded at export: mapping_id -> reason.
    Optional file; entries carry reviewer,
    date, and reason so every exclusion is auditable. Checkpoints are never
    edited; this is the export-side gate. Entries matching no record are
    ignored (the config is shared by fixture-scale and corpus-scale runs)."""
    path = _dir(config_dir) / "review_drops.json"
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {e["mapping_id"]: e["reason"] for e in raw.get("drops", [])}


def load_indicators(config_dir: Path | None = None) -> dict[str, IndicatorDef]:
    """The Indicator registry (M6 prompt input), generated from the organizer's
    sheets by scripts/make_indicators.py. The SET is data: this validates the
    shape of every entry rather than a fixed list of ids, so adding an Indicator
    is an edit to indicators.json and nothing else.

    Checked here: the id is text matching the dotted pattern, its pillar equals
    the number before the first dot, and name and definition are non-empty."""
    raw = json.loads((_dir(config_dir) / "indicators.json").read_text(encoding="utf-8"))
    defs: dict[str, IndicatorDef] = {}
    for key, entry in raw.items():
        if key.startswith("_"):
            continue
        if not _INDICATOR_ID_RE.match(key):
            raise ConfigInvalid(
                f"indicators.json '{key}' is not a dotted text indicator id"
                f" (pattern {INDICATOR_ID_PATTERN})"
            )
        try:
            definition = IndicatorDef(**entry)
        except Exception as e:  # noqa: BLE001 - re-raised with the key named
            raise ConfigInvalid(f"indicators.json '{key}' is invalid: {e}") from e
        if definition.pillar != int(key.split(".")[0]):
            raise ConfigInvalid(
                f"indicators.json '{key}' declares pillar {definition.pillar}"
                f" but its id sits under pillar {int(key.split('.')[0])}"
            )
        defs[key] = definition
    if not defs:
        raise ConfigInvalid("indicators.json defines no indicators")
    return defs


def load_pillars(config_dir: Path | None = None) -> dict[int, PillarDef]:
    """Pillar number -> official name and the Indicator ids under it."""
    raw = json.loads((_dir(config_dir) / "pillars.json").read_text(encoding="utf-8"))
    return {int(k): PillarDef(**v) for k, v in raw.items() if k.isdigit()}


def load_default_pillars(config_dir: Path | None = None) -> tuple[int, ...]:
    """What a Run covers when the reviewer names no Pillar. This is DATA, not a
    literal in the code: Pillars 6 and 7 are the organizer's mandatory pair, and
    defaulting to all twelve would turn every unqualified Run into a
    61-Indicator sweep nobody asked for."""
    raw = json.loads((_dir(config_dir) / "pillars.json").read_text(encoding="utf-8"))
    configured = set(load_pillars(config_dir))
    default = tuple(int(p) for p in raw.get("default_pillars") or ())
    if not default:
        raise ConfigInvalid("pillars.json names no default_pillars")
    unknown = sorted(set(default) - configured)
    if unknown:
        raise ConfigInvalid(
            f"pillars.json default_pillars names unconfigured pillars: {unknown}"
        )
    return default


def indicator_ids(
    pillars: tuple[int, ...] | None = None,
    config_dir: Path | None = None,
    legislation_mapped_only: bool = True,
) -> tuple[str, ...]:
    """The Indicator ids the pipeline works on, in registry order, narrowed to
    the given Pillars. Indicators no statute can answer (6.5) are left out
    unless asked for: the Gate must never shortlist a chunk against them."""
    defs = load_indicators(config_dir)
    return tuple(
        i
        for i, d in defs.items()
        if (pillars is None or d.pillar in pillars)
        and (d.legislation_mapped or not legislation_mapped_only)
    )


class UnknownIndicator(ValueError):
    """A Run was narrowed to an Indicator that is not one of its Pillars'.
    Carries the valid ids so the CLI and the server can both print the list
    without rebuilding it."""

    def __init__(self, bad: list[str], valid: tuple[str, ...], pillars):
        self.bad = list(bad)
        self.valid = tuple(valid)
        self.pillars = tuple(pillars) if pillars is not None else None
        scope = (
            "the configured Pillars"
            if self.pillars is None
            else f"Pillar {', '.join(str(p) for p in self.pillars)}"
        )
        super().__init__(
            f"unknown Indicator {', '.join(self.bad)} for {scope}:"
            f" expected one of {', '.join(self.valid)}"
        )


def narrow_indicators(
    indicators,
    pillars: tuple[int, ...] | None = None,
    config_dir: Path | None = None,
) -> tuple[str, ...] | None:
    """The Indicator ids a Run was narrowed to, checked against the Run's
    Pillars and returned in registry order. None (the whole Pillar set) for no
    narrowing at all, so a caller can hand the result straight to the Gate.

    The live test names one Pillar and two Indicators, and a Run that quietly
    ignored a typo would answer a different question from the one asked; an id
    outside the Run's Pillars is refused with the valid list."""
    if not indicators:
        return None
    valid = indicator_ids(pillars, config_dir)
    wanted = [str(i).strip() for i in indicators]
    bad = [i for i in wanted if i not in valid]
    if bad:
        raise UnknownIndicator(bad, valid, pillars)
    chosen = set(wanted)
    return tuple(i for i in valid if i in chosen)


def load_keywords(pillar: int, config_dir: Path | None = None) -> dict[str, list[str]]:
    """Per-Indicator keyword vocabulary for one Pillar. Keys starting with '_'
    are file metadata (comment, pillar description, provenance), not
    Indicators."""
    path = _dir(config_dir) / f"pillar_{pillar}_keywords.json"
    if not path.is_file():
        raise ConfigInvalid(f"pillar {pillar} has no keyword file at {path.name}")
    raw: dict = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def load_pillar_description(pillar: int, config_dir: Path | None = None) -> str:
    """The pillar-gate description text embedded by BGE-M3 (M5 pillar tier)."""
    path = _dir(config_dir) / f"pillar_{pillar}_keywords.json"
    if not path.is_file():
        raise ConfigInvalid(f"pillar {pillar} has no keyword file at {path.name}")
    raw: dict = json.loads(path.read_text(encoding="utf-8"))
    if not raw.get("_pillar_description"):
        raise ConfigInvalid(f"{path.name} has no _pillar_description")
    return raw["_pillar_description"]


def validate_config(config_dir: Path | None = None) -> list[str]:
    """Every cross-file rule the loaders cannot see on their own, as a list of
    human-readable problems (empty means green). `regcompass check-config`
    prints it.

    The rules: every Pillar has a keyword file; that file carries a
    _pillar_description and exactly the Indicator ids of its Pillar; every
    Indicator has a non-empty definition and appears under its Pillar."""
    problems: list[str] = []
    indicators = load_indicators(config_dir)
    pillars = load_pillars(config_dir)

    listed = {i for p in pillars.values() for i in p.indicator_ids}
    for missing in sorted(set(indicators) - listed):
        problems.append(f"indicator {missing} is in indicators.json but no pillar lists it")
    for stray in sorted(listed - set(indicators)):
        problems.append(f"pillars.json lists indicator {stray}, which indicators.json has not")

    for pillar in sorted(pillars):
        expected = {
            i for i in pillars[pillar].indicator_ids if indicators[i].legislation_mapped
        }
        try:
            vocab = load_keywords(pillar, config_dir)
            load_pillar_description(pillar, config_dir)
        except ConfigInvalid as e:
            problems.append(str(e))
            continue
        for missing in sorted(expected - set(vocab)):
            problems.append(
                f"pillar_{pillar}_keywords.json has no vocabulary for indicator {missing}"
            )
        for stray in sorted(set(vocab) - set(pillars[pillar].indicator_ids)):
            problems.append(
                f"pillar_{pillar}_keywords.json has a vocabulary for {stray},"
                f" which is not a pillar {pillar} indicator"
            )
    for key, definition in indicators.items():
        if not definition.definition.strip():
            problems.append(f"indicator {key} has an empty definition")
    return problems
