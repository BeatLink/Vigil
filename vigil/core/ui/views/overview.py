"""The overview view: status and type donuts, the filterable monitor table and recent activity."""

from dataclasses import dataclass
from typing import Callable, Optional
from nicegui import ui
from vigil.core.contracts import EngineLike
from .. import theme
from ..theme import STATUS_COLORS, ACCENT
from ..components import card, section_title, on_data_event, offload, refresh_rows

_STATUS_ORDER = ('online', 'failed', 'warning', 'offline')

_MONITOR_COLUMNS = [
    {'name': 'name',   'label': 'Monitor', 'field': 'name',   'align': 'left', 'sortable': True},
    {'name': 'type',   'label': 'Type',    'field': 'type',   'align': 'left', 'sortable': True},
    {'name': 'host',   'label': 'Host',    'field': 'host',   'align': 'left', 'sortable': True},
    {'name': 'status', 'label': 'Status',  'field': 'status', 'align': 'left', 'sortable': True},
]

_METRIC_COLUMNS = [
    {'name': 'timestamp', 'label': 'Time', 'field': 'timestamp', 'align': 'left'},
    {'name': 'target', 'label': 'Host', 'field': 'target', 'align': 'left'},
    {'name': 'collector', 'label': 'Plugin', 'field': 'collector', 'align': 'left'},
    {'name': 'metric_name', 'label': 'Metric', 'field': 'metric_name', 'align': 'left'},
    {'name': 'value', 'label': 'Value', 'field': 'value', 'align': 'left'},
]

_RECENT_EVENT_COLUMNS = [
    {'name': 'timestamp', 'label': 'Time', 'field': 'timestamp', 'align': 'left'},
    {'name': 'level', 'label': 'Level', 'field': 'level', 'align': 'left'},
    {'name': 'target', 'label': 'Host', 'field': 'target', 'align': 'left'},
    {'name': 'message', 'label': 'Message', 'field': 'message', 'align': 'left'},
]

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
    """Base echart options shared by the status and type donuts."""
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
    """Tallies monitors by status and by type for the two donuts."""
    status_counts = {'online': 0, 'failed': 0, 'warning': 0, 'offline': 0}
    type_counts = {}
    for m in monitors:
        st = statuses.get(m.id, 'offline')
        status_counts[st] = status_counts.get(st, 0) + 1
        mtype = m.config.get('type', 'unknown')
        type_counts[mtype] = type_counts.get(mtype, 0) + 1
    return status_counts, type_counts


def _render_charts():
    """Renders the two donut cards and returns (status_chart, type_chart)."""
    with ui.row().classes('w-full gap-4 mb-6 halon-section-gap'):
        with card('flex-1 h-80 min-w-[320px]'):
            ui.label('Monitors by status').classes('halon-label mb-2')
            status_chart = ui.echart(_pie_options()).classes('w-full h-64')
        with card('flex-1 h-80 min-w-[320px]'):
            ui.label('Monitors by type').classes('halon-label mb-2')
            type_chart = ui.echart(_pie_options()).classes('w-full h-64')
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
    """Feeds both donuts and the monitor table from status changes, diffing before repaint."""
    last_statuses = {'value': None}
    chart_colors = {'value': theme.current_palette()}

    def _repaint_charts(colors):
        chart_colors['value'] = colors
        for chart in (status_chart, type_chart):
            chart.options['series'][0]['itemStyle']['borderColor'] = colors['surface']
            chart.options['legend'].setdefault('textStyle', {})['color'] = colors['text_secondary']
        for entry, state_name in zip(status_chart.options['series'][0]['data'], _STATUS_ORDER):
            entry['itemStyle'] = {'color': colors[state_name]}
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
        type_chart.options['series'][0]['data'] = [
            {'value': v, 'name': k.upper()} for k, v in type_counts.items()
        ]
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
    """Renders the overview: donut charts, the filterable monitor table and recent activity."""
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
