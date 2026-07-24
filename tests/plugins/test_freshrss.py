import json
import time
from unittest.mock import AsyncMock

import pytest

from vigil.plugins.freshrss import FreshrssCollectorPlugin, _parse_response, _build_fetch_script
from vigil.core.connectors.orchestration.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric


BASE_CFG = {
    "name": "test-freshrss",
    "id":   "test-freshrss",
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
    return make_plugin(FreshrssCollectorPlugin, BASE_CFG)


def _respond(plugin, run_cycle, feeds=None, refresh_hours_ago=1.0, auth=1):
    run_cycle(plugin, lambda c: CmdResult(0, _response(feeds, refresh_hours_ago, auth), ""))


def _latest_status(plugin_id: str = "test-freshrss") -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str, name: str = "test-freshrss") -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


class TestBuildFetchScript:
    def test_computes_md5_token(self):
        script = _build_fetch_script("http://127.0.0.1", 10, "user", None, "pw")
        assert "md5sum" in script

    def test_password_command_runs_on_remote_host(self):
        script = _build_fetch_script(
            "http://127.0.0.1", 10, "user", "cat /run/secrets/freshrss_api_password", None)
        assert "cat /run/secrets/freshrss_api_password" in script


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
    async def test_fresh_feeds_set_online(self, plugin, run_cycle):
        _respond(plugin, run_cycle, feeds=[_feed(hours_ago=1.0)], refresh_hours_ago=1.0)
        assert _latest_status() == "online"

    async def test_stale_feed_sets_warning(self, plugin, run_cycle):
        _respond(plugin, run_cycle, feeds=[_feed(hours_ago=60.0)])
        assert _latest_status() == "warning"

    async def test_very_stale_feed_sets_failed(self, plugin, run_cycle):
        _respond(plugin, run_cycle, feeds=[_feed(hours_ago=200.0)])
        assert _latest_status() == "failed"

    async def test_stale_refresh_cycle_sets_warning(self, plugin, run_cycle):
        _respond(plugin, run_cycle, refresh_hours_ago=10.0)
        assert _latest_status() == "warning"

    async def test_auth_failure_sets_failed(self, plugin, run_cycle):
        _respond(plugin, run_cycle, auth=0)
        assert _latest_status() == "failed"

    async def test_ssh_failure_sets_failed(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(1, "", "connection refused"))
        assert _latest_status() == "failed"

    async def test_missing_username_sets_failed(self, make_plugin, run_cycle):
        cfg = {k: v for k, v in BASE_CFG.items() if k != "username"}
        p = make_plugin(FreshrssCollectorPlugin, cfg)
        run_cycle(p)
        assert _latest_status("test-freshrss") == "failed"


class TestFreshrssActions:
    async def test_on_action_always_returns_false(self, plugin):
        assert plugin.plan_action("anything") is None
