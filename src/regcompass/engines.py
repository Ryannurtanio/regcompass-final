"""The single place that resolves an Engine and builds its transport.

One selected Engine drives every model-calling stage of a run (M4 boundary
fallback, M6 map, M7 verify's re-map, M8 reconcile, M12 classify, the M3 gloss
lane). Stages never load config themselves: they receive an Engine, or ask
resolve_engine for the configured default. That is what makes the Engine switch
in the interface honest, and what stops a stage quietly running a different
model from the one the Run Record names.

Transport rules, identical on every Engine: temperature 0, num_retries 0 (silent
SDK retries corrupt attempt counting), a hard per-request timeout, and a strict
JSON schema supplied by the calling stage. A missing key is a ConfigError raised
BEFORE any call, never a retry loop.

The fake Engine is the offline lane: scripted deterministic answers, no network,
no key. It is a real registry entry so `--engine fake` exercises the same
pipeline code path the paid Engines do.
"""

from __future__ import annotations

import json
import os
import re
import threading
from contextvars import ContextVar, copy_context
from dataclasses import dataclass
from typing import Callable, TypeVar

from .config import CONFIG_DIR, load_models
from .contracts import Engine, UnknownEngine

CompletionFn = Callable[[str, bool], str]

__all__ = [
    "ConfigError",
    "EngineCallDeadline",
    "CALL_DEADLINE_GRACE_SECONDS",
    "CALL_DEADLINE_SECONDS",
    "call_deadline_seconds",
    "call_with_deadline",
    "UnknownEngine",
    "resolve_engine",
    "preflight_key",
    "set_session_key",
    "forget_session_key",
    "session_key_set",
    "key_is_set",
    "completion_kwargs",
    "make_completion",
    "embed_fn_for",
    "fake_completion",
    "fake_embed",
    "DROP_INDICATOR",
    "NO_EVIDENCE_INDICATOR",
    "UsageTotals",
    "start_meter",
    "read_meter",
    "watch_meter",
    "copy_meter",
    "record_usage",
    "cost_usd_for",
]


class ConfigError(RuntimeError):
    """A configuration problem (missing API key): retrying cannot fix it, so
    transport-retry ladders must re-raise it immediately instead of burning
    backoff time and reporting a green run over zero records."""


# ---------------------------------------------------------------------------
# the run-scoped usage meter
# ---------------------------------------------------------------------------
#
# Every stage builds its OWN completion function (map, verify, reconcile,
# classify, the M4 boundary fallback), so there is no single call site to
# decorate. The meter is therefore a context variable owned here, with two
# recording points: the LiteLLM branch of make_completion, which reads the
# provider's usage numbers, and fake_completion, which counts its own scripted
# answers.
#
# A run's Mapping stage may put several calls in flight at once (the pipeline's
# bounded pair pool), and a bare worker thread starts with an EMPTY context, so
# the pool copies the submitting thread's context into each worker. The copy
# carries a reference to this same UsageTotals object, which is why the totals
# add up across threads, and why the addition itself is taken under a lock: the
# read-modify-write of four counters is not atomic, and a Run Record that
# undercounts tokens undercounts the money.
#
# Deliberate hole: a caller that injects a completion function of its own
# making (not fake_completion, not one from make_completion) bypasses both
# points and records nothing. That is the price of not threading a meter
# argument through every stage seam; the shipped lanes all go through one of
# the two points.


@dataclass
class UsageTotals:
    """What one run spent, as the meter saw it."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    provider_cost_usd: float | None = None
    calls: int = 0


_METER: ContextVar[UsageTotals | None] = ContextVar("regcompass_usage_meter", default=None)
_METER_LOCK = threading.Lock()
# Who wants to read this context's meter WHILE it runs. The server's Run panel
# shows a Run's spend as it grows, but the meter lives in the worker thread's
# context, out of reach of the request that asks. The worker names a watcher
# here before the Run starts, and start_meter hands it the fresh totals.
_WATCHER: ContextVar[Callable[[UsageTotals], None] | None] = ContextVar(
    "regcompass_usage_watcher", default=None
)


def watch_meter(callback: Callable[[UsageTotals], None] | None) -> None:
    """Hand every meter started later in this context to `callback`, so a
    reader on another thread can follow the totals live (through copy_meter)."""
    _WATCHER.set(callback)


def start_meter() -> None:
    """Begin a fresh usage meter for this context. Any earlier meter is
    dropped, so a second run never inherits the first one's totals."""
    totals = UsageTotals()
    _METER.set(totals)
    watcher = _WATCHER.get()
    if watcher is not None:
        try:
            watcher(totals)
        except Exception:  # noqa: BLE001 - a watcher never takes a run down
            pass


