import pytest
from unittest.mock import AsyncMock, MagicMock

pytestmark = pytest.mark.asyncio
from vigil.plugins.processes import ProcessesPlugin, _parse_ps_output, _level_for
from vigil.core.data.database import db, StatusHistory, Metric


BASE_CFG = {
    "name": "test-procs",
    "id":   "test-procs",
    "max_processes": 5,
    "ssh_config": {"host": "test.host"},
}

CFG_WITH_THRESHOLDS = {
    **BASE_CFG,
    "name": "test-procs-thresh",
    "id":   "test-procs-thresh",
    "cpu_warning":   50.0,
    "cpu_threshold": 80.0,
}

_PS_HEADER = "  PID USER     %CPU %MEM COMMAND"
_PS_ROWS = [
    "  123 root      45.2  2.1 python3",
    " 4567 beatlink   3.0  0.5 bash",
    "   89 www-data   0.1  0.3 nginx",
]

_PS_OUTPUT_OK = "\n".join([_PS_HEADER] + _PS_ROWS) + "\n"
_PS_OUTPUT_HEADER_ONLY = _PS_HEADER + "\n"
_PS_OUTPUT_EMPTY = ""

# High CPU for threshold tests
_PS_OUTPUT_HIGH_CPU = "\n".join([
    _PS_HEADER,
    "  999 root      85.0  1.0 stress",
]) + "\n"

_PS_OUTPUT_WARN_CPU = "\n".join([
    _PS_HEADER,
    "  999 root      60.0  1.0 stress",
]) + "\n"


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(ProcessesPlugin, BASE_CFG)


@pytest.fixture
def thresh_plugin(make_plugin):
    return make_plugin(ProcessesPlugin, CFG_WITH_THRESHOLDS)


def _latest_status(plugin_id: str = "test-procs") -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str, name: str = "test-procs") -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


# ---------------------------------------------------------------------------
# Unit tests for _parse_ps_output
# ---------------------------------------------------------------------------

class TestParsePsOutput:
    def test_parses_pid(self):
        procs = _parse_ps_output(_PS_OUTPUT_OK)
        assert procs[0]['pid'] == 123

    def test_parses_user(self):
        procs = _parse_ps_output(_PS_OUTPUT_OK)
        assert procs[0]['user'] == 'root'

    def test_parses_cpu(self):
        procs = _parse_ps_output(_PS_OUTPUT_OK)
        assert procs[0]['cpu'] == pytest.approx(45.2)

    def test_parses_mem(self):
        procs = _parse_ps_output(_PS_OUTPUT_OK)
        assert procs[0]['mem'] == pytest.approx(2.1)

    def test_parses_command(self):
        procs = _parse_ps_output(_PS_OUTPUT_OK)
        assert procs[0]['command'] == 'python3'

    def test_returns_all_rows(self):
        procs = _parse_ps_output(_PS_OUTPUT_OK)
        assert len(procs) == 3

    def test_header_only_returns_empty(self):
        procs = _parse_ps_output(_PS_OUTPUT_HEADER_ONLY)
        assert procs == []

    def test_empty_output_returns_empty(self):
        procs = _parse_ps_output(_PS_OUTPUT_EMPTY)
        assert procs == []

    def test_command_with_spaces_preserved(self):
        output = _PS_HEADER + "\n  1 root 0.0 0.0 my command with spaces\n"
        procs = _parse_ps_output(output)
        assert procs[0]['command'] == 'my command with spaces'

    def test_skips_malformed_lines(self):
        output = _PS_HEADER + "\nbadline\n  123 root 0.1 0.2 bash\n"
        procs = _parse_ps_output(output)
        assert len(procs) == 1
        assert procs[0]['pid'] == 123


class TestLevelFor:
    def test_below_warning_is_online(self):
        assert _level_for(30.0, 50.0, 80.0) == 'online'

    def test_at_warning_is_warning(self):
        assert _level_for(50.0, 50.0, 80.0) == 'warning'

    def test_between_thresholds_is_warning(self):
        assert _level_for(60.0, 50.0, 80.0) == 'warning'

    def test_at_failed_is_failed(self):
        assert _level_for(80.0, 50.0, 80.0) == 'failed'


# ---------------------------------------------------------------------------
# Collection integration tests
# ---------------------------------------------------------------------------

