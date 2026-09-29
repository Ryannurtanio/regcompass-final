"""M3 - translation gloss: non-English source text -> DRAFT English gloss.

Nothing here touches the canonical stream or the verbatim columns. A Gloss is a
rendering BESIDE the Verbatim Quote, and it ships carrying its label until a
named person approves the text (a Reviewed Gloss): M3 automates the DRAFTING,
never the authority.

Two lanes:
- the RUN's lane (draft_document_glosses, default on): the Run's own selected
  Engine drafts a Document's Glosses in one metered call, and they are stored
  beside the Mappings, unreviewed. This is the lane the interface and the
  Evidence Export read.
- the OFFLINE command (`regcompass gloss`): drafts into files for a human to
  read, on either gloss engine below. Kept as the optional path.

Offline gloss engines:
- "opus_mt" (default): opus-mt-mul-en as CTranslate2 int8, fully offline and
  deterministic (greedy decode + repetition guards). Weights are fetched by
  scripts/fetch_m3_weights.py (gitignored, digest-pinned, never committed).
- "llm": the selected Engine via LiteLLM (config alternate). Used where opus-mt
  quality collapses (observed: Lao); needs that Engine's key.

Fallback lane (tested): ANY per-item failure - engine error,
empty output, degenerate repetition - yields a Gloss with english=None and the
`no-clear-translation-equivalent` uncertainty flag. draft_glosses never raises
per item and never blocks a pipeline.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from regcompass.contracts import GLOSS_LABEL, Engine, Gloss, PipelineConfig
from regcompass.engines import make_completion
from regcompass.export import looks_non_english
from regcompass.map import ConfigError

__all__ = [
    "GLOSS_LABEL",
    "GlossError",
    "LlmEngine",
    "OpusMtEngine",
    "detect_language",
    "draft_document_glosses",
    "draft_glosses",
    "gloss_prompt",
    "gloss_response_format",
    "looks_degenerate",
    "make_engine",
    "needs_gloss",
    "parse_gloss_answer",
]


class GlossError(RuntimeError):
    """An engine-level gloss failure. Callers of draft_glosses never see it:
    it is converted into the english=None + uncertainty-flag fallback lane."""


# ---------------------------------------------------------------------------
# detection helpers
# ---------------------------------------------------------------------------

# Script ranges with an unambiguous ISO 639-1 language in THIS project's corpus
# scope. Latin-script languages (Malay vs English vs ...) are deliberately not
# guessed: the caller may know (economy metadata), detection may not.
_SCRIPT_LANGS = (
    ("lo", re.compile(r"[຀-໿]")),  # Lao
    ("th", re.compile(r"[฀-๿]")),  # Thai
    ("my", re.compile(r"[က-႟]")),  # Myanmar
    ("km", re.compile(r"[ក-៿]")),  # Khmer
    ("zh", re.compile(r"[一-鿿]")),  # CJK unified
)


def detect_language(text: str) -> str:
    """Conservative script-based language tag: an unambiguous non-Latin script
    wins; anything else is "und" (undetermined), never a Latin-language guess."""
    for lang, pattern in _SCRIPT_LANGS:
        if pattern.search(text):
            return lang
    return "und"


# Letters beyond the Latin blocks (Latin Extended-B ends at U+024F; IPA and
# spacing modifiers up to U+02FF carry no language signal either).
_NON_LATIN_LETTER_FLOOR = 8


def _non_latin_letters(text: str) -> int:
    return sum(1 for ch in text if ch.isalpha() and ord(ch) > 0x2FF)


def needs_gloss(text: str) -> bool:
    """A SUPERSET of the export battery's detector: everything
    looks_non_english flags, PLUS any text with 8+ letters of a non-Latin
    script. The battery's under-8-Latin-words rule deliberately passes pure
    non-Latin text as insufficient signal - right for the Round 1 shipping
    gate it guards (the corpus's only non-English is Latin-script Malay),
    wrong for a drafting lane whose whole job is non-English text (found via
    the multilanguage fixture, 10 Jul 2026: a pure Devanagari or Lao snippet
    returned False). Superset semantics keep the two gates consistent: no
    snippet the battery would require a translation for goes undrafted, and
    drafting more is harmless because shipping stays human-gated."""
    return looks_non_english(text) or _non_latin_letters(text) >= _NON_LATIN_LETTER_FLOOR


_TOKEN_RE = re.compile(r"\S+")


def looks_degenerate(text: str, max_trigram_repeats: int) -> bool:
    """Mechanical repetition checks for the observed opus-mt failure modes:
    (1) the decoder loops one phrase (Lao spike, 10 Jul 2026): a word 3-gram
    occurring more than max_trigram_repeats times; (2) the decoder stutters
    the same word back-to-back with a varying spacer ("whatsoever whatsoever
    X whatsoever whatsoever Y", A1472 fragment, same day): more than
    max_trigram_repeats adjacent-equal token pairs. Legal prose stays far
    below both thresholds."""
    tokens = [t.lower() for t in _TOKEN_RE.findall(text)]
    counts: dict[tuple[str, str, str], int] = {}
    for tri in zip(tokens, tokens[1:], tokens[2:]):
        counts[tri] = counts.get(tri, 0) + 1
        if counts[tri] > max_trigram_repeats:
            return True
    adjacent_pairs = sum(a == b for a, b in zip(tokens, tokens[1:]))
    return adjacent_pairs > max_trigram_repeats


# ---------------------------------------------------------------------------
# engines
# ---------------------------------------------------------------------------


class OpusMtEngine:
    """opus-mt-mul-en on CTranslate2 int8: offline, deterministic (greedy
    decode; the repetition guards do not add search randomness)."""

    name = "opus-mt-mul-en-ct2"

    def __init__(self, model_dir: Path):
        if not (model_dir / "model.bin").exists():
            raise ConfigError(
                f"gloss model not found at {model_dir}; run scripts/fetch_m3_weights.py"
            )
        import ctranslate2
        import sentencepiece as spm

        self._sp_src = spm.SentencePieceProcessor()
        self._sp_src.load(str(model_dir / "source.spm"))
        self._sp_tgt = spm.SentencePieceProcessor()
        self._sp_tgt.load(str(model_dir / "target.spm"))
        self._translator = ctranslate2.Translator(str(model_dir), device="cpu")

    # Sentence boundaries only (never semicolons: a legal list item like
    # "(a) ...; or" must translate as a unit; its severed "; or" tail sent
    # alone made the decoder free-associate - A1472, 10 Jul 2026).
    _SEGMENT_RE = re.compile(r"(?<=[.!?])\s+")
    _MIN_SEGMENT_CHARS = 20  # fragments shorter than this merge into a neighbor

    def translate(self, text: str) -> str:
        """Collapse PDF line-wrapping first (hard breaks mid-sentence destroy
        the model's context: observed on the A1472 snippet, 10 Jul 2026), then
        translate sentence-wise, merging fragments into their neighbor."""
        flat = " ".join(text.split())
        segments: list[str] = []
        for seg in self._SEGMENT_RE.split(flat):
            seg = seg.strip()
            if not seg:
                continue
            if segments and (len(seg) < self._MIN_SEGMENT_CHARS or len(segments[-1]) < self._MIN_SEGMENT_CHARS):
                segments[-1] = f"{segments[-1]} {seg}"
            else:
                segments.append(seg)
        if not segments:
            raise GlossError("nothing to translate")
        results = self._translator.translate_batch(
            [self._sp_src.encode(s, out_type=str) for s in segments],
            beam_size=1,
            repetition_penalty=1.3,
            no_repeat_ngram_size=3,
            max_decoding_length=256,
        )
        return " ".join(self._sp_tgt.decode(r.hypotheses[0]) for r in results).strip()


class LlmEngine:
    """The selected Engine as a gloss engine for the offline command (config
    alternate). It goes through the SHARED transport (engines.make_completion),
    which is what puts its tokens on the meter; a missing key raises ConfigError
    (fail fast, never a retry loop)."""

    # The one-item key of a single-text call, echoed back by the schema.
    _SINGLE = "gloss"

    def __init__(self, engine: Engine):
        from .engines import key_is_set

        self.name = f"llm:{engine.litellm_model}"
        self._engine = engine
        # ONE key reader for the whole codebase (engines._key_for): the session
        # store first, then the environment. Reading os.environ here would mean
        # a key typed into Settings ran every Engine except this lane.
        if not key_is_set(engine):
            raise ConfigError(
                f"the gloss lane needs {engine.api_key_env} (in .env, or typed"
                " into Settings); not set"
            )

    def translate(self, text: str) -> str:
        # make_completion, not a private litellm call: it preflights the key
        # (a key that went missing since construction is a clean ConfigError,
        # not a KeyError) and it records the call's usage on the open meter.
        fn = make_completion(self._engine, gloss_response_format())
        content = fn(gloss_prompt([(self._SINGLE, text)], PipelineConfig()), False)
        return (parse_gloss_answer(content).get(self._SINGLE) or "").strip()


def make_engine(cfg: PipelineConfig, engine: Engine | None = None):
    """Gloss engine per config. gloss_engine="llm" needs an Engine passed in
    (the caller owns config loading); "opus_mt" needs the fetched weights."""
    if cfg.gloss_engine == "llm":
        if engine is None:
            raise ConfigError(
                "gloss_engine 'llm' needs an Engine from config/models.yaml"
            )
        return LlmEngine(engine)
    return OpusMtEngine(Path(cfg.gloss_model_dir))


# ---------------------------------------------------------------------------
# the drafting lane
# ---------------------------------------------------------------------------


def _failure(key: str, text: str, language: str, engine_name: str) -> Gloss:
    return Gloss(
        key=key,
        source_text=text,
        source_language=language,
        english=None,
        engine=engine_name,
        uncertainty_flag="no-clear-translation-equivalent",
    )


def draft_gloss(
    key: str,
    text: str,
    engine,
    cfg: PipelineConfig,
    source_language: str | None = None,
) -> Gloss:
    """One DRAFT gloss. NEVER raises for content reasons: engine errors, empty
    output, and degenerate repetition all take the fallback lane (english=None
    + the uncertainty flag), so a bad item can never block a run."""
    language = source_language or detect_language(text)
    clipped = text[: cfg.gloss_max_chars]
    try:
        english = engine.translate(clipped)
    except ConfigError:
        raise  # configuration is the operator's problem, not a content failure
    except Exception:
        return _failure(key, text, language, engine.name)
    if not english.strip() or looks_degenerate(english, cfg.gloss_max_trigram_repeats):
        return _failure(key, text, language, engine.name)
    if needs_gloss(english):
        # the "translation" is not English (model echoed or transliterated)
        return _failure(key, text, language, engine.name)
    return Gloss(
        key=key,
        source_text=text,
        source_language=language,
        english=english,
        engine=engine.name,
    )


def draft_glosses(
    items: list[tuple[str, str]],
    engine,
    cfg: PipelineConfig,
    source_language: str | None = None,
) -> list[Gloss]:
    """Draft glosses for (key, source_text) pairs, one Gloss per item in input
    order. Per-item failures ship as fallback records; the list never shrinks."""
    return [draft_gloss(key, text, engine, cfg, source_language) for key, text in items]


# ---------------------------------------------------------------------------
# the Run's own lane: one call per Document, through the shared transport
# ---------------------------------------------------------------------------
#
# The offline lane above translates one text per call with its own engine
# object. A Run cannot: it must go through engines.make_completion, or the
# Gloss's tokens never reach the Run Record's meter, and it must not spend one
# round trip per Mapping inside the live-test window. So the Run batches a
# Document's Mappings into ONE call against a strict schema, and the answer is
# matched back BY MAPPING ID, never by position: a model that drops or reorders
# an item then loses that one Gloss to the fallback lane instead of attaching
# an English sentence to the wrong quote.


def gloss_response_format() -> dict:
    """The strict JSON schema for a batched gloss call. Hand-written and closed
    for the same reason M6's is: a stable shape every Engine can enforce."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "translation_glosses",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "glosses": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "mapping_id": {"type": "string"},
                                "english": {"type": "string"},
                            },
                            "required": ["mapping_id", "english"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["glosses"],
                "additionalProperties": False,
            },
        },
    }