def copy_meter(totals: UsageTotals) -> UsageTotals:
    """A consistent copy of totals another thread may be adding to."""
    with _METER_LOCK:
        return UsageTotals(
            prompt_tokens=totals.prompt_tokens,
            completion_tokens=totals.completion_tokens,
            provider_cost_usd=totals.provider_cost_usd,
            calls=totals.calls,
        )


def read_meter() -> UsageTotals:
    """The totals recorded since start_meter(); zeros when no meter is open."""
    meter = _METER.get()
    return UsageTotals() if meter is None else meter


def record_usage(
    prompt_tokens: int | None,
    completion_tokens: int | None,
    provider_cost_usd: float | None = None,
) -> None:
    """Add one model call to the open meter. Best effort by contract: a missing
    usage block is zero, never an exception, because no accounting figure is
    worth failing a run over."""
    meter = _METER.get()
    if meter is None:
        return
    with _METER_LOCK:
        meter.prompt_tokens += int(prompt_tokens or 0)
        meter.completion_tokens += int(completion_tokens or 0)
        meter.calls += 1
        if provider_cost_usd is not None:
            meter.provider_cost_usd = (meter.provider_cost_usd or 0.0) + float(
                provider_cost_usd
            )


def cost_usd_for(engine: Engine, prompt_tokens: int, completion_tokens: int) -> float:
    """Cost from the Engine's DECLARED per-million prices. Declared, not
    estimated: a steward reading the OpenRouter bill can check every term."""
    return (
        prompt_tokens * engine.usd_per_million_input_tokens / 1e6
        + completion_tokens * engine.usd_per_million_output_tokens / 1e6
    )


# OpenRouter returns its own billed figure for a call; LiteLLM parks it here.
# It is a cross-check on the declared-price arithmetic, never its source.
_PROVIDER_COST_HEADER = "llm_provider-x-litellm-response-cost"


def _record_response_usage(resp: object) -> None:
    """Recording point A: one LiteLLM response's usage numbers. Wrapped whole
    in a swallow: an accounting read must never take a run down."""
    try:
        usage = getattr(resp, "usage", None)
        # The provider's cost header is parsed in its OWN swallow: it is the
        # optional cross-check, and a provider sending nonsense there must not
        # cost us the token counts, which are the measurement that matters.
        provider_cost = None
        try:
            hidden = getattr(resp, "_hidden_params", None) or {}
            headers = hidden.get("additional_headers") or {}
            raw = headers.get(_PROVIDER_COST_HEADER)
            if raw is not None:
                provider_cost = float(raw)
        except Exception:  # noqa: BLE001 - a bad cost header is not a bad call
            provider_cost = None
        record_usage(
            getattr(usage, "prompt_tokens", None),
            getattr(usage, "completion_tokens", None),
            provider_cost,
        )
    except Exception:  # noqa: BLE001 - accounting is best effort, never fatal
        pass


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------


def resolve_engine(name: str | None, config_dir=CONFIG_DIR) -> Engine:
    """The named Engine from config/models.yaml; None means the configured
    default_engine. Raises UnknownEngine (listing the names) on a typo."""
    models = load_models(config_dir)
    return models.engine(models.default_engine if name is None else name)


# ---------------------------------------------------------------------------
# the session key store
# ---------------------------------------------------------------------------
#
# A key typed into the interface's Settings screen lives HERE, in this process,
# for as long as it runs: a module-level dict keyed by the env variable NAME.
# It is deliberately not written into os.environ, because the process
# environment leaks (subprocesses inherit it, a crash dump prints it, and
# _load_dotenv would overwrite or be overwritten by it), and not written to
# disk at all, because a judge's or a teammate's key is theirs and must vanish
# when the server stops. Restart forgets it by construction.
#
# Every read of a key in this codebase goes through _key_for, so there is one
# place that knows the store exists and one order of precedence.

_SESSION_KEYS: dict[str, str] = {}


def set_session_key(env: str, value: str) -> None:
    """Hold one Engine's key for this server session. The value is never
    logged, never echoed back and never persisted."""
    env = str(env).strip()
    if not env:
        raise ValueError("no environment variable name for this key")
    value = str(value).strip()
    if not value:
        raise ValueError("empty key")
    _SESSION_KEYS[env] = value


def forget_session_key(env: str) -> None:
    """Drop a session key. Forgetting one that was never set is not an error:
    the caller asked for a state, not for an event."""
    _SESSION_KEYS.pop(str(env).strip(), None)


def session_key_set(env: str) -> bool:
    return bool(_SESSION_KEYS.get(str(env).strip()))


