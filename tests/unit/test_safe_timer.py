import asyncio
import pytest
from nicegui import ui, Client

from vigil.core.ui.components import safe_timer, _SafeTimer


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def page():
    with Client(lambda: None, request=None).layout:
        yield


class TestDetachment:
    def test_uses_the_safe_subclass(self, page):
        assert isinstance(safe_timer(1.0, lambda: None), _SafeTimer)

    def test_attached_timer_keeps_running(self, page):
        with ui.card():
            t = safe_timer(1.0, lambda: None)
        assert t._detached() is False

    def test_detects_deleted_parent(self, page):
        with ui.card() as card:
            t = safe_timer(1.0, lambda: None)
        card.delete()
        assert t._detached() is True

    def test_stops_once_detached(self, page):
        with ui.card() as card:
            t = safe_timer(1.0, lambda: None)
        card.delete()
        assert t._should_stop() is True

    def test_parent_slot_is_not_the_signal(self, page):
        with ui.card() as card:
            t = safe_timer(1.0, lambda: None)
        card.delete()
        assert t.parent_slot is not None
        assert t._detached() is True


class TestCallbackGuard:
    def test_callback_runs_while_attached(self, page):
        calls = []
        with ui.card():
            t = safe_timer(1.0, lambda: calls.append(1))
        _run(t.callback())
        assert calls == [1]

    def test_teardown_error_mid_callback_is_swallowed(self, page):
        def boom():
            raise RuntimeError('The parent slot of the element has been deleted.')
        with ui.card():
            t = safe_timer(1.0, boom)
        _run(t.callback())

    def test_unrelated_errors_still_propagate(self, page):
        def boom():
            raise RuntimeError('something else entirely')
        with ui.card():
            t = safe_timer(1.0, boom)
        with pytest.raises(RuntimeError, match='something else'):
            _run(t.callback())
