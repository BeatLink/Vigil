from nicegui import ui
from .theme import TEXT, TEXT_MUTED, PRIMARY, STATUS_COLORS, BACKGROUND_MUTED

# Standardized UI Sizing Constants
LABEL_CLASS = 'text-xs font-bold'
VALUE_CLASS = 'text-4xl font-black'
SECTION_CLASS = 'text-xl font-bold'
HOVER_STYLE = 'hover:bg-blue-50 cursor-pointer'

def card(classes: str = '', padding: bool = True):
    """A standard container with consistent padding and shadow."""
    p = 'p-4' if padding else 'p-0'
    return ui.card().classes(f'{p} shadow-sm {classes}')

def info_card(title: str, value: str = '--', value_classes: str = VALUE_CLASS, card_classes: str = 'flex-1'):
    """A card component for displaying a label and a large value."""
    with card(f'{card_classes} items-center justify-center'):
        ui.label(title.upper()).classes(LABEL_CLASS).style(f'color: {TEXT_MUTED}')
        return ui.label(value).classes(value_classes).style(f'color: {TEXT}')

def action_button(text: str, on_click=None, icon: str = 'play_arrow'):
    """A standardized button for control actions."""
    return ui.button(text, on_click=on_click).props(f'outline rounded icon={icon}')

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
            table.rows[:] = [m.__data__ for m in query]

        ui.timer(5.0, update)
        return table

def log_table(target: str, filter_prefix: str = '', title: str = 'Recent Logs', limit: int = 15, full_height: bool = False):
    """A standardized table for displaying log events, optionally filling available height."""
    from vigil.core.data.database import Event
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
            condition = (Event.target == target)
            if filter_prefix:
                condition &= (Event.message.contains(f"[{filter_prefix}]"))
            query = Event.select().where(condition).order_by(Event.timestamp.desc()).limit(limit)
            table.rows[:] = [e.__data__ for e in query]

        ui.timer(5.0, update_logs)
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

        ui.timer(5.0, update)
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
    ui.timer(2.0, update)
    return lbl