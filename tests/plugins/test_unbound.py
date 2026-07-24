from unittest.mock import AsyncMock

import pytest

from vigil.plugins.unbound import (
    Unbound,
    _SEP,
    _build_probe_script,
    _parse_stats,
    _resolved_ok,
    _split_response,
)
from vigil.core.connectors.orchestration.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric


BASE_CFG = {
    "name": "test-unbound",
    "id":   "test-unbound",
    "query_domain": "cloudflare.com",
    "servfail_warning": 5,
    "servfail_threshold": 20,
    "min_queries": 20,
    "ssh_config": {"host": "test.host"},
}

_STATS_TEMPLATE = """total.num.queries={total}
total.num.cachehits={hits}
total.num.cachemiss={miss}
total.num.servfail={servfail}
total.num.uptime={uptime}
"""


def _stats(total=5000, hits=4200, miss=800, servfail=0, uptime=86400):
    return _STATS_TEMPLATE.format(
        total=total, hits=hits, miss=miss, servfail=servfail, uptime=uptime)


def _query_ok():
    return ";; ->>HEADER<<- opcode: QUERY, rcode: NOERROR, id: 1"


def _query_fail():
    return ";; ->>HEADER<<- opcode: QUERY, rcode: SERVFAIL, id: 1"


def _response(stats=None, query_output=None):
    stats_raw = stats if stats is not None else _stats()
    query_raw = query_output if query_output is not None else _query_ok()
    return f"{stats_raw}\n{_SEP}\n{query_raw}"


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(Unbound, BASE_CFG)


def _respond(plugin, run_cycle, stats=None, query_output=None):
    run_cycle(plugin, lambda c: CmdResult(0, _response(stats, query_output), ""))


def _latest_status(plugin_id: str = "test-unbound") -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str, name: str = "test-unbound") -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


class TestBuildProbeScript:
    def test_includes_control_command(self):
        script = _build_probe_script(
            "unbound-control stats_noreset", "127.0.0.1", 53, "cloudflare.com", 5)
        assert "unbound-control stats_noreset" in script

    def test_includes_query_domain(self):
        script = _build_probe_script(
            "unbound-control stats_noreset", "127.0.0.1", 53, "cloudflare.com", 5)
        assert "cloudflare.com" in script

    def test_uses_dig(self):
        script = _build_probe_script(
            "unbound-control stats_noreset", "127.0.0.1", 53, "cloudflare.com", 5)
        assert "dig" in script


class TestParseStats:
    def test_parses_key_value_lines(self):
        stats = _parse_stats(_stats(total=1000, servfail=5))
        assert stats["total.num.queries"] == 1000
        assert stats["total.num.servfail"] == 5

    def test_skips_lines_without_equals(self):
        stats = _parse_stats("garbage line\ntotal.num.queries=10\n")
        assert stats["total.num.queries"] == 10

    def test_skips_non_numeric_values(self):
        stats = _parse_stats("some.key=notanumber\ntotal.num.queries=10\n")
        assert "some.key" not in stats
        assert stats["total.num.queries"] == 10


class TestResolvedOk:
    def test_noerror_is_ok(self):
        assert _resolved_ok(_query_ok()) is True

    def test_servfail_is_not_ok(self):
        assert _resolved_ok(_query_fail()) is False

    def test_empty_output_is_not_ok(self):
        assert _resolved_ok("") is False


class TestSplitResponse:
    def test_splits_on_separator(self):
        stats_raw, query_raw = _split_response(_response())
        assert "total.num.queries" in stats_raw
        assert "NOERROR" in query_raw

    def test_missing_separator_raises(self):
        with pytest.raises(ValueError, match="unexpected control output"):
            _split_response("no separator here")


class TestUnboundCollection:
    async def test_healthy_sets_online(self, plugin, run_cycle):
        _respond(plugin, run_cycle)
        assert _latest_status() == "online"

    async def test_metrics_recorded(self, plugin, run_cycle):
        _respond(plugin, run_cycle, _stats(total=5000, hits=4200, miss=800, servfail=0))
        assert _latest_metric("queries_total") == 5000
        assert _latest_metric("resolved_ok") == 1.0
        assert _latest_metric("cache_hit_rate_pct") == pytest.approx(84.0)

    async def test_ssh_failure_sets_failed(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(1, "", "connection refused"))
        assert _latest_status() == "failed"

    async def test_garbage_response_sets_failed(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, "no separator", ""))
        assert _latest_status() == "failed"


class TestResolutionFailure:
    async def test_failed_resolution_sets_failed(self, plugin, run_cycle):
        _respond(plugin, run_cycle, query_output=_query_fail())
        assert _latest_status() == "failed"

    async def test_resolved_ok_metric_reflects_failure(self, plugin, run_cycle):
        _respond(plugin, run_cycle, query_output=_query_fail())
        assert _latest_metric("resolved_ok") == 0.0


class TestServfailThresholds:
    async def test_high_servfail_rate_sets_failed(self, plugin, run_cycle):
        _respond(plugin, run_cycle, _stats(total=1000, servfail=250))
        assert _latest_status() == "failed"

    async def test_moderate_servfail_rate_sets_warning(self, plugin, run_cycle):
        _respond(plugin, run_cycle, _stats(total=1000, servfail=80))
        assert _latest_status() == "warning"

    async def test_low_query_volume_is_not_judged(self, plugin, run_cycle):
        _respond(plugin, run_cycle, _stats(total=5, servfail=5))
        assert _latest_status() == "online"

    async def test_worst_condition_wins(self, plugin, run_cycle):
        _respond(plugin, run_cycle, _stats(total=1000, servfail=250), query_output=_query_fail())
        assert _latest_status() == "failed"


class TestUnboundActions:
    async def test_on_action_always_returns_false(self, plugin):
        assert plugin.plan_action("anything") is None
