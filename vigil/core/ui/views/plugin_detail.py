"""The plugin detail view: header, action buttons and the plugin's own UI."""

import asyncio
from typing import Any, Callable
from nicegui import ui
from vigil.core.contracts import EngineLike
from ..components import action_button


def render_plugin_detail(engine: EngineLike, switch_view: Callable, plugin: Any):
    """Renders one plugin's page: name header, action row and its render_ui output."""
    header = ui.row().classes('w-full items-center justify-between gap-4 mb-6').style('flex-wrap: wrap;')
    with header:
        with ui.column().classes('min-w-0'):
            ui.label(plugin.name).classes('halon-title-page break-words')
        actions_row = ui.row().classes('gap-2 items-center').style('flex-wrap: wrap;')

    asyncio.create_task(_render_actions(plugin, actions_row))

    plugin.render_ui()


async def _render_actions(plugin: Any, actions_row: Any):
    """Renders the Poll Now button plus the plugin's declared action buttons."""
    with actions_row:
        async def poll_now():
            await plugin.run_cycle()
            ui.notify(f'{plugin.name} polled', type='positive')
        # The one filled button on the view; everything else is a bordered ghost so the accent keeps meaning "the action".
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
