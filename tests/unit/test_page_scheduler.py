import asyncio
from unittest.mock import MagicMock, patch

import pytest

import vigil.core.ui.model as model
from vigil.core.state.changes import ChangeBus
from vigil.core.ui.model import schedule_callback, _CallbackTick, _schedulers


@pytest.fixture
def fake_client():
    """A stable fake client on model.context plus a private change bus, so the
    scheduler can be exercised without a running NiceGUI server. Returns the
    bus the scheduler subscribes to."""
    _schedulers.clear()
    client = MagicMock()
    client.id = "client-A"
    bus = ChangeBus()

    with patch.object(model, "context") as ctx, \
         patch.object(model, "CHANGES", bus), \
         patch.object(model, "asyncio") as aio:
        ctx.client = client
        aio.create_task = lambda coro: coro.close()
        aio.get_running_loop = MagicMock()
        # Client.instances membership drives _CallbackTick._detached().
        with patch.object(model.Client, "instances", {client.id: client}):
            yield client, bus
    _schedulers.clear()


class TestSharedScheduling:
    def test_multiple_callbacks_share_one_scheduler_and_subscription(self, fake_client):
        client, bus = fake_client
        for _ in range(5):
            schedule_callback(lambda: None, run_now=False)

        # One scheduler for the client, one bus subscription, five tickables —
        # not five independent subscriptions.
        assert len(_schedulers) == 1
        sched = _schedulers[client.id]
        assert len(sched._tickables) == 5
        assert len(bus._subscribers) == 1

    def test_callback_tick_registered_and_attached(self, fake_client):
        client, _ = fake_client
        schedule_callback(lambda: None, run_now=False)
        tick = _schedulers[client.id]._tickables[0]
        assert isinstance(tick, _CallbackTick)
        assert tick._detached() is False

    def test_published_change_wakes_the_scheduler(self, fake_client):
        client, bus = fake_client
        schedule_callback(lambda: None, run_now=False)
        sched = _schedulers[client.id]
        assert sched._pending is False

        bus.publish("status", "some-monitor")

        # The bus subscriber hands the wake-up to the UI loop, which the fake
        # asyncio records rather than running.
        sched._loop.call_soon_threadsafe.assert_called_once_with(sched._wake)


class TestCallbackTick:
    @pytest.mark.asyncio
    async def test_run_now_false_skips_first_tick(self, fake_client):
        calls = []
        tick = _CallbackTick(lambda: calls.append(1), run_now=False)
        await tick._tick()          # first tick: skipped
        assert calls == []
        await tick._tick()          # second tick: runs
        assert calls == [1]

    @pytest.mark.asyncio
    async def test_run_now_true_runs_first_tick(self, fake_client):
        calls = []
        tick = _CallbackTick(lambda: calls.append(1), run_now=True)
        await tick._tick()
        assert calls == [1]


class TestChangeBus:
    def test_unsubscribe_stops_delivery(self):
        bus = ChangeBus()
        seen = []
        stop = bus.subscribe(lambda kind, pid: seen.append((kind, pid)))
        bus.publish("metric", "cpu")
        stop()
        bus.publish("metric", "cpu")
        assert seen == [("metric", "cpu")]

    def test_a_failing_subscriber_does_not_break_the_write_path(self):
        bus = ChangeBus()
        seen = []
        bus.subscribe(lambda kind, pid: (_ for _ in ()).throw(RuntimeError("boom")))
        bus.subscribe(lambda kind, pid: seen.append(kind))
        bus.publish("status", None)
        assert seen == ["status"]
