"""Dashboard shell: page routing, header and the view container."""
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote
from nicegui import app, ui
from vigil.core.contracts import EngineLike
from . import theme
from .auth import LOGOUT_PATH, AuthConfig
from .sidebar import render_sidebar
from .views import render_overview, render_events, render_plugin_detail
from .views.notifications import open_notifications_dialog

_ICON = Path(__file__).parent / 'static' / 'icon.svg'

_navigation_state = {'switch_func': None}


def navigate_to(plugin_instance: Any):
    """Switches the dashboard to a plugin's detail view, or the overview for None."""
    if _navigation_state['switch_func']:
        if plugin_instance is None:
            _navigation_state['switch_func']('overview')
        else:
            _navigation_state['switch_func']('plugin', plugin_instance)


def view_path(view_type: str, plugin: Optional[Any] = None) -> str:
    """The address of a dashboard view, so each monitor's page can be linked to."""
    if view_type == 'events':
        return '/events'
    if view_type == 'plugin' and plugin is not None:
        return f"/monitor/{quote(plugin.id, safe='')}"
    return '/'


def _find_monitor(plugins: list, monitor_id: str) -> Optional[Any]:
    """The monitor with this id anywhere in the tree, or None."""
    stack = list(plugins)
    while stack:
        plugin = stack.pop()
        if plugin.id == monitor_id:
            return plugin
        stack.extend(plugin.children)
    return None


# The views switch in place, so going back re-renders the page for the address it returns to.
_RELOAD_ON_BACK = '<script>window.addEventListener("popstate", () => location.reload());</script>'


@dataclass
class DashboardState:
    """Per-client view state: what the main area shows and how to redraw it."""
    current_view: str = 'overview'
    selected_plugin: Any = None
    render_main: Optional[Callable[[], None]] = None
    sync_nav: Optional[Callable[[], None]] = None


def _register_endpoints(engine: EngineLike) -> Optional[AuthConfig]:
    """Registers auth, the REST API and the agent push endpoint on the app,
    returning the auth config so the header can offer a sign-out."""
    from vigil.core.ui.auth import register_auth
    auth_config = register_auth(app, engine.config_loader.auth_settings)

    # A broken API module must not stop the dashboard from starting.
    try:
        from vigil.core.ui.api import register_api
        register_api(app, engine)
    except Exception as e:
        logging.error(f"Failed to register REST API — /api endpoints will be unavailable: {e}")

    # A broken agent endpoint must not stop the dashboard from starting.
    try:
        from vigil.core.ui.agent_endpoint import register_agent_endpoint
        register_agent_endpoint(app, engine.connectors.agents)
    except Exception as e:
        logging.error(f"Failed to register the agent endpoint — agents will be unable to connect: {e}")

    return auth_config


def _render_account_menu(auth_config: AuthConfig):
    """Renders the signed-in account menu and its sign-out item."""
    with ui.button(icon='account_circle', color=None).props('flat dense round'):
        ui.tooltip(f"Signed in as {auth_config.username}")
        with ui.menu():
            ui.label(auth_config.username).classes('px-4 py-2 text-xs').style(
                f'color: {theme.TEXT_SECONDARY}'
            )
            ui.separator()
            ui.menu_item('Sign out', on_click=lambda: ui.navigate.to(LOGOUT_PATH))


def _render_header(engine: EngineLike, left_drawer_toggle: Callable,
                   auth_config: Optional[AuthConfig]):
    """Renders the top bar: drawer toggle, brand, notifications and the account menu."""
    with ui.header().classes('items-center gap-2'):
        ui.button(on_click=left_drawer_toggle, icon='menu', color=None).props('flat dense round')
        ui.image('/icon.svg').style('width: 18px; height: 18px;')
        ui.label('Vigil').classes('halon-brand')
        ui.space()
        if engine.notifications.channels:
            with ui.button(on_click=lambda: open_notifications_dialog(engine), icon='notifications',
                           color=None).props('flat dense round'):
                ui.tooltip('Notifications')
        if auth_config is not None:
            _render_account_menu(auth_config)


def _render_index(engine: EngineLike, auth_config: Optional[AuthConfig] = None,
                  view: str = 'overview', plugin: Optional[Any] = None):
    """Renders the dashboard page: header, sidebar and the switchable main view."""
    theme.install()
    ui.add_head_html(_RELOAD_ON_BACK)

    state = DashboardState(current_view=view, selected_plugin=plugin)

    def switch_view(view_type: str, plugin: Optional[Any] = None):
        path = view_path(view_type, plugin)
        if path != view_path(state.current_view, state.selected_plugin):
            ui.navigate.history.push(path)
        state.current_view = view_type
        state.selected_plugin = plugin
        if state.render_main:
            state.render_main()

    _navigation_state['switch_func'] = switch_view

    _render_header(engine, lambda: left_drawer.toggle(), auth_config)
    left_drawer, sync_nav = render_sidebar(engine, switch_view)
    state.sync_nav = lambda: sync_nav(state.current_view)

    main_container = ui.column().classes('w-full halon-page bg-transparent').style('min-width: 0; flex: 1 1 0;')

    def render_main():
        try:
            main_container.clear()
        except RuntimeError:
            return
        if state.sync_nav:
            state.sync_nav()
        with main_container:
            if state.current_view == 'overview':
                render_overview(engine, switch_view)
            elif state.current_view == 'events':
                render_events(engine, switch_view)
            else:
                render_plugin_detail(engine, switch_view, state.selected_plugin)

    state.render_main = render_main
    render_main()


def init_gui(engine: EngineLike, port: int = 8080):
    """Wires the engine into NiceGUI, registers the endpoints and starts the server."""
    app.on_startup(engine.run)
    app.on_shutdown(engine.shutdown)

    app.add_static_file(local_file=_ICON, url_path='/icon.svg')

    auth_config = _register_endpoints(engine)

    @ui.page('/')
    def index_page():
        _render_index(engine, auth_config)

    @ui.page('/events')
    def events_page():
        _render_index(engine, auth_config, view='events')

    @ui.page('/monitor/{monitor_id}')
    def monitor_page(monitor_id: str):
        plugin = _find_monitor(engine.plugins, monitor_id)
        if plugin is None:
            _render_index(engine, auth_config)
            ui.notify(f'No monitor with id {monitor_id!r}', type='warning')
        else:
            _render_index(engine, auth_config, view='plugin', plugin=plugin)

    svg = _ICON.read_text()
    ui.run(
        title='Vigil', favicon=svg[svg.index('<svg'):], port=port, reload=False, show=False,
    )
