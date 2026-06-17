import logging
from nicegui import app, ui
from vigil.core.data.database import DatabaseManager as VigilDatabase, Metric, Event, StatusHistory
from typing import Any, Dict, Optional
from .theme import COLOR_MAP, BG_PAGE

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

    ui.query('body').style(f'background-color: {BG_PAGE}')

    with ui.header().classes('items-center bg-primary text-white p-4'):
        ui.button(on_click=lambda: left_drawer.toggle(), icon='menu').props('flat color=white')
        ui.icon('security', size='md')
        ui.label('Vigil System Monitor').classes('text-2xl font-bold ml-2')

    with ui.left_drawer(value=True).classes('bg-slate-100 p-0 shadow-lg').props('width=300') as left_drawer:
        with ui.list().classes('w-full').props('dense'):
            ui.item('Overview', on_click=lambda: switch_view('overview')).props('clickable dense').classes('text-lg font-semibold border-b py-4 px-4')
            ui.item_label('MONITORS').classes('text-xs text-gray-500 mt-6 mb-2 px-4')

        def build_tree_nodes(plugins):
            """Recursive helper to build data structure for ui.tree."""
            with StatusHistory._meta.database.connection_context():
                nodes = []
                for p in plugins:
                    # Fetch the latest status to determine the icon color
                    latest = StatusHistory.select().where(StatusHistory.collector_id == p.id).order_by(StatusHistory.timestamp.desc()).first()
                    state = latest.state if latest else 'inactive'

                    node = {
                        'id': p.id,
                        'label': p.name,
                        'icon': 'circle',
                        'color': COLOR_MAP[state]
                    }
                    if p.children:
                        node['children'] = build_tree_nodes(p.children)
                    nodes.append(node)
                return nodes

        def find_plugin_by_id(plugins, target_id):
            """Helper to locate a plugin instance by its unique ID."""
            for p in plugins:
                if p.id == target_id:
                    return p
                found = find_plugin_by_id(p.children, target_id)
                if found:
                    return found
            return None

        def handle_select(e):
            if e.value:
                target_plugin = find_plugin_by_id(engine.plugins, e.value)
                if target_plugin:
                    switch_view('plugin', target_plugin)

        # Initialize the built-in tree component
        tree = ui.tree(nodes=build_tree_nodes(engine.plugins), on_select=handle_select).props('').classes('w-full px-6 text-lg')

        tree.add_slot('default-header', '''
            <span class="flex items-center gap-2">
                <q-icon name="circle" :style="{ color: props.node.color }" size="12px" />
                {{ props.node.label }}
            </span>
        ''')
        
        # Periodically refresh tree data (dots and nodes)
        ui.timer(5.0, lambda: setattr(tree, 'nodes', build_tree_nodes(engine.plugins)))

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