"""The Evidence Export's refusals, in words a reviewer can act on.

A Discovery record is not a Run: it fetched Documents and mapped none, so an
Export of it has nothing to write. Asked anyway (the Run history once opened a
Discovery row on the Evidence screen), the answer must say that, not blame an
Engine that was never called. A Document with no Source URL is named by its
title, and the refusal names the control that fixes it as it is on screen.

Offline: a hand-seeded database, no Engine, no network.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from regcompass.server import create_app
from regcompass.storage import Storage, utc_now_iso


@pytest.fixture()
def discovery_db(tmp_path):
    db = tmp_path / "regcompass.db"
    storage = Storage(db)
    storage.apply_schema()
    storage.run_start(
        run_id="disc_20260928T000000Z_abcdef", kind="discovery", economy="IN",
        pillars=[], indicators=None, engine=None, started_at=utc_now_iso(),
    )
    storage.run_finish(
        "disc_20260928T000000Z_abcdef", status="completed", ended_at=utc_now_iso(),
        documents_fetched=3,
    )
    storage.close()
    return db


def test_exporting_a_discovery_says_it_is_a_discovery(discovery_db, tmp_path):
    client = TestClient(create_app(
        db_path=discovery_db, out_dir=tmp_path / "out", data_dir=tmp_path / "data",
        ui_dir=None,
    ))
    r = client.post("/api/export", params={"run_id": "disc_20260928T000000Z_abcdef"})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, str)
    assert "Discovery" in detail
    assert "Engine" not in detail, "no Engine ran, so none is to blame"
    assert "Run history" in detail, "the refusal names where to go instead"
