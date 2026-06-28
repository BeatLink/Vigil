import pytest
from unittest.mock import AsyncMock

pytestmark = pytest.mark.asyncio
from vigil.plugins.system_stats import SystemStatsPlugin, _parse_cpu_line, _cpu_pct, _fmt_gb, _level_for
from vigil.core.data.database import db, StatusHistory, Metric


BASE_CFG = {
    "name": "test-stats",
    "id":   "test-stats",
    "cpu_warning":      70,
    "cpu_threshold":    85,
    "memory_warning":   75,
    "memory_threshold": 90,
    "temp_warning":     70,
    "temp_threshold":   80,
    "ssh_config": {"host": "test.host"},
}


def _make_output(cpu1, cpu2, mem_total_kb, mem_avail_kb, temps_mc=None):
    """Build synthetic SSH output for system_stats collection.

    cpu1/cpu2: list of ints [user, nice, system, idle, iowait, irq, softirq, steal]
    """
    lines = [
        f"cpu  {' '.join(str(f) for f in cpu1)}",
        f"MemTotal:       {mem_total_kb} kB",
        f"MemAvailable:   {mem_avail_kb} kB",
    ]
    for t in (temps_mc or []):
        lines.append(f"TEMP:{t}")
    lines.append(f"cpu  {' '.join(str(f) for f in cpu2)}")
    return "\n".join(lines) + "\n"


# cpu fields that produce a known usage when diffed
# delta_total=1000, delta_idle=500 → 50% (below warning threshold of 70)
_CPU1_50 = [100, 0, 0, 900, 0, 0, 0, 0]
_CPU2_50 = [600, 0, 0, 1400, 0, 0, 0, 0]

# delta_total=1000, delta_idle=250 → 75% (between warning 70 and failed 85)
_CPU1_75 = [100, 0, 0, 900, 0, 0, 0, 0]
_CPU2_75 = [850, 0, 0, 1150, 0, 0, 0, 0]

# delta_total=1000, delta_idle=100 → 90% (above failed threshold of 85)
_CPU1_90 = [100, 0, 0, 900, 0, 0, 0, 0]
_CPU2_90 = [1000, 0, 0, 1000, 0, 0, 0, 0]

_MEM_TOTAL_KB  = 16_000_000    # ~16 GB
_MEM_AVAIL_50  = 8_000_000     # 50% used → online (below 75% warning)
_MEM_AVAIL_20  = 3_200_000     # 80% used → warning (between 75 warning and 90 failed)
_MEM_AVAIL_5   = 800_000       # 95% used → failed (above 90% threshold)

_TEMPS_OK      = [42_000, 38_000, 45_000]   # max 45°C → online
_TEMPS_WARNING = [72_000, 68_000]            # max 72°C → warning (between 70 and 80)
_TEMPS_FAILED  = [85_000, 90_000]            # max 90°C → failed


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(SystemStatsPlugin, BASE_CFG)


def _latest_status(plugin_id: str = "test-stats") -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str, name: str = "test-stats") -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


# ---------------------------------------------------------------------------
# Unit tests for pure helpers
# ---------------------------------------------------------------------------

class TestParseCpuLine:
    def test_total_and_idle_correct(self):
        line = "cpu  100 0 50 800 50 0 0 0"
        total, idle = _parse_cpu_line(line)
        assert total == 1000
        assert idle == 850  # idle(800) + iowait(50)

    def test_short_line_defaults_missing_fields(self):
        line = "cpu  100 0 50 800"
        total, idle = _parse_cpu_line(line)
        assert total == 950
        assert idle == 800


class TestCpuPct:
    def test_50_percent(self):
        line1 = f"cpu  {' '.join(str(f) for f in _CPU1_50)}"
        line2 = f"cpu  {' '.join(str(f) for f in _CPU2_50)}"
        assert _cpu_pct(line1, line2) == pytest.approx(50.0)

    def test_90_percent(self):
        line1 = f"cpu  {' '.join(str(f) for f in _CPU1_90)}"
        line2 = f"cpu  {' '.join(str(f) for f in _CPU2_90)}"
        assert _cpu_pct(line1, line2) == pytest.approx(90.0)

    def test_zero_delta_returns_zero(self):
        line = "cpu  100 0 0 900 0 0 0 0"
        assert _cpu_pct(line, line) == pytest.approx(0.0)

    def test_clamped_to_100(self):
        # pathological case: idle goes negative (shouldn't happen, but guard it)
        line1 = "cpu  0 0 0 1000 0 0 0 0"
        line2 = "cpu  2000 0 0 0 0 0 0 0"
        assert _cpu_pct(line1, line2) == pytest.approx(100.0)


