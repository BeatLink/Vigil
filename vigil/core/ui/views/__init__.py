"""The three views the dashboard's main area can show."""

from .overview import render_overview
from .events import render_events
from .plugin_detail import render_plugin_detail

__all__ = ['render_overview', 'render_events', 'render_plugin_detail']
