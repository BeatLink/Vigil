"""End-to-end proof of the push path: a Database Engine write reaches a
dashboard client's scheduler without any timer in between."""

import asyncio

import pytest

from vigil.core.database.database import DatabaseManager, db
from vigil.core.connectors.types import CollectResult
from vigil.core.ui.model import COOLDOWN_SECONDS, _PageScheduler, _schedulers


@pytest.fixture
def mgr(tmp_path):
    if not db.is_closed():
        db.close()
    manager = DatabaseManager(str(tmp_path / "test.db"))
    yield manager
    if not db.is_closed():
        db.close()


class _Tickable:
    """A scheduler tickable that records each refresh and stays attached."""

    def __init__(self):
        self.ticks = 0
        self.event = asyncio.Event()

    def _detached(self) -> bool:
        return False

    async def _tick(self) -> None:
        self.ticks += 1
        self.event.set()


@pytest.fixture
async def scheduler():
    _schedulers.clear()
    sched = _PageScheduler("client-A")
    tickable = _Tickable()
    sched.add(tickable)
    await asyncio.sleep(0)          # let the inline first tick run
    tickable.ticks = 0
    tickable.event.clear()
    yield sched, tickable
    sched._shutdown()
    _schedulers.clear()


async def _wait(tickable, timeout=2.0):
    await asyncio.wait_for(tickable.event.wait(), timeout)


class TestWritesWakeTheDashboard:
    @pytest.mark.asyncio
    async def test_a_status_write_refreshes_the_client(self, mgr, scheduler):
        _, tickable = scheduler
        mgr.insert_status("some-monitor", "online")
        await _wait(tickable)
        assert tickable.ticks == 1

    @pytest.mark.asyncio
    async def test_a_write_from_a_worker_thread_refreshes_the_client(self, mgr, scheduler):
        """Collectors write off the UI loop, so the wake-up has to cross
        threads — this is the path every real collection takes."""
        _, tickable = scheduler
        await asyncio.to_thread(mgr.insert_metric, "host", "some-monitor", "cpu", 1.0)
        await _wait(tickable)
        assert tickable.ticks == 1

    @pytest.mark.asyncio
    async def test_one_cycles_many_writes_collapse_into_one_refresh(self, mgr, scheduler):
        """apply_result publishes a change per metric, log and status; the
        cooldown collapses them so a cycle costs one re-render, not eight."""
        _, tickable = scheduler
        result = CollectResult(success=True, status="online")
        result.metrics = {f"m{i}": float(i) for i in range(8)}
        mgr.apply_result("host", "some-monitor", "Some Monitor", result)
        await _wait(tickable)
        await asyncio.sleep(COOLDOWN_SECONDS + 0.1)
        assert tickable.ticks == 1

    @pytest.mark.asyncio
    async def test_an_idle_system_does_not_refresh(self, mgr, scheduler):
        """The point of the switch: nothing written means nothing rendered."""
        _, tickable = scheduler
        await asyncio.sleep(0.6)     # longer than the cooldown, shorter than the idle sweep
        assert tickable.ticks == 0

    @pytest.mark.asyncio
    async def test_a_detached_client_unsubscribes(self, mgr, scheduler):
        sched, tickable = scheduler
        tickable._detached = lambda: True
        mgr.insert_status("some-monitor", "online")
        await asyncio.sleep(0.05)
        assert sched._unsubscribe is None
        assert "client-A" not in _schedulers
