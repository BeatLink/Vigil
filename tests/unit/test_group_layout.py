import pytest

from vigil.core.ui.layout import CompositeLayout, resolve_rows
from vigil.plugins.group import Group, spec_with_titles
from vigil.plugins.uptime import Uptime


@pytest.fixture
def no_scheduler(monkeypatch):
    """Render without the change-bus scheduler, which needs a live client and loop."""
    monkeypatch.setattr('vigil.core.ui.model.PluginPage.start', lambda self: None)
    monkeypatch.setattr('vigil.core.ui.model.schedule_callback',
                        lambda callback, run_now=True: None)


TITLE_CLASSES = ('halon-label', 'halon-title-section')


def _labels(element, classes=None):
    from nicegui import ui

    for slot in element.slots.values():
        for child in slot.children:
            if isinstance(child, ui.label) and (classes is None
                                                or set(child._classes) & set(classes)):
                yield child.text
            yield from _labels(child, classes)


def _render(plugin, classes=TITLE_CLASSES):
    """Render into a detached container and read back the widget titles it built."""
    from nicegui import ui

    container = ui.element('div')
    with container:
        plugin.render_ui()
    return list(_labels(container, classes))


class TestResolveRows:
    def test_fills_defaults_for_a_bare_widget_name(self):
        (row,) = resolve_rows([['chart']], {})
        assert row == [('chart', {'flex': 1, 'visible': True, 'height': None,
                                  'min_width': '280px', 'title': None})]

    def test_config_overrides_reach_a_declared_widget(self):
        (row,) = resolve_rows([['chart']], {'chart': {'height': '500px', 'visible': False}})
        assert row[0][1]['height'] == '500px'
        assert row[0][1]['visible'] is False

    def test_inline_properties_win_over_config_overrides(self):
        (row,) = resolve_rows([[{'widget': 'chart', 'flex': 3}]], {'chart': {'flex': 1}})
        assert row[0][1]['flex'] == 3


class TestCompositeLayout:
    def test_splits_a_ref_into_child_and_widget(self):
        grid = CompositeLayout([['host-a.cpu_card', 'host-b.cpu_card']])
        assert list(grid.widgets_for('host-a')) == ['cpu_card']
        assert list(grid.widgets_for('host-b')) == ['cpu_card']

    def test_a_dotted_child_id_keeps_its_dots(self):
        grid = CompositeLayout([['srv.example.com.cpu_card']])
        assert list(grid.widgets_for('srv.example.com')) == ['cpu_card']

    def test_a_bare_ref_names_a_whole_child(self):
        grid = CompositeLayout([['host-a']])
        assert grid.hosts_child('host-a')
        assert grid.widgets_for('host-a') == {}

    def test_a_view_renders_only_its_own_placed_widgets(self):
        view = CompositeLayout([['host-a.cpu_card']]).view('host-a')
        assert view.renders('cpu_card')
        assert not view.renders('mem_card')

    def test_a_view_skips_a_widget_hidden_by_config(self):
        grid = CompositeLayout([[{'widget': 'host-a.cpu_card', 'visible': False}]])
        assert not grid.view('host-a').renders('cpu_card')

    def test_unclaimed_reports_a_ref_nothing_rendered_into(self):
        grid = CompositeLayout([['host-a.cpu_card', 'typo.cpu_card']])
        with grid.cell('host-a.cpu_card'):
            pass
        assert grid.unclaimed() == ['typo.cpu_card']

    def test_unclaimed_ignores_a_deliberately_hidden_ref(self):
        grid = CompositeLayout([[{'widget': 'host-a.cpu_card', 'visible': False}]])
        assert grid.unclaimed() == []


