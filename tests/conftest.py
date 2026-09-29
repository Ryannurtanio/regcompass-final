"""Shared live-service probes and opt-in gates for skipif markers.

A probe must resolve the service the same way the code under test does:
gate.py and map.py read OLLAMA_HOST, so the probe reads OLLAMA_HOST too. A
probe pinned to localhost while OLLAMA_HOST points elsewhere makes live tests
FAIL instead of skip. Stdlib-only on
purpose: conftest is imported for every collection, including base-tier
installs without the `live` extra.
"""

from __future__ import annotations

import os
import urllib.request

import pytest

# the pytester fixture lets a test prove the opt-in gate end to end in a child
# session (tests/test_crawl.py::TestLiveLaneIsOptIn); it ships with pytest
pytest_plugins = ["pytester"]


def _ollama_up() -> bool:
    base = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = f"http://{base}"
    try:
        with urllib.request.urlopen(f"{base}/api/version", timeout=2):
            return True
    except (OSError, ValueError):
        return False


needs_ollama = pytest.mark.skipif(not _ollama_up(), reason="ollama server not reachable")

# Money is opt-in, twice over. A key check alone does NOT make a paid test
# opt-in: importing litellm runs its own load_dotenv, which fills
# OPENROUTER_API_KEY from the repo .env, so a bare pytest run on a developer
# machine would silently bill the account (observed 16 Sep 2026). Every test
# that calls a paid Engine carries needs_paid AS WELL AS its key check, and
# tests/test_paid_optin.py proves none is forgotten. REGCOMPASS_LIVE is a
# different switch (portal fetches); never reuse it for money.
needs_paid = pytest.mark.skipif(
    not os.environ.get("REGCOMPASS_PAID"),
    reason="paid model call; set REGCOMPASS_PAID=1 and the Engine's key to opt in",
)

# The live portal lane hits real government sites. It is opt-in, not
# probe-driven: a DNS lookup succeeds on any online machine, so probing alone
# let the lane fire in CI and on every developer's `pytest -q`. Set
# REGCOMPASS_LIVE=1 to run it; the DNS probe stays as a second condition so an
# opted-in but offline machine skips instead of erroring.
LIVE_ENV_VAR = "REGCOMPASS_LIVE"
LIVE_OPT_IN_REASON = "live portal test; set REGCOMPASS_LIVE=1 to opt in"

needs_live = pytest.mark.skipif(
    not os.environ.get(LIVE_ENV_VAR), reason=LIVE_OPT_IN_REASON
)