def _key_for(engine: Engine) -> str | None:
    """This Engine's key: the session store first, then the process
    environment. None when the Engine needs none or none is set. The ONE
    reader; preflight_key and completion_kwargs both come through here so a key
    typed into Settings and a key exported in the shell behave identically."""
    if not engine.api_key_env:
        return None
    return _SESSION_KEYS.get(engine.api_key_env) or os.environ.get(engine.api_key_env) or None


def key_is_set(engine: Engine) -> bool:
    """Whether this Engine can run: the question the interface asks. A boolean,
    never the value."""
    return not engine.api_key_env or bool(_key_for(engine))


def preflight_key(engine: Engine) -> None:
    """Fail fast when the selected Engine needs a key that is not set. Called
    before the database is opened and before any model call, so a missing key
    costs a message instead of a whole run's backoff ladder. The value itself is
    never read into a message or a log line."""
    if not engine.api_key_env:
        return
    if not _key_for(engine):
        raise ConfigError(
            f"Engine '{engine.name}' ({engine.display_name}) needs"
            f" {engine.api_key_env} (in .env, or typed into Settings); not set"
        )


# ---------------------------------------------------------------------------
# transport: the wall-clock deadline RegCompass enforces itself
# ---------------------------------------------------------------------------
#
# The per-request timeout below is handed to the model library, and twice on
# 23 Sep 2026 it simply did not fire: a paid Run stopped dead mid-Document for
# 20 to 30 minutes with one request still open at the provider, the process
# idle, and no audit row. Because the first try never returned, the transport
# ladder in pipeline.py never got its turn and the Mapping pool, which reads
# its results in pair order, waited forever on that one pair. The operator had
# to kill the Run by hand.
#
# So the ceiling is enforced HERE, where we can see the clock: the library call
# runs on a daemon thread and the caller waits at most the deadline. The
# deadline is the library's own request timeout plus a grace, so an honest slow
# answer is never cut; only a call the library has already given up on, or
# never noticed, hits this wall. On expiry the caller raises EngineCallDeadline,
# which the transport ladder treats like any other transport failure: same
# three tries, same backoff, same skipped-pair lane when it persists.
#
# The abandoned thread is left to die with the process. It is a daemon, so it
# cannot hold the interpreter open at exit, and that is exactly why this is a
# bare Thread and not a pool worker: a pool joins its workers at exit and one
# wedged worker would keep the whole process alive.

CALL_DEADLINE_GRACE_SECONDS = 60

# Normally None: the deadline is derived per call from the request timeout, so
# the hosted Engines get 240s and the Ollama tier (600s timeout) gets 660s. Set
# it to a number to override both, which is how a test shrinks the wait.
CALL_DEADLINE_SECONDS: float | None = None

_T = TypeVar("_T")


class EngineCallDeadline(TimeoutError):
    """One Engine call outlived the wall-clock ceiling. A TimeoutError subclass
    because that is what it is, and because the transport ladder retries
    anything that is not a config, auth or rate-limit problem: a deadline is a
    transport failure with no model output, so attempt counting is untouched."""


def call_deadline_seconds(request_timeout: float) -> float:
    """The ceiling for one call: the library's own request timeout plus the
    grace, unless CALL_DEADLINE_SECONDS overrides it."""
    if CALL_DEADLINE_SECONDS is not None:
        return float(CALL_DEADLINE_SECONDS)
    return float(request_timeout) + CALL_DEADLINE_GRACE_SECONDS


def call_with_deadline(fn: Callable[[], _T], deadline: float, label: str) -> _T:
    """Run fn() on a daemon thread, wait at most `deadline` seconds for it, and
    hand back what it returned (or re-raise what it raised) on THIS thread.

    fn runs under a copy of the caller's context, exactly as the Mapping pool's
    workers do: a bare thread starts with an EMPTY context, and the usage meter
    lives in one. The copy carries a reference to the same UsageTotals object,
    so fn can record its own call's tokens and the caller sees them. That also
    means a call we abandoned still meters itself if the provider answers late,
    which keeps the money honest.
    """
    result: list[_T] = []
    failure: list[BaseException] = []
    ctx = copy_context()

    def runner() -> None:
        try:
            result.append(ctx.run(fn))  # type: ignore[arg-type]
        except BaseException as exc:  # noqa: BLE001 - handed back to the caller
            failure.append(exc)

    worker = threading.Thread(target=runner, name="rc-engine-call", daemon=True)
    worker.start()
    worker.join(deadline)
    if worker.is_alive():
        expired = EngineCallDeadline(
            f"{label}: no answer within the {deadline:g}s call deadline;"
            f" the request was abandoned"
        )
        # The caller's ladder can see the thread it walked away from, which is
        # what the test asserts is a daemon and still running.
        expired.thread = worker  # type: ignore[attr-defined]
        raise expired
    if failure:
        raise failure[0]
    return result[0]


