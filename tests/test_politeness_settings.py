"""The read-only politeness endpoint behind the Settings card "Polite crawling".

Every value on that card must come from the same Portal configuration and the
same constants the crawler uses, so the screen can never disagree with the
behaviour. No network: the endpoint reads config/portals.yaml and nothing else.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from regcompass.config import load_portals
from regcompass.contracts import CONNECTIONS_PER_HOST, ROBOTS_UNREACHABLE_GRACE_DAYS
from regcompass.server import create_app, robots_unavailable_words
from regcompass.storage import Storage


@pytest.fixture()
def client(tmp_path):
    db = tmp_path / "web.db"
    storage = Storage(db)
    storage.apply_schema()
    storage.conn.close()
    out = tmp_path / "out"
    out.mkdir()
    return TestClient(
        create_app(db_path=db, out_dir=out, data_dir=tmp_path / "data", ui_dir=None)
    )


def _body(client):
    r = client.get("/api/settings/politeness")
    assert r.status_code == 200, r.text
    return r.json()


def test_every_portal_with_a_host_is_listed_with_its_configured_values(client):
    body = _body(client)
    portals = load_portals()
    with_hosts = [code for code, p in portals.items() if p.hosts]
    rows = {row["economy"]: row for row in body["portals"]}
    assert list(rows) == with_hosts, "config order, and only Portals we can contact"
    for code in with_hosts:
        p = portals[code]
        row = rows[code]
        assert row["name"] == p.official_name
        assert row["host"] == p.hosts[0], "the host whose robots.txt binds us"
        assert row["hosts"] == p.hosts
        assert row["min_interval_seconds"] == p.min_interval_seconds
        assert row["connections_per_host"] == CONNECTIONS_PER_HOST == 1
        assert row["robots_respected"] is True
        assert row["robots_unavailable_setting"] == p.robots_unavailable_policy
        assert isinstance(row["robots_unavailable_policy"], str)
        assert row["robots_unavailable_policy"].strip()


def test_every_floor_is_at_least_one_second(client):
    for row in _body(client)["portals"]:
        assert row["min_interval_seconds"] >= 1


def test_the_unavailable_robots_policy_is_stated_per_portal_in_plain_words(client):
    rows = {row["economy"]: row for row in _body(client)["portals"]}
    # India carries the operator's recorded `proceed` decision.
    assert rows["IN"]["robots_unavailable_setting"] == "proceed"
    assert "no rules" in rows["IN"]["robots_unavailable_policy"]
    # Malaysia refuses, with a recorded first-failure date: the 30-day rule.
    assert rows["MY"]["robots_unavailable_setting"] == "refuse"
    assert "2026-07-06" in rows["MY"]["robots_unavailable_policy"]
    # Singapore refuses with no date: nothing is fetched until it recovers.
    assert rows["SG"]["robots_unavailable_setting"] == "refuse"
    assert "nothing is fetched" in rows["SG"]["robots_unavailable_policy"].lower()
    for row in rows.values():
        assert "\u2014" not in row["robots_unavailable_policy"]


def test_the_card_rules_are_carried_with_the_list(client):
    body = _body(client)
    assert body["connections_per_host"] == CONNECTIONS_PER_HOST
    assert body["robots_respected"] is True
    assert "lengthen" in body["crawl_delay_rule"]
    assert "cannot be reached" in body["robots_unreachable_rule"]


def test_politeness_cannot_be_changed_through_the_api(client):
    for method in ("post", "put", "patch", "delete"):
        r = getattr(client, method)("/api/settings/politeness")
        assert r.status_code == 405, (method, r.status_code)


def test_the_crawler_uses_the_same_connection_cap():
    from regcompass.crawl import one_connection

    with one_connection() as c:
        pool = c._transport._pool  # httpx's own pool settings
        assert pool._max_connections == CONNECTIONS_PER_HOST


def _malaysia():
    portal = load_portals()["MY"]
    assert portal.robots_unreachable_since == date(2026, 7, 6)
    return portal


def test_before_the_30_days_run_out_nothing_is_fetched():
    words = robots_unavailable_words(_malaysia(), today=date(2026, 8, 5))
    assert words.startswith("Nothing is fetched")
    assert "2026-08-06" in words, "the first day the crawler lifts the refusal"


def test_once_the_30_days_have_run_out_it_reads_as_no_rules():
    words = robots_unavailable_words(_malaysia(), today=date(2026, 8, 6))
    assert words.startswith("Read as publishing no rules since 2026-08-06")
    assert f"{ROBOTS_UNREACHABLE_GRACE_DAYS} days" in words
    assert "minimum wait still applies" in words


def test_the_words_agree_with_the_crawler_on_the_boundary():
    """The crawler's own decision (read_robots_policy) and the card's words
    flip on the same day."""
    from regcompass.crawl import FetchResult, RobotsUnavailableError, read_robots_policy

    portal = _malaysia()

    def five_hundred(url):
        return FetchResult(url, url, 500, b"", None, "httpx")

    for today in (date(2026, 8, 5), date(2026, 8, 6)):
        words = robots_unavailable_words(portal, today=today)
        try:
            read_robots_policy(
                portal.hosts[0], five_hundred,
                unreachable_since=portal.robots_unreachable_since, today=today,
            )
            crawler_proceeds = True
        except RobotsUnavailableError:
            crawler_proceeds = False
        assert crawler_proceeds == words.startswith("Read as publishing no rules"), today
