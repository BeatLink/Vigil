import pytest

from vigil.plugins.radicale import Radicale
from vigil.core.connectors.types import HttpRequest, HttpResult
from vigil.core.database.database import db, StatusHistory, Metric


BASE_CFG = {
    "name": "test-radicale",
    "id":   "test-radicale",
    "url": "http://radicale.test:5232",
    "username": "vigil",
    "password": "hunter2",
    "ssh_config": {"host": "test.host"},
}


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(Radicale, BASE_CFG)


def _respond(plugin, run_requests, status=207, body="<multistatus/>", elapsed_ms=30.0):
    return run_requests(plugin, lambda r: HttpResult(
        status_code=status, text=body, elapsed_ms=elapsed_ms))


def _latest_status(plugin_id: str = "test-radicale") -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str, name: str = "test-radicale") -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


class TestRequests:
    def test_propfind_with_headers_body_and_auth(self, plugin):
        reqs = plugin.requests()
        assert len(reqs) == 1
        req = reqs[0]
        assert isinstance(req, HttpRequest)
        assert req.method == "PROPFIND"
        assert req.url == "http://radicale.test:5232/"
        assert req.headers == {"Depth": "0", "Content-Type": "application/xml"}
        assert "<propfind" in req.body
        assert req.auth == ("vigil", "hunter2")

    def test_no_url_yields_no_requests(self, make_plugin):
        p = make_plugin(Radicale, {"name": "r", "id": "r",
                                   "ssh_config": {"host": "h"}})
        assert p.requests() == []

    def test_password_command_resolved_at_construction(self, make_plugin):
        p = make_plugin(Radicale, {**BASE_CFG, "password": None,
                                   "password_command": "printf remote-secret"})
        assert p.requests()[0].auth == ("vigil", "remote-secret")


class TestRadicaleCollection:
    async def test_207_sets_online(self, plugin, run_requests):
        _respond(plugin, run_requests, status=207)
        assert _latest_status() == "online"

    async def test_401_sets_failed(self, plugin, run_requests):
        _respond(plugin, run_requests, status=401)
        assert _latest_status() == "failed"

    async def test_500_sets_failed(self, plugin, run_requests):
        _respond(plugin, run_requests, status=500)
        assert _latest_status() == "failed"

    async def test_http_error_sets_failed(self, plugin, run_requests):
        run_requests(plugin, lambda r: HttpResult(
            status_code=None, text="", error="connection refused"))
        assert _latest_status() == "failed"

    async def test_ok_records_metric_and_latency(self, plugin, run_requests):
        _respond(plugin, run_requests, status=207, elapsed_ms=33.0)
        assert _latest_metric("propfind_ok") == 1.0
        assert _latest_metric("propfind_latency_ms") == pytest.approx(33.0)

    async def test_missing_url_sets_failed(self, make_plugin, run_requests):
        p = make_plugin(Radicale, {"name": "r", "id": "r",
                                   "ssh_config": {"host": "h"}})
        run_requests(p, lambda r: HttpResult(status_code=207, text="<multistatus/>"))
        assert _latest_status("r") == "failed"


class TestRadicaleActions:
    async def test_on_action_always_returns_false(self, plugin):
        assert plugin.plan_action("anything") is None
