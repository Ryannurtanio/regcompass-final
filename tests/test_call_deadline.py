"""A hung Engine call cannot stall a Run.

The per-request timeout handed to the model library did not fire twice on
23 Sep 2026: a paid Run sat idle for 20 to 30 minutes with one request still
open at the provider, because the first try never returned and the transport
ladder never got its turn. Every Engine call now carries a wall-clock deadline
RegCompass enforces itself, on a daemon thread, and blowing it is an ordinary
transport failure.

Every test here is OFFLINE and fast: the deadline is shrunk to a fraction of a
second, the backoff waits go through the pipeline's _sleep seam, and nothing
reaches the network.
"""

from __future__ import annotations

import threading
import time

import pytest

from regcompass import engines
from regcompass import pipeline as pipeline_mod
from regcompass.engines import (
    EngineCallDeadline,
    call_deadline_seconds,
    call_with_deadline,
    fake_completion,
    read_meter,
    record_usage,
    resolve_engine,
    start_meter,
)

FAKE = resolve_engine("fake")


@pytest.fixture()
def blocker():
    """A call that never returns on its own, released when the test ends so no
    abandoned thread outlives the module."""
    released = threading.Event()

    def never_answers() -> str:
        released.wait(30)
        return "too late"

    try:
        yield never_answers
    finally:
        released.set()


class TestTheDeadlineIsDerivedFromTheRequestTimeout:
    def test_a_hosted_call_gets_the_library_timeout_plus_the_grace(self):
        assert call_deadline_seconds(180) == 180 + engines.CALL_DEADLINE_GRACE_SECONDS
        assert call_deadline_seconds(180) == 240

    def test_the_ollama_tier_keeps_its_own_much_longer_number(self):
        """Local inference is legitimately slow; its 600s timeout must not be
        cut down to the hosted wall."""
        assert call_deadline_seconds(600) == 660

    def test_an_override_shrinks_every_call(self, monkeypatch):
        monkeypatch.setattr(engines, "CALL_DEADLINE_SECONDS", 0.2)
        assert call_deadline_seconds(180) == 0.2
        assert call_deadline_seconds(600) == 0.2


class TestACallThatNeverReturnsIsAbandoned:
    def test_the_deadline_fires_and_names_itself(self, blocker):
        started = time.perf_counter()
        with pytest.raises(EngineCallDeadline) as caught:
            call_with_deadline(blocker, 0.2, "test-engine")
        elapsed = time.perf_counter() - started
        assert 0.2 <= elapsed < 5
        assert "deadline" in str(caught.value)
        assert "0.2" in str(caught.value)
        assert "test-engine" in str(caught.value)

    def test_the_abandoned_thread_is_a_daemon_and_is_left_running(self, blocker):
        """The whole point of a bare daemon thread: the caller walks away, the
        thread cannot hold the interpreter open at exit, and no pool is left
        with a worker to join."""
        with pytest.raises(EngineCallDeadline) as caught:
            call_with_deadline(blocker, 0.2, "test-engine")
        abandoned = caught.value.thread
        assert abandoned.daemon is True
        assert abandoned.is_alive() is True

    def test_it_is_not_mistaken_for_a_rate_limit(self):
        """The Mapping pool hands 429s to a different, much shorter ladder. A
        deadline must take the transport ladder instead."""
        expired = EngineCallDeadline("fake: no answer within the 240s call deadline")
        assert pipeline_mod.is_rate_limit_error(expired) is False
        assert isinstance(expired, TimeoutError)


class TestAnHonestCallIsNeverCut:
    def test_a_call_returning_just_under_the_deadline_is_kept(self):
        def slow() -> str:
            time.sleep(0.05)
            return "the answer"

        assert call_with_deadline(slow, 1.0, "test-engine") == "the answer"

    def test_the_usage_meter_survives_the_helper_thread(self):
        """The meter is a context variable and a bare thread starts with an
        EMPTY context. Without copying the caller's context into the helper,
        every token of every call would go unrecorded and the Run Record would
        report a free Run."""
        start_meter()

        def slow_and_billed() -> str:
            record_usage(120, 30)
            return "the answer"

        assert call_with_deadline(slow_and_billed, 1.0, "test-engine") == "the answer"
        assert read_meter().calls == 1
        assert read_meter().prompt_tokens == 120
        assert read_meter().completion_tokens == 30

    def test_the_call_s_own_exception_comes_back_unchanged(self):
        def boom() -> str:
            raise ConnectionError("connection reset by peer")

        with pytest.raises(ConnectionError, match="reset by peer"):
            call_with_deadline(boom, 1.0, "test-engine")


class TestTheTransportLadderOwnsIt:
    def test_the_deadline_error_is_retried_then_given_up_on(self, monkeypatch):
        waits: list[float] = []
        monkeypatch.setattr(pipeline_mod, "_sleep", waits.append)
        calls = {"n": 0}

        def always_expires():
            calls["n"] += 1
            raise EngineCallDeadline("fake: no answer within the 0.2s call deadline")

        with pytest.raises(RuntimeError, match="transport error persisted"):
            pipeline_mod._with_transport_retry("m8 SG", lambda s: None, always_expires)
        assert calls["n"] == pipeline_mod.TRANSPORT_RETRIES
        assert waits == [5, 10, 20]

    def test_a_pair_that_can_be_skipped_is_skipped_not_raised(self, monkeypatch):
        monkeypatch.setattr(pipeline_mod, "_sleep", lambda s: None)
        seen: list[str] = []

        def always_expires():
            raise EngineCallDeadline("fake: no answer within the 0.2s call deadline")

        out = pipeline_mod._with_transport_retry(
            "m6 doc:c0001::7.2", seen.append, always_expires, required=False
        )
        assert out is None
        assert any("EngineCallDeadline" in line for line in seen)

    def test_one_that_clears_on_the_second_try_costs_one_wait(self, monkeypatch):
        monkeypatch.setattr(pipeline_mod, "_sleep", lambda s: None)
        calls = {"n": 0}

        def expires_once() -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise EngineCallDeadline("fake: no answer within the 0.2s call deadline")
            return "mapped"

        assert (
            pipeline_mod._with_transport_retry("m6 x", lambda s: None, expires_once)
            == "mapped"
        )
        assert calls["n"] == 2


class TestTheProductionCallPathIsWrapped:
    """make_completion is the ONE callable every model-calling stage uses (map,
    verify, reconcile, classify, the boundary fallback, the gloss lane), which
    is why wrapping it there covers all of them."""

    def test_a_library_call_that_never_returns_raises_the_deadline(
        self, monkeypatch, blocker
    ):
        import litellm

        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-never-used-offline")
        monkeypatch.setattr(engines, "CALL_DEADLINE_SECONDS", 0.2)
        monkeypatch.setattr(litellm, "completion", lambda **kw: blocker())

        engine = resolve_engine("engine-b")
        completion = engines.make_completion(engine, None)
        with pytest.raises(EngineCallDeadline):
            completion("map this provision", False)

    def test_an_answered_call_still_comes_back_metered(self, monkeypatch):
        """The library's own offline mock lane, through the helper thread: the
        content arrives and the provider's usage block lands on the meter."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-never-used-offline")
        engine = resolve_engine("engine-b")
        completion = engines.make_completion(engine, None, mock_response="{}")
        start_meter()
        assert completion("map this provision", False) == "{}"
        assert read_meter().calls == 1

    def test_the_fake_engine_path_is_untouched(self):
        assert engines.make_completion(FAKE, None) is fake_completion