def _ollama_base_url() -> str:
    base = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = f"http://{base}"
    return base


def completion_kwargs(
    engine: Engine,
    response_format: dict | None = None,
    default_response_format: dict | None = None,
) -> dict:
    """Everything but the messages, so tests can assert the transport contract
    without a network: model, schema, key handling, retries pinned 0. The
    calling stage supplies its own strict schema; default_response_format is the
    stage's fallback when the caller passed none."""
    if engine.structured_output == "none":
        raise ValueError(
            f"Engine '{engine.name}' has no structured output mode; cannot map with it"
        )
    kwargs: dict = {
        "model": engine.litellm_model,
        "temperature": 0,
        "num_retries": 0,
        # Hard per-request ceiling: a stalled streamed response otherwise hangs
        # forever (wedged a 40+ min M8 reconcile on 6 Jul 2026). A timeout is a
        # TRANSPORT failure (no model output): the transport ladder retries
        # it loudly; attempt counting is untouched.
        "timeout": 180,
        "response_format": response_format or default_response_format,
    }
    if engine.api_key_env:
        preflight_key(engine)
        kwargs["api_key"] = _key_for(engine)
    if engine.litellm_model.startswith(("ollama/", "ollama_chat/")):
        kwargs["api_base"] = _ollama_base_url()
        # Ollama's server default context is 4,096 tokens; long statute
        # chunks overflow it (observed live: a 5,058-token prompt 400s with
        # exceed_context_size_error). 16k covers the largest chunk the
        # splitter emits plus the task rules. LiteLLM passes num_ctx through
        # to Ollama's options.
        kwargs["num_ctx"] = 16384
        # Local inference is legitimately SLOW on big chunks (observed live:
        # an 18k-char section needs several minutes of prompt processing on
        # a CPU-only small model). The 180s ceiling exists for remote stalled
        # streams; locally it would misclassify honest work as a failure.
        kwargs["timeout"] = 600
        # Bound RUNAWAY generation: observed live, a small local model looped
        # on a 2.7k-char search-and-seizure section until the 16k context
        # filled (>600s). 4,096 tokens still covers a verbatim quote of the
        # largest chunk the splitter emits; a response truncated here is
        # malformed output and burns a content attempt, the honest lane, not
        # a hang. LiteLLM maps max_tokens to Ollama's num_predict.
        kwargs["max_tokens"] = 4096
    return kwargs


def make_completion(
    engine: Engine, response_format: dict | None, mock_response: str | None = None
) -> CompletionFn:
    """The (prompt, strict) -> content callable for one stage on one Engine.
    The fake Engine answers offline from the prompt itself; every other Engine
    goes through LiteLLM (imported lazily so the base tier stays importable).
    response_format is the stage's own strict schema, or None for the one stage
    whose answer is a JSON array (M4's boundary fallback). mock_response is the
    stages' offline test hook (litellm's own mock lane)."""
    if engine.provider == "fake":
        return fake_completion

    def completion(prompt: str, strict: bool) -> str:
        import litellm

        # LiteLLM prices every answer against its own model list, and a model
        # it does not list (the Qwen one on OpenRouter) makes that lookup fail
        # and print a red "Provider List" banner several times per call,
        # flooding the server log. The failure is swallowed inside LiteLLM and
        # its price is never used (the declared prices are); this only gates
        # the prints, and a real error still raises.
        litellm.suppress_debug_info = True
        kwargs = completion_kwargs(engine, response_format)
        if mock_response is not None:
            kwargs["mock_response"] = mock_response

        def call() -> str:
            resp = litellm.completion(
                messages=[{"role": "user", "content": prompt}], **kwargs
            )
            # Metered on the helper thread, under the copied context, so a late
            # answer to an abandoned call still lands on the run's meter.
            _record_response_usage(resp)
            return resp.choices[0].message.content or ""

        return call_with_deadline(
            call, call_deadline_seconds(kwargs["timeout"]), engine.name
        )

    return completion


def embed_fn_for(engine: Engine):
    """The M5 embedding function for this Engine, or None to use the shared
    Ollama embedder. Only the fake Engine overrides it: an offline run must not
    reach the network at the gate either."""
    if engine.provider == "fake":
        return fake_embed
    return None


# ---------------------------------------------------------------------------
# the fake Engine: deterministic offline answers
# ---------------------------------------------------------------------------