class TestFmtGb:
    def test_less_than_1024_shows_gb(self):
        assert _fmt_gb(512.0) == "512.0 GB"

    def test_1024_shows_tb(self):
        assert _fmt_gb(1024.0) == "1.0 TB"


class TestLevelFor:
    def test_below_warning_is_online(self):
        assert _level_for(50.0, 70.0, 85.0) == 'online'

    def test_at_warning_threshold_is_warning(self):
        assert _level_for(70.0, 70.0, 85.0) == 'warning'

    def test_between_warning_and_failed_is_warning(self):
        assert _level_for(75.0, 70.0, 85.0) == 'warning'

    def test_at_failed_threshold_is_failed(self):
        assert _level_for(85.0, 70.0, 85.0) == 'failed'

    def test_above_failed_threshold_is_failed(self):
        assert _level_for(95.0, 70.0, 85.0) == 'failed'


# ---------------------------------------------------------------------------
# Collection integration tests
# ---------------------------------------------------------------------------

class TestSystemStatsCollection:
    async def test_all_below_warning_sets_online(self, plugin):
        stdout = _make_output(_CPU1_50, _CPU2_50, _MEM_TOTAL_KB, _MEM_AVAIL_50, _TEMPS_OK)
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, stdout, ""))
        await plugin.on_collect()
        assert _latest_status() == "online"

    async def test_cpu_above_warning_sets_warning(self, plugin):
        stdout = _make_output(_CPU1_75, _CPU2_75, _MEM_TOTAL_KB, _MEM_AVAIL_50, _TEMPS_OK)
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, stdout, ""))
        await plugin.on_collect()
        assert _latest_status() == "warning"

    async def test_memory_above_warning_sets_warning(self, plugin):
        stdout = _make_output(_CPU1_50, _CPU2_50, _MEM_TOTAL_KB, _MEM_AVAIL_20, _TEMPS_OK)
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, stdout, ""))
        await plugin.on_collect()
        assert _latest_status() == "warning"

    async def test_temp_above_warning_sets_warning(self, plugin):
        stdout = _make_output(_CPU1_50, _CPU2_50, _MEM_TOTAL_KB, _MEM_AVAIL_50, _TEMPS_WARNING)
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, stdout, ""))
        await plugin.on_collect()
        assert _latest_status() == "warning"

    async def test_cpu_above_failed_threshold_sets_failed(self, plugin):
        stdout = _make_output(_CPU1_90, _CPU2_90, _MEM_TOTAL_KB, _MEM_AVAIL_50, _TEMPS_OK)
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, stdout, ""))
        await plugin.on_collect()
        assert _latest_status() == "failed"

    async def test_memory_above_failed_threshold_sets_failed(self, plugin):
        stdout = _make_output(_CPU1_50, _CPU2_50, _MEM_TOTAL_KB, _MEM_AVAIL_5, _TEMPS_OK)
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, stdout, ""))
        await plugin.on_collect()
        assert _latest_status() == "failed"

    async def test_temp_above_failed_threshold_sets_failed(self, plugin):
        stdout = _make_output(_CPU1_50, _CPU2_50, _MEM_TOTAL_KB, _MEM_AVAIL_50, _TEMPS_FAILED)
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, stdout, ""))
        await plugin.on_collect()
        assert _latest_status() == "failed"

    async def test_failed_overrides_warning(self, plugin):
        # CPU in warning range, temp in failed range → overall failed
        stdout = _make_output(_CPU1_75, _CPU2_75, _MEM_TOTAL_KB, _MEM_AVAIL_50, _TEMPS_FAILED)
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, stdout, ""))
        await plugin.on_collect()
        assert _latest_status() == "failed"

    async def test_cpu_pct_metric_recorded(self, plugin):
        stdout = _make_output(_CPU1_50, _CPU2_50, _MEM_TOTAL_KB, _MEM_AVAIL_50)
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, stdout, ""))
        await plugin.on_collect()
        assert _latest_metric("cpu_pct") == pytest.approx(50.0)

    async def test_memory_pct_metric_recorded(self, plugin):
        stdout = _make_output(_CPU1_50, _CPU2_50, _MEM_TOTAL_KB, _MEM_AVAIL_50)
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, stdout, ""))
        await plugin.on_collect()
        assert _latest_metric("memory_pct") == pytest.approx(50.0)

    async def test_memory_used_gb_metric_recorded(self, plugin):
        stdout = _make_output(_CPU1_50, _CPU2_50, _MEM_TOTAL_KB, _MEM_AVAIL_50)
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, stdout, ""))
        await plugin.on_collect()
        used_gb = _MEM_TOTAL_KB / (1024 ** 2) * 0.5
        assert _latest_metric("memory_used_gb") == pytest.approx(used_gb, rel=1e-3)

    async def test_temp_metric_recorded(self, plugin):
        stdout = _make_output(_CPU1_50, _CPU2_50, _MEM_TOTAL_KB, _MEM_AVAIL_50, _TEMPS_OK)
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, stdout, ""))
        await plugin.on_collect()
        assert _latest_metric("temp_c") == pytest.approx(45.0)

    async def test_temp_takes_maximum_zone(self, plugin):
        temps = [30_000, 75_000, 60_000]
        stdout = _make_output(_CPU1_50, _CPU2_50, _MEM_TOTAL_KB, _MEM_AVAIL_50, temps)
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, stdout, ""))
        await plugin.on_collect()
        assert _latest_metric("temp_c") == pytest.approx(75.0)

    async def test_no_temp_data_still_online(self, plugin):
        stdout = _make_output(_CPU1_50, _CPU2_50, _MEM_TOTAL_KB, _MEM_AVAIL_50, temps_mc=[])
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, stdout, ""))
        await plugin.on_collect()
        assert _latest_status() == "online"
        assert _latest_metric("temp_c") is None

    async def test_no_temp_data_does_not_store_metric(self, plugin):
        stdout = _make_output(_CPU1_50, _CPU2_50, _MEM_TOTAL_KB, _MEM_AVAIL_50, temps_mc=[])
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, stdout, ""))
        await plugin.on_collect()
        assert _latest_metric("temp_c") is None

    async def test_custom_thresholds_respected(self, make_plugin):
        cfg = {**BASE_CFG, "name": "test-custom", "id": "test-custom",
               "cpu_warning": 40, "cpu_threshold": 50,
               "memory_warning": 40, "memory_threshold": 50,
               "temp_threshold": 40}
        plugin = make_plugin(SystemStatsPlugin, cfg)
        # 50% CPU → exactly at failed threshold → failed
        stdout = _make_output(_CPU1_50, _CPU2_50, _MEM_TOTAL_KB, _MEM_AVAIL_50, _TEMPS_OK)
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, stdout, ""))
        await plugin.on_collect()
        assert _latest_status("test-custom") == "failed"

    async def test_ssh_failure_sets_failed(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(-1, "", "timeout"))
        await plugin.on_collect()
        assert _latest_status() == "failed"

    async def test_malformed_output_sets_failed(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, "garbage", ""))
        await plugin.on_collect()
        assert _latest_status() == "failed"

    async def test_missing_memory_lines_sets_failed(self, plugin):
        stdout = "cpu  100 0 0 900 0 0 0 0\ncpu  600 0 0 1400 0 0 0 0\n"
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, stdout, ""))
        await plugin.on_collect()
        assert _latest_status() == "failed"

    async def test_only_one_cpu_sample_sets_failed(self, plugin):
        stdout = "cpu  100 0 0 900 0 0 0 0\nMemTotal: 16000000 kB\nMemAvailable: 8000000 kB\n"
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, stdout, ""))
        await plugin.on_collect()
        assert _latest_status() == "failed"


class TestSystemStatsActions:
    async def test_on_action_always_returns_false(self, plugin):
        assert await plugin.on_action("anything") is False
