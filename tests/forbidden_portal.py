"""A Portal whose own site rules forbid automated collection, for the tests.

No shipped Economy carries `manual_only` today: China used to, while its only
known host was the national law database whose robots.txt disallows every
agent, and it lost the flag once its statutes were found on a whitelisted host
that permits them. The refusal itself is permanent behaviour and stays
covered, so these tests point the loaders at a copy of the shipped config/
with one extra Portal that carries the flag: Testland (ZZ), no host, strategy
`manual`, exactly the shape the Portal contract demands of a manual-only
Economy.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import yaml

from regcompass.config import CONFIG_DIR

FORBIDDEN = "ZZ"
FORBIDDEN_NAME = "Testland"


def forbidden_config(tmp_path: Path) -> Path:
    """A copy of config/ whose portals.yaml adds the manual-only Testland."""
    config_dir = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, config_dir)
    path = config_dir / "portals.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["portals"][FORBIDDEN] = {
        "official_name": FORBIDDEN_NAME,
        "languages": ["English"],
        "hosts": [],
        "strategy": "manual",
        "manual_only": True,
        "prepared": False,
        "live_test_pool": False,
        "notes": "Its own site rules forbid any automated collection.",
    }
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return config_dir


def point_loaders_at(monkeypatch, config_dir: Path) -> None:
    """The loaders called with no directory (the server's registry, the CLI's
    Economy check) resolve config.CONFIG_DIR at call time, so this moves them.
    A function whose signature binds CONFIG_DIR as its default is handed the
    directory explicitly instead."""
    import regcompass.config as config_mod

    monkeypatch.setattr(config_mod, "CONFIG_DIR", config_dir)
