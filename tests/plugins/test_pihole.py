import json
import time
from unittest.mock import AsyncMock

import pytest

from vigil.plugins.pihole import (
    Pihole,
    _SEP,
    _build_blocking_script,
    _build_fetch_script,
    _build_gravity_script,
    _format_age,
    _parse_response,
)
from vigil.core.connectors.orchestration.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric


BASE_CFG = {
    "name": "test-pihole",
    "id":   "test-pihole",
    "api_url": "http://127.0.0.1:9018",
    "block_rate_warning":   5,
    "block_rate_threshold": 1,
    "gravity_max_age": "8d",
    "min_queries": 100,
    "ssh_config": {"host": "test.host"},
}


def _summary(total=237819, blocked=38288, percent=16.1, domains=347306,
             last_update=None, active_clients=16, forwarded=2344, cached=196457):
    if last_update is None:
        last_update = time.time() - 3600
    return {
        "queries": {
            "total": total,
            "blocked": blocked,
            "percent_blocked": percent,
            "unique_domains": 1030,
            "forwarded": forwarded,
            "cached": cached,
        },
        "clients": {"active": active_clients, "total": 17},
        "gravity": {"domains_being_blocked": domains, "last_update": last_update},
    }


def _response(summary=None, blocking="enabled"):
    body = json.dumps(summary if summary is not None else _summary())
    return f'{body}\n{_SEP}\n{json.dumps({"blocking": blocking, "timer": None})}'


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(Pihole, BASE_CFG)


def _respond(plugin, run_cycle, summary=None, blocking="enabled"):
    return run_cycle(plugin, lambda c: CmdResult(0, _response(summary, blocking), ""))


def _latest_status(plugin_id: str = "test-pihole") -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str, name: str = "test-pihole") -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


class TestFormatAge:
    def test_minutes(self):
        assert _format_age(1800) == "30m"

    def test_hours_and_minutes(self):
        assert _format_age(5400) == "1h 30m"

    def test_days_and_hours(self):
        assert _format_age(280800) == "3d 6h"


class TestBuildFetchScript:
    def test_requests_both_endpoints(self):
        script = _build_fetch_script("http://127.0.0.1:9018", 10, None, None)
        assert "/api/stats/summary" in script
        assert "/api/dns/blocking" in script

    def test_no_auth_when_no_password(self):
        script = _build_fetch_script("http://127.0.0.1:9018", 10, None, None)
        assert "X-FTL-SID" not in script
        assert "/api/auth" not in script

    def test_authenticates_when_password_given(self):
        script = _build_fetch_script("http://127.0.0.1:9018", 10, None, "hunter2")
        assert "/api/auth" in script
        assert "X-FTL-SID" in script

    def test_password_command_runs_on_remote_host(self):
        script = _build_fetch_script(
            "http://127.0.0.1:9018", 10, "cat /run/secrets/pihole_api", None)
        assert "cat /run/secrets/pihole_api" in script

    def test_trailing_slash_does_not_double_up(self):
        script = _build_fetch_script("http://127.0.0.1:9018/", 10, None, None)
        assert "//api/stats" not in script


class TestParseResponse:
    def test_parses_both_payloads(self):
        summary, blocking = _parse_response(_response())
        assert summary["queries"]["total"] == 237819
        assert blocking["blocking"] == "enabled"

    def test_missing_separator_raises(self):
        with pytest.raises(ValueError, match="unexpected API response"):
            _parse_response('{"queries": {}}')

    def test_malformed_summary_raises(self):
        with pytest.raises(ValueError, match="summary was not JSON"):
            _parse_response(f'not json\n{_SEP}\n{{"blocking": "enabled"}}')

    def test_auth_error_names_the_cause(self):
        body = json.dumps({"error": {"message": "Unauthorized"}})
        with pytest.raises(ValueError, match="api_password"):
            _parse_response(f'{body}\n{_SEP}\n{{"blocking": "enabled"}}')


class TestPiholeCollection:
    async def test_healthy_sets_online(self, plugin, run_cycle):
        _respond(plugin, run_cycle)
        assert _latest_status() == "online"

    async def test_block_rate_metric_recorded(self, plugin, run_cycle):
        _respond(plugin, run_cycle)
        assert _latest_metric("block_rate_pct") == pytest.approx(16.1)

    async def test_core_metrics_recorded(self, plugin, run_cycle):
        _respond(plugin, run_cycle)
        assert _latest_metric("queries_total") == 237819
        assert _latest_metric("gravity_domains") == 347306
        assert _latest_metric("clients_active") == 16
        assert _latest_metric("blocking_enabled") == 1.0

    async def test_computes_block_rate_when_absent(self, plugin, run_cycle):
        s = _summary(total=1000, blocked=250)
        del s["queries"]["percent_blocked"]
        _respond(plugin, run_cycle, s)
        assert _latest_metric("block_rate_pct") == pytest.approx(25.0)

    async def test_ssh_failure_sets_failed(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(1, "", "connection refused"))
        assert _latest_status() == "failed"

    async def test_garbage_response_sets_failed(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, "<html>404</html>", ""))
        assert _latest_status() == "failed"


