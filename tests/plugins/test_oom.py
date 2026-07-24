import pytest
from unittest.mock import AsyncMock

pytestmark = pytest.mark.asyncio
from vigil.plugins.oom import OomCollectorPlugin, _extract_counter
from vigil.core.data.database import db, StatusHistory, Metric


BASE_CFG = {"name": "test-oom", "id": "test-oom",
            "ssh_config": {"host": "test.host"}}


def _latest_status(pid="test-oom"):
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == pid
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric, name="test-oom"):
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


def _vmstat(oom_kill=0, include=True):
    lines = ["nr_free_pages 123456", "pgfault 987654", "pgmajfault 4321"]
    if include:
        lines.append(f"oom_kill {oom_kill}")
    lines.append("pgpgin 111")
    return "\n".join(lines) + "\n"


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(OomCollectorPlugin, BASE_CFG)


async def _collect(plugin, oom_kill, ret=0, include=True):
    plugin.ssh_collector.fetch_output = AsyncMock(
        return_value=(ret, _vmstat(oom_kill, include), "")
    )
    await plugin.on_collect()


class TestExtractCounter:
    def test_finds_counter(self):
        assert _extract_counter(_vmstat(7), 'oom_kill') == 7

    def test_missing_key(self):
        assert _extract_counter(_vmstat(include=False), 'oom_kill') is None

    def test_malformed_value(self):
        assert _extract_counter("oom_kill notanumber\n", 'oom_kill') is None


class TestBaseline:
    async def test_first_collection_is_baseline(self, plugin):
        await _collect(plugin, 5)
        assert _latest_status() == "online"
        assert _latest_metric("oom_kills_total") == pytest.approx(5.0)

    async def test_no_kills_stays_online(self, plugin):
        await _collect(plugin, 5)
        await _collect(plugin, 5)
        assert _latest_status() == "online"
        assert _latest_metric("oom_kills_new") == pytest.approx(0.0)


class TestKillDetection:
    async def test_new_kill_fails(self, plugin):
        await _collect(plugin, 5)
        await _collect(plugin, 6)
        assert _latest_status() == "failed"
        assert _latest_metric("oom_kills_new") == pytest.approx(1.0)

    async def test_multiple_kills_recorded(self, plugin):
        await _collect(plugin, 0)
        await _collect(plugin, 3)
        assert _latest_status() == "failed"
        assert _latest_metric("oom_kills_new") == pytest.approx(3.0)

    async def test_kill_as_warning_when_configured(self, make_plugin):
        p = make_plugin(OomCollectorPlugin, dict(BASE_CFG, is_warning=True))
        await _collect(p, 0)
        await _collect(p, 1)
        assert _latest_status() == "warning"


class TestAlertDecay:
    async def test_alert_holds_then_clears(self, make_plugin):
        p = make_plugin(OomCollectorPlugin, dict(BASE_CFG, alert_for=3))
        await _collect(p, 0)
        await _collect(p, 1)
        assert _latest_status() == "failed"
        await _collect(p, 1)
        assert _latest_status() == "warning"
        await _collect(p, 1)
        assert _latest_status() == "warning"
        await _collect(p, 1)
        assert _latest_status() == "online"

    async def test_new_kill_resets_decay(self, make_plugin):
        p = make_plugin(OomCollectorPlugin, dict(BASE_CFG, alert_for=3))
        await _collect(p, 0)
        await _collect(p, 1)
        await _collect(p, 1)
        assert _latest_status() == "warning"
        await _collect(p, 2)
        assert _latest_status() == "failed"

    async def test_alert_for_zero_clears_immediately(self, make_plugin):
        p = make_plugin(OomCollectorPlugin, dict(BASE_CFG, alert_for=0))
        await _collect(p, 0)
        await _collect(p, 1)
        assert _latest_status() == "failed"
        await _collect(p, 1)
        assert _latest_status() == "online"


class TestCounterReset:
    async def test_reboot_rebaselines_without_alerting(self, plugin):
        await _collect(plugin, 9)
        await _collect(plugin, 2)
        assert _latest_status() == "online"

    async def test_kills_after_reboot_still_detected(self, plugin):
        await _collect(plugin, 9)
        await _collect(plugin, 0)
        await _collect(plugin, 1)
        assert _latest_status() == "failed"
        assert _latest_metric("oom_kills_new") == pytest.approx(1.0)


class TestFailureModes:
    async def test_ssh_failure_is_failed(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(1, "", "no such file"))
        await plugin.on_collect()
        assert _latest_status() == "failed"

    async def test_missing_counter_is_offline(self, plugin):
        await _collect(plugin, 0, include=False)
        assert _latest_status() == "offline"


class TestOomActions:
    async def test_on_action_returns_false(self, plugin):
        assert await plugin.on_action("anything") is False
