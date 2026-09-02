import base64
import re
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from vigil.core.ui import auth as auth_module
from vigil.core.ui.auth import COOKIE_NAME, build_config, issue_session, read_session, register_auth

HTML = {"Accept": "text/html,application/xhtml+xml"}
CREDS = {"username": "admin", "password": "secret", "session_secret": "test-key"}


def _basic(username: str, password: str) -> dict:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _make_app(auth_settings: dict) -> FastAPI:
    app = FastAPI()
    register_auth(app, auth_settings)

    @app.get("/")
    def index():
        return {"page": True}

    @app.get("/x")
    def x():
        return {"ok": True}

    @app.get("/api/monitors")
    def monitors():
        return {"monitors": []}

    @app.get("/api/push/mon/token")
    def push():
        return {"pushed": True}

    return app


def _client(auth_settings: dict = None) -> TestClient:
    settings = CREDS if auth_settings is None else auth_settings
    return TestClient(_make_app(settings), follow_redirects=False)


def _session_cookie_header(resp) -> str:
    return next(h for h in resp.headers.get_list("set-cookie") if h.startswith(COOKIE_NAME))


def _csrf(client: TestClient) -> str:
    page = client.get("/login")
    return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)


def _sign_in(client: TestClient, username="admin", password="secret", **extra):
    form = {"username": username, "password": password, "csrf": _csrf(client), **extra}
    return client.post("/login", data=form)


class TestDisabled:
    def test_no_auth_settings_leaves_routes_open(self):
        client = _client({})
        assert client.get("/x").status_code == 200

    def test_username_without_password_disables_auth(self):
        client = _client({"username": "admin"})
        assert client.get("/x").status_code == 200

    def test_password_without_username_disables_auth(self):
        client = _client({"password": "secret"})
        assert client.get("/x").status_code == 200

    def test_no_login_page_when_auth_is_off(self):
        client = _client({})
        assert client.get("/login").status_code == 404


class TestGate:
    def test_browser_request_redirects_to_login(self):
        client = _client()
        resp = client.get("/", headers=HTML)
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/login?next=")

    def test_redirect_preserves_the_original_target(self):
        client = _client()
        resp = client.get("/x?a=1", headers=HTML)
        _sign_in(client)
        assert client.get(resp.headers["location"]).headers["location"] == "/x?a=1"

    def test_api_request_gets_401_without_a_basic_prompt(self):
        client = _client()
        resp = client.get("/api/monitors")
        assert resp.status_code == 401
        assert "www-authenticate" not in resp.headers

    def test_push_endpoint_stays_public(self):
        client = _client()
        assert client.get("/api/push/mon/token").status_code == 200

    def test_login_page_is_reachable_unauthenticated(self):
        client = _client()
        resp = client.get("/login")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]


class TestSignIn:
    def test_correct_credentials_set_a_session_and_redirect(self):
        client = _client()
        resp = _sign_in(client)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"
        assert COOKIE_NAME in client.cookies

    def test_session_cookie_opens_every_route(self):
        client = _client()
        _sign_in(client)
        assert client.get("/x").status_code == 200
        assert client.get("/api/monitors").status_code == 200

    def test_wrong_password_re_renders_the_form_with_an_error(self):
        client = _client()
        resp = _sign_in(client, password="wrong")
        assert resp.status_code == 401
        assert "Incorrect username or password." in resp.text
        assert COOKIE_NAME not in client.cookies

    def test_wrong_username_is_rejected(self):
        client = _client()
        assert _sign_in(client, username="someone-else").status_code == 401

    def test_missing_csrf_token_is_rejected(self):
        client = _client()
        client.get("/login")
        resp = client.post("/login", data={"username": "admin", "password": "secret"})
        assert resp.status_code == 400
        assert COOKIE_NAME not in client.cookies

    def test_mismatched_csrf_token_is_rejected(self):
        client = _client()
        _csrf(client)
        resp = client.post("/login", data={"username": "admin", "password": "secret",
                                          "csrf": "forged"})
        assert resp.status_code == 400

    def test_remember_me_persists_the_cookie(self):
        client = _client()
        resp = _sign_in(client, remember="1")
        assert "Max-Age=" in _session_cookie_header(resp)

    def test_without_remember_me_the_cookie_is_session_scoped(self):
        client = _client()
        resp = _sign_in(client)
        assert "Max-Age=" not in _session_cookie_header(resp)

    def test_session_cookie_is_httponly_and_samesite_lax(self):
        client = _client()
        cookie = _session_cookie_header(_sign_in(client))
        assert "HttpOnly" in cookie
        assert "SameSite=lax" in cookie

    def test_signing_in_follows_the_next_target(self):
        client = _client()
        location = client.get("/x", headers=HTML).headers["location"]
        next_token = location.split("next=")[1]
        form = {"username": "admin", "password": "secret", "csrf": _csrf(client),
                "next": next_token}
        assert client.post("/login", data=form).headers["location"] == "/x"

    def test_an_offsite_next_target_falls_back_to_the_root(self):
        client = _client()
        from vigil.core.ui.auth import encode_next
        form = {"username": "admin", "password": "secret", "csrf": _csrf(client),
                "next": encode_next("//evil.example/")}
        assert client.post("/login", data=form).headers["location"] == "/"

    def test_visiting_login_while_signed_in_redirects_away(self):
        client = _client()
        _sign_in(client)
        assert client.get("/login").status_code == 303


class TestSignOut:
    def test_logout_clears_the_session(self):
        client = _client()
        _sign_in(client)
        resp = client.get("/logout")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"
        assert client.get("/x").status_code == 401


