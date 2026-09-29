"""Tests for the optional login on the one RegCompass server.

A hosted copy on a rented server sets REGCOMPASS_AUTH_USER and
REGCOMPASS_AUTH_PASSWORD and every route asks for them: a browser is sent to
the /login page, which signs it in with a session cookie, and a script may
still send the pair as an HTTP Basic header. The local path sets neither and
nothing changes. /api/status, /login and /api/login stay open, because the
Docker health check calls the first and nobody could sign in without the other
two. Every test clears both variables first, so a developer's shell cannot
switch the login on behind a test's back.
"""

from __future__ import annotations

import base64
import time

import pytest
from fastapi.testclient import TestClient

import regcompass.server as server_mod
from regcompass.server import (
    AUTH_PASSWORD_ENV,
    AUTH_USER_ENV,
    SESSION_COOKIE,
    SESSION_SECONDS,
    create_app,
)
from regcompass.storage import Storage

HTML = {"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}


@pytest.fixture(autouse=True)
def _no_login_env(monkeypatch):
    monkeypatch.delenv(AUTH_USER_ENV, raising=False)
    monkeypatch.delenv(AUTH_PASSWORD_ENV, raising=False)
    # the wrong-login pause is real in the server and nothing but waiting here
    monkeypatch.setattr(server_mod, "LOGIN_FAIL_DELAY", 0)


def _basic(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _app(tmp_path, ui: bool = False, **kwargs):
    db = tmp_path / "web.db"
    storage = Storage(db)
    storage.apply_schema()
    storage.conn.close()
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    ui_dir = None
    if ui:
        ui_dir = tmp_path / "dist"
        (ui_dir / "assets").mkdir(parents=True)
        (ui_dir / "index.html").write_text("<!doctype html><title>RegCompass</title>")
        (ui_dir / "assets" / "app.js").write_text("// bundle")
    return create_app(
        db_path=db, out_dir=out, data_dir=tmp_path / "data", ui_dir=ui_dir,
        **kwargs,
    )


class TestLoginOff:
    def test_no_variables_means_no_login(self, tmp_path):
        c = TestClient(_app(tmp_path, ui=True))
        assert c.get("/api/documents").status_code == 200
        assert c.get("/").status_code == 200
        assert c.get("/assets/app.js").status_code == 200

    def test_empty_variables_mean_no_login(self, tmp_path, monkeypatch):
        monkeypatch.setenv(AUTH_USER_ENV, "")
        monkeypatch.setenv(AUTH_PASSWORD_ENV, "")
        c = TestClient(_app(tmp_path))
        assert c.get("/api/documents").status_code == 200

    def test_a_browser_navigation_is_not_redirected(self, tmp_path):
        c = TestClient(_app(tmp_path, ui=True))
        r = c.get("/runs", headers=HTML, follow_redirects=False)
        assert r.status_code == 200
        assert "RegCompass" in r.text

    def test_the_status_says_the_login_is_off(self, tmp_path):
        c = TestClient(_app(tmp_path))
        assert c.get("/api/status").json()["login"] is False

    def test_the_login_page_sends_the_browser_home(self, tmp_path):
        c = TestClient(_app(tmp_path, ui=True))
        r = c.get("/login", headers=HTML, follow_redirects=False)
        assert r.status_code in (302, 303, 307)
        assert r.headers["location"] == "/"


class TestLoginOn:
    @pytest.fixture()
    def client(self, tmp_path):
        return TestClient(
            _app(tmp_path, ui=True, auth_user="admin", auth_password="s3cret")
        )

    def test_an_api_call_without_a_login_gets_401_json_and_no_popup(self, client):
        r = client.get("/api/documents")
        assert r.status_code == 401
        assert r.json() == {"detail": "login required"}
        # no challenge header, so the browser never draws its own login box
        assert "www-authenticate" not in r.headers

    def test_wrong_user_is_refused(self, client):
        r = client.get("/api/documents", headers=_basic("root", "s3cret"))
        assert r.status_code == 401

    def test_wrong_password_is_refused(self, client):
        r = client.get("/api/documents", headers=_basic("admin", "nope"))
        assert r.status_code == 401

    @pytest.mark.parametrize(
        "header", ["Basic", "Basic !!!not-base64", "Bearer abc",
                   "Basic " + base64.b64encode(b"no-colon").decode()],
    )
    def test_a_malformed_header_is_refused(self, client, header):
        r = client.get("/api/documents", headers={"Authorization": header})
        assert r.status_code == 401

    def test_the_right_login_is_let_through(self, client):
        r = client.get("/api/documents", headers=_basic("admin", "s3cret"))
        assert r.status_code == 200

    def test_a_password_holding_a_colon_works(self, tmp_path):
        c = TestClient(_app(tmp_path, auth_user="admin", auth_password="a:b"))
        assert c.get("/api/documents", headers=_basic("admin", "a:b")).status_code == 200

    def test_the_interface_itself_needs_the_login(self, client):
        assert client.get("/").status_code == 401
        assert client.get("/runs").status_code == 401
        assert client.get("/assets/app.js").status_code == 401
        assert "www-authenticate" not in client.get("/").headers
        ok = _basic("admin", "s3cret")
        assert client.get("/", headers=ok).status_code == 200
        assert client.get("/assets/app.js", headers=ok).status_code == 200

    def test_the_status_endpoint_stays_open_for_the_health_check(self, client):
        assert client.get("/api/status").status_code == 200

    def test_only_the_exact_status_path_is_open(self, client):
        assert client.get("/api/status/").status_code == 401
        assert client.get("/api/statusx").status_code == 401

    def test_the_status_says_the_login_is_on_and_nothing_more(self, client):
        body = client.get("/api/status").json()
        assert body["login"] is True
        text = client.get("/api/status").text
        assert "s3cret" not in text and "admin" not in text

    def test_the_progress_stream_passes_through_behind_the_login(self, client):
        assert client.get("/api/events").status_code == 401
        with client.stream(
            "GET", "/api/events", headers=_basic("admin", "s3cret"),
        ) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            body = "".join(resp.iter_text())
        assert ": connected" in body and "event: end" in body

    def test_the_variables_switch_it_on(self, tmp_path, monkeypatch):
        monkeypatch.setenv(AUTH_USER_ENV, "admin")
        monkeypatch.setenv(AUTH_PASSWORD_ENV, "admin123")
        c = TestClient(_app(tmp_path))
        assert c.get("/api/documents").status_code == 401
        ok = _basic("admin", "admin123")
        assert c.get("/api/documents", headers=ok).status_code == 200


def _login(client, user="admin", password="s3cret"):
    return client.post("/api/login", json={"user": user, "password": password})


def _forged_token(app, user: str, issued: float, expires: float) -> str:
    """A token signed with the app's own key, for the expiry and user checks."""
    return app.state.sessions.issue(user, issued=issued, expires=expires)


class TestLoginPage:
    @pytest.fixture()
    def app(self, tmp_path):
        return _app(tmp_path, ui=True, auth_user="admin", auth_password="s3cret")

    @pytest.fixture()
    def client(self, app):
        return TestClient(app)

    def test_a_browser_navigation_is_sent_to_the_login_page(self, client):
        r = client.get("/runs", headers=HTML, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/login?next=%2Fruns"
        assert "www-authenticate" not in r.headers

    def test_the_query_string_travels_in_next(self, client):
        r = client.get("/runs?x=1&y=2", headers=HTML, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/login?next=%2Fruns%3Fx%3D1%26y%3D2"

    def test_a_non_get_html_request_gets_401_not_a_redirect(self, client):
        r = client.post("/api/run", headers=HTML, json={})
        assert r.status_code == 401
        assert r.json() == {"detail": "login required"}

    def test_the_login_page_is_open_and_self_contained(self, client):
        r = client.get("/login", headers=HTML)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")
        page = r.text
        assert "RegCompass" in page or "REG" in page
        assert 'autocomplete="username"' in page
        assert 'autocomplete="current-password"' in page
        assert 'role="alert"' in page
        assert "prefers-color-scheme: dark" in page
        # no external requests: no remote script, stylesheet or font
        assert "http://" not in page and "https://" not in page
        assert "<script src" not in page and "<link" not in page

    def test_the_right_login_sets_a_signed_session_cookie(self, client):
        r = _login(client)
        assert r.status_code == 200
        cookie = r.headers["set-cookie"]
        assert cookie.startswith(f"{SESSION_COOKIE}=")
        low = cookie.lower()
        assert "httponly" in low
        assert "samesite=lax" in low
        assert "path=/" in low
        assert f"max-age={SESSION_SECONDS}" in low
        # plain http here, so the cookie is not marked Secure
        assert "secure" not in low.replace("samesite", "")
        assert "s3cret" not in cookie

    def test_the_session_opens_every_route(self, client):
        assert _login(client).status_code == 200
        assert client.get("/api/documents").status_code == 200
        assert client.get("/", headers=HTML).status_code == 200
        assert client.get("/assets/app.js").status_code == 200

    def test_the_session_passes_the_progress_stream(self, client):
        assert _login(client).status_code == 200
        with client.stream("GET", "/api/events") as resp:
            assert resp.status_code == 200
            body = "".join(resp.iter_text())
        assert "event: end" in body

    def test_behind_https_the_cookie_is_secure(self, client):
        r = client.post(
            "/api/login", json={"user": "admin", "password": "s3cret"},
            headers={"X-Forwarded-Proto": "https"},
        )
        assert r.status_code == 200
        assert "; secure" in r.headers["set-cookie"].lower()

    @pytest.mark.parametrize(
        "user,password", [("admin", "nope"), ("root", "s3cret"), ("", ""),
                          ("admin", "s3cret "), ("Admin", "s3cret")],
    )
    def test_a_wrong_login_is_refused_with_a_plain_message(
        self, client, user, password,
    ):
        r = _login(client, user, password)
        assert r.status_code == 401
        assert r.json() == {"detail": "wrong user name or password"}
        assert "set-cookie" not in r.headers
        assert "www-authenticate" not in r.headers
        assert client.get("/api/documents").status_code == 401

    @pytest.mark.parametrize(
        "body", [[], {"user": 1, "password": 2}, {"user": "admin"}, "text"],
    )
    def test_a_malformed_login_body_is_refused(self, client, body):
        r = client.post("/api/login", json=body)
        assert r.status_code in (400, 401)
        assert "set-cookie" not in r.headers

    def test_a_wrong_login_waits_before_answering(self, client, monkeypatch):
        monkeypatch.setattr(server_mod, "LOGIN_FAIL_DELAY", 0.2)
        t0 = time.monotonic()
        assert _login(client, "admin", "nope").status_code == 401
        assert time.monotonic() - t0 >= 0.2

    def test_parallel_wrong_logins_queue_behind_one_another(
        self, app, monkeypatch,
    ):
        from concurrent.futures import ThreadPoolExecutor

        delay, n = 0.05, 6
        monkeypatch.setattr(server_mod, "LOGIN_FAIL_DELAY", delay)
        # one client as a context manager: one event loop serves every thread,
        # as uvicorn's one loop does
        with TestClient(app) as c:
            t0 = time.monotonic()
            with ThreadPoolExecutor(max_workers=n) as pool:
                codes = list(pool.map(lambda _: _login(c, "admin", "nope").status_code,
                                      range(n)))
            elapsed = time.monotonic() - t0
            assert codes == [401] * n
            assert elapsed >= n * delay * 0.9
            # the right pair is never held up behind the queue of wrong ones
            assert _login(c).status_code == 200

    def test_a_tampered_cookie_is_refused(self, client):
        assert _login(client).status_code == 200
        token = client.cookies.get(SESSION_COOKIE)
        head, _, sig = token.rpartition(".")
        flipped = ("A" if sig[0] != "A" else "B") + sig[1:]
        client.cookies.clear()
        client.cookies.set(SESSION_COOKIE, f"{head}.{flipped}")
        assert client.get("/api/documents").status_code == 401

    @pytest.mark.parametrize("junk", ["", "abc", "a.b.c", "....", "%%%.%%%"])
    def test_a_junk_cookie_is_refused(self, client, junk):
        client.cookies.set(SESSION_COOKIE, junk)
        assert client.get("/api/documents").status_code == 401

    def test_an_expired_session_is_refused(self, app, client):
        now = time.time()
        client.cookies.set(
            SESSION_COOKIE, _forged_token(app, "admin", now - 13 * 3600, now - 3600),
        )
        assert client.get("/api/documents").status_code == 401

    def test_a_live_forged_session_for_another_user_is_refused(self, app, client):
        now = time.time()
        client.cookies.set(
            SESSION_COOKIE, _forged_token(app, "root", now, now + 3600),
        )
        assert client.get("/api/documents").status_code == 401

    def test_the_session_expires_after_twelve_hours(self, client, monkeypatch):
        assert _login(client).status_code == 200
        assert client.get("/api/documents").status_code == 200
        real = time.time()
        monkeypatch.setattr(
            server_mod, "_session_clock", lambda: real + SESSION_SECONDS + 1,
        )
        assert client.get("/api/documents").status_code == 401

    def test_a_cookie_from_another_server_start_is_refused(self, tmp_path, client):
        assert _login(client).status_code == 200
        token = client.cookies.get(SESSION_COOKIE)
        other_dir = tmp_path / "b"
        other_dir.mkdir()
        other = TestClient(
            _app(other_dir, auth_user="admin", auth_password="s3cret"),
        )
        other.cookies.set(SESSION_COOKIE, token)
        assert other.get("/api/documents").status_code == 401

    def test_logout_clears_the_cookie(self, client):
        assert _login(client).status_code == 200
        r = client.post("/api/logout")
        assert r.status_code == 200
        cookie = r.headers["set-cookie"].lower()
        assert cookie.startswith(f"{SESSION_COOKIE}=")
        assert "max-age=0" in cookie
        assert client.get("/api/documents").status_code == 401

    def test_basic_still_works_for_scripts(self, client):
        r = client.get("/api/documents", headers=_basic("admin", "s3cret"))
        assert r.status_code == 200
        assert "set-cookie" not in r.headers

    @pytest.mark.parametrize(
        "path", ["/login", "/api/login", "/api/status"],
    )
    def test_the_open_paths_need_no_login(self, client, path):
        if path == "/api/login":
            assert _login(client).status_code == 200
        else:
            assert client.get(path, headers=HTML).status_code == 200

    def test_a_websocket_without_a_session_is_closed(self, app):
        from starlette.websockets import WebSocketDisconnect

        @app.websocket("/ws-probe")
        async def probe(ws):  # pragma: no cover - never reached without login
            await ws.accept()
            await ws.close()

        c = TestClient(app)
        with pytest.raises(WebSocketDisconnect) as err:
            with c.websocket_connect("/ws-probe"):
                pass
        assert err.value.code == 1008


class TestNextParameter:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("/runs", "/runs"),
            ("/runs?x=1", "/runs?x=1"),
            ("/", "/"),
            (None, "/"),
            ("", "/"),
            ("//evil.example", "/"),
            ("/\\evil.example", "/"),
            ("https://evil.example", "/"),
            ("javascript:alert(1)", "/"),
            ("runs", "/"),
            ("/login", "/"),
            ("/login?next=/x", "/"),
            ("/ok\nSet-Cookie: x", "/"),
        ],
    )
    def test_only_same_site_paths_survive(self, raw, expected):
        assert server_mod.safe_next(raw) == expected

    def test_an_open_redirect_is_not_carried_in_the_login_redirect(self, tmp_path):
        c = TestClient(
            _app(tmp_path, ui=True, auth_user="admin", auth_password="s3cret"),
        )
        r = c.get(
            "http://testserver//evil.example/x", headers=HTML,
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/login?next=%2F"


class TestHalfConfigured:
    @pytest.mark.parametrize("which", [AUTH_USER_ENV, AUTH_PASSWORD_ENV])
    def test_one_variable_alone_refuses_to_build(self, tmp_path, monkeypatch, which):
        monkeypatch.setenv(which, "x")
        with pytest.raises(ValueError) as err:
            _app(tmp_path)
        assert AUTH_USER_ENV in str(err.value)
        assert AUTH_PASSWORD_ENV in str(err.value)

    def test_one_argument_alone_refuses_to_build(self, tmp_path):
        with pytest.raises(ValueError):
            _app(tmp_path, auth_user="admin", auth_password="")

    def test_a_user_name_holding_a_colon_refuses_to_build(self, tmp_path):
        with pytest.raises(ValueError) as err:
            _app(tmp_path, auth_user="ad:min", auth_password="admin123")
        assert "colon" in str(err.value)

    def test_credentials_that_are_not_text_refuse_to_build(self, tmp_path):
        with pytest.raises(ValueError):
            _app(tmp_path, auth_user="admin", auth_password="bad\udcff")


class TestServeCommand:
    def _invoke(self, tmp_path, monkeypatch):
        import uvicorn
        from typer.testing import CliRunner

        import regcompass.cli as cli_mod
        import regcompass.server as server_mod

        real_create = server_mod.create_app
        monkeypatch.setattr(
            server_mod, "create_app",
            lambda **kw: real_create(**{**kw, "ui_dir": None}),
        )
        monkeypatch.setattr(uvicorn, "run", lambda app_, **kw: None)
        # a developer's own .env must not decide what these tests see
        monkeypatch.setattr(cli_mod, "_load_dotenv", lambda *a, **k: None)
        out = tmp_path / "out"
        out.mkdir(exist_ok=True)
        return CliRunner().invoke(
            cli_mod.app,
            ["serve", "--db", str(tmp_path / "serve.db"), "--out", str(out),
             "--data-dir", str(tmp_path / "data")],
        )

    def test_serve_says_login_is_off(self, tmp_path, monkeypatch):
        r = self._invoke(tmp_path, monkeypatch)
        assert r.exit_code == 0, r.output
        assert "login off" in r.output

    def test_serve_says_login_is_on_and_never_prints_the_password(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv(AUTH_USER_ENV, "admin")
        monkeypatch.setenv(AUTH_PASSWORD_ENV, "pw-must-not-show")
        r = self._invoke(tmp_path, monkeypatch)
        assert r.exit_code == 0, r.output
        assert "login on" in r.output
        assert "pw-must-not-show" not in r.output

    def test_serve_refuses_one_variable_alone(self, tmp_path, monkeypatch):
        monkeypatch.setenv(AUTH_PASSWORD_ENV, "x")
        r = self._invoke(tmp_path, monkeypatch)
        assert r.exit_code != 0
        assert AUTH_USER_ENV in r.output
