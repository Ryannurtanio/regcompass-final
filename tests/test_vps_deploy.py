"""The hosted-server override: docker-compose.vps.yml and its Caddyfile.

A copy on a rented server must ask for a login and must not be reachable
around it. Those promises are made in text files, so they are checked by
reading the text files, the same way test_docker checks the local stack.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from regcompass.server import AUTH_PASSWORD_ENV, AUTH_USER_ENV

ROOT = Path(__file__).resolve().parents[1]


class _ComposeLoader(yaml.SafeLoader):
    """SafeLoader plus Compose's `!reset` tag, read as a marker string."""


_ComposeLoader.add_constructor("!reset", lambda loader, node: "!reset")

VPS_TEXT = (ROOT / "docker-compose.vps.yml").read_text(encoding="utf-8")
VPS = yaml.load(VPS_TEXT, Loader=_ComposeLoader)
BASE = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
CADDYFILE = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
GUIDE = (ROOT / "docs" / "DEPLOY_VPS.md").read_text(encoding="utf-8")


def test_every_overridden_service_exists_in_the_base_file():
    for name in ("ollama", "regcompass"):
        assert name in BASE["services"]
        assert name in VPS["services"]


def test_the_app_gets_the_login_with_the_documented_defaults():
    env = VPS["services"]["regcompass"]["environment"]
    assert f"{AUTH_USER_ENV}=${{{AUTH_USER_ENV}:-admin}}" in env
    assert f"{AUTH_PASSWORD_ENV}=${{{AUTH_PASSWORD_ENV}:-admin123}}" in env


def test_only_caddy_publishes_a_port():
    assert VPS["services"]["regcompass"]["ports"] == "!reset"
    assert VPS["services"]["ollama"]["ports"] == "!reset"
    assert VPS["services"]["regcompass-bundle"]["ports"] == "!reset"
    caddy = VPS["services"]["caddy"]
    assert {p.split(":")[0] for p in caddy["ports"]} == {"80", "443"}


def test_caddy_is_the_official_image_pinned_and_proxies_the_app():
    caddy = VPS["services"]["caddy"]
    assert re.fullmatch(r"caddy:\d+\.\d+\.\d+", caddy["image"])
    assert "./deploy/Caddyfile:/etc/caddy/Caddyfile:ro" in caddy["volumes"]
    assert "reverse_proxy regcompass:8000" in CADDYFILE
    # the site address comes from the variable the guide tells an operator to set
    assert "{$REGCOMPASS_DOMAIN" in CADDYFILE
    assert "REGCOMPASS_DOMAIN" in caddy["environment"]


def test_the_guide_names_the_real_files_and_services():
    assert "docker-compose.vps.yml" in GUIDE
    assert "docker compose run --rm --no-deps regcompass-load" in GUIDE
    assert "chmod 600 .env" in GUIDE
    for name in (AUTH_USER_ENV, AUTH_PASSWORD_ENV, "REGCOMPASS_DOMAIN"):
        assert name in GUIDE
    for service in re.findall(r"docker compose (?:logs|restart|stop|start) ([\w-]+)", GUIDE):
        assert service in BASE["services"] or service in VPS["services"], service
