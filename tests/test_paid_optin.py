"""Money never leaks into a bare test run.

Gating a live test on OPENROUTER_API_KEY alone does NOT make it opt-in:
importing litellm runs its own load_dotenv, which fills the variable from the
repo .env, so a developer typing `uv run pytest` would silently bill the
account (observed 16 Sep 2026, two Engine B calls). Every paid test must ALSO
carry needs_paid (REGCOMPASS_PAID), and this test proves none was forgotten.

The check reads the decorator stack of every test class and function in tests/:
anything whose decorators mention the key variable, or the needs_openrouter
marker built from it, must carry needs_paid in the same stack.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

TESTS = Path(__file__).parent
KEY_MARKERS = ("OPENROUTER_API_KEY", "needs_openrouter")


def _offenders() -> list[str]:
    out: list[str] = []
    for path in sorted(TESTS.glob("test_*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith(("Test", "test_")):
                continue
            decorators = [
                ast.get_source_segment(source, d) or "" for d in node.decorator_list
            ]
            stack = "\n".join(decorators)
            if any(marker in stack for marker in KEY_MARKERS) and "needs_paid" not in stack:
                out.append(f"{path.name}:{node.lineno} {node.name}")
    return out


def test_every_paid_test_is_opt_in():
    assert _offenders() == [], (
        "these tests would call a paid Engine on a bare pytest run; add"
        " needs_paid (and @pytest.mark.paid) from tests/conftest.py"
    )


def test_the_switch_is_off_here():
    """The suite this assertion runs in is the free one. If REGCOMPASS_PAID is
    set, the paid tests ran too, and that was a deliberate choice."""
    if os.environ.get("REGCOMPASS_PAID"):
        return
    from conftest import needs_paid

    assert needs_paid.args[0] is True  # skipif condition: skip when unset
