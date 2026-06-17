"""
Centralized theme and styling configuration for the Vigil UI.
"""

# Status Color Mapping (Hex codes for consistency across components)
COLOR_MAP = {
    'online': '#22c55e',   # Green-500
    'warning': '#f59e0b',  # Amber-500
    'failed': '#ef4444',   # Red-500
    'offline': '#9ca3af'   # Gray-400
}

# Logic for status aggregation
SEVERITY_ORDER = {
    'online': 0,
    'offline': 1,
    'warning': 2,
    'failed': 3
}

# Typography Constants
TEXT_XS = 'text-xs'
TEXT_SM = 'text-sm'
TEXT_LG = 'text-lg'
TEXT_XL = 'text-xl'
TEXT_2XL = 'text-2xl'
TEXT_3XL = 'text-3xl'
TEXT_4XL = 'text-4xl'
TEXT_5XL = 'text-5xl'

FONT_BOLD = 'font-bold'
FONT_BLACK = 'font-black'

LABEL_CLASS = f'{TEXT_XS} {FONT_BOLD} text-gray-400'
VALUE_CLASS = f'{TEXT_3XL} {FONT_BLACK} text-slate-500'
HOVER_STYLE = 'hover:bg-blue-50 cursor-pointer'

# Layout and Brand Colors
BG_PAGE = '#f8f9fa'
CHART_PRIMARY = '#00acff'
TEXT_MUTED = 'text-gray-400'
HEADER_BG = "#00ACFF" #'#1e293b'  # Slate-800
HEADER_TEXT = '#ffffff'
SIDEBAR_BG = '#FFFFFF'  # Slate-100
SIDEBAR_TEXT = '#000000' #'#334155' # Slate-700
SIDEBAR_LABEL = '#64748b' # Slate-500