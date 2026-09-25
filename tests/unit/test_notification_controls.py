"""The dashboard's notification controls, rendered in a real NiceGUI client."""

from types import SimpleNamespace

import pytest
from nicegui import ui
from nicegui.testing import User

from vigil.core.connectors.agent_connector import AgentRegistry
from vigil.core.notifications import NotificationEngine
from vigil.core.ui.views.notifications import open_notifications_dialog, render_mute_button

pytest_plugins = ['nicegui.testing.user_plugin']
pytestmark = pytest.mark.nicegui_main_file(None)


def _plugin(plugin_id, children=(), type_='cpu'):
    return SimpleNamespace(id=plugin_id, name=plugin_id.title(), target='host.lan',
                           config={'type': type_}, children=list(children))


class Channel:
    TYPE = 'desktop'

    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def send(self, message):
        if self.fail:
            raise ConnectionError('agent is not connected')
        self.sent.append(message)


@pytest.fixture
def notifications(db_manager):
    engine = NotificationEngine(db_manager, {}, AgentRegistry())
    engine.channels = {'desk': Channel(), 'broken': Channel(fail=True)}
    nas = _plugin('nas')
    engine.nas = nas
    engine.group = _plugin('storage', [nas], type_='group')
    yield engine
    engine.stop()


@pytest.fixture
def pages(notifications):
    """A monitor page's mute button at /nas and /storage, and the dialog at /dialog."""
    dashboard = SimpleNamespace(notifications=notifications)

    @ui.page('/nas')
    def _nas():
        render_mute_button(dashboard, notifications.nas)

    @ui.page('/storage')
    def _storage():
        render_mute_button(dashboard, notifications.group)

    @ui.page('/dialog')
    def _dialog():
        open_notifications_dialog(dashboard)


async def _started(notifications):
    notifications.start([notifications.group])


class TestMuteButton:
    async def test_toggles_the_monitors_mute(self, user: User, notifications, pages):
        await _started(notifications)
        await user.open('/nas')
        user.find('Mute').click()
        assert notifications.muted_by('nas') == 'nas'
        await user.should_see('Unmute')
        user.find('Unmute').click()
        assert notifications.muted_by('nas') is None
        await user.should_see('Mute')

    async def test_a_monitor_muted_by_its_group_says_so(self, user: User, notifications, pages):
        await _started(notifications)
        notifications.set_muted('storage', True)
        await user.open('/nas')
        await user.should_see('Muted by Storage')

    async def test_no_channels_means_no_button(self, user: User, notifications, pages):
        notifications.channels = {}
        await user.open('/nas')
        await user.should_not_see('Mute')


class TestDialog:
    async def test_lists_channels_and_sends_a_test(self, user: User, notifications, pages):
        await _started(notifications)
        await user.open('/dialog')
        await user.should_see('desk')
        await user.should_see('broken')
        user.find(marker='test-desk').click()
        await user.should_see('Test sent to desk')
        assert len(notifications.channels['desk'].sent) == 1
        user.find(marker='test-broken').click()
        await user.should_see('Test to broken failed: agent is not connected')

    async def test_unmutes_from_the_list(self, user: User, notifications, pages):
        await _started(notifications)
        notifications.set_muted('storage', True)
        await user.open('/dialog')
        await user.should_see('Storage')
        user.find('Unmute').click()
        await user.should_see('No monitors are muted.')
        assert notifications.muted_by('nas') is None

    async def test_says_when_nothing_is_muted(self, user: User, notifications, pages):
        await _started(notifications)
        await user.open('/dialog')
        await user.should_see('No monitors are muted.')
