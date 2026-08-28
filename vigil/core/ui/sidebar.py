"""The dashboard sidebar: resizable drawer, nav items and the monitor tree."""

import json
from typing import Any, Callable, Dict
from nicegui import ui
from vigil.core.contracts import EngineLike
from .theme import STATUS_COLORS
from .components import on_data_event, offload
from . import nicegui_compat as ng

# Tracks the drag on the gutter and emits 'drawer_resized' with the final width in pixels.
DRAWER_RESIZE_JS = '''
    (e) => {
        e.preventDefault();
        const drawerEl = e.target.closest('.q-drawer');
        const startX = e.clientX;
        const startWidth = drawerEl.offsetWidth;
        const onMove = (moveEvent) => {
            const newWidth = Math.min(600, Math.max(200, startWidth + (moveEvent.clientX - startX)));
            drawerEl.style.width = newWidth + 'px';
        };
        const onUp = () => {
            document.removeEventListener('mousemove', onMove);
            document.removeEventListener('mouseup', onUp);
            emitEvent('drawer_resized', drawerEl.offsetWidth);
        };
        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
    }
'''

_TREE_HEADER_SLOT = '''
    <span class="flex items-center gap-2">
        <q-icon class="halon-status-dot" name="circle"
                :style="{ color: props.node.color }" size="8px" />
        {{ props.node.label }}
    </span>
'''


def _load_drawer_width(engine: EngineLike) -> int:
    """Reads the persisted drawer width, falling back to the default."""
    try:
        return int(engine.db.get_setting('drawer_width') or 248)
    except ValueError:
        return 248


def _save_drawer_width(engine: EngineLike, width: int):
    """Persists the drawer width chosen by dragging the gutter."""
    engine.db.set_setting('drawer_width', str(width))


def _load_expanded(engine: EngineLike) -> list:
    """Reads the persisted set of expanded tree node ids."""
    try:
        return json.loads(engine.db.get_setting('tree_expanded') or '[]')
    except ValueError:
        return []


def build_tree_nodes(engine: EngineLike, plugins, statuses=None) -> list:
    """Builds the ui.tree node dicts for the plugin hierarchy with status colors."""
    if statuses is None:
        statuses = engine.db.latest_statuses()
    nodes = []
    for p in plugins:
        status = statuses.get(p.id, 'offline')
        node = {
            'id': p.id,
            'label': p.name,
            'icon': 'circle',
            'color': STATUS_COLORS[status],
        }
        if p.children:
            node['children'] = build_tree_nodes(engine, p.children, statuses)
        nodes.append(node)
    return nodes


def find_plugin_by_id(plugins, target_id):
    """Finds a plugin anywhere in the hierarchy by its id."""
    for p in plugins:
        if p.id == target_id:
            return p
        found = find_plugin_by_id(p.children, target_id)
        if found:
            return found
    return None


def _apply_nav_selection(nav_items: Dict[str, Any], current_view: str):
    """Marks the nav item for the current view as selected."""
    # A sidebar item is a place you are, so selection is elevation (halon-item-selected), not an accent fill.
    for view, item in nav_items.items():
        selected = current_view == view
        item.classes(add='halon-item-selected' if selected else None,
                     remove=None if selected else 'halon-item-selected')


def _render_resize_handle(engine: EngineLike):
    """Renders the drag gutter that resizes the drawer and persists the width."""
    resize_handle = ui.element('div').classes('halon-resize-gutter').style(
        'position: absolute; top: 0; right: 0; width: 6px; height: 100%; '
        'cursor: ew-resize; z-index: 2000;'
    )
    resize_handle.on('mousedown', js_handler=DRAWER_RESIZE_JS)
    ui.on('drawer_resized', lambda e: _save_drawer_width(engine, int(e.args)))


def _render_nav_items(switch_view: Callable) -> Dict[str, Any]:
    """Renders the fixed nav list and returns its items keyed by view name."""
    with ui.list().classes('w-full').props('dense'):
        return {
            'overview': ui.item('All Monitors', on_click=lambda: switch_view('overview')).props('clickable dense'),
            'events': ui.item('Events', on_click=lambda: switch_view('events')).props('clickable dense'),
        }


def _render_monitor_tree(engine: EngineLike, switch_view: Callable):
    """Renders the monitor tree with live status dots and persisted expansion."""
    def handle_select(e):
        if e.value:
            target_plugin = find_plugin_by_id(engine.plugins, e.value)
            if target_plugin:
                switch_view('plugin', target_plugin)

    tree = ui.tree(nodes=build_tree_nodes(engine, engine.plugins), on_select=handle_select).classes('w-full')
    tree.add_slot('default-header', _TREE_HEADER_SLOT)

    async def refresh_tree():
        new_nodes = await offload(build_tree_nodes)(engine, engine.plugins)
        if new_nodes != ng.tree_nodes(tree):
            ng.set_tree_nodes(tree, new_nodes)
            tree.update()

    on_data_event(refresh_tree, run_now=False)

    def _save_expanded(e):
        ids = e.args if isinstance(e.args, list) else []
        engine.db.set_setting('tree_expanded', json.dumps(ids))

    ng.set_tree_expanded(tree, _load_expanded(engine))
    tree.update()
    tree.on('update:expanded', _save_expanded)


def render_sidebar(engine: EngineLike, switch_view: Callable):
    """Renders the left drawer and returns it with a sync_nav(current_view) callback."""
    drawer_width = _load_drawer_width(engine)
    with ui.left_drawer(value=True).classes('p-0').props(f'width={drawer_width}') as left_drawer:
        _render_resize_handle(engine)
        nav_items = _render_nav_items(switch_view)
        ui.label('Monitors').classes('halon-sidebar-label')
        _render_monitor_tree(engine, switch_view)

    def sync_nav(current_view: str):
        _apply_nav_selection(nav_items, current_view)

    return left_drawer, sync_nav
