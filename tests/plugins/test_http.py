import pytest

from vigil.plugins.http import HttpCheck, _body_mismatch
from vigil.core.connectors.types import CmdResult, HttpRequest, HttpResult
from vigil.core.database.database import db, StatusHistory, Metric


FEED_CFG = {
    "name": "test-http",
    "id":   "test-http",
    "url": "http://calibre.test:8083/opds",
    "username": "vigil",
    "password": "hunter2",
    "expect": {"body_contains": "<feed", "body_contains_any": ["atom", "opds"]},
    "ssh_config": {"host": "test.host"},
}

DAV_CFG = {
    "name": "test-http",
    "id":   "test-http",
    "url": "http://radicale.test:5232/",
    "method": "PROPFIND",
    "headers": {"Depth": "0", "Content-Type": "application/xml"},
    "body": '<?xml version="1.0"?><propfind xmlns="DAV:"/>',
    "username": "vigil",
    "password": "hunter2",
    "expect": {"status": 207},
    "ssh_config": {"host": "test.host"},
}

WS_CFG = {
    "name": "test-http",
    "id":   "test-http",
    "url": "ws://127.0.0.1:9777/ws",
    "body": '{"type":1,"payload":{}}',
    "expect": {"body_contains": '"appearance":1'},
    "ssh_config": {"host": "test.host"},
}

FEED_BODY = '<feed xmlns="http://www.w3.org/2005/Atom"><title>Library</title></feed>'


@pytest.fixture
def feed_plugin(make_plugin):
    return make_plugin(HttpCheck, FEED_CFG)


@pytest.fixture
def ws_plugin(make_plugin):
    return make_plugin(HttpCheck, WS_CFG)


def _respond(plugin, run_requests, status=200, body=FEED_BODY, elapsed_ms=30.0):
    return run_requests(plugin, lambda r: HttpResult(
        status_code=status, text=body, elapsed_ms=elapsed_ms))


def _latest_status(plugin_id: str = "test-http") -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.plugin_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str, name: str = "test-http") -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.plugin_id == name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


class TestBodyMatching:
    def test_real_feed_passes(self):
        assert not _body_mismatch(FEED_BODY, FEED_CFG["expect"])

    def test_html_page_fails(self):
        assert _body_mismatch("<html><body>Login</body></html>", FEED_CFG["expect"])

    def test_no_expect_passes_anything(self):
        assert not _body_mismatch("whatever", {})


class TestRequests:
    def test_get_with_basic_auth(self, feed_plugin):
        reqs = feed_plugin.requests()
        assert len(reqs) == 1
        req = reqs[0]
        assert isinstance(req, HttpRequest)
        assert req.method == "GET"
        assert req.url == "http://calibre.test:8083/opds"
        assert req.auth == ("vigil", "hunter2")

    def test_method_headers_and_body_carried(self, make_plugin):
        req = make_plugin(HttpCheck, DAV_CFG).requests()[0]
        assert req.method == "PROPFIND"
        assert req.headers == {"Depth": "0", "Content-Type": "application/xml"}
        assert "<propfind" in req.body

    def test_no_url_yields_no_requests(self, make_plugin):
        p = make_plugin(HttpCheck, {"name": "h", "id": "h",
                                    "ssh_config": {"host": "h"}})
        assert p.requests() == []

    def test_password_command_resolved_at_construction(self, make_plugin):
        cfg = {**FEED_CFG, "password": None, "password_command": "echo s3cret"}
        p = make_plugin(HttpCheck, cfg)
        assert p.requests()[0].auth == ("vigil", "s3cret")

    def test_ws_url_probes_with_websocat(self, ws_plugin):
        cmd = ws_plugin.commands()[0].text
        assert "websocat" in cmd
        assert "ws://127.0.0.1:9777/ws" in cmd
        assert '{"type":1,"payload":{}}' in cmd

    def test_ws_plugin_offers_sample_stream(self, ws_plugin):
        assert ws_plugin.event_driven()


@pytest.mark.asyncio
class TestHttpParsing:
    async def test_expected_status_and_body_sets_online(self, feed_plugin, run_requests):
        _respond(feed_plugin, run_requests)
        assert _latest_status() == "online"

    async def test_records_latency(self, feed_plugin, run_requests):
        _respond(feed_plugin, run_requests, elapsed_ms=42.0)
        assert _latest_metric("probe_latency_ms") == 42.0

    async def test_login_page_with_200_sets_failed(self, feed_plugin, run_requests):
        _respond(feed_plugin, run_requests, body="<html>Login</html>")
        assert _latest_status() == "failed"

    async def test_401_sets_failed(self, feed_plugin, run_requests):
        _respond(feed_plugin, run_requests, status=401, body="")
        assert _latest_status() == "failed"

    async def test_non_default_expected_status(self, make_plugin, run_requests):
        p = make_plugin(HttpCheck, DAV_CFG)
        run_requests(p, lambda r: HttpResult(status_code=207, text="<multistatus/>", elapsed_ms=10.0))
        assert _latest_status() == "online"

    async def test_http_error_sets_failed(self, feed_plugin, run_requests):
        run_requests(feed_plugin, lambda r: HttpResult(
            status_code=None, text="", error="connection refused"))
        assert _latest_status() == "failed"

    async def test_missing_url_sets_failed(self, make_plugin, run_requests):
        p = make_plugin(HttpCheck, {"name": "h", "id": "h",
                                    "ssh_config": {"host": "h"}})
        result = run_requests(p)
        assert result.status == "failed"


@pytest.mark.asyncio
class TestWsParsing:
    async def test_matching_reply_sets_online(self, ws_plugin, run_cycle):
        run_cycle(ws_plugin, lambda c: CmdResult(0, '{"type":1,"payload":{"appearance":1}}', ""))
        assert _latest_status() == "online"
        assert _latest_metric("probe_ok") == 1.0

    async def test_mismatched_reply_sets_failed(self, ws_plugin, run_cycle):
        run_cycle(ws_plugin, lambda c: CmdResult(0, '{"type":0,"payload":{"appearance":3}}', ""))
        assert _latest_status() == "failed"

    async def test_probe_failure_sets_failed(self, ws_plugin, run_cycle):
        run_cycle(ws_plugin, lambda c: CmdResult(1, "", "connection refused"))
        assert _latest_status() == "failed"
        assert _latest_metric("probe_ok") == 0.0


@pytest.mark.asyncio
class TestActions:
    async def test_on_action_always_returns_none(self, feed_plugin):
        assert feed_plugin.plan_action("anything") is None
