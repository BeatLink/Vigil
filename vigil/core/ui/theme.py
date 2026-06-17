"""
Centralized theme and styling configuration for the Vigil UI.
"""

# Status Color Mapping (Hex codes for consistency across components)
COLOR_MAP = {
    'success': '#22c55e',  # Green-500
    'warning': '#f59e0b',  # Amber-500
    'fail': '#ef4444',     # Red-500
    'inactive': '#9ca3af'  # Gray-400
}

# Logic for status aggregation
SEVERITY_ORDER = {
    'success': 0,
    'inactive': 1,
    'warning': 2,
    'fail': 3
}

# Layout and Brand Colors
BG_PAGE = '#f8f9fa'
CHART_PRIMARY = '#00acff'
TEXT_MUTED = 'text-gray-400'
HEADER_BG = "#00ACFF" #'#1e293b'  # Slate-800
HEADER_TEXT = '#ffffff'