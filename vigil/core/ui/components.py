from nicegui import ui
from .theme import TEXT, TEXT_MUTED, PRIMARY, ACCENT, STATUS_COLORS, BACKGROUND_MUTED, BACKGROUND


class _SafeTimer(ui.timer):
    """
    A ui.timer that stops itself once the page it belongs to is gone.

    NiceGUI resolves the timer's context *outside* the callback — both in
    `_run_in_loop` (once, around the whole loop) and again in
    `_invoke_callback`. Both do `with self._get_context()`, which raises
    "The parent slot of the element has been deleted." as soon as the client
    disconnects or the page re-renders. Because that raise happens in NiceGUI's
    own task, a try/except around the callback never sees it; the error reaches
    `app.handle_exception` and floods the log every tick instead.

    Overriding `_should_stop` — the hook NiceGUI checks each iteration — makes
    detachment an ordinary stop condition, so the timer exits its loop cleanly
    rather than raising. `_can_start` gets the same treatment for a timer whose
    page dies before its first tick.
    """

    def _detached(self) -> bool:
        """
        True once this timer's element has been removed from its client.

        `parent_slot` is deliberately not used as the signal: after a delete it
        still returns the (now orphaned) Slot object, and only raises later
        once the slot's own parent is gone — the very raise this class exists
        to avoid. NiceGUI marks the element `is_deleted` and drops it from
        `client.elements` at delete time, so those are checked instead.
        """
        if getattr(self, "is_deleted", False):
            return True
        try:
            return self.id not in self.client.elements
        except Exception:
            # No client at all (page fully torn down) — nothing left to update.
            return True

    def _should_stop(self) -> bool:
        return self._detached() or super()._should_stop()


def safe_timer(interval: float, callback):
    """
    Create a periodic timer that goes quiet once its page is torn down.

    Plain `ui.timer` keeps firing against deleted elements after a client
    disconnects, raising on every tick. This variant cancels itself instead —
    see _SafeTimer. The callback is additionally guarded so a teardown race
    mid-callback is swallowed rather than logged.
    """
    timer = None

    def _wrapped():
        try:
            callback()
        except RuntimeError as e:
            if 'parent slot' in str(e) or 'has been deleted' in str(e):
                if timer is not None:
                    timer.cancel()
                return
            raise

    timer = _SafeTimer(interval, _wrapped)
    return timer


# Standardized UI Sizing Constants
LABEL_CLASS = 'text-xs font-bold'
VALUE_CLASS = 'text-2xl font-bold'
SECTION_CLASS = 'text-xl font-bold'
HOVER_STYLE = 'hover:bg-blue-50 cursor-pointer'

def card(classes: str = '', padding: bool = True):
    """A standard container with consistent padding and shadow."""
    p = 'p-4' if padding else 'p-0'
    return ui.card().classes(f'{p} shadow-sm {classes}')

def info_card(title: str, value: str = '--', value_classes: str = VALUE_CLASS, card_classes: str = 'flex-1'):
    """A card component for displaying a label and a large value."""
    with card(f'min-w-36 h-28 overflow-hidden items-center justify-center {card_classes}'):
        ui.label(title.upper()).classes(LABEL_CLASS).style(f'color: {TEXT_MUTED}')
        return ui.label(value).classes(f'{value_classes} w-full text-center break-words').style(f'color: {PRIMARY}')

def action_chip(text: str, on_click=None, icon: str = 'play_arrow', color: str = PRIMARY):
    """A standardized chip for control actions."""
    return ui.chip(text, icon=icon, on_click=on_click, color=color, text_color=BACKGROUND).props('clickable')

def section_title(text: str, classes: str = ''):
    """A standardized heading for dashboard sections."""
    return ui.label(text).classes(f'{SECTION_CLASS} mb-4 {classes}').style(f'color: {TEXT}')

def metric_table(collector: str, title: str = 'Monitor Metrics', limit: int = 15):
    """A standardized table for displaying recent metrics for a specific collector."""
    from vigil.core.data.database import Metric
    with card():
        ui.label(title).classes('font-bold mb-2').style(f'color: {PRIMARY}')
        table = ui.table(columns=[
            {'name': 'ts', 'label': 'Time', 'field': 'timestamp', 'align': 'left'},
            {'name': 'name', 'label': 'Metric', 'field': 'metric_name', 'align': 'left'},
            {'name': 'val', 'label': 'Value', 'field': 'value', 'align': 'left'},
        ], rows=[]).classes('w-full border-none')

        def update():
            query = Metric.select().where(Metric.collector == collector).order_by(Metric.timestamp.desc()).limit(limit)
            # Assign (not slice-mutate) and call update(): NiceGUI only pushes
            # _props to the client on an explicit update(), so an in-place edit
            # of table.rows would never reach the browser.
            table.rows = [m.__data__ for m in query]
            table.update()

        safe_timer(5.0, update)
        return table

