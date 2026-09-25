"""The plugin detail view's Poll Now button, driven inside a real NiceGUI client.

A poll can outlast the page it was started from; these pin that its notice
reaches the viewer, and that nothing raises once the viewer's tab is gone.
"""

import asyncio
import gc
from types import SimpleNamespace

import pytest
from nicegui import ui
from nicegui.testing import User

from vigil.core.ui.views.plugin_detail import render_plugin_detail

pytest_plugins = ['nicegui.testing.user_plugin']
pytestmark = pytest.mark.nicegui_main_file(None)


class _SlowPlugin:
    """A monitor whose poll waits until the test releases it."""

    name = 'Vault'
    id = 'vault'

    def __init__(self):
        self.release = asyncio.Event()
        self.polled = False

    async def run_cycle(self):
        await self.release.wait()
        self.polled = True
        return True

    async def present(self):
        return {'actions': []}

    def render_ui(self, context='page'):
        ui.label('plugin body')


@pytest.fixture(autouse=True)
def notify_finds_its_client(monkeypatch):
    """The test harness records notifications without looking up a client, so this restores the lookup the real ui.notify starts with."""
    from nicegui import context
    from nicegui.testing.user_notify import UserNotify
    record = UserNotify.__call__

    def notify(self, *args, **kwargs):
        context.client
        return record(self, *args, **kwargs)

    monkeypatch.setattr(UserNotify, '__call__', notify)


@pytest.fixture
def plugin():
    return _SlowPlugin()


@pytest.fixture
def page(plugin):
    engine = SimpleNamespace(notifications=SimpleNamespace(channels=[]))

    @ui.page('/')
    def _page():
        render_plugin_detail(engine, lambda *_: None, plugin)


async def _poll_and_wait(user: User, plugin):
    await user.should_see('Poll Now')
    user.find('Poll Now').click()
    await asyncio.sleep(0.01)
    return plugin


class TestPollNow:
    async def test_notifies_when_the_poll_finishes(self, user: User, page, plugin):
        await user.open('/')
        await _poll_and_wait(user, plugin)
        plugin.release.set()
        await user.should_see('Vault polled')


def _orphaned_slot(client):
    """A slot whose element has been deleted and collected: where a handler stands once its page is gone."""
    with client:
        element = ui.row()
    slot = element.default_slot
    element.delete()
    del element
    gc.collect()
    return slot


class TestNotifyClient:
    async def test_a_plain_notify_from_a_dead_slot_raises(self, user: User, page):
        await user.open('/')
        with _orphaned_slot(user.client), pytest.raises(RuntimeError, match='has been deleted'):
            ui.notify('lost')

    async def test_reaches_the_viewer_from_a_dead_slot(self, user: User, page):
        from vigil.core.ui.components import notify_client
        await user.open('/')
        with _orphaned_slot(user.client):
            notify_client(user.client, 'still delivered')
        await user.should_see('still delivered')

    async def test_a_deleted_client_gets_nothing(self, user: User, page):
        from vigil.core.ui.components import notify_client
        await user.open('/')
        client = user.client
        client.delete()
        notify_client(client, 'nobody is listening')