class TestBlockRateThresholds:
    async def test_low_block_rate_sets_warning(self, plugin, run_cycle):
        _respond(plugin, run_cycle, _summary(total=10000, blocked=300, percent=3.0))
        assert _latest_status() == "warning"

    async def test_collapsed_block_rate_sets_failed(self, plugin, run_cycle):
        _respond(plugin, run_cycle, _summary(total=10000, blocked=20, percent=0.2))
        assert _latest_status() == "failed"

    async def test_low_query_volume_is_not_judged(self, plugin, run_cycle):
        _respond(plugin, run_cycle, _summary(total=5, blocked=0, percent=0.0))
        assert _latest_status() == "online"

    async def test_threshold_boundary_is_not_failed(self, plugin, run_cycle):
        _respond(plugin, run_cycle, _summary(total=10000, blocked=100, percent=1.0))
        assert _latest_status() == "warning"


class TestGravityHealth:
    async def test_empty_gravity_sets_failed(self, plugin, run_cycle):
        _respond(plugin, run_cycle, _summary(domains=0))
        assert _latest_status() == "failed"

    async def test_stale_gravity_sets_warning(self, plugin, run_cycle):
        old = time.time() - (86400 * 30)
        _respond(plugin, run_cycle, _summary(last_update=old))
        assert _latest_status() == "warning"

    async def test_fresh_gravity_stays_online(self, plugin, run_cycle):
        _respond(plugin, run_cycle, _summary(last_update=time.time() - 3600))
        assert _latest_status() == "online"

    async def test_gravity_age_metric_recorded(self, plugin, run_cycle):
        _respond(plugin, run_cycle, _summary(last_update=time.time() - 7200))
        assert _latest_metric("gravity_age_seconds") == pytest.approx(7200, abs=60)

    async def test_never_updated_sets_warning(self, plugin, run_cycle):
        s = _summary()
        del s["gravity"]["last_update"]
        _respond(plugin, run_cycle, s)
        assert _latest_status() == "warning"


class TestBlockingDisabled:
    async def test_disabled_blocking_sets_failed(self, plugin, run_cycle):
        _respond(plugin, run_cycle, blocking="disabled")
        assert _latest_status() == "failed"

    async def test_disabled_blocking_recorded_as_metric(self, plugin, run_cycle):
        _respond(plugin, run_cycle, blocking="disabled")
        assert _latest_metric("blocking_enabled") == 0.0

    async def test_disabled_outranks_healthy_block_rate(self, plugin, run_cycle):
        _respond(plugin, run_cycle, _summary(percent=16.1), blocking="disabled")
        assert _latest_status() == "failed"

    async def test_worst_condition_wins(self, plugin, run_cycle):
        old = time.time() - (86400 * 30)
        _respond(plugin, run_cycle, _summary(domains=0, last_update=old))
        assert _latest_status() == "failed"


class TestBuildBlockingScript:
    def test_posts_to_blocking_endpoint(self):
        script = _build_blocking_script(
            "http://127.0.0.1:9018", 10, None, None, enabled=True)
        assert "/api/dns/blocking" in script
        assert '"blocking": true' in script

    def test_disabled_body(self):
        script = _build_blocking_script(
            "http://127.0.0.1:9018", 10, None, None, enabled=False)
        assert '"blocking": false' in script

    def test_authenticates_when_password_given(self):
        script = _build_blocking_script(
            "http://127.0.0.1:9018", 10, None, "hunter2", enabled=True)
        assert "/api/auth" in script
        assert "X-FTL-SID" in script

    def test_password_command_runs_on_remote_host(self):
        script = _build_blocking_script(
            "http://127.0.0.1:9018", 10, "cat /run/secrets/pihole_api", None, enabled=True)
        assert "cat /run/secrets/pihole_api" in script


class TestBuildGravityScript:
    def test_hits_gravity_endpoint(self):
        script = _build_gravity_script("http://127.0.0.1:9018", 120, None, None)
        assert "/api/action/gravity" in script

    def test_authenticates_when_password_given(self):
        script = _build_gravity_script("http://127.0.0.1:9018", 120, None, "hunter2")
        assert "/api/auth" in script
        assert "X-FTL-SID" in script


class TestPiholeActions:
    def test_exposes_expected_actions(self, plugin):
        ids = {a["action_id"] for a in plugin.get_actions()}
        assert ids == {"enable_blocking", "update_gravity"}

    def test_disable_blocking_is_not_offered(self, plugin):
        blob = json.dumps(plugin.get_actions()).lower()
        assert "disable" not in blob

    async def test_unknown_action_returns_false(self, plugin):
        assert plugin.plan_action("anything") is None

    async def test_enable_blocking_success(self, plugin):
        plan = plugin.plan_action("enable_blocking")
        assert "/api/dns/blocking" in plan.command
        assert '"blocking": true' in plan.command
        outcome = plugin.interpret_action("enable_blocking", CmdResult(0, "", ""))
        assert outcome.success is True

    async def test_enable_blocking_failure(self, plugin):
        outcome = plugin.interpret_action("enable_blocking", CmdResult(1, "", "connection refused"))
        assert outcome.success is False

    async def test_update_gravity_success(self, plugin):
        plan = plugin.plan_action("update_gravity")
        assert "/api/action/gravity" in plan.command
        outcome = plugin.interpret_action("update_gravity", CmdResult(0, "", ""))
        assert outcome.success is True

    async def test_update_gravity_failure(self, plugin):
        outcome = plugin.interpret_action("update_gravity", CmdResult(1, "", "timed out"))
        assert outcome.success is False

    async def test_update_gravity_uses_gravity_timeout(self, plugin):
        plan = plugin.plan_action("update_gravity")
        assert plan.timeout == plugin.gravity_timeout
