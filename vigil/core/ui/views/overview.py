"""The overview view: the status donut and type treemap, the filterable monitor table and recent activity."""

from dataclasses import dataclass
from typing import Callable, Optional
from nicegui import ui
from vigil.core.contracts import EngineLike
from .. import theme
from ..theme import STATUS_COLORS, ACCENT
from ..components import card, feed_columns, section_title, on_data_event, offload, refresh_rows

_STATUS_ORDER = ('online', 'failed', 'warning', 'offline')

# Worst first. A type's tile takes the most severe status among its monitors, so
# one failed service is visible even inside a type that is otherwise healthy.
_STATUS_SEVERITY = ('failed', 'warning', 'offline', 'online')

# The treemap's key, in DOM rather than on the canvas so it follows the scheme
# with no repaint.
_STATUS_LEGEND = ''.join(
    '<span style="display:inline-flex;align-items:center;gap:4px;margin-right:12px">'
    f'<span style="width:8px;height:8px;border-radius:2px;background:{STATUS_COLORS[name]}"></span>'
    f'{name.capitalize()}</span>'
    for name in _STATUS_ORDER
)

_MONITOR_COLUMNS = [
    {'name': 'name',   'label': 'Monitor', 'field': 'name',   'align': 'left', 'sortable': True},
    {'name': 'type',   'label': 'Type',    'field': 'type',   'align': 'left', 'sortable': True},
    {'name': 'host',   'label': 'Host',    'field': 'host',   'align': 'left', 'sortable': True},
    {'name': 'status', 'label': 'Status',  'field': 'status', 'align': 'left', 'sortable': True},
]

_METRIC_COLUMNS = [
    {'name': 'timestamp', 'label': 'Time', 'field': 'timestamp', 'align': 'left'},
    {'name': 'target', 'label': 'Host', 'field': 'target', 'align': 'left'},
    {'name': 'plugin_id', 'label': 'Plugin', 'field': 'plugin_id', 'align': 'left'},
    {'name': 'metric_name', 'label': 'Metric', 'field': 'metric_name', 'align': 'left'},
    {'name': 'value', 'label': 'Value', 'field': 'value', 'align': 'left'},
]

_RECENT_EVENT_COLUMNS = feed_columns(target_label='Host')

_NAME_CELL_SLOT = '''
    <q-td :props="props">
        <span class="cursor-pointer font-medium hover:underline"
              style="color: %s"
              @click="$parent.$emit('navigate', props.row)">
            {{ props.row.name }}
        </span>
    </q-td>
''' % ACCENT

_STATUS_CELL_SLOT = '''
    <q-td :props="props">
        <span :style="{ color: props.row.status_color }" class="font-semibold halon-caption">
            {{ props.row.status }}
        </span>
    </q-td>
'''


@dataclass
class _ChartFilter:
    """The chart-click filter currently applied to the monitor table."""
    field: Optional[str] = None
    value: Optional[str] = None


def _pie_options() -> dict:
    """Options for the status donut: four fixed slices, one per state."""
    return {
        'tooltip': {'trigger': 'item', 'formatter': '{b}: {c} ({d}%)'},
        'legend': {'bottom': '0', 'left': 'center', 'textStyle': {'fontSize': 12}},
        'series': [{
            'type': 'pie',
            'radius': ['40%', '70%'],
            'avoidLabelOverlap': False,
            'cursor': 'pointer',
            'itemStyle': {'borderRadius': 8, 'borderWidth': 2},
            'label': {'show': False},
            'data': []
        }]
    }


def _treemap_options() -> dict:
    """Options for the type treemap: tile area is the monitor count, fill the
    worst status among them. One tile per type, so 25 types cost no palette —
    the label inside each tile carries identity and color is left to mean state."""
    return {
        'tooltip': {
            ':formatter':
                "p => `${p.name}: ${p.value} monitor${p.value === 1 ? '' : 's'}"
                " — ${p.data.breakdown}`",
        },
        'series': [{
            'type': 'treemap',
            'roam': False,
            # Without this a click zooms into the tile instead of filtering the table.
            'nodeClick': False,
            'breadcrumb': {'show': False},
            'width': '100%', 'height': '100%',
            'top': 0, 'left': 0, 'right': 0, 'bottom': 0,
            # Both of these must sit on the series: a treemap silently ignores a
            # levels[0] block, so styling put there never reaches the tiles.
            'itemStyle': {'borderWidth': 2, 'gapWidth': 2, 'borderRadius': 4},
            'label': {'show': True, 'position': 'insideTopLeft', 'fontSize': 10,
                      'lineHeight': 13, 'overflow': 'truncate', 'formatter': '{b}\n{c}'},
            'data': [],
        }],
    }


