import json
import time

import pytest

from vigil.plugins.freshrss import Freshrss, _parse_response, _fever_api_key
from vigil.core.connectors.types import HttpRequest, HttpResult
from vigil.core.database.database import db, StatusHistory, Metric


BASE_CFG = {
    "name": "test-freshrss",
    "id":   "test-freshrss",
    "api_url": "http://freshrss.test:80",
    "username": "beatlink",
    "api_password": "apipw",
    "feed_stale_warning": 48,
    "feed_stale_threshold": 168,
    "refresh_stale_warning": 6,
    "ssh_config": {"host": "test.host"},
}


def _feed(title="Example", hours_ago=1.0):
    return {"id": 1, "title": title, "last_updated_on_time": time.time() - hours_ago * 3600}


def _response(feeds=None, refresh_hours_ago=1.0, auth=1):
    return json.dumps({
        "api_version": 3,
        "auth": auth,
        "last_refreshed_on_time": time.time() - refresh_hours_ago * 3600,
        "feeds": feeds if feeds is not None else [_feed()],
    })


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(Freshrss, BASE_CFG)


def _respond(plugin, run_requests, feeds=None, refresh_hours_ago=1.0, auth=1):
    run_requests(plugin, lambda r: HttpResult(
        status_code=200, text=_response(feeds, refresh_hours_ago, auth)))


def _latest_status(plugin_id: str = "test-freshrss") -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.plugin_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str, name: str = "test-freshrss") -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.plugin_id == name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


class TestRequests:
    def test_posts_md5_token_as_form_body(self, plugin):
        import hashlib
        reqs = plugin.requests()
        assert len(reqs) == 1
        req = reqs[0]
        assert isinstance(req, HttpRequest)
        assert req.method == "POST"
        assert req.url == "http://freshrss.test:80/api/fever.php?api&feeds"
        expected = hashlib.md5(b"beatlink:apipw").hexdigest()
        assert req.body == f"api_key={expected}"
        assert req.headers == {"Content-Type": "application/x-www-form-urlencoded"}

    def test_fever_api_key_matches_protocol(self):
        import hashlib
        assert _fever_api_key("u", "p") == hashlib.md5(b"u:p").hexdigest()

    def test_no_url_yields_no_requests(self, make_plugin):
        p = make_plugin(Freshrss, {"name": "f", "id": "f", "username": "u",
                                   "ssh_config": {"host": "h"}})
        assert p.requests() == []

    def test_password_command_resolved_at_construction(self, make_plugin):
        import hashlib
        p = make_plugin(Freshrss, {**BASE_CFG, "api_password": None,
                                   "api_password_command": "printf remotepw"})
        expected = hashlib.md5(b"beatlink:remotepw").hexdigest()
        assert p.requests()[0].body == f"api_key={expected}"


class TestParseResponse:
    def test_parses_feeds(self):
        data = _parse_response(_response())
        assert len(data["feeds"]) == 1

    def test_auth_zero_raises(self):
        with pytest.raises(ValueError, match="rejected the credentials"):
            _parse_response(_response(auth=0))

    def test_malformed_json_raises(self):
        with pytest.raises(ValueError):
            _parse_response("not json")


class TestFreshrssCollection:
    async def test_fresh_feeds_set_online(self, plugin, run_requests):
        _respond(plugin, run_requests, feeds=[_feed(hours_ago=1.0)], refresh_hours_ago=1.0)
        assert _latest_status() == "online"

    async def test_stale_feed_sets_warning(self, plugin, run_requests):
        _respond(plugin, run_requests, feeds=[_feed(hours_ago=60.0)])
        assert _latest_status() == "warning"

    async def test_very_stale_feed_sets_failed(self, plugin, run_requests):
        _respond(plugin, run_requests, feeds=[_feed(hours_ago=200.0)])
        assert _latest_status() == "failed"

    async def test_stale_refresh_cycle_sets_warning(self, plugin, run_requests):
        _respond(plugin, run_requests, feeds=[_feed(hours_ago=10.0)], refresh_hours_ago=10.0)
        assert _latest_status() == "warning"
        assert _latest_metric("refresh_age_hours") == pytest.approx(10.0, abs=0.01)

    async def test_refresh_age_is_newest_fetch_not_fever_field(self, plugin, run_requests):
        _respond(plugin, run_requests,
                 feeds=[_feed("Paused", hours_ago=14.0), _feed("Live", hours_ago=1.0)],
                 refresh_hours_ago=14.0)
        assert _latest_status() == "online"
        assert _latest_metric("refresh_age_hours") == pytest.approx(1.0, abs=0.01)

    async def test_auth_failure_sets_failed(self, plugin, run_requests):
        _respond(plugin, run_requests, auth=0)
        assert _latest_status() == "failed"

    async def test_http_error_sets_failed(self, plugin, run_requests):
        run_requests(plugin, lambda r: HttpResult(
            status_code=None, text="", error="connection refused"))
        assert _latest_status() == "failed"

    async def test_missing_username_sets_failed(self, make_plugin, run_requests):
        cfg = {k: v for k, v in BASE_CFG.items() if k != "username"}
        p = make_plugin(Freshrss, cfg)
        run_requests(p, lambda r: HttpResult(status_code=200, text="{}"))
        assert _latest_status("test-freshrss") == "failed"


class TestFreshrssActions:
    async def test_on_action_always_returns_false(self, plugin):
        assert plugin.plan_action("anything") is None
