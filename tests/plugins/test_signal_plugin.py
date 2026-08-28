import importlib

import pytest

from vigil.plugins.base.signal_plugin import SignalPlugin, worst_status

# Every single-signal monitor, as the config's `type` names it.
SIGNALS = [
    'cpu', 'memory', 'load', 'temperature', 'interrupts', 'gpu', 'oom',
    'throughput', 'connections', 'wifi',
    'smart', 'zfs', 'md', 'disk_io',
]


def _plugin_class(signal: str) -> type:
    from vigil.core.coordination.engine import _plugin_class as resolve
    path = f'vigil.plugins.{signal}'
    return resolve(importlib.import_module(path), path)


@pytest.fixture(params=SIGNALS)
def signal(request, make_plugin):
    cls = _plugin_class(request.param)
    return make_plugin(cls, {"name": request.param, "id": f'test-{request.param}',
                             "ssh_config": {"host": "test.host"}})


class TestContract:
    def test_every_signal_is_its_own_plugin(self, signal):
        assert isinstance(signal, SignalPlugin)

    def test_a_cycle_issues_exactly_one_command(self, signal):
        assert len(signal.commands()) == 1


class TestUiSpec:
    def test_the_page_starts_with_the_host_card_and_ends_with_events(self, signal):
        layout = signal.UI_SPEC['layout']
        assert layout[0][0] == 'host_card'
        assert layout[-1] == ['events']

    def test_every_card_and_chart_has_a_layout_cell(self, signal):
        spec = signal.UI_SPEC
        placed = {name for row in spec['layout'] for name in row}
        assert set(spec['cards']) <= placed
        assert set(spec['charts']) <= placed


class TestRendering:
    """A card naming an attribute the plugin does not have only fails when the
    page is built, which no other test does."""

    @pytest.fixture
    def no_scheduler(self, monkeypatch):
        monkeypatch.setattr('vigil.core.ui.model.PluginPage.start', lambda self: None)
        monkeypatch.setattr('vigil.core.ui.model.schedule_callback',
                            lambda callback, run_now=True: None)

    def test_the_page_builds(self, signal, no_scheduler):
        from nicegui import ui
        with ui.element('div'):
            signal.render_ui()


class TestWorstStatus:
    def test_offline_ranks_below_warning(self):
        assert worst_status(['offline', 'warning']) == 'warning'

    def test_failed_wins(self):
        assert worst_status(['online', 'warning', 'failed']) == 'failed'

    def test_empty_is_online(self):
        assert worst_status([]) == 'online'
