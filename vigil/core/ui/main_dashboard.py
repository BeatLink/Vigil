import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional
from nicegui import app, ui
from vigil.core.data.database import DatabaseManager as VigilDatabase, Metric, Event, StatusHistory, Setting
from .theme import STATUS_COLORS, BACKGROUND_MUTED, PRIMARY, BACKGROUND, TEXT, TEXT_MUTED
from .components import action_chip, card, section_title, safe_timer

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

    # Register HTTP Basic Auth before the routes below are defined — Starlette
    # middleware wraps the whole app regardless of registration order relative
    # to routes, but registering it early keeps intent obvious: everything
    # that follows is meant to be gated by it.
    from vigil.core.auth import register_auth
    register_auth(app, engine.config_loader.auth_settings)

    # Register the REST API + Prometheus /metrics routes on the FastAPI app.
    try:
        from vigil.core.api import register_api
        register_api(app, engine)
    except Exception as e:
        logging.error(f"Failed to register REST API: {e}")

    if engine_run_func:
        app.on_startup(engine_run_func)

    # Navigation state is created per client inside index_page(): `init_gui`
    # runs once at startup, but each browser connection builds its own element
    # tree. Sharing one dict here meant a new tab overwrote the previous tab's
    # render callback, so navigating in an older tab called .clear() on a
    # disconnected client and raised "The client this element belongs to has
    # been deleted." before the detail view (logs included) could render.

    # Build the dashboard as an explicit page route rather than relying on
    # NiceGUI's auto-index page. In NiceGUI 3.x the auto-index re-executes the
    # main script via runpy.run_path(sys.argv[0]); under the Nix wrapper,
    # sys.argv[0] is a shell script, so that parse fails with a SyntaxError and
    # every request 500s. A @ui.page function is served directly and avoids it.
    @ui.page('/')
    def index_page():
      ui.query('body').style(f'background-color: {BACKGROUND_MUTED}')

      # Per-client navigation state — see the note above init_gui's page route.
      state: Dict[str, Any] = {
          'current_view': 'overview',
          'selected_plugin': None,
      }

      def switch_view(view_type: str, plugin: Optional[Any] = None):
          state['current_view'] = view_type
          state['selected_plugin'] = plugin
          render = state.get('render_main')
          if render:
              render()

      # navigate_to() (used by plugins) drives the most recently connected
      # client. Rebinding per client keeps it pointing at a live element tree.
      _navigation_state['switch_func'] = switch_view

      with ui.header().classes('items-center p-4').style(f'background-color: {PRIMARY}; color: {BACKGROUND}'):
          ui.button(on_click=lambda: left_drawer.toggle(), icon='menu').props('flat color=white')
          ui.image('/icon.svg').style('width: 2rem; height: 2rem;')
          ui.label('Vigil').classes('text-2xl font-bold ml-2')

      def _load_drawer_width() -> int:
          with Setting._meta.database.connection_context():
              try:
                  return int(Setting.get(Setting.key == 'drawer_width').value)
              except Exception:
                  return 350

      def _save_drawer_width(width: int):
          with Setting._meta.database.connection_context():
              Setting.insert(
                  key='drawer_width', value=str(width)
              ).on_conflict_replace().execute()

      drawer_width = _load_drawer_width()

      with ui.left_drawer(value=True).classes('p-0 shadow-lg').props(f'width={drawer_width}').style(f'background-color: {BACKGROUND}') as left_drawer:
          # Drag handle on the drawer's right edge — Quasar's q-drawer has no
          # built-in resize, so width is adjusted by hand and persisted like
          # the tree's expanded state, restoring on the next page load.
          resize_handle = ui.element('div').style(
              'position: absolute; top: 0; right: 0; width: 6px; height: 100%; '
              'cursor: ew-resize; z-index: 2000;'
          )
          resize_handle.on('mousedown', js_handler=f'''
              (e) => {{
                  e.preventDefault();
                  const drawerEl = e.target.closest('.q-drawer');
                  const startX = e.clientX;
                  const startWidth = drawerEl.offsetWidth;
                  const onMove = (moveEvent) => {{
                      const newWidth = Math.min(600, Math.max(200, startWidth + (moveEvent.clientX - startX)));
                      drawerEl.style.width = newWidth + 'px';
                  }};
                  const onUp = () => {{
                      document.removeEventListener('mousemove', onMove);
                      document.removeEventListener('mouseup', onUp);
                      emitEvent('drawer_resized', drawerEl.offsetWidth);
                  }};
                  document.addEventListener('mousemove', onMove);
                  document.addEventListener('mouseup', onUp);
              }}
          ''')
          ui.on('drawer_resized', lambda e: _save_drawer_width(int(e.args)))
          with ui.list().classes('w-full').props('dense'):
              ui.item('All Monitors', on_click=lambda: switch_view('overview')).props('clickable dense').classes('text-lg font-semibold border-b py-4 px-4').style(f'color: {TEXT}')
              ui.item('Events', on_click=lambda: switch_view('events')).props('clickable dense').classes('text-lg font-semibold border-b py-4 px-4').style(f'color: {TEXT}')
            
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
          safe_timer(5.0, refresh_tree)

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
          try:
              main_container.clear()
          except RuntimeError:
              # Client disconnected (tab closed/reloaded) while a handler still
              # referenced this tree. Nothing to draw into; drop the render.
              return
          with main_container:
              if state['current_view'] == 'overview':
                  render_overview()
              elif state['current_view'] == 'events':
                  render_events()
              else:
                  render_plugin_detail(state['selected_plugin'])

      # Expose render_main to switch_view (defined above in this page scope).
      state['render_main'] = render_main

      def render_events():
          """Unified, filterable feed of every event Vigil has recorded across all
          monitors (status changes, threshold crossings, collection errors)."""
          section_title('Events', 'mb-6 font-light')

          _LEVEL_COLORS = {
              'ERROR': STATUS_COLORS['failed'],
              'WARNING': STATUS_COLORS['warning'],
              'INFO': TEXT_MUTED,
          }

          # Filter controls: level, target host, free-text search.
          ev_filter = {'level': None, 'target': None, 'search': None}

          with ui.row().classes('w-full gap-4 mb-4 items-end'):
              level_sel = ui.select(
                  {None: 'All levels', 'ERROR': 'Error', 'WARNING': 'Warning', 'INFO': 'Info'},
                  value=None, label='Level',
              ).props('outlined dense options-dense').classes('w-40')
              target_in = ui.input(label='Target').props('outlined dense clearable').classes('w-56')
              search_in = ui.input(label='Search message').props('outlined dense clearable').classes('flex-1')

          columns = [
              {'name': 'timestamp', 'label': 'Time', 'field': 'timestamp', 'align': 'left', 'sortable': True},
              {'name': 'level', 'label': 'Level', 'field': 'level', 'align': 'left', 'sortable': True},
              {'name': 'target', 'label': 'Target', 'field': 'target', 'align': 'left', 'sortable': True},
              {'name': 'message', 'label': 'Message', 'field': 'message', 'align': 'left'},
          ]
          with card('w-full'):
              events_table = ui.table(columns=columns, rows=[], row_key='timestamp',
                                      pagination=25).classes('w-full').style(f'color: {TEXT}')
              # Color the level cell by severity.
              events_table.add_slot('body-cell-level', '''
                  <q-td :props="props">
                      <span :style="{ color: props.row.level === 'ERROR' ? '%s'
                                          : props.row.level === 'WARNING' ? '%s' : '%s',
                                      fontWeight: 600 }">
                          {{ props.row.level }}
                      </span>
                  </q-td>
              ''' % (_LEVEL_COLORS['ERROR'], _LEVEL_COLORS['WARNING'], _LEVEL_COLORS['INFO']))

          def refresh_events():
              rows = engine.db.recent_events(
                  limit=500,
                  level=ev_filter['level'],
                  target=(ev_filter['target'] or None),
                  search=(ev_filter['search'] or None),
              )
              events_table.rows = rows
              events_table.update()

          def _on_level(e):
              ev_filter['level'] = e.value
              refresh_events()

          def _on_target(e):
              ev_filter['target'] = (e.value or '').strip() or None
              refresh_events()

          def _on_search(e):
              ev_filter['search'] = (e.value or '').strip() or None
              refresh_events()

          level_sel.on_value_change(_on_level)
          target_in.on_value_change(_on_target)
          search_in.on_value_change(_on_search)

          refresh_events()
          safe_timer(5.0, refresh_events)

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
              monitor_table.rows = rows
              monitor_table.update()

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
          safe_timer(10.0, update_charts)

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
                      m_table.rows = [m.__data__ for m in query]
                      m_table.update()
                  safe_timer(5.0, update_m)

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
                      e_table.rows = [e.__data__ for e in query]
                      e_table.update()
                  safe_timer(5.0, update_e)

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
