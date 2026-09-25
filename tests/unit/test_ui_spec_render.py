"""generic_render building a real card, inside a real NiceGUI client.

Every other spec test asserts on the UI_SPEC dict or on a resolved rule; this
one renders one and colors it, so a card's inline thresholds are proven against
the widgets a viewer actually gets rather than against the dict describing them.
"""

import pytest
from nicegui import ui
from nicegui.testing import User

from vigil.core.connectors.types import CollectResult
from vigil.core.ui import spec
from vigil.core.ui.theme import STATUS_COLORS
from vigil.plugins.cpu import Cpu

# The user fixture only — nicegui.testing.plugin also pulls in its selenium Screen plugin.
pytest_plugins = ['nicegui.testing.user_plugin']

# No app main file to auto-import: the page under test is declared in this module.
pytestmark = pytest.mark.nicegui_main_file(None)


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(Cpu, {"name": "probe", "id": "probe", "warning": 70, "threshold": 85,
                             "ssh_config": {"host": "test.host"}})


@pytest.fixture
def render_page(plugin):
    """Serve the plugin's declarative UI at '/', handing the test its PluginPage.

    `start=False` keeps the page off the client's refresh scheduler, so the test
    ticks it itself and nothing fires between assertions.
    """
    built = {}

    @ui.page('/')
    def _page():
        from nicegui import context
        built['page'] = spec.generic_render(plugin, start=False)
        built['client'] = context.client

    return built


async def _ticked(user: User, built: dict, page_path: str = '/'):
    """Open the page, run one refresh, and let the bindings settle.

    A colored card has two halves: the value arrives through a NiceGUI binding
    and the color is set by the refresh callback. Stepping the binding loop by
    hand keeps both readable in the same assertion without sleeping on its timer.
    """
    from nicegui import binding
    await user.open(page_path)
    await built['page']._tick()
    binding._refresh_step()
    return built


def _cpu_label(built: dict):
    """The CPU card's value label — the one bound to the cpu_pct metric."""
    from nicegui.elements.label import Label
    labels = [e for e in built['client'].elements.values()
              if isinstance(e, Label) and '%' in (e.text or '')]
    assert labels, "no formatted percentage label was rendered"
    return labels[0]


class TestInlineThresholdColoring:
    async def test_a_reading_under_the_warning_renders_online(self, user: User, plugin, render_page):
        plugin.storage.apply(CollectResult(metrics={'cpu_pct': 12.0}, status='online'))
        built = await _ticked(user, render_page)
        label = _cpu_label(built)
        assert label.text == '12.0%'
        assert STATUS_COLORS['online'] in label._style['color']

    async def test_a_reading_past_the_threshold_renders_failed(self, user: User, plugin, render_page):
        plugin.storage.apply(CollectResult(metrics={'cpu_pct': 97.5}, status='failed'))
        built = await _ticked(user, render_page)
        label = _cpu_label(built)
        assert label.text == '97.5%'
        assert STATUS_COLORS['failed'] in label._style['color']

    async def test_a_reading_between_the_bounds_renders_warning(self, user: User, plugin, render_page):
        plugin.storage.apply(CollectResult(metrics={'cpu_pct': 78.0}, status='warning'))
        built = await _ticked(user, render_page)
        assert STATUS_COLORS['warning'] in _cpu_label(built)._style['color']

    async def test_no_reading_leaves_the_card_uncolored(self, user: User, render_page):
        built = await _ticked(user, render_page)
        label = _cpu_label(built)
        assert label.text == '-- %'
        assert 'color' not in label._style