def _collect_leaf_monitors(plugins) -> list:
    """Flattens the plugin hierarchy into its leaf monitors."""
    monitors = []
    for p in plugins:
        if not p.children:
            monitors.append(p)
        else:
            monitors.extend(_collect_leaf_monitors(p.children))
    return monitors


def _build_table_rows(monitors, statuses, flt: _ChartFilter) -> list:
    """Builds the monitor-table rows, honoring the active chart filter."""
    rows = []
    for m in monitors:
        st = statuses.get(m.id, 'offline')
        mtype = m.config.get('type', 'unknown')
        if flt.field == 'status' and st != flt.value:
            continue
        if flt.field == 'type' and mtype != flt.value:
            continue
        rows.append({
            'id': m.id,
            'name': m.name,
            'type': mtype.upper(),
            'host': m.target,
            'status': st.upper(),
            'status_color': STATUS_COLORS.get(st, STATUS_COLORS['offline']),
        })
    return rows


def _build_chart_counts(monitors, statuses):
    """Tallies monitors by status, and per type by status, for the two charts."""
    status_counts = {'online': 0, 'failed': 0, 'warning': 0, 'offline': 0}
    type_counts = {}
    for m in monitors:
        st = statuses.get(m.id, 'offline')
        status_counts[st] = status_counts.get(st, 0) + 1
        by_status = type_counts.setdefault(m.config.get('type', 'unknown'), {})
        by_status[st] = by_status.get(st, 0) + 1
    return status_counts, type_counts


def _worst_status(by_status: dict) -> str:
    """The most severe status present among one type's monitors."""
    for name in _STATUS_SEVERITY:
        if by_status.get(name):
            return name
    return 'offline'


def _treemap_tiles(type_counts: dict, colors: dict) -> list:
    """Builds the treemap's tiles, largest type first so the layout is stable."""
    tiles = []
    for mtype, by_status in type_counts.items():
        status = _worst_status(by_status)
        tiles.append({
            'name': mtype.upper(),
            'value': sum(by_status.values()),
            'status': status,
            'breakdown': ', '.join(f'{by_status[s]} {s}' for s in _STATUS_ORDER if by_status.get(s)),
            'itemStyle': {'color': colors[status]},
        })
    tiles.sort(key=lambda t: (-t['value'], t['name']))
    return tiles


def _render_charts():
    """Renders the status and type cards and returns (status_chart, type_chart)."""
    with ui.row().classes('w-full gap-4 mb-6 halon-section-gap'):
        with card('flex-1 h-80 min-w-[320px]'):
            ui.label('Monitors by status').classes('halon-label mb-2')
            status_chart = ui.echart(_pie_options()).classes('w-full h-64')
        with card('flex-1 h-80 min-w-[320px]'):
            ui.label('Monitors by type').classes('halon-label')
            ui.html(_STATUS_LEGEND).classes('halon-caption mb-2')
            type_chart = ui.echart(_treemap_options()).classes('w-full h-56')
    return status_chart, type_chart


def _render_monitor_card(plugin_by_id, switch_view: Callable):
    """Renders the all-monitors card and returns its table and filter chrome."""
    with card('w-full mb-6'):
        with ui.row().classes('w-full items-center justify-between mb-3'):
            ui.label('All monitors').classes('halon-label')
            with ui.row().classes('items-center gap-1') as filter_row:
                filter_label = ui.label('').classes('halon-caption')
                clear_button = ui.button(icon='close', color=None).props('flat dense round size=sm')
        filter_row.set_visibility(False)

        monitor_table = ui.table(columns=_MONITOR_COLUMNS, rows=[]).classes('w-full border-none')
        monitor_table.add_slot('body-cell-name', _NAME_CELL_SLOT)
        monitor_table.add_slot('body-cell-status', _STATUS_CELL_SLOT)

        def _navigate_to_row(e):
            row_id = (e.args or {}).get('id')
            if row_id and row_id in plugin_by_id:
                switch_view('plugin', plugin_by_id[row_id])
        monitor_table.on('navigate', _navigate_to_row)
    return monitor_table, filter_row, filter_label, clear_button


