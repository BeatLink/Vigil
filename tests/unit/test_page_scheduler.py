import asyncio
from unittest.mock import MagicMock, patch

import pytest

import vigil.core.ui.model as model
from vigil.core.state.changes import ChangeBus
from vigil.core.ui.model import (
    schedule_callback, _CallbackTick, _PageScheduler, _is_detached, _schedulers,
)


@pytest.fixture
def fake_client():
    """A stable fake client on model.context plus a private change bus, so the
    scheduler can be exercised without a running NiceGUI server. Returns the
    bus the scheduler subscribes to."""
    _schedulers.clear()
    client = MagicMock()
    client.id = "client-A"
    bus = ChangeBus()

    # Anchoring needs a live render slot; these tests exercise the scheduler
    # without one, so anchors are stubbed out and lifetime falls back to the
    # client. TestViewSwitchLifetime sets its own anchors.
    with patch.object(model, "context") as ctx, \
         patch.object(model, "CHANGES", bus), \
         patch.object(model, "_anchor_element", lambda: None), \
         patch.object(model, "asyncio") as aio:
        ctx.client = client
        client.elements = {}
        aio.create_task = lambda coro: coro.close()
        aio.get_running_loop = MagicMock()
        # Client.instances membership drives _CallbackTick._detached().
        with patch.object(model.ng.Client, "instances", {client.id: client}):
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


class TestViewSwitchLifetime:
    """A tickable lives as long as the elements it redraws. Switching views
    clears the main container, which deletes their anchors; without that the
    scheduler accumulates one dead tickable per view visited and refreshes
    widgets the browser no longer has."""

    def _anchor(self, client, anchor_id=1):
        anchor = MagicMock()
        anchor.id = anchor_id
        client.elements = {anchor_id: anchor}
        return anchor

    def test_an_anchor_still_rendered_is_attached(self, fake_client):
        client, _ = fake_client
        assert _is_detached(client, self._anchor(client)) is False

    def test_a_cleared_anchor_detaches(self, fake_client):
        client, _ = fake_client
        anchor = self._anchor(client)
        client.elements.clear()     # what main_container.clear() does
        assert _is_detached(client, anchor) is True

    def test_no_anchor_falls_back_to_client_lifetime(self, fake_client):
        client, _ = fake_client
        client.elements = {}
        assert _is_detached(client, None) is False

    def test_a_disconnected_client_detaches_a_live_anchor(self, fake_client):
        client, _ = fake_client
        anchor = self._anchor(client)
        with patch.object(model.ng.Client, "instances", {}):
            assert _is_detached(client, anchor) is True

    @pytest.mark.asyncio
    async def test_switching_views_retires_the_old_view(self, fake_client):
        client, _ = fake_client
        sched = _PageScheduler(client.id)
        _schedulers[client.id] = sched

        calls = []
        for view in range(3):
            anchor = MagicMock()
            anchor.id = view
            tick = _CallbackTick(lambda v=view: calls.append(v), run_now=True)
            tick._anchor = anchor
            sched._tickables.append(tick)

        # Only the third view's elements survive the switch.
        client.elements = {2: object()}
        model.asyncio.gather = asyncio.gather      # the fixture stubs asyncio out
        await sched._tick()

        assert len(sched._tickables) == 1
        assert calls == [2]

    def test_shutdown_leaves_a_replacement_scheduler_alone(self, fake_client):
        client, _ = fake_client
        stale = _PageScheduler(client.id)
        live = _PageScheduler(client.id)
        _schedulers[client.id] = live

        stale._shutdown()

        assert _schedulers[client.id] is live


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
