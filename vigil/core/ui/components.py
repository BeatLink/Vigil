from nicegui import ui
from .theme import TEXT_MUTED

def card(classes: str = '', padding: bool = True):
    """A standard container with consistent padding and shadow."""
    p = 'p-4' if padding else 'p-0'
    return ui.card().classes(f'{p} shadow-sm {classes}')

def info_card(title: str, value: str = '--', value_classes: str = 'text-3xl font-black text-slate-500', card_classes: str = 'flex-1'):
    """A card component for displaying a label and a large value."""
    with card(f'{card_classes} items-center justify-center'):
        ui.label(title.upper()).classes(f'text-xs {TEXT_MUTED} font-bold')
        return ui.label(value).classes(value_classes)

def action_button(text: str, on_click=None, icon: str = 'play_arrow'):
    """A standardized button for control actions."""
    return ui.button(text, on_click=on_click).props(f'outline rounded icon={icon}')

def section_title(text: str, classes: str = ''):
    """A standardized heading for dashboard sections."""
    return ui.label(text).classes(f'text-xl font-bold mb-4 {classes}')