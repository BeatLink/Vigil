import pytest

from vigil.plugins.calibre_web import CalibreWeb, _looks_like_opds
from vigil.core.connectors.types import HttpRequest, HttpResult
from vigil.core.database.database import db, StatusHistory, Metric


BASE_CFG = {
    "name": "test-calibre-web",
    "id":   "test-calibre-web",
    "url": "http://calibre.test:8083",
    "username": "vigil",
    "password": "hunter2",
    "ssh_config": {"host": "test.host"},
}

_OPDS_BODY = '<feed xmlns="http://www.w3.org/2005/Atom"><title>Calibre-Web</title></feed>'


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(CalibreWeb, BASE_CFG)


def _result(status=200, body=_OPDS_BODY, elapsed_ms=50.0):
    return HttpResult(status_code=status, text=body, elapsed_ms=elapsed_ms)


def _latest_status(plugin_id: str = "test-calibre-web") -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str, name: str = "test-calibre-web") -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


class TestLooksLikeOpds:
    def test_real_feed(self):
        assert _looks_like_opds(_OPDS_BODY) is True

    def test_html_page(self):
        assert _looks_like_opds("<html><body>Login</body></html>") is False


class TestRequests:
    def test_targets_opds_with_basic_auth(self, plugin):
        reqs = plugin.requests()
        assert len(reqs) == 1
        assert isinstance(reqs[0], HttpRequest)
        assert reqs[0].url == "http://calibre.test:8083/opds"
        assert reqs[0].auth == ("vigil", "hunter2")

    def test_no_url_yields_no_requests(self, make_plugin):
        p = make_plugin(CalibreWeb, {"name": "c", "id": "c",
                                     "ssh_config": {"host": "h"}})
        assert p.requests() == []

    def test_password_command_resolved_at_construction(self, make_plugin):
        p = make_plugin(CalibreWeb, {**BASE_CFG, "password": None,
                                     "password_command": "printf secret123"})
        assert p.requests()[0].auth == ("vigil", "secret123")


class TestCalibreWebCollection:
    async def test_valid_feed_sets_online(self, plugin, run_requests):
        run_requests(plugin, lambda r: _result(status=200, body=_OPDS_BODY))
        assert _latest_status() == "online"

    async def test_records_latency(self, plugin, run_requests):
        run_requests(plugin, lambda r: _result(elapsed_ms=42.0))
        assert _latest_metric("feed_latency_ms") == pytest.approx(42.0)

    async def test_login_page_with_200_sets_failed(self, plugin, run_requests):
        run_requests(plugin, lambda r: _result(status=200, body="<html><body>Please log in</body></html>"))
        assert _latest_status() == "failed"

    async def test_401_sets_failed(self, plugin, run_requests):
        run_requests(plugin, lambda r: _result(status=401, body=""))
        assert _latest_status() == "failed"

    async def test_http_error_sets_failed(self, plugin, run_requests):
        run_requests(plugin, lambda r: HttpResult(
            status_code=None, text="", error="connection refused"))
        assert _latest_status() == "failed"

    async def test_missing_url_sets_failed(self, make_plugin, run_requests):
        p = make_plugin(CalibreWeb, {"name": "c", "id": "c",
                                     "ssh_config": {"host": "h"}})
        run_requests(p, lambda r: _result())
        assert _latest_status("c") == "failed"


class TestCalibreWebActions:
    async def test_on_action_always_returns_false(self, plugin):
        assert plugin.plan_action("anything") is None
