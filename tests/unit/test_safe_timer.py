"""
Tests for safe_timer's teardown handling.

Production symptom: after a client disconnected or a page re-rendered, timers
kept firing against deleted elements and NiceGUI raised "The parent slot of the
element has been deleted." on every tick, flooding the journal.

The raise happens inside NiceGUI's own task — `_run_in_loop` and
`_invoke_callback` both enter `self._get_context()` before the callback runs —
so wrapping the callback in try/except cannot catch it. `_should_stop` is the
hook that can.
"""
import pytest
from nicegui import ui, Client

from vigil.web.ui.components import safe_timer, _SafeTimer


@pytest.fixture
def page():
    """A live client layout to build elements in."""
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
        # _should_stop is checked each loop iteration, so returning True here
        # ends the timer cleanly instead of raising on the next tick.
        with ui.card() as card:
            t = safe_timer(1.0, lambda: None)
        card.delete()
        assert t._should_stop() is True

    def test_parent_slot_is_not_the_signal(self, page):
        # After a delete, parent_slot still returns the orphaned Slot — using
        # it as the detachment test silently never fires.
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
        t.callback()
        assert calls == [1]

    def test_teardown_error_mid_callback_is_swallowed(self, page):
        def boom():
            raise RuntimeError('The parent slot of the element has been deleted.')
        with ui.card():
            t = safe_timer(1.0, boom)
        t.callback()   # must not propagate

    def test_unrelated_errors_still_propagate(self, page):
        def boom():
            raise RuntimeError('something else entirely')
        with ui.card():
            t = safe_timer(1.0, boom)
        with pytest.raises(RuntimeError, match='something else'):
            t.callback()
