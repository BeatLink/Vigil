import logging
from nicegui import app, ui
from vigil.core.data.database import DatabaseManager as VigilDatabase, Metric, Event, StatusHistory
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

    with ui.left_drawer(value=True).classes('bg-slate-100 p-0 shadow-lg').props('width=400') as left_drawer:
        with ui.list().classes('w-full mt-4'):
            ui.item('Overview', on_click=lambda: switch_view('overview')).props('clickable').classes('text-lg font-semibold border-b')
            ui.item_label('MONITORS').classes('text-xs text-gray-500 mt-4 px-4')
            
            COLOR_MAP = {
                'success': '#22c55e',
                'warning': '#f59e0b',
                'fail': '#ef4444',
                'inactive': '#94a3b8'
            }

            def render_sidebar_tree(plugins, level=0):
                for plugin in plugins:
                    info = plugin.present()
                    is_group = plugin.config.get('type') == 'group'
                    
                    if is_group:
                        # Only toggle expansion on arrow click; clicking the header title navigates to the group dashboard
                        with ui.expansion().classes('w-full').props(f'expand-icon-toggle header-class="font-medium ml-{level*2}"') as exp:
                            with exp.add_slot('header'):
                                with ui.row().classes('items-center w-full h-full cursor-pointer').on('click', lambda p=plugin: switch_view('plugin', p)):
                                    ui.icon('folder').classes('mr-2')
                                    ui.label(info['name']).classes('flex-grow text-left truncate')
                            
                            render_sidebar_tree(plugin.children, level + 1)
                    else:
                        with ui.item(on_click=lambda p=plugin: switch_view('plugin', p)).props('clickable').classes(f'ml-{level*2} no-wrap items-center'):
                            with ui.item_section().props('side'):
                                ui.icon('sensors', color='green' if info.get('actions') else 'blue', size='sm')
                            
                            with ui.item_section():
                                ui.item_label(info['name']).classes('text-sm text-left truncate')

                            # Status dots
                            with ui.item_section().props('side').classes('self-center'):
                                with ui.row().classes('gap-1 items-center no-wrap'):
                                    def get_dots(pid=plugin.id):
                                        history = StatusHistory.select().where(StatusHistory.collector_id == pid).order_by(StatusHistory.timestamp.desc()).limit(15)
                                        return reversed([h.state for h in history])
                                    
                                    states = list(get_dots())
                                    for state in states:
                                        ui.label().classes('w-2 h-2 rounded-full').style(f'background-color: {COLOR_MAP.get(state, "#ccc")}')
                                    if not states:
                                        ui.label('No data').classes('text-[10px] text-gray-400')
            
            render_sidebar_tree(engine.plugins)

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
                {'name': 'timestamp', 'label': 'Time', 'field': 'timestamp', 'align': 'left'},
                {'name': 'target', 'label': 'Host', 'field': 'target', 'align': 'left'},
                {'name': 'collector', 'label': 'Plugin', 'field': 'collector', 'align': 'left'},
                {'name': 'metric_name', 'label': 'Metric', 'field': 'metric_name', 'align': 'left'},
                {'name': 'value', 'label': 'Value', 'field': 'value', 'align': 'left'},
            ]
            m_table = ui.table(columns=metric_columns, rows=[]).classes('w-full')
            
            def update_m():
                query = Metric.select().order_by(Metric.timestamp.desc()).limit(20)
                m_table.rows[:] = [m.__data__ for m in query]
            ui.timer(5.0, update_m)

        with ui.card().classes('w-full p-4'):
            ui.label('Recent Events').classes('text-lg font-bold mb-2')
            event_columns = [
                {'name': 'timestamp', 'label': 'Time', 'field': 'timestamp', 'align': 'left'},
                {'name': 'level', 'label': 'Level', 'field': 'level', 'align': 'left'},
                {'name': 'target', 'label': 'Host', 'field': 'target', 'align': 'left'},
                {'name': 'message', 'label': 'Message', 'field': 'message', 'align': 'left'},
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
            
            with ui.row():
                for action in info.get('actions', []):
                    async def do_action(aid=action['action_id']):
                        success = await plugin.on_action(aid)
                        ui.notify('Action completed successfully' if success else 'Action failed', 
                                  type='positive' if success else 'negative')
                    ui.button(action['name'], on_click=do_action).props('outline rounded icon=play_arrow')

        # Delegate specific UI rendering to the plugin instance
        plugin.render_ui()

    # Initial render
    render_main()

    # Run the NiceGUI app
    ui.run(title='Vigil Dashboard', port=port, reload=False, show=False)