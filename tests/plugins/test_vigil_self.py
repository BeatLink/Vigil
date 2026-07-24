import time
from unittest.mock import MagicMock, patch

import pytest

from vigil.plugins.vigil_self import (
    VigilSelfCollectorPlugin,
    _format_uptime,
    _read_rss_mb,
    _read_cpu_seconds,
)
from vigil.core.data.database import db, StatusHistory, Metric


BASE_CFG = {
    "name": "test-self",
    "id":   "test-self",
    "memory_warning":   256,
    "memory_threshold": 512,
    "stale_warning":     3,
    "stale_threshold":  10,
}


@pytest.fixture
def plugin(make_plugin):
    p = make_plugin(VigilSelfCollectorPlugin, BASE_CFG)
    p.engine = MagicMock(plugins=[])
    yield p
    VigilSelfCollectorPlugin.engine = None


def _latest_status(plugin_id: str = "test-self") -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str, name: str = "test-self") -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


def _fake_monitor(name: str, interval: float, last_collected: float, children=None):
    m = MagicMock()
    m.name = name
    m.interval = interval
    m._last_collected = last_collected
    m.children = children or []
    return m


class TestFormatUptime:
    def test_minutes_only(self):
        assert _format_uptime(300) == "5m"

    def test_hours_and_minutes(self):
        assert _format_uptime(3900) == "1h 5m"

    def test_days_and_hours(self):
        assert _format_uptime(280800) == "3d 6h"


class TestProcReaders:
    def test_rss_is_positive(self):
        assert _read_rss_mb() > 0

    def test_cpu_seconds_is_non_negative(self):
        assert _read_cpu_seconds() >= 0

    def test_rss_returns_none_when_proc_unreadable(self):
        with patch("builtins.open", side_effect=OSError("no /proc")):
            assert _read_rss_mb() is None

    def test_cpu_returns_none_when_proc_unreadable(self):
        with patch("builtins.open", side_effect=OSError("no /proc")):
            assert _read_cpu_seconds() is None


class TestSelfCollection:
    async def test_healthy_process_sets_online(self, plugin):
        await plugin.on_collect()
        assert _latest_status() == "online"

    async def test_uptime_metric_recorded(self, plugin):
        await plugin.on_collect()
        assert _latest_metric("uptime_seconds") is not None

    async def test_memory_metric_recorded(self, plugin):
        await plugin.on_collect()
        assert _latest_metric("memory_mb") > 0

    async def test_memory_above_threshold_sets_failed(self, plugin):
        with patch("vigil.plugins.vigil_self._read_rss_mb", return_value=600.0):
            await plugin.on_collect()
        assert _latest_status() == "failed"

    async def test_memory_above_warning_sets_warning(self, plugin):
        with patch("vigil.plugins.vigil_self._read_rss_mb", return_value=300.0):
            await plugin.on_collect()
        assert _latest_status() == "warning"

    async def test_unreadable_memory_does_not_fail(self, plugin):
        with patch("vigil.plugins.vigil_self._read_rss_mb", return_value=None):
            await plugin.on_collect()
        assert _latest_status() == "online"

    async def test_first_collection_records_no_cpu(self, plugin):
        await plugin.on_collect()
        assert _latest_metric("cpu_pct") is None

    async def test_second_collection_records_cpu(self, plugin):
        await plugin.on_collect()
        await plugin.on_collect()
        assert _latest_metric("cpu_pct") is not None


class TestCollectionHealth:
    async def test_counts_only_leaf_monitors(self, plugin):
        now = time.monotonic()
        leaf = _fake_monitor("leaf", 60, now)
        group = _fake_monitor("group", 60, now, children=[leaf])
        plugin.engine = MagicMock(plugins=[group])
        await plugin.on_collect()
        assert _latest_metric("monitors_total") == 1.0

    async def test_fresh_monitors_are_not_late(self, plugin):
        now = time.monotonic()
        plugin.engine = MagicMock(plugins=[_fake_monitor("fresh", 60, now)])
        await plugin.on_collect()
        assert _latest_metric("monitors_late") == 0.0
        assert _latest_status() == "online"

    async def test_late_monitor_sets_warning(self, plugin):
        stale = time.monotonic() - (60 * 4)
        plugin.engine = MagicMock(plugins=[_fake_monitor("late", 60, stale)])
        await plugin.on_collect()
        assert _latest_metric("monitors_late") == 1.0
        assert _latest_status() == "warning"

    async def test_stalled_monitor_sets_failed(self, plugin):
        stale = time.monotonic() - (60 * 12)
        plugin.engine = MagicMock(plugins=[_fake_monitor("stalled", 60, stale)])
        await plugin.on_collect()
        assert _latest_metric("monitors_stalled") == 1.0
        assert _latest_status() == "failed"

    async def test_never_collected_monitor_is_not_stalled(self, plugin):
        plugin.engine = MagicMock(plugins=[_fake_monitor("new", 3600, 0.0)])
        await plugin.on_collect()
        assert _latest_metric("monitors_stalled") == 0.0
        assert _latest_status() == "online"

    async def test_staleness_is_relative_to_each_interval(self, plugin):
        ten_min_ago = time.monotonic() - 600
        plugin.engine = MagicMock(plugins=[
            _fake_monitor("hourly", 3600, ten_min_ago),
            _fake_monitor("frequent", 30, ten_min_ago),
        ])
        await plugin.on_collect()
        assert _latest_metric("monitors_total") == 2.0
        assert _latest_metric("monitors_stalled") == 1.0

    async def test_stalled_outranks_healthy_memory(self, plugin):
        stale = time.monotonic() - (60 * 12)
        plugin.engine = MagicMock(plugins=[_fake_monitor("stalled", 60, stale)])
        with patch("vigil.plugins.vigil_self._read_rss_mb", return_value=10.0):
            await plugin.on_collect()
        assert _latest_status() == "failed"

    async def test_no_engine_reports_zero_monitors(self, plugin):
        plugin.engine = None
        await plugin.on_collect()
        assert _latest_metric("monitors_total") == 0.0
        assert _latest_status() == "online"

    async def test_excludes_itself_from_the_count(self, plugin):
        plugin.engine = MagicMock(plugins=[plugin])
        await plugin.on_collect()
        assert _latest_metric("monitors_total") == 0.0


class TestSelfActions:
    async def test_on_action_always_returns_false(self, plugin):
        assert await plugin.on_action("anything") is False
