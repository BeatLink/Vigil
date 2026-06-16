import logging
from nicegui import app, ui
from vigil.core.database import VigilDatabase, Metric, Event

def init_gui(db_path: str, port: int = 8080, engine_run_func=None):
    # Initialize database connection context
    try:
        VigilDatabase(db_path)
    except Exception as e:
        logging.error(f"Could not connect to database: {e}")
        return

    # If an engine loop is provided, schedule it in the background
    if engine_run_func:
        app.on_startup(engine_run_func)

    ui.query('body').style('background-color: #f8f9fa')

    with ui.header().classes('items-center bg-primary text-white p-4'):
        ui.icon('security', size='md')
        ui.label('Vigil System Monitor').classes('text-2xl font-bold ml-2')
        ui.space()
        ui.label('Lightweight Monitoring').classes('text-sm italic opacity-80')

    with ui.tabs().classes('w-full') as tabs:
        metrics_tab = ui.tab('METRICS', icon='show_chart')
        events_tab = ui.tab('EVENTS', icon='list')

    with ui.tab_panels(tabs, value=metrics_tab).classes('w-full bg-transparent p-4'):
        # Metrics Panel
        with ui.tab_panel(metrics_tab):
            ui.label('Recent System Metrics').classes('text-xl mb-4')
            
            metric_columns = [
                {'name': 'timestamp', 'label': 'Time', 'field': 'timestamp', 'sortable': True},
                {'name': 'target', 'label': 'Host', 'field': 'target', 'sortable': True},
                {'name': 'collector', 'label': 'Collector', 'field': 'collector'},
                {'name': 'metric_name', 'label': 'Metric', 'field': 'metric_name'},
                {'name': 'value', 'label': 'Value', 'field': 'value'},
            ]
            metric_table = ui.table(columns=metric_columns, rows=[], row_key='id').classes('w-full shadow-lg')

            def refresh_metrics():
                # Query the last 50 metrics
                query = Metric.select().order_by(Metric.timestamp.desc()).limit(50)
                metric_table.rows[:] = [m.__data__ for m in query]

            ui.timer(5.0, refresh_metrics) # Auto-refresh every 5 seconds

        # Events Panel
        with ui.tab_panel(events_tab):
            ui.label('System Events & Logs').classes('text-xl mb-4')

            event_columns = [
                {'name': 'timestamp', 'label': 'Time', 'field': 'timestamp', 'sortable': True},
                {'name': 'level', 'label': 'Level', 'field': 'level'},
                {'name': 'target', 'label': 'Host', 'field': 'target'},
                {'name': 'message', 'label': 'Message', 'field': 'message', 'classes': 'text-wrap'},
            ]
            event_table = ui.table(columns=event_columns, rows=[], row_key='id').classes('w-full shadow-lg')

            def refresh_events():
                # Query the last 50 events
                query = Event.select().order_by(Event.timestamp.desc()).limit(50)
                event_table.rows[:] = [e.__data__ for e in query]

            ui.timer(5.0, refresh_events)

    # Run the NiceGUI app
    ui.run(title='Vigil Dashboard', port=port, reload=False, show=False)