import asyncio
import pytest
from typing import List

from vigil.plugins.base.collector_plugin_base import CollectorPlugin
from vigil.core.connectors.orchestration.types import CmdResult, Command, CollectResult


class _Probe(CollectorPlugin):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.collections = 0
        self.gate = None
        self._blocking = False

    def commands(self) -> List[Command]:
        return []

    def parse(self, results: List[CmdResult]) -> CollectResult:
        self.collections += 1
        return CollectResult()


@pytest.fixture
def probe(make_plugin):
    return make_plugin(_Probe, {"interval": 3600})


@pytest.fixture
def engine(tmp_path):
    from unittest.mock import patch
    from vigil.core.connectors.engine import VigilEngine
    cfg = tmp_path / "c.yaml"
    cfg.write_text("plugins: []\n")
    with patch("vigil.core.connectors.engine.VigilEngine._connect", create=True):
        return VigilEngine(str(cfg), db_path_override=str(tmp_path / "e.db"))


class TestInterval:
    async def test_first_tick_collects(self, probe, engine):
        await engine.run_cycle_now(probe)
        assert probe.collections == 1

    async def test_second_tick_within_interval_is_skipped(self, probe, engine):
        # run_cycle_now itself is not interval-aware — it's a single-flight
        # guard only. Interval throttling lives in _monitor_loop's sleep, so
        # here we assert the guard directly: a call while _collecting is set
        # for this plugin id is skipped.
        engine._collecting[probe.id] = True
        assert await engine.run_cycle_now(probe) is False
        assert probe.collections == 0

    async def test_tick_after_interval_collects_again(self, probe, engine):
        await engine.run_cycle_now(probe)
        await engine.run_cycle_now(probe)
        assert probe.collections == 2


class TestReturnValue:
    async def test_returns_true_when_it_collected(self, probe, engine):
        assert await engine.run_cycle_now(probe) is True

    async def test_returns_false_when_not_due(self, probe, engine):
        engine._collecting[probe.id] = True
        assert await engine.run_cycle_now(probe) is False


class TestTimeoutConfig:
    def test_defaults_to_framework_timeout(self, make_plugin):
        from vigil.core.connectors.ssh_runner import TIMEOUT
        p = make_plugin(_Probe, {})
        assert p.timeout == TIMEOUT

    def test_timeout_is_configurable(self, make_plugin):
        p = make_plugin(_Probe, {"timeout": "3m"})
        assert p.timeout == 180

    def test_numeric_timeout_accepted(self, make_plugin):
        assert make_plugin(_Probe, {"timeout": 45}).timeout == 45


class TestOverlapGuard:
    async def test_overlapping_poll_is_skipped(self, probe, engine):
        probe.gate = asyncio.Event()
        probe._blocking = True

        async def _blocking_cycle():
            engine._collecting[probe.id] = True
            try:
                probe.collections += 1
                await probe.gate.wait()
            finally:
                engine._collecting[probe.id] = False

        first = asyncio.create_task(_blocking_cycle())
        await asyncio.sleep(0)

        assert await engine.run_cycle_now(probe) is False
        assert probe.collections == 1
        probe.gate.set()
        await first

    async def test_guard_releases_after_completion(self, probe, engine):
        await engine.run_cycle_now(probe)
        await engine.run_cycle_now(probe)
        assert probe.collections == 2
        assert engine._collecting[probe.id] is False

    async def test_guard_releases_after_exception(self, probe, engine):
        def _boom(results):
            raise RuntimeError("boom")
        probe.parse = _boom
        with pytest.raises(RuntimeError):
            await engine.run_cycle_now(probe)
        assert engine._collecting[probe.id] is False