class TestThrottle:
    def test_repeated_failures_are_throttled(self):
        client = _client()
        for _ in range(5):
            assert _sign_in(client, password="wrong").status_code == 401
        resp = _sign_in(client, password="wrong")
        assert resp.status_code == 429
        assert int(resp.headers["Retry-After"]) > 0

    def test_throttling_also_blocks_the_correct_password(self):
        client = _client()
        for _ in range(5):
            _sign_in(client, password="wrong")
        assert _sign_in(client).status_code == 429

    def test_a_successful_sign_in_clears_the_counter(self):
        client = _client()
        for _ in range(4):
            _sign_in(client, password="wrong")
        assert _sign_in(client).status_code == 303
        client.cookies.clear()
        assert _sign_in(client, password="wrong").status_code == 401


class TestBasicFallback:
    def test_basic_credentials_still_open_the_api(self):
        client = _client()
        assert client.get("/api/monitors", headers=_basic("admin", "secret")).status_code == 200

    def test_wrong_basic_credentials_are_rejected(self):
        client = _client()
        assert client.get("/api/monitors", headers=_basic("admin", "wrong")).status_code == 401

    def test_non_basic_scheme_is_rejected(self):
        client = _client()
        resp = client.get("/api/monitors", headers={"Authorization": "Bearer sometoken"})
        assert resp.status_code == 401

    def test_malformed_base64_is_rejected(self):
        client = _client()
        resp = client.get("/api/monitors", headers={"Authorization": "Basic not-valid!!"})
        assert resp.status_code == 401


class TestSessionToken:
    def test_a_token_round_trips(self):
        config = build_config(CREDS)
        token, lifetime = issue_session(config, remember=False)
        assert lifetime == 12 * 3600
        assert read_session(token, config) == "admin"

    def test_remember_uses_the_longer_lifetime(self):
        config = build_config(CREDS)
        _, lifetime = issue_session(config, remember=True)
        assert lifetime == 30 * 86400

    def test_lifetimes_are_configurable(self):
        config = build_config({**CREDS, "session_hours": 2, "remember_days": 1})
        assert issue_session(config, remember=False)[1] == 2 * 3600
        assert issue_session(config, remember=True)[1] == 86400

    def test_an_invalid_lifetime_falls_back_to_the_default(self):
        config = build_config({**CREDS, "session_hours": "nonsense"})
        assert issue_session(config, remember=False)[1] == 12 * 3600

    def test_a_tampered_payload_is_rejected(self):
        config = build_config(CREDS)
        token, _ = issue_session(config, remember=False)
        version, payload, signature = token.split(".")
        assert read_session(f"{version}.{payload}x.{signature}", config) is None

    def test_a_token_signed_with_another_key_is_rejected(self):
        token, _ = issue_session(build_config({**CREDS, "session_secret": "other"}), False)
        assert read_session(token, build_config(CREDS)) is None

    def test_an_expired_token_is_rejected(self, monkeypatch):
        config = build_config(CREDS)
        token, _ = issue_session(config, remember=False)
        later = time.time() + 13 * 3600
        monkeypatch.setattr(auth_module.time, "time", lambda: later)
        assert read_session(token, config) is None

    def test_a_token_for_another_username_is_rejected(self):
        token, _ = issue_session(build_config({**CREDS, "username": "someone"}), False)
        assert read_session(token, build_config(CREDS)) is None

    def test_garbage_tokens_are_rejected(self):
        config = build_config(CREDS)
        for token in ("", "nonsense", "v1.only-one-part", "v2.a.b"):
            assert read_session(token, config) is None

    def test_a_forged_cookie_does_not_open_the_dashboard(self):
        client = _client()
        client.cookies.set(COOKIE_NAME, "v1.forged.signature")
        assert client.get("/api/monitors").status_code == 401


class TestSecretFiles:
    def test_password_is_read_from_a_file(self, tmp_path):
        pw_file = tmp_path / "password"
        pw_file.write_text("filesecret\n")
        client = _client({"username": "admin", "password_file": str(pw_file)})
        assert _sign_in(client, password="filesecret").status_code == 303

    def test_missing_password_file_disables_auth(self, tmp_path):
        client = _client({"username": "admin", "password_file": str(tmp_path / "missing")})
        assert client.get("/x").status_code == 200

    def test_explicit_password_takes_precedence_over_the_file(self, tmp_path):
        pw_file = tmp_path / "password"
        pw_file.write_text("fromfile")
        client = _client({"username": "admin", "password": "fromconfig",
                          "password_file": str(pw_file)})
        assert _sign_in(client, password="fromconfig").status_code == 303
        client.cookies.clear()
        assert _sign_in(client, password="fromfile").status_code == 401

    def test_session_secret_is_read_from_a_file(self, tmp_path):
        secret_file = tmp_path / "session"
        secret_file.write_text("shared-key\n")
        settings = {"username": "admin", "password": "secret",
                    "session_secret_file": str(secret_file)}
        token, _ = issue_session(build_config(settings), remember=False)
        assert read_session(token, build_config(settings)) == "admin"

    def test_a_generated_secret_differs_between_starts(self):
        settings = {"username": "admin", "password": "secret"}
        assert build_config(settings).secret != build_config(settings).secret


class TestLoginPageTheme:
    def test_the_page_follows_the_browser_scheme_by_default(self):
        client = _client()
        assert '<html lang="en">' in client.get("/login").text

    def test_a_pinned_scheme_is_written_onto_the_document(self, monkeypatch):
        from vigil.core.ui import theme
        monkeypatch.setattr(theme, "_forced_scheme", "dark")
        assert '<html lang="en" data-theme="dark">' in _client().get("/login").text

    def test_configured_token_overrides_reach_the_page(self, monkeypatch):
        from vigil.core.ui import theme
        monkeypatch.setattr(theme, "_overrides", {"accent": "#ff00ff"})
        assert "--accent: #ff00ff;" in _client().get("/login").text
