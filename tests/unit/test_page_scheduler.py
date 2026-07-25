from unittest.mock import MagicMock, patch

import pytest

import vigil.core.ui.model as model
from vigil.core.ui.model import schedule_callback, _CallbackTick, _schedulers


@pytest.fixture
def fake_client():
    """A stable fake client on model.context plus a no-op safe_timer, so the
    scheduler can be exercised without a running NiceGUI server. Returns the
    number of real timers the scheduler asked safe_timer to create."""
    _schedulers.clear()
    client = MagicMock()
    client.id = "client-A"
    created = {"timers": 0}

    def fake_safe_timer(interval, cb, defer_first=False):
        created["timers"] += 1
        return MagicMock()

    with patch.object(model, "context") as ctx, \
         patch.object(model, "safe_timer", fake_safe_timer), \
         patch.object(model, "asyncio") as aio:
        ctx.client = client
        # Don't actually run the inline first-tick coroutine the scheduler spawns.
        aio.create_task = lambda coro: coro.close()
        # Client.instances membership drives _CallbackTick._detached().
        with patch.object(model.Client, "instances", {client.id: client}):
            yield client, created
    _schedulers.clear()


class TestSharedScheduling:
    def test_multiple_callbacks_share_one_scheduler_and_timer(self, fake_client):
        client, created = fake_client
        for _ in range(5):
            schedule_callback(lambda: None, run_now=False)

        # One scheduler for the client, one timer, five tickables — not five
        # independent timers (the pre-PERF-4 behavior).
        assert len(_schedulers) == 1
        sched = _schedulers[client.id]
        assert len(sched._tickables) == 5
        assert created["timers"] == 1

    def test_callback_tick_registered_and_attached(self, fake_client):
        client, _ = fake_client
        schedule_callback(lambda: None, run_now=False)
        tick = _schedulers[client.id]._tickables[0]
        assert isinstance(tick, _CallbackTick)
        assert tick._detached() is False


class TestCallbackTick:
    @pytest.mark.asyncio
    async def test_run_now_false_skips_first_tick(self, fake_client):
        calls = []
        tick = _CallbackTick(lambda: calls.append(1), interval=1.0, run_now=False)
        await tick._tick()          # first tick: skipped
        assert calls == []
        await tick._tick()          # second tick: runs
        assert calls == [1]

    @pytest.mark.asyncio
    async def test_run_now_true_runs_first_tick(self, fake_client):
        calls = []
        tick = _CallbackTick(lambda: calls.append(1), interval=1.0, run_now=True)
        await tick._tick()
        assert calls == [1]