_PROVISION_RE = re.compile(r"<<<PROVISION\n(.*?)\nPROVISION>>>", re.DOTALL)
# The gloss lane's own prompt marker (translate.gloss_prompt). One block per
# Mapping, so the fake Engine can answer a whole Document's Glosses in the one
# call the real Engines take.
_GLOSS_ITEM_RE = re.compile(r"<<<GLOSS (\S+)\n")
# Every Indicator ID shape the registry can carry, including the three-level
# (12.4.1) and leading-zero (4.01, 12.01) spellings. A narrower pattern would
# make the fake Engine silently answer "not json at all" on a pillar 12 prompt
# and the offline run would report zero mappings for no visible reason.
_INDICATOR_RE = re.compile(r"INDICATOR (\d{1,2}(?:\.\d{1,2}){1,2})")

# Per-indicator behaviour of the fake Engine (pillar 7 lanes):
#   7.3 -> invented quote, never in the stream: M7 must retry then DROP
#   7.1 -> maps_to_indicator false: the honest no-evidence lane
#   everything else -> verbatim quote from the provision block: PASSES
DROP_INDICATOR = "7.3"
NO_EVIDENCE_INDICATOR = "7.1"


# The fake Engine's scripted token counts: four characters to a token, the
# usual rule of thumb, applied to the real prompt and the real answer. It is
# deterministic, so a test can recompute the expected total from the same two
# strings and assert the exact cost arithmetic.
_FAKE_CHARS_PER_TOKEN = 4


def fake_completion(prompt: str, strict: bool) -> str:
    """The fake Engine's answer, with its scripted usage metered (recording
    point B). Tests that inject fake_completion directly, bypassing
    make_completion, are counted here and nowhere else."""
    answer = _fake_answer(prompt, strict)
    record_usage(
        len(prompt) // _FAKE_CHARS_PER_TOKEN,
        len(answer) // _FAKE_CHARS_PER_TOKEN,
    )
    return answer


def _fake_answer(prompt: str, strict: bool) -> str:
    """Scripted answers driving all three record lanes plus the M8 fallback.

    The answer is parsed out of the prompt itself, so the stitch under test is
    the real one (shared attempt budget, retry-and-drop, byte-for-byte anchor):

      * a VERBATIM line copied from the prompt's own <<<PROVISION block, which
        survives M7's byte-for-byte check: the PASS lane;
      * indicator 7.3: a quote that appears nowhere in the document, which
        exhausts the attempt budget: the DROP lane;
      * indicator 7.1: maps_to_indicator false: the no-evidence lane;
      * an M8 group prompt (no INDICATOR/PROVISION block): non-JSON, so
        reconciliation falls back to the legal-hierarchy ladder, the RULE-wins
        lane.

    The M3 gloss prompt is answered separately: one scripted English line per
    Mapping id in the prompt, so an offline Run drafts a Gloss for every
    non-English Mapping and its tokens land on the Run Record like any other
    call.
    """
    gloss_ids = _GLOSS_ITEM_RE.findall(prompt)
    if gloss_ids:
        return json.dumps(
            {
                "glosses": [
                    {"mapping_id": key, "english": f"English gloss of the quote at {key}."}
                    for key in gloss_ids
                ]
            }
        )
    ind = _INDICATOR_RE.search(prompt)
    prov = _PROVISION_RE.search(prompt)
    if ind is None or prov is None:
        return "not json at all"
    indicator = ind.group(1)
    if indicator == NO_EVIDENCE_INDICATOR:
        return json.dumps({"maps_to_indicator": False, "verbatim_quote": ""})
    if indicator == DROP_INDICATOR:
        return json.dumps(
            {
                "maps_to_indicator": True,
                "verbatim_quote": "THIS SENTENCE APPEARS NOWHERE IN THE SOURCE DOCUMENT AT ALL.",
                "impact": "fabricated",
            }
        )
    text = prov.group(1)
    lines = [ln.strip() for ln in text.splitlines() if len(ln.strip()) >= 40]
    if not lines:
        return json.dumps({"maps_to_indicator": False, "verbatim_quote": ""})
    return json.dumps(
        {
            "maps_to_indicator": True,
            "verbatim_quote": lines[0],
            "impact": "The provision regulates the matter described in the quote.",
        }
    )


def fake_embed(texts: list[str]):
    """Identical unit vectors: every chunk's pillar cosine is 1.0, so the gate
    decision reduces to the deterministic BM25 tier over real vocabulary. No
    Ollama, no network."""
    import numpy as np

    return np.ones((len(texts), 8), dtype=np.float32)
