import asyncio
import pytest
from unittest.mock import AsyncMock

from vigil.collector.plugin_base import CollectorPlugin


class _Probe(CollectorPlugin):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.collections = 0
        self.gate = None

    async def on_collect(self):
        self.collections += 1
        if self.gate is not None:
            await self.gate.wait()

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False


@pytest.fixture
def probe(make_plugin):
    return make_plugin(_Probe, {"interval": 3600})


class TestInterval:
    async def test_first_tick_collects(self, probe):
        await probe.run_cycle()
        assert probe.collections == 1

    async def test_second_tick_within_interval_is_skipped(self, probe):
        await probe.run_cycle()
        await probe.run_cycle()
        assert probe.collections == 1

    async def test_tick_after_interval_collects_again(self, probe):
        await probe.run_cycle()
        probe._last_collected -= probe.interval + 1
        await probe.run_cycle()
        assert probe.collections == 2

    async def test_short_interval_polls_every_tick(self, make_plugin):
        p = make_plugin(_Probe, {"interval": 0})
        await p.run_cycle()
        await p.run_cycle()
        assert p.collections == 2


class TestReturnValue:
    async def test_returns_true_when_it_collected(self, probe):
        assert await probe.run_cycle() is True

    async def test_returns_false_when_not_due(self, probe):
        await probe.run_cycle()
        assert await probe.run_cycle() is False


class TestTimeoutConfig:
    def test_defaults_to_framework_timeout(self, make_plugin):
        from vigil.collector.collectors.ssh_collector import TIMEOUT
        p = make_plugin(_Probe, {})
        assert p.timeout == TIMEOUT

    def test_timeout_is_configurable(self, make_plugin):
        p = make_plugin(_Probe, {"timeout": "3m"})
        assert p.timeout == 180

    def test_numeric_timeout_accepted(self, make_plugin):
        assert make_plugin(_Probe, {"timeout": 45}).timeout == 45


class TestOverlapGuard:
    async def test_overlapping_poll_is_skipped(self, probe):
        probe.gate = asyncio.Event()
        first = asyncio.create_task(probe.run_cycle())
        await asyncio.sleep(0)

        probe._last_collected = 0.0
        await probe.run_cycle()

        assert probe.collections == 1
        probe.gate.set()
        await first

    async def test_guard_releases_after_completion(self, probe):
        probe.gate = asyncio.Event()
        probe.gate.set()
        await probe.run_cycle()
        probe._last_collected = 0.0
        await probe.run_cycle()
        assert probe.collections == 2

    async def test_guard_releases_after_exception(self, probe):
        probe.on_collect = AsyncMock(side_effect=RuntimeError("boom"))
        with pytest.raises(RuntimeError):
            await probe.run_cycle()
        assert probe._collecting is False
