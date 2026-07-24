import base64

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vigil.core.ui.auth import register_auth


def _basic(username: str, password: str) -> dict:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _make_app(auth_settings: dict) -> FastAPI:
    app = FastAPI()
    register_auth(app, auth_settings)

    @app.get("/x")
    def x():
        return {"ok": True}

    return app


class TestRegisterAuthDisabled:
    def test_no_auth_settings_leaves_routes_open(self):
        app = _make_app({})
        client = TestClient(app)
        assert client.get("/x").status_code == 200

    def test_username_without_password_disables_auth(self, caplog):
        app = _make_app({"username": "admin"})
        client = TestClient(app)
        assert client.get("/x").status_code == 200

    def test_password_without_username_disables_auth(self):
        app = _make_app({"password": "secret"})
        client = TestClient(app)
        assert client.get("/x").status_code == 200


class TestRegisterAuthEnabled:
    def test_rejects_missing_credentials(self):
        app = _make_app({"username": "admin", "password": "secret"})
        client = TestClient(app)
        resp = client.get("/x")
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == 'Basic realm="Vigil"'

    def test_rejects_wrong_password(self):
        app = _make_app({"username": "admin", "password": "secret"})
        client = TestClient(app)
        resp = client.get("/x", headers=_basic("admin", "wrong"))
        assert resp.status_code == 401

    def test_rejects_wrong_username(self):
        app = _make_app({"username": "admin", "password": "secret"})
        client = TestClient(app)
        resp = client.get("/x", headers=_basic("someone-else", "secret"))
        assert resp.status_code == 401

    def test_accepts_correct_credentials(self):
        app = _make_app({"username": "admin", "password": "secret"})
        client = TestClient(app)
        resp = client.get("/x", headers=_basic("admin", "secret"))
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_rejects_non_basic_scheme(self):
        app = _make_app({"username": "admin", "password": "secret"})
        client = TestClient(app)
        resp = client.get("/x", headers={"Authorization": "Bearer sometoken"})
        assert resp.status_code == 401

    def test_rejects_malformed_base64(self):
        app = _make_app({"username": "admin", "password": "secret"})
        client = TestClient(app)
        resp = client.get("/x", headers={"Authorization": "Basic not-valid-base64!!"})
        assert resp.status_code == 401


class TestPasswordFile:
    def test_reads_password_from_file(self, tmp_path):
        pw_file = tmp_path / "password"
        pw_file.write_text("filesecret\n")
        app = _make_app({"username": "admin", "password_file": str(pw_file)})
        client = TestClient(app)
        assert client.get("/x", headers=_basic("admin", "filesecret")).status_code == 200
        assert client.get("/x", headers=_basic("admin", "wrong")).status_code == 401

    def test_missing_password_file_disables_auth(self, tmp_path):
        app = _make_app({"username": "admin", "password_file": str(tmp_path / "missing")})
        client = TestClient(app)
        assert client.get("/x").status_code == 200

    def test_explicit_password_takes_precedence_over_file(self, tmp_path):
        pw_file = tmp_path / "password"
        pw_file.write_text("fromfile")
        app = _make_app({
            "username": "admin",
            "password": "fromconfig",
            "password_file": str(pw_file),
        })
        client = TestClient(app)
        assert client.get("/x", headers=_basic("admin", "fromconfig")).status_code == 200
        assert client.get("/x", headers=_basic("admin", "fromfile")).status_code == 401
