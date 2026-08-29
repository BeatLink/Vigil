"""The events view: a filterable table of recent engine events."""

from typing import Callable
from nicegui import ui
from vigil.core.contracts import EngineLike
from ..theme import STATUS_COLORS, TEXT_SECONDARY
from ..components import card, feed_columns, section_title, on_data_event, offload, refresh_rows

_EVENT_COLUMNS = feed_columns(target_label='Target',
                              sortable=('timestamp', 'level', 'target'))

_LEVEL_CELL_SLOT = '''
    <q-td :props="props">
        <span :style="{ color: props.row.level === 'ERROR' ? '%s'
                            : props.row.level === 'WARNING' ? '%s' : '%s',
                        fontWeight: 600 }">
            {{ props.row.level }}
        </span>
    </q-td>
''' % (STATUS_COLORS['failed'], STATUS_COLORS['warning'], TEXT_SECONDARY)


def _render_filter_bar():
    """Renders the level/target/search filter inputs and returns them."""
    with ui.row().classes('w-full gap-3 mb-4 items-end'):
        level_sel = ui.select(
            {None: 'All levels', 'ERROR': 'Error', 'WARNING': 'Warning', 'INFO': 'Info'},
            value=None, label='Level',
        ).props('outlined dense options-dense').classes('w-40')
        target_in = ui.input(label='Target').props('outlined dense clearable').classes('w-56')
        search_in = ui.input(label='Search message').props('outlined dense clearable').classes('flex-1')
    return level_sel, target_in, search_in


def render_events(engine: EngineLike, switch_view: Callable):
    """Renders the events view: filter inputs over a level-colored event table."""
    section_title('Events')

    ev_filter = {'level': None, 'target': None, 'search': None}
    level_sel, target_in, search_in = _render_filter_bar()

    with card('w-full'):
        events_table = ui.table(columns=_EVENT_COLUMNS, rows=[], row_key='timestamp',
                                pagination=25).classes('w-full')
        events_table.add_slot('body-cell-level', _LEVEL_CELL_SLOT)

    async def refresh_events():
        rows = await offload(engine.db.recent_events)(
            limit=500,
            level=ev_filter['level'],
            target=(ev_filter['target'] or None),
            search=(ev_filter['search'] or None),
        )
        refresh_rows(events_table, rows)

    async def _on_level(e):
        ev_filter['level'] = e.value
        await refresh_events()

    async def _on_target(e):
        ev_filter['target'] = (e.value or '').strip() or None
        await refresh_events()

    async def _on_search(e):
        ev_filter['search'] = (e.value or '').strip() or None
        await refresh_events()

    level_sel.on_value_change(_on_level)
    target_in.on_value_change(_on_target)
    search_in.on_value_change(_on_search)

    on_data_event(refresh_events, run_now=False)
