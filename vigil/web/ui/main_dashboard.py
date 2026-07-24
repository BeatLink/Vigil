import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional
from nicegui import app, ui
from vigil.core.data.database import Setting
from .theme import STATUS_COLORS, BACKGROUND_MUTED, PRIMARY, BACKGROUND, TEXT, TEXT_MUTED
from .components import action_chip, card, section_title, safe_timer, on_data_event, offload, refresh_rows

_ICON = Path(__file__).parent.parent.parent / 'static' / 'icon.svg'

_navigation_state = {'switch_func': None}

def navigate_to(plugin_instance: Any):
    if _navigation_state['switch_func']:
        if plugin_instance is None:
            _navigation_state['switch_func']('overview')
        else:
            _navigation_state['switch_func']('plugin', plugin_instance)


def init_gui(engine: Any, port: int = 8080):
    from vigil.core.data.events import bus
    bus.polling_mode = True

    app.add_static_file(local_file=_ICON, url_path='/icon.svg')

    from vigil.web.auth import register_auth
    register_auth(app, engine.config_loader.auth_settings)

    try:
        from vigil.web.api import register_api
        register_api(app, engine)
    except Exception as e:
        logging.error(f"Failed to register REST API: {e}")

    @ui.page('/')
    def index_page():
      ui.query('body').style(f'background-color: {BACKGROUND_MUTED}')

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
          engine.db.set_setting('drawer_width', str(width))

      drawer_width = _load_drawer_width()

      with ui.left_drawer(value=True).classes('p-0 shadow-lg').props(f'width={drawer_width}').style(f'background-color: {BACKGROUND}') as left_drawer:
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

          tree = ui.tree(nodes=build_tree_nodes(engine.plugins), on_select=handle_select).props('').classes('w-full px-6 text-lg').style(f'color: {TEXT}')

          tree.add_slot('default-header', f'''
              <span class="flex items-center gap-2" style="color: {TEXT}">
                  <q-icon name="circle" :style="{{ color: props.node.color }}" size="12px" />
                  {{{{ props.node.label }}}}
              </span>
          ''')
        
          async def refresh_tree():
              new_nodes = await offload(build_tree_nodes)(engine.plugins)
              if new_nodes != tree._props['nodes']:
                  tree._props['nodes'] = new_nodes
                  tree.update()

          on_data_event('status', tree, refresh_tree, run_now=False)

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

      main_container = ui.column().classes('w-full p-6 bg-transparent')

      def render_main():
          try:
              main_container.clear()
          except RuntimeError:
              return
          with main_container:
              if state['current_view'] == 'overview':
                  render_overview()
              elif state['current_view'] == 'events':
                  render_events()
              else:
                  render_plugin_detail(state['selected_plugin'])

      state['render_main'] = render_main

      def render_events():
          section_title('Events', 'mb-6 font-light')

          _LEVEL_COLORS = {
              'ERROR': STATUS_COLORS['failed'],
              'WARNING': STATUS_COLORS['warning'],
              'INFO': TEXT_MUTED,
          }

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
              rows = await offload(engine.db.recent_events_cached)(
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

          on_data_event('event', events_table, refresh_events, run_now=False)

      def render_overview():
          section_title('Monitors', 'mb-6 font-light')

          all_monitors = []
          def collect_leafs(plist):
              for p in plist:
                  if not p.children: all_monitors.append(p)
                  else: collect_leafs(p.children)
          collect_leafs(engine.plugins)
          plugin_by_id = {p.id: p for p in all_monitors}

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

              monitor_table.add_slot('body-cell-name', f'''
                  <q-td :props="props">
                      <span class="cursor-pointer font-medium hover:underline"
                            style="color: {PRIMARY}"
                            @click="$parent.$emit('navigate', props.row)">
                          {{{{ props.row.name }}}}
                      </span>
                  </q-td>
              ''')

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

          async def update_charts():
              statuses = await offload(engine.db.latest_statuses)()
              if statuses == _last_statuses['value']:
                  return
              _last_statuses['value'] = statuses

              status_counts, type_counts = _build_chart_counts(statuses)

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
              monitor_table.rows = _build_table_rows(statuses)
              monitor_table.update()

          on_data_event('status', status_chart, update_charts, run_now=False)

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

                  async def update_m():
                      refresh_rows(m_table, await offload(engine.db.recent_metrics_raw_cached)(limit=20))
                  on_data_event('metric', m_table, update_m)

              with card('flex-1'):
                  ui.label('Recent Events').classes('text-lg font-bold mb-2').style(f'color: {TEXT}')
                  event_columns = [
                      {'name': 'timestamp', 'label': 'Time', 'field': 'timestamp', 'align': 'left'},
                      {'name': 'level', 'label': 'Level', 'field': 'level', 'align': 'left'},
                      {'name': 'target', 'label': 'Host', 'field': 'target', 'align': 'left'},
                      {'name': 'message', 'label': 'Message', 'field': 'message', 'align': 'left'},
                  ]
                  e_table = ui.table(columns=event_columns, rows=[]).classes('w-full')

                  async def update_e():
                      refresh_rows(e_table, await offload(engine.db.recent_events_raw_cached)(limit=20))
                  on_data_event('event', e_table, update_e)

      def render_plugin_detail(plugin: Any):
          header = ui.row().classes('w-full items-center justify-between mb-6')
          with header:
              with ui.column():
                  ui.label(plugin.name).classes('text-3xl font-bold').style(f'color: {TEXT}')
              actions_row = ui.row().classes('gap-2 items-center')

          async def render_actions():
              with actions_row:
                  async def poll_now():
                      await plugin.run_cycle()
                      ui.notify(f'{plugin.name} polled', type='positive')
                  action_chip('Poll Now', on_click=poll_now, icon='refresh')

                  info = await plugin.present()
                  for action in info.get('actions', []):
                      async def do_action(aid=action['action_id']):
                          success = await plugin.on_action(aid)
                          ui.notify('Action completed successfully' if success else 'Action failed',
                                    type='positive' if success else 'negative')

                      btn_color = PRIMARY if action.get('variant') != 'danger' else STATUS_COLORS['failed']
                      action_chip(action['name'], on_click=do_action, color=btn_color, icon=action.get('icon', 'play_arrow'))

          import asyncio
          asyncio.create_task(render_actions())

          plugin.render_ui()

      render_main()

    svg = _ICON.read_text()
    ui.run(
        title='Vigil', favicon=svg[svg.index('<svg'):], port=port, reload=False, show=False,
        binding_refresh_interval=2.0,
    )