class TestSpecWithTitles:
    SPEC = {
        'cards': {'cpu_card': {'metric': 'cpu', 'title': 'CPU'}},
        'chart': {'metric': 'cpu', 'title': 'CPU HISTORY'},
        'charts': {'mem_chart': {'metric': 'mem', 'title': 'MEMORY'}},
    }

    def test_overrides_a_card_title(self):
        patched = spec_with_titles(self.SPEC, {'cpu_card': 'RAGNAROK CPU'})
        assert patched['cards']['cpu_card']['title'] == 'RAGNAROK CPU'

    def test_overrides_a_chart_title(self):
        patched = spec_with_titles(self.SPEC, {'chart': 'A', 'mem_chart': 'B'})
        assert patched['chart']['title'] == 'A'
        assert patched['charts']['mem_chart']['title'] == 'B'

    def test_leaves_the_plugins_own_spec_untouched(self):
        spec_with_titles(self.SPEC, {'cpu_card': 'RAGNAROK CPU', 'chart': 'A'})
        assert self.SPEC['cards']['cpu_card']['title'] == 'CPU'
        assert self.SPEC['chart']['title'] == 'CPU HISTORY'

    def test_returns_the_spec_unchanged_when_nothing_is_overridden(self):
        assert spec_with_titles(self.SPEC, {}) is self.SPEC


class TestDescendants:
    def test_walks_nested_groups_depth_first(self, make_plugin):
        leaf_a = make_plugin(Uptime, {'name': 'A', 'id': 'a'})
        leaf_b = make_plugin(Uptime, {'name': 'B', 'id': 'b'})
        inner = make_plugin(Group, {'name': 'Inner', 'id': 'inner', 'type': 'group'})
        outer = make_plugin(Group, {'name': 'Outer', 'id': 'outer', 'type': 'group'})
        inner.children = [leaf_b]
        outer.children = [inner, leaf_a]
        assert [p.id for p in outer._descendants()] == ['inner', 'b', 'a']


class TestDeclaredOrder:
    def test_a_page_follows_its_layout_rows_not_the_build_order(self, make_plugin, no_scheduler):
        plugin = make_plugin(Uptime, {'name': 'A', 'id': 'a'})
        labels = _render(plugin)
        assert labels[:3] == ['TARGET HOST', 'CURRENT STATUS', 'LAST LATENCY']


class TestCompositeRender:
    def _group(self, make_plugin, layout):
        group = make_plugin(Group, {'name': 'G', 'id': 'g', 'type': 'group', 'layout': layout})
        group.children = [
            make_plugin(Uptime, {'name': 'A', 'id': 'a'}),
            make_plugin(Uptime, {'name': 'B', 'id': 'b'}),
        ]
        return group

    def test_mixes_widgets_of_two_children_in_declared_order(self, make_plugin, no_scheduler):
        group = self._group(make_plugin, [['a.status_card', 'b.latency_card'], ['a.chart']])
        labels = _render(group)
        assert labels[:3] == ['CURRENT STATUS', 'LAST LATENCY', 'RESPONSE TIME HISTORY (ms)']

    def test_skips_every_widget_the_layout_leaves_out(self, make_plugin, no_scheduler):
        group = self._group(make_plugin, [['a.status_card']])
        labels = _render(group)
        assert 'TARGET HOST' not in labels
        assert 'LAST LATENCY' not in labels
        assert labels.count('CURRENT STATUS') == 1

    def test_a_title_override_renames_a_childs_card(self, make_plugin, no_scheduler):
        group = self._group(
            make_plugin,
            [[{'widget': 'a.latency_card', 'title': 'NODE A LATENCY'},
              {'widget': 'b.latency_card', 'title': 'NODE B LATENCY'}]],
        )
        labels = _render(group)
        assert labels[:2] == ['NODE A LATENCY', 'NODE B LATENCY']

    def test_a_bare_ref_renders_the_whole_child_under_its_title(self, make_plugin, no_scheduler):
        group = self._group(make_plugin, [[{'widget': 'a', 'title': 'Node A'}]])
        labels = _render(group)
        assert labels[0] == 'Node A'
        assert 'CURRENT STATUS' in labels

    def test_the_status_pseudo_widget_shows_the_childs_live_state(self, make_plugin,
                                                                  db_manager, no_scheduler):
        db_manager.insert_status('a', 'failed')
        group = self._group(make_plugin, [['a.status']])
        assert _render(group, classes=None) == ['A', 'FAILED']