def _wire_filtering(engine, monitors, flt: _ChartFilter, status_chart, type_chart,
                    monitor_table, filter_row, filter_label, clear_button):
    """Connects chart clicks and the clear button to the monitor-table filter."""
    def _update_filter_ui():
        if flt.field:
            filter_label.text = f'Showing: {flt.value.upper()} — click again to clear'
            filter_row.set_visibility(True)
        else:
            filter_row.set_visibility(False)

    async def update_table():
        statuses = await offload(engine.db.latest_statuses)()
        refresh_rows(monitor_table, _build_table_rows(monitors, statuses, flt))

    async def _clear_filter():
        flt.field = None
        flt.value = None
        _update_filter_ui()
        await update_table()

    async def _set_filter(field: str, raw_value: str):
        value = raw_value.lower()
        if flt.field == field and flt.value == value:
            flt.field = None
            flt.value = None
        else:
            flt.field = field
            flt.value = value
        _update_filter_ui()
        await update_table()

    clear_button.on_click(_clear_filter)
    status_chart.on_point_click(lambda e: _set_filter('status', e.name))
    type_chart.on_point_click(lambda e: _set_filter('type', e.name))


def _wire_charts(engine, monitors, flt: _ChartFilter, status_chart, type_chart, monitor_table):
    """Feeds both charts and the monitor table from status changes, diffing before repaint."""
    last_statuses = {'value': None}
    chart_colors = {'value': theme.current_palette()}

    def _repaint_charts(colors):
        chart_colors['value'] = colors
        donut = status_chart.options['series'][0]
        donut['itemStyle']['borderColor'] = colors['surface']
        status_chart.options['legend'].setdefault('textStyle', {})['color'] = colors['text_secondary']
        for entry, state_name in zip(donut['data'], _STATUS_ORDER):
            entry['itemStyle'] = {'color': colors[state_name]}

        treemap = type_chart.options['series'][0]
        treemap['itemStyle']['borderColor'] = colors['surface']
        treemap['label']['color'] = colors['text_on_fill']
        for tile in treemap['data']:
            tile['itemStyle'] = {'color': colors[tile['status']]}
        status_chart.update()
        type_chart.update()

    theme.on_scheme_change(_repaint_charts)

    async def update_charts():
        statuses = await offload(engine.db.latest_statuses)()
        if statuses == last_statuses['value']:
            return
        last_statuses['value'] = statuses
        status_counts, type_counts = _build_chart_counts(monitors, statuses)
        colors = chart_colors['value']
        status_chart.options['series'][0]['data'] = [
            {'value': status_counts[name], 'name': name.capitalize(), 'itemStyle': {'color': colors[name]}}
            for name in _STATUS_ORDER
        ]
        type_chart.options['series'][0]['data'] = _treemap_tiles(type_counts, colors)
        status_chart.update()
        type_chart.update()
        monitor_table.rows = _build_table_rows(monitors, statuses, flt)
        monitor_table.update()

    on_data_event(update_charts, run_now=False)


def _render_recent_metrics(engine):
    """Renders the recent system metrics card fed from the raw metric feed."""
    with card('flex-1 min-w-[320px]'):
        ui.label('Recent system metrics').classes('halon-label mb-2')
        m_table = ui.table(columns=_METRIC_COLUMNS, rows=[]).classes('w-full')

        async def update_m():
            refresh_rows(m_table, await offload(engine.db.recent_metrics_raw)(limit=20))
        on_data_event(update_m)


def _render_recent_events(engine):
    """Renders the recent events card fed from the raw event feed."""
    with card('flex-1 min-w-[320px]'):
        ui.label('Recent events').classes('halon-label mb-2')
        e_table = ui.table(columns=_RECENT_EVENT_COLUMNS, rows=[]).classes('w-full')

        async def update_e():
            refresh_rows(e_table, await offload(engine.db.recent_events_raw)(limit=20))
        on_data_event(update_e)


def render_overview(engine: EngineLike, switch_view: Callable):
    """Renders the overview: the two charts, the filterable monitor table and recent activity."""
    section_title('Monitors')

    monitors = _collect_leaf_monitors(engine.plugins)
    plugin_by_id = {p.id: p for p in monitors}
    flt = _ChartFilter()

    status_chart, type_chart = _render_charts()
    monitor_table, filter_row, filter_label, clear_button = _render_monitor_card(plugin_by_id, switch_view)
    _wire_filtering(engine, monitors, flt, status_chart, type_chart,
                    monitor_table, filter_row, filter_label, clear_button)
    _wire_charts(engine, monitors, flt, status_chart, type_chart, monitor_table)

    with ui.row().classes('w-full gap-4'):
        _render_recent_metrics(engine)
        _render_recent_events(engine)
