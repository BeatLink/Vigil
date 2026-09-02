from types import SimpleNamespace

from vigil.core.ui.views.overview import (
    _build_chart_counts, _collect_leaf_monitors, _treemap_options, _treemap_tiles,
    _worst_status,
)

COLORS = {'online': 'green', 'warning': 'amber', 'failed': 'red', 'offline': 'grey'}


def _monitor(mid: str, mtype: str, children=()):
    return SimpleNamespace(id=mid, name=mid, target='host',
                           config={'type': mtype}, children=list(children))


class TestChartCounts:
    def test_status_totals_are_tallied(self):
        monitors = [_monitor('a', 'cpu'), _monitor('b', 'cpu'), _monitor('c', 'zfs')]
        statuses = {'a': 'online', 'b': 'failed', 'c': 'online'}
        status_counts, _ = _build_chart_counts(monitors, statuses)
        assert status_counts == {'online': 2, 'failed': 1, 'warning': 0, 'offline': 0}

    def test_each_type_is_broken_down_by_status(self):
        monitors = [_monitor('a', 'cpu'), _monitor('b', 'cpu'), _monitor('c', 'zfs')]
        statuses = {'a': 'online', 'b': 'failed', 'c': 'warning'}
        _, type_counts = _build_chart_counts(monitors, statuses)
        assert type_counts == {'cpu': {'online': 1, 'failed': 1}, 'zfs': {'warning': 1}}

    def test_an_unknown_monitor_counts_as_offline(self):
        status_counts, type_counts = _build_chart_counts([_monitor('a', 'cpu')], {})
        assert status_counts['offline'] == 1
        assert type_counts == {'cpu': {'offline': 1}}

    def test_a_monitor_without_a_type_is_grouped_as_unknown(self):
        m = SimpleNamespace(id='a', name='a', target='h', config={}, children=[])
        _, type_counts = _build_chart_counts([m], {'a': 'online'})
        assert type_counts == {'unknown': {'online': 1}}


class TestWorstStatus:
    def test_failed_outranks_everything(self):
        assert _worst_status({'online': 9, 'warning': 2, 'offline': 1, 'failed': 1}) == 'failed'

    def test_warning_outranks_offline_and_online(self):
        assert _worst_status({'online': 9, 'offline': 3, 'warning': 1}) == 'warning'

    def test_offline_outranks_online(self):
        assert _worst_status({'online': 9, 'offline': 1}) == 'offline'

    def test_an_all_healthy_type_reads_online(self):
        assert _worst_status({'online': 4}) == 'online'

    def test_a_zero_count_does_not_win(self):
        assert _worst_status({'failed': 0, 'online': 2}) == 'online'

    def test_an_empty_tally_falls_back_to_offline(self):
        assert _worst_status({}) == 'offline'


class TestTreemapTiles:
    def _tiles(self, type_counts):
        return _treemap_tiles(type_counts, COLORS)

    def test_tile_area_is_the_monitor_count(self):
        tiles = self._tiles({'cpu': {'online': 3, 'failed': 1}})
        assert tiles[0]['value'] == 4

    def test_tile_color_is_the_worst_status(self):
        tiles = self._tiles({'cpu': {'online': 3, 'failed': 1}})
        assert tiles[0]['status'] == 'failed'
        assert tiles[0]['itemStyle'] == {'color': 'red'}

    def test_a_healthy_type_takes_the_online_color(self):
        assert self._tiles({'cpu': {'online': 2}})[0]['itemStyle'] == {'color': 'green'}

    def test_the_label_is_the_uppercased_type(self):
        assert self._tiles({'systemd_service': {'online': 1}})[0]['name'] == 'SYSTEMD_SERVICE'

    def test_the_breakdown_lists_only_present_statuses(self):
        tiles = self._tiles({'cpu': {'online': 3, 'failed': 1}})
        assert tiles[0]['breakdown'] == '3 online, 1 failed'

    def test_largest_type_comes_first(self):
        tiles = self._tiles({'zfs': {'online': 1}, 'cpu': {'online': 5}, 'md': {'online': 3}})
        assert [t['name'] for t in tiles] == ['CPU', 'MD', 'ZFS']

    def test_equal_counts_fall_back_to_name_order_for_a_stable_layout(self):
        tiles = self._tiles({'zfs': {'online': 1}, 'cpu': {'online': 1}, 'md': {'online': 1}})
        assert [t['name'] for t in tiles] == ['CPU', 'MD', 'ZFS']

    def test_no_types_yields_no_tiles(self):
        assert self._tiles({}) == []


class TestLeafMonitors:
    def test_groups_are_flattened_to_their_leaves(self):
        tree = [_monitor('g', 'group', children=[_monitor('a', 'cpu'), _monitor('b', 'zfs')])]
        assert [m.id for m in _collect_leaf_monitors(tree)] == ['a', 'b']

    def test_a_group_is_not_itself_a_leaf(self):
        tree = [_monitor('g', 'group', children=[_monitor('a', 'cpu')])]
        _, type_counts = _build_chart_counts(_collect_leaf_monitors(tree), {'a': 'online'})
        assert 'group' not in type_counts


class TestTreemapOptions:
    def test_styling_sits_on_the_series_not_under_levels(self):
        # ECharts silently ignores a treemap's levels[0] block, so a label or
        # itemStyle parked there never reaches a tile.
        series = _treemap_options()['series'][0]
        assert 'levels' not in series
        assert series['label']['show'] is True
        assert series['itemStyle']['gapWidth'] == 2

    def test_the_label_carries_the_type_and_its_count(self):
        assert _treemap_options()['series'][0]['label']['formatter'] == '{b}\n{c}'

    def test_clicking_a_tile_filters_rather_than_zooming(self):
        assert _treemap_options()['series'][0]['nodeClick'] is False

    def test_the_tooltip_is_passed_as_a_js_function(self):
        # NiceGUI forwards a ':'-prefixed key to the frontend as JavaScript.
        assert ':formatter' in _treemap_options()['tooltip']