def log_table(target: str, filter_prefix: str = '', title: str = 'Recent Logs', limit: int = 15, full_height: bool = False):
    """
    A standardized table for displaying persisted log lines, optionally filling
    available height.

    Reads from the LogLine table (deduplicated, retained log storage). When
    `filter_prefix` is given it scopes to that source (the plugin name); with no
    prefix it shows every source for the target.
    """
    from vigil.core.data.database import LogLine
    card_classes = 'w-full overflow-hidden flex-grow' if full_height else ''
    
    with card(card_classes, padding=not full_height):
        if full_height:
            ui.label(title).classes('font-bold p-4 w-full border-b').style(f'background-color: {BACKGROUND_MUTED}; color: {PRIMARY}')
        else:
            ui.label(title).classes('font-bold mb-2').style(f'color: {PRIMARY}')

        columns = [
            {'name': 'ts', 'label': 'Time', 'field': 'timestamp', 'align': 'left', 'sortable': True},
            {'name': 'lvl', 'label': 'Level', 'field': 'level', 'align': 'left'},
            {'name': 'msg', 'label': 'Message', 'field': 'message', 'align': 'left', 
             'classes': 'text-wrap font-mono text-xs' if full_height else ''},
        ]

        table_classes = 'w-full border-none'
        if full_height:
            table_classes += ' h-[600px]'

        table = ui.table(columns=columns, rows=[]).classes(table_classes)
        if full_height:
            table.props('virtual-scroll')

        def update_logs():
            condition = (LogLine.target == target)
            if filter_prefix:
                condition &= (LogLine.source == filter_prefix)
            query = LogLine.select().where(condition).order_by(LogLine.timestamp.desc()).limit(limit)
            table.rows = [e.__data__ for e in query]
            table.update()

        safe_timer(5.0, update_logs)
        return table

def event_table(plugin_name: str, target: str = '', title: str = 'Recent Events',
                limit: int = 100, full_height: bool = False):
    """
    A table of a plugin's own Event messages — everything it wrote via
    `db_logger.write`.

    Distinct from `log_table`, which reads LogLine: that table holds raw log
    output pulled off a target (journald and friends), deduplicated so a
    re-fetched line is stored once. Plugins that don't collect logs from a
    target — borg, for instance — have no LogLine rows at all, so a log_table
    on their page renders permanently empty while their Events pile up unseen.

    Events are prefixed "[Plugin Name] " by the logger, so that prefix is both
    the filter and something to strip for display.
    """
    from vigil.core.data.database import Event
    prefix = f"[{plugin_name}] "
    card_classes = 'w-full overflow-hidden flex-grow' if full_height else ''

    with card(card_classes, padding=not full_height):
        if full_height:
            ui.label(title).classes('font-bold p-4 w-full border-b').style(
                f'background-color: {BACKGROUND_MUTED}; color: {PRIMARY}')
        else:
            ui.label(title).classes('font-bold mb-2').style(f'color: {PRIMARY}')

        columns = [
            {'name': 'ts', 'label': 'Time', 'field': 'timestamp', 'align': 'left', 'sortable': True},
            {'name': 'lvl', 'label': 'Level', 'field': 'level', 'align': 'left'},
            {'name': 'msg', 'label': 'Message', 'field': 'message', 'align': 'left',
             'classes': 'text-wrap font-mono text-xs' if full_height else ''},
        ]
        table_classes = 'w-full border-none' + (' h-[600px]' if full_height else '')
        table = ui.table(columns=columns, rows=[]).classes(table_classes)
        if full_height:
            table.props('virtual-scroll')

        def update():
            condition = Event.message.startswith(prefix)
            if target:
                condition &= (Event.target == target)
            query = (Event.select()
                     .where(condition)
                     .order_by(Event.timestamp.desc())
                     .limit(limit))
            table.rows = [
                {
                    'timestamp': e.timestamp.isoformat(sep=' ', timespec='seconds'),
                    'level': e.level,
                    # The prefix is how rows are found; repeating it on every
                    # line of a single plugin's own table is just noise.
                    'message': e.message[len(prefix):] if e.message.startswith(prefix)
                               else e.message,
                }
                for e in query
            ]
            table.update()

        update()
        safe_timer(5.0, update)
        return table


def history_chart(title: str, collector: str, metric_name: str, limit: int = 30):
    """A standardized EChart for displaying metric history over time."""
    from vigil.core.data.database import Metric
    with card('w-full h-80 mb-4'):
        ui.label(title.upper()).classes(f'{LABEL_CLASS} mb-2')
        chart = ui.echart({
            'tooltip': {'trigger': 'axis'},
            'xAxis': {'type': 'category', 'data': []},
            'yAxis': {'type': 'value', 'splitLine': {'show': False}},
            'series': [{
                'data': [],
                'type': 'line',
                'smooth': True,
                'color': PRIMARY,
                'areaStyle': {'opacity': 0.1}
            }]
        }).classes('w-full h-64')

        def update():
            history = Metric.select().where(
                (Metric.collector == collector) & (Metric.metric_name == metric_name)
            ).order_by(Metric.timestamp.desc()).limit(limit)
            history = list(reversed(history))
            chart.options['xAxis']['data'] = [m.timestamp.strftime('%H:%M:%S') for m in history]
            chart.options['series'][0]['data'] = [m.value for m in history]
            chart.update()

        safe_timer(5.0, update)
        update()
        return chart

def render_host_card(target: str):
    """Renders the standard target host information card."""
    return info_card('TARGET HOST', target)

def render_status_card(collector: str, metric_name: str, title: str = 'STATUS', 
                       on_text: str = 'ACTIVE', off_text: str = 'INACTIVE', 
                       value_classes: str = VALUE_CLASS):
    """A reusable card for monitoring a binary metric state with auto-refresh."""
    from vigil.core.data.database import Metric
    
    lbl = info_card(title, 'Checking...', value_classes=value_classes)
    
    def update():
        last = Metric.select().where(
            (Metric.collector == collector) & (Metric.metric_name == metric_name)
        ).order_by(Metric.timestamp.desc()).first()
        if last:
            is_on = last.value > 0.5
            lbl.text = on_text if is_on else off_text
            lbl.style(f"color: {STATUS_COLORS['online'] if is_on else STATUS_COLORS['failed']}")
    safe_timer(2.0, update)
    return lbl
