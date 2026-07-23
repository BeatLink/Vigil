from nicegui import ui
from vigil.core.data.events import bus
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


def safe_timer(interval: float, callback, defer_first: bool = False):
    """
    Create a periodic timer that goes quiet once its page is torn down.

    Plain `ui.timer` keeps firing against deleted elements after a client
    disconnects, raising on every tick. This variant cancels itself instead —
    see _SafeTimer. The callback is additionally guarded so a teardown race
    mid-callback is swallowed rather than logged.

    `defer_first=True` skips ui.timer's immediate first call (which otherwise
    runs inline during widget construction, before the page has painted) and
    instead fires it on the next event-loop tick, so navigation/clicks aren't
    stuck behind that first DB query.
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

    timer = _SafeTimer(interval, _wrapped, immediate=not defer_first)
    return timer


# In the web process, DataBus never emits (writes happen only in the
# collector — see DataBus.polling_mode). Widgets instead poll on this
# interval, chosen to match the collector's own write-batch window
# (ConfigFileManager.DEFAULT_WRITE_BATCH_SECONDS): polling faster couldn't
# see fresher data anyway, since that's the floor on when a write actually
# lands, and polling much slower would add its own visible lag on top.
POLL_FALLBACK_SECONDS = 1.0


def on_data_event(event, element, callback, run_now: bool = True):
    """
    Re-run `callback` whenever DataBus fires `event`, instead of polling it
    on a fixed interval.

    `event` is a single event name, or an iterable of several — some widgets
    read more than one data type in one callback (e.g. a plugin card that
    displays both a Setting and a Metric) and need to refresh on any of them
    without running `callback` more than once per actual firing or paying
    for `run_now`'s initial call more than once.

    `element` is the widget `callback` updates (a table, chart, label, ...) —
    used purely to detect when it's gone, the same way _SafeTimer checks its
    own `self.id not in self.client.elements`. This helper isn't itself a
    NiceGUI Element the way a timer is, so it has no such attachment of its
    own to check; the caller's widget stands in for it.

    `run_now=True` (default) calls it once immediately for the widget's
    initial paint, same as safe_timer(..., defer_first=False) does today.

    In the web process (`bus.polling_mode`), there is no writer thread in
    this process to ever call `bus.emit()` — writes happen only in the
    collector. Falls back to a `safe_timer` polling `callback` every
    `POLL_FALLBACK_SECONDS`; every call site (all 48 plugins' render_ui()
    methods) is unchanged, since this is the only place the distinction is
    made.

    Unlike safe_timer, a DataBus subscription has no natural next tick to
    detect its own detachment on and cancel itself — DataBus may not fire
    `event` again for a long time (or ever) after the widget is gone, and
    until it does, the callback (and everything its closure holds) stays
    registered, leaking. So detachment is checked from two directions:
      - each firing checks whether `element` has since been detached and
        unsubscribes if so — handles same-client navigation
        (main_container.clear()), but only runs when another event fires;
      - client.on_disconnect() unsubscribes immediately on a full browser
        disconnect, which otherwise might never trigger another event.
    (safe_timer's own teardown logic already covers the polling-mode path,
    so only the DataBus path needs this handling.)
    """
    if bus.polling_mode:
        safe_timer(POLL_FALLBACK_SECONDS, callback, defer_first=not run_now)
        return

    events = [event] if isinstance(event, str) else list(event)

    def _detached() -> bool:
        if getattr(element, 'is_deleted', False):
            return True
        try:
            return element.id not in element.client.elements
        except Exception:
            return True

    offs: list = []

    def _unsubscribe():
        for off in offs:
            off()

    def _wrapped():
        if _detached():
            _unsubscribe()
            return
        try:
            callback()
        except RuntimeError as e:
            if 'parent slot' not in str(e) and 'has been deleted' not in str(e):
                raise
            _unsubscribe()

    offs.extend(bus.on(ev, _wrapped) for ev in events)
    element.client.on_disconnect(_unsubscribe)
    if run_now:
        _wrapped()


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

def metric_table(page, collector: str, title: str = 'Monitor Metrics', limit: int = 15):
    """
    A standardized table for displaying recent metrics for a specific collector.

    `page` is the plugin's PluginPage (see ui/model.py) — its rows refresh on
    the page's single shared timer via page.on_refresh(), rather than this
    widget running its own independent timer. Table rows can't use real
    NiceGUI binding: ui.table.rows has no on-change hook to push to the
    browser (verified against NiceGUI's Table source), so this still needs
    an explicit `.rows = ...; .update()` step — just driven by the shared
    tick instead of a per-widget one.
    """
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
            table.rows = [m.__data__ for m in query]
            table.update()

        page.on_refresh(update)
        update()
        return table

def log_table(page, target: str, filter_prefix: str = '', title: str = 'Recent Logs',
             limit: int = 15, full_height: bool = False):
    """
    A standardized table for displaying persisted log lines, optionally filling
    available height.

    Reads from the LogLine table (deduplicated, retained log storage). When
    `filter_prefix` is given it scopes to that source (the plugin name); with no
    prefix it shows every source for the target. See metric_table's docstring
    for why this refreshes via `page` rather than binding table.rows directly.
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

        page.on_refresh(update_logs)
        update_logs()
        return table

def event_table(page, plugin_name: str, plugin_id: str = '', target: str = '',
                title: str = 'Recent Events', limit: int = 100,
                full_height: bool = False):
    """
    A table of a plugin's own Event messages — everything it wrote via
    `db_logger.write`.

    Distinct from `log_table`, which reads LogLine: that table holds raw log
    output pulled off a target (journald and friends), deduplicated so a
    re-fetched line is stored once. Plugins that don't collect logs from a
    target — borg, for instance — have no LogLine rows at all, so a log_table
    on their page renders permanently empty while their Events pile up unseen.

    Rows are selected by `plugin_id`, the monitor's unique id, not by the
    "[Display Name] " prefix the logger writes: names repeat across groups
    (several monitors are called "On Disk"), so a prefix match pulls in other
    monitors' events. The prefix is still stripped for display. See
    metric_table's docstring for why this refreshes via `page`.
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
            if plugin_id:
                condition = (Event.source_id == plugin_id)
            else:
                # No id given (or rows predating source_id): fall back to the
                # message prefix, narrowed by target. Ambiguous where two
                # monitors share a name on one host, but better than nothing.
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

        page.on_refresh(update)
        update()
        return table


def history_chart(page, title: str, collector: str, metric_name: str, limit: int = 30):
    """
    A standardized EChart for displaying metric history over time. See
    metric_table's docstring for why this refreshes via `page` — ui.echart's
    `options` has the same no-on-change-hook limitation as table.rows.
    """
    from vigil.core.data.database import Metric
    with card('w-full h-80 mb-4 p-2', padding=False):
        ui.label(title.upper()).classes(f'{LABEL_CLASS} mb-1')
        chart = ui.echart({
            'tooltip': {'trigger': 'axis'},
            'grid': {'left': 4, 'right': 8, 'top': 8, 'bottom': 4, 'containLabel': True},
            'xAxis': {'type': 'category', 'data': []},
            'yAxis': {'type': 'value', 'splitLine': {'show': False}},
            'series': [{
                'data': [],
                'type': 'line',
                'smooth': True,
                'color': PRIMARY,
                'areaStyle': {'opacity': 0.1}
            }]
        }).classes('w-full h-72')

        def update():
            history = Metric.select().where(
                (Metric.collector == collector) & (Metric.metric_name == metric_name)
            ).order_by(Metric.timestamp.desc()).limit(limit)
            history = list(reversed(history))
            chart.options['xAxis']['data'] = [m.timestamp.strftime('%H:%M:%S') for m in history]
            chart.options['series'][0]['data'] = [m.value for m in history]
            chart.update()

        page.on_refresh(update)
        update()
        return chart

def render_host_card(target: str):
    """Renders the standard target host information card."""
    return info_card('TARGET HOST', target)

def render_status_card(page, collector: str, metric_name: str, title: str = 'STATUS',
                       on_text: str = 'ACTIVE', off_text: str = 'INACTIVE',
                       value_classes: str = VALUE_CLASS):
    """
    A reusable card for monitoring a binary metric state with auto-refresh.

    Text uses real NiceGUI binding (label.text is itself a BindableProperty
    with an on_change hook that pushes to the browser — see ui/model.py's
    docstring) against `page.model.metrics[metric_name]`, transformed
    on/off by `backward`. Color has no such bindable hook on a plain label,
    so it's still set via an explicit page.on_refresh() callback, same as
    the row-based widgets.
    """
    lbl = info_card(title, 'Checking...', value_classes=value_classes)
    page.track_metric(metric_name)

    def _on_off_text(value):
        if value is None:
            return 'Checking...'
        return on_text if value > 0.5 else off_text

    lbl.bind_text_from(page.model, ('metrics', metric_name), backward=_on_off_text)

    def update_color():
        value = page.model.metrics.get(metric_name)
        if value is not None:
            is_on = value > 0.5
            lbl.style(f"color: {STATUS_COLORS['online'] if is_on else STATUS_COLORS['failed']}")

    page.on_refresh(update_color)
    return lbl
