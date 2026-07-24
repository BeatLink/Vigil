import asyncio
from nicegui import ui
from vigil.core.data.events import bus
from .theme import TEXT, TEXT_MUTED, PRIMARY, ACCENT, STATUS_COLORS, BACKGROUND_MUTED, BACKGROUND


def offload(read_fn):
    async def _run(*args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: read_fn(*args, **kwargs))
    return _run


def refresh_rows(table, new_rows) -> None:
    if new_rows != table.rows:
        table.rows = new_rows
        table.update()


class _SafeTimer(ui.timer):
    def _detached(self) -> bool:
        if getattr(self, "is_deleted", False):
            return True
        try:
            return self.id not in self.client.elements
        except Exception:
            return True

    def _should_stop(self) -> bool:
        return self._detached() or super()._should_stop()


def safe_timer(interval: float, callback, defer_first: bool = False):
    from nicegui import helpers
    timer = None

    async def _wrapped():
        try:
            result = callback()
            if helpers.should_await(result):
                await result
        except RuntimeError as e:
            if 'parent slot' in str(e) or 'has been deleted' in str(e):
                if timer is not None:
                    timer.cancel()
                return
            raise

    timer = _SafeTimer(interval, _wrapped, immediate=not defer_first)
    return timer


POLL_FALLBACK_SECONDS = 1.0


def on_data_event(event, element, callback, run_now: bool = True):
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

    from nicegui import helpers
    offs: list = []

    def _unsubscribe():
        for off in offs:
            off()

    async def _wrapped():
        if _detached():
            _unsubscribe()
            return
        try:
            result = callback()
            if helpers.should_await(result):
                await result
        except RuntimeError as e:
            if 'parent slot' not in str(e) and 'has been deleted' not in str(e):
                raise
            _unsubscribe()

    offs.extend(bus.on(ev, _wrapped) for ev in events)
    element.client.on_disconnect(_unsubscribe)
    if run_now:
        _wrapped()


LABEL_CLASS = 'text-xs font-bold'
VALUE_CLASS = 'text-2xl font-bold'
SECTION_CLASS = 'text-xl font-bold'
HOVER_STYLE = 'hover:bg-blue-50 cursor-pointer'

def card(classes: str = '', padding: bool = True):
    p = 'p-4' if padding else 'p-0'
    return ui.card().classes(f'{p} shadow-sm {classes}')

def info_card(title: str, value: str = '--', value_classes: str = VALUE_CLASS, card_classes: str = 'flex-1'):
    with card(f'min-w-36 h-28 overflow-hidden items-center justify-center {card_classes}'):
        ui.label(title.upper()).classes(LABEL_CLASS).style(f'color: {TEXT_MUTED}')
        return ui.label(value).classes(f'{value_classes} w-full text-center break-words').style(f'color: {PRIMARY}')

def action_chip(text: str, on_click=None, icon: str = 'play_arrow', color: str = PRIMARY):
    return ui.chip(text, icon=icon, on_click=on_click, color=color, text_color=BACKGROUND).props('clickable')

def section_title(text: str, classes: str = ''):
    return ui.label(text).classes(f'{SECTION_CLASS} mb-4 {classes}').style(f'color: {TEXT}')

def metric_table(page, collector: str, title: str = 'Monitor Metrics', limit: int = 15):
    with card():
        ui.label(title).classes('font-bold mb-2').style(f'color: {PRIMARY}')
        table = ui.table(columns=[
            {'name': 'ts', 'label': 'Time', 'field': 'timestamp', 'align': 'left'},
            {'name': 'name', 'label': 'Metric', 'field': 'metric_name', 'align': 'left'},
            {'name': 'val', 'label': 'Value', 'field': 'value', 'align': 'left'},
        ], rows=[]).classes('w-full border-none')

        def _read():
            return page.plugin.db.collector_metrics_cached(collector, limit=limit)

        async def update():
            refresh_rows(table, await offload(_read)())

        page.on_refresh(update)
        table.rows = _read()
        table.update()
        return table

def log_table(page, target: str, filter_prefix: str = '', title: str = 'Recent Logs',
             limit: int = 15, full_height: bool = False):
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

        def _read():
            return page.plugin.db.log_lines_cached(target, filter_prefix, limit=limit)

        async def update_logs():
            refresh_rows(table, await offload(_read)())

        page.on_refresh(update_logs)
        table.rows = _read()
        table.update()
        return table

def event_table(page, plugin_name: str, plugin_id: str = '', target: str = '',
                title: str = 'Recent Events', limit: int = 100,
                full_height: bool = False):
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

        def _read():
            return page.plugin.db.plugin_events_cached(plugin_id, prefix, target, limit=limit)

        async def update():
            refresh_rows(table, await offload(_read)())

        page.on_refresh(update)
        table.rows = _read()
        table.update()
        return table


def history_chart(page, title: str, collector: str, metric_name: str, limit: int = 30):
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

        def _read():
            history = page.plugin.db.metric_history_cached(collector, metric_name, limit=limit)
            return (
                [m.timestamp.strftime('%H:%M:%S') for m in history],
                [m.value for m in history],
            )

        def _apply(data):
            x, y = data
            if x == chart.options['xAxis']['data'] and y == chart.options['series'][0]['data']:
                return
            chart.options['xAxis']['data'] = x
            chart.options['series'][0]['data'] = y
            chart.update()

        async def update():
            _apply(await offload(_read)())

        page.on_refresh(update)
        _apply(_read())
        return chart

def render_host_card(target: str):
    return info_card('TARGET HOST', target)

def render_status_card(page, collector: str, metric_name: str, title: str = 'STATUS',
                       on_text: str = 'ACTIVE', off_text: str = 'INACTIVE',
                       value_classes: str = VALUE_CLASS):
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
