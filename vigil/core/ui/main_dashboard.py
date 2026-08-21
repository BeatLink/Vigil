import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional
from nicegui import app, ui
from vigil.core.contracts import EngineLike
from vigil.core.database.database import Setting
from . import theme
from .theme import STATUS_COLORS, ACCENT, TEXT_SECONDARY
from .components import action_button, card, section_title, on_data_event, offload, refresh_rows

_ICON = Path(__file__).parent / 'static' / 'icon.svg'

_navigation_state = {'switch_func': None}

def navigate_to(plugin_instance: Any):
    if _navigation_state['switch_func']:
        if plugin_instance is None:
            _navigation_state['switch_func']('overview')
        else:
            _navigation_state['switch_func']('plugin', plugin_instance)


def init_gui(engine: EngineLike, port: int = 8080):
    app.on_startup(engine.run)

    app.add_static_file(local_file=_ICON, url_path='/icon.svg')

    from vigil.core.ui.auth import register_auth
    register_auth(app, engine.config_loader.auth_settings)

    try:
        from vigil.core.ui.api import register_api
        register_api(app, engine)
    except Exception as e:
        logging.error(f"Failed to register REST API: {e}")

    try:
        from vigil.core.ui.agent_endpoint import register_agent_endpoint
        register_agent_endpoint(app, engine.connectors.agents)
    except Exception as e:
        logging.error(f"Failed to register the agent endpoint: {e}")

    @ui.page('/')
    def index_page():
      theme.install()

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

      _navigation_state['switch_func'] = switch_view

      with ui.header().classes('items-center gap-2'):
          ui.button(on_click=lambda: left_drawer.toggle(), icon='menu', color=None).props('flat dense round')
          ui.image('/icon.svg').style('width: 18px; height: 18px;')
          ui.label('Vigil').classes('halon-brand')

      def _load_drawer_width() -> int:
          with Setting._meta.database.connection_context():
              try:
                  return int(Setting.get(Setting.key == 'drawer_width').value)
              except Exception:
                  return 248

      def _save_drawer_width(width: int):
          engine.db.set_setting('drawer_width', str(width))

      drawer_width = _load_drawer_width()

      with ui.left_drawer(value=True).classes('p-0').props(f'width={drawer_width}') as left_drawer:
          resize_handle = ui.element('div').classes('halon-resize-gutter').style(
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
              nav_items = {
                  'overview': ui.item('All Monitors', on_click=lambda: switch_view('overview')).props('clickable dense'),
                  'events': ui.item('Events', on_click=lambda: switch_view('events')).props('clickable dense'),
              }

          def sync_nav_selection():
              # A sidebar item is a place you are, so selection is elevation
              # (halon-item-selected), not an accent fill.
              for view, item in nav_items.items():
                  selected = state['current_view'] == view
                  item.classes(add='halon-item-selected' if selected else None,
                               remove=None if selected else 'halon-item-selected')

          state['sync_nav'] = sync_nav_selection

          ui.label('Monitors').classes('halon-sidebar-label')

          def build_tree_nodes(plugins, statuses=None):
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

          tree = ui.tree(nodes=build_tree_nodes(engine.plugins), on_select=handle_select).classes('w-full')

          tree.add_slot('default-header', '''
              <span class="flex items-center gap-2">
                  <q-icon class="halon-status-dot" name="circle"
                          :style="{ color: props.node.color }" size="8px" />
                  {{ props.node.label }}
              </span>
          ''')
        
          async def refresh_tree():
              new_nodes = await offload(build_tree_nodes)(engine.plugins)
              if new_nodes != tree._props['nodes']:
                  tree._props['nodes'] = new_nodes
                  tree.update()

          on_data_event(refresh_tree, run_now=False)

          def _load_expanded() -> list:
              with Setting._meta.database.connection_context():
                  try:
                      return json.loads(Setting.get(Setting.key == 'tree_expanded').value)
                  except Exception:
                      return []

          def _save_expanded(e):
              ids = e.args if isinstance(e.args, list) else []
              engine.db.set_setting('tree_expanded', json.dumps(ids))

          tree._props['expanded'] = _load_expanded()
          tree.update()
          tree.on('update:expanded', _save_expanded)

      main_container = ui.column().classes('w-full halon-page bg-transparent').style('min-width: 0; flex: 1 1 0;')

      def render_main():
          try:
              main_container.clear()
          except RuntimeError:
              return
          sync_nav = state.get('sync_nav')
          if sync_nav:
              sync_nav()
          with main_container:
              if state['current_view'] == 'overview':
                  render_overview()
              elif state['current_view'] == 'events':
                  render_events()
              else:
                  render_plugin_detail(state['selected_plugin'])

      state['render_main'] = render_main

      def render_events():
          section_title('Events')

          _LEVEL_COLORS = {
              'ERROR': STATUS_COLORS['failed'],
              'WARNING': STATUS_COLORS['warning'],
              'INFO': TEXT_SECONDARY,
          }

          ev_filter = {'level': None, 'target': None, 'search': None}

          with ui.row().classes('w-full gap-3 mb-4 items-end'):
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
                                      pagination=25).classes('w-full')
              events_table.add_slot('body-cell-level', '''
                  <q-td :props="props">
                      <span :style="{ color: props.row.level === 'ERROR' ? '%s'
                                          : props.row.level === 'WARNING' ? '%s' : '%s',
                                      fontWeight: 600 }">
                          {{ props.row.level }}
                      </span>
                  </q-td>
              ''' % (_LEVEL_COLORS['ERROR'], _LEVEL_COLORS['WARNING'], _LEVEL_COLORS['INFO']))

          async def refresh_events():
              rows = await offload(engine.db.recent_events)(
                  limit=500,
                  level=ev_filter['level'],
                  target=(ev_filter['target'] or None),
                  search=(ev_filter['search'] or None),
              )
              refresh_rows(events_table, rows)

          async def _on_level(e):
              ev_filter['level'] = e.value
              await refresh_events()

          async def _on_target(e):
              ev_filter['target'] = (e.value or '').strip() or None
              await refresh_events()

          async def _on_search(e):
              ev_filter['search'] = (e.value or '').strip() or None
              await refresh_events()

          level_sel.on_value_change(_on_level)
          target_in.on_value_change(_on_target)
          search_in.on_value_change(_on_search)

          on_data_event(refresh_events, run_now=False)

      def render_overview():
          section_title('Monitors')

          all_monitors = []
          def collect_leafs(plist):
              for p in plist:
                  if not p.children: all_monitors.append(p)
                  else: collect_leafs(p.children)
          collect_leafs(engine.plugins)
          plugin_by_id = {p.id: p for p in all_monitors}

          filter_state = {'field': None, 'value': None}

          with ui.row().classes('w-full gap-4 mb-6 halon-section-gap'):
              with card('flex-1 h-80 min-w-[320px]'):
                  ui.label('Monitors by status').classes('halon-label mb-2')
                  status_chart = ui.echart({
                      'tooltip': {'trigger': 'item', 'formatter': '{b}: {c} ({d}%)'},
                      'legend': {'bottom': '0', 'left': 'center', 'textStyle': {'fontSize': 12}},
                      'series': [{
                          'type': 'pie',
                          'radius': ['40%', '70%'],
                          'avoidLabelOverlap': False,
                          'cursor': 'pointer',
                          'itemStyle': {'borderRadius': 8, 'borderWidth': 2},
                          'label': {'show': False},
                          'data': []
                      }]
                  }).classes('w-full h-64')

              with card('flex-1 h-80 min-w-[320px]'):
                  ui.label('Monitors by type').classes('halon-label mb-2')
                  type_chart = ui.echart({
                      'tooltip': {'trigger': 'item', 'formatter': '{b}: {c} ({d}%)'},
                      'legend': {'bottom': '0', 'left': 'center', 'textStyle': {'fontSize': 12}},
                      'series': [{
                          'type': 'pie',
                          'radius': ['40%', '70%'],
                          'avoidLabelOverlap': False,
                          'cursor': 'pointer',
                          'itemStyle': {'borderRadius': 8, 'borderWidth': 2},
                          'label': {'show': False},
                          'data': []
                      }]
                  }).classes('w-full h-64')

          with card('w-full mb-6'):
              with ui.row().classes('w-full items-center justify-between mb-3'):
                  ui.label('All monitors').classes('halon-label')
                  with ui.row().classes('items-center gap-1') as filter_row:
                      filter_label = ui.label('').classes('halon-caption')
                      ui.button(icon='close', on_click=lambda: _clear_filter(), color=None).props('flat dense round size=sm')
              filter_row.set_visibility(False)

              monitor_columns = [
                  {'name': 'name',   'label': 'Monitor', 'field': 'name',   'align': 'left', 'sortable': True},
                  {'name': 'type',   'label': 'Type',    'field': 'type',   'align': 'left', 'sortable': True},
                  {'name': 'host',   'label': 'Host',    'field': 'host',   'align': 'left', 'sortable': True},
                  {'name': 'status', 'label': 'Status',  'field': 'status', 'align': 'left', 'sortable': True},
              ]
              monitor_table = ui.table(columns=monitor_columns, rows=[]).classes('w-full border-none')

              monitor_table.add_slot('body-cell-name', f'''
                  <q-td :props="props">
                      <span class="cursor-pointer font-medium hover:underline"
                            style="color: {ACCENT}"
                            @click="$parent.$emit('navigate', props.row)">
                          {{{{ props.row.name }}}}
                      </span>
                  </q-td>
              ''')

              monitor_table.add_slot('body-cell-status', '''
                  <q-td :props="props">
                      <span :style="{ color: props.row.status_color }" class="font-semibold halon-caption">
                          {{ props.row.status }}
                      </span>
                  </q-td>
              ''')

              def _navigate_to_row(e):
                  row_id = (e.args or {}).get('id')
                  if row_id and row_id in plugin_by_id:
                      navigate_to(plugin_by_id[row_id])
              monitor_table.on('navigate', _navigate_to_row)

          def _update_filter_ui():
              if filter_state['field']:
                  filter_label.text = f'Showing: {filter_state["value"].upper()} — click again to clear'
                  filter_row.set_visibility(True)
              else:
                  filter_row.set_visibility(False)

          async def _clear_filter():
              filter_state['field'] = None
              filter_state['value'] = None
              _update_filter_ui()
              await update_table()

          async def _set_filter(field: str, raw_value: str):
              value = raw_value.lower()
              if filter_state['field'] == field and filter_state['value'] == value:
                  filter_state['field'] = None
                  filter_state['value'] = None
              else:
                  filter_state['field'] = field
                  filter_state['value'] = value
              _update_filter_ui()
              await update_table()

          status_chart.on_point_click(lambda e: _set_filter('status', e.name))
          type_chart.on_point_click(lambda e: _set_filter('type', e.name))

          def _build_table_rows(statuses):
              rows = []
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
              return rows

          async def update_table():
              statuses = await offload(engine.db.latest_statuses)()
              monitor_table.rows = _build_table_rows(statuses)
              monitor_table.update()

          def _build_chart_counts(statuses):
              status_counts = {'online': 0, 'failed': 0, 'warning': 0, 'offline': 0}
              type_counts = {}
              for m in all_monitors:
                  st = statuses.get(m.id, 'offline')
                  status_counts[st] = status_counts.get(st, 0) + 1
                  mtype = m.config.get('type', 'unknown')
                  type_counts[mtype] = type_counts.get(mtype, 0) + 1
              return status_counts, type_counts

          _last_statuses = {'value': None}
          chart_colors = {'value': theme.current_palette()}

          def _repaint_charts(colors):
              chart_colors['value'] = colors
              for chart in (status_chart, type_chart):
                  chart.options['series'][0]['itemStyle']['borderColor'] = colors['surface']
                  chart.options['legend'].setdefault('textStyle', {})['color'] = colors['text_secondary']
              for entry, state_name in zip(
                      status_chart.options['series'][0]['data'],
                      ('online', 'failed', 'warning', 'offline')):
                  entry['itemStyle'] = {'color': colors[state_name]}
              status_chart.update()
              type_chart.update()

          theme.on_scheme_change(_repaint_charts)

          async def update_charts():
              statuses = await offload(engine.db.latest_statuses)()
              if statuses == _last_statuses['value']:
                  return
              _last_statuses['value'] = statuses

              status_counts, type_counts = _build_chart_counts(statuses)

              colors = chart_colors['value']
              status_chart.options['series'][0]['data'] = [
                  {'value': status_counts['online'],  'name': 'Online',  'itemStyle': {'color': colors['online']}},
                  {'value': status_counts['failed'],  'name': 'Failed',  'itemStyle': {'color': colors['failed']}},
                  {'value': status_counts['warning'], 'name': 'Warning', 'itemStyle': {'color': colors['warning']}},
                  {'value': status_counts['offline'], 'name': 'Offline', 'itemStyle': {'color': colors['offline']}},
              ]
              type_chart.options['series'][0]['data'] = [
                  {'value': v, 'name': k.upper()} for k, v in type_counts.items()
              ]
              status_chart.update()
              type_chart.update()
              monitor_table.rows = _build_table_rows(statuses)
              monitor_table.update()

          on_data_event(update_charts, run_now=False)

          with ui.row().classes('w-full gap-4'):
              with card('flex-1 min-w-[320px]'):
                  ui.label('Recent system metrics').classes('halon-label mb-2')
                  metric_columns = [
                      {'name': 'timestamp', 'label': 'Time', 'field': 'timestamp', 'align': 'left'},
                      {'name': 'target', 'label': 'Host', 'field': 'target', 'align': 'left'},
                      {'name': 'collector', 'label': 'Plugin', 'field': 'collector', 'align': 'left'},
                      {'name': 'metric_name', 'label': 'Metric', 'field': 'metric_name', 'align': 'left'},
                      {'name': 'value', 'label': 'Value', 'field': 'value', 'align': 'left'},
                  ]
                  m_table = ui.table(columns=metric_columns, rows=[]).classes('w-full')

                  async def update_m():
                      refresh_rows(m_table, await offload(engine.db.recent_metrics_raw)(limit=20))
                  on_data_event(update_m)

              with card('flex-1 min-w-[320px]'):
                  ui.label('Recent events').classes('halon-label mb-2')
                  event_columns = [
                      {'name': 'timestamp', 'label': 'Time', 'field': 'timestamp', 'align': 'left'},
                      {'name': 'level', 'label': 'Level', 'field': 'level', 'align': 'left'},
                      {'name': 'target', 'label': 'Host', 'field': 'target', 'align': 'left'},
                      {'name': 'message', 'label': 'Message', 'field': 'message', 'align': 'left'},
                  ]
                  e_table = ui.table(columns=event_columns, rows=[]).classes('w-full')

                  async def update_e():
                      refresh_rows(e_table, await offload(engine.db.recent_events_raw)(limit=20))
                  on_data_event(update_e)

      def render_plugin_detail(plugin: Any):
          header = ui.row().classes('w-full items-center justify-between gap-4 mb-6').style('flex-wrap: wrap;')
          with header:
              with ui.column().classes('min-w-0'):
                  ui.label(plugin.name).classes('halon-title-page break-words')
              actions_row = ui.row().classes('gap-2 items-center').style('flex-wrap: wrap;')

          async def render_actions():
              with actions_row:
                  async def poll_now():
                      await plugin.run_cycle()
                      ui.notify(f'{plugin.name} polled', type='positive')
                  # The one filled button on the view; everything else is a
                  # bordered ghost so the accent keeps meaning "the action".
                  action_button('Poll Now', on_click=poll_now, icon='refresh', weight='filled')

                  info = await plugin.present()
                  for action in info.get('actions', []):
                      async def do_action(aid=action['action_id']):
                          success = await plugin.on_action(aid)
                          ui.notify('Action completed successfully' if success else 'Action failed',
                                    type='positive' if success else 'negative')

                      action_button(action['name'], on_click=do_action,
                                    icon=action.get('icon', 'play_arrow'),
                                    danger=action.get('variant') == 'danger')

          import asyncio
          asyncio.create_task(render_actions())

          plugin.render_ui()

      render_main()

    svg = _ICON.read_text()
    ui.run(
        title='Vigil', favicon=svg[svg.index('<svg'):], port=port, reload=False, show=False,
        binding_refresh_interval=2.0,
    )
