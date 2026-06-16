import logging
from typing import Dict, Any, List
from vigil.core.common.base_plugin import BasePlugin

class GroupPlugin(BasePlugin):
    """
    A container plugin that groups other monitors.
    Provides an aggregated view of the status of its children.
    """
    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)

    async def on_collect(self):
        """Group plugins don't collect data themselves; they act as containers."""
        pass

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False

    def render_ui(self):
        """Render a summary dashboard for the group members."""
        from nicegui import ui
        
        ui.label('Group Members').classes('text-xl font-bold mb-4')
        with ui.grid(columns=3).classes('w-full gap-4'):
            for child in self.children:
                info = child.present()
                with ui.card().classes('p-4 items-center shadow-sm hover:bg-blue-50 cursor-pointer'):
                    ui.icon('sensors', color='blue', size='md')
                    ui.label(info['name']).classes('font-bold')
                    ui.label(info['target']).classes('text-xs text-gray-400')

        with ui.card().classes('w-full mt-6 p-4'):
            ui.label('Group Configuration').classes('font-bold text-gray-500 mb-2')
            ui.json_editor({'content': {'json': self.config}}).props('readonly')