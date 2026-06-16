import logging
from nicegui import app, ui
from vigil.core.data.database import DatabaseManager as VigilDatabase, Metric, Event
from typing import Any, Dict, Optional

def init_gui(engine: Any, port: int = 8080):
    db_path = engine.db_path
    engine_run_func = engine.run

    # Initialize database connection context
    try:
        VigilDatabase(db_path)
    except Exception as e:
        logging.error(f"Could not connect to database: {e}")
        return

    if engine_run_func:
        app.on_startup(engine_run_func)

    # State management for navigation
    state = {
        'current_view': 'overview',
        'selected_plugin': None
    }

    ui.query('body').style('background-color: #f8f9fa')

    with ui.header().classes('items-center bg-primary text-white p-4'):
        ui.button(on_click=lambda: left_drawer.toggle(), icon='menu').props('flat color=white')
        ui.icon('security', size='md')
        ui.label('Vigil System Monitor').classes('text-2xl font-bold ml-2')

    with ui.left_drawer(value=True).classes('bg-slate-100 p-0 shadow-lg') as left_drawer:
        with ui.list().classes('w-full mt-4'):
            ui.item('Overview', on_click=lambda: switch_view('overview')).props('clickable').classes('text-lg font-semibold border-b')
            ui.item_label('MONITORS').classes('text-xs text-gray-500 mt-4 px-4')
            
            for plugin in engine.plugins:
                info = plugin.present()
                with ui.item(on_click=lambda p=plugin: switch_view('plugin', p)).props('clickable'):
                    with ui.item_section().props('avatar'):
                        ui.icon('sensors', color='green' if info.get('actions') else 'blue')
                    with ui.item_section():
                        ui.item_label(info['name'])
                        ui.item_label(info['target']).props('caption')

    main_container = ui.column().classes('w-full p-6 bg-transparent')

    def switch_view(view_type: str, plugin: Optional[Any] = None):
        state['current_view'] = view_type
        state['selected_plugin'] = plugin
        render_main()

    def render_main():
        main_container.clear()
        with main_container:
            if state['current_view'] == 'overview':
                render_overview()
            else:
                render_plugin_detail(state['selected_plugin'])

    def render_overview():
        ui.label('Infrastructure Overview').classes('text-2xl mb-6 font-light')
        
        with ui.card().classes('w-full p-4 mb-6'):
            ui.label('Recent System Metrics').classes('text-lg font-bold mb-2')
            metric_columns = [
                {'name': 'timestamp', 'label': 'Time', 'field': 'timestamp'},
                {'name': 'target', 'label': 'Host', 'field': 'target'},
                {'name': 'collector', 'label': 'Plugin', 'field': 'collector'},
                {'name': 'metric_name', 'label': 'Metric', 'field': 'metric_name'},
                {'name': 'value', 'label': 'Value', 'field': 'value'},
            ]
            m_table = ui.table(columns=metric_columns, rows=[]).classes('w-full')
            
            def update_m():
                query = Metric.select().order_by(Metric.timestamp.desc()).limit(20)
                m_table.rows[:] = [m.__data__ for m in query]
            ui.timer(5.0, update_m)

        with ui.card().classes('w-full p-4'):
            ui.label('Recent Events').classes('text-lg font-bold mb-2')
            event_columns = [
                {'name': 'timestamp', 'label': 'Time', 'field': 'timestamp'},
                {'name': 'level', 'label': 'Level', 'field': 'level'},
                {'name': 'target', 'label': 'Host', 'field': 'target'},
                {'name': 'message', 'label': 'Message', 'field': 'message'},
            ]
            e_table = ui.table(columns=event_columns, rows=[]).classes('w-full')
            
            def update_e():
                query = Event.select().order_by(Event.timestamp.desc()).limit(20)
                e_table.rows[:] = [e.__data__ for e in query]
            ui.timer(5.0, update_e)

    def render_plugin_detail(plugin: Any):
        info = plugin.present()
        with ui.row().classes('w-full items-center justify-between mb-6'):
            with ui.column():
                ui.label(info['name']).classes('text-3xl font-bold')
                ui.label(f"Target Host: {info['target']}").classes('text-gray-500 italic')
            
            with ui.row():
                for action in info.get('actions', []):
                    async def do_action(aid=action['action_id']):
                        success = await plugin.on_action(aid)
                        ui.notify('Action completed successfully' if success else 'Action failed', 
                                  type='positive' if success else 'negative')
                    ui.button(action['name'], on_click=do_action).props('outline rounded icon=play_arrow')

        with ui.grid(columns=2).classes('w-full gap-4'):
            # Monitor Metrics
            with ui.card().classes('p-4'):
                ui.label('Monitor Metrics').classes('font-bold mb-2')
                p_metric_table = ui.table(columns=[
                    {'name': 'ts', 'label': 'Time', 'field': 'timestamp'},
                    {'name': 'name', 'label': 'Metric', 'field': 'metric_name'},
                    {'name': 'val', 'label': 'Value', 'field': 'value'},
                ], rows=[]).classes('w-full')
                
                def update_pm():
                    query = Metric.select().where(Metric.collector == plugin.name).order_by(Metric.timestamp.desc()).limit(15)
                    p_metric_table.rows[:] = [m.__data__ for m in query]
                ui.timer(5.0, update_pm)

            # Monitor Logs/Events
            with ui.card().classes('p-4'):
                ui.label('Recent Logs').classes('font-bold mb-2')
                p_event_table = ui.table(columns=[
                    {'name': 'ts', 'label': 'Time', 'field': 'timestamp'},
                    {'name': 'lvl', 'label': 'Level', 'field': 'level'},
                    {'name': 'msg', 'label': 'Message', 'field': 'message'},
                ], rows=[]).classes('w-full')

                def update_pe():
                    # Search for logs prefixed with the plugin name or matching target
                    query = Event.select().where(
                        (Event.target == info['target']) & (Event.message.contains(f"[{plugin.name}]"))
                    ).order_by(Event.timestamp.desc()).limit(15)
                    p_event_table.rows[:] = [e.__data__ for e in query]
                ui.timer(5.0, update_pe)

    # Initial render
    render_main()

    # Run the NiceGUI app
    ui.run(title='Vigil Dashboard', port=port, reload=False, show=False)