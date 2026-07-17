import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional
from nicegui import app, ui
from vigil.core.data.database import DatabaseManager as VigilDatabase, Metric, Event, StatusHistory, Setting
from .theme import STATUS_COLORS, BACKGROUND_MUTED, PRIMARY, BACKGROUND, TEXT, TEXT_MUTED
from .components import action_chip, card, section_title

_ICON = Path(__file__).parent.parent.parent / 'static' / 'icon.svg'

# Global state/helper for cross-component navigation
_navigation_state = {'switch_func': None}

def navigate_to(plugin_instance: Any):
    """External helper to trigger dashboard navigation from within plugins."""
    if _navigation_state['switch_func']:
        if plugin_instance is None:
            _navigation_state['switch_func']('overview')
        else:
            _navigation_state['switch_func']('plugin', plugin_instance)


def init_gui(engine: Any, port: int = 8080):
    db_path = engine.db_path
    engine_run_func = engine.run

    # Initialize database connection context
    try:
        VigilDatabase(db_path)
    except Exception as e:
        logging.error(f"Could not connect to database: {e}")
        return

    app.add_static_file(local_file=_ICON, url_path='/icon.svg')

    if engine_run_func:
        app.on_startup(engine_run_func)

    # State management for navigation
    state = {
        'current_view': 'overview',
        'selected_plugin': None
    }

    def switch_view(view_type: str, plugin: Optional[Any] = None):
        state['current_view'] = view_type
        state['selected_plugin'] = plugin
        # render_main is defined inside the page function (below); it registers
        # itself in `state` so navigation from this outer scope can reach it.
        render = state.get('render_main')
        if render:
            render()

    _navigation_state['switch_func'] = switch_view

    # Build the dashboard as an explicit page route rather than relying on
    # NiceGUI's auto-index page. In NiceGUI 3.x the auto-index re-executes the
    # main script via runpy.run_path(sys.argv[0]); under the Nix wrapper,
    # sys.argv[0] is a shell script, so that parse fails with a SyntaxError and
    # every request 500s. A @ui.page function is served directly and avoids it.
    @ui.page('/')
    def index_page():
      ui.query('body').style(f'background-color: {BACKGROUND_MUTED}')

      with ui.header().classes('items-center p-4').style(f'background-color: {PRIMARY}; color: {BACKGROUND}'):
          ui.button(on_click=lambda: left_drawer.toggle(), icon='menu').props('flat color=white')
          ui.image('/icon.svg').style('width: 2rem; height: 2rem;')
          ui.label('Vigil').classes('text-2xl font-bold ml-2')

      with ui.left_drawer(value=True).classes('p-0 shadow-lg').props('width=350').style(f'background-color: {BACKGROUND}') as left_drawer:
          with ui.list().classes('w-full').props('dense'):
              ui.item('All Monitors', on_click=lambda: switch_view('overview')).props('clickable dense').classes('text-lg font-semibold border-b py-4 px-4').style(f'color: {TEXT}')
            
          def build_tree_nodes(plugins, statuses=None):
              """Recursive helper to build data structure for ui.tree."""
              # Fetch every monitor's latest status in one query, then reuse the
              # map across the whole (recursive) tree instead of querying per node.
              if statuses is None:
                  statuses = engine.db.latest_statuses()
              nodes = []
              for p in plugins:
                  state = statuses.get(p.id, 'offline')

                  node = {
                      'id': p.id,
                      'label': p.name,
                      'icon': 'circle',
                      'color': STATUS_COLORS[state]
                  }
                  if p.children:
                      node['children'] = build_tree_nodes(p.children, statuses)
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
          tree = ui.tree(nodes=build_tree_nodes(engine.plugins), on_select=handle_select).props('').classes('w-full px-6 text-lg').style(f'color: {TEXT}')

          tree.add_slot('default-header', f'''
              <span class="flex items-center gap-2" style="color: {TEXT}">
                  <q-icon name="circle" :style="{{ color: props.node.color }}" size="12px" />
                  {{{{ props.node.label }}}}
              </span>
          ''')
        
          def refresh_tree():
              tree._props['nodes'] = build_tree_nodes(engine.plugins)
              tree.update()

          # Periodically refresh tree data (dots and nodes)
          ui.timer(5.0, refresh_tree)

          # Restore previously saved expanded state
          def _load_expanded() -> list:
              with Setting._meta.database.connection_context():
                  try:
                      return json.loads(Setting.get(Setting.key == 'tree_expanded').value)
                  except Exception:
                      return []

          def _save_expanded(e):
              ids = e.args if isinstance(e.args, list) else []
              with Setting._meta.database.connection_context():
                  Setting.insert(key='tree_expanded', value=json.dumps(ids)).on_conflict_replace().execute()

          tree._props['expanded'] = _load_expanded()
          tree.update()
          tree.on('update:expanded', _save_expanded)

      main_container = ui.column().classes('w-full p-6 bg-transparent')

      def render_main():
          main_container.clear()
          with main_container:
              if state['current_view'] == 'overview':
                  render_overview()
              else:
                  render_plugin_detail(state['selected_plugin'])

      # Expose render_main to switch_view (defined in the outer scope).
      state['render_main'] = render_main

      def render_overview():
          section_title('Monitors', 'mb-6 font-light')

          # Collect all leaf monitors once — shared by charts, table, and filter logic
          all_monitors = []
          def collect_leafs(plist):
              for p in plist:
                  if not p.children: all_monitors.append(p)
                  else: collect_leafs(p.children)
          collect_leafs(engine.plugins)
          plugin_by_id = {p.id: p for p in all_monitors}

          # Active filter: {'field': 'status'|'type'|None, 'value': str|None}
          filter_state = {'field': None, 'value': None}

          with ui.row().classes('w-full gap-4 mb-6'):
              with card('flex-1 h-80'):
                  ui.label('MONITORS BY STATUS').classes('text-xs font-bold mb-2').style(f'color: {TEXT_MUTED}')
                  status_chart = ui.echart({
                      'tooltip': {'trigger': 'item', 'formatter': '{b}: {c} ({d}%)'},
                      'legend': {'bottom': '0', 'left': 'center', 'textStyle': {'fontSize': 10}},
                      'series': [{
                          'type': 'pie',
                          'radius': ['40%', '70%'],
                          'avoidLabelOverlap': False,
                          'cursor': 'pointer',
                          'itemStyle': {'borderRadius': 10, 'borderColor': '#fff', 'borderWidth': 2},
                          'label': {'show': False},
                          'data': []
                      }]
                  }).classes('w-full h-64')

              with card('flex-1 h-80'):
                  ui.label('MONITORS BY TYPE').classes('text-xs font-bold mb-2').style(f'color: {TEXT_MUTED}')
                  type_chart = ui.echart({
                      'tooltip': {'trigger': 'item', 'formatter': '{b}: {c} ({d}%)'},
                      'legend': {'bottom': '0', 'left': 'center', 'textStyle': {'fontSize': 10}},
                      'series': [{
                          'type': 'pie',
                          'radius': ['40%', '70%'],
                          'avoidLabelOverlap': False,
                          'cursor': 'pointer',
                          'itemStyle': {'borderRadius': 10, 'borderColor': '#fff', 'borderWidth': 2},
                          'label': {'show': False},
                          'data': []
                      }]
                  }).classes('w-full h-64')

          # Monitors table
          with card('w-full mb-6'):
              with ui.row().classes('w-full items-center justify-between mb-3'):
                  ui.label('ALL MONITORS').classes('text-xs font-bold').style(f'color: {TEXT_MUTED}')
                  with ui.row().classes('items-center gap-1') as filter_row:
                      filter_label = ui.label('').classes('text-xs italic').style(f'color: {TEXT_MUTED}')
                      ui.button(icon='close', on_click=lambda: _clear_filter()).props('flat dense round size=xs').style(f'color: {TEXT_MUTED}')
              filter_row.set_visibility(False)

              monitor_columns = [
                  {'name': 'name',   'label': 'Monitor', 'field': 'name',   'align': 'left', 'sortable': True},
                  {'name': 'type',   'label': 'Type',    'field': 'type',   'align': 'left', 'sortable': True},
                  {'name': 'host',   'label': 'Host',    'field': 'host',   'align': 'left', 'sortable': True},
                  {'name': 'status', 'label': 'Status',  'field': 'status', 'align': 'left', 'sortable': True},
              ]
              monitor_table = ui.table(columns=monitor_columns, rows=[]).classes('w-full border-none')

              # Name column: clickable link that navigates to the monitor's detail page
              monitor_table.add_slot('body-cell-name', f'''
                  <q-td :props="props">
                      <span class="cursor-pointer font-medium hover:underline"
                            style="color: {PRIMARY}"
                            @click="$parent.$emit('navigate', props.row)">
                          {{{{ props.row.name }}}}
                      </span>
                  </q-td>
              ''')

              # Status column: color-coded text
              monitor_table.add_slot('body-cell-status', '''
                  <q-td :props="props">
                      <span :style="{ color: props.row.status_color }" class="font-semibold text-xs">
                          {{ props.row.status }}
                      </span>
                  </q-td>
              ''')

              def _navigate_to_row(e):
                  row_id = (e.args or {}).get('id')
                  if row_id and row_id in plugin_by_id:
                      navigate_to(plugin_by_id[row_id])
              monitor_table.on('navigate', _navigate_to_row)

          # -- Update functions ------------------------------------------------

          def _update_filter_ui():
              if filter_state['field']:
                  filter_label.text = f'Showing: {filter_state["value"].upper()} — click again to clear'
                  filter_row.set_visibility(True)
              else:
                  filter_row.set_visibility(False)

          def _clear_filter():
              filter_state['field'] = None
              filter_state['value'] = None
              _update_filter_ui()
              update_table()

          def _set_filter(field: str, raw_value: str):
              value = raw_value.lower()
              if filter_state['field'] == field and filter_state['value'] == value:
                  filter_state['field'] = None
                  filter_state['value'] = None
              else:
                  filter_state['field'] = field
                  filter_state['value'] = value
              _update_filter_ui()
              update_table()

          status_chart.on_point_click(lambda e: _set_filter('status', e.name))
          type_chart.on_point_click(lambda e: _set_filter('type', e.name))

          def update_table():
              rows = []
              statuses = engine.db.latest_statuses()
              for m in all_monitors:
                  st = statuses.get(m.id, 'offline')
                  mtype = m.config.get('type', 'unknown')

                  if filter_state['field'] == 'status' and st != filter_state['value']:
                      continue
                  if filter_state['field'] == 'type' and mtype != filter_state['value']:
                      continue

                  rows.append({
                      'id': m.id,
                      'name': m.name,
                      'type': mtype.upper(),
                      'host': m.target,
                      'status': st.upper(),
                      'status_color': STATUS_COLORS.get(st, STATUS_COLORS['offline']),
                  })
              monitor_table.rows[:] = rows

          def update_charts():
              status_counts = {'online': 0, 'failed': 0, 'warning': 0, 'offline': 0}
              type_counts = {}
              statuses = engine.db.latest_statuses()
              for m in all_monitors:
                  st = statuses.get(m.id, 'offline')
                  status_counts[st] = status_counts.get(st, 0) + 1
                  mtype = m.config.get('type', 'unknown')
                  type_counts[mtype] = type_counts.get(mtype, 0) + 1

              status_chart.options['series'][0]['data'] = [
                  {'value': status_counts['online'],  'name': 'Online',  'itemStyle': {'color': STATUS_COLORS['online']}},
                  {'value': status_counts['failed'],  'name': 'Failed',  'itemStyle': {'color': STATUS_COLORS['failed']}},
                  {'value': status_counts['warning'], 'name': 'Warning', 'itemStyle': {'color': STATUS_COLORS['warning']}},
                  {'value': status_counts['offline'], 'name': 'Offline', 'itemStyle': {'color': STATUS_COLORS['offline']}},
              ]
              type_chart.options['series'][0]['data'] = [
                  {'value': v, 'name': k.upper()} for k, v in type_counts.items()
              ]
              status_chart.update()
              type_chart.update()
              update_table()

          update_charts()
          ui.timer(10.0, update_charts)

          with ui.row().classes('w-full gap-4'):
              with card('flex-1'):
                  ui.label('Recent System Metrics').classes('text-lg font-bold mb-2').style(f'color: {TEXT}')
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

              with card('flex-1'):
                  ui.label('Recent Events').classes('text-lg font-bold mb-2').style(f'color: {TEXT}')
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
                  ui.label(info['name']).classes('text-3xl font-bold').style(f'color: {TEXT}')

              with ui.row().classes('gap-2 items-center'):
                  async def poll_now():
                      await plugin.run_cycle()
                      ui.notify(f'{info["name"]} polled', type='positive')
                  action_chip('Poll Now', on_click=poll_now, icon='refresh')

                  for action in info.get('actions', []):
                      async def do_action(aid=action['action_id']):
                          success = await plugin.on_action(aid)
                          ui.notify('Action completed successfully' if success else 'Action failed',
                                    type='positive' if success else 'negative')

                      btn_color = PRIMARY if action.get('variant') != 'danger' else STATUS_COLORS['failed']
                      action_chip(action['name'], on_click=do_action, color=btn_color, icon=action.get('icon', 'play_arrow'))

          # Delegate specific UI rendering to the plugin instance
          plugin.render_ui()

      # Initial render
      render_main()

    # Run the NiceGUI app
    svg = _ICON.read_text()
    ui.run(title='Vigil', favicon=svg[svg.index('<svg'):], port=port, reload=False, show=False)