class TestProcessesCollection:
    async def test_successful_collection_sets_online(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, _PS_OUTPUT_OK, ""))
        await plugin.on_collect()
        assert _latest_status() == "online"

    async def test_process_list_populated(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, _PS_OUTPUT_OK, ""))
        await plugin.on_collect()
        assert len(plugin._processes) == 3
        assert plugin._processes[0]['pid'] == 123

    async def test_process_count_metric_recorded(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, _PS_OUTPUT_OK, ""))
        await plugin.on_collect()
        assert _latest_metric("process_count") == pytest.approx(3.0)

    async def test_top_cpu_metric_recorded(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, _PS_OUTPUT_OK, ""))
        await plugin.on_collect()
        assert _latest_metric("top_cpu_pct") == pytest.approx(45.2)

    async def test_empty_process_list_sets_online(self, plugin):
        # Empty output (header only) — no processes running is valid
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, _PS_OUTPUT_HEADER_ONLY, ""))
        await plugin.on_collect()
        assert _latest_status() == "online"
        assert plugin._processes == []

    async def test_unparseable_output_sets_failed(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, "complete garbage\n!!!\n", ""))
        await plugin.on_collect()
        assert _latest_status() == "failed"

    async def test_ssh_failure_sets_failed(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(-1, "", "timeout"))
        await plugin.on_collect()
        assert _latest_status() == "failed"

    async def test_no_thresholds_always_online(self, plugin):
        # BASE_CFG has no cpu thresholds — even 85% top process stays online
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, _PS_OUTPUT_HIGH_CPU, ""))
        await plugin.on_collect()
        assert _latest_status() == "online"

    async def test_top_cpu_above_warning_sets_warning(self, thresh_plugin):
        thresh_plugin.ssh_collector.fetch_output = AsyncMock(
            return_value=(0, _PS_OUTPUT_WARN_CPU, ""))
        await thresh_plugin.on_collect()
        assert _latest_status("test-procs-thresh") == "warning"

    async def test_top_cpu_above_threshold_sets_failed(self, thresh_plugin):
        thresh_plugin.ssh_collector.fetch_output = AsyncMock(
            return_value=(0, _PS_OUTPUT_HIGH_CPU, ""))
        await thresh_plugin.on_collect()
        assert _latest_status("test-procs-thresh") == "failed"


# ---------------------------------------------------------------------------
# Kill action tests
# ---------------------------------------------------------------------------

class TestProcessesKillAction:
    async def test_kill_term_sends_correct_command(self, plugin):
        plugin.ssh_controller.execute_action = AsyncMock(return_value=(0, "", ""))
        result = await plugin.on_action('kill', pid=123, signal='TERM')
        assert result is True
        plugin.ssh_controller.execute_action.assert_called_once_with("kill -TERM 123")

    async def test_kill_kill_sends_correct_command(self, plugin):
        plugin.ssh_controller.execute_action = AsyncMock(return_value=(0, "", ""))
        result = await plugin.on_action('kill', pid=456, signal='KILL')
        assert result is True
        plugin.ssh_controller.execute_action.assert_called_once_with("kill -KILL 456")

    async def test_kill_with_sudo(self, make_plugin):
        cfg = {**BASE_CFG, "name": "test-sudo", "id": "test-sudo", "require_sudo": True}
        p = make_plugin(ProcessesPlugin, cfg)
        p.ssh_controller.execute_action = AsyncMock(return_value=(0, "", ""))
        await p.on_action('kill', pid=99, signal='TERM')
        p.ssh_controller.execute_action.assert_called_once_with("sudo kill -TERM 99")

    async def test_kill_uses_default_signal_from_config(self, make_plugin):
        cfg = {**BASE_CFG, "name": "test-sig", "id": "test-sig", "kill_signal": "KILL"}
        p = make_plugin(ProcessesPlugin, cfg)
        p.ssh_controller.execute_action = AsyncMock(return_value=(0, "", ""))
        await p.on_action('kill', pid=77)  # no signal kwarg → uses config default
        p.ssh_controller.execute_action.assert_called_once_with("kill -KILL 77")

    async def test_kill_failure_returns_false(self, plugin):
        plugin.ssh_controller.execute_action = AsyncMock(
            return_value=(1, "", "Operation not permitted"))
        result = await plugin.on_action('kill', pid=1, signal='TERM')
        assert result is False

    async def test_kill_missing_pid_returns_false(self, plugin):
        result = await plugin.on_action('kill')
        assert result is False

    async def test_unknown_action_returns_false(self, plugin):
        result = await plugin.on_action('reboot')
        assert result is False