def gloss_prompt(items: list[tuple[str, str]], cfg: PipelineConfig) -> str:
    """One prompt covering every Mapping of one Document. Each quote is fenced
    and tagged with its Mapping id, which is what the answer keys on."""
    blocks = "\n\n".join(
        f"<<<GLOSS {key}\n{text[: cfg.gloss_max_chars]}\nGLOSS>>>" for key, text in items
    )
    return (
        "Translate each fenced legal quote below into English. Reply with ONLY"
        " JSON of the form {\"glosses\": [{\"mapping_id\": ..., \"english\":"
        " ...}]}, one entry per fenced quote, echoing each mapping_id exactly as"
        " given. Translate the quote and nothing else: no commentary, no notes,"
        " no summary. Preserve numbering.\n\n" + blocks
    )


def parse_gloss_answer(content: str) -> dict[str, str]:
    """mapping_id -> English, from a batched gloss answer. Never raises: a
    malformed answer is an empty mapping, which sends every item of that call
    down the fallback lane."""
    text = (content or "").strip()
    fence = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    if not text.startswith("{"):
        lo, hi = text.find("{"), text.rfind("}")
        if lo == -1 or hi <= lo:
            return {}
        text = text[lo : hi + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("glosses"), list):
        return {}
    out: dict[str, str] = {}
    for entry in data["glosses"]:
        if not isinstance(entry, dict):
            continue
        key, english = entry.get("mapping_id"), entry.get("english")
        if isinstance(key, str) and isinstance(english, str):
            out[key] = english
    return out


def _accept(key: str, text: str, english: str, language: str, engine_name: str, cfg) -> Gloss:
    """One drafted item through the SAME content guards the offline lane uses:
    empty, degenerate, or still-not-English output is a failure, not a Gloss."""
    english = english.strip()
    if not english or looks_degenerate(english, cfg.gloss_max_trigram_repeats):
        return _failure(key, text, language, engine_name)
    if needs_gloss(english):
        return _failure(key, text, language, engine_name)
    return Gloss(
        key=key,
        source_text=text,
        source_language=language,
        english=english,
        engine=engine_name,
    )


def draft_document_glosses(
    items: list[tuple[str, str]],
    engine: Engine,
    cfg: PipelineConfig,
    completion_fn=None,
    source_language: str | None = None,
) -> list[Gloss]:
    """Draft one Gloss per (mapping_id, verbatim quote) of ONE Document with the
    SELECTED Engine, in a single metered call.

    One Gloss comes back per item in input order, exactly as draft_glosses
    promises: a transport error, a malformed answer or a missing entry all take
    the fallback lane (english=None plus the uncertainty flag), so the gloss
    lane can never take a Run down."""
    if not items:
        return []
    engine_name = f"llm:{engine.litellm_model}" if engine.provider != "fake" else engine.name
    fn = completion_fn or make_completion(engine, gloss_response_format())
    try:
        answer = parse_gloss_answer(fn(gloss_prompt(items, cfg), False))
    except ConfigError:
        raise  # configuration is the operator's problem, not a content failure
    except Exception:  # noqa: BLE001 - a gloss never blocks a Run
        answer = {}
    out: list[Gloss] = []
    for key, text in items:
        language = source_language or detect_language(text)
        english = answer.get(key)
        if english is None:
            out.append(_failure(key, text, language, engine_name))
        else:
            out.append(_accept(key, text, english, language, engine_name, cfg))
    return out
